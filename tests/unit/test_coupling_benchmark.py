from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.coupling_benchmark import benchmark_planning_on_risk_frames
from arctic_route_planning.domain.models import (
    ModelCalibrationStatus,
    PlannerConfig,
    VesselModelConfig,
)
from arctic_route_planning.risk import SampleCacheMode

from .factories import T0, make_frame


def test_bc_coupling_benchmark_preserves_real_planner_result() -> None:
    risk = np.full((3, 5), 0.1, dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            risk,
            risk_id=f"risk-{index}",
            environment_speed_factor=np.full(risk.shape, 0.9, dtype=np.float32),
            latitudes=(70.0, 70.01, 70.02),
            longitudes=(20.0, 20.01, 20.02, 20.03, 20.04),
        )
        for index in range(5)
    )
    summary = benchmark_planning_on_risk_frames(
        frames,
        start=(1, 0),
        goal=(1, 4),
        planner_config=PlannerConfig(),
        vessel_config=VesselModelConfig(
            schema_version="c.vessel-model-config.v1",
            vessel_profile_id="vessel-demo",
            vessel_profile_version="1.0.0",
            economic_speed_knots=10.0,
            minimum_steerage_speed_knots=2.0,
            maximum_speed_knots=12.0,
            minimum_speed_factor=0.2,
            turn_radius_m=1000.0,
            under_keel_clearance_m=1.0,
            bathymetry_hard_constraint_enabled=False,
            calibration_status=ModelCalibrationStatus.DEMO_UNVALIDATED,
            source_notes="synthetic unit fixture",
        ),
    )

    assert summary["status"] == "SUCCESS"
    assert summary["grid_nodes"] == 15
    assert summary["route_nodes"] == 5
    assert summary["expanded_states"] > 0
    assert summary["edge_geometry_cache"]["misses"] > 0
    assert summary["risk_sample_cache"]["mode"] == "off"
    assert summary["risk_sample_cache"]["total_requests"] > 0
    assert summary["source_schema_versions"] == ["bc.risk-frame.v2"]


def test_bc_coupling_bounded_sample_cache_preserves_route_digest() -> None:
    risk = np.full((3, 5), 0.1, dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            risk,
            risk_id=f"risk-{index}",
            environment_speed_factor=np.full(risk.shape, 0.9, dtype=np.float32),
            latitudes=(70.0, 70.01, 70.02),
            longitudes=(20.0, 20.01, 20.02, 20.03, 20.04),
        )
        for index in range(5)
    )
    vessel = VesselModelConfig(
        schema_version="c.vessel-model-config.v1",
        vessel_profile_id="vessel-demo",
        vessel_profile_version="1.0.0",
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
        turn_radius_m=1000.0,
        under_keel_clearance_m=1.0,
        bathymetry_hard_constraint_enabled=False,
        calibration_status=ModelCalibrationStatus.DEMO_UNVALIDATED,
        source_notes="synthetic unit fixture",
    )

    baseline = benchmark_planning_on_risk_frames(
        frames,
        start=(1, 0),
        goal=(1, 4),
        planner_config=PlannerConfig(),
        vessel_config=vessel,
    )
    cached = benchmark_planning_on_risk_frames(
        frames,
        start=(1, 0),
        goal=(1, 4),
        planner_config=PlannerConfig(),
        vessel_config=vessel,
        sample_cache_mode=SampleCacheMode.BOUNDED_LRU,
        sample_cache_capacity=32,
    )

    assert cached["route_digest"] == baseline["route_digest"]
    assert cached["expanded_states"] == baseline["expanded_states"]
    assert cached["risk_sample_cache"]["cache_hits"] > 0
