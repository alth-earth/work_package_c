from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import (
    EndpointBlockedError,
    PlanningCancelled,
    PlanningRequest,
    TimeDependentAStar,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(frames: tuple[object, ...]) -> TimeDependentAStar:
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return TimeDependentAStar(grid, sampler, vessel)


def _risk_window(
    risks: tuple[np.ndarray, np.ndarray, np.ndarray],
    *,
    hard_masks: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> tuple[object, ...]:
    times = (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=3))
    latitudes = (0.0, 0.05, 0.10)
    longitudes = (0.0, 0.05, 0.10, 0.15)
    if hard_masks is None:
        hard_masks = tuple(np.zeros((3, 4), dtype=np.bool_) for _ in times)
    return tuple(
        make_frame(
            valid_time,
            risk,
            risk_id=f"risk-{index}",
            hard_mask=hard_masks[index],
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index, (valid_time, risk) in enumerate(zip(times, risks, strict=True))
    )


def test_astar_matches_zero_heuristic_dijkstra_on_a_small_grid() -> None:
    zeros = tuple(np.zeros((3, 4), dtype=np.float32) for _ in range(3))
    planner = _planner(_risk_window(zeros))
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
    )

    astar = planner.plan(request)
    dijkstra = planner.plan(
        PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=request.objective,
            use_heuristic=False,
        )
    )

    assert astar.total_cost_hours == pytest.approx(dijkstra.total_cost_hours)
    assert astar.distance_km == pytest.approx(dijkstra.distance_km)
    assert astar.nodes == dijkstra.nodes
    assert astar.metrics.expanded_states <= dijkstra.metrics.expanded_states


def test_future_risk_changes_the_low_risk_route() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    future = zero.copy()
    future[1, 1:3] = 1.0
    static_planner = _planner(_risk_window((zero, zero, zero)))
    dynamic_planner = _planner(_risk_window((zero, future, future)))
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.LOW_RISK,
    )

    static_route = static_planner.plan(request)
    dynamic_route = dynamic_planner.plan(request)
    dynamic_fastest = dynamic_planner.plan(
        PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=ObjectiveMode.FASTEST,
        )
    )

    assert static_route.nodes == ((1, 0), (1, 1), (1, 2), (1, 3))
    assert dynamic_fastest.nodes == static_route.nodes
    assert dynamic_route.nodes != static_route.nodes
    assert any(row != 1 for row, _ in dynamic_route.nodes[1:-1])
    assert dynamic_route.average_risk < dynamic_fastest.average_risk


def test_goal_is_evaluated_at_eta_not_rejected_from_departure_snapshot() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    hard_at_departure = np.zeros((3, 4), dtype=np.bool_)
    hard_at_departure[1, 3] = True
    clear = np.zeros((3, 4), dtype=np.bool_)
    planner = _planner(
        _risk_window(
            (zero, zero, zero),
            hard_masks=(hard_at_departure, clear, clear),
        )
    )

    result = planner.plan(
        PlanningRequest(
            start=(1, 0),
            goal=(1, 3),
            departure_time=T0,
            objective=ObjectiveMode.FASTEST,
        )
    )

    assert result.nodes[-1] == (1, 3)
    assert result.steps[-1].eta > T0 + timedelta(hours=1)


def test_blocked_start_is_rejected_without_implicit_snapping() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    hard = np.zeros((3, 4), dtype=np.bool_)
    hard[1, 0] = True
    planner = _planner(_risk_window((zero, zero, zero), hard_masks=(hard, hard, hard)))

    with pytest.raises(EndpointBlockedError):
        planner.plan(
            PlanningRequest(
                start=(1, 0),
                goal=(1, 3),
                departure_time=T0,
            )
        )


def test_planning_can_be_cancelled_before_search() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))

    with pytest.raises(PlanningCancelled):
        planner.plan(
            PlanningRequest(
                start=(1, 0),
                goal=(1, 3),
                departure_time=T0,
                cancel_check=lambda: True,
            )
        )


def test_plan_candidates_runs_all_three_objectives() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    candidates = planner.plan_candidates(request)

    assert set(candidates) == set(ObjectiveMode)
    assert all(result.objective is mode for mode, result in candidates.items())
