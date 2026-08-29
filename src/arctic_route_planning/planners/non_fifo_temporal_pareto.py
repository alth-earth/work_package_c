"""Actual temporal-edge bridge for the finite non-FIFO Pareto sidecar.

This module is an explicit C-internal research path.  It adapts the real
``TemporalLabelAStar`` edge evaluator to the finite exact-arrival Pareto
session without changing the production planner, its default policies, or
any route contract.  The bridge keeps the incoming heading in the state and
therefore does not accidentally erase turn-dependent future behaviour.  An
explicit certified heuristic may order the finite queue, but it never removes
a label and is never enabled implicitly.

The vector is additive and objective-scoped.  Its first component is the
ordinary equivalent-hours total, followed by the raw business cost
components.  The first component makes the selected label comparable to the
scalar objective while the remaining components make trade-offs and safe
same-exact-state pruning auditable.  Different exact arrival times are never
compared by this module except through the finite sidecar's exact-state rule.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from arctic_route_planning.cost import CostBreakdown, UnnavigableSpeedError
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.risk import RiskCoverageError, RiskSamplingError

from .eta_refinement import EtaRefinementError
from .non_fifo_feasibility import (
    NonFifoBusinessEvidence,
    NonFifoEvaluationSkipped,
    NonFifoParetoCheckpoint,
    NonFifoParetoFrontierCertificate,
    NonFifoParetoIncumbentBoundCertificate,
    NonFifoParetoLabel,
    NonFifoParetoSearchResult,
    NonFifoParetoSession,
    NonFifoParetoSessionIdentity,
    NonFifoParetoTerminalBoundCertificate,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    certify_non_fifo_pareto_frontier,
    create_non_fifo_pareto_session,
    restore_non_fifo_pareto_session,
)
from .temporal_bounds import TemporalStateBoundCertificate
from .temporal_heuristic_bounds import TemporalHeuristicCertificate
from .temporal_label_astar import TemporalLabelAStar, _eta_rejection_reason, _RejectedEdge
from .temporal_qualification import TemporalScope
from .time_dependent_astar import PlanningRequest, _EdgeTraversal


class NonFifoTemporalParetoError(ValueError):
    """The actual Pareto bridge was invoked outside its research fence."""


class TemporalParetoComponent(StrEnum):
    """Stable names for the additive research vector components."""

    TOTAL_EQUIVALENT_HOURS = "total_equivalent_hours"
    TRAVEL_HOURS = "travel_hours"
    RISK_EXPOSURE_HOURS = "risk_exposure_hours"
    DISTANCE_EQUIVALENT_HOURS = "distance_equivalent_hours"
    TURN_EQUIVALENT_HOURS = "turn_equivalent_hours"
    DEVIATION_EQUIVALENT_HOURS = "deviation_equivalent_hours"
    LOW_CONFIDENCE_HOURS = "low_confidence_hours"


TEMPORAL_PARETO_COMPONENTS = tuple(TemporalParetoComponent)
TEMPORAL_PARETO_SCHEMA = "c.p0.2-temporal-pareto-bridge.v1"
_STATE_BOUND_DISABLED_DIGEST = "temporal-state-bound-disabled"
_INCUMBENT_BOUND_DISABLED_DIGEST = "non-fifo-pareto-incumbent-bound-disabled"
_HEURISTIC_DISABLED_DIGEST = "non-fifo-pareto-heuristic-disabled"
_HEADING_NONE: tuple[int, int] | None = None
type TemporalParetoState = tuple[tuple[int, int], tuple[int, int] | None]


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not isfinite(value):
        raise ValueError("temporal Pareto evidence contains a non-finite float")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TemporalParetoStepEvidence:
    """Business evidence for one accepted actual temporal edge."""

    start: tuple[int, int]
    end: tuple[int, int]
    eta: datetime
    heading_degrees: float
    speed_knots: float
    distance_km: float
    risk_score: float
    maximum_risk: float
    confidence: float
    cost: CostBreakdown
    source_risk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.eta.tzinfo is None or self.eta.utcoffset() is None:
            raise ValueError("Pareto step ETA must be timezone-aware")
        object.__setattr__(self, "eta", self.eta.astimezone(UTC))
        for name in (
            "heading_degrees",
            "speed_knots",
            "distance_km",
            "risk_score",
            "maximum_risk",
            "confidence",
        ):
            value = getattr(self, name)
            if not isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.risk_score > 1 or self.maximum_risk > 1 or self.confidence > 1:
            raise ValueError("risk and confidence values must be at most one")
        object.__setattr__(self, "source_risk_ids", tuple(self.source_risk_ids))

    @classmethod
    def from_traversal(cls, traversal: _EdgeTraversal) -> TemporalParetoStepEvidence:
        return cls(
            start=traversal.start,
            end=traversal.end,
            eta=traversal.arrival_time,
            heading_degrees=traversal.heading_degrees,
            speed_knots=traversal.speed_knots,
            distance_km=traversal.distance_km,
            risk_score=traversal.risk_score,
            maximum_risk=traversal.maximum_risk,
            confidence=traversal.confidence,
            cost=traversal.cost,
            source_risk_ids=traversal.source_risk_ids,
        )

    @property
    def business(self) -> NonFifoBusinessEvidence:
        return NonFifoBusinessEvidence(
            speed_knots=self.speed_knots,
            risk_score=self.risk_score,
            maximum_risk=self.maximum_risk,
            confidence=self.confidence,
            source_ids=self.source_risk_ids,
        )

    @property
    def vector(self) -> tuple[float, ...]:
        cost = self.cost
        return (
            cost.total_equivalent_hours,
            cost.travel_hours,
            cost.risk_exposure_hours,
            cost.distance_equivalent_hours,
            cost.turn_equivalent_hours,
            cost.deviation_equivalent_hours,
            cost.low_confidence_hours,
        )


@dataclass(frozen=True, slots=True)
class TemporalParetoRoute:
    """Research-only route reconstructed from an exact-arrival label."""

    states: tuple[TemporalParetoState, ...]
    arrival_times: tuple[datetime, ...]
    costs: tuple[float, ...]
    steps: tuple[TemporalParetoStepEvidence, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "states", tuple(self.states))
        object.__setattr__(self, "arrival_times", tuple(self.arrival_times))
        object.__setattr__(self, "costs", tuple(self.costs))
        object.__setattr__(self, "steps", tuple(self.steps))
        if len(self.states) != len(self.steps) + 1:
            raise ValueError("Pareto route state/step lengths do not match")
        if len(self.arrival_times) != len(self.states):
            raise ValueError("Pareto route state/arrival lengths do not match")
        if not self.costs or any(not isfinite(value) or value < 0 for value in self.costs):
            raise ValueError("Pareto route costs must be finite and non-negative")
        for value in self.arrival_times:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Pareto route arrivals must be timezone-aware")
        object.__setattr__(
            self,
            "arrival_times",
            tuple(value.astimezone(UTC) for value in self.arrival_times),
        )

    @property
    def nodes(self) -> tuple[tuple[int, int], ...]:
        return tuple(state[0] for state in self.states)

    @property
    def semantic_digest(self) -> str:
        return _digest(
            {
                "schema": TEMPORAL_PARETO_SCHEMA,
                "states": self.states,
                "arrival_times": self.arrival_times,
                "costs": self.costs,
                "steps": self.steps,
            }
        )


@dataclass(frozen=True, slots=True)
class NonFifoTemporalParetoResult:
    """Actual-edge Pareto result without pretending to be a PlanningResult."""

    status: NonFifoSearchStatus
    selected: TemporalParetoRoute | None
    frontier: tuple[TemporalParetoRoute, ...]
    raw_result: NonFifoParetoSearchResult
    scope_digest: str
    session_identity: str
    diagnostics: Any = None

    @property
    def session_id(self) -> str:
        return self.session_identity

    @property
    def semantic_digest(self) -> str | None:
        return self.selected.semantic_digest if self.selected is not None else None

    @property
    def frontier_digest(self) -> str:
        return _digest(
            {
                "schema": TEMPORAL_PARETO_SCHEMA,
                "status": self.status,
                "scope_digest": self.scope_digest,
                "frontier": self.frontier,
                "raw_frontier_digest": self.raw_result.frontier_digest,
                "frontier_complete": self.raw_result.frontier_complete,
                "selection_only": self.raw_result.selection_only,
            }
        )

    @property
    def pareto_pruned(self) -> int:
        return self.raw_result.pareto_pruned

    @property
    def evaluator_errors(self) -> tuple[str, ...]:
        return self.raw_result.evaluator_errors

    @property
    def reason(self) -> str | None:
        return self.raw_result.reason

    @property
    def incumbent_bound_pruned(self) -> int:
        return self.raw_result.incumbent_bound_pruned

    @property
    def incumbent_bound_rejected(self) -> int:
        return self.raw_result.incumbent_bound_rejected

    @property
    def incumbent_bound_digest(self) -> str:
        return self.raw_result.incumbent_bound_digest

    @property
    def incumbent_bound_rejection_reasons(self) -> tuple[tuple[str, int], ...]:
        return self.raw_result.incumbent_bound_rejection_reasons

    @property
    def frontier_complete(self) -> bool:
        """Whether the result contains a complete exact-arrival frontier."""

        return self.raw_result.frontier_complete

    @property
    def selection_only(self) -> bool:
        """Whether an explicit terminal bound limited this to one selection."""

        return self.raw_result.selection_only


@dataclass(frozen=True, slots=True)
class NonFifoTemporalParetoCheckpoint:
    """Research checkpoint with a bridge/schema/scope digest fence."""

    pareto_checkpoint: NonFifoParetoCheckpoint
    scope_digest: str
    component_digest: str
    state_bound_digest: str = _STATE_BOUND_DISABLED_DIGEST
    state_bound_checks: int = 0
    state_bound_pruned: int = 0
    state_bound_arrival_pruned: int = 0
    state_bound_rejected: int = 0
    state_bound_rejection_reasons: tuple[tuple[str, int], ...] = ()
    incumbent_bound_digest: str = _INCUMBENT_BOUND_DISABLED_DIGEST
    schema_version: str = TEMPORAL_PARETO_SCHEMA
    state_digest: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != TEMPORAL_PARETO_SCHEMA:
            raise NonFifoTemporalParetoError("unsupported actual Pareto checkpoint schema")
        if not isinstance(self.state_bound_digest, str) or not self.state_bound_digest:
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint state-bound digest is invalid"
            )
        if not isinstance(self.incumbent_bound_digest, str) or not self.incumbent_bound_digest:
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint incumbent-bound digest is invalid"
            )
        if self.scope_digest != self.pareto_checkpoint.identity.scope_digest:
            raise NonFifoTemporalParetoError("actual Pareto checkpoint scope identity mismatch")
        if (
            self.incumbent_bound_digest
            != self.pareto_checkpoint.identity.incumbent_bound_digest
        ):
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint incumbent-bound identity mismatch"
            )
        for name in (
            "state_bound_checks",
            "state_bound_pruned",
            "state_bound_arrival_pruned",
            "state_bound_rejected",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise NonFifoTemporalParetoError(f"actual Pareto checkpoint {name} is invalid")
        reasons = tuple(self.state_bound_rejection_reasons)
        if any(
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or item[1] < 0
            for item in reasons
        ):
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint state-bound rejection reasons are invalid"
            )
        object.__setattr__(self, "state_bound_rejection_reasons", reasons)
        expected = self._calculated_state_digest()
        if self.state_digest and self.state_digest != expected:
            raise NonFifoTemporalParetoError("actual Pareto checkpoint digest mismatch")
        object.__setattr__(self, "state_digest", expected)

    def _calculated_state_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "scope_digest": self.scope_digest,
                "component_digest": self.component_digest,
                "state_bound_digest": getattr(
                    self, "state_bound_digest", _STATE_BOUND_DISABLED_DIGEST
                ),
                "state_bound_checks": getattr(self, "state_bound_checks", 0),
                "state_bound_pruned": getattr(self, "state_bound_pruned", 0),
                "state_bound_arrival_pruned": getattr(self, "state_bound_arrival_pruned", 0),
                "state_bound_rejected": getattr(self, "state_bound_rejected", 0),
                "state_bound_rejection_reasons": getattr(self, "state_bound_rejection_reasons", ()),
                "incumbent_bound_digest": getattr(
                    self, "incumbent_bound_digest", _INCUMBENT_BOUND_DISABLED_DIGEST
                ),
                "pareto_checkpoint": self.pareto_checkpoint.digest,
            }
        )

    def assert_valid(self) -> None:
        self.pareto_checkpoint.assert_valid()
        state_bound_digest = getattr(self, "state_bound_digest", _STATE_BOUND_DISABLED_DIGEST)
        if not isinstance(state_bound_digest, str) or not state_bound_digest:
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint state-bound digest is invalid"
            )
        incumbent_bound_digest = getattr(
            self, "incumbent_bound_digest", _INCUMBENT_BOUND_DISABLED_DIGEST
        )
        if not isinstance(incumbent_bound_digest, str) or not incumbent_bound_digest:
            raise NonFifoTemporalParetoError(
                "actual Pareto checkpoint incumbent-bound digest is invalid"
            )
        if self.state_digest != self._calculated_state_digest():
            raise NonFifoTemporalParetoError("actual Pareto checkpoint digest mismatch")

    @property
    def digest(self) -> str:
        return self.state_digest


class NonFifoTemporalParetoResearchSession:
    """Resumable actual-edge Pareto session under an explicit research fence."""

    __slots__ = (
        "component_digest",
        "context",
        "planner",
        "request",
        "scope",
        "session",
    )

    def __init__(
        self,
        planner: TemporalLabelAStar,
        request: PlanningRequest,
        session: NonFifoParetoSession,
        context: Any,
        scope: TemporalScope,
        component_digest: str,
    ) -> None:
        self.planner = planner
        self.request = request
        self.session = session
        self.context = context
        self.scope = scope
        self.component_digest = component_digest

    @property
    def state(self) -> str:
        return self.session.state.value

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def identity(self) -> NonFifoParetoSessionIdentity:
        return self.session.identity

    @property
    def incumbent_bound_pruned(self) -> int:
        """Number of newly generated labels rejected by the bound."""

        return self.session.incumbent_bound_pruned

    @property
    def incumbent_bound_rejected(self) -> int:
        """Number of bound checks rejected without pruning."""

        return self.session.incumbent_bound_rejected

    @property
    def incumbent_bound_digest(self) -> str:
        """Digest of the installed incumbent-bound certificate or disabled fence."""

        return self.session.identity.incumbent_bound_digest

    @property
    def incumbent_bound_rejection_reasons(self) -> tuple[tuple[str, int], ...]:
        """Stable fail-closed rejection counters for audit evidence."""

        return tuple(sorted(self.session.incumbent_bound_rejection_reasons.items()))

    @property
    def incumbent_bound_authorized(self) -> bool:
        """Whether the supplied incumbent certificate remains authorized."""

        return self.session.incumbent_bound_authorized

    @property
    def frontier_complete(self) -> bool:
        """Whether this session is allowed to claim a complete frontier."""

        return not self.session.incumbent_bound_selection_only

    @property
    def selection_only(self) -> bool:
        """Whether this session uses selected-route terminal pruning."""

        return self.session.incumbent_bound_selection_only

    @property
    def frontier_certificate(self) -> NonFifoParetoFrontierCertificate:
        """Return a complete-frontier certificate after terminal completion.

        The bridge exposes this only on the explicit research session.  A
        paused or ready session has not drained all exact-arrival labels and
        therefore cannot be treated as a complete frontier.
        """

        if self.session.result is None:
            raise NonFifoTemporalParetoError(
                "frontier certificate requires a terminal Pareto session"
            )
        return certify_non_fifo_pareto_frontier(
            self.session.result,
            identity=self.session.identity,
            scope_digest=self.scope.digest,
        )

    def advance(self, expansion_slice: int | None = None) -> NonFifoTemporalParetoResult | None:
        raw = self.session.advance(expansion_slice=expansion_slice)
        if raw is None:
            return None
        return _wrap_result(
            raw, self.scope.digest, self.session.session_id, self.context, self.request
        )

    def run(self) -> NonFifoTemporalParetoResult:
        raw = self.session.run()
        return _wrap_result(
            raw, self.scope.digest, self.session.session_id, self.context, self.request
        )

    def checkpoint(self) -> NonFifoTemporalParetoCheckpoint:
        state_bound = self.context.state_bound_certificate
        diagnostics = self.context.diagnostics.freeze()
        return NonFifoTemporalParetoCheckpoint(
            pareto_checkpoint=self.session.checkpoint(),
            scope_digest=self.scope.digest,
            component_digest=self.component_digest,
            state_bound_digest=(
                state_bound.digest if state_bound is not None else _STATE_BOUND_DISABLED_DIGEST
            ),
            state_bound_checks=diagnostics.state_bound_checks,
            state_bound_pruned=diagnostics.state_bound_pruned,
            state_bound_arrival_pruned=diagnostics.state_bound_arrival_pruned,
            state_bound_rejected=diagnostics.state_bound_rejected,
            state_bound_rejection_reasons=diagnostics.state_bound_rejection_reasons,
            incumbent_bound_digest=self.session.identity.incumbent_bound_digest,
        )


def create_non_fifo_temporal_pareto_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    *,
    pareto_pruning: bool = False,
    skip_expected_rejections: bool = False,
    state_bound_certificate: TemporalStateBoundCertificate | None = None,
    incumbent_bound_certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ) = None,
    heuristic_certificate: TemporalHeuristicCertificate | None = None,
    identity: NonFifoParetoSessionIdentity | None = None,
) -> NonFifoTemporalParetoResearchSession:
    """Create an actual-edge Pareto session for explicit research only."""

    scope = _validate_bridge(
        planner,
        request,
        state_bound_certificate,
        incumbent_bound_certificate,
        heuristic_certificate,
    )
    callbacks, context, component_digest = _callbacks(
        planner,
        request,
        scope,
        skip_expected_rejections=skip_expected_rejections,
        state_bound_certificate=state_bound_certificate,
        incumbent_bound_certificate=incumbent_bound_certificate,
        heuristic_certificate=heuristic_certificate,
    )
    session = create_non_fifo_pareto_session(
        start=(request.start, _HEADING_NONE),
        goal=(request.goal, _HEADING_NONE),
        departure_time=request.departure_time,
        neighbors=callbacks.neighbors,
        evaluate_edge=callbacks.evaluate_edge,
        objective_count=len(TEMPORAL_PARETO_COMPONENTS),
        pareto_pruning=pareto_pruning,
        max_expansions=planner.limits.max_expansions,
        max_labels=planner.limits.max_labels,
        max_queue=planner.limits.max_queue,
        max_edge_evaluations=planner.limits.max_edge_evaluations,
        maximum_elapsed=request.maximum_elapsed,
        cancel_check=request.cancel_check,
        fixture_digest=f"temporal-scope:{scope.digest}",
        config_digest=component_digest,
        scope_digest=scope.digest,
        incumbent_bound_certificate=incumbent_bound_certificate,
        priority=callbacks.priority,
        priority_policy_digest=_heuristic_digest(heuristic_certificate),
        identity=identity,
    )
    return NonFifoTemporalParetoResearchSession(
        planner, request, session, context, scope, component_digest
    )


def restore_non_fifo_temporal_pareto_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    checkpoint: NonFifoTemporalParetoCheckpoint,
    *,
    cancel_check: Any = None,
    skip_expected_rejections: bool = False,
    state_bound_certificate: TemporalStateBoundCertificate | None = None,
    incumbent_bound_certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ) = None,
    heuristic_certificate: TemporalHeuristicCertificate | None = None,
) -> NonFifoTemporalParetoResearchSession:
    """Restore an actual-edge Pareto session after all bridge fences."""

    if not isinstance(checkpoint, NonFifoTemporalParetoCheckpoint):
        raise NonFifoTemporalParetoError("checkpoint type is invalid")
    checkpoint.assert_valid()
    scope = _validate_bridge(
        planner,
        request,
        state_bound_certificate,
        incumbent_bound_certificate,
        heuristic_certificate,
    )
    callbacks, context, component_digest = _callbacks(
        planner,
        request,
        scope,
        skip_expected_rejections=skip_expected_rejections,
        state_bound_certificate=state_bound_certificate,
        incumbent_bound_certificate=incumbent_bound_certificate,
        heuristic_certificate=heuristic_certificate,
    )
    if checkpoint.scope_digest != scope.digest:
        raise NonFifoTemporalParetoError("actual Pareto checkpoint scope mismatch")
    expected_state_bound_digest = _state_bound_digest(state_bound_certificate)
    if checkpoint.state_bound_digest != expected_state_bound_digest:
        raise NonFifoTemporalParetoError("actual Pareto checkpoint state-bound digest mismatch")
    expected_incumbent_bound_digest = _incumbent_bound_digest(incumbent_bound_certificate)
    if checkpoint.incumbent_bound_digest != expected_incumbent_bound_digest:
        raise NonFifoTemporalParetoError(
            "actual Pareto checkpoint incumbent-bound digest mismatch"
        )
    expected_heuristic_digest = _heuristic_digest(heuristic_certificate)
    if checkpoint.pareto_checkpoint.identity.priority_policy_digest != expected_heuristic_digest:
        raise NonFifoTemporalParetoError(
            "actual Pareto checkpoint heuristic policy digest mismatch"
        )
    if checkpoint.component_digest != component_digest:
        raise NonFifoTemporalParetoError("actual Pareto checkpoint component mismatch")
    context.diagnostics.state_bound_checks = checkpoint.state_bound_checks
    context.diagnostics.state_bound_pruned = checkpoint.state_bound_pruned
    context.diagnostics.state_bound_arrival_pruned = checkpoint.state_bound_arrival_pruned
    context.diagnostics.state_bound_rejected = checkpoint.state_bound_rejected
    context.diagnostics.state_bound_rejection_reasons = dict(
        checkpoint.state_bound_rejection_reasons
    )
    session = restore_non_fifo_pareto_session(
        checkpoint.pareto_checkpoint,
        neighbors=callbacks.neighbors,
        evaluate_edge=callbacks.evaluate_edge,
        cancel_check=request.cancel_check if cancel_check is None else cancel_check,
        incumbent_bound_certificate=incumbent_bound_certificate,
        priority=callbacks.priority,
    )
    return NonFifoTemporalParetoResearchSession(
        planner, request, session, context, scope, component_digest
    )


def run_non_fifo_temporal_pareto_search(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    *,
    pareto_pruning: bool = False,
    skip_expected_rejections: bool = False,
    state_bound_certificate: TemporalStateBoundCertificate | None = None,
    incumbent_bound_certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ) = None,
    heuristic_certificate: TemporalHeuristicCertificate | None = None,
) -> NonFifoTemporalParetoResult:
    """Run the actual-edge Pareto sidecar to a terminal state."""

    return create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=pareto_pruning,
        skip_expected_rejections=skip_expected_rejections,
        state_bound_certificate=state_bound_certificate,
        incumbent_bound_certificate=incumbent_bound_certificate,
        heuristic_certificate=heuristic_certificate,
    ).run()


@dataclass(frozen=True, slots=True)
class _Callbacks:
    neighbors: Any
    evaluate_edge: Any
    priority: Any = None


def _state_bound_digest(certificate: TemporalStateBoundCertificate | None) -> str:
    return certificate.digest if certificate is not None else _STATE_BOUND_DISABLED_DIGEST


def _incumbent_bound_digest(
    certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ),
) -> str:
    """Return the explicit incumbent-bound identity, or the disabled fence."""

    return (
        certificate.digest
        if certificate is not None
        else _INCUMBENT_BOUND_DISABLED_DIGEST
    )


def _heuristic_digest(certificate: TemporalHeuristicCertificate | None) -> str:
    return certificate.digest if certificate is not None else _HEURISTIC_DISABLED_DIGEST


def _validate_bridge(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    state_bound_certificate: TemporalStateBoundCertificate | None = None,
    incumbent_bound_certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ) = None,
    heuristic_certificate: TemporalHeuristicCertificate | None = None,
) -> TemporalScope:
    if not isinstance(planner, TemporalLabelAStar):
        raise NonFifoTemporalParetoError("actual Pareto bridge requires TemporalLabelAStar")
    if request.use_heuristic:
        raise NonFifoTemporalParetoError("actual Pareto bridge requires use_heuristic=False")
    if planner.dominance_policy.enabled:
        raise NonFifoTemporalParetoError(
            "actual Pareto bridge requires TemporalDominancePolicy.disabled()"
        )
    if state_bound_certificate is None and planner.state_bound_certificate is not None:
        raise NonFifoTemporalParetoError("actual Pareto bridge rejects state-bound certificates")
    if state_bound_certificate is not None:
        if not isinstance(state_bound_certificate, TemporalStateBoundCertificate):
            raise NonFifoTemporalParetoError("state-bound certificate type is invalid")
        installed = planner.state_bound_certificate
        if installed is not None and installed.digest != state_bound_certificate.digest:
            raise NonFifoTemporalParetoError("state-bound certificate digest mismatch")
    if incumbent_bound_certificate is not None and not isinstance(
        incumbent_bound_certificate,
        (NonFifoParetoIncumbentBoundCertificate, NonFifoParetoTerminalBoundCertificate),
    ):
        raise NonFifoTemporalParetoError("incumbent-bound certificate type is invalid")
    if planner.heuristic_certificate is not None:
        raise NonFifoTemporalParetoError("actual Pareto bridge rejects heuristic certificates")
    if heuristic_certificate is not None and not isinstance(
        heuristic_certificate, TemporalHeuristicCertificate
    ):
        raise NonFifoTemporalParetoError("heuristic certificate type is invalid")
    scope = planner.temporal_scope(request)
    if not scope.evaluator_identity_known:
        raise NonFifoTemporalParetoError("actual Pareto bridge requires known evaluator identity")
    if heuristic_certificate is not None:
        if not heuristic_certificate.usable:
            raise NonFifoTemporalParetoError(
                "heuristic certificate is unusable or incomplete"
            )
        if not heuristic_certificate.permits(scope):
            raise NonFifoTemporalParetoError("heuristic certificate scope mismatch")
        if heuristic_certificate.objective != ObjectiveMode(request.objective).value:
            raise NonFifoTemporalParetoError("heuristic certificate objective mismatch")
    return scope


def _callbacks(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    scope: TemporalScope,
    *,
    skip_expected_rejections: bool,
    state_bound_certificate: TemporalStateBoundCertificate | None,
    incumbent_bound_certificate: (
        NonFifoParetoIncumbentBoundCertificate
        | NonFifoParetoTerminalBoundCertificate
        | None
    ),
    heuristic_certificate: TemporalHeuristicCertificate | None,
) -> tuple[_Callbacks, Any, str]:
    component_digest = _digest(
        {
            "schema": TEMPORAL_PARETO_SCHEMA,
            "components": TEMPORAL_PARETO_COMPONENTS,
            "objective": ObjectiveMode(request.objective),
            "scope": scope.digest,
            "skip_expected_rejections": skip_expected_rejections,
            "state_bound_digest": _state_bound_digest(state_bound_certificate),
            "incumbent_bound_digest": _incumbent_bound_digest(incumbent_bound_certificate),
            "heuristic_policy_digest": _heuristic_digest(heuristic_certificate),
        }
    )
    context = planner._new_execution_context()
    context.state_bound_certificate = state_bound_certificate
    priority = None
    if heuristic_certificate is not None:
        context.heuristic_certificate = heuristic_certificate
        context.heuristic_authorized = True
        context.diagnostics.heuristic_policy = "certified"
        context.diagnostics.heuristic_certificate_digest = heuristic_certificate.digest
        context.diagnostics.heuristic_scope_match = True
    cost_model = planner._cost_model(ObjectiveMode(request.objective))
    token = f"{TEMPORAL_PARETO_SCHEMA}:{scope.digest}:{component_digest}"

    def neighbors(state: TemporalParetoState) -> tuple[TemporalParetoState, ...]:
        node, _heading = _state_parts(state)
        return tuple(
            (
                neighbor,
                _HEADING_NONE
                if neighbor == request.goal
                else (neighbor[0] - node[0], neighbor[1] - node[1]),
            )
            for neighbor in planner.grid.neighbors(node)
        )

    def evaluate_edge(
        state: TemporalParetoState,
        next_state: TemporalParetoState,
        arrival_time: datetime,
    ) -> NonFifoParetoTransition:
        node, incoming_code = _state_parts(state)
        next_node, _next_heading = _state_parts(next_state)
        previous_heading = planner._previous_heading(node, incoming_code)
        try:
            traversal = planner._evaluate_edge(
                node,
                next_node,
                arrival_time,
                previous_heading,
                request,
                cost_model,
                context=context,
            )
        except Exception as error:
            if skip_expected_rejections:
                reason = _expected_rejection_reason(error, context)
                if reason is not None:
                    raise NonFifoEvaluationSkipped(reason) from error
            raise
        if not isinstance(traversal, _EdgeTraversal):
            raise NonFifoTemporalParetoError("actual edge evaluator returned an invalid traversal")
        if state_bound_certificate is not None:
            heading_code = (
                _HEADING_NONE
                if next_node == request.goal
                else (next_node[0] - node[0], next_node[1] - node[1])
            )
            candidate_state = (next_node, heading_code, traversal.arrival_time)
            if planner._should_prune_state_bound(
                candidate_state,
                request,
                context=context,
            ):
                # The finite Pareto sidecar treats this marker as an
                # unavailable edge.  The label has not entered the session,
                # so an already-expanded label is never deleted.
                raise NonFifoEvaluationSkipped("state_bound")
        step = TemporalParetoStepEvidence.from_traversal(traversal)
        return NonFifoParetoTransition(
            arrival_time=traversal.arrival_time,
            costs=step.vector,
            payload={"step": step},
            business=step.business,
        )

    if heuristic_certificate is not None:
        def priority(label: NonFifoParetoLabel) -> float:
            node, _heading = _state_parts(label.node)
            lower_bound = heuristic_certificate.lower_bound(node)
            if lower_bound is None or not isfinite(lower_bound) or lower_bound < 0.0:
                raise NonFifoTemporalParetoError(
                    "heuristic certificate has no finite bound for a queued node"
                )
            value = label.costs[0] + float(lower_bound)
            if not isfinite(value) or value < 0.0:
                raise NonFifoTemporalParetoError(
                    "heuristic priority is non-finite or negative"
                )
            return value

        priority.__non_fifo_identity__ = (
            f"priority:{token}:{heuristic_certificate.digest}"
        )

    neighbors.__non_fifo_identity__ = f"neighbors:{token}"
    evaluate_edge.__non_fifo_identity__ = f"evaluator:{token}"
    return _Callbacks(neighbors, evaluate_edge, priority), context, component_digest


def _expected_rejection_reason(error: Exception, context: Any) -> str | None:
    """Classify only known domain rejections as unavailable edges."""

    if isinstance(error, RiskCoverageError):
        reason = "coverage"
    elif isinstance(error, RiskSamplingError):
        reason = "sampling"
    elif isinstance(error, UnnavigableSpeedError):
        reason = "speed"
    elif isinstance(error, EtaRefinementError):
        context.diagnostics.eta_failures += 1
        reason = _eta_rejection_reason(error)
        context.diagnostics.eta_failure_reasons[error.failure_class] = (
            context.diagnostics.eta_failure_reasons.get(error.failure_class, 0) + 1
        )
    elif isinstance(error, _RejectedEdge):
        reason = error.reason
    else:
        return None
    context.diagnostics.reject(reason)
    return reason


def _state_parts(state: TemporalParetoState) -> tuple[tuple[int, int], tuple[int, int] | None]:
    if not isinstance(state, tuple) or len(state) != 2:
        raise NonFifoTemporalParetoError("invalid temporal Pareto state")
    node, heading = state
    if (
        not isinstance(node, tuple)
        or len(node) != 2
        or not all(isinstance(value, int) for value in node)
    ):
        raise NonFifoTemporalParetoError("invalid temporal Pareto node")
    if heading is not None and (
        not isinstance(heading, tuple)
        or len(heading) != 2
        or not all(isinstance(value, int) for value in heading)
    ):
        raise NonFifoTemporalParetoError("invalid temporal Pareto heading")
    return node, heading


def _route(label: NonFifoParetoLabel, departure_time: datetime) -> TemporalParetoRoute:
    steps: list[TemporalParetoStepEvidence] = []
    for transition in label.transitions:
        payload = transition.payload or {}
        step = payload.get("step")
        if not isinstance(step, TemporalParetoStepEvidence):
            raise NonFifoTemporalParetoError("Pareto label is missing actual step evidence")
        steps.append(step)
    arrivals = (departure_time.astimezone(UTC), *(step.eta for step in steps))
    return TemporalParetoRoute(
        states=tuple(label.path),
        arrival_times=arrivals,
        costs=label.costs,
        steps=tuple(steps),
    )


def _wrap_result(
    raw: NonFifoParetoSearchResult,
    scope_digest: str,
    session_identity: str,
    context: Any,
    request: PlanningRequest,
) -> NonFifoTemporalParetoResult:
    # ``goal_frontier`` is empty for every failed/cancelled/resource result;
    # this preserves the finite sidecar's no-partial-route rule.
    frontier = tuple(
        sorted(
            (_route(label, request.departure_time) for label in raw.goal_frontier),
            key=lambda route: (route.costs, route.arrival_times[-1], route.nodes),
        )
    )
    selected = _route(raw.label, request.departure_time) if raw.label is not None else None
    return NonFifoTemporalParetoResult(
        status=raw.status,
        selected=selected,
        frontier=frontier,
        raw_result=raw,
        scope_digest=scope_digest,
        session_identity=session_identity,
        diagnostics=context.diagnostics.freeze(),
    )


__all__ = [
    "TEMPORAL_PARETO_COMPONENTS",
    "NonFifoTemporalParetoCheckpoint",
    "NonFifoTemporalParetoError",
    "NonFifoTemporalParetoResearchSession",
    "NonFifoTemporalParetoResult",
    "TemporalParetoComponent",
    "TemporalParetoRoute",
    "TemporalParetoStepEvidence",
    "create_non_fifo_temporal_pareto_session",
    "restore_non_fifo_temporal_pareto_session",
    "run_non_fifo_temporal_pareto_search",
]
