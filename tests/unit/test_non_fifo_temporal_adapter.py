"""P0.2-M2 checks for the actual temporal non-FIFO research adapter."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import PlanningRequest
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame

_REFERENCE_SPEC = spec_from_file_location(
    "c_non_fifo_temporal_adapter_reference",
    Path(__file__).parents[1].joinpath("reference_temporal_oracle.py"),
)
if _REFERENCE_SPEC is None or _REFERENCE_SPEC.loader is None:
    raise RuntimeError("unable to load the reference temporal oracle")
_REFERENCE = module_from_spec(_REFERENCE_SPEC)
sys.modules[_REFERENCE_SPEC.name] = _REFERENCE
_REFERENCE_SPEC.loader.exec_module(_REFERENCE)
OracleEdge = _REFERENCE.OracleEdge
reference_dijkstra = _REFERENCE.reference_dijkstra


def _planner(
    *,
    rows: int = 2,
    columns: int = 3,
    limits: TemporalSearchLimits | None = None,
    edge_evaluator=None,
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
        for index, time in enumerate((T0, T0 + timedelta(hours=1), T0 + timedelta(hours=8)))
    )
    planner = TemporalLabelAStar(
        RegularGrid(
            latitudes=latitudes,
            longitudes=longitudes,
            allow_diagonal=False,
        ),
        RiskSampler(frames),
        VesselPerformanceModel(
            economic_speed_knots=10.0,
            minimum_steerage_speed_knots=2.0,
            maximum_speed_knots=12.0,
            minimum_speed_factor=0.2,
        ),
        limits=limits,
        edge_evaluator=edge_evaluator,
    )
    return planner


def _scripted_edge(planner: TemporalLabelAStar, hours_for_edge):
    def evaluate(start, end, departure_time, previous_heading, _request, cost_model):
        hours = float(hours_for_edge(start, end, departure_time))
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=0.2,
                confidence=0.85,
                heading_change_degrees=heading_change_degrees(
                    previous_heading,
                    heading,
                ),
            )
        )
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=departure_time + timedelta(hours=hours),
            heading_degrees=heading,
            speed_knots=10.0,
            distance_km=distance,
            risk_score=0.2,
            maximum_risk=0.2,
            confidence=0.85,
            cost=cost,
            source_risk_ids=("scripted-source",),
        )

    return evaluate


def _research_request(**changes: object) -> PlanningRequest:
    values: dict[str, object] = {
        "start": (0, 0),
        "goal": (0, 2),
        "departure_time": T0,
        "objective": ObjectiveMode.RECOMMENDED,
        "use_heuristic": False,
        "maximum_elapsed": timedelta(hours=6),
    }
    values.update(changes)
    return PlanningRequest(
        **values,
    )


def test_adapter_uses_actual_session_and_preserves_business_evidence() -> None:
    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(
        planner,
        lambda _start, _end, _departure: 0.25,
    )
    request = _research_request()

    first = run_non_fifo_temporal_search(planner, request)
    second = run_non_fifo_temporal_search(planner, request)

    assert first.status is NonFifoSearchStatus.GOAL_FOUND
    assert first.planning_result is not None
    assert first.semantic_digest == second.semantic_digest
    assert first.session_id == second.session_id
    assert first.diagnostics.dominance_pruned == 0
    assert first.diagnostics.state_bound_pruned == 0
    assert first.diagnostics.dominance_policy == "none"
    assert first.business_evidence[-1].recommended_speed_knots == 10.0
    assert first.business_evidence[-1].edge_risk_score == pytest.approx(0.2)
    assert first.business_evidence[-1].edge_confidence == pytest.approx(0.85)
    assert first.business_evidence[-1].source_risk_ids == ("scripted-source",)
    assert first.business_evidence[-1].edge_cost is not None


def test_adapter_keeps_exact_arrivals_for_a_non_fifo_suffix() -> None:
    planner = _planner()

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

    planner._injected_edge_evaluator = _scripted_edge(planner, durations)
    result = run_non_fifo_temporal_search(planner, _research_request())

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.planning_result is not None
    assert result.planning_result.nodes == (
        (0, 0),
        (1, 0),
        (0, 0),
        (0, 1),
        (0, 2),
    )
    assert result.planning_result.steps[-1].eta == T0 + timedelta(hours=0.5)


def test_adapter_matches_independent_zero_heuristic_oracle() -> None:
    planner = _planner()

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

    planner._injected_edge_evaluator = _scripted_edge(planner, durations)
    request = _research_request()
    result = run_non_fifo_temporal_search(planner, request)
    cost_model = planner._cost_model(request.objective)

    def oracle_edge(state, neighbour):
        node, incoming_code, arrival_time = state
        previous_heading = planner._previous_heading(node, incoming_code)
        hours = durations(node, neighbour, arrival_time)
        distance = planner.grid.distance_km(node, neighbour)
        heading = planner.grid.heading_degrees(node, neighbour)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=0.2,
                confidence=0.85,
                heading_change_degrees=heading_change_degrees(
                    previous_heading,
                    heading,
                ),
            )
        )
        return OracleEdge(
            arrival_time + timedelta(hours=hours),
            cost.total_equivalent_hours,
            (neighbour[0] - node[0], neighbour[1] - node[1]),
        )

    oracle = reference_dijkstra(
        request.start,
        request.goal,
        request.departure_time,
        planner.grid.neighbors,
        oracle_edge,
        maximum_elapsed=request.maximum_elapsed,
    )

    assert result.planning_result is not None
    assert result.planning_result.nodes == oracle.nodes
    assert tuple(step.eta for step in result.planning_result.steps) == oracle.arrival_times
    assert result.planning_result.total_cost_hours == pytest.approx(oracle.total_cost)


def test_adapter_maps_resource_cancel_and_evaluator_failures_without_routes() -> None:
    limited = _planner(limits=TemporalSearchLimits(max_expansions=1))
    limited._injected_edge_evaluator = _scripted_edge(
        limited,
        lambda _start, _end, _departure: 0.25,
    )
    limited_result = run_non_fifo_temporal_search(limited, _research_request())
    assert limited_result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert limited_result.planning_result is None
    assert limited_result.semantic_digest is None

    cancelled = _planner()
    cancelled._injected_edge_evaluator = _scripted_edge(
        cancelled,
        lambda _start, _end, _departure: 0.25,
    )
    cancelled_result = run_non_fifo_temporal_search(
        cancelled,
        _research_request(cancel_check=lambda: True),
    )
    assert cancelled_result.status is NonFifoSearchStatus.CANCELLED
    assert cancelled_result.planning_result is None

    failed = _planner(edge_evaluator=lambda *_args: (_ for _ in ()).throw(ValueError("boom")))
    failed_result = run_non_fifo_temporal_search(failed, _research_request())
    assert failed_result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert failed_result.planning_result is None
    assert failed_result.error_type == "ValueError"

    horizon = _planner()
    horizon._injected_edge_evaluator = _scripted_edge(
        horizon,
        lambda _start, _end, _departure: 100.0,
    )
    horizon_result = run_non_fifo_temporal_search(
        horizon,
        _research_request(maximum_elapsed=timedelta(hours=1)),
    )
    assert horizon_result.status is NonFifoSearchStatus.EXHAUSTED
    assert horizon_result.reason == "horizon_exceeded"
    assert horizon_result.planning_result is None


def test_adapter_rejects_heuristic_dominance_and_state_bound_modes() -> None:
    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(
        planner,
        lambda _start, _end, _departure: 0.25,
    )
    with pytest.raises(NonFifoTemporalAdapterError, match="use_heuristic=False"):
        run_non_fifo_temporal_search(
            planner,
            replace(_research_request(), use_heuristic=True),
        )

    disabled_policy = planner.dominance_policy
    planner.dominance_policy = SimpleNamespace(enabled=True)
    with pytest.raises(NonFifoTemporalAdapterError, match=r"DominancePolicy\.disabled"):
        run_non_fifo_temporal_search(planner, _research_request())

    planner.dominance_policy = disabled_policy
    planner.state_bound_certificate = SimpleNamespace()
    with pytest.raises(NonFifoTemporalAdapterError, match="state-bound certificate"):
        run_non_fifo_temporal_search(planner, _research_request())


def test_adapter_rejects_identity_drift_as_invalid_evidence() -> None:
    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(
        planner,
        lambda _start, _end, _departure: 0.25,
    )
    request = _research_request()
    session_identity = TemporalSessionIdentity.from_planner(planner, request)
    drifted = replace(session_identity, dominance_policy_digest="drifted")

    with pytest.raises(NonFifoTemporalAdapterError, match="identity fence"):
        run_non_fifo_temporal_search(planner, request, identity=drifted)


def test_adapter_is_not_exported_as_a_formal_planner_api() -> None:
    import arctic_route_planning.planners as planners

    assert not hasattr(planners, "run_non_fifo_temporal_search")
