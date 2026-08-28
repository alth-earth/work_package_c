"""Conservative ETA-operator interval evidence for C research.

The production planner still evaluates an edge with its historical
two-round sampler.  This module is an opt-in sidecar which asks the sampler
for envelopes over a complete departure/travel domain.  It deliberately
returns evidence, not a new planner result: a finite interval image is only
authorizable when the caller supplies an independently audited evaluator and
contraction proof.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import isfinite, nextafter, ulp
from typing import Any

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import PlannerConfig
from arctic_route_planning.grid import GeoPoint, haversine_km
from arctic_route_planning.risk.sampler import RiskIntervalSample, RiskSampler

from .eta_analytic import (
    EtaAnalyticCertificate,
    NavigabilityStatus,
    SlopeInterval,
    derive_operator_sensitivity,
    qualify_analytic_eta,
)
from .eta_interval import (
    EtaInterval,
    EtaIntervalCertificate,
    EtaIntervalStatus,
    qualify_eta_interval,
)
from .eta_refinement import EtaRefinementPolicy
from .temporal_qualification import TemporalScope, canonical_digest


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(UTC)


def _outward_lower(value: float) -> float:
    return max(0.0, nextafter(float(value), float("-inf")))


def _outward_upper(value: float) -> float:
    return nextafter(float(value), float("inf"))


@dataclass(frozen=True, slots=True)
class EtaOperatorIntervalEvidence:
    """Auditable interval image for one edge ETA operator domain."""

    departure_lower: datetime
    departure_upper: datetime
    travel_domain: EtaInterval
    image: EtaInterval | None
    scope: TemporalScope
    policy_digest: str
    evaluator_digest: str
    partition_boundaries: tuple[float, ...] = ()
    interval_samples: tuple[RiskIntervalSample, ...] = ()
    edge_factor_lower: float | None = None
    edge_factor_upper: float | None = None
    speed_lower_knots: float | None = None
    speed_upper_knots: float | None = None
    edge_distance_km: float | None = None
    coverage_complete: bool = False
    evaluator_certified: bool = False
    continuity_certified: bool = False
    contraction_bound: float | None = None
    status: EtaIntervalStatus = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    reason: str | None = None
    certificate: EtaIntervalCertificate | None = None
    analytic_certificate: EtaAnalyticCertificate | None = None
    schema_version: str = "c.p0.1-eta-operator-interval-evidence.v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "departure_lower", _utc(self.departure_lower, field="departure_lower")
        )
        object.__setattr__(
            self, "departure_upper", _utc(self.departure_upper, field="departure_upper")
        )
        if self.departure_lower > self.departure_upper:
            raise ValueError("departure interval must be ordered")
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "status", EtaIntervalStatus(self.status))
        object.__setattr__(self, "partition_boundaries", tuple(self.partition_boundaries))
        object.__setattr__(self, "interval_samples", tuple(self.interval_samples))
        if not self.policy_digest:
            raise ValueError("policy_digest must be non-empty")
        if not self.evaluator_digest:
            raise ValueError("evaluator_digest must be non-empty")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not isinstance(self.evaluator_certified, bool):
            raise ValueError("evaluator_certified must be boolean")
        if not isinstance(self.continuity_certified, bool):
            raise ValueError("continuity_certified must be boolean")

    @property
    def residual_interval(self):
        if self.image is None:
            return None
        from .eta_interval import SignedInterval

        return SignedInterval(
            self.image.lower_hours - self.travel_domain.upper_hours,
            self.image.upper_hours - self.travel_domain.lower_hours,
        )

    @property
    def certificate_digest(self) -> str | None:
        return self.certificate.digest if self.certificate is not None else None

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "departure_lower": self.departure_lower,
                "departure_upper": self.departure_upper,
                "travel_domain": self.travel_domain,
                "image": self.image,
                "scope": self.scope.digest,
                "policy_digest": self.policy_digest,
                "evaluator_digest": self.evaluator_digest,
                "partition_boundaries": self.partition_boundaries,
                "interval_samples": tuple(
                    {
                        "start": sample.start,
                        "end": sample.end,
                        "risk_lower": sample.risk_lower,
                        "risk_upper": sample.risk_upper,
                        "confidence_lower": sample.confidence_lower,
                        "confidence_upper": sample.confidence_upper,
                        "speed_lower": sample.environment_speed_factor_lower,
                        "speed_upper": sample.environment_speed_factor_upper,
                        "risk_slope_lower": sample.risk_slope_lower,
                        "risk_slope_upper": sample.risk_slope_upper,
                        "speed_slope_lower": sample.environment_speed_factor_slope_lower,
                        "speed_slope_upper": sample.environment_speed_factor_slope_upper,
                        "hard_mask_possible": sample.hard_mask_possible,
                        "navigability_status": sample.navigability_status,
                        "source_risk_ids": sample.source_risk_ids,
                        "covered_frame_times": sample.covered_frame_times,
                        "coverage_complete": sample.coverage_complete,
                        "evaluator_digest": sample.evaluator_digest,
                        "failure_reason": sample.failure_reason,
                    }
                    for sample in self.interval_samples
                ),
                "edge_factor_lower": self.edge_factor_lower,
                "edge_factor_upper": self.edge_factor_upper,
                "speed_lower_knots": self.speed_lower_knots,
                "speed_upper_knots": self.speed_upper_knots,
                "edge_distance_km": self.edge_distance_km,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "continuity_certified": self.continuity_certified,
                "contraction_bound": self.contraction_bound,
                "status": self.status,
                "reason": self.reason,
                "certificate_digest": self.certificate_digest,
                "analytic_certificate_digest": (
                    self.analytic_certificate.digest
                    if self.analytic_certificate is not None
                    else None
                ),
            }
        )

    @property
    def authorization_usable(self) -> bool:
        return bool(
            self.certificate
            and self.certificate.authorization_usable
            and self.scope.matches(self.certificate.scope)
        )

    @property
    def permits_dominance(self) -> bool:
        if self.analytic_certificate is not None:
            return self.analytic_certificate.permits_dominance
        return self.authorization_usable

    @property
    def fifo_status(self) -> str | None:
        """Return the derived FIFO status when analytic evidence is present."""

        if self.analytic_certificate is None:
            return None
        return self.analytic_certificate.fifo_status.value


class TemporalEtaIntervalEvaluator:
    """Build conservative ETA interval evidence for one geometric edge."""

    def __init__(
        self,
        risk_sampler: RiskSampler,
        vessel_model: VesselPerformanceModel,
        request: Any,
        scope: Mapping[str, Any] | TemporalScope,
        *,
        edge_sample_points: Sequence[GeoPoint] | None = None,
        edge_distance_km: float | None = None,
        planner_config: PlannerConfig | None = None,
        eta_policy: EtaRefinementPolicy | None = None,
        evaluator_certified: bool = False,
        continuity_certified: bool = False,
        contraction_bound: float | None = None,
        evaluator_digest: str | None = None,
    ) -> None:
        self.risk_sampler = risk_sampler
        self.vessel_model = vessel_model
        self.request = request
        self.scope = TemporalScope.from_mapping(scope)
        self.edge_sample_points = tuple(edge_sample_points or ())
        self.edge_distance_km = edge_distance_km
        self.planner_config = planner_config or getattr(request, "planner_config", None)
        self.eta_policy = eta_policy or EtaRefinementPolicy(method="bounded")
        self.evaluator_certified = evaluator_certified
        self.continuity_certified = continuity_certified
        self.contraction_bound = contraction_bound
        self.evaluator_digest = evaluator_digest or canonical_digest(
            {
                "schema_version": "c.p0.1-eta-operator-interval-evaluator.v1",
                "risk_sampler": risk_sampler.interval_evaluator_digest,
                "vessel_model": vessel_model,
                "eta_policy": self.eta_policy,
                "scope": self.scope.digest,
            }
        )

    def evaluate(
        self,
        departure_interval: datetime | tuple[datetime, datetime] | EtaInterval,
        travel_hour_domain: EtaInterval,
        edge_sample_points: Sequence[GeoPoint] | None = None,
        scope: Mapping[str, Any] | TemporalScope | None = None,
        *,
        contraction_bound: float | None = None,
        evaluator_certified: bool | None = None,
        continuity_certified: bool | None = None,
        endpoint_residuals: tuple[float, float] | None = None,
    ) -> EtaOperatorIntervalEvidence:
        """Evaluate a complete domain and return fail-closed evidence.

        ``departure_interval`` is normally an exact UTC datetime or a pair
        of UTC datetimes.  For compact synthetic callers an ``EtaInterval``
        is accepted as an offset from ``request.departure_time``.  The
        latter is still converted to absolute UTC before risk sampling.
        """

        expected_scope = self.scope
        active_scope = TemporalScope.from_mapping(scope or expected_scope)
        try:
            dep_lower, dep_upper = self._departure_interval(departure_interval)
        except Exception as error:
            fallback = getattr(self.request, "departure_time", self.risk_sampler.start_time)
            fallback = _utc(fallback, field="fallback_departure")
            return self._uncertain(
                fallback,
                fallback,
                travel_hour_domain,
                active_scope,
                canonical_digest(self.eta_policy),
                reason=f"invalid_departure:{type(error).__name__}",
            )
        points = tuple(edge_sample_points or self.edge_sample_points)
        policy_digest = canonical_digest(self.eta_policy)
        certified = self.evaluator_certified if evaluator_certified is None else evaluator_certified
        continuous = (
            self.continuity_certified
            if continuity_certified is None
            else continuity_certified
        )
        contraction = self.contraction_bound if contraction_bound is None else contraction_bound

        if not active_scope.matches(expected_scope):
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason="scope_mismatch",
            )
        scoped_policy_digest = active_scope.mapping.get("eta_policy_digest")
        if scoped_policy_digest is not None and scoped_policy_digest != policy_digest:
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason="policy_digest_mismatch",
            )
        edge_evaluator_digest = active_scope.mapping.get("edge_evaluator_digest")
        if certified and (
            not active_scope.evaluator_identity_known
            or not isinstance(edge_evaluator_digest, str)
            or edge_evaluator_digest.startswith("unknown:")
        ):
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason="unknown_evaluator_identity",
            )
        if self.eta_policy.method != "bounded":
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason="unsupported_eta_policy",
            )
        if len(points) < 2:
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason="insufficient_edge_sample_points",
            )
        try:
            distance_km = self._edge_distance(points)
            samples = tuple(
                self._sample_point(
                    point,
                    index / (len(points) - 1),
                    dep_lower,
                    dep_upper,
                    travel_hour_domain,
                )
                for index, point in enumerate(points)
            )
        except Exception as error:
            return self._uncertain(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                reason=f"evaluator_failure:{type(error).__name__}",
            )

        failure = next((sample.failure_reason for sample in samples if not sample.usable), None)
        if failure is not None:
            status = (
                EtaIntervalStatus.UNCERTAIN_COVERAGE
                if "coverage" in failure.lower() or "gap" in failure.lower()
                else EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
            )
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=False,
                evaluator_certified=certified,
                continuity_certified=False,
                contraction_bound=contraction,
                status=status,
                reason=failure,
            )

        if any(sample.hard_mask_possible for sample in samples):
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=False,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_DISCONTINUITY,
                reason="hard_mask_discontinuity",
            )

        minimum_confidence = float(
            getattr(self.planner_config, "minimum_confidence", 0.0)
        )
        if any(
            sample.confidence_lower is None or sample.confidence_lower < minimum_confidence
            for sample in samples
        ):
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=False,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_DISCONTINUITY,
                reason="confidence_threshold_crossing",
            )
        maximum_risk = getattr(self.request, "maximum_risk", None)
        if maximum_risk is not None and any(
            sample.risk_upper is None or sample.risk_upper > maximum_risk
            for sample in samples
        ):
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=False,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_DISCONTINUITY,
                reason="risk_threshold_crossing",
            )

        factor_lower = min(sample.environment_speed_factor_lower for sample in samples)
        factor_upper = min(sample.environment_speed_factor_upper for sample in samples)
        assert factor_lower is not None and factor_upper is not None
        if not isfinite(factor_lower) or not isfinite(factor_upper) or factor_lower <= 0:
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=continuous,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE,
                reason="invalid_speed_factor_interval",
            )
        try:
            speed_lower = self.vessel_model.effective_speed(factor_lower)
            speed_upper = self.vessel_model.effective_speed(factor_upper)
        except Exception as error:
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=continuous,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE,
                reason=f"speed_evaluator_failure:{type(error).__name__}",
            )

        travel_lower = _outward_lower(distance_km / speed_upper.speed_km_per_hour)
        travel_upper = _outward_upper(distance_km / speed_lower.speed_km_per_hour)
        if travel_lower <= 0 or not isfinite(travel_upper):
            return self._evidence(
                dep_lower,
                dep_upper,
                travel_hour_domain,
                active_scope,
                policy_digest,
                samples,
                image=None,
                distance_km=distance_km,
                coverage_complete=True,
                evaluator_certified=certified,
                continuity_certified=continuous,
                contraction_bound=contraction,
                status=EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE,
                reason="invalid_travel_image",
            )
        image = EtaInterval(travel_lower, travel_upper)
        boundaries = self._partition_boundaries(samples, dep_lower, travel_hour_domain)
        boundary_continuity = continuous
        boundary_evidence = ("risk_frame_partition",) if boundaries else ()
        qualified_contraction = contraction if boundary_continuity else None
        certificate = qualify_eta_interval(
            travel_hour_domain,
            lambda _domain: image,
            scope=active_scope,
            coverage_complete=True,
            evaluator_certified=certified,
            contraction_bound=qualified_contraction,
            continuity_certified=boundary_continuity,
            endpoint_residuals=endpoint_residuals,
            policy_digest=policy_digest,
            partition_digest=canonical_digest(boundaries),
            boundary_evidence=boundary_evidence,
        )
        return self._evidence(
            dep_lower,
            dep_upper,
            travel_hour_domain,
            active_scope,
            policy_digest,
            samples,
            image=image,
            distance_km=distance_km,
            coverage_complete=True,
            evaluator_certified=certified,
            continuity_certified=boundary_continuity,
            contraction_bound=contraction,
            status=certificate.status,
            reason=certificate.reason,
            certificate=certificate,
            partition_boundaries=boundaries,
            edge_factor_lower=factor_lower,
            edge_factor_upper=factor_upper,
            speed_lower_knots=speed_lower.speed_knots,
            speed_upper_knots=speed_upper.speed_knots,
            edge_distance_km=distance_km,
            boundary_evidence=boundary_evidence,
        )

    __call__ = evaluate

    def evaluate_analytic(
        self,
        departure_interval: datetime | tuple[datetime, datetime] | EtaInterval,
        travel_hour_domain: EtaInterval,
        edge_sample_points: Sequence[GeoPoint] | None = None,
        scope: Mapping[str, Any] | TemporalScope | None = None,
        *,
        endpoint_residuals: tuple[float, float] | None = None,
    ) -> EtaOperatorIntervalEvidence:
        """Derive ETA-root and FIFO evidence without caller proof flags.

        The historical :meth:`evaluate` method remains available for the
        finite qualification runner.  This method only authorizes a proof
        when the scope explicitly names the certified evaluator, interval
        samples carry complete coverage, frame/threshold behavior is
        continuous, and the sensitivity bounds mechanically imply both a
        unique root and a non-negative arrival slope.
        """

        active_scope = TemporalScope.from_mapping(scope or self.scope)
        base = self.evaluate(
            departure_interval,
            travel_hour_domain,
            edge_sample_points=edge_sample_points,
            scope=active_scope,
            contraction_bound=0.0,
            evaluator_certified=True,
            continuity_certified=True,
            endpoint_residuals=endpoint_residuals,
        )
        policy_digest = canonical_digest(self.eta_policy)
        samples = base.interval_samples
        complete = bool(samples) and all(sample.usable for sample in samples)
        hard_values = tuple(sample.hard_mask_possible for sample in samples)
        if complete and all(not value for value in hard_values):
            navigation = NavigabilityStatus.ALWAYS_NAVIGABLE
        elif complete and hard_values and all(hard_values):
            navigation = NavigabilityStatus.ALWAYS_BLOCKED
        else:
            navigation = NavigabilityStatus.TRANSITION_OR_UNKNOWN

        evaluator_certified = bool(
            complete
            and active_scope.mapping.get("evaluator_certification")
            == "certified:c.temporal-evaluator.v1"
            and all(
                sample.evaluator_digest == self.risk_sampler.interval_evaluator_digest
                for sample in samples
            )
        )
        continuity_certified = bool(
            complete
            and navigation is NavigabilityStatus.ALWAYS_NAVIGABLE
            and all(
                _collapsed_interval(
                    sample.environment_speed_factor_lower,
                    sample.environment_speed_factor_upper,
                )
                and _collapsed_interval(sample.confidence_lower, sample.confidence_upper)
                and sample.risk_slope_lower is not None
                and sample.risk_slope_upper is not None
                and sample.environment_speed_factor_slope_lower is not None
                and sample.environment_speed_factor_slope_upper is not None
                for sample in samples
            )
        )

        phi_departure: SlopeInterval | None = None
        phi_travel: SlopeInterval | None = None
        if complete and base.edge_distance_km is not None:
            slope_lower = min(
                sample.environment_speed_factor_slope_lower or 0.0
                for sample in samples
            )
            slope_upper = max(
                sample.environment_speed_factor_slope_upper or 0.0
                for sample in samples
            )
            try:
                phi_departure, phi_travel, _ = derive_operator_sensitivity(
                    edge_distance_km=base.edge_distance_km,
                    vessel_model=self.vessel_model,
                    speed_factor_slope=SlopeInterval(slope_lower, slope_upper),
                )
            except ValueError:
                phi_departure = None
                phi_travel = None

        partition_digest = canonical_digest(
            {
                "boundaries": base.partition_boundaries,
                "sample_digests": tuple(
                    {
                        "start": sample.start,
                        "end": sample.end,
                        "frames": sample.covered_frame_times,
                        "sources": sample.source_risk_ids,
                    }
                    for sample in samples
                ),
            }
        )
        certificate = qualify_analytic_eta(
            domain=travel_hour_domain,
            image=base.image,
            scope=active_scope,
            expected_scope=self.scope,
            policy_digest=policy_digest,
            partition_digest=partition_digest,
            coverage_complete=complete,
            evaluator_certified=evaluator_certified,
            continuity_certified=continuity_certified,
            navigation=navigation,
            phi_departure_slope=phi_departure,
            phi_travel_slope=phi_travel,
            endpoint_residuals=endpoint_residuals,
        )
        return replace(
            base,
            analytic_certificate=certificate,
            status=certificate.root_status,
            reason=certificate.reason,
            evaluator_certified=certificate.evaluator_certified,
            continuity_certified=certificate.continuity_certified,
            contraction_bound=certificate.contraction_bound,
        )
    def _sample_point(
        self,
        point: GeoPoint,
        fraction: float,
        dep_lower: datetime,
        dep_upper: datetime,
        travel_domain: EtaInterval,
    ) -> RiskIntervalSample:
        lower = dep_lower + timedelta(hours=fraction * travel_domain.lower_hours)
        upper = dep_upper + timedelta(hours=fraction * travel_domain.upper_hours)
        return self.risk_sampler._sample_interval(
            lower,
            upper,
            point.longitude,
            point.latitude,
        )

    def _departure_interval(
        self,
        value: datetime | tuple[datetime, datetime] | EtaInterval,
    ) -> tuple[datetime, datetime]:
        if isinstance(value, datetime):
            instant = _utc(value, field="departure")
            return instant, instant
        if isinstance(value, EtaInterval):
            base = getattr(self.request, "departure_time", None)
            if not isinstance(base, datetime):
                raise ValueError("request.departure_time is required for offset departure interval")
            base = _utc(base, field="request.departure_time")
            return (
                base + timedelta(hours=value.lower_hours),
                base + timedelta(hours=value.upper_hours),
            )
        if len(value) != 2:
            raise ValueError("departure interval must contain two datetimes")
        return _utc(value[0], field="departure_lower"), _utc(value[1], field="departure_upper")

    def _edge_distance(self, points: Sequence[GeoPoint]) -> float:
        if self.edge_distance_km is not None:
            distance = float(self.edge_distance_km)
        else:
            from itertools import pairwise

            distance = sum(haversine_km(left, right) for left, right in pairwise(points))
        if not isfinite(distance) or distance <= 0:
            raise ValueError("edge distance must be finite and positive")
        return distance

    @staticmethod
    def _partition_boundaries(
        samples: Sequence[RiskIntervalSample],
        departure: datetime,
        domain: EtaInterval,
    ) -> tuple[float, ...]:
        values: set[float] = set()
        for sample in samples:
            for boundary in sample.covered_frame_times:
                offset = (boundary - departure).total_seconds() / 3600.0
                if domain.lower_hours < offset < domain.upper_hours:
                    values.add(offset)
        return tuple(sorted(values))

    def _uncertain(
        self,
        dep_lower: datetime,
        dep_upper: datetime,
        domain: EtaInterval,
        scope: TemporalScope,
        policy_digest: str,
        *,
        reason: str,
    ) -> EtaOperatorIntervalEvidence:
        return self._evidence(
            dep_lower,
            dep_upper,
            domain,
            scope,
            policy_digest,
            (),
            image=None,
            distance_km=None,
            coverage_complete=False,
            evaluator_certified=False,
            continuity_certified=False,
            contraction_bound=None,
            status=(
                EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
                if reason == "scope_mismatch"
                else EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
            ),
            reason=reason,
        )

    def _evidence(
        self,
        dep_lower: datetime,
        dep_upper: datetime,
        domain: EtaInterval,
        scope: TemporalScope,
        policy_digest: str,
        samples: Sequence[RiskIntervalSample],
        *,
        image: EtaInterval | None,
        distance_km: float | None,
        coverage_complete: bool,
        evaluator_certified: bool,
        continuity_certified: bool,
        contraction_bound: float | None,
        status: EtaIntervalStatus,
        reason: str | None,
        certificate: EtaIntervalCertificate | None = None,
        partition_boundaries: tuple[float, ...] = (),
        edge_factor_lower: float | None = None,
        edge_factor_upper: float | None = None,
        speed_lower_knots: float | None = None,
        speed_upper_knots: float | None = None,
        edge_distance_km: float | None = None,
        boundary_evidence: tuple[str, ...] = (),
    ) -> EtaOperatorIntervalEvidence:
        del distance_km  # retained in the call signature for evidence symmetry
        del boundary_evidence  # represented by partition_boundaries and certificate digest
        return EtaOperatorIntervalEvidence(
            departure_lower=dep_lower,
            departure_upper=dep_upper,
            travel_domain=domain,
            image=image,
            scope=scope,
            policy_digest=policy_digest,
            evaluator_digest=self.evaluator_digest,
            partition_boundaries=partition_boundaries,
            interval_samples=tuple(samples),
            edge_factor_lower=edge_factor_lower,
            edge_factor_upper=edge_factor_upper,
            speed_lower_knots=speed_lower_knots,
            speed_upper_knots=speed_upper_knots,
            edge_distance_km=edge_distance_km,
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            continuity_certified=continuity_certified,
            contraction_bound=contraction_bound,
            status=status,
            reason=reason,
            certificate=certificate,
        )


def _collapsed_interval(lower: float | None, upper: float | None) -> bool:
    """Recognize an outward-rounded constant without hiding real variation."""

    if lower is None or upper is None or not isfinite(lower) or not isfinite(upper):
        return False
    scale = max(1.0, abs(lower), abs(upper))
    return upper >= lower and upper - lower <= 4.0 * ulp(scale)


__all__ = ["EtaOperatorIntervalEvidence", "TemporalEtaIntervalEvaluator"]
