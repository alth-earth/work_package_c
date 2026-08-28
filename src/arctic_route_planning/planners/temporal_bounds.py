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
from enum import StrEnum
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "allowed_nodes", tuple(self.allowed_nodes))
        object.__setattr__(self, "excluded_nodes", tuple(self.excluded_nodes))
        object.__setattr__(self, "status", TemporalStateBoundStatus(self.status))
        if self.schema_version != "c.temporal-state-bound-certificate.v1":
            raise ValueError("unsupported temporal state-bound certificate schema")
        if not isinstance(self.exclusion_proof, bool):
            raise ValueError("exclusion_proof must be boolean")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not isinstance(self.evaluator_certified, bool):
            raise ValueError("evaluator_certified must be boolean")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.exclusion_proof:
            raise ValueError("certified state bound requires an exclusion proof")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.proof_digest:
            raise ValueError("certified state bound requires a proof digest")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.coverage_complete:
            raise ValueError("certified state bound requires complete coverage")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.evaluator_certified:
            raise ValueError("certified state bound requires a certified evaluator")

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


def qualify_state_bound(
    scope: Mapping[str, Any] | TemporalScope,
    allowed_nodes: Iterable[Any],
    *,
    universe_nodes: Iterable[Any] | None = None,
    exclusion_proof: bool = False,
    proof_digest: str | None = None,
    coverage_complete: bool = False,
    evaluator_certified: bool = False,
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
    else:
        reason = None
    status = TemporalStateBoundStatus.CERTIFIED if reason is None else TemporalStateBoundStatus.REJECTED
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
    )


__all__ = [
    "TemporalStateBoundCertificate",
    "TemporalStateBoundStatus",
    "qualify_state_bound",
]
