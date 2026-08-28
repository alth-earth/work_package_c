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
    schema_version: str = "c.temporal-state-bound-certificate.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "allowed_nodes", tuple(self.allowed_nodes))
        object.__setattr__(self, "status", TemporalStateBoundStatus(self.status))
        if self.schema_version != "c.temporal-state-bound-certificate.v1":
            raise ValueError("unsupported temporal state-bound certificate schema")
        if not isinstance(self.exclusion_proof, bool):
            raise ValueError("exclusion_proof must be boolean")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.exclusion_proof:
            raise ValueError("certified state bound requires an exclusion proof")
        if self.status is TemporalStateBoundStatus.CERTIFIED and not self.proof_digest:
            raise ValueError("certified state bound requires a proof digest")

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
    ) -> TemporalStateBoundCertificate:
        if not isinstance(proof_digest, str) or not proof_digest.strip():
            raise ValueError("proof_digest must be a non-empty stable string")
        return cls(
            scope=TemporalScope.from_mapping(scope),
            allowed_nodes=tuple(allowed_nodes),
            exclusion_proof=True,
            proof_digest=proof_digest,
            status=TemporalStateBoundStatus.CERTIFIED,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "allowed_nodes": self.allowed_nodes,
                "exclusion_proof": self.exclusion_proof,
                "proof_digest": self.proof_digest,
                "status": self.status,
                "reason": self.reason,
            }
        )

    @property
    def usable(self) -> bool:
        return (
            self.status is TemporalStateBoundStatus.CERTIFIED
            and self.exclusion_proof
            and bool(self.proof_digest)
            and self.scope.evaluator_identity_known
            and self.reason is None
        )

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.usable and self.scope.matches(expected_scope)

    def allows(self, node: Any) -> bool:
        return node in self.allowed_nodes


__all__ = [
    "TemporalStateBoundCertificate",
    "TemporalStateBoundStatus",
]
