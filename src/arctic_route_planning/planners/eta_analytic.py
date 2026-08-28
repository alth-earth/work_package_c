"""Mechanically derived ETA-root and FIFO evidence for C research.

This module is a research-only proof sidecar.  It never changes the formal
planner's ETA policy and it does not accept a caller supplied ``certified``
boolean as a proof.  The caller supplies interval arithmetic produced by the
sidecar evaluator; this module derives the contraction and implicit-arrival
slope bounds and applies the fail-closed authorization rules.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Any

from arctic_route_planning.cost.vessel import KNOT_TO_KM_PER_HOUR, VesselPerformanceModel

from .eta_interval import EtaInterval, EtaIntervalStatus
from .temporal_qualification import FifoStatus, TemporalScope, canonical_digest


class NavigabilityStatus(StrEnum):
    """Interval classification of the edge's navigation predicate."""

    ALWAYS_NAVIGABLE = "ALWAYS_NAVIGABLE"
    ALWAYS_BLOCKED = "ALWAYS_BLOCKED"
    TRANSITION_OR_UNKNOWN = "TRANSITION_OR_UNKNOWN"


@dataclass(frozen=True, slots=True, order=True)
class SlopeInterval:
    """Closed finite interval for a derivative or Lipschitz slope."""

    lower: float
    upper: float

    def __post_init__(self) -> None:
        if not isfinite(self.lower) or not isfinite(self.upper):
            raise ValueError("slope interval bounds must be finite")
        if self.lower > self.upper:
            raise ValueError("slope interval lower bound must not exceed upper bound")

    @property
    def absolute_upper(self) -> float:
        return max(abs(self.lower), abs(self.upper))


def derive_operator_sensitivity(
    *,
    edge_distance_km: float,
    vessel_model: VesselPerformanceModel,
    speed_factor_slope: SlopeInterval,
    fraction: float = 1.0,
) -> tuple[SlopeInterval, SlopeInterval, float]:
    """Derive conservative ``Phi_departure``/``Phi_travel`` slopes.

    The edge travel operator is ``distance / speed(factor)``.  For the
    monotone vessel model, the speed Lipschitz bound is bounded by the
    economic speed times the factor slope.  Dividing by the squared positive
    lower speed yields a conservative reciprocal-speed slope.  ``fraction``
    is the exact-arrival fraction at the sampled edge point and is clipped
    only after validating its finite domain.
    """

    if not isfinite(edge_distance_km) or edge_distance_km <= 0.0:
        raise ValueError("edge_distance_km must be finite and positive")
    if not isfinite(fraction) or not 0.0 <= fraction <= 1.0:
        raise ValueError("edge sample fraction must be within [0, 1]")
    factor_slope = speed_factor_slope.absolute_upper
    minimum_speed_kmh = (
        vessel_model.minimum_steerage_speed_knots * KNOT_TO_KM_PER_HOUR
    )
    economic_speed_kmh = vessel_model.economic_speed_knots * KNOT_TO_KM_PER_HOUR
    if minimum_speed_kmh <= 0.0 or not isfinite(minimum_speed_kmh):
        raise ValueError("vessel minimum speed must be finite and positive")
    reciprocal_speed_slope = (
        edge_distance_km * economic_speed_kmh * factor_slope / minimum_speed_kmh**2
    )
    if not isfinite(reciprocal_speed_slope):
        raise ValueError("derived ETA slope is not finite")
    departure = SlopeInterval(-reciprocal_speed_slope, reciprocal_speed_slope)
    travel = SlopeInterval(
        -reciprocal_speed_slope * fraction,
        reciprocal_speed_slope * fraction,
    )
    return departure, travel, travel.absolute_upper


def _interval_divide(numerator: SlopeInterval, denominator: SlopeInterval) -> SlopeInterval:
    if denominator.lower <= 0.0 <= denominator.upper:
        raise ValueError("interval denominator contains zero")
    values = (
        numerator.lower / denominator.lower,
        numerator.lower / denominator.upper,
        numerator.upper / denominator.lower,
        numerator.upper / denominator.upper,
    )
    return SlopeInterval(min(values), max(values))


@dataclass(frozen=True, slots=True)
class EtaAnalyticCertificate:
    """Combined ETA-root and independently derived FIFO evidence."""

    domain: EtaInterval
    image: EtaInterval | None
    scope: TemporalScope
    policy_digest: str
    partition_digest: str
    coverage_complete: bool
    evaluator_certified: bool
    continuity_certified: bool
    navigation: NavigabilityStatus
    phi_departure_slope: SlopeInterval | None
    phi_travel_slope: SlopeInterval | None
    contraction_bound: float | None
    arrival_slope: SlopeInterval | None
    root_status: EtaIntervalStatus
    fifo_status: FifoStatus
    reason: str | None = None
    fifo_reason: str | None = None
    endpoint_residuals: tuple[float, float] | None = None
    schema_version: str = "c.p0.1-temporal-eta-analytic-certificate.v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "root_status", EtaIntervalStatus(self.root_status))
        object.__setattr__(self, "fifo_status", FifoStatus(self.fifo_status))
        object.__setattr__(self, "navigation", NavigabilityStatus(self.navigation))
        if not self.policy_digest or not self.partition_digest:
            raise ValueError("analytic certificate policy and partition digests are required")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not isinstance(self.evaluator_certified, bool):
            raise ValueError("evaluator_certified must be boolean")
        if not isinstance(self.continuity_certified, bool):
            raise ValueError("continuity_certified must be boolean")
        if self.contraction_bound is not None and (
            not isfinite(self.contraction_bound) or self.contraction_bound < 0.0
        ):
            raise ValueError("contraction_bound must be finite and non-negative")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "domain": self.domain,
                "image": self.image,
                "scope": self.scope.digest,
                "policy_digest": self.policy_digest,
                "partition_digest": self.partition_digest,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "continuity_certified": self.continuity_certified,
                "navigation": self.navigation,
                "phi_departure_slope": self.phi_departure_slope,
                "phi_travel_slope": self.phi_travel_slope,
                "contraction_bound": self.contraction_bound,
                "arrival_slope": self.arrival_slope,
                "root_status": self.root_status,
                "fifo_status": self.fifo_status,
                "reason": self.reason,
                "fifo_reason": self.fifo_reason,
                "endpoint_residuals": self.endpoint_residuals,
            }
        )

    @property
    def root_authorized(self) -> bool:
        return (
            self.root_status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
            and self.image is not None
            and self.domain.contains_interval(self.image)
            and self.coverage_complete
            and self.evaluator_certified
            and self.continuity_certified
            and self.navigation is NavigabilityStatus.ALWAYS_NAVIGABLE
            and self.contraction_bound is not None
            and self.contraction_bound < 1.0
            and self.scope.evaluator_identity_known
            and self.reason is None
        )

    @property
    def fifo_authorized(self) -> bool:
        return self.root_authorized and self.fifo_status is FifoStatus.FIFO_CERTIFIED

    @property
    def permits_dominance(self) -> bool:
        return self.fifo_authorized

    def permits(self, expected_scope: Mapping[str, Any] | TemporalScope) -> bool:
        return self.fifo_authorized and self.scope.matches(expected_scope)


def qualify_analytic_eta(
    *,
    domain: EtaInterval,
    image: EtaInterval | None,
    scope: Mapping[str, Any] | TemporalScope,
    expected_scope: Mapping[str, Any] | TemporalScope | None = None,
    policy_digest: str,
    partition_digest: str,
    coverage_complete: bool,
    evaluator_certified: bool,
    continuity_certified: bool,
    navigation: NavigabilityStatus,
    phi_departure_slope: SlopeInterval | None,
    phi_travel_slope: SlopeInterval | None,
    endpoint_residuals: tuple[float, float] | None = None,
) -> EtaAnalyticCertificate:
    """Build a certificate from derived interval evidence, fail-closed."""

    active_scope = TemporalScope.from_mapping(scope)
    reason: str | None = None
    fifo_reason: str | None = None
    if expected_scope is not None and not active_scope.matches(expected_scope):
        reason = "scope_mismatch"
    scoped_policy = active_scope.mapping.get("eta_policy_digest")
    if reason is None and scoped_policy is not None and scoped_policy != policy_digest:
        reason = "policy_digest_mismatch"
    if not coverage_complete:
        reason = reason or "interval_domain_coverage_incomplete"
    if not evaluator_certified:
        reason = reason or "evaluator_not_certified"
    if not active_scope.evaluator_identity_known:
        reason = reason or "unknown_evaluator_identity"
    if not continuity_certified:
        reason = reason or "continuity_not_certified"
    if navigation is NavigabilityStatus.TRANSITION_OR_UNKNOWN:
        reason = reason or "navigability_transition_or_unknown"
    if image is None:
        reason = reason or "missing_interval_image"

    contraction: float | None = None
    arrival_slope: SlopeInterval | None = None
    if phi_departure_slope is not None and phi_travel_slope is not None:
        contraction = phi_travel_slope.absolute_upper
        try:
            denominator = SlopeInterval(
                1.0 - phi_travel_slope.upper,
                1.0 - phi_travel_slope.lower,
            )
            arrival_slope = SlopeInterval(
                1.0
                + _interval_divide(phi_departure_slope, denominator).lower,
                1.0
                + _interval_divide(phi_departure_slope, denominator).upper,
            )
        except ValueError:
            fifo_reason = fifo_reason or "implicit_sensitivity_denominator_contains_zero"

    if navigation is NavigabilityStatus.ALWAYS_BLOCKED and reason is None:
        root_status = EtaIntervalStatus.ROOT_EXCLUDED
        fifo_status = FifoStatus.FIFO_UNCERTAIN
        reason = "edge_always_blocked"
    elif image is not None and image.disjoint(domain) and reason is None:
        root_status = EtaIntervalStatus.ROOT_EXCLUDED
        fifo_status = FifoStatus.FIFO_UNCERTAIN
        reason = "interval_image_excludes_domain"
    elif (
        reason is None
        and contraction is not None
        and contraction < 1.0
        and image is not None
        and domain.contains_interval(image)
        and navigation is NavigabilityStatus.ALWAYS_NAVIGABLE
    ):
        root_status = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
        if arrival_slope is not None and arrival_slope.lower >= 0.0:
            fifo_status = FifoStatus.FIFO_CERTIFIED
        elif arrival_slope is not None and arrival_slope.upper < 0.0:
            fifo_status = FifoStatus.FIFO_VIOLATED
            fifo_reason = "arrival_operator_slope_negative"
        else:
            fifo_status = FifoStatus.FIFO_UNCERTAIN
            fifo_reason = "arrival_operator_monotonicity_unproven"
    elif (
        reason is None
        and endpoint_residuals is not None
        and continuity_certified
        and (endpoint_residuals[0] == 0.0 or endpoint_residuals[1] == 0.0 or (
            (endpoint_residuals[0] < 0.0) != (endpoint_residuals[1] < 0.0)
        ))
    ):
        root_status = EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
        fifo_status = FifoStatus.FIFO_UNCERTAIN
        reason = "continuous_endpoint_sign_change_without_contraction"
    else:
        root_status = (
            EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
            if not continuity_certified
            else EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
        )
        fifo_status = FifoStatus.FIFO_UNCERTAIN
        reason = reason or "missing_certified_contraction_or_fifo_slope_proof"

    return EtaAnalyticCertificate(
        domain=domain,
        image=image,
        scope=active_scope,
        policy_digest=policy_digest,
        partition_digest=partition_digest,
        coverage_complete=coverage_complete,
        evaluator_certified=evaluator_certified,
        continuity_certified=continuity_certified,
        navigation=navigation,
        phi_departure_slope=phi_departure_slope,
        phi_travel_slope=phi_travel_slope,
        contraction_bound=contraction,
        arrival_slope=arrival_slope,
        root_status=root_status,
        fifo_status=fifo_status,
        reason=reason,
        fifo_reason=fifo_reason,
        endpoint_residuals=endpoint_residuals,
    )


__all__ = [
    "EtaAnalyticCertificate",
    "NavigabilityStatus",
    "SlopeInterval",
    "derive_operator_sensitivity",
    "qualify_analytic_eta",
]
