"""Produce one formal four-layer route-motion set from a completed plan set."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.contracts.layered import FourLayerRoutePlanSet, RoutePlanV3
from arctic_route_planning.contracts.route_motion import (
    CONTINUOUS_RASTER_MODEL_SCOPE,
    ROUTE_MOTION_INTERPOLATION,
    ROUTE_MOTION_SET_SCHEMA_VERSION,
    MotionSample,
    RouteMotionMode,
    RouteMotionQualification,
    RouteMotionRecord,
    RouteMotionSet,
    WaypointMotionAnchor,
)
from arctic_route_planning.publishing.route_motion_serialization import (
    canonical_sha256,
    route_motion_set_semantic_digest,
)
from arctic_route_planning.risk.sampler import RiskSampler, SampledRisk

from .anchoring import find_anchor_indices, path_metric, time_at_distance
from .geometry import (
    MultiSpanRouteResult,
    RouteSmoothingPolicy,
    build_multispan_route_smoothing,
)
from .profile import KNOT_TO_MPS, EngineeringRouteMotionProfile

Coordinate = tuple[float, float]
CorridorValidator = Callable[
    [tuple[tuple[Coordinate, ...], ...], tuple[Coordinate, ...], tuple[datetime, ...], float],
    Mapping[str, Any] | bool,
]


def build_route_motion_set(
    plan_set: FourLayerRoutePlanSet,
    *,
    risk_window_id: str,
    risk_window_digest: str,
    vessel_profile_digest: str,
    producer_digest: str,
    profile: EngineeringRouteMotionProfile | None = None,
    risk_sampler: RiskSampler | None = None,
    corridor_validator: CorridorValidator | None = None,
    position_error_m: float = 0.0,
    transform_error_m: float = 0.0,
    chord_error_m: float = 0.0,
    generated_at: datetime | None = None,
    policy: RouteSmoothingPolicy | None = None,
) -> RouteMotionSet:
    """Build four recommended records without changing ``plan_set``.

    Missing or failed qualification produces an explicit raw passthrough for
    that layer.  The set itself remains complete and atomic, allowing old or
    constrained deployments to preserve the authoritative route.
    """

    chosen = profile or EngineeringRouteMotionProfile()
    if plan_set.vessel_profile_id != chosen.vessel_profile_id:
        raise ValueError("motion profile does not match plan-set vessel_profile_id")
    _digest(risk_window_digest, "risk_window_digest")
    _digest(vessel_profile_digest, "vessel_profile_digest")
    _digest(producer_digest, "producer_digest")
    if risk_sampler is not None:
        _validate_sampler_identity(plan_set, risk_sampler)
    records = tuple(
        _build_record(
            bundle.recommended,
            profile=chosen,
            risk_sampler=risk_sampler,
            corridor_validator=corridor_validator,
            corridor_buffer_m=chosen.corridor_buffer_m(
                position_error_m=position_error_m,
                transform_error_m=transform_error_m,
                chord_error_m=chord_error_m,
            ),
            policy=policy,
        )
        for bundle in plan_set.layers
    )
    provisional = RouteMotionSet(
        schema_version=ROUTE_MOTION_SET_SCHEMA_VERSION,
        motion_set_id="route-motion-set-sha256-" + "0" * 64,
        layer_set_id=plan_set.layer_set_id,
        run_id=plan_set.run_id,
        scenario_id=plan_set.scenario_id,
        corridor_id=plan_set.corridor_id,
        generation_id=plan_set.generation_id,
        input_revision=plan_set.input_revision,
        risk_window_id=risk_window_id,
        risk_window_digest=risk_window_digest,
        vessel_profile_id=plan_set.vessel_profile_id,
        vessel_profile_version=chosen.vessel_profile_version,
        vessel_profile_digest=vessel_profile_digest,
        motion_profile_id=chosen.profile_id,
        motion_profile_digest=chosen.digest,
        config_digest=plan_set.config_digest,
        model_config_digest=plan_set.model_config_digest,
        planner_config_digest=plan_set.planner_config_digest,
        producer_digest=producer_digest,
        generated_at=(generated_at or plan_set.generated_at),
        records=records,
    )
    return replace(
        provisional,
        motion_set_id="route-motion-set-sha256-"
        + route_motion_set_semantic_digest(provisional),
    )


def _build_record(
    plan: RoutePlanV3,
    *,
    profile: EngineeringRouteMotionProfile,
    risk_sampler: RiskSampler | None,
    corridor_validator: CorridorValidator | None,
    corridor_buffer_m: float,
    policy: RouteSmoothingPolicy | None,
) -> RouteMotionRecord:
    raw_points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    raw_digest = canonical_sha256(
        [
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": _iso(waypoint.eta),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            }
            for waypoint in plan.waypoints
        ]
    )
    if risk_sampler is None:
        return _raw_record(plan, raw_digest, "missing_risk_window")
    if corridor_validator is None:
        return _raw_record(plan, raw_digest, "missing_continuous_corridor_evidence")

    geometry = build_multispan_route_smoothing(
        [{"lon": lon, "lat": lat} for lon, lat in raw_points],
        policy=policy,
    )
    if not geometry.applied:
        return _raw_record(
            plan,
            raw_digest,
            geometry.fallback_reason or "no_qualified_curve",
        )
    try:
        samples, anchors, curve_distances = _anchored_motion(plan, geometry)
        corridor = _corridor(
            corridor_validator,
            geometry,
            samples,
            corridor_buffer_m,
        )
        curve_risks = _sample_risk(risk_sampler, samples)
        raw_samples = _raw_motion_samples(plan)
        raw_risks = _sample_risk(risk_sampler, raw_samples)
        if any(value.hard_mask for value in curve_risks):
            raise _QualificationFailure("hard_mask")
        if _maximum_risk(curve_risks) > _maximum_risk(raw_risks) + 1.0e-9:
            raise _QualificationFailure("maximum_risk_increased")
        if _integrated_risk(curve_risks) > _integrated_risk(raw_risks) + 1.0e-9:
            raise _QualificationFailure("integrated_risk_increased")
        speed_details = _validate_speed_and_motion(profile, geometry, samples)
    except _QualificationFailure as failure:
        return _raw_record(plan, raw_digest, failure.reason)
    evidence = {
        "risk": {
            "curve_maximum": _maximum_risk(curve_risks),
            "raw_maximum": _maximum_risk(raw_risks),
            "curve_integrated_risk_hours": _integrated_risk(curve_risks),
            "raw_integrated_risk_hours": _integrated_risk(raw_risks),
            "sample_count": len(curve_risks),
        },
        "corridor": corridor,
        "motion": speed_details,
        "eta_anchor_count": len(anchors),
        "curve_length_m": curve_distances[-1],
        "real_vessel_calibrated": False,
    }
    curve_payload = [[sample.longitude, sample.latitude] for sample in samples]
    motion_payload = [_sample_dict(sample) for sample in samples]
    return RouteMotionRecord(
        planning_layer=plan.planning_layer,
        plan_id=plan.plan_id,
        raw_route_digest=raw_digest,
        mode=RouteMotionMode.CURVE,
        fallback_reason=None,
        curve_digest=canonical_sha256(curve_payload),
        motion_digest=canonical_sha256(motion_payload),
        interpolation=ROUTE_MOTION_INTERPOLATION,
        waypoint_anchors=anchors,
        motion_samples=samples,
        qualification=RouteMotionQualification(
            result="QUALIFIED_ENGINEERING_REFERENCE",
            risk_rechecked=True,
            hard_mask_rechecked=True,
            coverage_complete=True,
            eta_anchors_preserved=True,
            speed_checked=True,
            curvature_checked=True,
            corridor_checked=True,
            manoeuvring_checked=True,
            corridor_proof_scope=CONTINUOUS_RASTER_MODEL_SCOPE,
            evidence_kind=profile.evidence_kind,
            real_vessel_calibrated=False,
            details_digest=canonical_sha256(evidence),
        ),
    )


def _anchored_motion(
    plan: RoutePlanV3,
    geometry: MultiSpanRouteResult,
) -> tuple[tuple[MotionSample, ...], tuple[WaypointMotionAnchor, ...], tuple[float, ...]]:
    raw_points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    raw_times = tuple(waypoint.eta for waypoint in plan.waypoints)
    anchor_indices = find_anchor_indices(raw_points, geometry.points)
    if anchor_indices is None:
        raise _QualificationFailure("non_monotonic_curve_anchors")
    # An internal raw waypoint is a temporal/arc-length anchor, not a point
    # that belongs to the smoothed geometry.  Replacing the nearest curve
    # sample with the raw corner creates a path of ``curve -> vertex ->
    # curve`` and can make the vessel visibly reverse at a turn.  The curve's
    # internal sample is therefore authoritative for geometry.
    # Preserve only the exact route endpoints (the local-frame inverse can
    # otherwise introduce ~1e-14 degree drift); internal anchors keep the
    # producer geometry and only receive the authoritative ETA below.
    authoritative_points = list(geometry.points)
    authoritative_points[0] = raw_points[0]
    authoritative_points[-1] = raw_points[-1]
    authoritative_points = tuple(authoritative_points)
    _, _, curve_distances = path_metric(authoritative_points)
    anchor_distances = tuple(curve_distances[index] for index in anchor_indices)
    times = tuple(
        time_at_distance(distance, anchor_distances, raw_times)
        for distance in curve_distances
    )
    if any(current <= previous for previous, current in pairwise(times)):
        raise _QualificationFailure("non_monotonic_anchored_eta")
    courses = _courses(authoritative_points)
    speeds = _interval_speeds(curve_distances, times)
    samples = tuple(
        MotionSample(point[0], point[1], eta, course, speed)
        for point, eta, course, speed in zip(
            authoritative_points, times, courses, speeds, strict=True
        )
    )
    anchors = tuple(
        WaypointMotionAnchor(
            waypoint_index=index,
            eta=raw_times[index],
            motion_sample_index=sample_index,
            arc_length_m=curve_distances[sample_index],
        )
        for index, sample_index in enumerate(anchor_indices)
    )
    return samples, anchors, curve_distances


def _raw_record(plan: RoutePlanV3, raw_digest: str, reason: str) -> RouteMotionRecord:
    samples = _raw_motion_samples(plan)
    _, _, distances = path_metric(
        tuple((sample.longitude, sample.latitude) for sample in samples)
    )
    anchors = tuple(
        WaypointMotionAnchor(index, sample.eta, index, distances[index])
        for index, sample in enumerate(samples)
    )
    curve_digest = canonical_sha256(
        [[sample.longitude, sample.latitude] for sample in samples]
    )
    motion_digest = canonical_sha256([_sample_dict(sample) for sample in samples])
    return RouteMotionRecord(
        planning_layer=plan.planning_layer,
        plan_id=plan.plan_id,
        raw_route_digest=raw_digest,
        mode=RouteMotionMode.RAW_PASSTHROUGH,
        fallback_reason=reason,
        curve_digest=curve_digest,
        motion_digest=motion_digest,
        interpolation=ROUTE_MOTION_INTERPOLATION,
        waypoint_anchors=anchors,
        motion_samples=samples,
        qualification=RouteMotionQualification(
            result="RAW_FALLBACK",
            risk_rechecked=False,
            hard_mask_rechecked=False,
            coverage_complete=False,
            eta_anchors_preserved=True,
            speed_checked=False,
            curvature_checked=False,
            corridor_checked=False,
            manoeuvring_checked=False,
            corridor_proof_scope="NOT_PROVED",
            evidence_kind="FORMULA_DERIVED_ENGINEERING_REFERENCE",
            real_vessel_calibrated=False,
            details_digest=canonical_sha256({"fallback_reason": reason}),
        ),
    )


def _raw_motion_samples(plan: RoutePlanV3) -> tuple[MotionSample, ...]:
    points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    courses = _courses(points)
    return tuple(
        MotionSample(
            waypoint.longitude,
            waypoint.latitude,
            waypoint.eta,
            course,
            waypoint.recommended_speed_mps / KNOT_TO_MPS,
        )
        for waypoint, course in zip(plan.waypoints, courses, strict=True)
    )


def _corridor(
    validator: CorridorValidator,
    geometry: MultiSpanRouteResult,
    samples: Sequence[MotionSample],
    expansion_m: float,
) -> dict[str, Any]:
    spline_hulls = tuple(
        hull
        for segment in geometry.segments
        for hull in segment.span_convex_hulls_m
    )
    _, local_samples, _ = path_metric(
        tuple((sample.longitude, sample.latitude) for sample in samples)
    )
    line_hulls = tuple((first, second) for first, second in pairwise(local_samples))
    hulls = spline_hulls + line_hulls
    try:
        value = validator(
            hulls,
            tuple((sample.longitude, sample.latitude) for sample in samples),
            tuple(sample.eta for sample in samples),
            expansion_m,
        )
    except Exception as exc:
        raise _QualificationFailure("corridor_validator_error") from exc
    evidence = dict(value) if isinstance(value, Mapping) else {"accepted": value is True}
    evidence.setdefault("spline_span_hull_count", len(spline_hulls))
    evidence.setdefault("display_line_hull_count", len(line_hulls))
    if not (
        evidence.get("accepted") is True
        and evidence.get("complete") is True
        and evidence.get("coverage_complete") is True
        and evidence.get("hard_mask_envelope_complete") is True
        and evidence.get("continuous_containment_proved") is True
        and evidence.get("continuous_containment_scope") == CONTINUOUS_RASTER_MODEL_SCOPE
    ):
        raise _QualificationFailure("continuous_corridor_not_proved")
    return evidence


def _sample_risk(
    sampler: RiskSampler,
    samples: Sequence[MotionSample],
) -> tuple[SampledRisk, ...]:
    try:
        return tuple(
            sampler.sample(sample.eta, sample.longitude, sample.latitude)
            for sample in samples
        )
    except Exception as exc:
        raise _QualificationFailure("risk_or_coverage_sampling_failed") from exc


def _validate_speed_and_motion(
    profile: EngineeringRouteMotionProfile,
    geometry: MultiSpanRouteResult,
    samples: Sequence[MotionSample],
) -> dict[str, Any]:
    speeds = tuple(sample.speed_knots for sample in samples)
    if min(speeds) < profile.minimum_steerage_speed_knots - 1.0e-9:
        raise _QualificationFailure("below_minimum_steerage_speed")
    if max(speeds) > profile.maximum_speed_knots + 1.0e-9:
        raise _QualificationFailure("maximum_speed_exceeded")
    yaw_rates = []
    lateral_accelerations = []
    radii = []
    for speed_knots, curvature in zip(speeds, geometry.curvatures_m_inv, strict=True):
        speed_m_s = speed_knots * KNOT_TO_MPS
        curvature = abs(curvature)
        radius = math.inf if curvature == 0.0 else 1.0 / curvature
        if radius + 1.0e-9 < profile.minimum_radius_m(speed_knots):
            raise _QualificationFailure("minimum_radius_exceeded")
        yaw_rate = math.degrees(speed_m_s * curvature)
        lateral = speed_m_s**2 * curvature
        if yaw_rate > profile.maximum_yaw_rate_deg_s + 1.0e-12:
            raise _QualificationFailure("maximum_yaw_rate_exceeded")
        if lateral > profile.maximum_lateral_acceleration_m_s2 + 1.0e-12:
            raise _QualificationFailure("lateral_acceleration_exceeded")
        radii.append(radius)
        yaw_rates.append(yaw_rate)
        lateral_accelerations.append(lateral)
    finite_radii = [value for value in radii if math.isfinite(value)]
    return {
        "minimum_speed_knots": min(speeds),
        "maximum_speed_knots": max(speeds),
        "minimum_radius_m": min(finite_radii) if finite_radii else None,
        "maximum_yaw_rate_deg_s": max(yaw_rates),
        "maximum_lateral_acceleration_m_s2": max(lateral_accelerations),
        "profile_digest": profile.digest,
    }


def _validate_sampler_identity(plan_set: FourLayerRoutePlanSet, sampler: RiskSampler) -> None:
    identity = sampler.identity
    for name in (
        "run_id", "scenario_id", "corridor_id", "vessel_profile_id",
        "model_config_digest", "generation_id",
    ):
        if getattr(identity, name) != getattr(plan_set, name):
            raise ValueError(f"RiskSampler identity differs from plan set: {name}")


def _courses(points: Sequence[Coordinate]) -> tuple[float, ...]:
    values = []
    for first, second in pairwise(points):
        lon1, lat1 = map(math.radians, first)
        lon2, lat2 = map(math.radians, second)
        delta_lon = lon2 - lon1
        x = math.sin(delta_lon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
        values.append(math.degrees(math.atan2(x, y)) % 360.0)
    values.append(values[-1])
    return tuple(values)


def _interval_speeds(
    distances_m: Sequence[float],
    times: Sequence[datetime],
) -> tuple[float, ...]:
    speeds = []
    for index in range(len(distances_m) - 1):
        seconds = (times[index + 1] - times[index]).total_seconds()
        distance = distances_m[index + 1] - distances_m[index]
        if seconds <= 0.0 or distance <= 0.0:
            raise _QualificationFailure("invalid_motion_interval")
        speeds.append(distance / seconds / KNOT_TO_MPS)
    speeds.append(speeds[-1])
    return tuple(speeds)


def _maximum_risk(values: Sequence[SampledRisk]) -> float:
    return max(value.risk_score for value in values)


def _integrated_risk(values: Sequence[SampledRisk]) -> float:
    return sum(
        (first.risk_score + second.risk_score) / 2.0
        * (second.sampled_at - first.sampled_at).total_seconds()
        / 3600.0
        for first, second in pairwise(values)
    )


def _sample_dict(sample: MotionSample) -> dict[str, Any]:
    return {
        "lon": sample.longitude,
        "lat": sample.latitude,
        "eta": _iso(sample.eta),
        "course_degrees": sample.course_degrees,
        "speed_knots": sample.speed_knots,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


class _QualificationFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


__all__ = ["CorridorValidator", "build_route_motion_set"]
