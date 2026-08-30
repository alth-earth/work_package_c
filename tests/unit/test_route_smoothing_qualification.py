"""Synthetic fail-closed gates for the route-smoothing research qualifier."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.cost.vessel import VesselPerformanceModel
from arctic_route_planning.research.route_smoothing import (
    evaluate_clamped_cubic_bspline,
    evaluate_clamped_cubic_bspline_derivatives,
)
from arctic_route_planning.research.route_smoothing_qualification import (
    build_qualified_route_smoothing_sidecar,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _route() -> dict:
    return {
        "plan_id": "qualification-route",
        "revision": 1,
        "effective_adoption_time": "2026-01-01T00:00:00Z",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": T0},
            {"lon": 0.2, "lat": 0.0, "eta": T0 + timedelta(hours=1)},
            {"lon": 0.2, "lat": 0.2, "eta": T0 + timedelta(hours=2)},
        ],
    }


def _sampler(
    *,
    risk: np.ndarray | None = None,
    hard_mask: np.ndarray | None = None,
    environment_speed_factor: np.ndarray | None = None,
    end_hours: int = 4,
) -> RiskSampler:
    values = np.zeros((3, 3), dtype=np.float32) if risk is None else risk
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=offset),
            values,
            risk_id=f"qualification-risk-{offset}",
            hard_mask=hard_mask,
            environment_speed_factor=environment_speed_factor
            if environment_speed_factor is not None
            else np.ones((3, 3), dtype=np.float32),
            latitudes=(0.0, 0.1, 0.2),
            longitudes=(0.0, 0.1, 0.2),
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


def _corridor(points, times):
    return {
        "accepted": True,
        "complete": True,
        "continuous_containment_proved": True,
        "hard_mask_envelope_complete": True,
        "sample_count": len(points),
        "time_count": len(times),
    }


def test_clamped_cubic_bspline_uses_endpoint_and_analytic_derivatives() -> None:
    controls = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (2.0, 1.0))
    assert evaluate_clamped_cubic_bspline(controls, 0.0) == controls[0]
    assert evaluate_clamped_cubic_bspline(controls, 1.0) == controls[-1]
    first, second = evaluate_clamped_cubic_bspline_derivatives(controls, 0.0)
    assert first == (3.0, 0.0)
    assert second == (-6.0, 6.0)


def test_qualified_synthetic_sidecar_passes_all_non_resource_gates() -> None:
    sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.qualification.synthetic.v1",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )

    assert sidecar["status"] == "ACCEPTED"
    assert sidecar["research_eligible"] is True
    assert sidecar["validation"]["research_gate_passed"] is True
    assert sidecar["validation"]["resource_evidence_complete"] is False
    assert sidecar["validation"]["production_qualified"] is False
    assert sidecar["eta_evidence"]["recomputed"] is True
    assert sidecar["hard_mask_evidence"]["curve_violations"] == 0
    assert (
        sidecar["sidecar_digest"]
        == build_qualified_route_smoothing_sidecar(
            _route(),
            experiment_id="c.route-smoothing.qualification.synthetic.v1",
            risk_sampler=_sampler(),
            vessel_model=_model(),
            corridor_validator=_corridor,
        )["sidecar_digest"]
    )


def test_missing_corridor_evidence_falls_back() -> None:
    sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.missing-corridor.synthetic.v1",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=None,
    )

    assert sidecar["status"] == "FALLBACK"
    assert sidecar["research_eligible"] is False
    assert sidecar["fallback_reason"] == "missing_corridor_evidence"


def test_identity_mismatch_fails_closed_before_research_motion_is_qualified() -> None:
    sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.identity-mismatch.synthetic.v1",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
        input_identity={"scenario_id": "another-scenario"},
    )

    assert sidecar["status"] == "FALLBACK"
    assert sidecar["research_eligible"] is False
    assert sidecar["fallback_reason"] == "identity_mismatch"
    assert sidecar["qualification_failure_evidence"]["field"] == "scenario_id"


def test_incomplete_corridor_envelope_fails_closed() -> None:
    sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.incomplete-envelope.synthetic.v1",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=lambda _points, _times: {
            "accepted": True,
            "complete": True,
            "continuous_containment_proved": True,
        },
    )

    assert sidecar["status"] == "FALLBACK"
    assert sidecar["research_eligible"] is False
    assert sidecar["fallback_reason"] == "hard_mask_envelope_unproved"


def test_hard_mask_and_coverage_fail_closed() -> None:
    hard = np.zeros((3, 3), dtype=np.bool_)
    hard[0, 0] = True
    hard_sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.hard.synthetic.v1",
        risk_sampler=_sampler(hard_mask=hard),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )
    assert hard_sidecar["status"] == "FALLBACK"
    assert hard_sidecar["fallback_reason"] == "hard_mask"

    coverage_sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.coverage.synthetic.v1",
        risk_sampler=_sampler(end_hours=1),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )
    assert coverage_sidecar["status"] == "FALLBACK"
    assert coverage_sidecar["fallback_reason"] == "risk_sampling_incomplete"


def test_speed_and_eta_fail_closed() -> None:
    slow_sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.speed.synthetic.v1",
        risk_sampler=_sampler(environment_speed_factor=np.full((3, 3), 0.1, dtype=np.float32)),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )
    assert slow_sidecar["status"] == "FALLBACK"
    assert slow_sidecar["fallback_reason"] == "speed_not_navigable"

    eta_sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.eta.synthetic.v1",
        risk_sampler=_sampler(),
        vessel_model=_model(),
        corridor_validator=_corridor,
        eta_max_iterations=1,
    )
    assert eta_sidecar["status"] == "FALLBACK"
    assert eta_sidecar["fallback_reason"] == "eta_not_converged"


def test_risk_increase_is_rejected_without_widening_the_display_constraint() -> None:
    risk = np.zeros((3, 3), dtype=np.float32)
    risk[1, 1] = 1.0
    sidecar = build_qualified_route_smoothing_sidecar(
        _route(),
        experiment_id="c.route-smoothing.risk-increase.synthetic.v1",
        risk_sampler=_sampler(risk=risk),
        vessel_model=_model(),
        corridor_validator=_corridor,
    )
    assert sidecar["status"] == "FALLBACK"
    assert sidecar["fallback_reason"] == "risk_increased"
