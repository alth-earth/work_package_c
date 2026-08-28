"""Fail-closed interval evidence for ETA fixed-point qualification.

This module is a C-internal research sidecar.  It does not change the formal
planner's ETA policy and it never turns a sampled bracket into a certificate
without an explicitly supplied interval-extension proof.  Callers must state
whether their evaluator covers the complete domain and whether its interval
enclosure/contraction bound has been independently audited.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from .temporal_qualification import TemporalScope, canonical_digest


class EtaIntervalStatus(StrEnum):
    """Evidence level for one interval ETA operator domain."""

    ROOT_EXISTS_UNIQUE = "ROOT_EXISTS_UNIQUE"
    ROOT_EXISTS_NONUNIQUE = "ROOT_EXISTS_NONUNIQUE"
    ROOT_EXCLUDED = "ROOT_EXCLUDED"
    UNCERTAIN_NO_INTERVAL_PROOF = "UNCERTAIN_NO_INTERVAL_PROOF"
    UNCERTAIN_COVERAGE = "UNCERTAIN_COVERAGE"
    UNCERTAIN_EVALUATOR_FAILURE = "UNCERTAIN_EVALUATOR_FAILURE"
    UNCERTAIN_DISCONTINUITY = "UNCERTAIN_DISCONTINUITY"


@dataclass(frozen=True, slots=True, order=True)
class EtaInterval:
    """Closed positive ETA interval, expressed in travel hours."""

    lower_hours: float
    upper_hours: float

    def __post_init__(self) -> None:
        if not isfinite(self.lower_hours) or not isfinite(self.upper_hours):
            raise ValueError("ETA interval bounds must be finite")
        if self.lower_hours <= 0.0 or self.upper_hours <= 0.0:
            raise ValueError("ETA interval bounds must be positive")
        if self.lower_hours > self.upper_hours:
            raise ValueError("ETA interval lower bound must not exceed upper bound")

    @property
    def width_hours(self) -> float:
        return self.upper_hours - self.lower_hours

    def contains(self, value: float) -> bool:
        return self.lower_hours <= value <= self.upper_hours

    def contains_interval(self, other: EtaInterval) -> bool:
        return self.contains(other.lower_hours) and self.contains(other.upper_hours)

    def disjoint(self, other: EtaInterval) -> bool:
        return self.upper_hours < other.lower_hours or other.upper_hours < self.lower_hours


@dataclass(frozen=True, slots=True)
class EtaIntervalCertificate:
    """Auditable, scope-bound result of interval ETA qualification."""

    status: EtaIntervalStatus
    domain: EtaInterval
    image: EtaInterval | None
    scope: TemporalScope
    tolerance_seconds: float
    coverage_complete: bool
    evaluator_certified: bool
    contraction_bound: float | None = None
    continuity_certified: bool = False
    reason: str | None = None
    schema_version: str = "c.temporal-eta-interval-certificate.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EtaIntervalStatus(self.status))
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        if self.schema_version != "c.temporal-eta-interval-certificate.v1":
            raise ValueError("unsupported ETA interval certificate schema")
        if not isfinite(self.tolerance_seconds) or self.tolerance_seconds < 0.0:
            raise ValueError("ETA interval tolerance must be finite and non-negative")
        if self.contraction_bound is not None and not isfinite(self.contraction_bound):
            raise ValueError("contraction bound must be finite")
        if self.contraction_bound is not None and self.contraction_bound < 0.0:
            raise ValueError("contraction bound must be non-negative")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not isinstance(self.evaluator_certified, bool):
            raise ValueError("evaluator_certified must be boolean")
        if not isinstance(self.continuity_certified, bool):
            raise ValueError("continuity_certified must be boolean")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "domain": self.domain,
                "image": self.image,
                "scope": self.scope.digest,
                "tolerance_seconds": self.tolerance_seconds,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "contraction_bound": self.contraction_bound,
                "continuity_certified": self.continuity_certified,
                "reason": self.reason,
            }
        )

    @property
    def certificate_digest(self) -> str:
        return self.digest

    @property
    def proves_fixed_point(self) -> bool:
        return self.status in {
            EtaIntervalStatus.ROOT_EXISTS_UNIQUE,
            EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE,
        }

    @property
    def usable(self) -> bool:
        """Whether this certificate can authorize an ETA fixed-point claim."""

        return (
            self.proves_fixed_point
            and self.image is not None
            and self.coverage_complete
            and self.evaluator_certified
            and self.reason is None
        )

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.usable and self.scope.matches(expected_scope)


def qualify_eta_interval(
    domain: EtaInterval,
    evaluate_interval: Callable[[EtaInterval], EtaInterval],
    *,
    scope: Mapping[str, Any] | TemporalScope | None = None,
    tolerance_seconds: float = 1.0,
    coverage_complete: bool = False,
    evaluator_certified: bool = False,
    contraction_bound: float | None = None,
    continuity_certified: bool = False,
    endpoint_residuals: tuple[float, float] | None = None,
) -> EtaIntervalCertificate:
    """Qualify a fixed-point domain without silently promoting samples.

    ``evaluate_interval`` must return a conservative enclosure of the ETA
    operator over the complete ``domain``.  Since this function cannot prove
    that a user callback is conservative, ``evaluator_certified`` is an
    explicit input.  Without it, even a visually perfect bracket remains
    ``UNCERTAIN_NO_INTERVAL_PROOF``.

    A contraction bound below one plus ``image ⊆ domain`` proves a unique
    fixed point.  A certified continuous endpoint sign change proves at least
    one root but not uniqueness.  An image disjoint from the domain proves
    exclusion only when the interval evaluator and coverage are certified.
    """

    active_scope = TemporalScope.from_mapping(scope or {"scope": "unbound"})
    if not isfinite(tolerance_seconds) or tolerance_seconds < 0.0:
        raise ValueError("ETA interval tolerance must be finite and non-negative")
    if contraction_bound is not None and (
        not isfinite(contraction_bound) or contraction_bound < 0.0
    ):
        raise ValueError("contraction_bound must be finite and non-negative")
    if endpoint_residuals is not None and any(
        not isfinite(value) for value in endpoint_residuals
    ):
        raise ValueError("endpoint residuals must be finite")

    if not coverage_complete:
        return EtaIntervalCertificate(
            status=EtaIntervalStatus.UNCERTAIN_COVERAGE,
            domain=domain,
            image=None,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=False,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
            reason="interval_domain_coverage_incomplete",
        )

    try:
        image = evaluate_interval(domain)
        if not isinstance(image, EtaInterval):
            raise TypeError("interval evaluator must return EtaInterval")
    except Exception as error:
        return EtaIntervalCertificate(
            status=EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE,
            domain=domain,
            image=None,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
            reason=f"evaluation_failed:{type(error).__name__}",
        )

    if image.disjoint(domain):
        status = (
            EtaIntervalStatus.ROOT_EXCLUDED
            if evaluator_certified
            else EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
        )
        return EtaIntervalCertificate(
            status=status,
            domain=domain,
            image=image,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
            reason=(
                None
                if status is EtaIntervalStatus.ROOT_EXCLUDED
                else "interval_extension_unverified"
            ),
        )

    if (
        evaluator_certified
        and contraction_bound is not None
        and contraction_bound < 1.0
        and domain.contains_interval(image)
    ):
        return EtaIntervalCertificate(
            status=EtaIntervalStatus.ROOT_EXISTS_UNIQUE,
            domain=domain,
            image=image,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
        )

    endpoint_brackets = endpoint_residuals is not None and (
        endpoint_residuals[0] == 0.0
        or endpoint_residuals[1] == 0.0
        or (endpoint_residuals[0] < 0.0) != (endpoint_residuals[1] < 0.0)
    )
    if evaluator_certified and continuity_certified and endpoint_brackets:
        return EtaIntervalCertificate(
            status=EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE,
            domain=domain,
            image=image,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
        )

    status = (
        EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
        if not continuity_certified
        else EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    )
    return EtaIntervalCertificate(
        status=status,
        domain=domain,
        image=image,
        scope=active_scope,
        tolerance_seconds=tolerance_seconds,
        coverage_complete=coverage_complete,
        evaluator_certified=evaluator_certified,
        contraction_bound=contraction_bound,
        continuity_certified=continuity_certified,
        reason="missing_certified_contraction_or_continuity_proof",
    )


__all__ = [
    "EtaInterval",
    "EtaIntervalCertificate",
    "EtaIntervalStatus",
    "qualify_eta_interval",
]
