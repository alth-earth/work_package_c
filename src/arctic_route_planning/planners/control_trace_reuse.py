"""Internal control-search trace and fail-closed trace reuse.

The control planner remains the source of truth for route semantics.  This
module carries a compact, deterministic certificate of the successful
label/OPEN writes made before the first goal pop.  It can return an existing
result for an identical query with only tighter elapsed-time/risk limits.

The production carrier deliberately does not retain every write.  Its
ordered rolling digest and conservative maxima are O(1) metadata; an optional
``ControlTraceObserver`` is available to unit tests and diagnostics when the
individual events are needed.  The module is internal and is not re-exported
from ``planners.__init__``.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from arctic_route_planning.grid import Node
    from arctic_route_planning.planners.time_dependent_astar import (
        PlanningRequest,
        PlanningResult,
        State,
        TimeDependentAStar,
    )


TRACE_ALGORITHM_VERSION = "control-trace-v1"
EDGE_EVALUATOR_VERSION = "time-dependent-astar-edge-evaluator-v1"
_EPSILON = 1e-12
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_WRITE_KIND_CODE = {"INITIAL": 0, "INSERT": 1, "REPLACEMENT": 2}
# Stable, network-byte-order carrier for the hot-path ordered digest.  Keeping
# this binary avoids several JSON serializations and temporary SHA objects per
# successful OPEN write.
_TRACE_WRITE_STRUCT = struct.Struct("!QBqqqBqqBqqqBqqd q d d d")


class ControlTraceReuseStatus(StrEnum):
    """Stable status of a trace lookup; no global-optimality claim."""

    HIT_EXACT = "HIT_EXACT"
    HIT_TRACE_EQUIVALENT = "HIT_TRACE_EQUIVALENT"
    MISS_INCOMPATIBLE = "MISS_INCOMPATIBLE"
    FALLBACK_CONTROL = "FALLBACK_CONTROL"
    COLD_CONTROL = "COLD_CONTROL"


class ControlTraceReuseReason(StrEnum):
    """Stable fail-closed reasons for diagnostics and tests."""

    HIT = "HIT"
    NO_TRACE = "NO_TRACE"
    INVALID_TRACE = "INVALID_TRACE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CONSTRAINT_WIDENING = "CONSTRAINT_WIDENING"
    TRACE_VIOLATES_TARGET = "TRACE_VIOLATES_TARGET"
    ROUTE_VIOLATES_TARGET = "ROUTE_VIOLATES_TARGET"


def _json_value(value: Any) -> Any:
    """Convert semantic values to deterministic, repr-free JSON."""

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trace identity datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (frozenset, set)):
        items = [_json_value(item) for item in value]
        return sorted(items, key=_canonical_json)
    if isinstance(value, bytes):
        return value.hex()
    if callable(value):
        raise ValueError("callables are not valid trace identity values")
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("trace digest does not accept non-finite floats")
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    # Never use callable id/repr in an identity.  Unknown values are fenced by
    # stable type only and therefore cannot accidentally pretend to be equal.
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


def _seconds(value: timedelta | float | int | None) -> float | None:
    if value is None:
        return None
    result = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError("maximum_elapsed must be finite and positive")
    return result


def _risk(value: float | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError("maximum_risk must be finite and in [0, 1]")
    return result


def _identity_digest(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and len(value) == 64:
        try:
            int(value, 16)
        except ValueError:
            pass
        else:
            return value.lower()
    return _digest(value)


@dataclass(frozen=True, slots=True)
class ControlTraceIdentity:
    """Stable identity fence, excluding only the two monotone limits."""

    start: Node
    goal: Node
    departure_time: datetime
    objective: str
    risk_identity_digest: str
    external_identity_digest: str | None
    planner_config_digest: str
    model_digest: str
    grid_digest: str
    edge_evaluator_digest: str
    time_bucket_seconds: float
    edge_sample_count: int
    use_heuristic: bool
    max_expansions: int
    progress_interval_seconds: float | None
    algorithm_version: str = TRACE_ALGORITHM_VERSION
    maximum_elapsed_seconds: float | None = None
    maximum_risk: float | None = None

    def __post_init__(self) -> None:
        if self.departure_time.tzinfo is None or self.departure_time.utcoffset() is None:
            raise ValueError("trace identity departure_time must be timezone-aware")
        object.__setattr__(self, "departure_time", self.departure_time.astimezone(UTC))
        object.__setattr__(self, "objective", str(self.objective))
        object.__setattr__(self, "maximum_elapsed_seconds", _seconds(self.maximum_elapsed_seconds))
        object.__setattr__(self, "maximum_risk", _risk(self.maximum_risk))
        if self.time_bucket_seconds <= 0 or not isfinite(self.time_bucket_seconds):
            raise ValueError("time_bucket_seconds must be finite and positive")
        if self.edge_sample_count < 3 or self.max_expansions < 1:
            raise ValueError("trace identity contains invalid search settings")

    @classmethod
    def from_planner(
        cls,
        planner: TimeDependentAStar,
        request: PlanningRequest,
        *,
        identity: Any = None,
    ) -> ControlTraceIdentity:
        """Build a process-independent identity from planner/request semantics."""

        grid = planner.grid
        grid_payload = {
            "latitudes": tuple(float(value) for value in grid.latitudes),
            "longitudes": tuple(float(value) for value in grid.longitudes),
            "allow_diagonal": bool(getattr(grid, "allow_diagonal", False)),
        }
        config_payload = {
            "planner_config": planner.planner_config,
            "weights": planner._weights,
            "full_turn_penalty_hours": planner._full_turn_penalty_hours,
        }
        risk_identity = getattr(planner, "risk_identity", None)
        if risk_identity is None:
            raise ValueError("planner has no stable risk identity")
        return cls(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=request.objective.value,
            risk_identity_digest=_digest(risk_identity),
            external_identity_digest=_identity_digest(identity),
            planner_config_digest=_digest(config_payload),
            model_digest=_digest(planner.vessel_model),
            grid_digest=_digest(grid_payload),
            edge_evaluator_digest=_digest(
                {
                    "method": "TimeDependentAStar._evaluate_edge",
                    "version": EDGE_EVALUATOR_VERSION,
                }
            ),
            time_bucket_seconds=request.time_bucket_size.total_seconds(),
            edge_sample_count=request.edge_sample_count,
            use_heuristic=bool(request.use_heuristic),
            max_expansions=request.max_expansions,
            progress_interval_seconds=request.progress_interval_seconds,
            maximum_elapsed_seconds=_seconds(request.maximum_elapsed),
            maximum_risk=_risk(request.maximum_risk),
        )

    @property
    def base_digest(self) -> str:
        return _digest(
            replace(self, maximum_elapsed_seconds=None, maximum_risk=None)
        )

    @property
    def digest(self) -> str:
        return _digest(self)


@dataclass(frozen=True, slots=True)
class ControlTraceWrite:
    """One successful label/OPEN write for optional test observation."""

    sequence: int
    write_kind: str
    state_digest: str
    label_digest: str
    open_digest: str
    path_elapsed_seconds: float
    path_max_edge_risk: float
    transient: bool = False
    digest: str = ""

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("trace write sequence must be non-negative")
        for value in (self.path_elapsed_seconds, self.path_max_edge_risk):
            if not isfinite(float(value)) or float(value) < 0:
                raise ValueError("trace write envelope must be finite and non-negative")
        if not self.digest:
            object.__setattr__(self, "digest", _digest(self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "write_kind": self.write_kind,
            "state_digest": self.state_digest,
            "label_digest": self.label_digest,
            "open_digest": self.open_digest,
            "path_elapsed_seconds": self.path_elapsed_seconds,
            "path_max_edge_risk": self.path_max_edge_risk,
            "transient": self.transient,
        }

    def with_transient(self, transient: bool) -> ControlTraceWrite:
        return ControlTraceWrite(
            sequence=self.sequence,
            write_kind=self.write_kind,
            state_digest=self.state_digest,
            label_digest=self.label_digest,
            open_digest=self.open_digest,
            path_elapsed_seconds=self.path_elapsed_seconds,
            path_max_edge_risk=self.path_max_edge_risk,
            transient=transient,
        )


class ControlTraceObserver:
    """Test-only event sink; not retained by the production certificate."""

    def __init__(self) -> None:
        self.events: list[ControlTraceWrite] = []

    def __call__(self, event: ControlTraceWrite) -> None:
        self.events.append(event)

    def mark_transient(self, sequence: int) -> None:
        if 0 <= sequence < len(self.events):
            self.events[sequence] = self.events[sequence].with_transient(True)

    def finalize(self, live_sequences: set[int]) -> None:
        self.events = [
            event.with_transient(event.transient or event.sequence not in live_sequences)
            for event in self.events
        ]


@dataclass(frozen=True, slots=True)
class ControlTrace:
    """Compact immutable certificate for one completed control search."""

    identity: ControlTraceIdentity
    result: PlanningResult
    ordered_insertion_digest: str
    insertion_count: int
    replacement_count: int
    maximum_inserted_elapsed: float
    maximum_inserted_path_edge_risk: float
    source_route_digest: str
    termination: str = "FIRST_GOAL_POP"
    route_elapsed_seconds: float = 0.0
    route_max_edge_risk: float = 0.0
    certificate_digest: str = ""

    def __post_init__(self) -> None:
        generated_seal = not self.certificate_digest
        if generated_seal:
            object.__setattr__(self, "certificate_digest", self._seal())
        # The collector has just computed ``source_route_digest`` from the
        # immutable result.  Avoid hashing the full route a second time on the
        # traced-search hot path; lookup-time ``assert_valid`` still performs
        # the independent route check before any reuse.
        self._assert_shape()
        if not generated_seal and self.certificate_digest != self._seal():
            raise ValueError("trace certificate seal mismatch")

    def _seal_payload(self) -> dict[str, Any]:
        return {
            "identity": self.identity.digest,
            "ordered_insertion_digest": self.ordered_insertion_digest,
            "insertion_count": self.insertion_count,
            "replacement_count": self.replacement_count,
            "maximum_inserted_elapsed": self.maximum_inserted_elapsed,
            "maximum_inserted_path_edge_risk": self.maximum_inserted_path_edge_risk,
            "source_route_digest": self.source_route_digest,
            "termination": self.termination,
            "route_elapsed_seconds": self.route_elapsed_seconds,
            "route_max_edge_risk": self.route_max_edge_risk,
        }

    def _seal(self) -> str:
        return _digest(self._seal_payload())

    def _assert_shape(self) -> None:
        if self.termination != "FIRST_GOAL_POP":
            raise ValueError("trace must terminate at the first goal pop")
        if len(self.ordered_insertion_digest) != 64:
            raise ValueError("invalid ordered insertion digest")
        try:
            int(self.ordered_insertion_digest, 16)
        except ValueError as exc:
            raise ValueError("invalid ordered insertion digest") from exc
        if (
            self.insertion_count < 1
            or self.replacement_count < 0
            or self.replacement_count > self.insertion_count
        ):
            raise ValueError("invalid trace write counts")
        for value in (
            self.maximum_inserted_elapsed,
            self.maximum_inserted_path_edge_risk,
            self.route_elapsed_seconds,
            self.route_max_edge_risk,
        ):
            if not isfinite(float(value)) or float(value) < 0:
                raise ValueError("invalid trace envelope")
        if (
            self.maximum_inserted_path_edge_risk > 1.0
            or self.route_max_edge_risk > 1.0
        ):
            raise ValueError("trace risk envelope must be in [0, 1]")
    def assert_valid(self) -> None:
        """Reject stale/tampered seals, results, and malformed certificates."""

        self._assert_shape()
        if self.certificate_digest != self._seal():
            raise ValueError("trace certificate seal mismatch")
        if self.source_route_digest != _result_digest(self.result):
            raise ValueError("trace source route digest mismatch")

    @property
    def digest(self) -> str:
        return self.certificate_digest

    @property
    def trace_digest(self) -> str:
        return self.certificate_digest

    @property
    def write_count(self) -> int:
        return self.insertion_count

    @property
    def count(self) -> int:
        return self.insertion_count

    @property
    def ordered_write_digest(self) -> str:
        return self.ordered_insertion_digest

    @property
    def path_elapsed_envelope(self) -> float:
        return self.maximum_inserted_elapsed

    @property
    def path_max_edge_risk_envelope(self) -> float:
        return self.maximum_inserted_path_edge_risk

    @property
    def identity_digest(self) -> str:
        return self.identity.digest

    @property
    def ordered_digest(self) -> str:
        return self.ordered_insertion_digest

    @property
    def max_inserted_elapsed(self) -> float:
        return self.maximum_inserted_elapsed

    @property
    def maximum_inserted_path_risk(self) -> float:
        return self.maximum_inserted_path_edge_risk

    @property
    def route_digest(self) -> str:
        return self.source_route_digest


def _result_digest(result: PlanningResult) -> str:
    """Digest route semantics while excluding wall-clock metrics."""

    return _digest(
        {
            "objective": result.objective.value,
            "steps": tuple(
                {
                    "node": step.node,
                    "longitude": step.longitude,
                    "latitude": step.latitude,
                    "eta": step.eta,
                    "incoming_heading_degrees": step.incoming_heading_degrees,
                    "recommended_speed_knots": step.recommended_speed_knots,
                    "edge_distance_km": step.edge_distance_km,
                    "edge_risk_score": step.edge_risk_score,
                    "edge_maximum_risk": step.edge_maximum_risk,
                    "edge_confidence": step.edge_confidence,
                    "edge_cost": step.edge_cost,
                    "source_risk_ids": step.source_risk_ids,
                }
                for step in result.steps
            ),
            "total_cost_hours": result.total_cost_hours,
            "distance_km": result.distance_km,
            "travel_hours": result.travel_hours,
            "average_risk": result.average_risk,
            "maximum_risk": result.maximum_risk,
            "minimum_confidence": result.minimum_confidence,
            "source_risk_ids": result.source_risk_ids,
        }
    )


# The plan calls the carrier a certificate in prose; keep the internal alias
# available without introducing a second mutable representation.
ControlTraceCertificate = ControlTrace


@dataclass(frozen=True, slots=True)
class ControlTraceReuseOutcome:
    status: ControlTraceReuseStatus
    result: PlanningResult | None = None
    trace: ControlTrace | None = None
    reason: ControlTraceReuseReason = ControlTraceReuseReason.INVALID_TRACE
    used_search: bool = False

    @property
    def hit(self) -> bool:
        return self.status in {
            ControlTraceReuseStatus.HIT_EXACT,
            ControlTraceReuseStatus.HIT_TRACE_EQUIVALENT,
        }

    @property
    def reused(self) -> bool:
        return self.hit

    @property
    def cold_control(self) -> bool:
        return self.status is ControlTraceReuseStatus.COLD_CONTROL

    @property
    def fallback(self) -> bool:
        return self.status in {
            ControlTraceReuseStatus.FALLBACK_CONTROL,
            ControlTraceReuseStatus.COLD_CONTROL,
        }


class ControlTraceCollector:
    """Mutable O(1)-metadata collector owned by one traced invocation."""

    def __init__(
        self,
        planner: TimeDependentAStar,
        request: PlanningRequest,
        *,
        identity: Any = None,
        observer: Callable[[ControlTraceWrite], None] | None = None,
    ) -> None:
        self.identity = ControlTraceIdentity.from_planner(planner, request, identity=identity)
        self._rolling = hashlib.sha256(b"control-trace-ordered-insertions-v1")
        self._insertion_count = 0
        self._replacement_count = 0
        self._maximum_elapsed = 0.0
        self._maximum_path_risk = 0.0
        self._observer = observer
        # Event history and per-state path envelopes are diagnostics-only.
        # The production certificate needs only global maxima and the rolling
        # ordered digest, so its additional memory remains O(1).
        self._current_by_state: dict[Any, int] | None = (
            {} if observer is not None else None
        )
        self._envelope_by_state: dict[Any, tuple[float, float]] | None = (
            {} if observer is not None else None
        )
        self.trace: ControlTrace | None = None

    def record_write(
        self,
        *,
        state: State,
        parent_state: State | None,
        label_cost_hours: float,
        arrival_time: datetime,
        priority: float,
        path_elapsed_seconds: float,
        edge_maximum_risk: float,
        write_kind: str,
    ) -> None:
        edge_risk = float(edge_maximum_risk)
        if parent_state is None:
            path_max_risk = 0.0
        elif self._envelope_by_state is not None:
            _, parent_risk = self._envelope_by_state.get(parent_state, (0.0, 0.0))
            path_max_risk = max(parent_risk, edge_risk)
        else:
            # max(max-risk of every inserted path) equals max(risk of every
            # inserted edge), so no per-state mirror is needed in production.
            path_max_risk = edge_risk
        sequence = self._insertion_count
        replaced = write_kind == "REPLACEMENT"
        if replaced:
            self._replacement_count += 1
            if (
                self._observer is not None
                and self._current_by_state is not None
                and hasattr(self._observer, "mark_transient")
            ):
                self._observer.mark_transient(self._current_by_state[state])
        self._rolling.update(
            _pack_trace_write(
                sequence=sequence,
                write_kind=write_kind,
                state=state,
                parent_state=parent_state,
                label_cost_hours=label_cost_hours,
                arrival_time=arrival_time,
                priority=priority,
                path_elapsed_seconds=float(path_elapsed_seconds),
                edge_maximum_risk=edge_risk,
            )
        )
        self._insertion_count += 1
        self._maximum_elapsed = max(self._maximum_elapsed, float(path_elapsed_seconds))
        self._maximum_path_risk = max(self._maximum_path_risk, edge_risk)
        if self._observer is not None:
            state_digest = _digest(state)
            label_digest = _digest(
                {
                    "state": state_digest,
                    "cost_hours": label_cost_hours,
                    "arrival_time": arrival_time,
                    "path_elapsed_seconds": path_elapsed_seconds,
                    "path_max_edge_risk": path_max_risk,
                }
            )
            event = ControlTraceWrite(
                sequence=sequence,
                write_kind=write_kind,
                state_digest=state_digest,
                label_digest=label_digest,
                open_digest=_digest(
                    {
                        "priority": priority,
                        "label_digest": label_digest,
                        "serial": sequence,
                    }
                ),
                path_elapsed_seconds=float(path_elapsed_seconds),
                path_max_edge_risk=path_max_risk,
            )
            assert self._current_by_state is not None
            assert self._envelope_by_state is not None
            self._current_by_state[state] = sequence
            self._envelope_by_state[state] = (
                event.path_elapsed_seconds,
                event.path_max_edge_risk,
            )
            self._observer(event)

    def finish(
        self,
        result: PlanningResult,
        goal_state: State,
        predecessor: Mapping[State, tuple[State, Any]],
    ) -> ControlTrace:
        if self._observer is not None and hasattr(self._observer, "finalize"):
            final_states: set[Any] = set()
            state: Any = goal_state
            while True:
                final_states.add(state)
                if state not in predecessor:
                    break
                state = predecessor[state][0]
            assert self._current_by_state is not None
            self._observer.finalize(
                {
                    self._current_by_state[route_state]
                    for route_state in final_states
                    if route_state in self._current_by_state
                }
            )
        route_elapsed = (
            result.steps[-1].eta - result.steps[0].eta
        ).total_seconds()
        route_max_risk = result.maximum_risk if len(result.steps) > 1 else 0.0
        return ControlTrace(
            identity=self.identity,
            result=result,
            ordered_insertion_digest=self._rolling.hexdigest(),
            insertion_count=self._insertion_count,
            replacement_count=self._replacement_count,
            maximum_inserted_elapsed=self._maximum_elapsed,
            maximum_inserted_path_edge_risk=self._maximum_path_risk,
            source_route_digest=_result_digest(result),
            route_elapsed_seconds=route_elapsed,
            route_max_edge_risk=route_max_risk,
        )


def _pack_trace_write(
    *,
    sequence: int,
    write_kind: str,
    state: State,
    parent_state: State | None,
    label_cost_hours: float,
    arrival_time: datetime,
    priority: float,
    path_elapsed_seconds: float,
    edge_maximum_risk: float,
) -> bytes:
    """Encode one successful write deterministically for the rolling digest."""

    def parts(value: State | None) -> tuple[int, int, int, int, int, int]:
        if value is None:
            return (0, 0, 0, 0, 0, 0)
        node, bucket, heading = value
        if heading is None:
            return (int(node[0]), int(node[1]), int(bucket), 0, 0, 0)
        return (
            int(node[0]),
            int(node[1]),
            int(bucket),
            1,
            int(heading[0]),
            int(heading[1]),
        )

    state_row, state_col, state_bucket, state_heading, state_dr, state_dc = parts(state)
    parent_row, parent_col, parent_bucket, parent_heading, parent_dr, parent_dc = parts(
        parent_state
    )
    arrival_utc = arrival_time.astimezone(UTC)
    delta = arrival_utc - _EPOCH
    arrival_microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    return _TRACE_WRITE_STRUCT.pack(
        sequence,
        _WRITE_KIND_CODE[write_kind],
        state_row,
        state_col,
        state_bucket,
        state_heading,
        state_dr,
        state_dc,
        int(parent_state is not None),
        parent_row,
        parent_col,
        parent_bucket,
        parent_heading,
        parent_dr,
        parent_dc,
        float(label_cost_hours),
        arrival_microseconds,
        float(priority),
        float(path_elapsed_seconds),
        float(edge_maximum_risk),
    )


def try_reuse(
    trace: ControlTrace | None,
    planner: TimeDependentAStar,
    request: PlanningRequest,
    *,
    identity: Any = None,
) -> ControlTraceReuseOutcome:
    """Return a trace result only when every identity/envelope fence passes."""

    # Cancellation is a control-flow fence and takes precedence even when the
    # supplied research certificate is absent or malformed.
    planner._check_cancelled(request)
    if trace is None:
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.NO_TRACE,
        )
    try:
        trace.assert_valid()
    except (TypeError, ValueError):
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.INVALID_TRACE,
        )
    try:
        target = ControlTraceIdentity.from_planner(planner, request, identity=identity)
    except (AttributeError, TypeError, ValueError):
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.INVALID_TRACE,
        )
    if target.base_digest != trace.identity.base_digest:
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.IDENTITY_MISMATCH,
        )
    if not _tightened(
        trace.identity.maximum_elapsed_seconds,
        target.maximum_elapsed_seconds,
    ) or not _tightened(trace.identity.maximum_risk, target.maximum_risk):
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.CONSTRAINT_WIDENING,
        )
    if (
        target.maximum_elapsed_seconds is not None
        and trace.maximum_inserted_elapsed
        > target.maximum_elapsed_seconds + _EPSILON
    ) or (
        target.maximum_risk is not None
        and trace.maximum_inserted_path_edge_risk > target.maximum_risk + _EPSILON
    ):
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.TRACE_VIOLATES_TARGET,
        )
    if (
        target.maximum_elapsed_seconds is not None
        and trace.route_elapsed_seconds > target.maximum_elapsed_seconds + _EPSILON
    ) or (
        target.maximum_risk is not None
        and trace.route_max_edge_risk > target.maximum_risk + _EPSILON
    ):
        return ControlTraceReuseOutcome(
            status=ControlTraceReuseStatus.MISS_INCOMPATIBLE,
            reason=ControlTraceReuseReason.ROUTE_VIOLATES_TARGET,
        )
    exact = (
        trace.identity.maximum_elapsed_seconds == target.maximum_elapsed_seconds
        and trace.identity.maximum_risk == target.maximum_risk
    )
    return ControlTraceReuseOutcome(
        status=(
            ControlTraceReuseStatus.HIT_EXACT
            if exact
            else ControlTraceReuseStatus.HIT_TRACE_EQUIVALENT
        ),
        result=trace.result,
        trace=trace,
        reason=ControlTraceReuseReason.HIT,
    )


def reuse_or_plan(
    trace: ControlTrace | None,
    planner: TimeDependentAStar,
    request: PlanningRequest,
    *,
    identity: Any = None,
) -> ControlTraceReuseOutcome:
    """Reuse when certified, otherwise explicitly run cold control."""

    outcome = try_reuse(trace, planner, request, identity=identity)
    if outcome.hit:
        return outcome
    result = planner.plan(request)
    status = (
        ControlTraceReuseStatus.COLD_CONTROL
        if outcome.reason is ControlTraceReuseReason.NO_TRACE
        else ControlTraceReuseStatus.FALLBACK_CONTROL
    )
    return ControlTraceReuseOutcome(
        status=status,
        result=result,
        reason=outcome.reason,
        used_search=True,
    )


def trace_plan(
    planner: TimeDependentAStar,
    request: PlanningRequest,
    *,
    identity: Any = None,
    observer: Callable[[ControlTraceWrite], None] | None = None,
) -> tuple[PlanningResult, ControlTrace]:
    """Module-level adapter for the private traced planner entry."""

    return planner._plan_traced(request, identity=identity, observer=observer)


def _tightened(source: float | None, target: float | None) -> bool:
    if source is None:
        return True
    if target is None:
        return False
    return target <= source + _EPSILON


__all__ = [
    "EDGE_EVALUATOR_VERSION",
    "TRACE_ALGORITHM_VERSION",
    "ControlTrace",
    "ControlTraceCertificate",
    "ControlTraceIdentity",
    "ControlTraceObserver",
    "ControlTraceReuseOutcome",
    "ControlTraceReuseReason",
    "ControlTraceReuseStatus",
    "ControlTraceWrite",
    "reuse_or_plan",
    "trace_plan",
    "try_reuse",
]
