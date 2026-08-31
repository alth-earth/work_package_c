from __future__ import annotations

from datetime import timedelta

from arctic_route_planning.research.route_smoothing_r2 import (
    BLOCKED,
    CALIBRATION_SCHEMA,
    CONTINUOUS_CORRIDOR_SCHEMA,
    READY,
    StageTimingCollector,
    assess_production_proposal_readiness,
    build_eta_drift_diagnostic,
)

from .factories import T0

_DIGEST = "a" * 64
_IDENTITY = {
    "route_digest": "route-digest",
    "risk_window_commit": "risk-window-commit",
    "vessel_profile_id": "target-vessel-v1",
}


def _eta(*, drift_seconds: float = 0.0) -> dict:
    points = ((0.0, 0.0), (0.5, 0.0), (1.0, 0.0))
    published = (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2))
    recomputed = (
        T0,
        T0 + timedelta(hours=1, seconds=drift_seconds / 2.0),
        T0 + timedelta(hours=2, seconds=drift_seconds),
    )
    return build_eta_drift_diagnostic(
        points,
        published,
        recomputed,
        route_identity={"route_digest": _IDENTITY["route_digest"]},
        risk_window_identity={"commit_id": _IDENTITY["risk_window_commit"]},
        vessel_profile_id=_IDENTITY["vessel_profile_id"],
        vessel_model_version="target-vessel-performance-v1",
        published_distance_km=100.0,
    )


def _performance() -> dict:
    return {
        "complete": True,
        "qualified": True,
        "cgroup_limits_enforced": True,
        "cold_wall_overhead_ratio": 0.09,
    }


def _calibration() -> dict:
    return {
        "schema_version": CALIBRATION_SCHEMA,
        "status": "TARGET_VESSEL_TRACEABLE_CALIBRATED",
        "source_kind": "TARGET_VESSEL_TRIAL",
        "source_sha256": _DIGEST,
        "holdout": {"passed": True, "digest": _DIGEST},
        "yaw_rate_limit_degrees_per_second": 0.1,
        "lateral_acceleration_limit_mps2": 0.01,
        **_IDENTITY,
    }


def _corridor() -> dict:
    return {
        "schema_version": CONTINUOUS_CORRIDOR_SCHEMA,
        "accepted": True,
        "complete": True,
        "continuous_containment_proved": True,
        "hard_mask_envelope_complete": True,
        "navigation_semantics_bound": True,
        "coverage_complete": True,
        "unknown_region_count": 0,
        "source_sha256": _DIGEST,
        **_IDENTITY,
    }


def test_eta_diagnostic_keeps_existing_drift_separate_from_smoothing() -> None:
    diagnostic = _eta(drift_seconds=5_061.0)

    assert diagnostic["status"] == "UNRESOLVED_EXISTING_PUBLISHED_VS_RECOMPUTED_DRIFT"
    assert diagnostic["resolution_required"] is True
    assert diagnostic["smoothing_attribution"] == "EXCLUDED"
    assert diagnostic["cause_classification"] == (
        "PARTIAL_ROOT_CAUSE_DISTANCE_BASIS_MISMATCH_OBSERVED"
    )
    assert diagnostic["distance_basis"]["status"] == "MISMATCH_OBSERVED"
    assert diagnostic["distance_basis"]["full_eta_attribution_claimed"] is False
    assert diagnostic["total_delta_seconds"] == 5_061.0
    assert diagnostic["diagnostic_digest"]


def test_readiness_rejects_current_r1_evidence_fail_closed() -> None:
    readiness = assess_production_proposal_readiness(
        performance_evidence={
            "complete": True,
            "qualified": False,
            "cgroup_limits_enforced": True,
            "cold_wall_overhead_ratio": 252.15,
        },
        manoeuvring_calibration={"status": "SYNTHETIC_UNCALIBRATED"},
        continuous_corridor_evidence={
            "raster_resolution_containment_proved": True,
            "continuous_containment_proved": False,
        },
        eta_diagnostic=_eta(drift_seconds=5_061.0),
        expected_identity=_IDENTITY,
    )

    assert readiness["status"] == BLOCKED
    assert readiness["proposal_ready"] is False
    assert readiness["production_qualified"] is False
    assert readiness["cutover_authorized"] is False
    assert "performance_gate_failed" in readiness["blockers"]
    assert "target_vessel_calibration_missing" in readiness["blockers"]
    assert "continuous_corridor_proof_missing" in readiness["blockers"]
    assert "published_eta_drift_unresolved" in readiness["blockers"]


def test_complete_external_evidence_only_allows_a_later_proposal() -> None:
    readiness = assess_production_proposal_readiness(
        performance_evidence=_performance(),
        manoeuvring_calibration=_calibration(),
        continuous_corridor_evidence=_corridor(),
        eta_diagnostic=_eta(),
        expected_identity=_IDENTITY,
    )

    assert readiness["status"] == READY
    assert readiness["proposal_ready"] is True
    assert readiness["blockers"] == []
    assert readiness["production_qualified"] is False
    assert readiness["cutover_authorized"] is False


def test_stage_timing_collector_summarises_repeated_observations() -> None:
    collector = StageTimingCollector()
    collector.observe("risk", 0.3)
    collector.observe("risk", 0.1)

    assert collector.summary()["risk"] == {
        "count": 2,
        "total_seconds": 0.4,
        "median_seconds": 0.2,
        "maximum_seconds": 0.3,
    }
