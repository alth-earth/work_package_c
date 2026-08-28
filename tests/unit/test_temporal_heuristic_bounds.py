"""Certified heuristic ordering tests for the C temporal research path."""

from __future__ import annotations

from dataclasses import replace

import pytest

from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    create_non_fifo_temporal_certified_heuristic_session,
    restore_non_fifo_temporal_certified_heuristic_session,
    run_non_fifo_temporal_certified_heuristic_search,
    run_non_fifo_temporal_search,
)
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
