"""Focused P0 checks for the internal exact-arrival-time candidate."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import NoRouteError, PlanningCancelled, PlanningRequest
from arctic_route_planning.planners.eta_refinement import EtaRefinementError
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabel,
    TemporalLabelAStar,
    TemporalSearchLimitExceeded,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(
    *,
    rows: int = 3,
    columns: int = 4,
    edge_evaluator=None,
    limits: TemporalSearchLimits | None = None,
    allow_diagonal: bool = True,
) -> TemporalLabelAStar:
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
    grid = RegularGrid(
        latitudes=latitudes,
        longitudes=longitudes,
        allow_diagonal=allow_diagonal,
    )
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return TemporalLabelAStar(
        grid,
        sampler,
        vessel,
        edge_evaluator=edge_evaluator,
        limits=limits,
    )


def _scripted_edge(planner: TemporalLabelAStar, hours: float):
    def evaluate(start, end, departure_time, previous_heading, request, cost_model):
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=0.0,
                confidence=0.9,
                heading_change_degrees=heading_change_degrees(previous_heading, heading),
            )
        )
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=departure_time + timedelta(hours=hours),
            heading_degrees=heading,
            speed_knots=10.0,
            distance_km=distance,
            risk_score=0.0,
            maximum_risk=0.0,
            confidence=0.9,
            cost=cost,
            source_risk_ids=("scripted",),
        )

    return evaluate


def _mapped_edge(planner: TemporalLabelAStar, durations):
    def evaluate(start, end, departure_time, previous_heading, request, cost_model):
        hours = durations(start, end, departure_time)
        return _scripted_edge(planner, hours)(
            start,
            end,
            departure_time,
            previous_heading,
            request,
            cost_model,
        )

    return evaluate


def test_exact_temporal_label_keeps_arrival_time_in_identity() -> None:
    first = TemporalLabel((1, 1), (0, 1), T0 + timedelta(minutes=1), 2.0)
    second = TemporalLabel((1, 1), (0, 1), T0 + timedelta(minutes=2), 1.0)

    assert first.state != second.state
    assert first.state[0:2] == second.state[0:2]


def test_constant_grid_returns_a_complete_deterministic_route() -> None:
    planner = _planner()
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
    )

    first = planner.plan(request)
    second = planner.plan(request)

    assert first.nodes == second.nodes
    assert first.planning_result.total_cost_hours == pytest.approx(
        second.planning_result.total_cost_hours
    )
    assert first.steps[-1].eta == second.steps[-1].eta
    assert first.steps[-1].eta > T0
    assert first.diagnostics.fifo_status == "FIFO_UNCERTAIN"


def test_candidate_honours_expansion_limit_without_partial_result() -> None:
    planner = _planner(limits=TemporalSearchLimits(max_expansions=1))
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    with pytest.raises(TemporalSearchLimitExceeded, match="expansions=1"):
        planner.plan(request)


@pytest.mark.parametrize(
    ("limit_name", "message"),
    (
        ("max_labels", "labels=1"),
        ("max_queue", "queue=1"),
        ("max_edge_evaluations", "edge evaluations=1"),
    ),
)
def test_each_non_expansion_limit_fails_closed(limit_name: str, message: str) -> None:
    planner = _planner(limits=TemporalSearchLimits(**{limit_name: 1}))
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    with pytest.raises(TemporalSearchLimitExceeded, match=message):
        planner.plan(request)


def test_candidate_cooperatively_cancels() -> None:
    planner = _planner()
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        cancel_check=lambda: True,
    )

    with pytest.raises(PlanningCancelled, match="cancelled"):
        planner.plan(request)


def test_scripted_equal_edges_are_reproducible() -> None:
    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(planner, 0.25)
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    first = planner.plan(request)
    second = planner.plan(request)

    assert first.nodes == second.nodes
    assert first.steps == second.steps


def test_goal_incumbent_waits_for_active_open_lower_bound() -> None:
    planner = _planner(rows=2, columns=3, allow_diagonal=False)

    def durations(start, end, _departure_time):
        return {
            ((0, 0), (0, 1)): 0.1,
            ((0, 1), (0, 2)): 5.0,
            ((0, 0), (1, 0)): 0.1,
            ((1, 0), (1, 1)): 0.1,
            ((1, 1), (1, 2)): 0.1,
            ((1, 2), (0, 2)): 0.1,
        }.get((start, end), 10.0)

    planner._injected_edge_evaluator = _mapped_edge(planner, durations)
    request = PlanningRequest(start=(0, 0), goal=(0, 2), departure_time=T0)

    result = planner.plan(request)

    assert result.nodes == ((0, 0), (1, 0), (1, 1), (1, 2), (0, 2))
    assert result.total_cost_hours < 1.0


def test_same_bucket_exact_arrivals_are_not_cross_dominated() -> None:
    planner = _planner(rows=2, columns=3, allow_diagonal=False)

    def durations(start, end, departure_time):
        if (start, end) == ((0, 0), (0, 1)):
            return 0.1
        if (start, end) == ((1, 0), (0, 0)):
            return 0.2
        if (start, end) == ((0, 1), (0, 2)):
            return 5.0 if departure_time < T0 + timedelta(hours=0.2) else 0.1
        if (start, end) == ((0, 0), (1, 0)):
            return 0.1
        return 100.0

    planner._injected_edge_evaluator = _mapped_edge(planner, durations)
    request = PlanningRequest(
        start=(0, 0),
        goal=(0, 2),
        departure_time=T0,
        time_bucket_size=timedelta(hours=1),
    )

    result = planner.plan(request)

    assert result.nodes == ((0, 0), (1, 0), (0, 0), (0, 1), (0, 2))
    assert result.total_cost_hours < 1.0


def test_static_control_and_candidate_agree_on_constant_grid() -> None:
    from arctic_route_planning.planners import TimeDependentAStar

    planner = _planner()
    control = TimeDependentAStar(planner.grid, planner.risk_sampler, planner.vessel_model)
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    candidate = planner.plan(request).planning_result
    baseline = control.plan(request)

    assert candidate.nodes == baseline.nodes
    assert candidate.total_cost_hours == pytest.approx(baseline.total_cost_hours)
    assert candidate.distance_km == pytest.approx(baseline.distance_km)


def test_eta_failure_rejects_edge_and_does_not_return_partial_route() -> None:
    def failing_edge(*_args):
        raise EtaRefinementError("cycle", {"message": "synthetic cycle"})

    planner = _planner(rows=2, columns=2, edge_evaluator=failing_edge)
    request = PlanningRequest(start=(0, 0), goal=(0, 1), departure_time=T0)

    with pytest.raises(NoRouteError, match="no exact-arrival route"):
        planner.plan(request)
