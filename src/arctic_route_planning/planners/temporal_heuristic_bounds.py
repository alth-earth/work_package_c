"""Certified lower-bound heuristics for the C temporal research path.

The production planner already has a geometric lower bound for ordinary A*.
This module makes a stronger *finite-graph* lower bound explicit for the
non-FIFO research adapter.  It is ordering evidence only: it does not exclude
labels and it is never installed implicitly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import isfinite, nextafter
from typing import Any

from arctic_route_planning.cost import CostModel

from .temporal_qualification import TemporalScope, canonical_digest
from .temporal_topology_bounds import TopologicalLowerBoundEvidence

HEURISTIC_METHOD = "graph-topological-objective-lower-bound-v1"
HEURISTIC_EVALUATOR = "certified:cost-model-graph-lower-bound-v1"
HEURISTIC_SCHEMA = "c.p0.2-temporal-heuristic-certificate.v1"


def _unique(values: Iterable[Any]) -> tuple[Any, ...]:
    result: list[Any] = []
    seen: set[Any] = set()
    for value in values:
        try:
            if value in seen:
                continue
            seen.add(value)
        except TypeError as error:
            raise ValueError("heuristic universe nodes must be hashable") from error
        result.append(value)
    return tuple(result)


def _normalise_bounds(
    values: Mapping[Any, float] | Iterable[tuple[Any, float]],
) -> tuple[tuple[Any, float], ...]:
    items = values.items() if isinstance(values, Mapping) else values
    result: list[tuple[Any, float]] = []
    seen: set[Any] = set()
    for item in items:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ValueError("heuristic lower bounds must be node/value pairs")
        node, raw_value = item
        try:
            if node in seen:
                raise ValueError("heuristic lower bounds must not contain duplicates")
            seen.add(node)
        except TypeError as error:
            raise ValueError("heuristic lower-bound nodes must be hashable") from error
        value = float(raw_value)
        if not isfinite(value) or value < 0.0:
            raise ValueError("heuristic lower bounds must be finite and non-negative")
        result.append((node, value))
    return tuple(result)


def _cost_model_digest(cost_model: CostModel) -> str:
    return canonical_digest(
        {
            "weights": cost_model.weights,
            "maximum_speed_km_per_hour": cost_model.maximum_speed_km_per_hour,
            "full_turn_penalty_hours": cost_model.full_turn_penalty_hours,
            "full_deviation_penalty_hours": cost_model.full_deviation_penalty_hours,
            "deviation_weight": cost_model.deviation_weight,
            "policy_version": cost_model.policy_version,
        }
    )


@dataclass(frozen=True, slots=True)
class TemporalHeuristicCertificate:
    """Proof-carrying objective lower bounds for heuristic ordering.

    ``objective_lower_hours`` is a lower bound on the remaining objective
    cost, not a route answer.  A usable certificate proves both admissibility
    and consistency over the finite graph used by the research adapter.
    """

    scope: TemporalScope
    objective: str
    universe_nodes: tuple[Any, ...]
    reverse_travel_lower_hours: tuple[tuple[Any, float], ...]
    objective_lower_hours: tuple[tuple[Any, float], ...]
    cost_model_digest: str
    proof_digest: str
    admissible: bool = False
    consistent: bool = False
    coverage_complete: bool = False
    reason: str | None = None
    method: str = HEURISTIC_METHOD
    evaluator_digest: str = HEURISTIC_EVALUATOR
    schema_version: str = HEURISTIC_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "universe_nodes", _unique(self.universe_nodes))
        object.__setattr__(
            self,
            "reverse_travel_lower_hours",
            _normalise_bounds(self.reverse_travel_lower_hours),
        )
        object.__setattr__(
            self,
            "objective_lower_hours",
            _normalise_bounds(self.objective_lower_hours),
        )
        if self.schema_version != HEURISTIC_SCHEMA:
            raise ValueError("unsupported temporal heuristic certificate schema")
        if not self.objective or not self.cost_model_digest or not self.proof_digest:
            raise ValueError("heuristic certificate requires stable identity digests")
        if not self.method or not self.evaluator_digest:
            raise ValueError("heuristic certificate requires method/evaluator identity")
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
                "universe_nodes": self.universe_nodes,
                "reverse_travel_lower_hours": self.reverse_travel_lower_hours,
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
    def reverse_map(self) -> Mapping[Any, float]:
        return dict(self.reverse_travel_lower_hours)

    @property
    def objective_map(self) -> Mapping[Any, float]:
        return dict(self.objective_lower_hours)

    @property
    def usable(self) -> bool:
        universe = set(self.universe_nodes)
        objective_keys = set(self.objective_map)
        reverse_keys = set(self.reverse_map)
        scope_objective = self.scope.mapping.get("objective")
        objective_matches = scope_objective is None or str(scope_objective) == self.objective
        return (
            self.admissible
            and self.consistent
            and self.coverage_complete
            and self.reason is None
            and self.scope.evaluator_identity_known
            and bool(universe)
            and reverse_keys == universe
            and objective_keys == universe
            and objective_matches
            and not self.evaluator_digest.startswith(("unknown:", "type:"))
        )

    def permits(self, expected_scope: TemporalScope | Mapping[str, Any]) -> bool:
        return self.usable and self.scope.matches(expected_scope)

    def lower_bound(self, node: Any) -> float | None:
        """Return the certified bound, retaining unknown nodes fail-closed."""

        return self.objective_map.get(node)

    @classmethod
    def from_topological(
        cls,
        *,
        scope: TemporalScope | Mapping[str, Any],
        topology: TopologicalLowerBoundEvidence,
        cost_model: CostModel,
        objective: str,
        expected_scope: TemporalScope | Mapping[str, Any] | None = None,
    ) -> TemporalHeuristicCertificate:
        """Convert a complete graph travel bound into a cost lower bound."""

        active_scope = TemporalScope.from_mapping(scope)
        model_digest = _cost_model_digest(cost_model)
        universe = tuple(topology.universe_nodes)
        reverse = tuple(topology.reverse_lower_hours)
        reason: str | None = None
        admissible = False
        consistent = False
        complete = False
        objective_bounds: tuple[tuple[Any, float], ...] = ()
        if expected_scope is not None and not active_scope.matches(expected_scope):
            reason = "scope_mismatch"
        elif not topology.usable:
            reason = f"topology_not_usable:{topology.reason or 'unknown'}"
        elif not topology.scope.matches(active_scope):
            reason = "topology_scope_mismatch"
        elif not objective:
            reason = "missing_objective"
        elif (
            topology.scope.mapping.get("objective") is not None
            and str(topology.scope.mapping["objective"]) != objective
        ):
            reason = "objective_scope_mismatch"
        else:
            weights = cost_model.weights
            weight_sum = float(weights.travel_time + weights.distance)
            if not isfinite(weight_sum) or weight_sum < 0.0:
                reason = "invalid_cost_weights"
            else:
                objective_bounds = tuple(
                    (
                        node,
                        max(0.0, nextafter(weight_sum * float(hours), float("-inf"))),
                    )
                    for node, hours in reverse
                )
                complete = (
                    set(universe) == set(node for node, _ in reverse)
                    and set(universe) == set(node for node, _ in objective_bounds)
                )
                if not complete:
                    reason = "coverage_incomplete"
                else:
                    admissible = True
                    # Reverse shortest paths over non-negative certified edge
                    # lower bounds satisfy the triangle inequality.  The
                    # actual edge objective is no smaller because all other
                    # cost terms are non-negative.
                    consistent = True

        proof_digest = canonical_digest(
            {
                "schema_version": HEURISTIC_SCHEMA,
                "scope": active_scope.digest,
                "topology_digest": topology.digest,
                "objective": objective,
                "universe_nodes": universe,
                "reverse_travel_lower_hours": reverse,
                "objective_lower_hours": objective_bounds,
                "cost_model_digest": model_digest,
                "method": HEURISTIC_METHOD,
                "evaluator_digest": HEURISTIC_EVALUATOR,
                "admissible": admissible,
                "consistent": consistent,
                "coverage_complete": complete,
                "reason": reason,
            }
        )
        return cls(
            scope=active_scope,
            objective=objective,
            universe_nodes=universe,
            reverse_travel_lower_hours=reverse,
            objective_lower_hours=objective_bounds,
            cost_model_digest=model_digest,
            proof_digest=proof_digest,
            admissible=admissible,
            consistent=consistent,
            coverage_complete=complete,
            reason=reason,
        )


def qualify_temporal_heuristic(
    *,
    scope: TemporalScope | Mapping[str, Any],
    topology: TopologicalLowerBoundEvidence,
    cost_model: CostModel,
    objective: str,
    expected_scope: TemporalScope | Mapping[str, Any] | None = None,
) -> TemporalHeuristicCertificate:
    """Research-only convenience wrapper for certificate construction."""

    return TemporalHeuristicCertificate.from_topological(
        scope=scope,
        topology=topology,
        cost_model=cost_model,
        objective=objective,
        expected_scope=expected_scope,
    )


__all__ = [
    "HEURISTIC_EVALUATOR",
    "HEURISTIC_METHOD",
    "HEURISTIC_SCHEMA",
    "TemporalHeuristicCertificate",
    "qualify_temporal_heuristic",
]
