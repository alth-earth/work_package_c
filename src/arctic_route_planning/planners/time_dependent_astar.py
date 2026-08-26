"""Time-dependent A* over an implicit rectilinear grid."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from itertools import count
from math import isfinite
from time import perf_counter

try:
    import resource
except ImportError:  # pragma: no cover - resource is unavailable on Windows.
    resource = None

from arctic_route_planning.cost import (
    KNOT_TO_KM_PER_HOUR,
    CostBreakdown,
    CostModel,
    EdgeCostInput,
    UnnavigableSpeedError,
    VesselPerformanceModel,
)
from arctic_route_planning.domain.models import CostWeights, ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import GeoPoint, Node, RegularGrid, heading_change_degrees
from arctic_route_planning.risk import (
    RiskCoverageError,
    RiskIdentity,
    RiskSampler,
    SampledRisk,
)

from .errors import (
    EndpointBlockedError,
    NoRouteError,
    PlanningCancelled,
    PlanningHorizonExceeded,
)

type HeadingCode = tuple[int, int] | None
type State = tuple[Node, int, HeadingCode]

# Label relaxation tolerance: costs closer than this are treated as equal,
# absorbing floating-point noise from the equivalent-hours accumulator.
_COST_EPSILON = 1e-12

# Two refinement rounds keep ETA and environment-dependent speed mutually
# consistent without hiding a complex optimizer inside the baseline: round one
# samples risk with a calm-water ETA estimate, round two re-estimates travel
# time with the sampled environment speed.  Further rounds would change the
# ETA by less than the risk frames' temporal resolution.
_EDGE_REFINEMENT_ROUNDS = 2


@dataclass(frozen=True, slots=True)
class PlanningRequest:
    start: Node
    goal: Node
    departure_time: datetime
    objective: ObjectiveMode = ObjectiveMode.RECOMMENDED
    time_bucket_size: timedelta = timedelta(minutes=15)
    edge_sample_count: int = 3
    maximum_elapsed: timedelta | None = None
    maximum_risk: float | None = None
    max_expansions: int = 250_000
    cancel_check: Callable[[], bool] | None = None
    use_heuristic: bool = True
    progress_interval_seconds: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "objective", ObjectiveMode(self.objective))
        if self.departure_time.tzinfo is None or self.departure_time.utcoffset() is None:
            raise ValueError("departure_time must be timezone-aware UTC")
        if self.departure_time.utcoffset() != timedelta(0):
            raise ValueError("departure_time must use UTC")
        object.__setattr__(self, "departure_time", self.departure_time.astimezone(UTC))
        if self.time_bucket_size <= timedelta(0):
            raise ValueError("time_bucket_size must be positive")
        if self.edge_sample_count < 3:
            raise ValueError("edge_sample_count must be at least 3")
        if self.maximum_elapsed is not None and self.maximum_elapsed <= timedelta(0):
            raise ValueError("maximum_elapsed must be positive")
        if self.maximum_risk is not None and not 0 <= self.maximum_risk <= 1:
            raise ValueError("maximum_risk must be in [0, 1]")
        if self.max_expansions < 1:
            raise ValueError("max_expansions must be positive")
        if self.progress_interval_seconds is not None and (
            not isfinite(self.progress_interval_seconds)
            or self.progress_interval_seconds <= 0
        ):
            raise ValueError("progress_interval_seconds must be positive")


@dataclass(frozen=True, slots=True)
class RouteStep:
    node: Node
    longitude: float
    latitude: float
    eta: datetime
    incoming_heading_degrees: float | None
    recommended_speed_knots: float | None
    edge_distance_km: float
    edge_risk_score: float
    edge_maximum_risk: float
    edge_confidence: float
    edge_cost: CostBreakdown | None
    source_risk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchMetrics:
    expanded_states: int
    generated_states: int
    rejected_hard_edges: int
    rejected_risk_edges: int
    rejected_speed_edges: int
    rejected_coverage_edges: int
    queue_peak: int
    compute_ms: float
    unique_states: int = 0
    heap_pushes: int = 0
    heap_pops: int = 0
    stale_pops: int = 0
    reopened_states: int = 0
    max_time_index: int = 0


@dataclass(frozen=True, slots=True)
class PlanningResult:
    objective: ObjectiveMode
    steps: tuple[RouteStep, ...]
    total_cost_hours: float
    distance_km: float
    travel_hours: float
    average_risk: float
    maximum_risk: float
    minimum_confidence: float
    source_risk_ids: tuple[str, ...]
    metrics: SearchMetrics

    @property
    def nodes(self) -> tuple[Node, ...]:
        return tuple(step.node for step in self.steps)


@dataclass(frozen=True, slots=True)
class _EdgeTraversal:
    start: Node
    end: Node
    arrival_time: datetime
    heading_degrees: float
    speed_knots: float
    distance_km: float
    risk_score: float
    maximum_risk: float
    confidence: float
    cost: CostBreakdown
    source_risk_ids: tuple[str, ...]


@dataclass(slots=True)
class _Counters:
    """Mutable hot-loop accumulator; snapshotted into SearchMetrics on exit."""

    expanded: int = 0
    generated: int = 0
    unique: int = 1
    hard: int = 0
    risk: int = 0
    speed: int = 0
    coverage: int = 0
    queue_peak: int = 1
    stale_pop: int = 0
    reopened: int = 0
    heap_pushes: int = 0
    heap_pops: int = 0
    best_f: float = float("inf")
    last_f: float = 0.0
    last_g: float = 0.0
    max_bucket: int = 0


class _RejectedEdge(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class TimeDependentAStar:
    """Baseline time-dependent A* with state ``(node, time bucket, heading)``.

    Waiting actions are deliberately absent in v1.  The search supports a
    zero-heuristic mode, which is Dijkstra over the same time-expanded state
    graph and is useful as a correctness oracle for small fixtures.
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
    ) -> None:
        self.grid = grid
        self.risk_sampler = risk_sampler
        self.vessel_model = vessel_model
        self.planner_config = planner_config or PlannerConfig()
        self._full_turn_penalty_hours = full_turn_penalty_hours
        self._weights = {mode: self.planner_config.weights_for(mode) for mode in ObjectiveMode}
        if cost_weights is not None:
            self._weights.update(
                {ObjectiveMode(mode): weights for mode, weights in cost_weights.items()}
            )
        self._edge_cache: dict[
            tuple[Node, Node, int], tuple[float, float, tuple[GeoPoint, ...]]
        ] = {}
        self._edge_cache_hits = 0
        self._edge_cache_misses = 0
        self._heur_dist: dict[Node, float] = {}
        self._validate_grid_alignment()

    @property
    def risk_identity(self) -> RiskIdentity:
        """Return the frozen BC context consumed by this planner instance."""

        return self.risk_sampler.identity

    @property
    def risk_as_of_times(self) -> tuple[datetime, ...]:
        """Expose BC knowledge cutoffs for service-level leakage checks."""

        return self.risk_sampler.as_of_times

    @property
    def edge_geometry_cache_stats(self) -> dict[str, int]:
        """Return observational counters without changing cache behavior."""

        return {
            "hits": self._edge_cache_hits,
            "misses": self._edge_cache_misses,
            "entries": len(self._edge_cache),
        }

    def plan(self, request: PlanningRequest) -> PlanningResult:
        """Plan with the unchanged control-search API and default behavior."""

        return self._plan(request, trace=None)

    def _plan_traced(
        self,
        request: PlanningRequest,
        *,
        identity: object = None,
        observer: object = None,
    ) -> tuple[PlanningResult, object]:
        """Run the control search with an internal, opt-in write trace.

        The collector is imported lazily so this internal research carrier
        cannot affect the normal planner import graph or public exports.
        ``plan`` never enables this path.
        """

        from .control_trace_reuse import ControlTraceCollector

        collector = ControlTraceCollector(
            self,
            request,
            identity=identity,
            observer=observer,
        )
        result = self._plan(request, trace=collector)
        return result, collector.trace

    def _plan(self, request: PlanningRequest, *, trace: object | None) -> PlanningResult:
        started = perf_counter()
        self._last_counters: _Counters | None = None
        self._heur_dist = {}
        self._calm_speed = self.vessel_model.effective_speed(1.0)
        # Progress cadence is resolved by the application layer via
        # ``PlanningRequest.progress_interval_seconds``; the planner core no
        # longer reads environment variables directly.
        self._progress_interval = request.progress_interval_seconds
        self._check_cancelled(request)
        self._validate_request_nodes(request)
        start_sample = self._sample_node(request.start, request.departure_time)
        if start_sample.hard_mask:
            raise EndpointBlockedError(f"start node {request.start} is hard-blocked")

        start_state: State = (request.start, 0, None)
        if request.start == request.goal:
            result = self._zero_length_result(request, start_sample, started)
            if trace is not None:
                trace.record_write(
                    state=start_state,
                    parent_state=None,
                    label_cost_hours=0.0,
                    arrival_time=request.departure_time,
                    priority=0.0,
                    path_elapsed_seconds=0.0,
                    edge_maximum_risk=0.0,
                    write_kind="INITIAL",
                )
                trace.trace = trace.finish(result, start_state, {})
            return result

        cost_model = self._cost_model(request.objective)
        labels: dict[State, tuple[float, datetime]] = {start_state: (0.0, request.departure_time)}
        predecessor: dict[State, tuple[State, _EdgeTraversal]] = {}
        serial = count()
        queue: list[tuple[float, float, int, State]] = []
        heuristic = self._heuristic(request.start, request.goal, cost_model, request)
        heappush(queue, (heuristic, 0.0, next(serial), start_state))
        if trace is not None:
            trace.record_write(
                state=start_state,
                parent_state=None,
                label_cost_hours=0.0,
                arrival_time=request.departure_time,
                priority=heuristic,
                path_elapsed_seconds=0.0,
                edge_maximum_risk=0.0,
                write_kind="INITIAL",
            )
        counters = _Counters()
        counters.heap_pushes = 1
        next_progress = 0.0

        while queue:
            self._check_cancelled(request)
            popped_priority, queued_cost, _, state = heappop(queue)
            counters.heap_pops += 1
            best_cost, arrival_time = labels[state]
            if queued_cost != best_cost:
                counters.stale_pop += 1
                continue
            node, _, incoming_code = state
            counters.expanded += 1
            counters.unique = len(labels)
            self._last_counters = counters
            counters.best_f = min(counters.best_f, popped_priority)
            counters.last_f = popped_priority
            counters.last_g = queued_cost
            if counters.expanded > request.max_expansions:
                raise NoRouteError(f"planning exceeded max_expansions={request.max_expansions}")
            if (
                self._progress_interval is not None
                and perf_counter() - next_progress >= self._progress_interval
            ):
                next_progress = perf_counter()
                self._emit_progress(
                    request,
                    counters,
                    labels=labels,
                    open_size=len(queue),
                    started=started,
                )
            if node == request.goal:
                result = self._build_result(
                    request,
                    state,
                    start_sample,
                    labels,
                    predecessor,
                    counters,
                    started,
                )
                if trace is not None:
                    trace.trace = trace.finish(result, state, predecessor)
                return result

            previous_heading = None
            if incoming_code is not None:
                prev_node = (node[0] - incoming_code[0], node[1] - incoming_code[1])
                previous_heading = self._edge_geometry(prev_node, node)[1]
            for neighbor in self.grid.neighbors(node):
                self._check_cancelled(request)
                try:
                    traversal = self._evaluate_edge(
                        node,
                        neighbor,
                        arrival_time,
                        previous_heading,
                        request,
                        cost_model,
                    )
                except RiskCoverageError:
                    counters.coverage += 1
                    continue
                except UnnavigableSpeedError:
                    counters.speed += 1
                    continue
                except _RejectedEdge as rejection:
                    if rejection.reason == "hard":
                        counters.hard += 1
                    else:
                        counters.risk += 1
                    continue
                elapsed = traversal.arrival_time - request.departure_time
                if request.maximum_elapsed is not None and elapsed > request.maximum_elapsed:
                    counters.coverage += 1
                    continue
                time_bucket = int(
                    elapsed.total_seconds() // request.time_bucket_size.total_seconds()
                )
                if time_bucket > counters.max_bucket:
                    counters.max_bucket = time_bucket
                heading_code = (neighbor[0] - node[0], neighbor[1] - node[1])
                next_state: State = (neighbor, time_bucket, heading_code)
                tentative_cost = best_cost + traversal.cost.total_equivalent_hours
                previous = labels.get(next_state)
                if previous is not None and tentative_cost >= previous[0] - _COST_EPSILON:
                    continue
                if previous is not None:
                    counters.reopened += 1
                labels[next_state] = (tentative_cost, traversal.arrival_time)
                predecessor[next_state] = (state, traversal)
                priority = tentative_cost + self._heuristic(
                    neighbor,
                    request.goal,
                    cost_model,
                    request,
                )
                heappush(queue, (priority, tentative_cost, next(serial), next_state))
                if trace is not None:
                    trace.record_write(
                        state=next_state,
                        parent_state=state,
                        label_cost_hours=tentative_cost,
                        arrival_time=traversal.arrival_time,
                        priority=priority,
                        path_elapsed_seconds=elapsed.total_seconds(),
                        edge_maximum_risk=traversal.maximum_risk,
                        write_kind="REPLACEMENT" if previous is not None else "INSERT",
                    )
                counters.generated += 1
                counters.heap_pushes += 1
                counters.queue_peak = max(counters.queue_peak, len(queue))

        if counters.coverage:
            self._last_counters = counters
            raise PlanningHorizonExceeded(
                "no complete route fits inside the available risk time window"
            )
        self._last_counters = counters
        raise NoRouteError("no route satisfies hard, risk, and vessel constraints")

    def plan_candidates(
        self,
        request: PlanningRequest,
        objectives: Iterable[ObjectiveMode | str] = tuple(ObjectiveMode),
    ) -> dict[ObjectiveMode, PlanningResult]:
        """Plan the same request independently under multiple objective policies."""

        return {
            mode: self.plan(replace(request, objective=mode))
            for raw_mode in objectives
            for mode in (ObjectiveMode(raw_mode),)
        }

    def _evaluate_edge(
        self,
        start: Node,
        end: Node,
        departure_time: datetime,
        previous_heading: float | None,
        request: PlanningRequest,
        cost_model: CostModel,
    ) -> _EdgeTraversal:
        distance_km, _, points = self._edge_geometry(
            start, end, minimum_samples=request.edge_sample_count
        )
        calm_speed = self._calm_speed
        travel_hours = distance_km / calm_speed.speed_km_per_hour
        samples: tuple[SampledRisk, ...] = ()
        speed = calm_speed

        # See _EDGE_REFINEMENT_ROUNDS: two passes keep ETA and environment
        # speed mutually consistent without hiding a complex optimizer.
        for _ in range(_EDGE_REFINEMENT_ROUNDS):
            samples = tuple(
                self.risk_sampler.sample(
                    departure_time + timedelta(hours=travel_hours * fraction),
                    point.longitude,
                    point.latitude,
                )
                for point, fraction in _with_fractions(points)
            )
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
            speed = self.vessel_model.effective_speed(
                min(sample.environment_speed_factor for sample in samples)
            )
            travel_hours = distance_km / speed.speed_km_per_hour

        risk_score = _trapezoidal_average(sample.risk_score for sample in samples)
        maximum_risk = max(sample.risk_score for sample in samples)
        confidence = min(sample.confidence for sample in samples)
        heading = self._edge_geometry(start, end)[1]
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

    def _build_result(
        self,
        request: PlanningRequest,
        goal_state: State,
        start_sample: SampledRisk,
        labels: Mapping[State, tuple[float, datetime]],
        predecessor: Mapping[State, tuple[State, _EdgeTraversal]],
        counters: _Counters,
        started: float,
    ) -> PlanningResult:
        traversals: list[_EdgeTraversal] = []
        state = goal_state
        while state in predecessor:
            state, traversal = predecessor[state]
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
        return PlanningResult(
            objective=request.objective,
            steps=tuple(steps),
            total_cost_hours=labels[goal_state][0],
            distance_km=distance_km,
            travel_hours=travel_hours,
            average_risk=risk_exposure / travel_hours if travel_hours else 0.0,
            maximum_risk=max(edge.maximum_risk for edge in traversals),
            minimum_confidence=min(edge.confidence for edge in traversals),
            source_risk_ids=source_ids,
            metrics=_metrics(counters, started),
        )

    def _zero_length_result(
        self,
        request: PlanningRequest,
        sample: SampledRisk,
        started: float,
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
            metrics=_metrics(_Counters(), started),
        )

    def _cost_model(self, objective: ObjectiveMode) -> CostModel:
        return CostModel(
            weights=self._weights[objective],
            maximum_speed_km_per_hour=(self.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR),
            full_turn_penalty_hours=self._full_turn_penalty_hours,
        )

    def _heuristic(
        self,
        node: Node,
        goal: Node,
        cost_model: CostModel,
        request: PlanningRequest,
    ) -> float:
        if not request.use_heuristic:
            return 0.0
        distance = self._heur_dist.get(node)
        if distance is None:
            distance = self.grid.distance_km(node, goal)
            self._heur_dist[node] = distance
        return cost_model.lower_bound(distance)

    def _edge_geometry(
        self,
        start: Node,
        end: Node,
        *,
        minimum_samples: int = 3,
    ) -> tuple[float, float, tuple[GeoPoint, ...]]:
        cache_key = (start, end, minimum_samples)
        cached = self._edge_cache.get(cache_key)
        if cached is None:
            self._edge_cache_misses += 1
            distance = self.grid.distance_km(start, end)
            heading = self.grid.heading_degrees(start, end)
            points = self.grid.edge_sample_points(
                start,
                end,
                minimum_samples=minimum_samples,
            )
            cached = (distance, heading, points)
            self._edge_cache[cache_key] = cached
        else:
            self._edge_cache_hits += 1
        return cached

    def _sample_node(self, node: Node, sampled_at: datetime) -> SampledRisk:
        point = self.grid.point(node)
        return self.risk_sampler.sample(sampled_at, point.longitude, point.latitude)

    def _validate_grid_alignment(self) -> None:
        payload = self.risk_sampler.frames[0].payload
        latitudes = tuple(float(value) for value in payload.coords["latitude"].values)
        longitudes = tuple(float(value) for value in payload.coords["longitude"].values)
        if self.grid.latitudes != latitudes or self.grid.longitudes != longitudes:
            raise ValueError("planner grid must exactly match the RiskFrame coordinate grid")

    def _validate_request_nodes(self, request: PlanningRequest) -> None:
        if not self.grid.contains(request.start):
            raise ValueError(f"start node {request.start} is outside the planning grid")
        if not self.grid.contains(request.goal):
            raise ValueError(f"goal node {request.goal} is outside the planning grid")

    @staticmethod
    def _check_cancelled(request: PlanningRequest) -> None:
        if request.cancel_check is not None and request.cancel_check():
            raise PlanningCancelled("planning request was cancelled")

    @staticmethod
    def _emit_progress(
        request: PlanningRequest,
        counters: _Counters,
        *,
        labels: Mapping[State, tuple[float, datetime]],
        open_size: int,
        started: float,
    ) -> None:
        """Emit one progress line to stderr; observability lives here, not in plan()."""

        elapsed = perf_counter() - started
        rate = counters.expanded / elapsed if elapsed > 0 else 0.0
        rss_mb = _peak_rss_mb()
        rss_part = f"rss={rss_mb:.0f}MB" if isfinite(rss_mb) else "rss=na"
        horizon_h = (
            request.maximum_elapsed.total_seconds() / 3600.0
            if request.maximum_elapsed is not None
            else 0.0
        )
        expanded_line = (
            f"elapsed={elapsed:.1f}s expanded={counters.expanded} "
            f"generated={counters.generated} "
        )
        set_line = (
            f"unique={len(labels)} open={open_size} stale={counters.stale_pop} "
        )
        reopen_line = (
            f"reopened={counters.reopened} rate={rate:.0f}/s "
            f"max_bucket={counters.max_bucket} "
        )
        print(
            f"[astar] obj={request.objective.value} horizon_h={horizon_h:.0f} "
            f"{expanded_line}{set_line}{reopen_line}"
            f"f={counters.last_f:.3f} g={counters.last_g:.3f} "
            f"h={counters.last_f - counters.last_g:.3f} {rss_part}",
            file=sys.stderr,
            flush=True,
        )


def _with_fractions(points: tuple[GeoPoint, ...]) -> Iterable[tuple[GeoPoint, float]]:
    denominator = len(points) - 1
    return ((point, index / denominator) for index, point in enumerate(points))


def _peak_rss_mb() -> float:
    """Peak RSS in MiB, or NaN where the Unix ``resource`` module is absent."""

    try:
        import resource
    except ImportError:
        return float("nan")
    try:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except OSError:
        return float("nan")


def _trapezoidal_average(values: Iterable[float]) -> float:
    sequence = tuple(values)
    if not sequence:
        raise ValueError("at least one risk sample is required")
    if len(sequence) == 1:
        return sequence[0]
    return (0.5 * sequence[0] + sum(sequence[1:-1]) + 0.5 * sequence[-1]) / (len(sequence) - 1)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _metrics(counters: _Counters, started: float) -> SearchMetrics:
    compute_ms = (perf_counter() - started) * 1_000.0
    if not isfinite(compute_ms):
        raise RuntimeError("non-finite planning duration")
    return SearchMetrics(
        expanded_states=counters.expanded,
        generated_states=counters.generated,
        rejected_hard_edges=counters.hard,
        rejected_risk_edges=counters.risk,
        rejected_speed_edges=counters.speed,
        rejected_coverage_edges=counters.coverage,
        queue_peak=counters.queue_peak,
        compute_ms=compute_ms,
        unique_states=counters.unique,
        heap_pushes=counters.heap_pushes,
        heap_pops=counters.heap_pops,
        stale_pops=counters.stale_pop,
        reopened_states=counters.reopened,
        max_time_index=counters.max_bucket,
    )
