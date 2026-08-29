"""Scope-bound exact-search state bounds for C-internal experiments.

A geographic corridor is not automatically safe: excluding a node can remove
the only lower-cost route.  This module therefore represents a bound only as a
certificate supplied with an explicit exclusion proof and a complete planner
scope.  The default policy is disabled and no public planner contract imports
this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite, nextafter
from typing import Any

from .temporal_qualification import TemporalScope, canonical_digest


class TemporalStateBoundStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    DISABLED = "DISABLED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class TemporalStateBoundCertificate:
    """Proof-carrying allowed-node bound for newly generated labels."""

    scope: TemporalScope
    allowed_nodes: tuple[Any, ...]
    exclusion_proof: bool = False
    proof_digest: str | None = None
    status: TemporalStateBoundStatus = TemporalStateBoundStatus.REJECTED
    reason: str | None = None
    excluded_nodes: tuple[Any, ...] = ()
    coverage_complete: bool = False
    evaluator_certified: bool = False
    schema_version: str = "c.temporal-state-bound-certificate.v1"
    # Optional label-level envelope.  Values are conservative upper bounds on
    # elapsed hours at which a newly generated label may still reach the goal.
    # An empty tuple preserves the historical node-only bound semantics.
    arrival_upper_hours: tuple[tuple[Any, float], ...] = ()
    # Optional transition-level envelope.  Values are conservative lower
    # travel times for directed edges.  The map is used only when the caller
    # explicitly certifies complete edge coverage; missing edges remain live.
    edge_lower_hours: tuple[tuple[Any, Any, float], ...] = ()
    edge_bound_complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "allowed_nodes", tuple(self.allowed_nodes))
        object.__setattr__(self, "excluded_nodes", tuple(self.excluded_nodes))
        object.__setattr__(self, "status", TemporalStateBoundStatus(self.status))
        raw_arrival_bounds = self.arrival_upper_hours
        if isinstance(raw_arrival_bounds, Mapping):
            raw_arrival_bounds = tuple(raw_arrival_bounds.items())
        else:
            raw_arrival_bounds = tuple(raw_arrival_bounds)
        normalized_arrival_bounds: list[tuple[Any, float]] = []
        seen_arrival_nodes: set[Any] = set()
        for item in raw_arrival_bounds:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("arrival upper bounds must be node/value pairs")
            node, upper_hours = item
            try:
                duplicate = node in seen_arrival_nodes
                seen_arrival_nodes.add(node)
            except TypeError as error:
                raise ValueError("arrival upper-bound nodes must be hashable") from error
            if duplicate:
                raise ValueError("arrival upper bounds must not contain duplicate nodes")
            if not isfinite(float(upper_hours)) or float(upper_hours) < 0.0:
                raise ValueError("arrival upper bounds must be finite and non-negative")
            normalized_arrival_bounds.append((node, float(upper_hours)))
        if any(node not in self.allowed_nodes for node, _ in normalized_arrival_bounds):
            raise ValueError("arrival upper-bound nodes must be allowed nodes")
        object.__setattr__(self, "arrival_upper_hours", tuple(normalized_arrival_bounds))
        raw_edge_bounds = self.edge_lower_hours
        if isinstance(raw_edge_bounds, Mapping):
            raw_edge_bounds = tuple(
                (*key, value) for key, value in raw_edge_bounds.items()
            )
        else:
            raw_edge_bounds = tuple(raw_edge_bounds)
        normalized_edge_bounds: list[tuple[Any, Any, float]] = []
        seen_edges: set[tuple[Any, Any]] = set()
        for item in raw_edge_bounds:
            if not isinstance(item, (tuple, list)) or len(item) != 3:
                raise ValueError("edge lower bounds must be start/end/value triples")
            start, end, lower_hours = item
            try:
                edge = (start, end)
                duplicate = edge in seen_edges
                seen_edges.add(edge)
            except TypeError as error:
                raise ValueError("edge lower-bound endpoints must be hashable") from error
            if duplicate:
                raise ValueError("edge lower bounds must not contain duplicate edges")
            if not isfinite(float(lower_hours)) or float(lower_hours) < 0.0:
                raise ValueError("edge lower bounds must be finite and non-negative")
            normalized_edge_bounds.append((start, end, float(lower_hours)))
        object.__setattr__(self, "edge_lower_hours", tuple(normalized_edge_bounds))
        if self.schema_version != "c.temporal-state-bound-certificate.v1":
            raise ValueError("unsupported temporal state-bound certificate schema")
        if not isinstance(self.exclusion_proof, bool):
            raise ValueError("exclusion_proof must be boolean")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not isinstance(self.evaluator_certified, bool):
            raise ValueError("evaluator_certified must be boolean")
        if not isinstance(self.edge_bound_complete, bool):
            raise ValueError("edge_bound_complete must be boolean")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.exclusion_proof:
            raise ValueError("certified state bound requires an exclusion proof")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.proof_digest:
            raise ValueError("certified state bound requires a proof digest")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.coverage_complete:
            raise ValueError("certified state bound requires complete coverage")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.evaluator_certified:
            raise ValueError("certified state bound requires a certified evaluator")
        if (
            self.status is TemporalStateBoundStatus.CERTIFIED
            and self.edge_bound_complete
            and not self.edge_lower_hours
        ):
            raise ValueError("complete edge bound requires edge lower bounds")

    @classmethod
    def disabled(
        cls,
        scope: Mapping[str, Any] | TemporalScope | None = None,
    ) -> TemporalStateBoundCertificate:
        return cls(
            scope=TemporalScope.from_mapping(scope or {"scope": "unbound"}),
            allowed_nodes=(),
            status=TemporalStateBoundStatus.DISABLED,
        )

    @classmethod
    def certified(
        cls,
        scope: Mapping[str, Any] | TemporalScope,
        allowed_nodes: Iterable[Any],
        *,
        proof_digest: str,
        excluded_nodes: Iterable[Any] = (),
        coverage_complete: bool = True,
        evaluator_certified: bool = True,
        arrival_upper_hours: Mapping[Any, float] | Iterable[tuple[Any, float]] = (),
        edge_lower_hours: Mapping[Any, float] | Iterable[tuple[Any, Any, float]] = (),
        edge_bound_complete: bool = False,
    ) -> TemporalStateBoundCertificate:
        if not isinstance(proof_digest, str) or not proof_digest.strip():
            raise ValueError("proof_digest must be a non-empty stable string")
        return cls(
            scope=TemporalScope.from_mapping(scope),
            allowed_nodes=tuple(allowed_nodes),
            excluded_nodes=tuple(excluded_nodes),
            exclusion_proof=True,
            proof_digest=proof_digest,
            status=TemporalStateBoundStatus.CERTIFIED,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            arrival_upper_hours=arrival_upper_hours,
            edge_lower_hours=edge_lower_hours,
            edge_bound_complete=edge_bound_complete,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "allowed_nodes": self.allowed_nodes,
                "excluded_nodes": self.excluded_nodes,
                "exclusion_proof": self.exclusion_proof,
                "proof_digest": self.proof_digest,
                "status": self.status,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "reason": self.reason,
                "arrival_upper_hours": self.arrival_upper_hours,
                "edge_lower_hours": self.edge_lower_hours,
                "edge_bound_complete": self.edge_bound_complete,
            }
        )

    @property
    def usable(self) -> bool:
        return (
            self.status is TemporalStateBoundStatus.CERTIFIED
            and self.exclusion_proof
            and bool(self.proof_digest)
            and self.coverage_complete
            and self.evaluator_certified
            and self.scope.evaluator_identity_known
            and self.reason is None
        )

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.usable and self.scope.matches(expected_scope)

    def allows(self, node: Any) -> bool:
        return node in self.allowed_nodes

    @property
    def arrival_bound_complete(self) -> bool:
        """Whether an envelope is present for every allowed node."""

        if not self.arrival_upper_hours:
            return False
        return {node for node, _ in self.arrival_upper_hours} == set(self.allowed_nodes)

    @property
    def edge_bound_digest(self) -> str:
        """Return a stable digest for the optional transition envelope."""

        return canonical_digest(
            {
                "edge_lower_hours": self.edge_lower_hours,
                "edge_bound_complete": self.edge_bound_complete,
            }
        )

    def allows_state(
        self,
        node: Any,
        arrival_time: datetime,
        departure_time: datetime,
    ) -> bool:
        """Check a node and its optional conservative arrival envelope.

        Missing envelope entries, malformed timestamps, and non-finite elapsed
        times are retained rather than pruned.  A caller must separately
        require :attr:`arrival_bound_complete` when it needs the stronger
        label-level proof.
        """

        if not self.allows(node) or not self.arrival_bound_complete:
            return self.allows(node)
        upper_by_node = dict(self.arrival_upper_hours)
        upper_hours = upper_by_node.get(node)
        if upper_hours is None:
            return True
        if (
            arrival_time.tzinfo is None
            or arrival_time.utcoffset() is None
            or departure_time.tzinfo is None
            or departure_time.utcoffset() is None
        ):
            return True
        elapsed_hours = (
            arrival_time.astimezone(UTC) - departure_time.astimezone(UTC)
        ).total_seconds() / 3600.0
        if not isfinite(elapsed_hours) or elapsed_hours < 0.0:
            return True
        # Round the observed elapsed value downward so a mathematically equal
        # boundary cannot be rejected because of binary floating-point noise.
        return nextafter(elapsed_hours, float("-inf")) <= upper_hours

    def allows_transition(
        self,
        start_node: Any,
        end_node: Any,
        arrival_time: datetime,
        departure_time: datetime,
    ) -> bool:
        """Return whether a transition can still reach its node envelope.

        This is deliberately conservative: without a complete edge envelope,
        a matching edge, a complete arrival envelope, or valid UTC timestamps,
        the transition is retained.  A transition is rejected only when the
        certified lower travel time makes the destination's upper arrival
        bound impossible even after outward rounding.
        """

        if not self.edge_bound_complete or not self.arrival_bound_complete:
            return True
        edge_lower_by_pair = {
            (start, end): lower for start, end, lower in self.edge_lower_hours
        }
        lower_hours = edge_lower_by_pair.get((start_node, end_node))
        upper_hours = dict(self.arrival_upper_hours).get(end_node)
        if lower_hours is None or upper_hours is None:
            return True
        if (
            arrival_time.tzinfo is None
            or arrival_time.utcoffset() is None
            or departure_time.tzinfo is None
            or departure_time.utcoffset() is None
        ):
            return True
        elapsed_hours = (
            arrival_time.astimezone(UTC) - departure_time.astimezone(UTC)
        ).total_seconds() / 3600.0
        if not isfinite(elapsed_hours) or elapsed_hours < 0.0:
            return True
        lower_arrival_hours = nextafter(elapsed_hours + lower_hours, float("-inf"))
        return lower_arrival_hours <= upper_hours


def qualify_state_bound(
    scope: Mapping[str, Any] | TemporalScope,
    allowed_nodes: Iterable[Any],
    *,
    universe_nodes: Iterable[Any] | None = None,
    exclusion_proof: bool = False,
    proof_digest: str | None = None,
    coverage_complete: bool = False,
    evaluator_certified: bool = False,
    arrival_upper_hours: Mapping[Any, float] | Iterable[tuple[Any, float]] = (),
    edge_lower_hours: Mapping[Any, float] | Iterable[tuple[Any, Any, float]] = (),
    edge_bound_complete: bool = False,
) -> TemporalStateBoundCertificate:
    """Construct a state-bound certificate without accepting partial proofs.

    ``universe_nodes`` is optional for compatibility with a caller that has a
    separately audited region representation.  When supplied, the helper
    derives the excluded set and rejects any allowed node outside the finite
    universe.  A certificate is emitted as ``CERTIFIED`` only when the proof,
    evaluator and complete-domain flags are all true; otherwise it is a
    rejected, non-pruning record carrying a stable reason.
    """

    active_scope = TemporalScope.from_mapping(scope)
    allowed = tuple(dict.fromkeys(allowed_nodes))
    universe = None if universe_nodes is None else tuple(dict.fromkeys(universe_nodes))
    if universe is not None:
        universe_set = set(universe)
        if any(node not in universe_set for node in allowed):
            return TemporalStateBoundCertificate(
                scope=active_scope,
                allowed_nodes=allowed,
                excluded_nodes=(),
                status=TemporalStateBoundStatus.REJECTED,
                reason="allowed_node_outside_universe",
                coverage_complete=False,
                evaluator_certified=evaluator_certified,
                arrival_upper_hours=arrival_upper_hours,
                edge_lower_hours=edge_lower_hours,
                edge_bound_complete=False,
            )
        excluded = tuple(node for node in universe if node not in set(allowed))
    else:
        excluded = ()
    if not exclusion_proof:
        reason = "missing_exclusion_proof"
    elif not coverage_complete:
        reason = "coverage_incomplete"
    elif not evaluator_certified or not active_scope.evaluator_identity_known:
        reason = "unknown_evaluator"
    elif not proof_digest:
        reason = "missing_proof_digest"
    elif edge_bound_complete and not edge_lower_hours:
        reason = "edge_bound_coverage_incomplete"
    else:
        reason = None
    status = (
        TemporalStateBoundStatus.CERTIFIED
        if reason is None
        else TemporalStateBoundStatus.REJECTED
    )
    return TemporalStateBoundCertificate(
        scope=active_scope,
        allowed_nodes=allowed,
        excluded_nodes=excluded,
        exclusion_proof=reason is None,
        proof_digest=proof_digest,
        status=status,
        reason=reason,
        coverage_complete=coverage_complete,
        evaluator_certified=evaluator_certified,
        arrival_upper_hours=arrival_upper_hours,
        edge_lower_hours=edge_lower_hours,
        edge_bound_complete=edge_bound_complete and reason is None,
    )


__all__ = [
    "TemporalStateBoundCertificate",
    "TemporalStateBoundStatus",
    "qualify_state_bound",
]
