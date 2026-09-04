"""Time-dependent A* over an implicit rectilinear grid."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
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
    SpeedEstimate,
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
from .eta_refinement import (
    EtaEvaluation,
    EtaRefinementError,
    EtaRefinementPolicy,
    refine_eta,
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
    traversal_cache_hits: int = 0
    traversal_cache_misses: int = 0


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


@dataclass(frozen=True, slots=True)
class _EdgeTraversalData:
    """Objective-independent edge evaluation result for cross-objective reuse."""

    arrival_time: datetime
    heading_degrees: float
    speed_knots: float
    distance_km: float
    travel_hours: float
    risk_score: float
    maximum_risk: float
    confidence: float
    heading_change_degrees: float
    source_risk_ids: tuple[str, ...]


_CACHE_MISS = object()


@dataclass(frozen=True, slots=True)
class _CachedRejection:
    """Small, traceback-free representation of a rejected edge.

    Storing the original exception in the cross-objective cache retains its
    traceback (and therefore hot-loop frames and locals) until the cache is
    released.  A rejection is deterministic for the exact cache key, so only
    its stable kind/detail need to be retained; a fresh exception is raised on
    a later cache hit.
    """

    kind: str
    detail: str


def _cache_entry_shallow_size(
    key: tuple[Node, Node, datetime, HeadingCode],
    value: _EdgeTraversalData | _CachedRejection,
) -> int:
    """Estimate cache footprint without traversing shared object graphs.

    The diagnostic is deliberately a lower-bound estimate: it includes the
    dictionary key/value containers and their immediate scalar/tuple members,
    but not allocator or dictionary-table overhead.  It is only collected by
    the explicit research diagnostic mode and is never used as a production
    resource limit.
    """

    size = sys.getsizeof(key) + sum(sys.getsizeof(item) for item in key)
    size += sys.getsizeof(value)
    if isinstance(value, _EdgeTraversalData):
        members = (
            value.arrival_time,
            value.heading_degrees,
            value.speed_knots,
            value.distance_km,
            value.travel_hours,
            value.risk_score,
            value.maximum_risk,
            value.confidence,
            value.heading_change_degrees,
            value.source_risk_ids,
        )
        size += sum(sys.getsizeof(item) for item in members)
        size += sum(sys.getsizeof(item) for item in value.source_risk_ids)
    else:
        size += sys.getsizeof(value.kind) + sys.getsizeof(value.detail)
    return size


@dataclass(slots=True)
class _TraversalCacheStats:
    """Per-call observational statistics for the shared traversal cache."""

    hits: int = 0
    misses: int = 0
    accepted_hits: int = 0
    rejected_hits: int = 0
    accepted_misses: int = 0
    rejected_misses: int = 0
    entries: int = 0
    peak_entries: int = 0
    rejected_entries: int = 0
    diagnostics_enabled: bool = False
    exact_key_lookups: int = 0
    exact_key_hits: int = 0
    exact_key_misses: int = 0
    physical_edge_reuse_lookups: int = 0
    time_variant_exact_misses: int = 0
    estimated_shallow_bytes: int = 0
    peak_estimated_shallow_bytes: int = 0
    objective_lookups: dict[str, int] = field(default_factory=dict)
    objective_hits: dict[str, int] = field(default_factory=dict)
    objective_misses: dict[str, int] = field(default_factory=dict)
    _seen_exact_keys: set[tuple[Node, Node, datetime, HeadingCode]] | None = field(
        default=None,
        repr=False,
    )
    _physical_departures: dict[tuple[Node, Node, HeadingCode], set[datetime]] | None = field(
        default=None,
        repr=False,
    )
    _time_variant_keys: set[tuple[Node, Node, datetime, HeadingCode]] | None = field(
        default=None,
        repr=False,
    )

    def record_lookup(
        self,
        key: tuple[Node, Node, datetime, HeadingCode],
        objective: ObjectiveMode,
        *,
        hit: bool,
    ) -> None:
        """Record opt-in exact-key and physical-edge reuse diagnostics."""

        if not self.diagnostics_enabled:
            return
        self.exact_key_lookups += 1
        if hit:
            self.exact_key_hits += 1
        else:
            self.exact_key_misses += 1
        objective_name = objective.value
        self.objective_lookups[objective_name] = (
            self.objective_lookups.get(objective_name, 0) + 1
        )
        objective_counts = self.objective_hits if hit else self.objective_misses
        objective_counts[objective_name] = objective_counts.get(objective_name, 0) + 1

        if self._seen_exact_keys is None:
            self._seen_exact_keys = set()
        if self._physical_departures is None:
            self._physical_departures = {}
        if self._time_variant_keys is None:
            self._time_variant_keys = set()
        start, end, departure_time, incoming_code = key
        physical_key = (start, end, incoming_code)
        departures = self._physical_departures.get(physical_key)
        if departures is None:
            departures = set()
            self._physical_departures[physical_key] = departures
        elif key not in self._seen_exact_keys:
            self.time_variant_exact_misses += 1
            self._time_variant_keys.add(key)
        if departures:
            self.physical_edge_reuse_lookups += 1
        departures.add(departure_time)
        self._seen_exact_keys.add(key)

    def record_entry(
        self,
        key: tuple[Node, Node, datetime, HeadingCode],
        value: _EdgeTraversalData | _CachedRejection,
    ) -> None:
        """Record a lower-bound entry-size estimate for an inserted key."""

        if not self.diagnostics_enabled:
            return
        self.estimated_shallow_bytes += _cache_entry_shallow_size(key, value)
        self.peak_estimated_shallow_bytes = max(
            self.peak_estimated_shallow_bytes,
            self.estimated_shallow_bytes,
        )


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
    cache_hits: int = 0
    cache_misses: int = 0


class _RejectedEdge(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
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
        eta_refinement_policy: EtaRefinementPolicy | None = None,
    ) -> None:
        self.grid = grid
        self.risk_sampler = risk_sampler
        self.vessel_model = vessel_model
        self.planner_config = planner_config or PlannerConfig()
        # C-ALG-03 (progressive): default None keeps the historical two-round
        # refinement so the formal route digest is unchanged.  Injecting an
        # EtaRefinementPolicy opts into the fail-closed damped fixed point.
        self.eta_refinement_policy = eta_refinement_policy
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
        self._last_traversal_cache_stats = _TraversalCacheStats()
        self._validate_grid_alignment()

    def _planning_speed(self, environment_speed_factor: float) -> SpeedEstimate:
        """Return the vessel speed used by C for ETA and route waypoints.

        The environmental factor is first evaluated by the vessel model so
        the existing minimum-factor and minimum-steerage checks remain
        authoritative.  The optional planner reserve then reduces only the
        operational planning speed.  It never changes B's factor, the
        vessel's declared maximum, or the later motion qualification limits.
        """

        physical = self.vessel_model.effective_speed(environment_speed_factor)
        reserve = self.planner_config.operational_speed_reserve_fraction
        if reserve == 0.0:
            return physical
        speed_knots = physical.speed_knots * (1.0 - reserve)
        if speed_knots < self.vessel_model.minimum_steerage_speed_knots:
            raise UnnavigableSpeedError(
                "operational speed reserve puts planned speed below the vessel's "
                "minimum steerage speed"
            )
        return replace(
            physical,
            speed_knots=speed_knots,
            speed_km_per_hour=speed_knots * KNOT_TO_KM_PER_HOUR,
            relative_to_economic_speed=speed_knots / self.vessel_model.economic_speed_knots,
        )

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

    @property
    def traversal_cache_stats(self) -> dict[str, int]:
        """Return shared-traversal statistics from the last candidate call."""

        stats = self._last_traversal_cache_stats
        return {
            "hits": stats.hits,
            "misses": stats.misses,
            "accepted_hits": stats.accepted_hits,
            "rejected_hits": stats.rejected_hits,
            "accepted_misses": stats.accepted_misses,
            "rejected_misses": stats.rejected_misses,
            "entries": stats.entries,
            "peak_entries": stats.peak_entries,
            "rejected_entries": stats.rejected_entries,
        }

    @property
    def traversal_cache_diagnostics(self) -> dict[str, object]:
        """Return opt-in exact-key diagnostics from the last candidate call.

        The private sets used to classify physical-edge/time variants are
        intentionally exposed only as aggregate counters.  A normal planner
        call returns ``enabled=False`` and does not retain diagnostic keys.
        """

        stats = self._last_traversal_cache_stats
        if not stats.diagnostics_enabled:
            return {
                "enabled": False,
                "exact_key_lookups": 0,
                "exact_key_hits": 0,
                "exact_key_misses": 0,
                "unique_exact_keys": 0,
                "unique_physical_edges": 0,
                "physical_edge_reuse_lookups": 0,
                "time_variant_exact_misses": 0,
                "time_variant_unique_keys": 0,
                "estimated_shallow_bytes": 0,
                "peak_estimated_shallow_bytes": 0,
                "objective": {},
            }
        objective = {
            name: {
                "lookups": stats.objective_lookups.get(name, 0),
                "hits": stats.objective_hits.get(name, 0),
                "misses": stats.objective_misses.get(name, 0),
            }
            for name in (mode.value for mode in ObjectiveMode)
        }
        return {
            "enabled": True,
            "exact_key_lookups": stats.exact_key_lookups,
            "exact_key_hits": stats.exact_key_hits,
            "exact_key_misses": stats.exact_key_misses,
            "unique_exact_keys": len(stats._seen_exact_keys or ()),
            "unique_physical_edges": len(stats._physical_departures or {}),
            "physical_edge_reuse_lookups": stats.physical_edge_reuse_lookups,
            "time_variant_exact_misses": stats.time_variant_exact_misses,
            "time_variant_unique_keys": len(stats._time_variant_keys or ()),
            "estimated_shallow_bytes": stats.estimated_shallow_bytes,
            "peak_estimated_shallow_bytes": stats.peak_estimated_shallow_bytes,
            "objective": objective,
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

        from ._archive.control_trace_reuse import ControlTraceCollector

        collector = ControlTraceCollector(
            self,
            request,
            identity=identity,
            observer=observer,
        )
        result = self._plan(request, trace=collector)
        return result, collector.trace

    def _plan(
        self,
        request: PlanningRequest,
        *,
        trace: object | None,
        traversal_cache: dict | None = None,
        cache_stats: _TraversalCacheStats | None = None,
        cache_write: bool = True,
    ) -> PlanningResult:
        started = perf_counter()
        self._last_counters: _Counters | None = None
        self._heur_dist = {}
        self._calm_speed = self._planning_speed(1.0)
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

            for neighbor in self.grid.neighbors(node):
                self._check_cancelled(request)
                try:
                    traversal = self._evaluate_edge_cached(
                        node,
                        neighbor,
                        arrival_time,
                        incoming_code,
                        request,
                        cost_model,
                        traversal_cache,
                        counters,
                        cache_stats,
                        cache_write,
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
        *,
        shared_edge_evaluation: bool = False,
        traversal_cache_diagnostics: bool = False,
    ) -> dict[ObjectiveMode, PlanningResult]:
        """Plan the same request under multiple objective policies.

        When *shared_edge_evaluation* is true the three objective searches
        share a session-scoped traversal cache so that risk sampling, speed
        calculation and edge geometry are evaluated at most once per
        (start, end, departure_time, incoming_code) tuple.  Each
        objective still maintains independent labels, predecessors and open
        sets, so the returned routes are identical to the non-shared path.
        ``traversal_cache_diagnostics`` is an internal research switch; when
        enabled it records aggregate exact-key and physical-edge reuse
        counters without exposing cache keys or changing search behavior.
        """

        modes = tuple(ObjectiveMode(raw_mode) for raw_mode in objectives)
        self._last_traversal_cache_stats = _TraversalCacheStats()
        if not shared_edge_evaluation:
            return {
                mode: self.plan(replace(request, objective=mode))
                for mode in modes
            }
        traversal_cache: dict = {}
        cache_stats = _TraversalCacheStats(
            diagnostics_enabled=traversal_cache_diagnostics,
        )
        self._last_traversal_cache_stats = cache_stats
        results: dict[ObjectiveMode, PlanningResult] = {}
        for index, mode in enumerate(modes):
            results[mode] = self._plan(
                replace(request, objective=mode),
                trace=None,
                traversal_cache=traversal_cache,
                cache_stats=cache_stats,
                # No later objective can consume entries written by the final
                # objective.  It may still read entries produced earlier.
                cache_write=index < len(modes) - 1,
            )
        cache_stats.entries = len(traversal_cache)
        return results

    def _evaluate_edge_data(
        self,
        start: Node,
        end: Node,
        departure_time: datetime,
        incoming_code: HeadingCode,
        request: PlanningRequest,
    ) -> _EdgeTraversalData:
        """Objective-independent edge evaluation: risk, speed, geometry.

        All inputs (start, end, departure_time, incoming_code, request
        parameters) are identical across objectives, so the result is
        deterministic and safe to share via a session-scoped cache.
        """
        previous_heading = None
        if incoming_code is not None:
            prev_node = (start[0] - incoming_code[0], start[1] - incoming_code[1])
            previous_heading = self._edge_geometry(prev_node, start)[1]

        distance_km, _, points = self._edge_geometry(
            start, end, minimum_samples=request.edge_sample_count
        )
        calm_speed = self._calm_speed
        initial_travel_hours = distance_km / calm_speed.speed_km_per_hour
        samples: tuple[SampledRisk, ...] = ()
        speed = calm_speed

        # C-ALG-03 correctness debt (progressive): the default remains the
        # historical fixed two-round refinement (exactly _EDGE_REFINEMENT_ROUNDS)
        # so route digest is unchanged on the formal path.  When an
        # ``eta_refinement_policy`` is injected, evaluation switches to the
        # fail-closed damped fixed point (eta_refinement.refine_eta) with the
        # same domain checks; non-convergence is reported via EtaRefinementError
        # instead of silently accepting an un-converged ETA.  The formal Winter
        # field ETA fixed point was observed to diverge under the damped update
        # (recorded as a known correctness debt; see SSOT C-ALG-03), so the
        # default must stay on the two-round path until a robust fixed-point
        # algorithm lands (C-ALG-03B).
        def _evaluate_at(guess_hours: float) -> EtaEvaluation:
            sampled = tuple(
                self.risk_sampler.sample(
                    departure_time + timedelta(hours=guess_hours * fraction),
                    point.longitude,
                    point.latitude,
                )
                for point, fraction in _with_fractions(points)
            )
            if any(sample.hard_mask for sample in sampled):
                raise _RejectedEdge("hard")
            if any(
                sample.confidence < self.planner_config.minimum_confidence
                for sample in sampled
            ):
                raise _RejectedEdge("risk")
            if request.maximum_risk is not None and any(
                sample.risk_score > request.maximum_risk for sample in sampled
            ):
                raise _RejectedEdge("risk")
            effective_speed = self._planning_speed(
                min(sample.environment_speed_factor for sample in sampled)
            )
            return EtaEvaluation(
                samples=sampled,
                speed=effective_speed,
                implied_travel_hours=distance_km / effective_speed.speed_km_per_hour,
            )

        if self.eta_refinement_policy is not None:
            try:
                refined = refine_eta(
                    initial_travel_hours,
                    _evaluate_at,
                    policy=self.eta_refinement_policy,
                )
            except EtaRefinementError as error:
                # A rejection raised inside the callback surfaces as
                # invalid_operator; restore the domain rejection exception so
                # cached evaluator callers keep their existing catch semantics.
                operator_exception = error.diagnostics.get("operator_exception")
                if error.reason == "invalid_operator" and isinstance(
                    operator_exception, str
                ):
                    message = str(error.diagnostics.get("operator_message", ""))
                    if operator_exception == "_RejectedEdge":
                        raise _RejectedEdge(message) from None
                    if operator_exception == "RiskCoverageError":
                        raise RiskCoverageError(message) from None
                    if operator_exception == "UnnavigableSpeedError":
                        raise UnnavigableSpeedError(message) from None
                # Non-rejection failures (cycle / max_iterations /
                # terminal_mismatch / unexpected invalid_operator) fail closed.
                raise
            samples = tuple(refined.evaluation.samples)
            speed = refined.evaluation.speed
            travel_hours = refined.travel_hours
        else:
            # Historical default: exactly two refinement rounds.
            travel_hours = initial_travel_hours
            for _ in range(_EDGE_REFINEMENT_ROUNDS):
                evaluated = _evaluate_at(travel_hours)
                samples = tuple(evaluated.samples)
                speed = evaluated.speed
                travel_hours = evaluated.implied_travel_hours

        risk_score = _trapezoidal_average(sample.risk_score for sample in samples)
        maximum_risk = max(sample.risk_score for sample in samples)
        confidence = min(sample.confidence for sample in samples)
        heading = self._edge_geometry(start, end)[1]
        return _EdgeTraversalData(
            arrival_time=departure_time + timedelta(hours=travel_hours),
            heading_degrees=heading,
            speed_knots=speed.speed_knots,
            distance_km=distance_km,
            travel_hours=travel_hours,
            risk_score=risk_score,
            maximum_risk=maximum_risk,
            confidence=confidence,
            heading_change_degrees=heading_change_degrees(previous_heading, heading),
            source_risk_ids=_unique(
                risk_id for sample in samples for risk_id in sample.source_risk_ids
            ),
        )

    def _compute_cost(
        self,
        data: _EdgeTraversalData,
        cost_model: CostModel,
    ) -> CostBreakdown:
        """Objective-specific cost from cached traversal data."""
        return cost_model.evaluate(
            EdgeCostInput(
                distance_km=data.distance_km,
                travel_hours=data.travel_hours,
                risk_score=data.risk_score,
                confidence=data.confidence,
                heading_change_degrees=data.heading_change_degrees,
            )
        )

    def _build_traversal(
        self,
        start: Node,
        end: Node,
        data: _EdgeTraversalData,
        cost: CostBreakdown,
    ) -> _EdgeTraversal:
        """Combine cached traversal data with objective-specific cost."""
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=data.arrival_time,
            heading_degrees=data.heading_degrees,
            speed_knots=data.speed_knots,
            distance_km=data.distance_km,
            risk_score=data.risk_score,
            maximum_risk=data.maximum_risk,
            confidence=data.confidence,
            cost=cost,
            source_risk_ids=data.source_risk_ids,
        )

    def _evaluate_edge_cached(
        self,
        start: Node,
        end: Node,
        departure_time: datetime,
        incoming_code: HeadingCode,
        request: PlanningRequest,
        cost_model: CostModel,
        traversal_cache: dict | None,
        counters: _Counters,
        cache_stats: _TraversalCacheStats | None = None,
        cache_write: bool = True,
    ) -> _EdgeTraversal:
        """Edge evaluation with optional cross-objective traversal cache.

        On cache hit only the cheap cost computation is redone; the
        expensive risk sampling and speed calculation are skipped.
        Rejected edges (hard mask, risk threshold, coverage, speed) are
        also cached so subsequent objectives skip the expensive evaluation.
        """
        cache_key = None
        if traversal_cache is not None:
            cache_key = (start, end, departure_time, incoming_code)
            cached = traversal_cache.get(cache_key, _CACHE_MISS)
            if cache_stats is not None:
                cache_stats.record_lookup(
                    cache_key,
                    request.objective,
                    hit=cached is not _CACHE_MISS,
                )
            if cached is not _CACHE_MISS:
                counters.cache_hits += 1
                if cache_stats is not None:
                    cache_stats.hits += 1
                if isinstance(cached, _CachedRejection):
                    if cache_stats is not None:
                        cache_stats.rejected_hits += 1
                    self._raise_cached_rejection(cached)
                if cache_stats is not None:
                    cache_stats.accepted_hits += 1
                cost = self._compute_cost(cached, cost_model)
                return self._build_traversal(start, end, cached, cost)

        if traversal_cache is not None:
            counters.cache_misses += 1
            if cache_stats is not None:
                cache_stats.misses += 1
        try:
            data = self._evaluate_edge_data(
                start, end, departure_time, incoming_code, request
            )
        except (_RejectedEdge, RiskCoverageError, UnnavigableSpeedError) as exc:
            if cache_stats is not None:
                cache_stats.rejected_misses += 1
            if traversal_cache is not None and cache_write:
                cached_rejection = self._cache_rejection(exc)
                traversal_cache[cache_key] = cached_rejection
                if cache_stats is not None:
                    cache_stats.record_entry(cache_key, cached_rejection)
                    cache_stats.rejected_entries += 1
                    cache_stats.entries = len(traversal_cache)
                    cache_stats.peak_entries = max(
                        cache_stats.peak_entries, cache_stats.entries
                    )
            raise

        if cache_stats is not None:
            cache_stats.accepted_misses += 1
        if traversal_cache is not None and cache_write:
            traversal_cache[cache_key] = data
            if cache_stats is not None:
                cache_stats.record_entry(cache_key, data)
                cache_stats.entries = len(traversal_cache)
                cache_stats.peak_entries = max(
                    cache_stats.peak_entries, cache_stats.entries
                )

        cost = self._compute_cost(data, cost_model)
        return self._build_traversal(start, end, data, cost)

    @staticmethod
    def _cache_rejection(
        exception: _RejectedEdge | RiskCoverageError | UnnavigableSpeedError,
    ) -> _CachedRejection:
        if isinstance(exception, _RejectedEdge):
            return _CachedRejection(f"rejected:{exception.reason}", exception.reason)
        if isinstance(exception, RiskCoverageError):
            return _CachedRejection("coverage", str(exception))
        return _CachedRejection("speed", str(exception))

    @staticmethod
    def _raise_cached_rejection(rejection: _CachedRejection) -> None:
        if rejection.kind.startswith("rejected:"):
            raise _RejectedEdge(rejection.detail)
        if rejection.kind == "coverage":
            raise RiskCoverageError(rejection.detail)
        if rejection.kind == "speed":
            raise UnnavigableSpeedError(rejection.detail)
        raise RuntimeError(f"unknown cached rejection kind: {rejection.kind}")

    def _evaluate_edge(
        self,
        start: Node,
        end: Node,
        departure_time: datetime,
        previous_heading: float | None,
        request: PlanningRequest,
        cost_model: CostModel,
    ) -> _EdgeTraversal:
        """Edge evaluation with full risk sampling and cost computation.

        This method is kept for direct callers.  The hot path in
        _plan now goes through _evaluate_edge_cached which may
        share the expensive risk-sampling step across objectives.
        """
        distance_km, _, points = self._edge_geometry(
            start, end, minimum_samples=request.edge_sample_count
        )
        calm_speed = self._calm_speed
        travel_hours = distance_km / calm_speed.speed_km_per_hour
        samples: tuple[SampledRisk, ...] = ()
        speed = calm_speed

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
                sample.confidence < self.planner_config.minimum_confidence
                for sample in samples
            ):
                raise _RejectedEdge("risk")
            if request.maximum_risk is not None and any(
                sample.risk_score > request.maximum_risk for sample in samples
            ):
                raise _RejectedEdge("risk")
            speed = self._planning_speed(
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
        traversal_cache_hits=counters.cache_hits,
        traversal_cache_misses=counters.cache_misses,
    )
