"""Synthetic qualification gates for the multi-span research sidecar v2."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.cost.vessel import VesselPerformanceModel
from arctic_route_planning.research.route_smoothing_qualification_v2 import (
    build_qualified_route_smoothing_sidecar_v2,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _route() -> dict:
    return {
        "plan_id": "qualification-route-v2",
        "revision": 1,
        "effective_adoption_time": "2026-01-01T00:00:00Z",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": T0},
            {"lon": 0.8, "lat": 0.0, "eta": T0 + timedelta(hours=12)},
            {"lon": 0.8, "lat": 0.8, "eta": T0 + timedelta(hours=24)},
        ],
    }


def _sampler(*, end_hours: float = 40) -> RiskSampler:
    values = np.zeros((3, 3), dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=offset),
            values,
            risk_id=f"qualification-v2-risk-{offset}",
            hard_mask=np.zeros((3, 3), dtype=np.bool_),
            environment_speed_factor=np.ones((3, 3), dtype=np.float32),
            latitudes=(0.0, 0.4, 0.8),
            longitudes=(0.0, 0.4, 0.8),
        )
        for offset in (0, end_hours)
    )
    return RiskSampler(frames)


def _model() -> VesselPerformanceModel:
    return VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=3.0,
        maximum_speed_knots=15.7,
        minimum_speed_factor=0.2,
    )


def _corridor(hulls, points, times, margin):
    return {
        "accepted": True,
        "complete": True,
        "continuous_containment_proved": False,
        "raster_resolution_containment_proved": True,
        "hard_mask_envelope_complete": True,
        "scope": "RASTER_RESOLUTION_CONTAINMENT_PASS",
        "hull_count": len(hulls),
        "sample_count": len(points),
        "time_count": len(times),
        "expansion_m": margin,
    }


def test_v2_qualification_is_deterministic_and_pointwise() -> None:
    first = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.qualification.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )
    second = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.qualification.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )

    assert first["status"] == "ACCEPTED"
    assert first["sidecar_digest"] == second["sidecar_digest"]
    assert first["validation"]["manoeuvring_checked"] is True
    assert first["manoeuvring_evidence"]["accepted"] is True
    assert len(first["manoeuvring_evidence"]["curvatures_m_inv"]) == len(
        first["motion_samples"]
    )
    assert all(
        "course_degrees" in item and "speed_knots" in item
        for item in first["motion_samples"]
    )
    assert first["calibration_status"] == "SYNTHETIC_UNCALIBRATED"
    assert first["production_qualified"] is False
    assert first["validation"]["resource_evidence_complete"] is False


def test_v2_missing_or_incomplete_raster_evidence_falls_back() -> None:
    missing = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.missing-raster.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=None,
    )
    incomplete = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.incomplete-raster.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=lambda _hulls, _points, _times, _margin: {
            "accepted": True,
            "complete": True,
            "continuous_containment_proved": False,
            "raster_resolution_containment_proved": True,
        },
    )

    assert missing["status"] == "FALLBACK"
    assert missing["fallback_reason"] == "missing_raster_corridor_evidence"
    assert incomplete["status"] == "FALLBACK"
    assert incomplete["fallback_reason"] == "raster_corridor_evidence_failed"


def test_v2_final_eta_coverage_failure_is_atomic() -> None:
    sidecar = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.coverage.synthetic.v2",
        risk_sampler=_sampler(end_hours=8.0),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )

    assert sidecar["status"] == "FALLBACK"
    assert sidecar["motion_samples"] == []
    assert sidecar["research_eligible"] is False
    assert sidecar["fallback_reason"] in {
        "risk_sampling_incomplete",
        "pointwise_manoeuvring_limit_exceeded",
    }


def test_v2_stage_observer_is_out_of_band_and_does_not_change_digest() -> None:
    stages: list[tuple[str, float]] = []
    observed = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.profile.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
        stage_observer=lambda name, seconds: stages.append((name, seconds)),
    )
    canonical = build_qualified_route_smoothing_sidecar_v2(
        _route(),
        experiment_id="c.route-smoothing.profile.synthetic.v2",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )

    assert observed["sidecar_digest"] == canonical["sidecar_digest"]
    assert stages
    assert all(seconds >= 0.0 for _, seconds in stages)
    names = {name for name, _ in stages}
    assert "geometry_and_candidate_screening" in names
    assert "candidate_corridor" in names
    assert "curve_eta_risk_integration" in names
    assert "raw_eta_risk_integration" in names
    assert "final_corridor_and_sensitivity" in names
