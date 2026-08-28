"""Proof-carrying graph lower bounds for temporal arrival envelopes.

This module is a C-internal research sidecar.  It computes conservative
forward and reverse travel-time lower bounds over the *same finite adjacency*
used by a planner.  The result can be supplied to the existing corridor
certificate, but it is never installed on a planner implicitly and is not
exported through the public planner package.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from heapq import heappop, heappush
from math import isfinite, nextafter
from typing import Any

from .temporal_qualification import TemporalScope, canonical_digest

TOPOLOGICAL_BOUND_METHOD = "graph-max-speed-lower-bound-v1"
TOPOLOGICAL_BOUND_EVALUATOR = "certified:grid-adjacency-distance-max-speed-v1"


@dataclass(frozen=True, slots=True)
class TopologicalLowerBoundEvidence:
    """Audited graph lower bounds suitable for an explicit corridor call."""

    scope: TemporalScope
    start: Any
    goal: Any
    universe_nodes: tuple[Any, ...]
    adjacency: tuple[tuple[Any, tuple[Any, ...]], ...]
    edge_distances_km: tuple[tuple[Any, Any, float], ...]
    max_speed_km_per_hour: float
    forward_lower_hours: tuple[tuple[Any, float], ...]
    reverse_lower_hours: tuple[tuple[Any, float], ...]
    method: str = TOPOLOGICAL_BOUND_METHOD
    evaluator_digest: str = TOPOLOGICAL_BOUND_EVALUATOR
    proof_digest: str = ""
    admissible: bool = False
    coverage_complete: bool = False
    reason: str | None = None
    schema_version: str = "c.p0.2-temporal-topological-bound.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "universe_nodes", tuple(self.universe_nodes))
        object.__setattr__(self, "adjacency", tuple(self.adjacency))
        object.__setattr__(self, "edge_distances_km", tuple(self.edge_distances_km))
        object.__setattr__(self, "forward_lower_hours", tuple(self.forward_lower_hours))
        object.__setattr__(self, "reverse_lower_hours", tuple(self.reverse_lower_hours))
        if self.schema_version != "c.p0.2-temporal-topological-bound.v1":
            raise ValueError("unsupported topological lower-bound schema")
        if not self.method or not self.evaluator_digest or not self.proof_digest:
            raise ValueError("topological evidence requires stable method/evaluator/proof digests")
        if (
            not isfinite(self.max_speed_km_per_hour)
            or self.max_speed_km_per_hour <= 0.0
        ) and self.admissible:
            raise ValueError("maximum speed must be finite and positive")
        if not isinstance(self.admissible, bool) or not isinstance(self.coverage_complete, bool):
            raise ValueError("topological evidence flags must be boolean")
        for _node, value in (*self.forward_lower_hours, *self.reverse_lower_hours):
            if not isfinite(float(value)) or float(value) < 0.0:
                raise ValueError("topological lower bounds must be finite and non-negative")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "start": self.start,
                "goal": self.goal,
                "universe_nodes": self.universe_nodes,
                "adjacency": self.adjacency,
                "edge_distances_km": self.edge_distances_km,
                "max_speed_km_per_hour": self.max_speed_km_per_hour,
                "forward_lower_hours": self.forward_lower_hours,
                "reverse_lower_hours": self.reverse_lower_hours,
                "method": self.method,
                "evaluator_digest": self.evaluator_digest,
                "proof_digest": self.proof_digest,
                "admissible": self.admissible,
                "coverage_complete": self.coverage_complete,
                "reason": self.reason,
            }
        )

    @property
    def usable(self) -> bool:
        return (
            self.admissible
            and self.coverage_complete
            and self.reason is None
            and self.scope.evaluator_identity_known
            and bool(self.forward_lower_hours)
            and bool(self.reverse_lower_hours)
        )

    @property
    def forward_map(self) -> Mapping[Any, float]:
        return dict(self.forward_lower_hours)

    @property
    def reverse_map(self) -> Mapping[Any, float]:
        return dict(self.reverse_lower_hours)

    def as_admissible_bound_evidence(self) -> Any:
        """Adapt to the existing corridor evidence without a public export."""

        from .temporal_corridor import AdmissibleBoundEvidence

        return AdmissibleBoundEvidence(
            scope=self.scope,
            method=self.method,
            evaluator_digest=self.evaluator_digest,
            proof_digest=self.proof_digest,
            admissible=self.admissible,
            coverage_complete=self.coverage_complete,
        )


def _unique_nodes(values: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        try:
            if value in seen:
                continue
            seen.add(value)
        except TypeError as error:
            raise ValueError("topological-bound nodes must be hashable") from error
        result.append(value)
    return tuple(result)


def _failure(
    *,
    scope: TemporalScope,
    start: Any,
    goal: Any,
    universe_nodes: tuple[Any, ...],
    adjacency: tuple[tuple[Any, tuple[Any, ...]], ...],
    edge_distances_km: tuple[tuple[Any, Any, float], ...],
    max_speed_km_per_hour: float,
    reason: str,
    method: str,
    evaluator_digest: str,
) -> TopologicalLowerBoundEvidence:
    proof_digest = canonical_digest(
        {
            "schema_version": "c.p0.2-temporal-topological-bound.v1",
            "scope": scope.digest,
            "start": start,
            "goal": goal,
            "universe_nodes": universe_nodes,
            "adjacency": adjacency,
            "edge_distances_km": edge_distances_km,
            "max_speed_km_per_hour": max_speed_km_per_hour,
            "method": method,
            "evaluator_digest": evaluator_digest,
            "reason": reason,
        }
    )
    return TopologicalLowerBoundEvidence(
        scope=scope,
        start=start,
        goal=goal,
        universe_nodes=universe_nodes,
        adjacency=adjacency,
        edge_distances_km=edge_distances_km,
        max_speed_km_per_hour=max_speed_km_per_hour,
        forward_lower_hours=(),
        reverse_lower_hours=(),
        method=method,
        evaluator_digest=evaluator_digest,
        proof_digest=proof_digest,
        admissible=False,
        coverage_complete=False,
        reason=reason,
    )


def _shortest_lower_bounds(
    source: Any,
    nodes: tuple[Any, ...],
    edges: tuple[tuple[Any, Any, float], ...],
) -> dict[Any, float]:
    graph: dict[Any, list[tuple[Any, float]]] = {node: [] for node in nodes}
    for start, end, weight in edges:
        graph[start].append((end, weight))
    distances: dict[Any, float] = {source: 0.0}
    queue: list[tuple[float, int, Any]] = [(0.0, 0, source)]
    sequence = 1
    while queue:
        distance, _order, node = heappop(queue)
        if distance != distances.get(node):
            continue
        for neighbor, weight in graph[node]:
            # Each edge weight is already a lower bound.  Round the sum down
            # and clamp zero so a zero-length edge remains non-negative.
            candidate = max(0.0, nextafter(distance + weight, float("-inf")))
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                heappush(queue, (candidate, sequence, neighbor))
                sequence += 1
    return distances


def qualify_topological_lower_bound(
    *,
    scope: TemporalScope | Mapping[str, Any],
    universe_nodes: Iterable[Any],
    start: Any,
    goal: Any,
    neighbors: Callable[[Any], Iterable[Any]],
    edge_distance_km: Callable[[Any, Any], float],
    max_speed_km_per_hour: float,
    method: str = TOPOLOGICAL_BOUND_METHOD,
    evaluator_digest: str = TOPOLOGICAL_BOUND_EVALUATOR,
) -> TopologicalLowerBoundEvidence:
    """Qualify a complete finite graph as a conservative travel-time bound.

    Every enumerated neighbor must belong to ``universe_nodes``.  This is an
    important safety fence: silently omitting an outside edge could overstate
    the shortest path and make an unsafe exclusion.  The graph is treated as
    directed, and reverse distances are computed by reversing the observed
    edge list.  Any evaluator or coverage failure yields an unusable record.
    """

    active_scope = TemporalScope.from_mapping(scope)
    try:
        speed = float(max_speed_km_per_hour)
    except (TypeError, ValueError):
        speed = 0.0
    if not isfinite(speed):
        speed = 0.0
    try:
        universe = _unique_nodes(universe_nodes)
    except (TypeError, ValueError):
        universe = ()
    empty_adjacency: tuple[tuple[Any, tuple[Any, ...]], ...] = ()
    empty_edges: tuple[tuple[Any, Any, float], ...] = ()
    if not universe:
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=empty_adjacency,
            edge_distances_km=empty_edges,
            max_speed_km_per_hour=speed,
            reason="empty_universe",
            method=method,
            evaluator_digest=evaluator_digest,
        )
    try:
        universe_set = set(universe)
    except TypeError:
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=empty_adjacency,
            edge_distances_km=empty_edges,
            max_speed_km_per_hour=speed,
            reason="unhashable_universe",
            method=method,
            evaluator_digest=evaluator_digest,
        )
    if start not in universe_set or goal not in universe_set:
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=empty_adjacency,
            edge_distances_km=empty_edges,
            max_speed_km_per_hour=speed,
            reason="start_or_goal_outside_universe",
            method=method,
            evaluator_digest=evaluator_digest,
        )
    if not isfinite(speed) or speed <= 0.0:
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=empty_adjacency,
            edge_distances_km=empty_edges,
            max_speed_km_per_hour=speed,
            reason="invalid_max_speed",
            method=method,
            evaluator_digest=evaluator_digest,
        )
    if not active_scope.evaluator_identity_known:
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=empty_adjacency,
            edge_distances_km=empty_edges,
            max_speed_km_per_hour=speed,
            reason="unknown_evaluator",
            method=method,
            evaluator_digest=evaluator_digest,
        )

    adjacency_rows: list[tuple[Any, tuple[Any, ...]]] = []
    edge_rows: list[tuple[Any, Any, float]] = []
    try:
        for node in universe:
            seen_neighbors: set[Any] = set()
            row_neighbors: list[Any] = []
            for neighbor in neighbors(node):
                if neighbor not in universe_set:
                    raise ValueError("adjacency_outside_universe")
                if neighbor in seen_neighbors:
                    continue
                seen_neighbors.add(neighbor)
                distance = float(edge_distance_km(node, neighbor))
                if not isfinite(distance) or distance < 0.0:
                    raise ValueError("invalid_edge_distance")
                row_neighbors.append(neighbor)
                edge_rows.append((node, neighbor, distance))
            adjacency_rows.append((node, tuple(row_neighbors)))
    except Exception as error:
        reason = str(error) if str(error) in {
            "adjacency_outside_universe",
            "invalid_edge_distance",
        } else f"evaluator_failure:{type(error).__name__}"
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=tuple(adjacency_rows),
            edge_distances_km=tuple(edge_rows),
            max_speed_km_per_hour=speed,
            reason=reason,
            method=method,
            evaluator_digest=evaluator_digest,
        )

    edge_lower_rows = tuple(
        (
            edge_start,
            edge_end,
            max(0.0, nextafter(distance / speed, float("-inf"))),
        )
        for edge_start, edge_end, distance in edge_rows
    )
    forward = _shortest_lower_bounds(start, universe, edge_lower_rows)
    reverse_edges = tuple((end, begin, weight) for begin, end, weight in edge_lower_rows)
    reverse = _shortest_lower_bounds(goal, universe, reverse_edges)
    if len(forward) != len(universe) or len(reverse) != len(universe):
        return _failure(
            scope=active_scope,
            start=start,
            goal=goal,
            universe_nodes=universe,
            adjacency=tuple(adjacency_rows),
            edge_distances_km=tuple(edge_rows),
            max_speed_km_per_hour=speed,
            reason="unreachable_domain",
            method=method,
            evaluator_digest=evaluator_digest,
        )

    forward_rows = tuple((node, forward[node]) for node in universe)
    reverse_rows = tuple((node, reverse[node]) for node in universe)
    proof_digest = canonical_digest(
        {
            "schema_version": "c.p0.2-temporal-topological-bound.v1",
            "scope": active_scope.digest,
            "start": start,
            "goal": goal,
            "universe_nodes": universe,
            "adjacency": tuple(adjacency_rows),
            "edge_distances_km": tuple(edge_rows),
            "edge_lower_hours": edge_lower_rows,
            "max_speed_km_per_hour": speed,
            "forward_lower_hours": forward_rows,
            "reverse_lower_hours": reverse_rows,
            "method": method,
            "evaluator_digest": evaluator_digest,
        }
    )
    return TopologicalLowerBoundEvidence(
        scope=active_scope,
        start=start,
        goal=goal,
        universe_nodes=universe,
        adjacency=tuple(adjacency_rows),
        edge_distances_km=tuple(edge_rows),
        max_speed_km_per_hour=speed,
        forward_lower_hours=forward_rows,
        reverse_lower_hours=reverse_rows,
        method=method,
        evaluator_digest=evaluator_digest,
        proof_digest=proof_digest,
        admissible=True,
        coverage_complete=True,
    )


__all__ = [
    "TOPOLOGICAL_BOUND_EVALUATOR",
    "TOPOLOGICAL_BOUND_METHOD",
    "TopologicalLowerBoundEvidence",
    "qualify_topological_lower_bound",
]
