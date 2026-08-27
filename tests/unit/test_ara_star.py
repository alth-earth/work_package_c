"""M0 semantic checks for the internal ARA* feasibility candidate."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners.ara_star import (
    AnytimeRepairingAStar,
    AraSearchLimitExceeded,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(*, rows: int = 3, columns: int = 4) -> AnytimeRepairingAStar:
    risk = np.zeros((rows, columns), dtype=np.float32)
    latitudes = tuple(index * 0.05 for index in range(rows))
    longitudes = tuple(index * 0.05 for index in range(columns))
    frames = tuple(
        make_frame(
            time,
            risk,
            risk_id=f"risk-{index}",
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index, time in enumerate((T0, T0 + timedelta(hours=1), T0 + timedelta(hours=3)))
    )
    sampler = RiskSampler(frames)
    grid = RegularGrid(latitudes=latitudes, longitudes=longitudes)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return AnytimeRepairingAStar(grid, sampler, vessel)


def _request(**kwargs) -> PlanningRequest:
    return PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
        **kwargs,
    )


def test_fixed_schedule_produces_monotonic_incumbents() -> None:
    planner = _planner()
    candidate = planner.plan(_request())

    assert tuple(stage.epsilon for stage in candidate.stages) == (2.5, 2.0, 1.5, 1.0)
    costs = [stage.result.total_cost_hours for stage in candidate.stages]
    assert costs == sorted(costs, reverse=True)
    assert all(
        stage.first_solution_cost_hours >= stage.result.total_cost_hours
        for stage in candidate.stages
    )
    assert all(stage.lower_bound_hours >= 0 for stage in candidate.stages)
    assert all(stage.observed_gap >= 0 for stage in candidate.stages)
    assert candidate.final_result.steps[-1].eta > T0


def test_epsilon_one_matches_control_on_static_fixture() -> None:
    planner = _planner()
    control = TimeDependentAStar(planner.grid, planner.risk_sampler, planner.vessel_model)
    candidate = planner.plan(_request()).final_result
    baseline = control.plan(_request())

    assert candidate.nodes == baseline.nodes
    assert candidate.total_cost_hours == pytest.approx(baseline.total_cost_hours)
    assert candidate.distance_km == pytest.approx(baseline.distance_km)
    assert candidate.travel_hours == pytest.approx(baseline.travel_hours)


def test_schedule_requires_nonincreasing_values_and_final_one() -> None:
    planner = _planner()
    with pytest.raises(ValueError, match="non-increasing"):
        planner.plan(_request(), epsilon_schedule=(1.0, 2.0, 1.0))
    with pytest.raises(ValueError, match="terminate"):
        planner.plan(_request(), epsilon_schedule=(2.0, 1.5))


def test_expansion_limit_fails_closed() -> None:
    planner = _planner()
    with pytest.raises(AraSearchLimitExceeded, match="max_expansions=1"):
        planner.plan(_request(max_expansions=1))
