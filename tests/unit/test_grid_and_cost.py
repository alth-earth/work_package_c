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


def test_lower_bound_is_admissible_against_exact_oracle() -> None:
    """C-ALG-04: A* heuristic never exceeds the exact remaining cost.

    On a small grid the zero-heuristic run is an exact Dijkstra oracle.  For
    every node on the exact route, the admissible heuristic from that node
    must stay at or below the true cheapest remaining cost.  We reconstruct
    the remaining cost along the exact route: the oracle's accumulated
    equivalent-hours from the node to the goal.
    """
    from datetime import UTC, datetime, timedelta

    from arctic_route_planning.cost import VesselPerformanceModel
    from arctic_route_planning.domain.models import ObjectiveMode
    from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
    from arctic_route_planning.risk import RiskSampler

    from .factories import make_frame

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    times = (t0, t0 + timedelta(hours=1), t0 + timedelta(hours=3))
    risk = np.zeros((3, 4), dtype=np.float32)
    # asymmetric field so the cheapest path is not the straight one
    risk[1, 1] = 0.9
    risk[1, 2] = 0.9
    frames = tuple(
        make_frame(
            valid_time,
            risk,
            risk_id=f"risk-{index}",
            latitudes=(0.0, 0.05, 0.10),
            longitudes=(0.0, 0.05, 0.10, 0.15),
        )
        for index, valid_time in enumerate(times)
    )
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    planner = TimeDependentAStar(grid, sampler, vessel)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=t0,
        objective=ObjectiveMode.RECOMMENDED,
    )
    exact = planner.plan(
        PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=request.objective,
            use_heuristic=False,
        )
    )
    cost_model = planner._cost_model(request.objective)
    # Walk the exact route backward, accumulating the true remaining cost from
    # each node to the goal.  The heuristic from that node must never exceed it.
    remaining = 0.0
    for step in reversed(exact.steps):
        heuristic = planner._heuristic(step.node, request.goal, cost_model, request)
        assert heuristic <= remaining + 1e-6, (
            f"heuristic {heuristic:.6f} exceeds exact remaining cost "
            f"{remaining:.6f} at node {step.node}"
        )
        if step.edge_cost is not None:
            remaining += step.edge_cost.total_equivalent_hours
    assert exact.total_cost_hours > 0.0
