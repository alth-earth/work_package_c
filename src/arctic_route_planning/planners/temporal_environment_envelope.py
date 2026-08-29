"""C-internal environmental speed envelopes for exact-arrival search.

The production edge evaluator remains the source of truth.  This sidecar only
derives conservative *lower* travel-time bounds from a complete interval
upper bound on the environmental speed factor.  A partial certificate can
authorize the explicitly listed directed edges; missing, blocked, or
uncertain edges remain live.  No public planner or contract imports this
module implicitly.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite, nextafter
from typing import Any

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.risk.sampler import RiskIntervalSample, RiskSampler

from .temporal_qualification import TemporalScope, canonical_digest

ENVIRONMENT_SPEED_ENVELOPE_SCHEMA = "c.p0.2-temporal-environment-speed-envelope.v1"
ENVIRONMENT_SPEED_ENVELOPE_METHOD = "risk-interval-speed-upper-v1"


class EnvironmentalSpeedEnvelopeStatus(StrEnum):
    """Qualification state for an environmental edge envelope."""

    CERTIFIED = "CERTIFIED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(UTC)


def _outward_lower(value: float) -> float:
    """Round a lower bound toward negative infinity without going below zero."""

    if value <= 0.0:
        return 0.0
    return max(0.0, nextafter(float(value), float("-inf")))


def _point_payload(point: GeoPoint) -> tuple[float, float]:
    latitude = float(point.latitude)
    longitude = float(point.longitude)
    if not isfinite(latitude) or not isfinite(longitude):
        raise ValueError("edge sample point coordinates must be finite")
    return longitude, latitude


def _sample_digest(sample: RiskIntervalSample) -> dict[str, Any]:
    return {
        "start": sample.start,
        "end": sample.end,
        "risk_lower": sample.risk_lower,
        "risk_upper": sample.risk_upper,
        "confidence_lower": sample.confidence_lower,
        "confidence_upper": sample.confidence_upper,
        "speed_lower": sample.environment_speed_factor_lower,
        "speed_upper": sample.environment_speed_factor_upper,
        "effective_speed_lower": sample.effective_environment_speed_factor_lower,
        "effective_speed_upper": sample.effective_environment_speed_factor_upper,
        "hard_mask_possible": sample.hard_mask_possible,
        "navigability_status": sample.navigability_status,
        "covered_frame_times": sample.covered_frame_times,
        "source_risk_ids": sample.source_risk_ids,
        "coverage_complete": sample.coverage_complete,
        "evaluator_digest": sample.evaluator_digest,
        "failure_reason": sample.failure_reason,
    }


@dataclass(frozen=True, slots=True)
class EnvironmentalEdgeSpeedEvidence:
    """Auditable speed upper bound for one directed edge."""

    start_node: Any
    end_node: Any
    edge_distance_km: float
    departure_lower: datetime
    departure_upper: datetime
    sample_points: tuple[GeoPoint, ...]
    interval_samples: tuple[RiskIntervalSample, ...]
    scope: TemporalScope
    speed_factor_upper: float | None = None
    speed_upper_km_per_hour: float | None = None
    travel_lower_hours: float | None = None
    partition_boundaries: tuple[datetime, ...] = ()
    coverage_complete: bool = False
    evaluator_certified: bool = False
    hard_mask_possible: bool = False
    navigability_status: str = "TRANSITION_OR_UNKNOWN"
    status: EnvironmentalSpeedEnvelopeStatus = EnvironmentalSpeedEnvelopeStatus.REJECTED
    reason: str | None = None
    evaluator_digest: str = ""
    schema_version: str = ENVIRONMENT_SPEED_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "departure_lower", _utc(self.departure_lower, field="departure_lower")
        )
        object.__setattr__(
            self, "departure_upper", _utc(self.departure_upper, field="departure_upper")
        )
        if self.departure_lower > self.departure_upper:
            raise ValueError("departure interval must be ordered")
        if not isfinite(float(self.edge_distance_km)) or self.edge_distance_km < 0.0:
            raise ValueError("edge distance must be finite and non-negative")
        object.__setattr__(self, "edge_distance_km", float(self.edge_distance_km))
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(self, "sample_points", tuple(self.sample_points))
        object.__setattr__(self, "interval_samples", tuple(self.interval_samples))
        object.__setattr__(
            self,
            "partition_boundaries",
            tuple(_utc(value, field="partition_boundary") for value in self.partition_boundaries),
        )
        object.__setattr__(self, "status", EnvironmentalSpeedEnvelopeStatus(self.status))
        if self.schema_version != ENVIRONMENT_SPEED_ENVELOPE_SCHEMA:
            raise ValueError("unsupported environmental speed envelope schema")
        for name in ("coverage_complete", "evaluator_certified", "hard_mask_possible"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be boolean")
        for name in ("speed_factor_upper", "speed_upper_km_per_hour", "travel_lower_hours"):
            value = getattr(self, name)
            if value is not None and (not isfinite(float(value)) or float(value) < 0.0):
                raise ValueError(f"{name} must be finite and non-negative")
        if self.status is EnvironmentalSpeedEnvelopeStatus.CERTIFIED:
            if not self.coverage_complete or not self.evaluator_certified:
                raise ValueError("certified edge evidence requires complete evaluator coverage")
            if self.speed_factor_upper is None or self.speed_upper_km_per_hour is None:
                raise ValueError("certified edge evidence requires a speed upper bound")
            if self.travel_lower_hours is None:
                raise ValueError("certified edge evidence requires a travel lower bound")
        if not self.evaluator_digest:
            raise ValueError("edge evidence requires evaluator identity")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "start_node": self.start_node,
                "end_node": self.end_node,
                "edge_distance_km": self.edge_distance_km,
                "departure_lower": self.departure_lower,
                "departure_upper": self.departure_upper,
                "sample_points": tuple(_point_payload(point) for point in self.sample_points),
                "interval_samples": tuple(
                    _sample_digest(sample) for sample in self.interval_samples
                ),
                "scope": self.scope.digest,
                "speed_factor_upper": self.speed_factor_upper,
                "speed_upper_km_per_hour": self.speed_upper_km_per_hour,
                "travel_lower_hours": self.travel_lower_hours,
                "partition_boundaries": self.partition_boundaries,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "hard_mask_possible": self.hard_mask_possible,
                "navigability_status": self.navigability_status,
                "status": self.status,
                "reason": self.reason,
                "evaluator_digest": self.evaluator_digest,
            }
        )

    @property
    def usable(self) -> bool:
        return bool(
            self.status is EnvironmentalSpeedEnvelopeStatus.CERTIFIED
            and self.coverage_complete
            and self.evaluator_certified
            and self.speed_factor_upper is not None
            and self.speed_upper_km_per_hour is not None
            and self.travel_lower_hours is not None
            and not self.hard_mask_possible
            and self.navigability_status == "ALWAYS_NAVIGABLE"
            and self.reason is None
        )


@dataclass(frozen=True, slots=True)
class TemporalEnvironmentalSpeedEnvelope:
    """Aggregate proof-carrying edge lower bounds.

    ``PARTIAL`` is intentionally usable only for the listed edge pairs.  A
    caller must pass ``edge_bound_partial=True`` to the state-bound adapter;
    absent pairs remain exact-search edges.
    """

    scope: TemporalScope
    departure_lower: datetime
    departure_upper: datetime
    horizon_hours: float
    universe_nodes: tuple[Any, ...]
    edge_evidence: tuple[EnvironmentalEdgeSpeedEvidence, ...]
    edge_lower_hours: tuple[tuple[Any, Any, float], ...]
    expected_edge_count: int
    covered_edge_count: int
    coverage_complete: bool
    evaluator_certified: bool
    proof_digest: str
    status: EnvironmentalSpeedEnvelopeStatus
    reason: str | None = None
    method: str = ENVIRONMENT_SPEED_ENVELOPE_METHOD
    evaluator_digest: str = ""
    schema_version: str = ENVIRONMENT_SPEED_ENVELOPE_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", TemporalScope.from_mapping(self.scope))
        object.__setattr__(
            self, "departure_lower", _utc(self.departure_lower, field="departure_lower")
        )
        object.__setattr__(
            self, "departure_upper", _utc(self.departure_upper, field="departure_upper")
        )
        object.__setattr__(self, "universe_nodes", tuple(self.universe_nodes))
        object.__setattr__(self, "edge_evidence", tuple(self.edge_evidence))
        object.__setattr__(self, "edge_lower_hours", tuple(self.edge_lower_hours))
        object.__setattr__(self, "status", EnvironmentalSpeedEnvelopeStatus(self.status))
        if self.schema_version != ENVIRONMENT_SPEED_ENVELOPE_SCHEMA:
            raise ValueError("unsupported environmental speed envelope schema")
        if self.departure_lower > self.departure_upper:
            raise ValueError("departure interval must be ordered")
        if not isfinite(float(self.horizon_hours)) or self.horizon_hours <= 0.0:
            raise ValueError("horizon_hours must be finite and positive")
        if self.expected_edge_count < 0 or self.covered_edge_count < 0:
            raise ValueError("edge counts must be non-negative")
        if self.covered_edge_count > self.expected_edge_count:
            raise ValueError("covered edge count cannot exceed expected count")
        if not isinstance(self.coverage_complete, bool) or not isinstance(
            self.evaluator_certified, bool
        ):
            raise ValueError("coverage and evaluator flags must be boolean")
        if not self.proof_digest or not self.evaluator_digest:
            raise ValueError("environment envelope requires stable digests")
        if self.status is EnvironmentalSpeedEnvelopeStatus.CERTIFIED and not self.coverage_complete:
            raise ValueError("certified environment envelope requires complete coverage")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "scope": self.scope.digest,
                "departure_lower": self.departure_lower,
                "departure_upper": self.departure_upper,
                "horizon_hours": self.horizon_hours,
                "universe_nodes": self.universe_nodes,
                "edge_evidence": tuple(item.digest for item in self.edge_evidence),
                "edge_lower_hours": self.edge_lower_hours,
                "expected_edge_count": self.expected_edge_count,
                "covered_edge_count": self.covered_edge_count,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "proof_digest": self.proof_digest,
                "status": self.status,
                "reason": self.reason,
                "method": self.method,
                "evaluator_digest": self.evaluator_digest,
            }
        )

    @property
    def edge_lower_map(self) -> dict[tuple[Any, Any], float]:
        return {(start, end): value for start, end, value in self.edge_lower_hours}

    @property
    def usable(self) -> bool:
        return bool(
            self.status
            in {
                EnvironmentalSpeedEnvelopeStatus.CERTIFIED,
                EnvironmentalSpeedEnvelopeStatus.PARTIAL,
            }
            and self.scope.evaluator_identity_known
            and self.covered_edge_count > 0
            and self.edge_lower_hours
            and self.reason in {None, "partial_edge_coverage"}
        )

    @property
    def partial(self) -> bool:
        return self.status is EnvironmentalSpeedEnvelopeStatus.PARTIAL


def _rejected(
    *,
    scope: TemporalScope,
    departure_lower: datetime,
    departure_upper: datetime,
    horizon_hours: float,
    universe_nodes: tuple[Any, ...],
    expected_edge_count: int,
    evaluator_digest: str,
    reason: str,
) -> TemporalEnvironmentalSpeedEnvelope:
    proof_digest = canonical_digest(
        {
            "schema_version": ENVIRONMENT_SPEED_ENVELOPE_SCHEMA,
            "scope": scope.digest,
            "departure_lower": departure_lower,
            "departure_upper": departure_upper,
            "horizon_hours": horizon_hours,
            "universe_nodes": universe_nodes,
            "expected_edge_count": expected_edge_count,
            "reason": reason,
            "method": ENVIRONMENT_SPEED_ENVELOPE_METHOD,
            "evaluator_digest": evaluator_digest,
        }
    )
    return TemporalEnvironmentalSpeedEnvelope(
        scope=scope,
        departure_lower=departure_lower,
        departure_upper=departure_upper,
        horizon_hours=horizon_hours,
        universe_nodes=universe_nodes,
        edge_evidence=(),
        edge_lower_hours=(),
        expected_edge_count=expected_edge_count,
        covered_edge_count=0,
        coverage_complete=False,
        evaluator_certified=False,
        proof_digest=proof_digest,
        status=EnvironmentalSpeedEnvelopeStatus.REJECTED,
        reason=reason,
        evaluator_digest=evaluator_digest,
    )


def _edge_evidence(
    *,
    risk_sampler: RiskSampler,
    vessel_model: VesselPerformanceModel,
    scope: TemporalScope,
    start_node: Any,
    end_node: Any,
    edge_distance_km: float,
    points: tuple[GeoPoint, ...],
    departure_lower: datetime,
    departure_upper: datetime,
    horizon_hours: float,
    evaluator_certified: bool,
) -> EnvironmentalEdgeSpeedEvidence:
    evaluator_digest = risk_sampler.interval_evaluator_digest
    samples: list[RiskIntervalSample] = []
    reason: str | None = None
    hard_mask_possible = False
    navigability = "ALWAYS_NAVIGABLE"
    # The edge pre-gate is only used for arrivals which can still fit inside
    # the finite horizon.  Every evaluator sample of such a traversal lies in
    # this closed interval; a failed interval therefore never becomes a safe
    # guessed value.
    interval_end = departure_upper + timedelta(hours=horizon_hours)
    for point in points:
        try:
            longitude, latitude = _point_payload(point)
            sample = risk_sampler._sample_interval(
                departure_lower,
                interval_end,
                longitude,
                latitude,
            )
        except Exception as error:  # private evaluator boundary is fail-closed
            reason = f"evaluator_failure:{type(error).__name__}"
            break
        samples.append(sample)
        hard_mask_possible = hard_mask_possible or sample.hard_mask_possible
        if sample.navigability_status != "ALWAYS_NAVIGABLE":
            navigability = "TRANSITION_OR_UNKNOWN"
        if not sample.usable:
            reason = sample.failure_reason or "coverage_incomplete"
            break
        if sample.evaluator_digest != evaluator_digest:
            reason = "evaluator_digest_mismatch"
            break
        if sample.hard_mask_possible:
            reason = "hard_mask_or_navigability_uncertain"
            break

    if reason is None and navigability != "ALWAYS_NAVIGABLE":
        reason = "hard_mask_or_navigability_uncertain"
    complete = reason is None and len(samples) == len(points)
    certified = bool(complete and evaluator_certified and scope.evaluator_identity_known)
    speed_factor_upper: float | None = None
    speed_upper_km_per_hour: float | None = None
    travel_lower_hours: float | None = None
    if certified:
        upper_values: list[float] = []
        for sample in samples:
            value = sample.effective_environment_speed_factor_upper
            if value is None:
                value = sample.environment_speed_factor_upper
            if value is None or not isfinite(float(value)) or not 0.0 < float(value) <= 1.0:
                reason = "invalid_speed_factor_upper"
                certified = False
                break
            upper_values.append(float(value))
        if certified:
            speed_factor_upper = min(upper_values)
            try:
                speed = vessel_model.effective_speed(speed_factor_upper)
            except Exception as error:
                reason = f"speed_evaluator_failure:{type(error).__name__}"
                certified = False
            else:
                speed_upper_km_per_hour = float(speed.speed_km_per_hour)
                if not isfinite(speed_upper_km_per_hour) or speed_upper_km_per_hour <= 0.0:
                    reason = "invalid_speed_upper"
                    certified = False
                else:
                    travel_lower_hours = _outward_lower(
                        float(edge_distance_km) / speed_upper_km_per_hour
                    )
    boundaries = tuple(
        sorted({boundary for sample in samples for boundary in sample.covered_frame_times})
    )
    status = (
        EnvironmentalSpeedEnvelopeStatus.CERTIFIED
        if certified and reason is None
        else EnvironmentalSpeedEnvelopeStatus.REJECTED
    )
    if status is EnvironmentalSpeedEnvelopeStatus.CERTIFIED:
        reason = None
    return EnvironmentalEdgeSpeedEvidence(
        start_node=start_node,
        end_node=end_node,
        edge_distance_km=edge_distance_km,
        departure_lower=departure_lower,
        departure_upper=departure_upper,
        sample_points=points,
        interval_samples=tuple(samples),
        scope=scope,
        speed_factor_upper=speed_factor_upper,
        speed_upper_km_per_hour=speed_upper_km_per_hour,
        travel_lower_hours=travel_lower_hours,
        partition_boundaries=boundaries,
        coverage_complete=complete,
        evaluator_certified=certified,
        hard_mask_possible=hard_mask_possible,
        navigability_status=navigability,
        status=status,
        reason=reason,
        evaluator_digest=evaluator_digest,
    )


def qualify_environmental_speed_envelope(
    *,
    risk_sampler: RiskSampler,
    vessel_model: VesselPerformanceModel,
    scope: TemporalScope,
    departure_lower: datetime,
    departure_upper: datetime | None = None,
    horizon_hours: float,
    edges: Iterable[tuple[Any, Any, float, Sequence[GeoPoint]]],
    universe_nodes: Iterable[Any] = (),
    expected_scope: TemporalScope | None = None,
    evaluator_certified: bool = False,
) -> TemporalEnvironmentalSpeedEnvelope:
    """Qualify per-edge environmental speed upper bounds.

    ``edges`` is normally the complete directed adjacency.  A failed edge is
    not removed from the graph; it is omitted from the certified map and the
    aggregate becomes ``PARTIAL`` when another edge remains usable.  This is
    the explicit fail-closed partial-coverage mode consumed by
    ``TemporalStateBoundCertificate.edge_bound_partial``.
    """

    active_scope = TemporalScope.from_mapping(scope)
    dep_upper = departure_lower if departure_upper is None else departure_upper
    try:
        dep_lower = _utc(departure_lower, field="departure_lower")
        dep_upper = _utc(dep_upper, field="departure_upper")
    except Exception as error:
        fallback = datetime(1970, 1, 1, tzinfo=UTC)
        return _rejected(
            scope=active_scope,
            departure_lower=fallback,
            departure_upper=fallback,
            horizon_hours=1.0,
            universe_nodes=tuple(universe_nodes),
            expected_edge_count=0,
            evaluator_digest=getattr(
                risk_sampler, "interval_evaluator_digest", "unknown:evaluator"
            ),
            reason=f"invalid_departure:{type(error).__name__}",
        )
    try:
        universe = tuple(dict.fromkeys(universe_nodes))
    except TypeError:
        universe = ()
    try:
        raw_edges = tuple(edges)
        edge_keys = [(item[0], item[1]) for item in raw_edges]
        if len(set(edge_keys)) != len(edge_keys):
            raise ValueError("duplicate_edge")
    except Exception as error:
        return _rejected(
            scope=active_scope,
            departure_lower=dep_lower,
            departure_upper=dep_upper,
            horizon_hours=1.0,
            universe_nodes=universe,
            expected_edge_count=0,
            evaluator_digest=getattr(
                risk_sampler, "interval_evaluator_digest", "unknown:evaluator"
            ),
            reason=f"invalid_edge_domain:{type(error).__name__}",
        )
    evaluator_digest = risk_sampler.interval_evaluator_digest
    if expected_scope is not None and not active_scope.matches(expected_scope):
        return _rejected(
            scope=active_scope,
            departure_lower=dep_lower,
            departure_upper=dep_upper,
            horizon_hours=float(horizon_hours),
            universe_nodes=universe,
            expected_edge_count=len(raw_edges),
            evaluator_digest=evaluator_digest,
            reason="scope_mismatch",
        )
    try:
        horizon = float(horizon_hours)
    except (TypeError, ValueError):
        horizon = 0.0
    if (
        dep_lower > dep_upper
        or not isfinite(horizon)
        or horizon <= 0.0
        or not active_scope.evaluator_identity_known
        or not evaluator_certified
    ):
        reason = "invalid_domain"
        if not active_scope.evaluator_identity_known:
            reason = "unknown_evaluator"
        elif not evaluator_certified:
            reason = "evaluator_not_certified"
        return _rejected(
            scope=active_scope,
            departure_lower=dep_lower,
            departure_upper=dep_upper,
            horizon_hours=horizon if isfinite(horizon) and horizon > 0.0 else 1.0,
            universe_nodes=universe,
            expected_edge_count=len(raw_edges),
            evaluator_digest=evaluator_digest,
            reason=reason,
        )

    evidence: list[EnvironmentalEdgeSpeedEvidence] = []
    lower_rows: list[tuple[Any, Any, float]] = []
    for item in raw_edges:
        if not isinstance(item, (tuple, list)) or len(item) != 4:
            evidence.append(
                EnvironmentalEdgeSpeedEvidence(
                    start_node=None,
                    end_node=None,
                    edge_distance_km=0.0,
                    departure_lower=dep_lower,
                    departure_upper=dep_upper,
                    sample_points=(),
                    interval_samples=(),
                    scope=active_scope,
                    coverage_complete=False,
                    evaluator_certified=False,
                    status=EnvironmentalSpeedEnvelopeStatus.REJECTED,
                    reason="invalid_edge_record",
                    evaluator_digest=evaluator_digest,
                )
            )
            continue
        start_node, end_node, raw_distance, raw_points = item
        try:
            distance = float(raw_distance)
            points = tuple(raw_points)
            if not isfinite(distance) or distance < 0.0 or len(points) < 2:
                raise ValueError("invalid_edge_geometry")
            # Validate coordinates before invoking the sampler so malformed
            # geometry cannot be hidden behind an evaluator exception.
            for point in points:
                _point_payload(point)
            current = _edge_evidence(
                risk_sampler=risk_sampler,
                vessel_model=vessel_model,
                scope=active_scope,
                start_node=start_node,
                end_node=end_node,
                edge_distance_km=distance,
                points=points,
                departure_lower=dep_lower,
                departure_upper=dep_upper,
                horizon_hours=horizon,
                evaluator_certified=True,
            )
        except Exception as error:
            current = EnvironmentalEdgeSpeedEvidence(
                start_node=start_node,
                end_node=end_node,
                edge_distance_km=float(raw_distance) if _finite_number(raw_distance) else 0.0,
                departure_lower=dep_lower,
                departure_upper=dep_upper,
                sample_points=(),
                interval_samples=(),
                scope=active_scope,
                coverage_complete=False,
                evaluator_certified=False,
                status=EnvironmentalSpeedEnvelopeStatus.REJECTED,
                reason=f"invalid_edge_geometry:{type(error).__name__}",
                evaluator_digest=evaluator_digest,
            )
        evidence.append(current)
        if current.usable:
            assert current.travel_lower_hours is not None
            lower_rows.append((current.start_node, current.end_node, current.travel_lower_hours))
    covered = len(lower_rows)
    complete = covered == len(raw_edges) and bool(raw_edges)
    status = (
        EnvironmentalSpeedEnvelopeStatus.CERTIFIED
        if complete
        else EnvironmentalSpeedEnvelopeStatus.PARTIAL
        if covered
        else EnvironmentalSpeedEnvelopeStatus.REJECTED
    )
    reason = None if complete else "partial_edge_coverage" if covered else "no_certified_edges"
    proof_digest = canonical_digest(
        {
            "schema_version": ENVIRONMENT_SPEED_ENVELOPE_SCHEMA,
            "method": ENVIRONMENT_SPEED_ENVELOPE_METHOD,
            "scope": active_scope.digest,
            "departure_lower": dep_lower,
            "departure_upper": dep_upper,
            "horizon_hours": horizon,
            "universe_nodes": universe,
            "edge_evidence": tuple(item.digest for item in evidence),
            "edge_lower_hours": tuple(lower_rows),
            "expected_edge_count": len(raw_edges),
            "covered_edge_count": covered,
            "coverage_complete": complete,
            "evaluator_certified": True,
            "evaluator_digest": evaluator_digest,
            "reason": reason,
        }
    )
    return TemporalEnvironmentalSpeedEnvelope(
        scope=active_scope,
        departure_lower=dep_lower,
        departure_upper=dep_upper,
        horizon_hours=horizon,
        universe_nodes=universe,
        edge_evidence=tuple(evidence),
        edge_lower_hours=tuple(lower_rows),
        expected_edge_count=len(raw_edges),
        covered_edge_count=covered,
        coverage_complete=complete,
        evaluator_certified=True,
        proof_digest=proof_digest,
        status=status,
        reason=reason,
        evaluator_digest=evaluator_digest,
    )


def _finite_number(value: Any) -> bool:
    try:
        return isfinite(float(value))
    except (TypeError, ValueError):
        return False


__all__ = [
    "ENVIRONMENT_SPEED_ENVELOPE_METHOD",
    "ENVIRONMENT_SPEED_ENVELOPE_SCHEMA",
    "EnvironmentalEdgeSpeedEvidence",
    "EnvironmentalSpeedEnvelopeStatus",
    "TemporalEnvironmentalSpeedEnvelope",
    "qualify_environmental_speed_envelope",
]
