"""Proof-carrying finite time-space corridor diagnostics.

The corridor is intentionally a C-internal sidecar.  It derives only
necessary-condition exclusions from independently supplied admissible lower
bounds.  It does not inject an oracle route, change the default planner, or
delete an already expanded label.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from math import isfinite, nextafter
from typing import Any

from .temporal_bounds import (
    TemporalStateBoundCertificate,
    TemporalStateBoundStatus,
    qualify_state_bound,
)
from .temporal_qualification import TemporalScope, canonical_digest


@dataclass(frozen=True, slots=True)
class AdmissibleBoundEvidence:
    """Audited identity for lower bounds used to exclude finite states."""

    scope: TemporalScope
    method: str
    evaluator_digest: str
    proof_digest: str
    admissible: bool
    coverage_complete: bool
    schema_version: str = "c.p0.1-temporal-admissible-bound.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        if not self.method or not self.evaluator_digest or not self.proof_digest:
            raise ValueError("bound evidence requires method and stable digests")
        if not isinstance(self.admissible, bool) or not isinstance(self.coverage_complete, bool):
            raise ValueError("bound evidence flags must be boolean")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "method": self.method,
                "evaluator_digest": self.evaluator_digest,
                "proof_digest": self.proof_digest,
                "admissible": self.admissible,
                "coverage_complete": self.coverage_complete,
            }
        )

    def usable(self, expected_scope: TemporalScope | Mapping[str, Any]) -> bool:
        return (
            self.admissible
            and self.coverage_complete
            and self.scope.matches(expected_scope)
            and self.scope.evaluator_identity_known
            and not self.evaluator_digest.startswith(("unknown:", "type:"))
        )


@dataclass(frozen=True, slots=True)
class TemporalCorridorEvidence:
    """Derived corridor certificate plus retention diagnostics."""

    certificate: TemporalStateBoundCertificate
    start: Any
    goal: Any
    horizon_hours: float
    universe_count: int
    allowed_count: int
    excluded_count: int
    forward_bounds: tuple[tuple[Any, float], ...]
    reverse_bounds: tuple[tuple[Any, float], ...]
    objective: str
    arrival_upper_bounds: tuple[tuple[Any, float], ...] = ()
    projected_label_reduction: float | None = None
    reason: str | None = None
    schema_version: str = "c.p0.1-temporal-corridor-evidence.v1"

    @property
    def proof_digest(self) -> str:
        return self.certificate.proof_digest or ""

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "certificate": self.certificate.digest,
                "start": self.start,
                "goal": self.goal,
                "horizon_hours": self.horizon_hours,
                "universe_count": self.universe_count,
                "allowed_count": self.allowed_count,
                "excluded_count": self.excluded_count,
                "forward_bounds": self.forward_bounds,
                "reverse_bounds": self.reverse_bounds,
                "arrival_upper_bounds": self.arrival_upper_bounds,
                "objective": self.objective,
                "projected_label_reduction": self.projected_label_reduction,
                "reason": self.reason,
            }
        )

    @property
    def usable(self) -> bool:
        return self.certificate.usable and self.excluded_count > 0


def _rejected(
    *,
    scope: TemporalScope,
    start: Any,
    goal: Any,
    horizon_hours: float,
    universe_count: int,
    objective: str,
    reason: str,
    forward_bounds: tuple[tuple[Any, float], ...] = (),
    reverse_bounds: tuple[tuple[Any, float], ...] = (),
    arrival_upper_bounds: tuple[tuple[Any, float], ...] = (),
) -> TemporalCorridorEvidence:
    certificate = TemporalStateBoundCertificate(
        scope=scope,
        allowed_nodes=(),
        excluded_nodes=(),
        status=TemporalStateBoundStatus.REJECTED,
        reason=reason,
        coverage_complete=False,
        evaluator_certified=False,
    )
    return TemporalCorridorEvidence(
        certificate=certificate,
        start=start,
        goal=goal,
        horizon_hours=horizon_hours,
        universe_count=universe_count,
        allowed_count=0,
        excluded_count=0,
        forward_bounds=forward_bounds,
        reverse_bounds=reverse_bounds,
        arrival_upper_bounds=arrival_upper_bounds,
        objective=objective,
        reason=reason,
    )


def derive_temporal_corridor(
    *,
    scope: TemporalScope | Mapping[str, Any],
    expected_scope: TemporalScope | Mapping[str, Any] | None = None,
    universe_nodes: Iterable[Any],
    start: Any,
    goal: Any,
    neighbors: Callable[[Any], Iterable[Any]] | None = None,
    forward_lower_hours: Mapping[Any, float] | Callable[[Any], float],
    reverse_lower_hours: Mapping[Any, float] | Callable[[Any], float],
    horizon_hours: float,
    objective: str,
    bound_evidence: AdmissibleBoundEvidence,
    incumbent_upper_bound: float | None = None,
    objective_lower_bound: Mapping[Any, float] | Callable[[Any], float] | None = None,
    generated_nodes: Iterable[Any] | None = None,
) -> TemporalCorridorEvidence:
    """Derive a necessary-condition corridor and a fail-closed certificate.

    A node is excluded only when the outward-rounded sum of admissible
    forward/reverse lower bounds exceeds the finite time horizon.  If an
    objective incumbent is supplied, an objective lower bound may additionally
    exclude a node only when its outward-rounded lower bound is strictly above
    that incumbent.  Missing or untrusted evidence yields a rejected
    certificate with an empty allowed set.
    """

    active_scope = TemporalScope.from_mapping(scope)
    universe = tuple(dict.fromkeys(universe_nodes))
    if not objective or not isfinite(horizon_hours) or horizon_hours <= 0.0:
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="invalid_corridor_domain",
        )
    if expected_scope is not None and not active_scope.matches(expected_scope):
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="scope_mismatch",
        )
    if not bound_evidence.usable(active_scope):
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="bound_evidence_not_admissible_or_complete",
        )
    if start not in universe or goal not in universe:
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="start_or_goal_outside_universe",
        )

    def read_bound(value: Mapping[Any, float] | Callable[[Any], float], node: Any) -> float:
        result = value[node] if isinstance(value, Mapping) else value(node)
        if not isfinite(result) or result < 0.0:
            raise ValueError("admissible lower bounds must be finite and non-negative")
        return float(result)

    forward: list[tuple[Any, float]] = []
    reverse: list[tuple[Any, float]] = []
    allowed: list[Any] = []
    try:
        for node in universe:
            forward_value = read_bound(forward_lower_hours, node)
            reverse_value = read_bound(reverse_lower_hours, node)
            forward.append((node, forward_value))
            reverse.append((node, reverse_value))
            # These are certified lower bounds.  Round the arithmetic
            # downward before testing strict exclusion so an exact boundary
            # cannot be lost to floating-point addition.
            total_lower = nextafter(forward_value + reverse_value, float("-inf"))
            feasible = total_lower <= horizon_hours
            if feasible and incumbent_upper_bound is not None and objective_lower_bound is not None:
                if not isfinite(incumbent_upper_bound):
                    raise ValueError("incumbent upper bound must be finite")
                objective_value = read_bound(objective_lower_bound, node)
                objective_lower = nextafter(objective_value, float("-inf"))
                feasible = objective_lower <= incumbent_upper_bound
            if feasible:
                allowed.append(node)
    except (KeyError, TypeError, ValueError) as error:
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason=f"invalid_bound_evidence:{type(error).__name__}",
            forward_bounds=tuple(forward),
            reverse_bounds=tuple(reverse),
        )

    excluded = tuple(node for node in universe if node not in set(allowed))
    if start not in allowed or goal not in allowed:
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="bound_excludes_start_or_goal",
            forward_bounds=tuple(forward),
            reverse_bounds=tuple(reverse),
        )

    reverse_by_node = dict(reverse)
    arrival_upper: list[tuple[Any, float]] = []
    try:
        for node in allowed:
            reverse_value = reverse_by_node[node]
            # The upper bound is deliberately rounded outward.  A label is
            # only rejected when even this generous bound is exceeded.
            upper_hours = nextafter(horizon_hours - reverse_value, float("inf"))
            if not isfinite(upper_hours) or upper_hours < 0.0:
                raise ValueError("invalid arrival upper bound")
            arrival_upper.append((node, upper_hours))
    except (KeyError, ValueError):
        return _rejected(
            scope=active_scope,
            start=start,
            goal=goal,
            horizon_hours=horizon_hours,
            universe_count=len(universe),
            objective=objective,
            reason="invalid_arrival_upper_bound",
            forward_bounds=tuple(forward),
            reverse_bounds=tuple(reverse),
        )
    proof_digest = canonical_digest(
        {
            "schema_version": "c.p0.1-temporal-corridor-proof.v1",
            "scope": active_scope.digest,
            "bound_evidence": bound_evidence.digest,
            "universe": universe,
            "allowed": tuple(allowed),
            "excluded": excluded,
            "forward": tuple(forward),
            "reverse": tuple(reverse),
            "arrival_upper": tuple(arrival_upper),
            "horizon_hours": horizon_hours,
            "objective": objective,
            "incumbent_upper_bound": incumbent_upper_bound,
        }
    )
    certificate = qualify_state_bound(
        active_scope,
        allowed,
        universe_nodes=universe,
        exclusion_proof=True,
        proof_digest=proof_digest,
        coverage_complete=True,
        evaluator_certified=True,
        arrival_upper_hours=tuple(arrival_upper),
    )
    projected = None
    if generated_nodes is not None:
        generated = tuple(generated_nodes)
        if generated:
            projected = sum(node in set(excluded) for node in generated) / len(generated)
    return TemporalCorridorEvidence(
        certificate=certificate,
        start=start,
        goal=goal,
        horizon_hours=horizon_hours,
        universe_count=len(universe),
        allowed_count=len(allowed),
        excluded_count=len(excluded),
        forward_bounds=tuple(forward),
        reverse_bounds=tuple(reverse),
        arrival_upper_bounds=tuple(arrival_upper),
        objective=objective,
        projected_label_reduction=projected,
        reason=None if certificate.usable else certificate.reason,
    )


__all__ = [
    "AdmissibleBoundEvidence",
    "TemporalCorridorEvidence",
    "derive_temporal_corridor",
]
