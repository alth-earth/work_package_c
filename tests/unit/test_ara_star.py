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
from arctic_route_planning.planners.errors import NoRouteError, PlanningCancelled
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


def _planner_from_frames(
    risks: tuple[np.ndarray, ...],
    *,
    hard_masks: tuple[np.ndarray, ...] | None = None,
) -> AnytimeRepairingAStar:
    rows, columns = risks[0].shape
    latitudes = tuple(index * 0.05 for index in range(rows))
    longitudes = tuple(index * 0.05 for index in range(columns))
    if hard_masks is None:
        hard_masks = tuple(np.zeros_like(risk, dtype=np.bool_) for risk in risks)
    times = tuple(T0 + timedelta(hours=index) for index in range(len(risks)))
    frames = tuple(
        make_frame(
            time,
            risk,
            risk_id=f"risk-custom-{index}",
            hard_mask=hard_masks[index],
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index, (time, risk) in enumerate(zip(times, risks, strict=True))
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
    objective = kwargs.pop("objective", ObjectiveMode.RECOMMENDED)
    return PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=objective,
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
    assert all(stage.first_solution_elapsed_ms >= 0 for stage in candidate.stages)
    assert len({stage.first_solution_elapsed_ms for stage in candidate.stages}) == 1
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


@pytest.mark.parametrize("objective", tuple(ObjectiveMode))
def test_all_objectives_record_first_solution_diagnostics(objective: ObjectiveMode) -> None:
    planner = _planner()
    candidate = planner.plan(_request(objective=objective))

    assert candidate.final_result.objective is objective
    assert candidate.stages[0].first_solution_cost_hours > 0
    assert candidate.stages[0].first_solution_elapsed_ms >= 0


def test_dynamic_risk_and_hard_mask_remain_fail_closed() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    dynamic = zero.copy()
    dynamic[0, 1:3] = 0.9
    hard = np.zeros((3, 4), dtype=np.bool_)
    hard[0, 1] = True
    hard[2, 1] = True
    planner = _planner_from_frames((zero, dynamic, dynamic), hard_masks=(hard, hard, hard))
    candidate = planner.plan(_request(maximum_risk=0.95))

    assert candidate.final_result.nodes[0] == (1, 0)
    assert candidate.final_result.nodes[-1] == (1, 3)
    assert all(stage.result.maximum_risk <= 0.95 for stage in candidate.stages)


def test_risk_and_time_horizon_constraints_reject_invalid_route() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    high = zero.copy()
    high[:, 1:3] = 0.9
    planner = _planner_from_frames((zero, high, high))

    with pytest.raises(NoRouteError):
        planner.plan(_request(maximum_risk=0.2))
    with pytest.raises(NoRouteError):
        planner.plan(_request(maximum_elapsed=timedelta(seconds=1)))


def test_cancellation_is_cooperative() -> None:
    planner = _planner()
    with pytest.raises(PlanningCancelled, match="cancelled"):
        planner.plan(_request(cancel_check=lambda: True))
