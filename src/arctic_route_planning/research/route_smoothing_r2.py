"""R2-only profiling, ETA diagnosis, and fail-closed proposal readiness.

The helpers in this module do not alter RoutePlan, publish a sidecar, or grant
production qualification.  They make the remaining evidence gaps explicit so
that missing external calibration or navigation proof cannot be represented as
a successful cutover.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise
from typing import Any

from .route_smoothing import _canonical_digest, _format_utc, _path_metric

ETA_DIAGNOSTIC_SCHEMA = "c.research-route-smoothing-eta-drift.v1"
READINESS_SCHEMA = "c.research-route-smoothing-proposal-readiness.v1"
CALIBRATION_SCHEMA = "c.route-smoothing-manoeuvring-calibration.v1"
CONTINUOUS_CORRIDOR_SCHEMA = "c.route-smoothing-continuous-corridor-proof.v1"
READY = "READY_FOR_PRODUCTION_PROPOSAL_NO_PRODUCTION_CUTOVER"
BLOCKED = "BLOCKED_NO_PRODUCTION_CUTOVER"

_CALIBRATION_SOURCES = frozenset(
    {"TARGET_VESSEL_BOOKLET", "TARGET_VESSEL_TRIAL", "APPROVED_SIMULATOR"}
)


@dataclass(slots=True)
class StageTimingCollector:
    """Collect out-of-band wall timings without entering sidecar identity."""

    _values: dict[str, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )

    def observe(self, name: str, seconds: float) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError("stage name must be non-empty")
        if not math.isfinite(seconds) or seconds < 0.0:
            raise ValueError("stage duration must be finite and non-negative")
        self._values[name].append(float(seconds))

    def summary(self) -> dict[str, dict[str, float | int]]:
        return {
            name: {
                "count": len(values),
                "total_seconds": sum(values),
                "median_seconds": statistics.median(values),
                "maximum_seconds": max(values),
            }
            for name, values in sorted(self._values.items())
        }


def _positive_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def build_eta_drift_diagnostic(
    points: Sequence[tuple[float, float]],
    published_times: Sequence[datetime],
    recomputed_times: Sequence[datetime],
    *,
    route_identity: Mapping[str, Any],
    risk_window_identity: Mapping[str, Any],
    vessel_profile_id: str,
    vessel_model_version: str,
    published_distance_km: float | None = None,
    published_distance_method: str = "UNDECLARED_IN_VIEWER_BUNDLE",
    recomputed_distance_method: str = "C_LOCAL_EQUIRECTANGULAR_PATH_METRIC",
    alignment_tolerance_seconds: float = 600.0,
) -> dict[str, Any]:
    """Describe published-vs-recomputed ETA drift without inferring a cause."""

    if len(points) < 2 or len(points) != len(published_times):
        raise ValueError("points and published_times must have the same length >= 2")
    if len(points) != len(recomputed_times):
        raise ValueError("recomputed_times must align with points")
    if not _positive_number(alignment_tolerance_seconds):
        raise ValueError("alignment_tolerance_seconds must be positive")
    if published_distance_km is not None and not _positive_number(published_distance_km):
        raise ValueError("published_distance_km must be positive when supplied")
    for name, values in (
        ("published_times", published_times),
        ("recomputed_times", recomputed_times),
    ):
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError(f"{name} must be timezone-aware")
        if any(current <= previous for previous, current in pairwise(values)):
            raise ValueError(f"{name} must be strictly increasing")

    _, _, cumulative = _path_metric(points)
    legs: list[dict[str, Any]] = []
    for index in range(len(points) - 1):
        distance_km = (cumulative[index + 1] - cumulative[index]) / 1000.0
        published_seconds = (
            published_times[index + 1] - published_times[index]
        ).total_seconds()
        recomputed_seconds = (
            recomputed_times[index + 1] - recomputed_times[index]
        ).total_seconds()
        legs.append(
            {
                "leg_index": index,
                "distance_km": distance_km,
                "published_duration_seconds": published_seconds,
                "recomputed_duration_seconds": recomputed_seconds,
                "delta_seconds": recomputed_seconds - published_seconds,
                "published_implied_speed_kmh": distance_km * 3600.0
                / published_seconds,
                "recomputed_implied_speed_kmh": distance_km * 3600.0
                / recomputed_seconds,
            }
        )
    total_delta = (
        recomputed_times[-1] - published_times[-1]
    ).total_seconds()
    recomputed_distance_km = cumulative[-1] / 1000.0
    distance_delta_km = (
        recomputed_distance_km - published_distance_km
        if published_distance_km is not None
        else None
    )
    distance_basis_mismatch = (
        distance_delta_km is not None and abs(distance_delta_km) > 0.001
    )
    aligned = abs(total_delta) <= alignment_tolerance_seconds
    diagnostic = {
        "schema_version": ETA_DIAGNOSTIC_SCHEMA,
        "status": (
            "ALIGNED_WITHIN_TOLERANCE"
            if aligned
            else "UNRESOLVED_EXISTING_PUBLISHED_VS_RECOMPUTED_DRIFT"
        ),
        "resolution_required": not aligned,
        "cause_classification": (
            "PARTIAL_ROOT_CAUSE_DISTANCE_BASIS_MISMATCH_OBSERVED"
            if distance_basis_mismatch
            else "NOT_INFERRED_FROM_TIMESTAMPS"
        ),
        "smoothing_attribution": "EXCLUDED",
        "route_identity": dict(route_identity),
        "risk_window_identity": dict(risk_window_identity),
        "vessel_profile_id": vessel_profile_id,
        "vessel_model_version": vessel_model_version,
        "published_start_eta": _format_utc(published_times[0]),
        "published_end_eta": _format_utc(published_times[-1]),
        "recomputed_start_eta": _format_utc(recomputed_times[0]),
        "recomputed_end_eta": _format_utc(recomputed_times[-1]),
        "total_delta_seconds": total_delta,
        "alignment_tolerance_seconds": alignment_tolerance_seconds,
        "distance_basis": {
            "status": (
                "MISMATCH_OBSERVED"
                if distance_basis_mismatch
                else "ALIGNED_OR_NOT_COMPARABLE"
            ),
            "published_distance_km": published_distance_km,
            "published_distance_method": published_distance_method,
            "recomputed_distance_km": recomputed_distance_km,
            "recomputed_distance_method": recomputed_distance_method,
            "delta_km": distance_delta_km,
            "full_eta_attribution_claimed": False,
        },
        "legs": legs,
        "unresolved_dimensions": (
            []
            if aligned
            else [
                "published_speed_model_version",
                "published_distance_method",
                "published_environment_factor_policy",
                "published_wait_or_replan_adjustments",
            ]
        ),
    }
    diagnostic["diagnostic_digest"] = _canonical_digest(diagnostic)
    return diagnostic


def _identity_blockers(
    evidence: Mapping[str, Any], expected: Mapping[str, Any], prefix: str
) -> list[str]:
    blockers = []
    for field_name in ("route_digest", "risk_window_commit", "vessel_profile_id"):
        expected_value = expected.get(field_name)
        if expected_value is not None and evidence.get(field_name) != expected_value:
            blockers.append(f"{prefix}_identity_mismatch:{field_name}")
    return blockers


def assess_production_proposal_readiness(
    *,
    performance_evidence: Mapping[str, Any] | None,
    manoeuvring_calibration: Mapping[str, Any] | None,
    continuous_corridor_evidence: Mapping[str, Any] | None,
    eta_diagnostic: Mapping[str, Any] | None,
    expected_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Gate only readiness for a later contract proposal, never cutover."""

    blockers: list[str] = []
    performance = dict(performance_evidence or {})
    if performance.get("complete") is not True:
        blockers.append("performance_evidence_incomplete")
    if performance.get("qualified") is not True:
        blockers.append("performance_gate_failed")
    if performance.get("cgroup_limits_enforced") is not True:
        blockers.append("performance_cgroup_not_enforced")
    ratio = performance.get("cold_wall_overhead_ratio")
    if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or ratio > 0.10:
        blockers.append("cold_wall_overhead_above_10_percent")

    calibration = dict(manoeuvring_calibration or {})
    if calibration.get("schema_version") != CALIBRATION_SCHEMA:
        blockers.append("target_vessel_calibration_missing")
    else:
        if calibration.get("status") != "TARGET_VESSEL_TRACEABLE_CALIBRATED":
            blockers.append("target_vessel_calibration_not_approved")
        if calibration.get("source_kind") not in _CALIBRATION_SOURCES:
            blockers.append("target_vessel_calibration_source_invalid")
        if not _sha256(calibration.get("source_sha256")):
            blockers.append("target_vessel_calibration_source_digest_invalid")
        holdout = calibration.get("holdout")
        if not isinstance(holdout, Mapping) or holdout.get("passed") is not True:
            blockers.append("target_vessel_calibration_holdout_failed")
        elif not _sha256(holdout.get("digest")):
            blockers.append("target_vessel_calibration_holdout_digest_invalid")
        if not _positive_number(calibration.get("yaw_rate_limit_degrees_per_second")):
            blockers.append("target_vessel_yaw_limit_invalid")
        if not _positive_number(calibration.get("lateral_acceleration_limit_mps2")):
            blockers.append("target_vessel_lateral_acceleration_limit_invalid")
        blockers.extend(_identity_blockers(calibration, expected_identity, "calibration"))

    corridor = dict(continuous_corridor_evidence or {})
    if corridor.get("schema_version") != CONTINUOUS_CORRIDOR_SCHEMA:
        blockers.append("continuous_corridor_proof_missing")
    else:
        required_true = (
            "accepted",
            "complete",
            "continuous_containment_proved",
            "hard_mask_envelope_complete",
            "navigation_semantics_bound",
            "coverage_complete",
        )
        if any(corridor.get(field) is not True for field in required_true):
            blockers.append("continuous_corridor_proof_failed")
        if corridor.get("unknown_region_count") != 0:
            blockers.append("continuous_corridor_unknown_regions")
        if not _sha256(corridor.get("source_sha256")):
            blockers.append("continuous_corridor_source_digest_invalid")
        blockers.extend(_identity_blockers(corridor, expected_identity, "corridor"))

    eta = dict(eta_diagnostic or {})
    if eta.get("schema_version") != ETA_DIAGNOSTIC_SCHEMA:
        blockers.append("eta_diagnostic_missing")
    elif (
        eta.get("status") != "ALIGNED_WITHIN_TOLERANCE"
        or eta.get("resolution_required") is not False
    ):
        blockers.append("published_eta_drift_unresolved")

    unique_blockers = list(dict.fromkeys(blockers))
    proposal_ready = not unique_blockers
    result = {
        "schema_version": READINESS_SCHEMA,
        "status": READY if proposal_ready else BLOCKED,
        "proposal_ready": proposal_ready,
        "production_qualified": False,
        "cutover_authorized": False,
        "blockers": unique_blockers,
        "expected_identity": dict(expected_identity),
    }
    result["readiness_digest"] = _canonical_digest(result)
    return result


__all__ = [
    "BLOCKED",
    "CALIBRATION_SCHEMA",
    "CONTINUOUS_CORRIDOR_SCHEMA",
    "ETA_DIAGNOSTIC_SCHEMA",
    "READINESS_SCHEMA",
    "READY",
    "StageTimingCollector",
    "assess_production_proposal_readiness",
    "build_eta_drift_diagnostic",
]
