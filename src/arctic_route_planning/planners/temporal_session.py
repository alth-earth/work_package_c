"""Active resumable sessions for the P0.1 temporal-label planner.

This module is intentionally not imported from ``planners.__init__``. It is an
internal C execution boundary: every mutable search object belongs to exactly
one objective/session, checkpoints are in-process immutable values, and
restoring a checkpoint requires the complete input/configuration identity to
match.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from heapq import heapify, heappop
from itertools import count
from math import isfinite
from time import perf_counter
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from arctic_route_planning.contracts.codec import risk_frame_content_digest
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.errors import NoRouteError, PlanningCancelled
from arctic_route_planning.planners.errors import EndpointBlockedError, PlanningHorizonExceeded

from .temporal_queue_compaction import (
    QUEUE_COMPACTION_DISABLED_DIGEST,
    TemporalQueueCompactionPolicy,
    is_well_formed_queue_entry,
)

if TYPE_CHECKING:
    from arctic_route_planning.planners.temporal_label_astar import (
        TemporalCandidateResult,
        TemporalDiagnostics,
        TemporalLabelAStar,
    )
    from arctic_route_planning.planners.time_dependent_astar import PlanningRequest


_ALGORITHM_VERSION = "ltcr-tda-temporal-session.v1"
_QUEUE_TYPE = tuple[float, float, int, int, int, int, int, int, datetime, Any]
_INTERNAL_WINDOW_DIGEST_KIND = "internal_sampler_v1"
_COMMITTED_WINDOW_DIGEST_KIND = "committed_window_v1"


class TemporalSessionState(StrEnum):
    READY = "READY"
    PAUSED = "PAUSED"
    GOAL_CERTIFIED = "GOAL_CERTIFIED"
    EXHAUSTED = "EXHAUSTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TemporalSessionError(RuntimeError):
    """Base class for session lifecycle and checkpoint errors."""


class TemporalSessionIdentityMismatch(TemporalSessionError):
    """A checkpoint cannot be used with the supplied planning identity."""


class TemporalSessionRestoreError(TemporalSessionError):
    """A checkpoint is not restorable in its current lifecycle state."""


def _json_value(value: Any) -> Any:
    """Convert frozen project values to deterministic JSON primitives.

    Callable objects are deliberately never represented with ``id`` or
    ``repr``.  Callable identity is separately fingerprinted from code bytes
    and explicit primitive closure values.
    """

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        values = [_json_value(item) for item in value]
        if isinstance(value, (frozenset, set)):
            return sorted(values, key=_canonical_json)
        return values
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not isfinite(value):
        return {"non_finite": str(value)}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _callable_digest(callback: Any) -> str:
    """Fingerprint an evaluator without process-specific callable identity."""

    explicit = getattr(callback, "__temporal_identity__", None)
    if isinstance(explicit, str) and explicit.strip():
        return f"explicit:{explicit.strip()}"
    function = getattr(callback, "__func__", callback)
    code = getattr(function, "__code__", None)
    if code is None:
        return f"type:{type(callback).__module__}.{type(callback).__qualname__}"
    closure: list[Any] = []
    for cell in getattr(function, "__closure__", ()) or ():
        try:
            item = cell.cell_contents
        except ValueError:
            item = None
        if callable(item):
            closure.append({"callable": _callable_digest(item)})
        else:
            closure.append(_json_value(item))
    payload = {
        "module": getattr(function, "__module__", ""),
        "qualname": getattr(function, "__qualname__", ""),
        "code": code.co_code.hex(),
        "consts": _json_value(code.co_consts),
        "names": _json_value(code.co_names),
        "defaults": _json_value(getattr(function, "__defaults__", None)),
        "closure": closure,
    }
    return f"code:{_digest(payload)}"


def _window_digest(planner: Any) -> str:
    frames = planner.risk_sampler.frames
    payload = {
        "frames": [
            {
                "valid_time": frame.valid_time,
                "risk_id": frame.risk_id,
                "content_digest": risk_frame_content_digest(frame),
            }
            for frame in frames
        ],
        "max_frame_gap": getattr(planner.risk_sampler, "_max_frame_gap", None),
    }
    return _digest(payload)


def _grid_digest(planner: Any) -> str:
    grid = planner.grid
    return _digest(
        {
            "latitudes": grid.latitudes,
            "longitudes": grid.longitudes,
            "allow_diagonal": grid.allow_diagonal,
        }
    )


def _planner_config_digest(planner: Any) -> str:
    return _digest(
        {
            "planner_config": planner.planner_config,
            "weights": planner._weights,
            "full_turn_penalty_hours": planner._full_turn_penalty_hours,
        }
    )


def _model_digest(planner: Any) -> str:
    return _digest(planner.vessel_model)


def _identity_payload(identity: TemporalSessionIdentity) -> dict[str, Any]:
    return {
        field.name: _json_value(getattr(identity, field.name))
        for field in fields(identity)
        if field.name != "_session_id"
    }


@dataclass(frozen=True, slots=True)
class TemporalSessionIdentity:
    """Complete fence for one candidate search session.

    ``from_planner`` is the normal constructor.  The defaults intentionally
    remain invalid for restore/create, preventing callers from silently
    omitting a required fence component.
    """

    risk_window_content_digest: str = ""
    risk_window_commit_id: str = ""
    risk_window_digest_kind: str = ""
    sampler_content_digest: str = ""
    input_revision: int = -1
    generation_id: int = -1
    risk_identity_digest: str = ""
    risk_config_digest: str = ""
    planner_config_digest: str = ""
    model_config_digest: str = ""
    objective: ObjectiveMode | None = None
    start: tuple[int, int] | None = None
    goal: tuple[int, int] | None = None
    departure_time: datetime | None = None
    maximum_elapsed_seconds: float | None = None
    maximum_risk: float | None = None
    time_bucket_seconds: float = 0.0
    edge_sample_count: int = 0
    request_max_expansions: int = 0
    use_heuristic: bool = True
    progress_interval_seconds: float | None = None
    grid_digest: str = ""
    vessel_model_digest: str = ""
    full_turn_penalty_hours: float = 0.0
    eta_policy_digest: str = ""
    search_limits_digest: str = ""
    edge_evaluator_digest: str = ""
    dominance_policy_digest: str = ""
    state_bound_policy_digest: str = ""
    heuristic_policy_digest: str = ""
    queue_compaction_policy_digest: str = QUEUE_COMPACTION_DISABLED_DIGEST
    algorithm_version: str = _ALGORITHM_VERSION

    @classmethod
    def from_planner(
        cls,
        planner: Any,
        request: PlanningRequest,
        *,
        input_revision: int = 0,
        risk_window_content_digest: str | None = None,
        risk_window_commit_id: str | None = None,
        edge_evaluator_digest: str | None = None,
    ) -> TemporalSessionIdentity:
        risk_identity = planner.risk_identity
        sampler_digest = _window_digest(planner)
        if (risk_window_content_digest is None) != (risk_window_commit_id is None):
            raise TemporalSessionIdentityMismatch(
                "committed window digest and commit id must be supplied together"
            )
        if risk_window_content_digest is None:
            window_digest = sampler_digest
            commit_id = f"temporal-sampler-sha256-{sampler_digest}"
            digest_kind = _INTERNAL_WINDOW_DIGEST_KIND
        else:
            window_digest = risk_window_content_digest
            commit_id = risk_window_commit_id
            if not _is_sha256(window_digest) or commit_id != (
                f"risk-window-sha256-{window_digest}"
            ):
                raise TemporalSessionIdentityMismatch(
                    "committed window identity is not canonically content-addressed"
                )
            digest_kind = _COMMITTED_WINDOW_DIGEST_KIND
        evaluator = getattr(planner, "_injected_edge_evaluator", None)
        if edge_evaluator_digest is None:
            edge_evaluator_digest = (
                "default:temporal-label-edge-v1"
                if evaluator is None
                else _callable_digest(evaluator)
            )
        return cls(
            risk_window_content_digest=window_digest,
            risk_window_commit_id=commit_id,
            risk_window_digest_kind=digest_kind,
            sampler_content_digest=sampler_digest,
            input_revision=input_revision,
            generation_id=risk_identity.generation_id,
            risk_identity_digest=_digest(risk_identity),
            risk_config_digest=risk_identity.config_digest,
            planner_config_digest=_planner_config_digest(planner),
            model_config_digest=risk_identity.model_config_digest,
            objective=ObjectiveMode(request.objective),
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            maximum_elapsed_seconds=(
                request.maximum_elapsed.total_seconds()
                if request.maximum_elapsed is not None
                else None
            ),
            maximum_risk=request.maximum_risk,
            time_bucket_seconds=request.time_bucket_size.total_seconds(),
            edge_sample_count=request.edge_sample_count,
            request_max_expansions=request.max_expansions,
            use_heuristic=request.use_heuristic,
            progress_interval_seconds=request.progress_interval_seconds,
            grid_digest=_grid_digest(planner),
            vessel_model_digest=_model_digest(planner),
            full_turn_penalty_hours=planner._full_turn_penalty_hours,
            eta_policy_digest=_digest(planner.eta_policy),
            search_limits_digest=_digest(planner.limits),
            edge_evaluator_digest=edge_evaluator_digest,
            dominance_policy_digest=getattr(
                planner,
                "dominance_policy_digest",
                "temporal-dominance-disabled",
            ),
            state_bound_policy_digest=getattr(
                planner,
                "state_bound_policy_digest",
                "temporal-state-bound-disabled",
            ),
            heuristic_policy_digest=getattr(
                planner,
                "heuristic_policy_digest",
                "temporal-heuristic-default",
            ),
            queue_compaction_policy_digest=getattr(
                planner,
                "queue_compaction_policy_digest",
                QUEUE_COMPACTION_DISABLED_DIGEST,
            ),
        )

    @property
    def session_id(self) -> str:
        return _digest(_identity_payload(self))

    @property
    def digest(self) -> str:
        return self.session_id

    def assert_complete(self) -> None:
        if self.input_revision < 0 or self.generation_id < 0:
            raise TemporalSessionIdentityMismatch("session identity is missing revision/generation")
        required = (
            self.risk_window_content_digest,
            self.risk_window_commit_id,
            self.risk_window_digest_kind,
            self.sampler_content_digest,
            self.risk_identity_digest,
            self.risk_config_digest,
            self.planner_config_digest,
            self.model_config_digest,
            self.grid_digest,
            self.vessel_model_digest,
            self.eta_policy_digest,
            self.search_limits_digest,
            self.edge_evaluator_digest,
            self.dominance_policy_digest,
            self.state_bound_policy_digest,
            self.heuristic_policy_digest,
            self.queue_compaction_policy_digest,
        )
        if (
            self.objective is None
            or self.start is None
            or self.goal is None
            or self.departure_time is None
        ):
            raise TemporalSessionIdentityMismatch("session identity is missing request semantics")
        if any(not isinstance(value, str) or not value for value in required):
            raise TemporalSessionIdentityMismatch(
                "session identity is missing a required digest fence"
            )
        if self.time_bucket_seconds <= 0 or self.edge_sample_count < 3:
            raise TemporalSessionIdentityMismatch("session identity has invalid request semantics")
        if not _is_sha256(self.sampler_content_digest):
            raise TemporalSessionIdentityMismatch("session sampler digest is invalid")
        if self.risk_window_digest_kind == _INTERNAL_WINDOW_DIGEST_KIND:
            if (
                self.risk_window_content_digest != self.sampler_content_digest
                or self.risk_window_commit_id
                != f"temporal-sampler-sha256-{self.sampler_content_digest}"
            ):
                raise TemporalSessionIdentityMismatch(
                    "internal sampler window identity is inconsistent"
                )
        elif self.risk_window_digest_kind == _COMMITTED_WINDOW_DIGEST_KIND:
            if (
                not _is_sha256(self.risk_window_content_digest)
                or self.risk_window_commit_id
                != f"risk-window-sha256-{self.risk_window_content_digest}"
            ):
                raise TemporalSessionIdentityMismatch("committed window identity is inconsistent")
        else:
            raise TemporalSessionIdentityMismatch("session window digest kind is invalid")


@dataclass(frozen=True, slots=True)
class TemporalSessionCheckpoint:
    """Immutable in-process snapshot; stale heap entries are intentionally kept."""

    identity: TemporalSessionIdentity
    request: PlanningRequest
    state: TemporalSessionState
    start_sample: Any
    labels: tuple[tuple[Any, float], ...]
    predecessors: tuple[tuple[Any, Any, Any], ...]
    queue: tuple[_QUEUE_TYPE, ...]
    serial_consumed: int
    incumbent_state: Any
    incumbent_cost: float
    diagnostics: TemporalDiagnostics
    heuristic_distances: tuple[tuple[Any, float], ...]
    calm_speed: Any
    compute_ms: float
    result: Any = None
    terminal_error_type: str | None = None
    terminal_error_message: str | None = None
    state_digest: str = ""

    def __post_init__(self) -> None:
        expected = self._calculated_state_digest()
        if self.state_digest and self.state_digest != expected:
            raise TemporalSessionRestoreError("checkpoint state digest mismatch")
        object.__setattr__(self, "state_digest", expected)

    def _calculated_state_digest(self) -> str:
        return _digest(
            {
                "identity": self.identity.session_id,
                "state": self.state,
                "labels": self.labels,
                "predecessors": self.predecessors,
                "queue": self.queue,
                "serial_consumed": self.serial_consumed,
                "incumbent_state": self.incumbent_state,
                "incumbent_cost": self.incumbent_cost,
                "diagnostics": self.diagnostics,
                "heuristic_distances": self.heuristic_distances,
                "calm_speed": self.calm_speed,
            }
        )

    def assert_valid(self) -> None:
        if self.state_digest != self._calculated_state_digest():
            raise TemporalSessionRestoreError("checkpoint state digest mismatch")

    @property
    def digest(self) -> str:
        return self.state_digest


class TemporalSession:
    """One mutable, objective-scoped exact-arrival-time search."""

    __slots__ = (
        "advance_started",
        "compute_ms",
        "context",
        "cost_model",
        "identity",
        "incumbent_cost",
        "incumbent_state",
        "labels",
        "planner",
        "predecessors",
        "queue",
        "queue_compaction_policy",
        "request",
        "result",
        "serial_consumed",
        "start_sample",
        "state",
        "terminal_error",
    )

    def __init__(
        self,
        planner: Any,
        request: PlanningRequest,
        identity: TemporalSessionIdentity,
    ) -> None:
        identity.assert_complete()
        self.planner = planner
        self.request = request
        self.identity = identity
        self.queue_compaction_policy = getattr(
            planner,
            "queue_compaction_policy",
            TemporalQueueCompactionPolicy.disabled(),
        )
        if not isinstance(self.queue_compaction_policy, TemporalQueueCompactionPolicy):
            raise TemporalSessionIdentityMismatch("queue compaction policy type is invalid")
        if self.queue_compaction_policy.digest != identity.queue_compaction_policy_digest:
            raise TemporalSessionIdentityMismatch("queue compaction policy digest mismatch")
        self.state = TemporalSessionState.READY
        self.context = planner._new_execution_context()
        planner._authorize_dominance(
            self.context,
            request,
            input_revision=identity.input_revision,
        )
        planner._authorize_state_bound(
            self.context,
            request,
            input_revision=identity.input_revision,
        )
        planner._check_cancelled(request)
        planner._validate_request_nodes(request)
        self.start_sample = planner._sample_node(request.start, request.departure_time)
        if self.start_sample.hard_mask:
            self.state = TemporalSessionState.FAILED
            raise EndpointBlockedError(f"start node {request.start} is hard-blocked")
        self.cost_model = planner._cost_model(request.objective)
        start_state = (request.start, None, request.departure_time)
        self.labels = {start_state: 0.0}
        self.predecessors = {}
        self.queue: list[_QUEUE_TYPE] = []
        self.serial_consumed = 0
        planner._ensure_queue_capacity(self.queue)
        planner._push_queue(
            self.queue,
            planner._priority(
                request.start,
                request.goal,
                request,
                self.cost_model,
                0.0,
                context=self.context,
            ),
            0.0,
            start_state,
            count(),
        )
        self.serial_consumed = 1
        self.context.diagnostics.heap_pushes = 1
        self.context.diagnostics.queue_peak = 1
        self._observe_queue_profile(start_state)
        self.context.diagnostics.label_peak = 1
        self.incumbent_state = None
        self.incumbent_cost = float("inf")
        self.compute_ms = 0.0
        self.result = None
        self.terminal_error: Exception | None = None
        self.advance_started: float | None = None

    @property
    def session_id(self) -> str:
        return self.identity.session_id

    def advance(self, expansion_slice: int | None = None) -> TemporalCandidateResult | None:
        if expansion_slice is not None and (
            isinstance(expansion_slice, bool) or expansion_slice < 1
        ):
            raise ValueError("expansion_slice must be a positive integer or None")
        if self.state is TemporalSessionState.GOAL_CERTIFIED:
            return self.result
        if self.state in (
            TemporalSessionState.EXHAUSTED,
            TemporalSessionState.FAILED,
            TemporalSessionState.CANCELLED,
        ):
            if self.terminal_error is not None:
                raise self.terminal_error
            return self.result
        started = perf_counter()
        self.advance_started = started
        expanded_this_call = 0
        try:
            if self.request.start == self.request.goal:
                return self._finish_zero()
            while True:
                self.planner._check_cancelled(self.request)
                self.planner._discard_stale(self.queue, self.labels, self.context.diagnostics)
                if self.incumbent_state is not None and (
                    not self.queue or self.incumbent_cost <= self.queue[0][0] + 1e-12
                ):
                    return self._finish_goal()
                if not self.queue:
                    if self.incumbent_state is not None:
                        return self._finish_goal()
                    self.state = TemporalSessionState.EXHAUSTED
                    if self.context.diagnostics.rejected_coverage_edges:
                        self.terminal_error = PlanningHorizonExceeded(
                            "no complete exact-arrival route fits inside the available risk window"
                        )
                    else:
                        self.terminal_error = NoRouteError(
                            "no exact-arrival route satisfies hard, risk, and vessel constraints"
                        )
                    raise self.terminal_error
                if expansion_slice is not None and expanded_this_call >= expansion_slice:
                    self.state = TemporalSessionState.PAUSED
                    return None
                popped = heappop(self.queue)
                self.context.diagnostics.heap_pops += 1
                _, queued_cost, _, _, _, _, _, _, _, state = popped
                current_cost = self.labels.get(state)
                if current_cost is None or queued_cost != current_cost:
                    self.context.diagnostics.stale_pops += 1
                    continue
                self.context.diagnostics.expanded_labels += 1
                expanded_this_call += 1
                if self.context.diagnostics.expanded_labels > self.planner.limits.max_expansions:
                    raise self.planner._limit("expansions", self.planner.limits.max_expansions)
                self.context.diagnostics.unique_labels = len(self.labels)
                self.context.diagnostics.label_peak = max(
                    self.context.diagnostics.label_peak,
                    len(self.labels),
                )
                node, incoming_code, arrival_time = state
                if node == self.request.goal:
                    if current_cost < self.incumbent_cost - 1e-12:
                        self.incumbent_cost = current_cost
                        self.incumbent_state = state
                    continue
                previous_heading = self.planner._previous_heading(node, incoming_code)
                for neighbor in self.planner.grid.neighbors(node):
                    self.planner._check_cancelled(self.request)
                    self.context.diagnostics.edge_evaluations += 1
                    if (
                        self.context.diagnostics.edge_evaluations
                        > self.planner.limits.max_edge_evaluations
                    ):
                        raise self.planner._limit(
                            "edge evaluations",
                            self.planner.limits.max_edge_evaluations,
                        )
                    try:
                        traversal = self.planner._evaluate_edge(
                            node,
                            neighbor,
                            arrival_time,
                            previous_heading,
                            self.request,
                            self.cost_model,
                            context=self.context,
                        )
                    except Exception as error:
                        reason = self._edge_rejection(error)
                        if reason is None:
                            raise
                        self.context.diagnostics.reject(reason)
                        continue
                    if traversal.arrival_time <= arrival_time:
                        self.context.diagnostics.reject("non_increasing_arrival")
                        continue
                    elapsed = traversal.arrival_time - self.request.departure_time
                    if (
                        self.request.maximum_elapsed is not None
                        and elapsed > self.request.maximum_elapsed
                    ):
                        self.context.diagnostics.reject("coverage")
                        continue
                    heading_code = (neighbor[0] - node[0], neighbor[1] - node[1])
                    next_state = (neighbor, heading_code, traversal.arrival_time)
                    if self.planner._should_prune_state_bound(
                        next_state,
                        self.request,
                        context=self.context,
                    ):
                        continue
                    tentative_cost = current_cost + traversal.cost.total_equivalent_hours
                    if self.planner._dominance_maybe_applicable(
                        next_state,
                        context=self.context,
                    ) and self.planner._should_prune_dominated_label(
                        next_state,
                        tentative_cost,
                        self.labels,
                        self.request,
                        context=self.context,
                    ):
                        continue
                    previous = self.labels.get(next_state)
                    if previous is not None and tentative_cost >= previous - 1e-12:
                        continue
                    if previous is None and len(self.labels) >= self.planner.limits.max_labels:
                        raise self.planner._limit("labels", self.planner.limits.max_labels)
                    priority = self.planner._priority(
                        neighbor,
                        self.request.goal,
                        self.request,
                        self.cost_model,
                        tentative_cost,
                        context=self.context,
                    )
                    if self.incumbent_state is not None and priority >= self.incumbent_cost - 1e-12:
                        # The planner's lower bound is admissible, so this
                        # newly generated label cannot improve the incumbent.
                        # Existing/expanded labels remain untouched.
                        self.context.diagnostics.incumbent_pruned += 1
                        continue
                    if previous is not None:
                        self.context.diagnostics.exact_state_replacements += 1
                    self.labels[next_state] = tentative_cost
                    self.predecessors[next_state] = (state, traversal)
                    self.planner._register_temporal_label(
                        next_state,
                        tentative_cost,
                        context=self.context,
                    )
                    self._maybe_compact_queue(
                        self.serial_consumed + 1,
                        force=len(self.queue) + 1 >= self.planner.limits.max_queue,
                    )
                    self.planner._ensure_queue_capacity(self.queue)
                    self.planner._push_queue(
                        self.queue,
                        priority,
                        tentative_cost,
                        next_state,
                        count(self.serial_consumed),
                    )
                    self.serial_consumed += 1
                    self.context.diagnostics.generated_labels += 1
                    self.context.diagnostics.heap_pushes += 1
                    self.context.diagnostics.queue_peak = max(
                        self.context.diagnostics.queue_peak,
                        len(self.queue),
                    )
                    self._observe_queue_profile(next_state)
                    self.context.diagnostics.label_peak = max(
                        self.context.diagnostics.label_peak,
                        len(self.labels),
                    )
        except PlanningCancelled as error:
            self.state = TemporalSessionState.CANCELLED
            self.terminal_error = error
            raise
        except Exception as error:
            if self.state not in (TemporalSessionState.EXHAUSTED, TemporalSessionState.CANCELLED):
                self.state = TemporalSessionState.FAILED
            self.terminal_error = error
            raise
        finally:
            self.compute_ms += (perf_counter() - started) * 1000.0
            self.advance_started = None

    def _observe_queue_profile(self, state: Any) -> None:
        """Record queue growth by exact-arrival elapsed-time bucket."""

        arrival_time = state[2]
        elapsed_hours = max(
            0.0,
            (arrival_time - self.request.departure_time).total_seconds() / 3600.0,
        )
        bucket = int(elapsed_hours)
        profile = self.context.diagnostics.queue_peak_by_elapsed_hour
        profile[bucket] = max(profile.get(bucket, 0), len(self.queue))

    def _maybe_compact_queue(self, event_count: int, *, force: bool = False) -> None:
        """Remove only heap entries proven stale by the current label map.

        The compaction scan is deliberately conservative.  Any malformed
        entry or unhashable state rejects this optimisation and leaves the
        queue untouched; the search therefore falls back to its historical
        lazy stale-pop behaviour instead of guessing which entries are live.
        """

        policy = self.queue_compaction_policy
        if not policy.should_check(event_count, force=force):
            return
        diagnostics = self.context.diagnostics
        diagnostics.queue_compaction_checks += 1
        before = len(self.queue)
        if before == 0:
            return
        live: list[_QUEUE_TYPE] = []
        stale = 0
        try:
            for entry in self.queue:
                if not is_well_formed_queue_entry(entry):
                    raise ValueError("malformed_queue_entry")
                state = entry[-1]
                current_cost = self.labels.get(state)
                if current_cost is None or entry[1] != current_cost:
                    stale += 1
                else:
                    live.append(entry)
        except (TypeError, ValueError, KeyError) as error:
            diagnostics.queue_compaction_rejected += 1
            reason = f"{type(error).__name__}:{error}"
            diagnostics.queue_compaction_rejection_reasons[reason] = (
                diagnostics.queue_compaction_rejection_reasons.get(reason, 0) + 1
            )
            return
        if not policy.qualifies(stale, before):
            return
        self.queue[:] = live
        heapify(self.queue)
        diagnostics.queue_compactions += 1
        diagnostics.queue_compaction_removed += stale

    def _current_compute_ms(self) -> float:
        if self.advance_started is None:
            return self.compute_ms
        return self.compute_ms + (perf_counter() - self.advance_started) * 1000.0

    def _edge_rejection(self, error: Exception) -> str | None:
        from arctic_route_planning.cost import UnnavigableSpeedError
        from arctic_route_planning.planners.eta_refinement import EtaRefinementError
        from arctic_route_planning.planners.temporal_label_astar import (
            _eta_rejection_reason,
            _RejectedEdge,
        )
        from arctic_route_planning.risk import RiskCoverageError, RiskSamplingError

        if isinstance(error, RiskCoverageError):
            return "coverage"
        if isinstance(error, RiskSamplingError):
            return "sampling"
        if isinstance(error, UnnavigableSpeedError):
            return "speed"
        if isinstance(error, EtaRefinementError):
            self.context.diagnostics.eta_failures += 1
            failure_class = error.failure_class
            self.context.diagnostics.eta_failure_reasons[failure_class] = (
                self.context.diagnostics.eta_failure_reasons.get(failure_class, 0) + 1
            )
            return _eta_rejection_reason(error)
        if isinstance(error, _RejectedEdge):
            return error.reason
        return None

    def _finish_goal(self) -> TemporalCandidateResult:
        from arctic_route_planning.planners.temporal_label_astar import TemporalCandidateResult

        result = self.planner._build_result(
            self.request,
            self.incumbent_state,
            self.start_sample,
            self.labels,
            self.predecessors,
            self.context.diagnostics,
            0.0,
            compute_ms=self._current_compute_ms(),
        )
        self.result = TemporalCandidateResult(result, self.context.diagnostics.freeze())
        self.state = TemporalSessionState.GOAL_CERTIFIED
        return self.result

    def _finish_zero(self) -> TemporalCandidateResult:
        from arctic_route_planning.planners.temporal_label_astar import TemporalCandidateResult

        result = self.planner._zero_length_result(
            self.request,
            self.start_sample,
            0.0,
            self.context.diagnostics,
            compute_ms=self._current_compute_ms(),
        )
        self.result = TemporalCandidateResult(result, self.context.diagnostics.freeze())
        self.state = TemporalSessionState.GOAL_CERTIFIED
        return self.result


@dataclass(frozen=True, slots=True)
class TemporalSessionBundle:
    """Independent per-objective sessions; mutable search state is not shared."""

    sessions: Mapping[ObjectiveMode, TemporalSession]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sessions", MappingProxyType(dict(self.sessions)))

    def __getitem__(self, objective: ObjectiveMode | str) -> TemporalSession:
        return self.sessions[ObjectiveMode(objective)]


def create_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> TemporalSession:
    actual = identity or TemporalSessionIdentity.from_planner(planner, request)
    actual.assert_complete()
    committed_digest = (
        actual.risk_window_content_digest
        if actual.risk_window_digest_kind == _COMMITTED_WINDOW_DIGEST_KIND
        else None
    )
    committed_id = (
        actual.risk_window_commit_id
        if actual.risk_window_digest_kind == _COMMITTED_WINDOW_DIGEST_KIND
        else None
    )
    expected_request = TemporalSessionIdentity.from_planner(
        planner,
        request,
        input_revision=actual.input_revision,
        risk_window_content_digest=committed_digest,
        risk_window_commit_id=committed_id,
    )
    if actual != expected_request:
        raise TemporalSessionIdentityMismatch("session identity does not match planner/request")
    return TemporalSession(planner, request, actual)


def advance_session(
    session: TemporalSession,
    *,
    expansion_slice: int | None = None,
) -> TemporalCandidateResult | None:
    return session.advance(expansion_slice)


def checkpoint_session(session: TemporalSession) -> TemporalSessionCheckpoint:
    labels = tuple(sorted(session.labels.items(), key=lambda item: _state_key(item[0])))
    predecessors = tuple(
        (state, previous, traversal)
        for state, (previous, traversal) in sorted(
            session.predecessors.items(),
            key=lambda item: _state_key(item[0]),
        )
    )
    return TemporalSessionCheckpoint(
        identity=session.identity,
        request=_semantic_request(session.request),
        state=session.state,
        start_sample=session.start_sample,
        labels=labels,
        predecessors=predecessors,
        queue=tuple(session.queue),
        serial_consumed=session.serial_consumed,
        incumbent_state=session.incumbent_state,
        incumbent_cost=session.incumbent_cost,
        diagnostics=session.context.diagnostics.freeze(),
        heuristic_distances=tuple(sorted(session.context.heuristic_distances.items())),
        calm_speed=session.context.calm_speed,
        compute_ms=session.compute_ms,
        result=session.result,
        terminal_error_type=(
            type(session.terminal_error).__name__ if session.terminal_error else None
        ),
        terminal_error_message=(str(session.terminal_error) if session.terminal_error else None),
    )


def restore_session(
    planner: TemporalLabelAStar,
    checkpoint: TemporalSessionCheckpoint,
    *,
    request: PlanningRequest | None = None,
    identity: TemporalSessionIdentity | None = None,
) -> TemporalSession:
    checkpoint.assert_valid()
    checkpoint.identity.assert_complete()
    if checkpoint.state not in (TemporalSessionState.READY, TemporalSessionState.PAUSED):
        raise TemporalSessionRestoreError("only READY or PAUSED sessions can be restored")
    if request is None:
        raise TemporalSessionRestoreError("restore requires the current PlanningRequest explicitly")
    restored_request = request
    if identity is not None and identity != checkpoint.identity:
        raise TemporalSessionIdentityMismatch("checkpoint identity fence mismatch")
    committed_digest = (
        checkpoint.identity.risk_window_content_digest
        if checkpoint.identity.risk_window_digest_kind == _COMMITTED_WINDOW_DIGEST_KIND
        else None
    )
    committed_id = (
        checkpoint.identity.risk_window_commit_id
        if checkpoint.identity.risk_window_digest_kind == _COMMITTED_WINDOW_DIGEST_KIND
        else None
    )
    expected = TemporalSessionIdentity.from_planner(
        planner,
        restored_request,
        input_revision=checkpoint.identity.input_revision,
        risk_window_content_digest=committed_digest,
        risk_window_commit_id=committed_id,
    )
    if expected != checkpoint.identity:
        raise TemporalSessionIdentityMismatch("checkpoint identity fence mismatch")
    session = object.__new__(TemporalSession)
    session.planner = planner
    session.request = restored_request
    session.identity = checkpoint.identity
    session.queue_compaction_policy = getattr(
        planner,
        "queue_compaction_policy",
        TemporalQueueCompactionPolicy.disabled(),
    )
    if not isinstance(session.queue_compaction_policy, TemporalQueueCompactionPolicy):
        raise TemporalSessionIdentityMismatch("queue compaction policy type is invalid")
    if session.queue_compaction_policy.digest != checkpoint.identity.queue_compaction_policy_digest:
        raise TemporalSessionIdentityMismatch("queue compaction policy digest mismatch")
    session.state = checkpoint.state
    session.context = planner._new_execution_context()
    planner._authorize_dominance(
        session.context,
        restored_request,
        input_revision=checkpoint.identity.input_revision,
    )
    planner._authorize_state_bound(
        session.context,
        restored_request,
        input_revision=checkpoint.identity.input_revision,
    )
    from arctic_route_planning.planners.temporal_label_astar import _MutableDiagnostics

    session.context.diagnostics = _MutableDiagnostics(
        **{
            field.name: (
                dict(getattr(checkpoint.diagnostics, field.name))
                if field.name
                in {
                    "rejection_reasons",
                    "eta_failure_reasons",
                    "dominance_rejection_reasons",
                    "heuristic_rejection_reasons",
                    "queue_peak_by_elapsed_hour",
                    "queue_compaction_rejection_reasons",
                    "state_bound_rejection_reasons",
                }
                else getattr(checkpoint.diagnostics, field.name)
            )
            for field in fields(checkpoint.diagnostics)
        }
    )
    session.context.heuristic_distances = dict(checkpoint.heuristic_distances)
    session.context.calm_speed = checkpoint.calm_speed
    session.start_sample = checkpoint.start_sample
    session.cost_model = planner._cost_model(restored_request.objective)
    session.labels = dict(checkpoint.labels)
    session.predecessors = {
        state: (previous, traversal) for state, previous, traversal in checkpoint.predecessors
    }
    session.queue = list(checkpoint.queue)
    session.serial_consumed = checkpoint.serial_consumed
    session.incumbent_state = checkpoint.incumbent_state
    session.incumbent_cost = checkpoint.incumbent_cost
    session.compute_ms = checkpoint.compute_ms
    session.result = checkpoint.result
    session.terminal_error = None
    session.advance_started = None
    return session


def create_session_bundle(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    objectives: Iterable[ObjectiveMode | str] = tuple(ObjectiveMode),
) -> TemporalSessionBundle:
    sessions = {
        mode: create_session(planner, replace(request, objective=mode))
        for raw_mode in objectives
        for mode in (ObjectiveMode(raw_mode),)
    }
    return TemporalSessionBundle(sessions)


def _state_key(state: Any) -> tuple[Any, ...]:
    node, heading, arrival = state
    heading_key = heading if heading is not None else (0, 0)
    return node[0], node[1], heading_key[0], heading_key[1], arrival


def _semantic_request(request: PlanningRequest) -> PlanningRequest:
    """Drop process-local callbacks before placing a request in a checkpoint."""

    return replace(request, cancel_check=None)


__all__ = [
    "TemporalSession",
    "TemporalSessionBundle",
    "TemporalSessionCheckpoint",
    "TemporalSessionError",
    "TemporalSessionIdentity",
    "TemporalSessionIdentityMismatch",
    "TemporalSessionRestoreError",
    "TemporalSessionState",
    "advance_session",
    "checkpoint_session",
    "create_session",
    "create_session_bundle",
    "restore_session",
]
