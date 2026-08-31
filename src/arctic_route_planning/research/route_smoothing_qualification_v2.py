"""Fail-closed qualification for the research-only route-smoothing sidecar v2.

This module binds the multi-span geometry to one existing RiskFrame sampler,
the existing vessel speed model, caller-owned raster corridor evidence and an
explicit synthetic manoeuvring envelope.  It never changes the formal route
and never produces a production qualification.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.cost.vessel import VesselPerformanceModel
from arctic_route_planning.risk.sampler import RiskSampler

from .route_smoothing import (
    CandidateDecision,
    RouteSmoothingPolicy,
    _anchor_indices,
    _canonical_digest,
    _format_utc,
    _path_metric,
    _route_member,
    _time_at_distance,
)
from .route_smoothing_manoeuvring import (
    SYNTHETIC_ONLY,
    SYNTHETIC_UNCALIBRATED,
    SyntheticManoeuvringEnvelope,
)
from .route_smoothing_multispan import KNOT_VECTOR
from .route_smoothing_qualification import (
    _candidate_times,
    _EvaluationFailure,
    _finish_digest,
    _identity_document,
    _identity_mismatch,
    _integrate_path,
    _local_distance_to_geo,
    _risk_stats,
    _route_records,
    _sample_points,
    _speed_values,
)
from .route_smoothing_v2 import (
    POLICY,
    SIDECAR_SCHEMA_VERSION,
    MultiSpanRouteSegment,
    build_multispan_route_smoothing,
)

Coordinate = tuple[float, float]
SpanHull = tuple[Coordinate, ...]
RasterCorridorValidator = Callable[
    [tuple[SpanHull, ...], tuple[Coordinate, ...], tuple[datetime, ...], float],
    Mapping[str, Any] | bool,
]

PRIMARY_CORRIDOR_MARGIN_M = 500.0
CORRIDOR_SENSITIVITY_M = (1_000.0, 2_000.0)
ETA_ABSOLUTE_TOLERANCE_S = 600.0
ETA_RELATIVE_TOLERANCE = 0.02


def _route_id(route: Any) -> str | None:
    value = _route_member(route, "plan_id") or _route_member(route, "route_id")
    return str(value) if value is not None else None


def _authoritative_waypoints(
    points: Sequence[Coordinate], times: Sequence[datetime]
) -> list[dict[str, Any]]:
    return [
        {"lon": point[0], "lat": point[1], "eta": _format_utc(eta)}
        for point, eta in zip(points, times, strict=True)
    ]


def _base_sidecar(
    route: Any,
    *,
    experiment_id: str,
    points: Sequence[Coordinate],
    times: Sequence[datetime],
    identity: Mapping[str, Any],
) -> dict[str, Any]:
    raw_digest = _canonical_digest([list(point) for point in points])
    route_id = _route_id(route)
    route_identity = {
        "route_id": route_id,
        "route_digest": raw_digest,
        "route_digest_scope": "waypoint_coordinates_only",
        "semantic_digest": identity.get("route_semantic_digest", raw_digest),
        "plan_revision": identity.get("plan_revision", _route_member(route, "revision")),
        "adoption_time": identity.get(
            "adoption_time", _route_member(route, "effective_adoption_time")
        ),
    }
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "policy": POLICY,
        "status": "FALLBACK",
        "applied": False,
        "research_only": True,
        "authoritative_semantics_unchanged": True,
        "experiment_id": experiment_id,
        "route_id": route_id,
        "raw_route_digest": raw_digest,
        "route_identity": route_identity,
        "authoritative_route": {
            "route_id": route_id,
            "route_digest": raw_digest,
            "waypoints": _authoritative_waypoints(points, times),
        },
        "input_identity": dict(identity),
        "risk_frame_identity": identity.get("risk_frame_identity"),
        "scenario_id": identity.get("scenario_id"),
        "corridor_id": identity.get("corridor_id"),
        "vessel_profile_id": identity.get("vessel_profile_id"),
        "model_config_digest": identity.get("model_config_digest"),
        "production_qualified": False,
        "calibration_status": SYNTHETIC_UNCALIBRATED,
        "manoeuvring_qualification": SYNTHETIC_ONLY,
        "qualification": {
            "production_qualified": False,
            "calibration_status": SYNTHETIC_UNCALIBRATED,
            "manoeuvring_qualification": SYNTHETIC_ONLY,
        },
        "validation": {
            "mode": "RISK_RASTER_ETA_AND_POINTWISE_SYNTHETIC_MANOEUVRING_RECHECK",
            "risk_rechecked": False,
            "hard_mask_rechecked": False,
            "coverage_complete": False,
            "eta_recomputed": False,
            "speed_checked": False,
            "curvature_checked": False,
            "corridor_containment_checked": False,
            "manoeuvring_checked": False,
            "research_gate_passed": False,
            "resource_evidence_complete": False,
            "production_qualified": False,
            "calibration_status": SYNTHETIC_UNCALIBRATED,
            "manoeuvring_qualification": SYNTHETIC_ONLY,
        },
        "motion_samples": [],
        "curve_samples": [],
        "sample_eta": [],
        "cumulative_distance_m": [],
        "fallback_reason": "not_evaluated",
        "research_eligible": False,
    }


def _fallback(
    sidecar: dict[str, Any],
    reason: str,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sidecar.update(
        {
            "status": "FALLBACK",
            "applied": False,
            "curve_status": "FALLBACK",
            "fallback_reason": reason,
            "research_eligible": False,
            "motion_samples": [],
            "curve_samples": [],
            "sample_eta": [],
            "cumulative_distance_m": [],
            "qualification_failure_evidence": dict(evidence or {}),
        }
    )
    sidecar["validation"].update(
        {
            "risk_rechecked": False,
            "hard_mask_rechecked": False,
            "coverage_complete": False,
            "eta_recomputed": False,
            "speed_checked": False,
            "curvature_checked": False,
            "corridor_containment_checked": False,
            "manoeuvring_checked": False,
            "research_gate_passed": False,
            "resource_evidence_complete": False,
            "production_qualified": False,
        }
    )
    return _finish_digest(sidecar)


def _corridor_evidence(
    validator: RasterCorridorValidator | None,
    hulls: tuple[SpanHull, ...],
    points: tuple[Coordinate, ...],
    times: tuple[datetime, ...],
    expansion_m: float,
) -> dict[str, Any]:
    if validator is None:
        raise _EvaluationFailure("missing_raster_corridor_evidence")
    try:
        value = validator(hulls, points, times, expansion_m)
    except Exception as error:
        raise _EvaluationFailure(
            "raster_corridor_validator_error", {"error": type(error).__name__}
        ) from error
    evidence = dict(value) if isinstance(value, Mapping) else {"accepted": value is True}
    accepted = evidence.get("accepted", evidence.get("complete", False)) is True
    if (
        not accepted
        or evidence.get("complete") is not True
        or evidence.get("raster_resolution_containment_proved") is not True
        or evidence.get("hard_mask_envelope_complete") is not True
    ):
        raise _EvaluationFailure("raster_corridor_evidence_failed", evidence)
    evidence.setdefault("expansion_m", expansion_m)
    evidence.setdefault("method", "caller_supplied_raster_resolution_containment")
    return evidence


def _corridor_observation(
    validator: RasterCorridorValidator,
    hulls: tuple[SpanHull, ...],
    points: tuple[Coordinate, ...],
    times: tuple[datetime, ...],
    expansion_m: float,
) -> dict[str, Any]:
    """Collect a non-gating sensitivity result without converting failure to pass."""

    try:
        value = validator(hulls, points, times, expansion_m)
    except Exception as error:
        return {
            "accepted": False,
            "complete": False,
            "expansion_m": expansion_m,
            "error": type(error).__name__,
        }
    evidence = dict(value) if isinstance(value, Mapping) else {"accepted": value is True}
    evidence.setdefault("expansion_m", expansion_m)
    evidence["gating"] = False
    return evidence


def _courses(points: Sequence[Coordinate]) -> tuple[float, ...]:
    values: list[float] = []
    for first, second in pairwise(points):
        lon1, lat1 = map(math.radians, first)
        lon2, lat2 = map(math.radians, second)
        delta_lon = lon2 - lon1
        x = math.sin(delta_lon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(
            lat2
        ) * math.cos(delta_lon)
        bearing = math.degrees(math.atan2(x, y)) % 360.0
        values.append(bearing)
    values.append(values[-1])
    return tuple(values)


def build_qualified_route_smoothing_sidecar_v2(
    route: Any,
    *,
    experiment_id: str,
    risk_sampler: RiskSampler,
    vessel_model: VesselPerformanceModel,
    corridor_validator: RasterCorridorValidator | None,
    policy: RouteSmoothingPolicy | None = None,
    manoeuvring_envelope: SyntheticManoeuvringEnvelope | None = None,
    input_identity: Mapping[str, Any] | None = None,
    eta_max_iterations: int = 8,
    eta_convergence_tolerance_s: float = 0.5,
    risk_tolerance: float = 1.0e-9,
    maximum_sample_count: int = 10_000,
    primary_corridor_margin_m: float = PRIMARY_CORRIDOR_MARGIN_M,
    corridor_sensitivity_m: Sequence[float] = CORRIDOR_SENSITIVITY_M,
) -> dict[str, Any]:
    """Build v2 evidence or atomically return a raw-route fallback sidecar."""

    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("experiment_id must be a non-empty string")
    if not isinstance(risk_sampler, RiskSampler):
        raise TypeError("risk_sampler must be a RiskSampler")
    if not isinstance(vessel_model, VesselPerformanceModel):
        raise TypeError("vessel_model must be a VesselPerformanceModel")
    if eta_max_iterations < 1 or eta_convergence_tolerance_s <= 0:
        raise ValueError("ETA iteration policy must be positive")
    if risk_tolerance < 0 or maximum_sample_count < 2:
        raise ValueError("risk tolerance and sample limit are invalid")
    margins = (
        float(primary_corridor_margin_m),
        *(float(value) for value in corridor_sensitivity_m),
    )
    if any(not math.isfinite(value) or value < 0 for value in margins):
        raise ValueError("corridor margins must be finite and non-negative")
    envelope = manoeuvring_envelope or SyntheticManoeuvringEnvelope.conservative()
    if not isinstance(envelope, SyntheticManoeuvringEnvelope):
        raise TypeError("manoeuvring_envelope must be SyntheticManoeuvringEnvelope")

    try:
        points, raw_times = _route_records(route)
    except _EvaluationFailure as failure:
        empty = _base_sidecar(
            route,
            experiment_id=experiment_id,
            points=(),
            times=(),
            identity={},
        )
        return _fallback(empty, failure.reason, failure.evidence)
    identity = dict(input_identity or {})
    identity.setdefault("risk_frame_identity", _identity_document(risk_sampler))
    identity.setdefault("scenario_id", risk_sampler.identity.scenario_id)
    identity.setdefault("corridor_id", risk_sampler.identity.corridor_id)
    identity.setdefault("vessel_profile_id", risk_sampler.identity.vessel_profile_id)
    identity.setdefault("model_config_digest", risk_sampler.identity.model_config_digest)
    sidecar = _base_sidecar(
        route,
        experiment_id=experiment_id,
        points=points,
        times=raw_times,
        identity=identity,
    )
    mismatch = _identity_mismatch(identity, risk_sampler, route)
    if mismatch is not None:
        return _fallback(sidecar, "identity_mismatch", mismatch)

    frame, _local_raw, raw_distances = _path_metric(points)

    def candidate_validator(
        segment: MultiSpanRouteSegment,
        local_points: tuple[Coordinate, ...],
    ) -> CandidateDecision:
        try:
            candidate_points = _local_distance_to_geo(frame, segment.samples)
            candidate_times = _candidate_times(
                segment, local_points, raw_times, raw_distances, frame
            )
            corridor = _corridor_evidence(
                corridor_validator,
                segment.span_convex_hulls_m,
                candidate_points,
                candidate_times,
                margins[0],
            )
            risks = _sample_points(risk_sampler, candidate_points, candidate_times)
            if any(value.hard_mask for value in risks):
                return CandidateDecision(False, "hard_mask")
            speeds_kmh = _speed_values(vessel_model, risks)
            manoeuvring = envelope.evaluate(
                segment.curvatures_m_inv,
                tuple(value / 3.6 for value in speeds_kmh),
            )
            if not manoeuvring.accepted:
                return CandidateDecision(
                    False,
                    "pointwise_manoeuvring_limit_exceeded",
                    manoeuvring.to_dict(),
                )
        except _EvaluationFailure as failure:
            return CandidateDecision(False, failure.reason, failure.evidence)
        return CandidateDecision(
            True,
            evidence={
                "corridor_evidence": corridor,
                "risk_sample_count": len(risks),
                "hard_mask_violations": 0,
                "provisional_manoeuvring_evidence": manoeuvring.to_dict(),
                "eta_method": "raw_eta_candidate_clock_for_radius_screening",
            },
        )

    route_points = [
        {"lon": point[0], "lat": point[1]} for point in points
    ]
    geometry = build_multispan_route_smoothing(
        route_points,
        policy=policy,
        candidate_validator=candidate_validator,
    )
    sidecar["geometry"] = geometry.to_dict()
    sidecar["curve_digest"] = geometry.curve_digest
    sidecar["degree"] = 3
    sidecar["knot_vector"] = list(KNOT_VECTOR)
    sidecar["span_count_per_corner"] = 4
    if not geometry.applied:
        reason = geometry.fallback_reason or "geometry_fallback"
        if reason == "all_curves_rejected" and geometry.rejected_corners:
            candidate_reason = geometry.rejected_corners[-1].get("reason")
            if isinstance(candidate_reason, str) and candidate_reason:
                reason = candidate_reason
        return _fallback(sidecar, reason)
    if len(geometry.points) > maximum_sample_count:
        return _fallback(
            sidecar, "sample_count_limit", {"sample_count": len(geometry.points)}
        )

    anchors = _anchor_indices(points, geometry.points)
    if anchors is None:
        return _fallback(sidecar, "non_monotonic_curve_anchors")
    _, _, curve_distances = _path_metric(geometry.points)
    anchor_distances = tuple(curve_distances[index] for index in anchors)
    provisional_times = tuple(
        _time_at_distance(distance, anchor_distances, raw_times)
        for distance in curve_distances
    )
    hulls = tuple(
        hull for segment in geometry.segments for hull in segment.span_convex_hulls_m
    )
    try:
        curve_times, curve_risks, curve_speeds, iterations = _integrate_path(
            risk_sampler,
            vessel_model,
            geometry.points,
            provisional_times,
            max_iterations=eta_max_iterations,
            convergence_tolerance_s=eta_convergence_tolerance_s,
        )
        raw_final_times, raw_risks, raw_speeds, raw_iterations = _integrate_path(
            risk_sampler,
            vessel_model,
            points,
            raw_times,
            max_iterations=eta_max_iterations,
            convergence_tolerance_s=eta_convergence_tolerance_s,
        )
        corridor_primary = _corridor_evidence(
            corridor_validator,
            hulls,
            geometry.points,
            curve_times,
            margins[0],
        )
        corridor_sensitivity = [
            _corridor_observation(
                corridor_validator, hulls, geometry.points, curve_times, margin
            )
            for margin in margins[1:]
        ]
        if any(value.hard_mask for value in curve_risks):
            raise _EvaluationFailure(
                "hard_mask",
                {"violations": sum(value.hard_mask for value in curve_risks)},
            )
        curve_risk = _risk_stats(curve_risks, curve_times)
        raw_risk = _risk_stats(raw_risks, raw_final_times)
        for field in ("maximum_risk", "integrated_risk_hours"):
            if curve_risk[field] > raw_risk[field] + risk_tolerance:
                raise _EvaluationFailure(
                    "risk_increased",
                    {
                        "field": field,
                        "curve": curve_risk[field],
                        "raw": raw_risk[field],
                        "tolerance": risk_tolerance,
                    },
                )
        raw_duration_s = (raw_times[-1] - raw_times[0]).total_seconds()
        eta_tolerance_s = max(
            ETA_ABSOLUTE_TOLERANCE_S, ETA_RELATIVE_TOLERANCE * raw_duration_s
        )
        # The qualification isolates smoothing impact.  Both paths must use
        # the same RiskFrame and vessel model; comparing only the curve to the
        # published ETA would incorrectly charge pre-existing model drift to
        # the smoothing candidate.
        eta_delta_s = abs((curve_times[-1] - raw_final_times[-1]).total_seconds())
        if eta_delta_s > eta_tolerance_s:
            raise _EvaluationFailure(
                "eta_delta_exceeded",
                {"delta_seconds": eta_delta_s, "tolerance_seconds": eta_tolerance_s},
            )
        final_manoeuvring = envelope.evaluate(
            geometry.curvatures_m_inv,
            tuple(value / 3.6 for value in curve_speeds),
        )
        if not final_manoeuvring.accepted:
            raise _EvaluationFailure(
                "final_pointwise_manoeuvring_failed", final_manoeuvring.to_dict()
            )
    except _EvaluationFailure as failure:
        return _fallback(sidecar, failure.reason, failure.evidence)

    courses = _courses(geometry.points)
    motion_samples = [
        {
            "lon": point[0],
            "lat": point[1],
            "eta": _format_utc(eta),
            "course_degrees": course,
            "speed_knots": speed / 1.852,
        }
        for point, eta, course, speed in zip(
            geometry.points, curve_times, courses, curve_speeds, strict=True
        )
    ]
    geometry_motion_digest = _canonical_digest(
        {
            "curve_digest": geometry.curve_digest,
            "motion_samples": motion_samples,
        }
    )
    sidecar.update(
        {
            "status": "ACCEPTED",
            "applied": True,
            "curve_status": "ACCEPTED",
            "fallback_reason": None,
            "research_eligible": True,
            "motion_samples": motion_samples,
            "curve_samples": [
                {"lon": point[0], "lat": point[1]} for point in geometry.points
            ],
            "sample_eta": [value["eta"] for value in motion_samples],
            "cumulative_distance_m": list(curve_distances),
            "same_geometry_motion_digest": geometry_motion_digest,
            "corridor_evidence": {
                "primary": corridor_primary,
                "sensitivity": corridor_sensitivity,
                "scope": "RASTER_RESOLUTION_CONTAINMENT_PASS",
            },
            "risk_evidence": {
                "complete": True,
                "curve": curve_risk,
                "raw_baseline": raw_risk,
                "maximum_risk_delta": (
                    curve_risk["maximum_risk"] - raw_risk["maximum_risk"]
                ),
                "integrated_risk_hours_delta": (
                    curve_risk["integrated_risk_hours"]
                    - raw_risk["integrated_risk_hours"]
                ),
                "tolerance": risk_tolerance,
            },
            "hard_mask_evidence": {
                "complete": True,
                "curve_violations": 0,
                "sample_count": len(curve_risks),
            },
            "coverage_evidence": {
                "complete": True,
                "risk_frame_identity": _identity_document(risk_sampler),
                "curve_sample_count": len(curve_risks),
                "raster_scope": "RASTER_RESOLUTION_CONTAINMENT_PASS",
            },
            "eta_evidence": {
                "complete": True,
                "recomputed": True,
                "strictly_increasing": True,
                "curve_end_eta": _format_utc(curve_times[-1]),
                "raw_recomputed_end_eta": _format_utc(raw_final_times[-1]),
                "formal_end_eta": _format_utc(raw_times[-1]),
                "curve_vs_raw_recomputed_delta_seconds": (
                    curve_times[-1] - raw_final_times[-1]
                ).total_seconds(),
                "raw_recomputed_vs_formal_delta_seconds": (
                    raw_final_times[-1] - raw_times[-1]
                ).total_seconds(),
                "tolerance_seconds": eta_tolerance_s,
                "iterations": iterations,
                "raw_iterations": raw_iterations,
            },
            "speed_evidence": {
                "complete": True,
                "model_version": vessel_model.model_version,
                "minimum_speed_knots": min(curve_speeds) / 1.852,
                "maximum_speed_knots": max(curve_speeds) / 1.852,
                "raw_minimum_speed_knots": min(raw_speeds) / 1.852,
                "raw_maximum_speed_knots": max(raw_speeds) / 1.852,
            },
            "manoeuvring_evidence": final_manoeuvring.to_dict(),
            "resource_evidence": {
                "complete": False,
                "status": "NOT_RUN",
                "production_qualified": False,
            },
        }
    )
    sidecar["validation"].update(
        {
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
            "curvature_checked": True,
            "corridor_containment_checked": True,
            "manoeuvring_checked": True,
            "research_gate_passed": True,
            "resource_evidence_complete": False,
            "production_qualified": False,
        }
    )
    sidecar["same_geometry_motion_evidence"] = {
        "same_geometry_motion_digest": geometry_motion_digest,
        "sample_count": len(motion_samples),
    }
    sidecar["determinism_digest"] = _canonical_digest(
        {
            "geometry_motion_digest": geometry_motion_digest,
            "risk_evidence": sidecar["risk_evidence"],
            "eta_evidence": sidecar["eta_evidence"],
            "manoeuvring_evidence": sidecar["manoeuvring_evidence"],
        }
    )
    return _finish_digest(sidecar)


__all__ = [
    "CORRIDOR_SENSITIVITY_M",
    "PRIMARY_CORRIDOR_MARGIN_M",
    "RasterCorridorValidator",
    "build_qualified_route_smoothing_sidecar_v2",
]
