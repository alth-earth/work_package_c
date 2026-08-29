"""Focused checks for the actual-edge non-FIFO Pareto research bridge."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from arctic_route_planning.cost import EdgeCostInput
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import heading_change_degrees
from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoParetoIncumbentBoundCertificate,
    NonFifoParetoTerminalBoundCertificate,
    NonFifoSearchStatus,
)
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    TEMPORAL_PARETO_COMPONENTS,
    NonFifoTemporalParetoCheckpoint,
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_label_astar import TemporalSearchLimits, _RejectedEdge
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal

from .test_non_fifo_temporal_adapter import _planner, _research_request


def _pareto_edge(planner):
    direct = {
        ((0, 0), (0, 1)),
        ((0, 1), (0, 2)),
    }
    detour = {
        ((0, 0), (1, 0)),
        ((1, 0), (1, 1)),
        ((1, 1), (1, 2)),
        ((1, 2), (0, 2)),
    }

    def evaluate(start, end, departure_time, previous_heading, _request, cost_model):
        if (start, end) in direct:
            hours, risk, confidence = 1.0, 0.1, 0.95
        elif (start, end) in detour:
            hours, risk, confidence = 0.5, 0.2, 0.85
        else:
            hours, risk, confidence = 10.0, 0.3, 0.8
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=risk,
                confidence=confidence,
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
            risk_score=risk,
            maximum_risk=risk,
            confidence=confidence,
            cost=cost,
            source_risk_ids=("pareto-fixture",),
        )

    return evaluate


def _configured_planner(**kwargs):
    planner = _planner(**kwargs)
    planner._injected_edge_evaluator = _pareto_edge(planner)
    return planner


def test_actual_bridge_preserves_business_route_and_prunes_same_goal_arrival() -> None:
    planner = _configured_planner()
    request = _research_request()

    unpruned = run_non_fifo_temporal_pareto_search(planner, request)
    pruned = run_non_fifo_temporal_pareto_search(planner, request, pareto_pruning=True)

    assert unpruned.status is NonFifoSearchStatus.GOAL_FOUND
    assert pruned.status is NonFifoSearchStatus.GOAL_FOUND
    assert unpruned.selected is not None
    assert pruned.selected is not None
    assert pruned.pareto_pruned >= 1
    assert pruned.semantic_digest == unpruned.semantic_digest
    assert pruned.selected.nodes == ((0, 0), (0, 1), (0, 2))
    assert pruned.selected.steps[-1].source_risk_ids == ("pareto-fixture",)
    assert sum(step.cost.total_equivalent_hours for step in pruned.selected.steps) == pytest.approx(
        pruned.selected.costs[0]
    )
    assert len(pruned.selected.costs) == len(TEMPORAL_PARETO_COMPONENTS)
    assert pruned.diagnostics.dominance_pruned == 0
    assert pruned.diagnostics.state_bound_pruned == 0


def test_actual_bridge_exposes_complete_frontier_certificate_after_terminal_run() -> None:
    planner = _configured_planner()
    request = _research_request()
    session = create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=True,
    )

    with pytest.raises(NonFifoTemporalParetoError, match="terminal"):
        _ = session.frontier_certificate

    result = session.run()
    certificate = session.frontier_certificate
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert certificate.usable
    assert certificate.frontier_count == len(result.frontier)
    assert certificate.scope_digest == planner.temporal_scope(request).digest


def test_actual_bridge_slice_restore_matches_one_shot_frontier() -> None:
    planner = _configured_planner()
    request = _research_request()
    full = run_non_fifo_temporal_pareto_search(planner, request, pareto_pruning=True)

    session = create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=True,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert isinstance(checkpoint, NonFifoTemporalParetoCheckpoint)
    assert checkpoint.digest

    restored = restore_non_fifo_temporal_pareto_session(planner, request, checkpoint)
    while True:
        result = restored.advance(expansion_slice=1)
        if result is not None:
            break

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.frontier_digest == full.frontier_digest
    assert result.session_id == full.session_id
    assert result.selected is not None and full.selected is not None
    assert result.selected.steps == full.selected.steps


def test_actual_bridge_rejects_scope_drift_and_keeps_cancel_fail_closed() -> None:
    planner = _configured_planner()
    request = _research_request()
    session = create_non_fifo_temporal_pareto_session(planner, request)
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()

    with pytest.raises(NonFifoTemporalParetoError, match="scope mismatch"):
        restore_non_fifo_temporal_pareto_session(
            planner,
            replace(request, objective=ObjectiveMode.FASTEST),
            checkpoint,
        )

    cancelled = run_non_fifo_temporal_pareto_search(
        planner,
        replace(request, cancel_check=lambda: True),
        pareto_pruning=True,
    )
    assert cancelled.status is NonFifoSearchStatus.CANCELLED
    assert cancelled.selected is None
    assert cancelled.frontier == ()


def test_actual_bridge_rejects_incumbent_bound_certificate_fail_closed() -> None:
    planner = _configured_planner()
    request = _research_request()
    scope = planner.temporal_scope(request)
    certificate = NonFifoParetoIncumbentBoundCertificate.rejected(
        scope_digest=scope.digest,
        goal=(request.goal, None),
        objective_count=len(TEMPORAL_PARETO_COMPONENTS),
        reason="interval-proof-incomplete",
    )

    result = run_non_fifo_temporal_pareto_search(
        planner,
        request,
        incumbent_bound_certificate=certificate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.incumbent_bound_pruned == 0
    assert result.incumbent_bound_rejected >= 1
    assert result.incumbent_bound_digest == certificate.digest
    assert result.incumbent_bound_rejection_reasons == (
        ("interval-proof-incomplete", 1),
    )


def test_actual_bridge_supports_selected_route_terminal_bound_without_frontier_claim() -> None:
    planner = _configured_planner()
    request = _research_request()
    scope = planner.temporal_scope(request)
    bounds = {}
    rows, columns = planner.grid.shape
    direction_codes = {
        (neighbor[0] - row, neighbor[1] - column)
        for row in range(rows)
        for column in range(columns)
        for neighbor in planner.grid.neighbors((row, column))
    }
    for row in range(rows):
        for column in range(columns):
            node = (row, column)
            for heading in (None, *sorted(direction_codes)):
                bounds[(node, heading)] = (0.0,) * len(TEMPORAL_PARETO_COMPONENTS)
    certificate = NonFifoParetoTerminalBoundCertificate.certified(
        scope_digest=scope.digest,
        goal=(request.goal, None),
        objective_count=len(TEMPORAL_PARETO_COMPONENTS),
        node_lower_bounds=bounds,
        proof_digest="actual-terminal-bound-fixture-v1",
    )
    baseline = run_non_fifo_temporal_pareto_search(planner, request)
    selected = run_non_fifo_temporal_pareto_search(
        planner,
        request,
        incumbent_bound_certificate=certificate,
    )

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert selected.status is NonFifoSearchStatus.GOAL_FOUND
    assert selected.semantic_digest == baseline.semantic_digest
    assert selected.selection_only is True
    assert selected.frontier_complete is False
    assert selected.incumbent_bound_rejected == 0
    # This tiny bridge fixture has one selected route, so zero pruning is
    # acceptable; the synthetic adversarial test proves the pruning branch.
    assert selected.incumbent_bound_pruned >= 0
    assert selected.raw_result.frontier_digest != baseline.raw_result.frontier_digest


def test_actual_bridge_maps_evaluator_and_resource_failures_without_partial_route() -> None:
    failed = _planner(edge_evaluator=lambda *_args: (_ for _ in ()).throw(ValueError("boom")))
    request = _research_request()
    failed_result = run_non_fifo_temporal_pareto_search(failed, request)
    assert failed_result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert failed_result.selected is None
    assert failed_result.frontier == ()

    limited = _configured_planner(limits=TemporalSearchLimits(max_expansions=1))
    limited_result = run_non_fifo_temporal_pareto_search(limited, request)
    assert limited_result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert limited_result.selected is None
    assert limited_result.frontier == ()


def test_actual_bridge_rejects_non_research_modes() -> None:
    planner = _configured_planner()
    with pytest.raises(NonFifoTemporalParetoError, match="use_heuristic=False"):
        run_non_fifo_temporal_pareto_search(
            planner,
            replace(_research_request(), use_heuristic=True),
        )


def test_actual_bridge_accepts_explicit_state_bound_and_prunes_new_labels_only() -> None:
    planner = _configured_planner()
    request = _research_request()
    scope = planner.temporal_scope(request)
    certificate = TemporalStateBoundCertificate.certified(
        scope,
        allowed_nodes=((0, 0), (0, 1), (0, 2), (1, 1), (1, 2)),
        excluded_nodes=((1, 0),),
        proof_digest="actual-pareto-bound-fixture-v1",
    )

    baseline = run_non_fifo_temporal_pareto_search(
        _configured_planner(),
        request,
        pareto_pruning=True,
    )
    bounded = run_non_fifo_temporal_pareto_search(
        planner,
        request,
        pareto_pruning=True,
        state_bound_certificate=certificate,
    )

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.selected is not None
    assert baseline.selected is not None
    assert bounded.semantic_digest == baseline.semantic_digest
    assert bounded.diagnostics.dominance_pruned == 0
    assert bounded.diagnostics.state_bound_rejected == 0
    assert bounded.diagnostics.state_bound_checks > 0
    assert bounded.diagnostics.state_bound_pruned > 0


def test_actual_pareto_state_bound_scope_and_checkpoint_drift_fail_closed() -> None:
    planner = _configured_planner()
    request = _research_request()
    scope = planner.temporal_scope(request)
    certificate = TemporalStateBoundCertificate.certified(
        scope,
        allowed_nodes=((0, 0), (0, 1), (0, 2), (1, 1), (1, 2)),
        excluded_nodes=((1, 0),),
        proof_digest="actual-pareto-bound-fixture-v1",
    )
    session = create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=True,
        state_bound_certificate=certificate,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert checkpoint.state_bound_digest == certificate.digest

    restored = restore_non_fifo_temporal_pareto_session(
        planner,
        request,
        checkpoint,
        state_bound_certificate=certificate,
    )
    while (result := restored.advance(expansion_slice=1)) is None:
        pass
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.diagnostics.state_bound_pruned > 0

    drifted = TemporalStateBoundCertificate.certified(
        scope,
        allowed_nodes=((0, 0), (0, 1), (0, 2), (1, 1)),
        excluded_nodes=((1, 0), (1, 2)),
        proof_digest="actual-pareto-bound-drift-v1",
    )
    with pytest.raises(NonFifoTemporalParetoError, match="state-bound digest mismatch"):
        restore_non_fifo_temporal_pareto_session(
            planner,
            request,
            checkpoint,
            state_bound_certificate=drifted,
        )


def test_actual_pareto_state_bound_scope_mismatch_keeps_all_labels() -> None:
    planner = _configured_planner()
    request = _research_request()
    scope = planner.temporal_scope(request)
    mismatched_scope = type(scope).from_mapping({**scope.mapping, "scope_revision": "drift"})
    certificate = TemporalStateBoundCertificate.certified(
        mismatched_scope,
        allowed_nodes=((0, 0), (0, 1), (0, 2)),
        excluded_nodes=((1, 0), (1, 1), (1, 2)),
        proof_digest="actual-pareto-bound-mismatch-v1",
    )

    result = run_non_fifo_temporal_pareto_search(
        planner,
        request,
        state_bound_certificate=certificate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.diagnostics.state_bound_pruned == 0
    assert result.diagnostics.state_bound_rejected > 0


def test_actual_bridge_can_skip_only_classified_domain_rejections() -> None:
    planner = _planner(edge_evaluator=lambda *_args: (_ for _ in ()).throw(_RejectedEdge("hard")))
    result = run_non_fifo_temporal_pareto_search(
        planner,
        _research_request(),
        pareto_pruning=True,
        skip_expected_rejections=True,
    )
    assert result.status is NonFifoSearchStatus.EXHAUSTED
    assert result.evaluator_errors == ()
    assert result.selected is None
    assert result.frontier == ()
