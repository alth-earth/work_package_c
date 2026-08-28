"""Certified heuristic ordering tests for the C temporal research path."""

from __future__ import annotations

from dataclasses import replace

import pytest

from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    create_non_fifo_temporal_certified_heuristic_session,
    create_non_fifo_temporal_composed_bound_heuristic_session,
    restore_non_fifo_temporal_certified_heuristic_session,
    restore_non_fifo_temporal_composed_bound_heuristic_session,
    run_non_fifo_temporal_certified_heuristic_search,
    run_non_fifo_temporal_composed_bound_heuristic_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_heuristic_bounds import (
    qualify_temporal_heuristic,
)
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

from .test_non_fifo_temporal_bounded_adapter import _planner, _request


def _heuristic(planner, request):
    scope = planner.temporal_scope(request)
    nodes = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=planner.grid.neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=planner.vessel_model.maximum_speed_knots * 1.852,
    )
    certificate = qualify_temporal_heuristic(
        scope=scope,
        topology=topology,
        cost_model=planner._cost_model(request.objective),
        objective=request.objective.value,
        expected_scope=scope,
    )
    return certificate


def _arrival_certificate(planner, request):
    scope = planner.temporal_scope(request)
    allowed = ((0, 0), (0, 1), (0, 2), (1, 1), (1, 2))
    return TemporalStateBoundCertificate.certified(
        scope,
        allowed_nodes=allowed,
        excluded_nodes=((1, 0),),
        proof_digest="proof-composed-bound-test-v1",
        arrival_upper_hours={node: 6.0 for node in allowed},
    )


def test_topological_objective_lower_bound_is_admissible_and_complete() -> None:
    planner = _planner()
    request = replace(_request(), use_heuristic=True)
    certificate = _heuristic(planner, request)

    assert certificate.usable
    assert certificate.scope.matches(planner.temporal_scope(request))
    assert set(certificate.objective_map) == set(certificate.universe_nodes)
    assert certificate.objective_map[request.goal] == 0.0
    assert all(value >= 0.0 for value in certificate.objective_map.values())
    assert certificate.digest


def test_incomplete_or_scope_mismatched_heuristic_fails_closed() -> None:
    planner = _planner()
    request = replace(_request(), use_heuristic=True)
    complete = _heuristic(planner, request)
    incomplete = replace(
        complete,
        objective_lower_hours=complete.objective_lower_hours[:-1],
    )
    planner.heuristic_certificate = incomplete
    with pytest.raises(NonFifoTemporalAdapterError, match="usable certified heuristic"):
        run_non_fifo_temporal_certified_heuristic_search(planner, request, incomplete)

    mismatched_scope = type(complete.scope).from_mapping(
        {**complete.scope.mapping, "heuristic_revision": "mismatch"}
    )
    mismatched = replace(complete, scope=mismatched_scope)
    planner.heuristic_certificate = mismatched
    with pytest.raises(NonFifoTemporalAdapterError, match="scope mismatch"):
        run_non_fifo_temporal_certified_heuristic_search(planner, request, mismatched)


def test_certified_heuristic_matches_zero_heuristic_semantics_without_pruning() -> None:
    request = _request()
    baseline = run_non_fifo_temporal_search(_planner(), request)
    heuristic_request = replace(request, use_heuristic=True)
    planner = _planner()
    certificate = _heuristic(planner, heuristic_request)
    planner.heuristic_certificate = certificate
    candidate = run_non_fifo_temporal_certified_heuristic_search(
        planner,
        heuristic_request,
        certificate,
    )

    assert baseline.planning_result is not None
    assert candidate.planning_result is not None
    assert candidate.semantic_digest == baseline.semantic_digest
    assert candidate.diagnostics.heuristic_policy == "certified"
    assert candidate.diagnostics.heuristic_scope_match
    assert candidate.diagnostics.heuristic_rejected == 0
    assert candidate.diagnostics.dominance_pruned == 0
    assert candidate.diagnostics.state_bound_pruned == 0
    assert candidate.diagnostics.expanded_labels <= baseline.diagnostics.expanded_labels
    assert candidate.diagnostics.queue_peak <= baseline.diagnostics.queue_peak


def test_certified_heuristic_checkpoint_binds_policy_digest() -> None:
    request = replace(_request(), use_heuristic=True)
    planner = _planner()
    certificate = _heuristic(planner, request)
    planner.heuristic_certificate = certificate
    session = create_non_fifo_temporal_certified_heuristic_session(
        planner,
        request,
        certificate,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert checkpoint.heuristic_policy_digest == certificate.digest

    other = replace(certificate, proof_digest="different-proof")
    planner.heuristic_certificate = other
    with pytest.raises(NonFifoTemporalAdapterError, match="heuristic digest mismatch"):
        restore_non_fifo_temporal_certified_heuristic_session(
            planner,
            checkpoint,
            request,
            other,
        )


def test_composed_bound_and_heuristic_preserve_semantics_and_prune() -> None:
    request = replace(_request(), use_heuristic=True)
    baseline = run_non_fifo_temporal_search(_planner(), replace(request, use_heuristic=False))
    planner = _planner()
    state_bound = _arrival_certificate(planner, request)
    heuristic = _heuristic(planner, request)
    planner.state_bound_certificate = state_bound
    planner.heuristic_certificate = heuristic

    candidate = run_non_fifo_temporal_composed_bound_heuristic_search(
        planner,
        request,
        state_bound,
        heuristic,
    )

    assert baseline.planning_result is not None
    assert candidate.planning_result is not None
    assert candidate.status.value == "GOAL_FOUND"
    assert candidate.semantic_digest == baseline.semantic_digest
    assert candidate.planning_result.nodes == baseline.planning_result.nodes
    assert candidate.diagnostics.dominance_pruned == 0
    assert candidate.diagnostics.state_bound_rejected == 0
    assert candidate.diagnostics.state_bound_checks > 0
    assert candidate.diagnostics.state_bound_pruned > 0
    assert candidate.diagnostics.heuristic_policy == "certified"
    assert candidate.diagnostics.heuristic_scope_match
    assert candidate.diagnostics.heuristic_rejected == 0


def test_composed_checkpoint_binds_both_certificate_digests() -> None:
    request = replace(_request(), use_heuristic=True)
    planner = _planner()
    state_bound = _arrival_certificate(planner, request)
    heuristic = _heuristic(planner, request)
    planner.state_bound_certificate = state_bound
    planner.heuristic_certificate = heuristic
    session = create_non_fifo_temporal_composed_bound_heuristic_session(
        planner,
        request,
        state_bound,
        heuristic,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert checkpoint.state_bound_policy_digest == state_bound.digest
    assert checkpoint.heuristic_policy_digest == heuristic.digest

    restored = restore_non_fifo_temporal_composed_bound_heuristic_session(
        planner,
        checkpoint,
        request,
        state_bound,
        heuristic,
    )
    while True:
        resumed = restored.advance(expansion_slice=1)
        if resumed is not None:
            break
    assert resumed.status.value == "GOAL_FOUND"
    assert resumed.semantic_digest == session.run().semantic_digest

    drifted = replace(heuristic, proof_digest="composed-drift")
    planner.heuristic_certificate = drifted
    with pytest.raises(NonFifoTemporalAdapterError, match="heuristic digest mismatch"):
        restore_non_fifo_temporal_composed_bound_heuristic_session(
            planner,
            checkpoint,
            request,
            state_bound,
            drifted,
        )


def test_composed_adapter_rejects_incomplete_or_mismatched_proofs() -> None:
    request = replace(_request(), use_heuristic=True)
    planner = _planner()
    state_bound = _arrival_certificate(planner, request)
    heuristic = _heuristic(planner, request)

    incomplete = replace(state_bound, arrival_upper_hours=state_bound.arrival_upper_hours[:-1])
    planner.state_bound_certificate = incomplete
    planner.heuristic_certificate = heuristic
    with pytest.raises(NonFifoTemporalAdapterError, match="complete arrival envelope"):
        run_non_fifo_temporal_composed_bound_heuristic_search(
            planner,
            request,
            incomplete,
            heuristic,
        )

    mismatch_scope = type(state_bound.scope).from_mapping(
        {**state_bound.scope.mapping, "composed_revision": "mismatch"}
    )
    mismatched = replace(state_bound, scope=mismatch_scope)
    planner.state_bound_certificate = mismatched
    with pytest.raises(NonFifoTemporalAdapterError, match="scope mismatch"):
        run_non_fifo_temporal_composed_bound_heuristic_search(
            planner,
            request,
            mismatched,
            heuristic,
        )
