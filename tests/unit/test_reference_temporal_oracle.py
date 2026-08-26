from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import numpy as np
import pytest

from arctic_route_planning.cost import (
    CostModel,
    EdgeCostInput,
    VesselPerformanceModel,
)
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar
from arctic_route_planning.risk import RiskSampler

from .factories import make_frame

_HELPER_SPEC = spec_from_file_location(
    "c_reference_temporal_oracle",
    Path(__file__).parents[1].joinpath("reference_temporal_oracle.py"),
)
if _HELPER_SPEC is None or _HELPER_SPEC.loader is None:
    raise RuntimeError("unable to load the reference temporal oracle")
_HELPER = module_from_spec(_HELPER_SPEC)
sys.modules[_HELPER_SPEC.name] = _HELPER
_HELPER_SPEC.loader.exec_module(_HELPER)

OracleEdge = _HELPER.OracleEdge
ReferenceOracleCancelled = _HELPER.ReferenceOracleCancelled
ReferenceOracleHorizonExceeded = _HELPER.ReferenceOracleHorizonExceeded
ReferenceOracleInvalidEdge = _HELPER.ReferenceOracleInvalidEdge
ReferenceOracleLimitExceeded = _HELPER.ReferenceOracleLimitExceeded
ReferenceOracleLimits = _HELPER.ReferenceOracleLimits
ReferenceOracleNoRoute = _HELPER.ReferenceOracleNoRoute
ReferenceTemporalOracle = _HELPER.ReferenceTemporalOracle


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _search(
    edges: dict[tuple[str, str], tuple[timedelta, float, str | None]],
    *,
    limits: ReferenceOracleLimits | None = None,
    maximum_elapsed: timedelta | None = None,
    cancel_check: object = None,
):
    def neighbours(node: str) -> tuple[str, ...]:
        return tuple(end for start, end in edges if start == node)

    def evaluate(state: tuple[str, str | None, datetime], neighbour: str) -> OracleEdge:
        delta, cost, heading = edges[(state[0], neighbour)]
        return OracleEdge(state[2] + delta, cost, heading)

    return ReferenceTemporalOracle(neighbours, evaluate, limits=limits).search(
        "s",
        "g",
        T0,
        maximum_elapsed=maximum_elapsed,
        cancel_check=cancel_check,  # type: ignore[arg-type]
    )


def test_oracle_source_is_independent_of_production_planners() -> None:
    source = Path(__file__).parents[1].joinpath("reference_temporal_oracle.py").read_text()

    assert "TimeDependentAStar" not in source
    assert "TemporalLabelAStar" not in source
    assert "arctic_route_planning" not in source


def test_fifo_graph_returns_the_shortest_exact_time_path() -> None:
    result = _search(
        {
            ("s", "a"): (timedelta(hours=1), 1.0, "east"),
            ("s", "b"): (timedelta(hours=2), 3.0, "south"),
            ("a", "g"): (timedelta(hours=1), 1.0, "north"),
            ("b", "g"): (timedelta(hours=1), 1.0, "north"),
        }
    )

    assert result.nodes == ("s", "a", "g")
    assert result.total_cost == pytest.approx(2.0)
    assert result.arrival_times == (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2))


def test_static_control_candidate_and_independent_oracle_agree() -> None:
    """Compare all three searches without calling either planner from the oracle."""

    risk = np.zeros((3, 4), dtype=np.float32)
    latitudes = (0.0, 0.05, 0.10)
    longitudes = (0.0, 0.05, 0.10, 0.15)
    frames = tuple(
        make_frame(
            valid_time,
            risk,
            risk_id=f"triad-{index}",
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index, valid_time in enumerate(
            (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=3))
        )
    )
    grid = RegularGrid.from_risk_frame(frames[0])
    sampler = RiskSampler(frames)
    vessel = VesselPerformanceModel(10.0, 2.0, 12.0, 0.2)
    config = PlannerConfig()
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
    )
    control = TimeDependentAStar(grid, sampler, vessel, planner_config=config).plan(request)
    candidate = TemporalLabelAStar(
        grid,
        sampler,
        vessel,
        planner_config=config,
    ).plan(request).planning_result

    speed = vessel.effective_speed(1.0)
    cost_model = CostModel(
        weights=config.weights_for(ObjectiveMode.RECOMMENDED),
        maximum_speed_km_per_hour=vessel.maximum_speed_knots * 1.852,
    )

    def evaluate_static_edge(state, neighbour):
        distance_km = grid.distance_km(state[0], neighbour)
        heading = grid.heading_degrees(state[0], neighbour)
        travel_hours = distance_km / speed.speed_km_per_hour
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance_km,
                travel_hours=travel_hours,
                risk_score=0.0,
                confidence=0.9,
                heading_change_degrees=heading_change_degrees(state[1], heading),
            )
        )
        return OracleEdge(
            state[2] + timedelta(hours=travel_hours),
            cost.total_equivalent_hours,
            heading,
        )

    oracle = ReferenceTemporalOracle(grid.neighbors, evaluate_static_edge).search(
        request.start,
        request.goal,
        request.departure_time,
    )

    assert candidate.nodes == control.nodes == oracle.nodes
    assert candidate.total_cost_hours == pytest.approx(control.total_cost_hours)
    assert candidate.total_cost_hours == pytest.approx(oracle.total_cost)
    assert tuple(step.eta for step in candidate.steps) == oracle.arrival_times


def test_non_fifo_later_departure_can_arrive_earlier() -> None:
    edges = {
        ("s", "u"): (timedelta(minutes=30), 0.5, "in"),
        ("s", "x"): (timedelta(minutes=30), 0.5, "in"),
        ("x", "u"): (timedelta(minutes=30), 0.5, "in"),
        ("u", "g"): (timedelta(hours=3), 3.0, "out"),
    }

    def neighbours(node: str) -> tuple[str, ...]:
        return tuple(end for start, end in edges if start == node)

    def evaluate(state: tuple[str, str | None, datetime], neighbour: str) -> OracleEdge:
        if (state[0], neighbour) == ("u", "g") and state[2] >= T0 + timedelta(hours=1):
            return OracleEdge(state[2] + timedelta(minutes=30), 0.5, "out")
        delta, cost, heading = edges[(state[0], neighbour)]
        return OracleEdge(state[2] + delta, cost, heading)

    result = ReferenceTemporalOracle(neighbours, evaluate).search("s", "g", T0)

    assert result.nodes == ("s", "x", "u", "g")
    assert result.arrival_times[-1] == T0 + timedelta(hours=1, minutes=30)
    assert result.total_cost == pytest.approx(1.5)


def test_same_bucket_different_eta_labels_are_retained() -> None:
    # Both arrivals at ``u`` are in the 60-minute bucket, but only the later
    # arrival sees the faster time-dependent final edge.
    edges = {
        ("s", "early"): (timedelta(minutes=15), 0.0, "in"),
        ("s", "late"): (timedelta(minutes=45), 0.0, "in"),
        ("early", "u"): (timedelta(minutes=1), 0.0, "in"),
        ("late", "u"): (timedelta(minutes=1), 0.0, "in"),
        ("u", "g"): (timedelta(hours=2), 2.0, "out"),
    }

    def neighbours(node: str) -> tuple[str, ...]:
        return tuple(end for start, end in edges if start == node)

    def evaluate(state: tuple[str, str | None, datetime], neighbour: str) -> OracleEdge:
        if (state[0], neighbour) == ("early", "u"):
            return OracleEdge(T0 + timedelta(minutes=16), 0.0, "in")
        if (state[0], neighbour) == ("late", "u"):
            return OracleEdge(T0 + timedelta(minutes=46), 0.0, "in")
        if (state[0], neighbour) == ("u", "g") and state[2] >= T0 + timedelta(minutes=46):
            return OracleEdge(state[2] + timedelta(minutes=15), 0.25, "out")
        delta, cost, heading = edges[(state[0], neighbour)]
        return OracleEdge(state[2] + delta, cost, heading)

    result = ReferenceTemporalOracle(neighbours, evaluate).search("s", "g", T0)

    assert result.nodes == ("s", "late", "u", "g")
    assert result.total_cost == pytest.approx(0.25)
    assert result.metrics.unique_labels == 7


def test_exact_state_keeps_only_the_lower_cost_label() -> None:
    result = _search(
        {
            ("s", "a"): (timedelta(hours=1), 1.0, "in"),
            ("s", "b"): (timedelta(hours=1), 5.0, "in"),
            ("a", "u"): (timedelta(hours=1), 10.0, "same"),
            ("b", "u"): (timedelta(hours=1), 1.0, "same"),
            ("u", "g"): (timedelta(hours=1), 100.0, "out"),
        }
    )

    assert result.nodes == ("s", "b", "u", "g")
    assert result.total_cost == pytest.approx(106.0)
    assert result.metrics.stale_pops == 1
    assert result.metrics.unique_labels == 5


@pytest.mark.parametrize(
    ("limits", "resource"),
    [
        (ReferenceOracleLimits(max_expansions=1), "expansions"),
        (ReferenceOracleLimits(max_labels=1), "labels"),
        (ReferenceOracleLimits(max_queue=1), "queue"),
        (ReferenceOracleLimits(max_edge_evaluations=1), "edge_evaluations"),
    ],
)
def test_each_resource_limit_is_fail_closed(
    limits: ReferenceOracleLimits,
    resource: str,
) -> None:
    with pytest.raises(ReferenceOracleLimitExceeded) as raised:
        _search(
            {
                ("s", "a"): (timedelta(hours=1), 1.0, "in"),
                ("s", "b"): (timedelta(hours=2), 2.0, "in"),
                ("a", "g"): (timedelta(hours=1), 1.0, "out"),
            },
            limits=limits,
        )

    assert raised.value.resource == resource
    assert raised.value.observed > raised.value.limit


def test_cancellation_is_cooperative() -> None:
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(ReferenceOracleCancelled):
        _search(
            {("s", "a"): (timedelta(hours=1), 1.0, "in"), ("a", "g"): (timedelta(1), 1.0, "out")},
            cancel_check=cancel,
        )


def test_no_route_and_horizon_are_distinct() -> None:
    with pytest.raises(ReferenceOracleNoRoute):
        _search({("s", "a"): (timedelta(hours=1), 1.0, "in")})

    with pytest.raises(ReferenceOracleHorizonExceeded):
        _search(
            {
                ("s", "a"): (timedelta(hours=2), 1.0, "in"),
                ("a", "g"): (timedelta(hours=1), 1.0, "out"),
            },
            maximum_elapsed=timedelta(hours=1),
        )


def test_invalid_non_increasing_arrival_is_rejected() -> None:
    with pytest.raises(ReferenceOracleInvalidEdge):
        _search({("s", "g"): (timedelta(0), 1.0, "out")})


def test_counters_and_route_are_deterministic_across_ten_runs() -> None:
    edges = {
        ("s", "a"): (timedelta(hours=1), 1.0, "east"),
        ("s", "b"): (timedelta(hours=1), 1.0, "south"),
        ("a", "g"): (timedelta(hours=1), 1.0, "north"),
        ("b", "g"): (timedelta(hours=1), 1.0, "north"),
    }
    observed = [_search(edges) for _ in range(10)]

    assert all(result.nodes == observed[0].nodes for result in observed)
    assert all(result.total_cost == observed[0].total_cost for result in observed)
    discrete_metrics = (
        "expanded_states",
        "generated_labels",
        "unique_labels",
        "queue_peak",
        "edge_evaluations",
        "heap_pushes",
        "heap_pops",
        "stale_pops",
        "horizon_rejections",
    )
    assert all(
        tuple(getattr(result.metrics, name) for name in discrete_metrics)
        == tuple(getattr(observed[0].metrics, name) for name in discrete_metrics)
        for result in observed
    )
