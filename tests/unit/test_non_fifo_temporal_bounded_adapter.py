"""P0.2-M5 proof-bound non-FIFO adapter safety checks."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import PlanningRequest
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    NonFifoTemporalResearchCheckpoint,
    create_non_fifo_temporal_bounded_session,
    restore_non_fifo_temporal_bounded_session,
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(*, certificate=None) -> TemporalLabelAStar:
    risk = np.zeros((2, 3), dtype=np.float32)
    latitudes = (0.0, 0.05)
    longitudes = (0.0, 0.05, 0.1)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            risk,
            risk_id=f"risk-{index}",
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index in (0, 1, 8)
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
        limits=TemporalSearchLimits(),
        state_bound_certificate=certificate,
    )

    def evaluate(start, end, departure_time, previous_heading, _request, cost_model):
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=0.25,
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
            arrival_time=departure_time + timedelta(hours=0.25),
            heading_degrees=heading,
            speed_knots=10.0,
            distance_km=distance,
            risk_score=0.2,
            maximum_risk=0.2,
            confidence=0.85,
            cost=cost,
            source_risk_ids=("scripted-source",),
        )

    planner._injected_edge_evaluator = evaluate
    return planner


def _request(**changes: object) -> PlanningRequest:
    values: dict[str, object] = {
        "start": (0, 0),
        "goal": (0, 2),
        "departure_time": T0,
        "objective": ObjectiveMode.RECOMMENDED,
        "use_heuristic": False,
        "maximum_elapsed": timedelta(hours=6),
    }
    values.update(changes)
    return PlanningRequest(**values)


def _certificate(planner: TemporalLabelAStar, request: PlanningRequest, *, scope=None):
    active_scope = planner.temporal_scope(request) if scope is None else scope
    return TemporalStateBoundCertificate.certified(
        active_scope,
        allowed_nodes=((0, 0), (0, 1), (0, 2), (1, 1), (1, 2)),
        excluded_nodes=((1, 0),),
        proof_digest="proof-bound-test-v1",
    )


def test_bounded_adapter_matches_unbounded_reference_and_observes_pruning() -> None:
    request = _request()
    baseline_planner = _planner()
    baseline = run_non_fifo_temporal_search(baseline_planner, request)

    bound_scope_planner = _planner()
    certificate = _certificate(bound_scope_planner, request)
    bounded_planner = _planner(certificate=certificate)
    bounded = run_non_fifo_temporal_bounded_search(
        bounded_planner,
        request,
        certificate,
    )

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.semantic_digest == baseline.semantic_digest
    assert bounded.planning_result is not None
    assert bounded.planning_result.nodes == baseline.planning_result.nodes
    assert bounded.diagnostics.dominance_pruned == 0
    assert bounded.diagnostics.state_bound_rejected == 0
    assert bounded.diagnostics.state_bound_checks > 0
    assert bounded.diagnostics.state_bound_pruned > 0


def test_unbounded_adapter_still_rejects_installed_state_bound() -> None:
    request = _request()
    probe = _planner()
    certificate = _certificate(probe, request)
    planner = _planner(certificate=certificate)

    with pytest.raises(NonFifoTemporalAdapterError, match="state-bound certificate"):
        run_non_fifo_temporal_search(planner, request)


def test_bounded_adapter_checkpoint_restore_keeps_bound_digest_and_semantics() -> None:
    request = _request()
    scope_probe = _planner()
    certificate = _certificate(scope_probe, request)
    planner = _planner(certificate=certificate)
    full = run_non_fifo_temporal_bounded_search(planner, request, certificate)

    session = create_non_fifo_temporal_bounded_session(planner, request, certificate)
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert isinstance(checkpoint, NonFifoTemporalResearchCheckpoint)
    assert checkpoint.state_bound_policy_digest == certificate.digest

    restored = restore_non_fifo_temporal_bounded_session(
        planner,
        checkpoint,
        request,
        certificate,
    )
    while True:
        resumed = restored.advance(expansion_slice=1)
        if resumed is not None:
            break

    assert full.status is NonFifoSearchStatus.GOAL_FOUND
    assert resumed.status is NonFifoSearchStatus.GOAL_FOUND
    assert resumed.semantic_digest == full.semantic_digest
    assert resumed.diagnostics.state_bound_pruned == full.diagnostics.state_bound_pruned
    assert resumed.diagnostics.state_bound_checks == full.diagnostics.state_bound_checks


def test_scope_mismatch_fails_closed_without_pruning() -> None:
    request = _request()
    probe = _planner()
    scope = probe.temporal_scope(request)
    mismatched_scope = type(scope).from_mapping({**scope.mapping, "scope_revision": "drift"})
    certificate = _certificate(probe, request, scope=mismatched_scope)
    planner = _planner(certificate=certificate)

    result = run_non_fifo_temporal_bounded_search(planner, request, certificate)

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert result.reason == "state_bound_rejected"
    assert result.planning_result is None
    assert result.diagnostics.state_bound_pruned == 0
    assert result.diagnostics.state_bound_rejected > 0


def test_certificate_digest_mismatch_is_rejected_before_search() -> None:
    request = _request()
    probe = _planner()
    certificate = _certificate(probe, request)
    planner = _planner(certificate=certificate)
    other_scope = type(certificate.scope).from_mapping(
        {**certificate.scope.mapping, "proof_revision": "different"}
    )
    different = replace(certificate, scope=other_scope)

    with pytest.raises(NonFifoTemporalAdapterError, match="certificate digest mismatch"):
        run_non_fifo_temporal_bounded_search(planner, request, different)


def test_non_fifo_bound_does_not_enable_dominance() -> None:
    request = _request()
    probe = _planner()
    certificate = _certificate(probe, request)
    planner = _planner(certificate=certificate)

    result = run_non_fifo_temporal_bounded_search(planner, request, certificate)

    assert result.diagnostics.dominance_policy == "none"
    assert result.diagnostics.dominance_checks == 0
    assert result.diagnostics.dominance_pruned == 0
