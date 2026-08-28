"""Fail-closed interval evidence for ETA fixed-point qualification.

This module is a C-internal research sidecar.  It does not change the formal
planner's ETA policy and it never turns a sampled bracket into a certificate
without an explicitly supplied interval-extension proof.  Callers must state
whether their evaluator covers the complete domain and whether its interval
enclosure/contraction bound has been independently audited.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
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
class SignedInterval:
    """Closed finite interval used for the residual ``g(t) = F(t) - t``.

    ETA travel-time intervals are strictly positive, but a fixed-point
    residual is allowed to cross zero.  Keeping that distinction explicit
    prevents callers from accidentally treating a sampled sign change as an
    interval proof.
    """

    lower: float
    upper: float

    def __post_init__(self) -> None:
        if not isfinite(self.lower) or not isfinite(self.upper):
            raise ValueError("signed interval bounds must be finite")
        if self.lower > self.upper:
            raise ValueError("signed interval lower bound must not exceed upper bound")

    @property
    def contains_zero(self) -> bool:
        return self.lower <= 0.0 <= self.upper

    def disjoint_zero(self) -> bool:
        return self.upper < 0.0 or self.lower > 0.0


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
    policy_digest: str | None = None
    partition_digest: str | None = None
    boundary_evidence: tuple[str, ...] = ()
    endpoint_residuals: tuple[float, float] | None = None

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
        if self.policy_digest is not None and not self.policy_digest:
            raise ValueError("policy_digest must be non-empty when supplied")
        if self.partition_digest is not None and not self.partition_digest:
            raise ValueError("partition_digest must be non-empty when supplied")
        object.__setattr__(self, "boundary_evidence", tuple(self.boundary_evidence))
        if self.endpoint_residuals is not None and (
            len(self.endpoint_residuals) != 2
            or any(not isfinite(value) for value in self.endpoint_residuals)
        ):
            raise ValueError("endpoint residuals must contain two finite values")

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
                "policy_digest": self.policy_digest,
                "partition_digest": self.partition_digest,
                "boundary_evidence": self.boundary_evidence,
                "endpoint_residuals": self.endpoint_residuals,
            }
        )

    @property
    def certificate_digest(self) -> str:
        return self.digest

    @property
    def residual_interval(self) -> SignedInterval | None:
        """Conservative residual enclosure for ``g(t)=F(t)-t``.

        The interval arithmetic uses the whole domain, not a midpoint or
        endpoint sample: ``F(domain) - domain`` is enclosed by
        ``[F_lo-domain_hi, F_hi-domain_lo]``.  A missing image means that the
        evaluator did not produce evidence and therefore has no residual
        interval to inspect.
        """

        if self.image is None:
            return None
        return SignedInterval(
            self.image.lower_hours - self.domain.upper_hours,
            self.image.upper_hours - self.domain.lower_hours,
        )

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

    @property
    def authorization_usable(self) -> bool:
        """Whether this certificate is strong enough for temporal dominance.

        A continuous endpoint sign change establishes existence only.  It does
        not establish a unique arrival operator, so the dominance sidecar
        accepts only the contraction-backed unique-root status.  ``usable``
        remains the broader historical fixed-point evidence predicate for
        compatibility with the finite qualification runner.
        """

        return self.usable and self.status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE

    @property
    def permits_dominance(self) -> bool:
        return self.authorization_usable

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.authorization_usable and self.scope.matches(expected_scope)


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
    policy_digest: str | None = None,
    partition_digest: str | None = None,
    boundary_evidence: Iterable[str] = (),
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
    evidence = tuple(str(item) for item in boundary_evidence)
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
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            boundary_evidence=evidence,
            endpoint_residuals=endpoint_residuals,
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
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            boundary_evidence=evidence,
            endpoint_residuals=endpoint_residuals,
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
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            boundary_evidence=evidence,
            endpoint_residuals=endpoint_residuals,
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
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            boundary_evidence=evidence,
            endpoint_residuals=endpoint_residuals,
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
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            boundary_evidence=evidence,
            endpoint_residuals=endpoint_residuals,
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
        policy_digest=policy_digest,
        partition_digest=partition_digest,
        boundary_evidence=evidence,
        endpoint_residuals=endpoint_residuals,
    )


@dataclass(frozen=True, slots=True)
class EtaIntervalQualification:
    """Aggregate proof envelope for a domain partition.

    Risk-frame boundaries, hard-mask changes, and evaluator-domain changes
    must be represented as explicit segment boundaries.  The aggregate is
    usable only when every segment is independently certified *and* the
    boundaries themselves have a continuity proof.  This makes a finite
    point scan, a hidden coverage gap, or a hard-mask discontinuity unable to
    authorize a global fixed-point claim.
    """

    status: EtaIntervalStatus
    domain: EtaInterval
    segments: tuple[EtaInterval, ...]
    certificates: tuple[EtaIntervalCertificate, ...]
    scope: TemporalScope
    boundary_digest: str
    boundary_continuity_certified: bool = False
    reason: str | None = None
    schema_version: str = "c.temporal-eta-qualification.v1"
    policy_digest: str | None = None
    boundary_evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", EtaIntervalStatus(self.status))
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "certificates", tuple(self.certificates))
        if self.schema_version != "c.temporal-eta-qualification.v1":
            raise ValueError("unsupported ETA interval qualification schema")
        if len(self.segments) != len(self.certificates) or not self.segments:
            raise ValueError("ETA qualification must have one certificate per segment")
        if not isinstance(self.boundary_continuity_certified, bool):
            raise ValueError("boundary continuity flag must be boolean")
        previous = self.domain.lower_hours
        for index, segment in enumerate(self.segments):
            if segment.lower_hours != previous:
                raise ValueError("ETA qualification segments must cover the domain contiguously")
            if segment.upper_hours > self.domain.upper_hours:
                raise ValueError("ETA qualification segment exceeds the domain")
            if index and segment.lower_hours == segment.upper_hours:
                raise ValueError("ETA qualification contains a zero-width interior segment")
            previous = segment.upper_hours
        if previous != self.domain.upper_hours:
            raise ValueError("ETA qualification segments do not cover the complete domain")
        if not isinstance(self.boundary_digest, str) or not self.boundary_digest:
            raise ValueError("boundary digest must be non-empty")
        if self.policy_digest is not None and not self.policy_digest:
            raise ValueError("policy_digest must be non-empty when supplied")
        object.__setattr__(self, "boundary_evidence", tuple(self.boundary_evidence))

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "domain": self.domain,
                "segments": self.segments,
                "certificates": tuple(certificate.digest for certificate in self.certificates),
                "scope": self.scope.digest,
                "boundary_digest": self.boundary_digest,
                "boundary_continuity_certified": self.boundary_continuity_certified,
                "reason": self.reason,
                "policy_digest": self.policy_digest,
                "boundary_evidence": self.boundary_evidence,
            }
        )

    @property
    def usable(self) -> bool:
        return (
            self.status
            in {EtaIntervalStatus.ROOT_EXISTS_UNIQUE, EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE}
            and self.boundary_continuity_certified
            and self.reason is None
            and self.scope.evaluator_identity_known
            and all(certificate.usable for certificate in self.certificates)
            and all(certificate.scope.matches(self.scope) for certificate in self.certificates)
        )

    @property
    def authorization_usable(self) -> bool:
        """Strict dominance authorization: every segment must be unique."""

        return self.usable and all(
            certificate.authorization_usable for certificate in self.certificates
        )

    @property
    def permits_dominance(self) -> bool:
        return self.authorization_usable

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.authorization_usable and self.scope.matches(expected_scope)


def partition_eta_domain(
    domain: EtaInterval,
    boundaries: Iterable[float] = (),
) -> tuple[EtaInterval, ...]:
    """Split an ETA domain at explicit RiskFrame/mask/evaluator boundaries."""

    points = {domain.lower_hours, domain.upper_hours}
    for boundary in boundaries:
        if not isfinite(boundary):
            raise ValueError("ETA partition boundaries must be finite")
        if domain.lower_hours < boundary < domain.upper_hours:
            points.add(float(boundary))
        elif boundary not in {domain.lower_hours, domain.upper_hours}:
            raise ValueError("ETA partition boundary lies outside the domain")
    ordered = sorted(points)
    return tuple(EtaInterval(start, end) for start, end in pairwise(ordered))


def qualify_eta_partition(
    domain: EtaInterval,
    boundaries: Iterable[float],
    evaluate_interval: Callable[[EtaInterval], EtaInterval],
    *,
    scope: Mapping[str, Any] | TemporalScope | None = None,
    tolerance_seconds: float = 1.0,
    coverage_complete: bool = False,
    evaluator_certified: bool = False,
    contraction_bound: float | None = None,
    continuity_certified: bool = False,
    boundary_continuity_certified: bool = False,
    endpoint_residuals: Mapping[int, tuple[float, float]] | None = None,
    boundary_reasons: Iterable[str] = (),
    policy_digest: str | None = None,
) -> EtaIntervalQualification:
    """Qualify every segment and aggregate only independently proven claims.

    ``boundary_reasons`` is intentionally diagnostic.  Supplying a reason
    (for example ``hard_mask_discontinuity``) forces the aggregate to remain
    uncertain even when individual segment callbacks happen to contract.
    ``endpoint_residuals`` is keyed by segment index so a continuous endpoint
    sign change can be audited without confusing it with a midpoint sample.
    """

    active_scope = TemporalScope.from_mapping(scope or {"scope": "unbound"})
    boundary_values = tuple(boundaries)
    segments = partition_eta_domain(domain, boundary_values)
    reasons = tuple(str(reason) for reason in boundary_reasons)
    certificates = tuple(
        qualify_eta_interval(
            segment,
            evaluate_interval,
            scope=active_scope,
            tolerance_seconds=tolerance_seconds,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            contraction_bound=contraction_bound,
            continuity_certified=continuity_certified,
            endpoint_residuals=(endpoint_residuals or {}).get(index),
            policy_digest=policy_digest,
            partition_digest=canonical_digest(
                {"domain": segment, "boundaries": boundary_values}
            ),
            boundary_evidence=reasons,
        )
        for index, segment in enumerate(segments)
    )
    status: EtaIntervalStatus
    if reasons:
        status = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    elif any(
        certificate.status is EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
        for certificate in certificates
    ):
        status = EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
    elif any(
        certificate.status is EtaIntervalStatus.UNCERTAIN_COVERAGE
        for certificate in certificates
    ):
        status = EtaIntervalStatus.UNCERTAIN_COVERAGE
    elif any(
        certificate.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
        for certificate in certificates
    ):
        status = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    elif any(
        certificate.status is EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
        for certificate in certificates
    ):
        status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    elif all(
        certificate.status is EtaIntervalStatus.ROOT_EXCLUDED
        for certificate in certificates
    ):
        status = EtaIntervalStatus.ROOT_EXCLUDED
    elif any(
        certificate.status is EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
        for certificate in certificates
    ):
        status = EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
    else:
        status = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    reason = reasons[0] if reasons else None
    if reason is None and not boundary_continuity_certified and len(segments) > 1:
        reason = "partition_boundary_continuity_unproven"
        if status in {
            EtaIntervalStatus.ROOT_EXISTS_UNIQUE,
            EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE,
        }:
            status = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    return EtaIntervalQualification(
        status=status,
        domain=domain,
        segments=segments,
        certificates=certificates,
        scope=active_scope,
        boundary_digest=canonical_digest(
            {"domain": domain, "boundaries": boundary_values, "reasons": reasons}
        ),
        boundary_continuity_certified=boundary_continuity_certified,
        reason=reason,
        policy_digest=policy_digest,
        boundary_evidence=reasons,
    )


__all__ = [
    "EtaInterval",
    "EtaIntervalCertificate",
    "EtaIntervalQualification",
    "EtaIntervalStatus",
    "SignedInterval",
    "partition_eta_domain",
    "qualify_eta_interval",
    "qualify_eta_partition",
]
