"""Safety and integration checks for the heading-aware heuristic sidecar."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import PlanningRequest
from arctic_route_planning.planners.temporal_heading_heuristic import (
    qualify_heading_heuristic,
)
from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentityMismatch
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner() -> TemporalLabelAStar:
    rows, columns = 3, 4
    risk = np.zeros((rows, columns), dtype=np.float32)
    latitudes = tuple(index * 0.05 for index in range(rows))
    longitudes = tuple(index * 0.05 for index in range(columns))
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            risk,
            risk_id=f"heading-risk-{index}",
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index in (0, 1, 3)
    )
    planner = TemporalLabelAStar(
        RegularGrid(latitudes=latitudes, longitudes=longitudes, allow_diagonal=False),
        RiskSampler(frames),
        VesselPerformanceModel(
            economic_speed_knots=10.0,
            minimum_steerage_speed_knots=2.0,
            maximum_speed_knots=12.0,
            minimum_speed_factor=0.2,
        ),
    )

    def evaluate(start, end, departure_time, previous_heading, _request, cost_model):
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=0.25,
                risk_score=0.1,
                confidence=0.9,
                heading_change_degrees=heading_change_degrees(previous_heading, heading),
            )
        )
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=departure_time + timedelta(hours=0.25),
            heading_degrees=heading,
            speed_knots=10.0,
            distance_km=distance,
            risk_score=0.1,
            maximum_risk=0.1,
            confidence=0.9,
            cost=cost,
            source_risk_ids=("heading-source",),
        )

    planner._injected_edge_evaluator = evaluate
    return planner


def _request() -> PlanningRequest:
    return PlanningRequest(
        start=(0, 0),
        goal=(2, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
        use_heuristic=True,
        maximum_elapsed=timedelta(hours=6),
    )


def _certificate(planner: TemporalLabelAStar, request: PlanningRequest):
    scope = planner.temporal_scope(request)
    nodes = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    return qualify_heading_heuristic(
        scope=scope,
        grid=planner.grid,
        nodes=nodes,
        goal=request.goal,
        cost_model=planner._cost_model(request.objective),
        objective=request.objective.value,
        expected_scope=scope,
    )


def test_heading_certificate_is_complete_and_consistent() -> None:
    planner = _planner()
    request = _request()
    certificate = _certificate(planner, request)

    assert certificate.usable
    assert certificate.scope.matches(planner.temporal_scope(request))
    assert certificate.lower_bound(request.goal, None) == 0.0
    assert len(certificate.universe_states) == len(certificate.lower_bound_map)
    assert certificate.digest


def test_heading_certificate_preserves_semantics_and_can_improve_ordering() -> None:
    request = _request()
    baseline = _planner().plan(request)
    planner = _planner()
    certificate = _certificate(planner, request)
    planner.heading_heuristic_certificate = certificate
    candidate = planner.plan(request)

    assert candidate.planning_result.nodes == baseline.planning_result.nodes
    assert candidate.planning_result.steps == baseline.planning_result.steps
    assert candidate.planning_result.total_cost_hours == baseline.planning_result.total_cost_hours
    assert candidate.diagnostics.heading_heuristic_policy == "certified-heading"
    assert candidate.diagnostics.heading_heuristic_scope_match
    assert candidate.diagnostics.heading_heuristic_rejected == 0
    assert candidate.diagnostics.dominance_pruned == 0
    assert candidate.diagnostics.state_bound_pruned == 0
    assert candidate.diagnostics.expanded_labels <= baseline.diagnostics.expanded_labels


def test_heading_certificate_scope_mismatch_fails_closed() -> None:
    request = _request()
    planner = _planner()
    certificate = _certificate(planner, request)
    mismatched_scope = type(certificate.scope).from_mapping(
        {**certificate.scope.mapping, "heading_revision": "drift"}
    )
    planner.heading_heuristic_certificate = replace(certificate, scope=mismatched_scope)
    result = planner.plan(request)

    assert result.planning_result.nodes == _planner().plan(request).planning_result.nodes
    assert result.diagnostics.heading_heuristic_scope_match is False
    assert result.diagnostics.heading_heuristic_rejected > 0
    assert result.diagnostics.heading_heuristic_rejection_reasons


def test_heading_certificate_checkpoint_binds_digest() -> None:
    request = _request()
    planner = _planner()
    certificate = _certificate(planner, request)
    planner.heading_heuristic_certificate = certificate
    session = planner.create_session(request)
    assert session.advance(expansion_slice=1) is None
    checkpoint = planner.checkpoint_session(session)
    assert checkpoint.identity.heading_heuristic_policy_digest == certificate.digest

    planner.heading_heuristic_certificate = replace(certificate, proof_digest="heading-drift")
    with pytest.raises(TemporalSessionIdentityMismatch, match=r"identity fence|policy digest"):
        planner.restore_session(checkpoint, request)
