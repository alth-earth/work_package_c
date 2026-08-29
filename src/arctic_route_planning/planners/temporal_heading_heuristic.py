"""Proof-carrying heading-aware objective lower bounds.

This is a C-internal research sidecar for the exact-arrival temporal search.
The ordinary certified heuristic is keyed only by node.  A temporal label also
contains the incoming grid heading, and a turn penalty is part of the cost.
This module computes a conservative lower bound on the finite expanded graph
``(node, incoming_heading)``.  It changes only queue ordering (and the
existing admissible incumbent test); it never removes a label by itself.

The certificate is deliberately explicit and default-off.  Incomplete graph
coverage, unknown identity, malformed bounds, or scope drift make it unusable
and callers must fall back to the safe baseline heuristic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from heapq import heappop, heappush
from math import isfinite, nextafter
from typing import Any

from arctic_route_planning.cost import CostModel
from arctic_route_planning.grid import Node, RegularGrid, heading_change_degrees

from .temporal_heuristic_bounds import _cost_model_digest
from .temporal_qualification import TemporalScope, canonical_digest

HeadingCode = tuple[int, int] | None
HeadingState = tuple[Node, HeadingCode]

HEADING_HEURISTIC_METHOD = "graph-heading-objective-lower-bound-v1"
HEADING_HEURISTIC_EVALUATOR = "certified:heading-expanded-cost-model-lower-bound-v1"
HEADING_HEURISTIC_SCHEMA = "c.p0.2-temporal-heading-heuristic-certificate.v1"


def _state_sort_key(state: HeadingState) -> tuple[int, int, int, int]:
    node, heading = state
    if heading is None:
        return node[0], node[1], 0, 0
    return node[0], node[1], heading[0] + 2, heading[1] + 2


def _unique_nodes(values: Iterable[Node]) -> tuple[Node, ...]:
    result: list[Node] = []
    seen: set[Node] = set()
    for value in values:
        node = (int(value[0]), int(value[1]))
        if node not in seen:
            seen.add(node)
            result.append(node)
    return tuple(result)


def _heading_states(grid: RegularGrid, nodes: tuple[Node, ...]) -> tuple[HeadingState, ...]:
    result: list[HeadingState] = []
    seen: set[HeadingState] = set()
    node_set = set(nodes)
    for node in nodes:
        candidate = (node, None)
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
        for previous in grid.neighbors(node):
            if previous not in node_set:
                raise ValueError("heading graph neighbor outside finite universe")
            heading = (node[0] - previous[0], node[1] - previous[1])
            candidate = (node, heading)
            if candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
    return tuple(sorted(result, key=_state_sort_key))


def _transition_lower_bound(
    grid: RegularGrid,
    state: HeadingState,
    neighbour: Node,
    cost_model: CostModel,
) -> tuple[HeadingState, float]:
    node, incoming = state
    heading = (neighbour[0] - node[0], neighbour[1] - node[1])
    distance = grid.distance_km(node, neighbour)
    travel_lower = distance / cost_model.maximum_speed_km_per_hour
    previous_heading = None
    if incoming is not None:
        previous = (node[0] - incoming[0], node[1] - incoming[1])
        if grid.contains(previous):
            previous_heading = grid.heading_degrees(previous, node)
    turn_degrees = heading_change_degrees(previous_heading, grid.heading_degrees(node, neighbour))
    weights = cost_model.weights
    lower = (
        weights.travel_time + weights.distance
    ) * travel_lower + weights.turn * turn_degrees / 180.0 * cost_model.full_turn_penalty_hours
    if not isfinite(lower) or lower < 0.0:
        raise ValueError("heading lower bound is not finite and non-negative")
    return (neighbour, heading), max(0.0, nextafter(lower, float("-inf")))


@dataclass(frozen=True, slots=True)
class TemporalHeadingHeuristicCertificate:
    """A complete, consistent lower-bound map over heading-expanded states."""

    scope: TemporalScope
    objective: str
    universe_states: tuple[HeadingState, ...]
    objective_lower_hours: tuple[tuple[HeadingState, float], ...]
    cost_model_digest: str
    proof_digest: str
    admissible: bool = False
    consistent: bool = False
    coverage_complete: bool = False
    reason: str | None = None
    method: str = HEADING_HEURISTIC_METHOD
    evaluator_digest: str = HEADING_HEURISTIC_EVALUATOR
    schema_version: str = HEADING_HEURISTIC_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        states = tuple(self.universe_states)
        bounds = tuple(self.objective_lower_hours)
        seen_states: set[HeadingState] = set()
        normalized_states: list[HeadingState] = []
        for state in states:
            if not isinstance(state, (tuple, list)) or len(state) != 2:
                raise ValueError("heading universe states must be (node, heading) pairs")
            node, heading = state
            normalized_heading = None if heading is None else (int(heading[0]), int(heading[1]))
            normalized = ((int(node[0]), int(node[1])), normalized_heading)
            if normalized in seen_states:
                raise ValueError("heading universe states must be unique")
            seen_states.add(normalized)
            normalized_states.append(normalized)
        object.__setattr__(self, "universe_states", tuple(normalized_states))
        normalized_bounds: list[tuple[HeadingState, float]] = []
        seen_bounds: set[HeadingState] = set()
        for item in bounds:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("heading lower bounds must be state/value pairs")
            state, raw_value = item
            if not isinstance(state, (tuple, list)) or len(state) != 2:
                raise ValueError("heading lower-bound state is malformed")
            node, heading = state
            normalized_heading = None if heading is None else (int(heading[0]), int(heading[1]))
            normalized = ((int(node[0]), int(node[1])), normalized_heading)
            if normalized in seen_bounds:
                raise ValueError("heading lower bounds must be unique")
            value = float(raw_value)
            if not isfinite(value) or value < 0.0:
                raise ValueError("heading lower bounds must be finite and non-negative")
            seen_bounds.add(normalized)
            normalized_bounds.append((normalized, value))
        object.__setattr__(self, "objective_lower_hours", tuple(normalized_bounds))
        if self.schema_version != HEADING_HEURISTIC_SCHEMA:
            raise ValueError("unsupported heading heuristic certificate schema")
        if not self.objective or not self.cost_model_digest or not self.proof_digest:
            raise ValueError("heading heuristic certificate requires stable identity digests")
        for name in ("admissible", "consistent", "coverage_complete"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "objective": self.objective,
                "universe_states": self.universe_states,
                "objective_lower_hours": self.objective_lower_hours,
                "cost_model_digest": self.cost_model_digest,
                "proof_digest": self.proof_digest,
                "admissible": self.admissible,
                "consistent": self.consistent,
                "coverage_complete": self.coverage_complete,
                "reason": self.reason,
                "method": self.method,
                "evaluator_digest": self.evaluator_digest,
            }
        )

    @property
    def lower_bound_map(self) -> Mapping[HeadingState, float]:
        return dict(self.objective_lower_hours)

    @property
    def usable(self) -> bool:
        scope_objective = self.scope.mapping.get("objective")
        return (
            self.admissible
            and self.consistent
            and self.coverage_complete
            and self.reason is None
            and self.scope.evaluator_identity_known
            and bool(self.universe_states)
            and set(self.universe_states) == set(self.lower_bound_map)
            and (scope_objective is None or str(scope_objective) == self.objective)
            and not self.evaluator_digest.startswith(("unknown:", "type:"))
        )

    def permits(self, expected_scope: TemporalScope | Mapping[str, Any]) -> bool:
        return self.usable and self.scope.matches(expected_scope)

    def lower_bound(self, node: Node, heading: HeadingCode) -> float | None:
        return self.lower_bound_map.get((node, heading))

    @classmethod
    def from_grid(
        cls,
        *,
        scope: TemporalScope | Mapping[str, Any],
        grid: RegularGrid,
        nodes: Iterable[Node],
        goal: Node,
        cost_model: CostModel,
        objective: str,
        expected_scope: TemporalScope | Mapping[str, Any] | None = None,
    ) -> TemporalHeadingHeuristicCertificate:
        active_scope = TemporalScope.from_mapping(scope)
        try:
            universe = _unique_nodes(nodes)
        except (IndexError, KeyError, TypeError, ValueError):
            universe = ()
        reason: str | None = None
        admissible = False
        consistent = False
        complete = False
        bounds: tuple[tuple[HeadingState, float], ...] = ()
        try:
            if expected_scope is not None and not active_scope.matches(expected_scope):
                reason = "scope_mismatch"
            elif not objective:
                reason = "missing_objective"
            elif active_scope.mapping.get("objective") not in (None, objective):
                reason = "objective_scope_mismatch"
            elif goal not in set(universe):
                reason = "goal_outside_universe"
            elif not universe:
                reason = "empty_universe"
            else:
                states = _heading_states(grid, universe)
                state_set = set(states)
                reverse: dict[HeadingState, list[tuple[HeadingState, float]]] = {
                    state: [] for state in states
                }
                for state in states:
                    node, _heading = state
                    for neighbour in grid.neighbors(node):
                        if neighbour not in set(universe):
                            raise ValueError("adjacency coverage incomplete")
                        next_state, lower = _transition_lower_bound(
                            grid, state, neighbour, cost_model
                        )
                        if next_state not in state_set:
                            raise ValueError("heading transition outside state universe")
                        reverse[next_state].append((state, lower))
                distances: dict[HeadingState, float] = {}
                queue: list[tuple[float, int, HeadingState]] = []
                sequence = 0
                for state in states:
                    if state[0] == goal:
                        distances[state] = 0.0
                        heappush(queue, (0.0, sequence, state))
                        sequence += 1
                while queue:
                    distance, _order, state = heappop(queue)
                    if distance != distances.get(state):
                        continue
                    for predecessor, lower in reverse[state]:
                        candidate = max(0.0, nextafter(distance + lower, float("-inf")))
                        if candidate < distances.get(predecessor, float("inf")):
                            distances[predecessor] = candidate
                            heappush(queue, (candidate, sequence, predecessor))
                            sequence += 1
                bounds = tuple(sorted(distances.items(), key=lambda item: _state_sort_key(item[0])))
                complete = len(distances) == len(states)
                if not complete:
                    reason = "coverage_incomplete"
                else:
                    admissible = True
                    consistent = True
        except (IndexError, KeyError, TypeError, ValueError, OverflowError) as error:
            reason = f"invalid_heading_graph:{type(error).__name__}"

        proof_digest = canonical_digest(
            {
                "schema_version": HEADING_HEURISTIC_SCHEMA,
                "scope": active_scope.digest,
                "objective": objective,
                "universe": universe,
                "goal": goal,
                "bounds": bounds,
                "cost_model_digest": _cost_model_digest(cost_model),
                "method": HEADING_HEURISTIC_METHOD,
                "evaluator_digest": HEADING_HEURISTIC_EVALUATOR,
                "admissible": admissible,
                "consistent": consistent,
                "coverage_complete": complete,
                "reason": reason,
            }
        )
        try:
            normalized_states = _heading_states(grid, universe) if universe else ()
        except (IndexError, KeyError, TypeError, ValueError):
            normalized_states = ()
        return cls(
            scope=active_scope,
            objective=objective,
            universe_states=normalized_states,
            objective_lower_hours=bounds,
            cost_model_digest=_cost_model_digest(cost_model),
            proof_digest=proof_digest,
            admissible=admissible,
            consistent=consistent,
            coverage_complete=complete,
            reason=reason,
        )


def qualify_heading_heuristic(
    *,
    scope: TemporalScope | Mapping[str, Any],
    grid: RegularGrid,
    nodes: Iterable[Node],
    goal: Node,
    cost_model: CostModel,
    objective: str,
    expected_scope: TemporalScope | Mapping[str, Any] | None = None,
) -> TemporalHeadingHeuristicCertificate:
    """Build a heading-expanded certificate for an explicit research call."""

    return TemporalHeadingHeuristicCertificate.from_grid(
        scope=scope,
        grid=grid,
        nodes=nodes,
        goal=goal,
        cost_model=cost_model,
        objective=objective,
        expected_scope=expected_scope,
    )


__all__ = [
    "HEADING_HEURISTIC_EVALUATOR",
    "HEADING_HEURISTIC_METHOD",
    "HEADING_HEURISTIC_SCHEMA",
    "TemporalHeadingHeuristicCertificate",
    "qualify_heading_heuristic",
]
