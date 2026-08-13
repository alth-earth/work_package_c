from __future__ import annotations

import numpy as np
import pytest

from arctic_route_planning.cost import (
    CostModel,
    EdgeCostInput,
    UnnavigableSpeedError,
    VesselPerformanceModel,
)
from arctic_route_planning.domain.models import (
    CostWeights,
    GeoPoint,
    ModelCalibrationStatus,
    VesselModelConfig,
)
from arctic_route_planning.grid import RegularGrid, haversine_km


def test_regular_grid_has_eight_neighbors_and_geodesic_distance() -> None:
    grid = RegularGrid(
        latitudes=(0.0, 1.0, 2.0),
        longitudes=(0.0, 1.0, 2.0),
    )

    assert len(tuple(grid.neighbors((1, 1)))) == 8
    assert len(tuple(grid.neighbors((0, 0)))) == 3
    assert grid.distance_km((0, 0), (0, 1)) == pytest.approx(111.195, rel=1e-3)
    assert haversine_km(GeoPoint(0.0, 0.0), GeoPoint(0.0, 1.0)) == pytest.approx(
        111.195,
        rel=1e-3,
    )


def test_edge_sampling_includes_endpoints_and_interior() -> None:
    grid = RegularGrid(latitudes=(0.0, 1.0), longitudes=(0.0, 1.0))
    points = grid.edge_sample_points((0, 0), (1, 1), minimum_samples=3)

    assert points == (
        GeoPoint(0.0, 0.0),
        GeoPoint(0.5, 0.5),
        GeoPoint(1.0, 1.0),
    )


def test_snap_is_explicit_limited_and_can_be_component_constrained() -> None:
    grid = RegularGrid(
        latitudes=(0.0, 1.0, 2.0),
        longitudes=(0.0, 1.0, 2.0),
    )
    hard = np.zeros(grid.shape, dtype=np.bool_)
    hard[:, 1] = True
    left_component = grid.connected_component((1, 0), hard)

    snapped = grid.snap_to_navigable(
        GeoPoint(1.9, 1.0),
        hard,
        max_adjustment_km=300.0,
        required_component=left_component,
    )

    assert snapped.node == (1, 0)
    assert snapped.adjustment_km > 200.0
    with pytest.raises(ValueError, match="no navigable node"):
        grid.snap_to_navigable(
            GeoPoint(1.9, 1.0),
            hard,
            max_adjustment_km=50.0,
            required_component=left_component,
        )


def test_vessel_model_applies_environment_factor_without_risk_double_counting() -> None:
    configuration = VesselModelConfig(
        schema_version="c.vessel-model-config.v1",
        vessel_profile_id="demo-bulker",
        vessel_profile_version="1.0.0",
        calibration_status=ModelCalibrationStatus.DEMO_UNVALIDATED,
        under_keel_clearance_m=2.0,
        minimum_steerage_speed_knots=3.0,
        economic_speed_knots=13.5,
        maximum_speed_knots=15.0,
        minimum_speed_factor=0.2,
        turn_radius_m=800.0,
        bathymetry_hard_constraint_enabled=False,
        source_notes="unvalidated fixture",
    )
    model = VesselPerformanceModel.from_configuration(configuration)

    estimate = model.effective_speed(0.5)

    assert estimate.speed_knots == pytest.approx(6.75)
    assert estimate.speed_km_per_hour == pytest.approx(12.501)
    with pytest.raises(UnnavigableSpeedError):
        model.effective_speed(0.1)


def test_cost_components_are_all_expressed_as_equivalent_hours() -> None:
    model = CostModel(
        weights=CostWeights(
            travel_time=1.0,
            risk=2.0,
            distance=0.5,
            turn=4.0,
            uncertainty=3.0,
        ),
        maximum_speed_km_per_hour=20.0,
    )

    result = model.evaluate(
        EdgeCostInput(
            distance_km=100.0,
            travel_hours=2.0,
            risk_score=0.5,
            confidence=0.8,
            heading_change_degrees=90.0,
        )
    )

    assert result.risk_exposure_hours == pytest.approx(1.0)
    assert result.distance_equivalent_hours == pytest.approx(5.0)
    assert result.turn_equivalent_hours == pytest.approx(0.125)
    assert result.low_confidence_hours == pytest.approx(0.4)
    assert result.total_equivalent_hours == pytest.approx(8.2)
    assert model.lower_bound(100.0) == pytest.approx(7.5)
