"""Research-only RiskFrame and vessel qualification for route smoothing.

The formal planner and its route contracts remain untouched.  This module is
an opt-in post-processor for synthetic or shadow experiments.  It binds the
geometry sidecar to one existing :class:`RiskSampler`, applies the existing C
vessel-speed model, and fails closed when a corridor, risk, coverage, ETA, or
speed decision cannot be demonstrated.

The corridor callback is deliberately caller-owned.  A RiskFrame can prove
that sampled points are in its grid and can expose hard-mask contributors, but
it cannot by itself prove containment in an arbitrary non-convex safe corridor
between samples.  Callers therefore have to provide that evidence explicitly
for an ``ACCEPTED`` qualified sidecar.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from arctic_route_planning.cost.vessel import (
    UnnavigableSpeedError,
    VesselPerformanceModel,
)
from arctic_route_planning.risk.errors import (
    RiskCoverageError,
    RiskOutOfBoundsError,
    RiskSamplingError,
)
from arctic_route_planning.risk.sampler import RiskSampler, SampledRisk

from .route_smoothing import (
    CandidateDecision,
    CurveSegment,
    RouteSmoothingPolicy,
    _anchor_indices,
    _canonical_digest,
    _format_utc,
    _Frame,
    _parse_utc,
    _path_metric,
    _route_member,
    _route_waypoint_record,
    _time_at_distance,
    build_route_smoothing_sidecar,
)

Coordinate = tuple[float, float]
CorridorValidator = Callable[
    [tuple[Coordinate, ...], tuple[datetime, ...]],
    Mapping[str, Any] | bool,
]


class RouteSmoothingQualificationError(ValueError):
    """A candidate cannot be used as a qualified research motion path."""


class _EvaluationFailure(RouteSmoothingQualificationError):
    def __init__(self, reason: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = dict(evidence or {})


def _identity_document(sampler: RiskSampler) -> dict[str, Any]:
    identity = sampler.identity
    return {
        "run_id": identity.run_id,
        "scenario_id": identity.scenario_id,
        "corridor_id": identity.corridor_id,
        "vessel_profile_id": identity.vessel_profile_id,
        "config_digest": identity.config_digest,
        "model_config_digest": identity.model_config_digest,
        "provenance": identity.provenance.value,
        "generation_id": identity.generation_id,
        "model_version": identity.model_version,
        "grid_id": identity.grid_id,
        "coordinate_digest": identity.coordinate_digest,
        "risk_window_start": _format_utc(sampler.start_time),
        "risk_window_end": _format_utc(sampler.end_time),
        "frame_count": len(sampler.frames),
    }


def _route_records(route: Any) -> tuple[tuple[Coordinate, ...], tuple[datetime, ...]]:
    values = _route_member(route, "waypoints")
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise _EvaluationFailure("invalid_route_waypoints")
    records = tuple(_route_waypoint_record(value) for value in values)
    if any(record is None for record in records):
        raise _EvaluationFailure("invalid_route_waypoint_coordinate_or_eta")
    typed = tuple(record for record in records if record is not None)
    points = tuple(record[0] for record in typed)
    times = tuple(record[1] for record in typed)
    if len(points) < 2 or any(current <= previous for previous, current in pairwise(times)):
        raise _EvaluationFailure("invalid_route_eta_sequence")
    if any(first == second for first, second in pairwise(points)):
        raise _EvaluationFailure("duplicate_point")
    return points, times


def _local_distance_to_geo(frame: _Frame, points: Sequence[Coordinate]) -> tuple[Coordinate, ...]:
    return tuple(frame.to_geo(point) for point in points)


def _candidate_times(
    segment: CurveSegment,
    local_points: Sequence[Coordinate],
    raw_times: Sequence[datetime],
    raw_distances: Sequence[float],
    frame: _Frame,
) -> tuple[datetime, ...]:
    entry_distance = raw_distances[segment.corner_index] - segment.trim_m
    anchor_distances = tuple(raw_distances[index] for index in range(len(raw_distances)))
    curve_distances = [0.0]
    for previous, current in pairwise(segment.samples):
        distance = math.hypot(current[0] - previous[0], current[1] - previous[1])
        if not math.isfinite(distance) or distance <= 1e-9:
            raise _EvaluationFailure("zero_length_curve_sample")
        curve_distances.append(curve_distances[-1] + distance)
    del frame
    # The entry and exit are inside the two raw legs.  The raw ETA path is a
    # deterministic provisional clock for candidate rejection; the final
    # whole-route clock is rebuilt by _integrate_path below.
    entry_time = _time_at_distance(entry_distance, anchor_distances, raw_times)
    exit_time = _time_at_distance(entry_distance + curve_distances[-1], anchor_distances, raw_times)
    if exit_time <= entry_time:
        raise _EvaluationFailure("candidate_eta_not_increasing")
    return tuple(
        entry_time + (exit_time - entry_time) * (distance / curve_distances[-1])
        for distance in curve_distances
    )


def _corridor_evidence(
    validator: CorridorValidator | None,
    points: tuple[Coordinate, ...],
    times: tuple[datetime, ...],
) -> dict[str, Any]:
    if validator is None:
        raise _EvaluationFailure(
            "missing_corridor_evidence",
            {"status": "INCOMPLETE", "complete": False, "validator_present": False},
        )
    try:
        value = validator(points, times)
    except Exception as error:
        raise _EvaluationFailure(
            "corridor_validator_error",
            {"status": "INCOMPLETE", "complete": False, "error": type(error).__name__},
        ) from error
    if isinstance(value, Mapping):
        evidence = dict(value)
        accepted = evidence.get("accepted", evidence.get("complete", False)) is True
    else:
        evidence = {"accepted": value is True}
        accepted = value is True
    evidence.setdefault("sample_count", len(points))
    evidence.setdefault("complete", accepted)
    evidence.setdefault("method", "caller_supplied_local_corridor_validator")
    if not accepted or evidence.get("complete") is not True:
        raise _EvaluationFailure("corridor_evidence_failed", evidence)
    if evidence.get("continuous_containment_proved") is not True:
        raise _EvaluationFailure("continuous_containment_unproved", evidence)
    if evidence.get("hard_mask_envelope_complete") is not True:
        raise _EvaluationFailure("hard_mask_envelope_unproved", evidence)
    return evidence


def _identity_mismatch(
    identity: Mapping[str, Any],
    sampler: RiskSampler,
    route: Any,
) -> dict[str, Any] | None:
    """Return evidence for any caller identity that conflicts with inputs."""

    actual = _identity_document(sampler)
    expected_fields = {
        "scenario_id": actual["scenario_id"],
        "corridor_id": actual["corridor_id"],
        "vessel_profile_id": actual["vessel_profile_id"],
        "model_config_digest": actual["model_config_digest"],
    }
    for field, expected in expected_fields.items():
        provided = identity.get(field)
        if provided is not None and provided != expected:
            return {"field": field, "provided": provided, "expected": expected}
    provided_risk_identity = identity.get("risk_frame_identity")
    if isinstance(provided_risk_identity, Mapping):
        aliases = {
            "scenario_id": "scenario_id",
            "corridor_id": "corridor_id",
            "vessel_profile_id": "vessel_profile_id",
            "model_config_digest": "model_config_digest",
            "run_id": "run_id",
            "grid_id": "grid_id",
            "coordinate_digest": "coordinate_digest",
        }
        for field, actual_field in aliases.items():
            provided = provided_risk_identity.get(field)
            expected = actual.get(actual_field)
            if provided is not None and expected is not None and provided != expected:
                return {
                    "field": f"risk_frame_identity.{field}",
                    "provided": provided,
                    "expected": expected,
                }
    route_revision = _route_member(route, "revision")
    provided_revision = identity.get("plan_revision")
    if (
        provided_revision is not None
        and route_revision is not None
        and provided_revision != route_revision
    ):
        return {
            "field": "plan_revision",
            "provided": provided_revision,
            "expected": route_revision,
        }
    route_adoption = _parse_utc(_route_member(route, "effective_adoption_time"))
    provided_adoption = _parse_utc(identity.get("adoption_time"))
    if identity.get("adoption_time") is not None and provided_adoption is None:
        return {
            "field": "adoption_time",
            "provided": identity.get("adoption_time"),
            "expected": "timezone-aware UTC",
        }
    if (
        route_adoption is not None
        and provided_adoption is not None
        and route_adoption != provided_adoption
    ):
        return {
            "field": "adoption_time",
            "provided": _format_utc(provided_adoption),
            "expected": _format_utc(route_adoption),
        }
    return None


def _sample_points(
    sampler: RiskSampler,
    points: Sequence[Coordinate],
    times: Sequence[datetime],
) -> tuple[SampledRisk, ...]:
    values: list[SampledRisk] = []
    for point, sampled_at in zip(points, times, strict=True):
        try:
            values.append(sampler.sample(sampled_at, point[0], point[1]))
        except (RiskCoverageError, RiskOutOfBoundsError, RiskSamplingError) as error:
            raise _EvaluationFailure(
                "risk_sampling_incomplete",
                {
                    "complete": False,
                    "failure_type": type(error).__name__,
                    "failure": str(error),
                },
            ) from error
        except Exception as error:
            raise _EvaluationFailure(
                "risk_sampling_error",
                {"complete": False, "failure_type": type(error).__name__},
            ) from error
    return tuple(values)


def _speed_values(
    model: VesselPerformanceModel,
    risk_values: Sequence[SampledRisk],
) -> tuple[float, ...]:
    speeds: list[float] = []
    for value in risk_values:
        try:
            speeds.append(model.effective_speed(value.environment_speed_factor).speed_km_per_hour)
        except (UnnavigableSpeedError, ValueError) as error:
            raise _EvaluationFailure(
                "speed_not_navigable",
                {
                    "complete": False,
                    "failure_type": type(error).__name__,
                    "environment_speed_factor": value.environment_speed_factor,
                },
            ) from error
    return tuple(speeds)


def _risk_stats(
    risk_values: Sequence[SampledRisk],
    times: Sequence[datetime],
) -> dict[str, Any]:
    if len(risk_values) != len(times) or len(risk_values) < 2:
        raise _EvaluationFailure("insufficient_risk_samples")
    intervals = [
        (current - previous).total_seconds() / 3600.0 for previous, current in pairwise(times)
    ]
    if any(value <= 0 or not math.isfinite(value) for value in intervals):
        raise _EvaluationFailure("eta_not_strictly_increasing")
    integrated = sum(
        (left.risk_score + right.risk_score) * 0.5 * hours
        for left, right, hours in zip(risk_values[:-1], risk_values[1:], intervals, strict=True)
    )
    duration = sum(intervals)
    source_ids = tuple(
        dict.fromkeys(source_id for value in risk_values for source_id in value.source_risk_ids)
    )
    return {
        "sample_count": len(risk_values),
        "average_risk": integrated / duration,
        "maximum_risk": max(value.risk_score for value in risk_values),
        "integrated_risk_hours": integrated,
        "minimum_confidence": min(value.confidence for value in risk_values),
        "source_risk_ids": list(source_ids),
        "duration_hours": duration,
    }


def _integrate_path(
    sampler: RiskSampler,
    model: VesselPerformanceModel,
    points: Sequence[Coordinate],
    initial_times: Sequence[datetime],
    *,
    max_iterations: int,
    convergence_tolerance_s: float,
) -> tuple[tuple[datetime, ...], tuple[SampledRisk, ...], tuple[float, ...], int]:
    if len(points) != len(initial_times) or len(points) < 2:
        raise _EvaluationFailure("invalid_path_for_eta_integration")
    _, _local_points, distances = _path_metric(points)
    leg_lengths_km = tuple((right - left) / 1000.0 for left, right in pairwise(distances))
    current_times = tuple(initial_times)
    converged = False
    final_risks: tuple[SampledRisk, ...] = ()
    final_speeds: tuple[float, ...] = ()
    completed_iterations = 0
    for iteration in range(1, max_iterations + 1):
        completed_iterations = iteration
        final_risks = _sample_points(sampler, points, current_times)
        final_speeds = _speed_values(model, final_risks)
        next_times = [current_times[0]]
        for length_km, left_speed, right_speed in zip(
            leg_lengths_km, final_speeds[:-1], final_speeds[1:], strict=True
        ):
            mean_speed = (left_speed + right_speed) * 0.5
            if mean_speed <= 0 or not math.isfinite(mean_speed):
                raise _EvaluationFailure("speed_not_positive")
            next_times.append(next_times[-1] + timedelta(hours=length_km / mean_speed))
        candidate_times = tuple(next_times)
        delta_seconds = max(
            abs((candidate - current).total_seconds())
            for candidate, current in zip(candidate_times, current_times, strict=True)
        )
        current_times = candidate_times
        if delta_seconds <= convergence_tolerance_s:
            converged = True
            break
    if not converged:
        raise _EvaluationFailure(
            "eta_not_converged",
            {"iterations": max_iterations, "convergence_tolerance_s": convergence_tolerance_s},
        )
    final_risks = _sample_points(sampler, points, current_times)
    final_speeds = _speed_values(model, final_risks)
    if any(current <= previous for previous, current in pairwise(current_times)):
        raise _EvaluationFailure("eta_not_strictly_increasing")
    return current_times, final_risks, final_speeds, completed_iterations


def _curve_diagnostics(
    segments: Sequence[CurveSegment | Mapping[str, Any]],
    *,
    speeds_kmh: Sequence[float],
) -> dict[str, Any]:
    def value(segment: CurveSegment | Mapping[str, Any], name: str) -> Any:
        return getattr(segment, name) if isinstance(segment, CurveSegment) else segment.get(name)

    minimum_radius = min(
        (float(value(segment, "minimum_radius_m")) for segment in segments),
        default=None,
    )
    maximum_deviation = max(
        (float(value(segment, "maximum_deviation_m")) for segment in segments),
        default=None,
    )
    maximum_curvature = (
        1.0 / minimum_radius if minimum_radius is not None and minimum_radius > 0 else None
    )
    maximum_speed_mps = max(speeds_kmh, default=0.0) / 3.6
    maximum_yaw_rate = (
        math.degrees(maximum_curvature * maximum_speed_mps)
        if maximum_curvature is not None
        else None
    )
    maximum_lateral_acceleration = (
        maximum_curvature * maximum_speed_mps**2 if maximum_curvature is not None else None
    )
    return {
        "maximum_curvature_per_m": maximum_curvature,
        "minimum_radius_m": minimum_radius,
        "maximum_deviation_m": maximum_deviation,
        "maximum_yaw_rate": maximum_yaw_rate,
        "maximum_lateral_acceleration": maximum_lateral_acceleration,
        "maximum_speed_knots": max(speeds_kmh, default=0.0) / 1.852,
        "minimum_speed_knots": min(speeds_kmh, default=0.0) / 1.852,
        "yaw_rate_status": "DIAGNOSTIC_ONLY",
        "lateral_acceleration_status": "DIAGNOSTIC_ONLY",
    }


def _finish_digest(sidecar: dict[str, Any]) -> dict[str, Any]:
    sidecar.pop("sidecar_digest", None)
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)
    return sidecar


def _fallback_qualified_sidecar(
    sidecar: dict[str, Any],
    reason: str,
    *,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sidecar.setdefault(
        "validation",
        {
            "mode": "RISK_AND_VESSEL_RECHECK",
            "risk_rechecked": False,
            "hard_mask_rechecked": False,
            "coverage_complete": False,
            "eta_recomputed": False,
            "speed_checked": False,
            "research_gate_passed": False,
            "resource_evidence_complete": False,
            "production_qualified": False,
        },
    )
    sidecar["status"] = "FALLBACK"
    sidecar["applied"] = False
    sidecar["curve_status"] = "FALLBACK"
    sidecar["fallback_reason"] = reason
    sidecar["research_eligible"] = False
    sidecar["motion_samples"] = []
    sidecar["curve_samples"] = []
    sidecar["sample_eta"] = []
    sidecar["cumulative_distance_m"] = []
    sidecar["validation"].update(
        {
            "mode": "RISK_AND_VESSEL_RECHECK",
            "risk_rechecked": False,
            "hard_mask_rechecked": False,
            "coverage_complete": False,
            "eta_recomputed": False,
            "speed_checked": False,
            "research_gate_passed": False,
            "production_qualified": False,
        }
    )
    sidecar["qualification_failure_evidence"] = dict(evidence or {})
    return _finish_digest(sidecar)


def build_qualified_route_smoothing_sidecar(
    route: Any,
    *,
    experiment_id: str,
    risk_sampler: RiskSampler,
    vessel_model: VesselPerformanceModel,
    corridor_validator: CorridorValidator | None,
    policy: RouteSmoothingPolicy | None = None,
    radius_sensitivity_m: Sequence[float] = (1_000.0, 2_000.0, 4_000.0),
    input_identity: Mapping[str, Any] | None = None,
    eta_max_iterations: int = 8,
    eta_convergence_tolerance_s: float = 0.5,
    risk_tolerance: float = 1e-9,
    maximum_sample_count: int = 10_000,
) -> dict[str, Any]:
    """Build a qualified research sidecar, or a fail-closed fallback.

    The public formal route is never changed.  ``corridor_validator`` must
    return a complete accepted evidence mapping (or ``True``) for each local
    candidate.  RiskFrame sampling and speed/ETA reconstruction then run on
    the whole selected curve.  A successful result is research-eligible only;
    vessel calibration and production qualification remain explicitly false.
    """

    if eta_max_iterations < 1 or eta_convergence_tolerance_s <= 0:
        raise ValueError("ETA iteration policy must be positive")
    if risk_tolerance < 0 or maximum_sample_count < 2:
        raise ValueError("risk tolerance and sample limit are invalid")
    if not isinstance(risk_sampler, RiskSampler):
        raise TypeError("risk_sampler must be a RiskSampler")
    try:
        points, raw_times = _route_records(route)
    except _EvaluationFailure as failure:
        sidecar = build_route_smoothing_sidecar(
            route,
            experiment_id=experiment_id,
            policy=policy,
            input_identity=input_identity,
            radius_sensitivity_m=radius_sensitivity_m,
        )
        return _fallback_qualified_sidecar(sidecar, failure.reason, evidence=failure.evidence)
    frame, local_raw, raw_distances = _path_metric(points)
    del local_raw
    identity = dict(input_identity or {})
    identity.setdefault("risk_frame_identity", _identity_document(risk_sampler))
    identity.setdefault("scenario_id", risk_sampler.identity.scenario_id)
    identity.setdefault("corridor_id", risk_sampler.identity.corridor_id)
    identity.setdefault("vessel_profile_id", risk_sampler.identity.vessel_profile_id)
    identity.setdefault("model_config_digest", risk_sampler.identity.model_config_digest)
    identity.setdefault("route_identity", {"route_id": _route_member(route, "route_id")})

    identity_failure = _identity_mismatch(identity, risk_sampler, route)
    if identity_failure is not None:
        sidecar = build_route_smoothing_sidecar(
            route,
            experiment_id=experiment_id,
            policy=policy,
            input_identity=identity,
            radius_sensitivity_m=radius_sensitivity_m,
        )
        return _fallback_qualified_sidecar(
            sidecar,
            "identity_mismatch",
            evidence=identity_failure,
        )

    def candidate_validator(
        segment: CurveSegment,
        local_points: tuple[Coordinate, ...],
    ) -> CandidateDecision:
        try:
            candidate_points = _local_distance_to_geo(frame, segment.samples)
            candidate_times = _candidate_times(
                segment,
                local_points,
                raw_times,
                raw_distances,
                frame,
            )
            corridor = _corridor_evidence(
                corridor_validator,
                candidate_points,
                candidate_times,
            )
            risk_values = _sample_points(risk_sampler, candidate_points, candidate_times)
            if any(value.hard_mask for value in risk_values):
                return CandidateDecision(
                    False,
                    reason="hard_mask",
                    evidence={
                        "hard_mask_evidence": {
                            "complete": True,
                            "violations": sum(value.hard_mask for value in risk_values),
                        },
                        "coverage_evidence": {"complete": True},
                        "corridor_evidence": corridor,
                    },
                )
            speeds = _speed_values(vessel_model, risk_values)
        except _EvaluationFailure as failure:
            return CandidateDecision(False, reason=failure.reason, evidence=failure.evidence)
        risk = _risk_stats(risk_values, candidate_times)
        return CandidateDecision(
            True,
            evidence={
                "corridor_evidence": corridor,
                "risk_evidence": {
                    "complete": True,
                    "maximum_risk": risk["maximum_risk"],
                    "source_risk_ids": risk["source_risk_ids"],
                },
                "hard_mask_evidence": {
                    "complete": True,
                    "violations": 0,
                },
                "coverage_evidence": {
                    "complete": True,
                    "sample_count": len(risk_values),
                },
                "eta_evidence": {
                    "strictly_increasing": True,
                    "method": "raw_eta_candidate_clock_for_radius_screening",
                },
                "speed_evidence": {
                    "complete": True,
                    "minimum_speed_knots": min(speeds) / 1.852,
                    "maximum_speed_knots": max(speeds) / 1.852,
                },
            },
        )

    try:
        sidecar = build_route_smoothing_sidecar(
            route,
            experiment_id=experiment_id,
            policy=policy,
            candidate_validator=candidate_validator,
            input_identity=identity,
            radius_sensitivity_m=radius_sensitivity_m,
        )
    except _EvaluationFailure as failure:
        # This branch is mostly for malformed route inputs; normal candidate
        # failures are returned by the geometry builder as FALLBACK.
        return _fallback_qualified_sidecar({}, failure.reason, evidence=failure.evidence)

    if sidecar.get("status") != "ACCEPTED" or not sidecar.get("applied"):
        reason = str(sidecar.get("fallback_reason") or "geometry_fallback")
        if reason == "all_curves_rejected":
            rejected = sidecar.get("geometry", {}).get("rejected_corners", [])
            if isinstance(rejected, list) and rejected:
                candidate_reason = rejected[-1].get("reason")
                if isinstance(candidate_reason, str) and candidate_reason:
                    reason = candidate_reason
        return _fallback_qualified_sidecar(
            sidecar,
            reason,
        )
    geometry_points = tuple(
        (float(value[0]), float(value[1]))
        for value in sidecar.get("geometry", {}).get("points", [])
        if isinstance(value, Sequence) and len(value) == 2
    )
    if len(geometry_points) < 2 or len(geometry_points) > maximum_sample_count:
        return _fallback_qualified_sidecar(
            sidecar,
            "sample_count_limit",
            evidence={"sample_count": len(geometry_points)},
        )
    anchors = _anchor_indices(points, geometry_points)
    if anchors is None:
        return _fallback_qualified_sidecar(sidecar, "non_monotonic_curve_anchors")
    _, _, curve_distances = _path_metric(geometry_points)
    anchor_distances = tuple(curve_distances[index] for index in anchors)
    provisional_times = tuple(
        _time_at_distance(distance, anchor_distances, raw_times) for distance in curve_distances
    )
    try:
        curve_times, curve_risks, curve_speeds, iterations = _integrate_path(
            risk_sampler,
            vessel_model,
            geometry_points,
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
        corridor = _corridor_evidence(
            corridor_validator,
            geometry_points,
            curve_times,
        )
        if any(value.hard_mask for value in curve_risks):
            raise _EvaluationFailure(
                "hard_mask",
                {
                    "violations": sum(value.hard_mask for value in curve_risks),
                    "complete": True,
                },
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
        diagnostics = _curve_diagnostics(
            tuple(
                segment
                for segment in sidecar.get("geometry", {}).get("segments", ())
                if isinstance(segment, Mapping)
            ),
            speeds_kmh=curve_speeds,
        )
    except _EvaluationFailure as failure:
        sidecar["risk_evidence"] = failure.evidence
        return _fallback_qualified_sidecar(sidecar, failure.reason, evidence=failure.evidence)

    motion_samples = [
        {"lon": point[0], "lat": point[1], "eta": _format_utc(eta)}
        for point, eta in zip(geometry_points, curve_times, strict=True)
    ]
    sidecar["motion_samples"] = motion_samples
    sidecar["curve_samples"] = [{"lon": point[0], "lat": point[1]} for point in geometry_points]
    sidecar["sample_eta"] = [item["eta"] for item in motion_samples]
    sidecar["cumulative_distance_m"] = list(curve_distances)
    sidecar["parameterization"] = {
        "method": "iterative_arc_length_integration_with_existing_vessel_model",
        "sample_count": len(motion_samples),
        "anchor_count": len(anchors),
        "anchor_indices": list(anchors),
        "path_length_m": curve_distances[-1],
        "anchor_distances_m": list(anchor_distances),
        "eta_iterations": iterations,
        "raw_eta_iterations": raw_iterations,
    }
    sidecar["risk_evidence"] = {
        "complete": True,
        "sampler": "RiskSampler.sample",
        "curve": curve_risk,
        "raw_baseline": raw_risk,
        "maximum_risk_delta": curve_risk["maximum_risk"] - raw_risk["maximum_risk"],
        "integrated_risk_hours_delta": (
            curve_risk["integrated_risk_hours"] - raw_risk["integrated_risk_hours"]
        ),
        "tolerance": risk_tolerance,
    }
    sidecar["hard_mask_evidence"] = {
        "complete": True,
        "method": "RiskSampler spatial contributor OR at every adaptive curve sample",
        "curve_violations": 0,
        "raw_violations": sum(value.hard_mask for value in raw_risks),
        "sample_count": len(curve_risks),
    }
    sidecar["coverage_evidence"] = {
        "complete": True,
        "risk_frame_identity": _identity_document(risk_sampler),
        "curve_sample_count": len(curve_risks),
        "raw_sample_count": len(raw_risks),
        "continuous_containment_proved": corridor.get("continuous_containment_proved", False),
        "corridor_evidence": corridor,
    }
    sidecar["eta_evidence"] = {
        "complete": True,
        "recomputed": True,
        "strictly_increasing": True,
        "method": "iterative_arc_length_integration_with_existing_vessel_model",
        "formal_start_eta": _format_utc(raw_times[0]),
        "formal_end_eta": _format_utc(raw_times[-1]),
        "curve_end_eta": _format_utc(curve_times[-1]),
        "delta_seconds": (curve_times[-1] - raw_times[-1]).total_seconds(),
    }
    sidecar["speed_evidence"] = {
        "complete": True,
        "model_version": vessel_model.model_version,
        "minimum_speed_knots": min(curve_speeds) / 1.852,
        "maximum_speed_knots": max(curve_speeds) / 1.852,
        "raw_minimum_speed_knots": min(raw_speeds) / 1.852,
        "raw_maximum_speed_knots": max(raw_speeds) / 1.852,
    }
    sidecar["minimum_radius_m"] = diagnostics["minimum_radius_m"]
    sidecar["maximum_deviation_m"] = diagnostics["maximum_deviation_m"]
    sidecar["maximum_yaw_rate"] = diagnostics["maximum_yaw_rate"]
    sidecar["maximum_lateral_acceleration"] = diagnostics["maximum_lateral_acceleration"]
    sidecar["diagnostics"] = diagnostics
    sidecar["validation"].update(
        {
            "mode": "RISK_AND_VESSEL_RECHECK",
            "risk_rechecked": True,
            "hard_mask_rechecked": True,
            "coverage_complete": True,
            "eta_recomputed": True,
            "speed_checked": True,
            "curvature_checked": True,
            "research_gate_passed": True,
            "resource_evidence_complete": False,
            "production_qualified": False,
            "calibration_status": "NOT_CALIBRATED",
            "manoeuvring_qualification": "NOT_MANOEUVRING_QUALIFIED",
        }
    )
    sidecar["research_eligible"] = True
    sidecar["curve_status"] = "ACCEPTED"
    sidecar["fallback_reason"] = None
    sidecar["determinism_digest"] = _canonical_digest(
        {
            "curve_digest": sidecar.get("curve_digest"),
            "motion_samples": motion_samples,
            "risk_evidence": sidecar["risk_evidence"],
            "hard_mask_evidence": sidecar["hard_mask_evidence"],
            "eta_evidence": sidecar["eta_evidence"],
        }
    )
    return _finish_digest(sidecar)


__all__ = [
    "CorridorValidator",
    "RouteSmoothingQualificationError",
    "build_qualified_route_smoothing_sidecar",
]
