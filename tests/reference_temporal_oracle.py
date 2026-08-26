"""Independent exact-time Dijkstra oracle for the P0 semantics fixtures.

This module intentionally has no dependency on a production planner.  It is
kept under ``tests`` so that the oracle can be made deliberately small and
transparent: callers inject the graph's neighbours and the transition/edge
evaluator.  A state is identified by the exact UTC arrival timestamp, rather
than by a time bucket.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from math import isfinite
from time import perf_counter

type OracleNode = Hashable
type OracleHeading = Hashable | None
type OracleKey = tuple[OracleNode, OracleHeading, datetime]
type NeighbourProvider = Callable[[OracleNode], Iterable[OracleNode]]
type EdgeEvaluator = Callable[[OracleKey, OracleNode], "ReferenceEdge | None"]


@dataclass(frozen=True, slots=True)
class ReferenceEdge:
    """The minimum transition data required by the independent oracle."""

    arrival_time: datetime
    cost: float
    heading: OracleHeading = None


# A short alias makes fixtures read naturally while retaining an explicit
# name for the fact that this is test-only reference data.
OracleEdge = ReferenceEdge


@dataclass(frozen=True, slots=True)
class ReferenceOracleLimits:
    """Fail-closed limits for a small reference search."""

    max_expansions: int = 50_000
    max_labels: int = 100_000
    max_queue: int = 50_000
    max_edge_evaluations: int = 400_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_expansions", self.max_expansions),
            ("max_labels", self.max_labels),
            ("max_queue", self.max_queue),
            ("max_edge_evaluations", self.max_edge_evaluations),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def max_edge_evals(self) -> int:
        """Compatibility spelling for callers that use the shorter metric name."""

        return self.max_edge_evaluations


@dataclass(frozen=True, slots=True)
class ReferenceOracleMetrics:
    """Deterministic counters emitted by a reference search."""

    expanded_states: int
    generated_labels: int
    unique_labels: int
    queue_peak: int
    edge_evaluations: int
    heap_pushes: int
    heap_pops: int
    stale_pops: int
    horizon_rejections: int
    compute_ms: float


@dataclass(frozen=True, slots=True)
class ReferenceOracleResult:
    """A route and its exact-time labels, with no production schema fields."""

    states: tuple[OracleKey, ...]
    edges: tuple[ReferenceEdge, ...]
    total_cost: float
    metrics: ReferenceOracleMetrics

    @property
    def nodes(self) -> tuple[OracleNode, ...]:
        return tuple(state[0] for state in self.states)

    @property
    def headings(self) -> tuple[OracleHeading, ...]:
        return tuple(state[1] for state in self.states)

    @property
    def arrival_times(self) -> tuple[datetime, ...]:
        return tuple(state[2] for state in self.states)


class ReferenceOracleError(RuntimeError):
    """Base error for the independent reference search."""


class ReferenceOracleCancelled(ReferenceOracleError):
    """The caller's cooperative cancellation predicate returned true."""


class ReferenceOracleNoRoute(ReferenceOracleError):
    """The injected graph has no route satisfying its transitions."""


class ReferenceOracleHorizonExceeded(ReferenceOracleNoRoute):
    """All reachable routes were rejected by the optional elapsed-time limit."""


class ReferenceOracleInvalidEdge(ReferenceOracleError):
    """An injected edge violated exact-time or non-negative-cost semantics."""


class ReferenceOracleLimitExceeded(ReferenceOracleError):
    """A bounded reference run exceeded one explicit resource limit."""

    def __init__(self, resource: str, limit: int, observed: int) -> None:
        self.resource = resource
        self.limit = limit
        self.observed = observed
        super().__init__(f"reference oracle exceeded {resource}={limit} (observed={observed})")


class ReferenceTemporalOracle:
    """Zero-heuristic Dijkstra over injected exact-time transitions.

    The evaluator receives the current exact ``(node, heading, arrival)`` key
    and the proposed neighbour.  It may return ``None`` to reject an edge.
    Only labels with the same exact key can replace one another; two labels
    whose timestamps merely fall in the same bucket remain distinct.
    """

    def __init__(
        self,
        neighbours: NeighbourProvider,
        evaluate_edge: EdgeEvaluator,
        *,
        limits: ReferenceOracleLimits | None = None,
    ) -> None:
        self._neighbours = neighbours
        self._evaluate_edge = evaluate_edge
        self._limits = limits or ReferenceOracleLimits()

    @property
    def limits(self) -> ReferenceOracleLimits:
        return self._limits

    def search(
        self,
        start: OracleNode,
        goal: OracleNode,
        departure_time: datetime,
        *,
        start_heading: OracleHeading = None,
        maximum_elapsed: timedelta | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> ReferenceOracleResult:
        """Return the least-cost route, or fail closed with an explicit error."""

        departure_time = _require_utc(departure_time, name="departure_time")
        if maximum_elapsed is not None and maximum_elapsed <= timedelta(0):
            raise ValueError("maximum_elapsed must be positive")

        started = perf_counter()
        start_key: OracleKey = (start, start_heading, departure_time)
        labels: dict[OracleKey, float] = {start_key: 0.0}
        predecessor: dict[OracleKey, tuple[OracleKey, ReferenceEdge]] = {}
        queue: list[tuple[float, int, OracleKey]] = []
        serial = 0
        heappush(queue, (0.0, serial, start_key))
        counters = _MutableMetrics(queue_peak=1)

        while queue:
            _check_cancelled(cancel_check)
            queued_cost, _, state = heappop(queue)
            counters.heap_pops += 1
            if queued_cost != labels.get(state):
                counters.stale_pops += 1
                continue

            counters.expanded_states += 1
            _check_limit(
                "expansions", counters.expanded_states, self._limits.max_expansions
            )
            if state[0] == goal:
                return _result_from_goal(
                    state,
                    labels[state],
                    len(labels),
                    predecessor,
                    counters,
                    started,
                )

            for neighbour in self._neighbours(state[0]):
                _check_cancelled(cancel_check)
                counters.edge_evaluations += 1
                _check_limit(
                    "edge_evaluations",
                    counters.edge_evaluations,
                    self._limits.max_edge_evaluations,
                )
                edge = self._evaluate_edge(state, neighbour)
                if edge is None:
                    continue
                _validate_edge(state, edge)
                if maximum_elapsed is not None and (
                    edge.arrival_time - departure_time > maximum_elapsed
                ):
                    counters.horizon_rejections += 1
                    continue

                next_state: OracleKey = (neighbour, edge.heading, edge.arrival_time)
                next_cost = queued_cost + edge.cost
                previous_cost = labels.get(next_state)
                if previous_cost is not None and next_cost >= previous_cost:
                    continue

                labels[next_state] = next_cost
                predecessor[next_state] = (state, edge)
                _check_limit("labels", len(labels), self._limits.max_labels)
                serial += 1
                heappush(queue, (next_cost, serial, next_state))
                counters.generated_labels += 1
                counters.heap_pushes += 1
                counters.queue_peak = max(counters.queue_peak, len(queue))
                _check_limit("queue", len(queue), self._limits.max_queue)

        if counters.horizon_rejections:
            raise ReferenceOracleHorizonExceeded(
                "no complete route fits inside the reference elapsed-time horizon"
            )
        raise ReferenceOracleNoRoute("no route satisfies the injected transitions")


def reference_dijkstra(
    start: OracleNode,
    goal: OracleNode,
    departure_time: datetime,
    neighbours: NeighbourProvider,
    evaluate_edge: EdgeEvaluator,
    *,
    limits: ReferenceOracleLimits | None = None,
    **kwargs: object,
) -> ReferenceOracleResult:
    """Functional convenience wrapper around :class:`ReferenceTemporalOracle`."""

    return ReferenceTemporalOracle(neighbours, evaluate_edge, limits=limits).search(
        start,
        goal,
        departure_time,
        **kwargs,
    )


@dataclass(slots=True)
class _MutableMetrics:
    expanded_states: int = 0
    generated_labels: int = 0
    queue_peak: int = 0
    edge_evaluations: int = 0
    heap_pushes: int = 1
    heap_pops: int = 0
    stale_pops: int = 0
    horizon_rejections: int = 0


def _result_from_goal(
    goal: OracleKey,
    total_cost: float,
    unique_labels: int,
    predecessor: dict[OracleKey, tuple[OracleKey, ReferenceEdge]],
    counters: _MutableMetrics,
    started: float,
) -> ReferenceOracleResult:
    states: list[OracleKey] = [goal]
    edges: list[ReferenceEdge] = []
    state = goal
    while state in predecessor:
        previous, edge = predecessor[state]
        states.append(previous)
        edges.append(edge)
        state = previous
    states.reverse()
    edges.reverse()
    elapsed_ms = (perf_counter() - started) * 1_000.0
    if not isfinite(elapsed_ms):
        raise RuntimeError("non-finite reference oracle duration")
    metrics = ReferenceOracleMetrics(
        expanded_states=counters.expanded_states,
        generated_labels=counters.generated_labels,
        unique_labels=unique_labels,
        queue_peak=counters.queue_peak,
        edge_evaluations=counters.edge_evaluations,
        heap_pushes=counters.heap_pushes,
        heap_pops=counters.heap_pops,
        stale_pops=counters.stale_pops,
        horizon_rejections=counters.horizon_rejections,
        compute_ms=elapsed_ms,
    )
    return ReferenceOracleResult(tuple(states), tuple(edges), total_cost, metrics)


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ReferenceOracleCancelled("reference oracle search was cancelled")


def _check_limit(resource: str, observed: int, limit: int) -> None:
    if observed > limit:
        raise ReferenceOracleLimitExceeded(resource, limit, observed)


def _require_utc(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC")
    return value.astimezone(UTC)


def _validate_edge(state: OracleKey, edge: ReferenceEdge) -> None:
    arrival = _require_utc(edge.arrival_time, name="edge.arrival_time")
    if arrival != edge.arrival_time:
        raise ReferenceOracleInvalidEdge("edge.arrival_time must be normalized to UTC")
    if arrival <= state[2]:
        raise ReferenceOracleInvalidEdge("edge arrival must be strictly later than departure")
    if not isfinite(edge.cost) or edge.cost < 0:
        raise ReferenceOracleInvalidEdge("edge cost must be finite and non-negative")
