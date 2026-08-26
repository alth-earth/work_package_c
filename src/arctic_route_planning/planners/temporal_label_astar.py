"""Experimental exact-arrival-time label-setting search.

This module is deliberately not imported from :mod:`arctic_route_planning.planners`.
It is the P0 candidate used to test the temporal-label semantics required by the
LTCR-TDA* work.  The production ``TimeDependentAStar`` remains the control
planner and is intentionally left untouched.

The important distinction from the control planner is the identity of a label:
``(node, incoming heading, exact UTC arrival time)``.  Two labels which arrive
at the same node in the same time bucket are therefore still independent labels
when their exact arrival instants differ.  This candidate makes no FIFO claim
and performs no cross-arrival dominance pruning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from math import isfinite
from time import perf_counter
from typing import Any

from arctic_route_planning.cost import (
    CostModel,
    EdgeCostInput,
    VesselPerformanceModel,
)
from arctic_route_planning.domain.models import CostWeights, ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import Node, RegularGrid, heading_change_degrees
from arctic_route_planning.risk import (
    RiskSampler,
    SampledRisk,
)

from .errors import NoRouteError
from .eta_refinement import (
    EtaEvaluation,
    EtaRefinementError,
    EtaRefinementPolicy,
    refine_eta,
)
from .time_dependent_astar import (
    PlanningRequest,
    PlanningResult,
    RouteStep,
    SearchMetrics,
    TimeDependentAStar,
    _EdgeTraversal,
    _trapezoidal_average,
    _unique,
)

type HeadingCode = tuple[int, int] | None
type TemporalState = tuple[Node, HeadingCode, datetime]
type EdgeEvaluator = Callable[
    [Node, Node, datetime, float | None, PlanningRequest, CostModel],
    _EdgeTraversal,
]

_COST_EPSILON = 1e-12
_TERMINATION_EPSILON = 1e-12
_DEFAULT_MAX_EXPANSIONS = 50_000
_DEFAULT_MAX_LABELS = 100_000
_DEFAULT_MAX_QUEUE = 50_000
_DEFAULT_MAX_EDGE_EVALUATIONS = 400_000


@dataclass(frozen=True, slots=True)
class TemporalLabel:
    """One non-dominated-only-by-exact-state temporal label.

    ``arrival_time`` is part of the state identity.  It is normalized to UTC
    but is otherwise retained at Python ``datetime`` microsecond precision;
    no time bucket is used to merge labels.
    """

    node: Node
    heading_code: HeadingCode
    arrival_time: datetime
    cost_hours: float

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("TemporalLabel.arrival_time must be timezone-aware UTC")
        if self.arrival_time.utcoffset() != timedelta(0):
            raise ValueError("TemporalLabel.arrival_time must use UTC")
        if not isfinite(self.cost_hours) or self.cost_hours < 0:
            raise ValueError("TemporalLabel.cost_hours must be finite and non-negative")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))

    @property
    def state(self) -> TemporalState:
        return self.node, self.heading_code, self.arrival_time

    @property
    def cost(self) -> float:
        """Compatibility alias for callers that use ``g``/``cost`` wording."""

        return self.cost_hours


@dataclass(frozen=True, slots=True)
class TemporalSearchLimits:
    """Hard per-query limits for the experimental candidate."""

    max_expansions: int = _DEFAULT_MAX_EXPANSIONS
    max_labels: int = _DEFAULT_MAX_LABELS
    max_queue: int = _DEFAULT_MAX_QUEUE
    max_edge_evaluations: int = _DEFAULT_MAX_EDGE_EVALUATIONS

    def __post_init__(self) -> None:
        for name in (
            "max_expansions",
            "max_labels",
            "max_queue",
            "max_edge_evaluations",
        ):
            value = getattr(self, name)
            if value < 1:
                raise ValueError(f"{name} must be positive")


class TemporalSearchLimitExceeded(NoRouteError):
    """The candidate reached a hard resource limit and returned no partial route."""


@dataclass(frozen=True, slots=True)
class TemporalDiagnostics:
    """Internal P0 observability; none of these fields enter C→D contracts."""

    expanded_labels: int = 0
    generated_labels: int = 0
    unique_labels: int = 0
    label_peak: int = 0
    queue_peak: int = 0
    edge_evaluations: int = 0
    heap_pushes: int = 0
    heap_pops: int = 0
    stale_pops: int = 0
    exact_state_replacements: int = 0
    eta_iterations: int = 0
    eta_resamples: int = 0
    eta_failures: int = 0
    eta_max_residual_seconds: float = 0.0
    rejected_hard_edges: int = 0
    rejected_risk_edges: int = 0
    rejected_speed_edges: int = 0
    rejected_coverage_edges: int = 0
    rejected_eta_edges: int = 0
    rejected_non_increasing_edges: int = 0
    rejection_reasons: tuple[tuple[str, int], ...] = ()
    fifo_status: str = "FIFO_UNCERTAIN"

    @property
    def labels_peak(self) -> int:
        return self.label_peak


@dataclass(frozen=True, slots=True)
class TemporalCandidateResult:
    """Planning result plus internal candidate diagnostics."""

    planning_result: PlanningResult
    diagnostics: TemporalDiagnostics

    @property
    def nodes(self) -> tuple[Node, ...]:
        return self.planning_result.nodes

    @property
    def steps(self) -> tuple[RouteStep, ...]:
        return self.planning_result.steps

    @property
    def total_cost_hours(self) -> float:
        return self.planning_result.total_cost_hours


@dataclass(slots=True)
class _MutableDiagnostics:
    expanded_labels: int = 0
    generated_labels: int = 0
    unique_labels: int = 0
    label_peak: int = 0
    queue_peak: int = 0
    edge_evaluations: int = 0
    heap_pushes: int = 0
    heap_pops: int = 0
    stale_pops: int = 0
    exact_state_replacements: int = 0
    eta_iterations: int = 0
    eta_resamples: int = 0
    eta_failures: int = 0
    eta_max_residual_seconds: float = 0.0
    rejected_hard_edges: int = 0
    rejected_risk_edges: int = 0
    rejected_speed_edges: int = 0
    rejected_coverage_edges: int = 0
    rejected_eta_edges: int = 0
    rejected_non_increasing_edges: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
        if reason == "hard":
            self.rejected_hard_edges += 1
        elif reason == "risk":
            self.rejected_risk_edges += 1
        elif reason == "speed":
            self.rejected_speed_edges += 1
        elif reason in {"coverage", "sampling"}:
            self.rejected_coverage_edges += 1
        elif reason == "eta":
            self.rejected_eta_edges += 1
        elif reason == "non_increasing_arrival":
            self.rejected_non_increasing_edges += 1

    def freeze(self, *, fifo_status: str = "FIFO_UNCERTAIN") -> TemporalDiagnostics:
        return TemporalDiagnostics(
            expanded_labels=self.expanded_labels,
            generated_labels=self.generated_labels,
            unique_labels=self.unique_labels,
            label_peak=self.label_peak,
            queue_peak=self.queue_peak,
            edge_evaluations=self.edge_evaluations,
            heap_pushes=self.heap_pushes,
            heap_pops=self.heap_pops,
            stale_pops=self.stale_pops,
            exact_state_replacements=self.exact_state_replacements,
            eta_iterations=self.eta_iterations,
            eta_resamples=self.eta_resamples,
            eta_failures=self.eta_failures,
            eta_max_residual_seconds=self.eta_max_residual_seconds,
            rejected_hard_edges=self.rejected_hard_edges,
            rejected_risk_edges=self.rejected_risk_edges,
            rejected_speed_edges=self.rejected_speed_edges,
            rejected_coverage_edges=self.rejected_coverage_edges,
            rejected_eta_edges=self.rejected_eta_edges,
            rejected_non_increasing_edges=self.rejected_non_increasing_edges,
            rejection_reasons=tuple(sorted(self.rejection_reasons.items())),
            fifo_status=fifo_status,
        )


@dataclass(slots=True)
class _TemporalExecutionContext:
    """Mutable state owned by one temporal search session.

    The candidate planner object is intentionally reusable.  Search-local
    diagnostics, heuristic memoization, calm-water speed and an injected edge
    evaluator therefore live here rather than on ``TemporalLabelAStar``.
    """

    diagnostics: _MutableDiagnostics
    heuristic_distances: dict[Node, float]
    calm_speed: Any
    edge_evaluator: EdgeEvaluator | None


class _RejectedEdge(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class TemporalLabelAStar(TimeDependentAStar):
    """P0 exact-arrival-time A* candidate.

    The class is intentionally only available from this module.  It subclasses
    the control implementation to reuse grid validation, geometry, the public
    vessel/cost primitives, and result construction semantics; its search
    state, edge ETA refinement, limits, and termination rule are independent.
    """

    def __init__(
        self,
        grid: RegularGrid,
        risk_sampler: RiskSampler,
        vessel_model: VesselPerformanceModel,
        *,
        planner_config: PlannerConfig | None = None,
        cost_weights: Mapping[ObjectiveMode | str, CostWeights] | None = None,
        full_turn_penalty_hours: float = 0.25,
        limits: TemporalSearchLimits | None = None,
        eta_policy: EtaRefinementPolicy | None = None,
        edge_evaluator: EdgeEvaluator | None = None,
    ) -> None:
        super().__init__(
            grid,
            risk_sampler,
            vessel_model,
            planner_config=planner_config,
            cost_weights=cost_weights,
            full_turn_penalty_hours=full_turn_penalty_hours,
        )
        self.limits = limits or TemporalSearchLimits()
        self.eta_policy = eta_policy or EtaRefinementPolicy()
        self._injected_edge_evaluator = edge_evaluator

    def solve(self, request: PlanningRequest) -> TemporalCandidateResult:
        """Solve one request and return the result with P0 diagnostics."""

        return self.plan(request)

    def plan(self, request: PlanningRequest) -> TemporalCandidateResult:
        # Keep the compatibility wrapper deliberately thin.  The search state
        # is owned by ``TemporalSession`` so one planner can safely create
        # independent sessions for multiple objectives.
        from arctic_route_planning.planners._archive.temporal_session import (
            advance_session,
            create_session,
        )

        session = create_session(self, request)
        result = advance_session(session)
        if result is None:  # pragma: no cover - a full advance is unbounded
            raise RuntimeError("unbounded temporal session did not reach a terminal state")
        return result

    def create_session(self, request: PlanningRequest, identity: Any = None) -> Any:
        """Create an internal resumable search session."""

        from arctic_route_planning.planners._archive.temporal_session import create_session

        return create_session(self, request, identity=identity)

    def create_session_bundle(
        self,
        request: PlanningRequest,
        objectives: Iterable[ObjectiveMode | str] = tuple(ObjectiveMode),
    ) -> Any:
        """Create isolated internal sessions for the requested objectives."""

        from arctic_route_planning.planners._archive.temporal_session import create_session_bundle

        return create_session_bundle(self, request, objectives)

    def advance_session(self, session: Any, expansion_slice: int | None = None) -> Any:
        """Advance one of this planner's internal sessions."""

        from arctic_route_planning.planners._archive.temporal_session import advance_session

        return advance_session(session, expansion_slice=expansion_slice)

    def checkpoint_session(self, session: Any) -> Any:
        """Return an immutable in-process checkpoint for ``session``."""

        from arctic_route_planning.planners._archive.temporal_session import checkpoint_session

        return checkpoint_session(session)

    def restore_session(
        self,
        checkpoint: Any,
        request: PlanningRequest | None = None,
        identity: Any = None,
    ) -> Any:
        """Restore a session after validating its complete identity fence."""

        from arctic_route_planning.planners._archive.temporal_session import restore_session

        return restore_session(self, checkpoint, request=request, identity=identity)

    def _evaluate_edge(
        self,
        start: Node,
        end: Node,
        departure_time: datetime,
        previous_heading: float | None,
        request: PlanningRequest,
        cost_model: CostModel,
        *,
        context: _TemporalExecutionContext | None = None,
    ) -> _EdgeTraversal:
        if context is None:
            context = self._new_execution_context()
        if context.edge_evaluator is not None:
            return context.edge_evaluator(
                start,
                end,
                departure_time,
                previous_heading,
                request,
                cost_model,
            )

        distance_km, _, points = self._edge_geometry(
            start,
            end,
            minimum_samples=request.edge_sample_count,
        )
        calm_speed = context.calm_speed
        initial_hours = distance_km / calm_speed.speed_km_per_hour

        def samples_at(value: float | datetime) -> tuple[SampledRisk, ...]:
            if isinstance(value, datetime):
                travel_hours = (value - departure_time).total_seconds() / 3600.0
            else:
                travel_hours = float(value)
            if not isfinite(travel_hours) or travel_hours <= 0:
                raise EtaRefinementError("ETA travel time must be finite and positive")
            samples = tuple(
                self.risk_sampler.sample(
                    departure_time + timedelta(hours=travel_hours * fraction),
                    point.longitude,
                    point.latitude,
                )
                for point, fraction in _with_fractions(points)
            )
            self._validate_samples(samples, request)
            return samples

        def speed_for_samples(samples: Iterable[SampledRisk]) -> Any:
            return self.vessel_model.effective_speed(
                min(sample.environment_speed_factor for sample in samples)
            )

        def evaluate(travel_hours: float) -> EtaEvaluation:
            samples = samples_at(travel_hours)
            speed = speed_for_samples(samples)
            return EtaEvaluation(
                samples=samples,
                speed=speed,
                implied_travel_hours=distance_km / speed.speed_km_per_hour,
            )

        refined = refine_eta(initial_hours, evaluate, self.eta_policy)
        accepted_hours = refined.travel_hours
        if not isfinite(accepted_hours) or accepted_hours <= 0:
            raise EtaRefinementError(
                "invalid_operator",
                {"message": "ETA refinement returned a non-positive travel time"},
            )

        # ``refine_eta`` returns a terminal evaluation sampled at its raw
        # fixed point. Re-sample at the value this planner will actually use so
        # final sampling time, arrival time, and cost.travel_hours agree.
        samples = samples_at(accepted_hours)
        diagnostics = context.diagnostics
        diagnostics.eta_resamples += refined.terminal_resamples + 1
        speed = speed_for_samples(samples)
        terminal_hours = distance_km / speed.speed_km_per_hour
        residual_seconds = abs(terminal_hours - accepted_hours) * 3600.0
        diagnostics.eta_iterations += refined.iterations
        diagnostics.eta_max_residual_seconds = max(
            diagnostics.eta_max_residual_seconds,
            refined.max_residual_seconds,
            residual_seconds,
        )
        if residual_seconds > _eta_tolerance_seconds(self.eta_policy, accepted_hours):
            raise EtaRefinementError(
                "terminal_mismatch",
                {"message": "terminal ETA samples are inconsistent with travel time"},
            )
        # Keep the accepted sampling instant. Replacing it with
        # ``terminal_hours`` would make the edge cost refer to an unsampled
        # instant when the residual is merely within tolerance.
        travel_hours = accepted_hours
        heading = self._edge_geometry(start, end)[1]
        risk_score = _trapezoidal_average(sample.risk_score for sample in samples)
        maximum_risk = max(sample.risk_score for sample in samples)
        confidence = min(sample.confidence for sample in samples)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance_km,
                travel_hours=travel_hours,
                risk_score=risk_score,
                confidence=confidence,
                heading_change_degrees=heading_change_degrees(previous_heading, heading),
            )
        )
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=departure_time + timedelta(hours=travel_hours),
            heading_degrees=heading,
            speed_knots=speed.speed_knots,
            distance_km=distance_km,
            risk_score=risk_score,
            maximum_risk=maximum_risk,
            confidence=confidence,
            cost=cost,
            source_risk_ids=_unique(
                risk_id for sample in samples for risk_id in sample.source_risk_ids
            ),
        )

    def _validate_samples(
        self,
        samples: tuple[SampledRisk, ...],
        request: PlanningRequest,
    ) -> None:
        if any(sample.hard_mask for sample in samples):
            raise _RejectedEdge("hard")
        if any(
            sample.confidence < self.planner_config.minimum_confidence for sample in samples
        ):
            raise _RejectedEdge("risk")
        if request.maximum_risk is not None and any(
            sample.risk_score > request.maximum_risk for sample in samples
        ):
            raise _RejectedEdge("risk")

    def _ensure_queue_capacity(self, queue: list[Any]) -> None:
        if len(queue) + 1 > self.limits.max_queue:
            raise self._limit("queue", self.limits.max_queue)

    def _priority(
        self,
        node: Node,
        goal: Node,
        request: PlanningRequest,
        cost_model: CostModel,
        cost: float,
        *,
        context: _TemporalExecutionContext | None = None,
    ) -> float:
        return cost + self._heuristic_for_context(
            node,
            goal,
            cost_model,
            request,
            context=context,
        )

    def _heuristic_for_context(
        self,
        node: Node,
        goal: Node,
        cost_model: CostModel,
        request: PlanningRequest,
        *,
        context: _TemporalExecutionContext | None = None,
    ) -> float:
        if not request.use_heuristic:
            return 0.0
        if context is None:
            context = self._new_execution_context()
        distance = context.heuristic_distances.get(node)
        if distance is None:
            distance = self.grid.distance_km(node, goal)
            context.heuristic_distances[node] = distance
        return cost_model.lower_bound(distance)

    def _new_execution_context(self) -> _TemporalExecutionContext:
        """Create the private mutable context captured by one session."""

        return _TemporalExecutionContext(
            diagnostics=_MutableDiagnostics(),
            heuristic_distances={},
            calm_speed=self.vessel_model.effective_speed(1.0),
            edge_evaluator=self._injected_edge_evaluator,
        )

    def _previous_heading(self, node: Node, heading_code: HeadingCode) -> float | None:
        if heading_code is None:
            return None
        previous = node[0] - heading_code[0], node[1] - heading_code[1]
        if not self.grid.contains(previous):
            return None
        return self._edge_geometry(previous, node)[1]

    def _build_result(
        self,
        request: PlanningRequest,
        goal_state: TemporalState,
        start_sample: SampledRisk,
        labels: Mapping[TemporalState, float],
        predecessors: Mapping[TemporalState, tuple[TemporalState, _EdgeTraversal]],
        diagnostics: _MutableDiagnostics,
        started: float,
        *,
        compute_ms: float | None = None,
    ) -> PlanningResult:
        traversals: list[_EdgeTraversal] = []
        state = goal_state
        while state in predecessors:
            state, traversal = predecessors[state]
            traversals.append(traversal)
        traversals.reverse()
        start_point = self.grid.point(request.start)
        steps = [
            RouteStep(
                node=request.start,
                longitude=start_point.longitude,
                latitude=start_point.latitude,
                eta=request.departure_time,
                incoming_heading_degrees=None,
                recommended_speed_knots=None,
                edge_distance_km=0.0,
                edge_risk_score=start_sample.risk_score,
                edge_maximum_risk=start_sample.risk_score,
                edge_confidence=start_sample.confidence,
                edge_cost=None,
                source_risk_ids=start_sample.source_risk_ids,
            )
        ]
        for traversal in traversals:
            point = self.grid.point(traversal.end)
            steps.append(
                RouteStep(
                    node=traversal.end,
                    longitude=point.longitude,
                    latitude=point.latitude,
                    eta=traversal.arrival_time,
                    incoming_heading_degrees=traversal.heading_degrees,
                    recommended_speed_knots=traversal.speed_knots,
                    edge_distance_km=traversal.distance_km,
                    edge_risk_score=traversal.risk_score,
                    edge_maximum_risk=traversal.maximum_risk,
                    edge_confidence=traversal.confidence,
                    edge_cost=traversal.cost,
                    source_risk_ids=traversal.source_risk_ids,
                )
            )
        distance_km = sum(edge.distance_km for edge in traversals)
        travel_hours = sum(edge.cost.travel_hours for edge in traversals)
        risk_exposure = sum(edge.cost.risk_exposure_hours for edge in traversals)
        source_ids = _unique(risk_id for step in steps for risk_id in step.source_risk_ids)
        diagnostics.unique_labels = len(labels)
        return PlanningResult(
            objective=request.objective,
            steps=tuple(steps),
            total_cost_hours=labels[goal_state],
            distance_km=distance_km,
            travel_hours=travel_hours,
            average_risk=risk_exposure / travel_hours if travel_hours else 0.0,
            maximum_risk=max(
                (edge.maximum_risk for edge in traversals),
                default=start_sample.risk_score,
            ),
            minimum_confidence=min(
                (edge.confidence for edge in traversals),
                default=start_sample.confidence,
            ),
            source_risk_ids=source_ids,
            metrics=self._metrics(diagnostics, started, compute_ms=compute_ms),
        )

    def _zero_length_result(
        self,
        request: PlanningRequest,
        sample: SampledRisk,
        started: float,
        diagnostics: _MutableDiagnostics,
        *,
        compute_ms: float | None = None,
    ) -> PlanningResult:
        point = self.grid.point(request.start)
        step = RouteStep(
            node=request.start,
            longitude=point.longitude,
            latitude=point.latitude,
            eta=request.departure_time,
            incoming_heading_degrees=None,
            recommended_speed_knots=None,
            edge_distance_km=0.0,
            edge_risk_score=sample.risk_score,
            edge_maximum_risk=sample.risk_score,
            edge_confidence=sample.confidence,
            edge_cost=None,
            source_risk_ids=sample.source_risk_ids,
        )
        diagnostics.unique_labels = 1
        diagnostics.label_peak = 1
        diagnostics.queue_peak = 1
        return PlanningResult(
            objective=request.objective,
            steps=(step,),
            total_cost_hours=0.0,
            distance_km=0.0,
            travel_hours=0.0,
            average_risk=sample.risk_score,
            maximum_risk=sample.risk_score,
            minimum_confidence=sample.confidence,
            source_risk_ids=sample.source_risk_ids,
            metrics=self._metrics(diagnostics, started, compute_ms=compute_ms),
        )

    @staticmethod
    def _metrics(
        diagnostics: _MutableDiagnostics,
        started: float,
        *,
        compute_ms: float | None = None,
    ) -> SearchMetrics:
        if compute_ms is None:
            compute_ms = (perf_counter() - started) * 1_000.0
        if not isfinite(compute_ms):
            raise RuntimeError("non-finite planning duration")
        return SearchMetrics(
            expanded_states=diagnostics.expanded_labels,
            generated_states=diagnostics.generated_labels,
            rejected_hard_edges=diagnostics.rejected_hard_edges,
            rejected_risk_edges=diagnostics.rejected_risk_edges,
            rejected_speed_edges=diagnostics.rejected_speed_edges,
            rejected_coverage_edges=diagnostics.rejected_coverage_edges,
            queue_peak=diagnostics.queue_peak,
            compute_ms=compute_ms,
            unique_states=diagnostics.unique_labels,
            heap_pushes=diagnostics.heap_pushes,
            heap_pops=diagnostics.heap_pops,
            stale_pops=diagnostics.stale_pops,
            reopened_states=diagnostics.exact_state_replacements,
            max_time_index=0,
        )

    @staticmethod
    def _push_queue(
        queue: list[tuple[float, float, int, int, int, int, int, int, datetime, TemporalState]],
        priority: float,
        cost: float,
        state: TemporalState,
        serial: Any,
    ) -> None:
        node, heading, arrival = state
        heading_row, heading_col = heading if heading is not None else (0, 0)
        heappush(
            queue,
            (
                priority,
                cost,
                node[0],
                node[1],
                heading_row,
                heading_col,
                arrival.toordinal(),
                arrival.microsecond,
                arrival,
                state,
            ),
        )
        # ``serial`` is consumed even though the structural state key supplies
        # deterministic tie-breaking; retaining it keeps the caller's intent
        # explicit and makes future queue instrumentation straightforward.
        next(serial)

    @staticmethod
    def _discard_stale(
        queue: list[tuple[float, float, int, int, int, int, int, int, datetime, TemporalState]],
        labels: Mapping[TemporalState, float],
        diagnostics: _MutableDiagnostics,
    ) -> None:
        while queue:
            queued_cost = queue[0][1]
            state = queue[0][-1]
            if labels.get(state) == queued_cost:
                return
            heappop(queue)
            diagnostics.heap_pops += 1
            diagnostics.stale_pops += 1

    def _limit(self, resource: str, limit: int) -> TemporalSearchLimitExceeded:
        return TemporalSearchLimitExceeded(f"temporal search exceeded {resource}={limit}")


def _with_fractions(points: tuple[Any, ...]) -> Iterable[tuple[Any, float]]:
    denominator = len(points) - 1
    return ((point, index / denominator) for index, point in enumerate(points))


def _eta_tolerance_seconds(policy: EtaRefinementPolicy, travel_hours: float) -> float:
    for name in ("tolerance_seconds", "convergence_tolerance_seconds"):
        value = getattr(policy, name, None)
        if value is not None:
            return float(value)
    absolute = getattr(policy, "absolute_tolerance_seconds", 1.0)
    relative = getattr(policy, "relative_tolerance", 1e-6)
    return max(absolute, relative * max(3600.0, travel_hours * 3600.0))


def _eta_rejection_reason(error: EtaRefinementError) -> str:
    """Recover a domain rejection wrapped by the ETA callback boundary."""

    diagnostics = getattr(error, "diagnostics", {})
    operator_name = diagnostics.get("operator_exception")
    operator_message = str(diagnostics.get("operator_message", "")).lower()
    if operator_name == "_RejectedEdge" and operator_message in {"hard", "risk"}:
        return operator_message
    if operator_name == "RiskCoverageError":
        return "coverage"
    if operator_name == "RiskSamplingError":
        return "sampling"
    if operator_name == "UnnavigableSpeedError":
        return "speed"
    return "eta"


__all__ = [
    "TemporalCandidateResult",
    "TemporalDiagnostics",
    "TemporalLabel",
    "TemporalLabelAStar",
    "TemporalSearchLimitExceeded",
    "TemporalSearchLimits",
]
