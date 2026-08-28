"""P0.2-M6 arrival-envelope pruning and fail-closed checks."""

from __future__ import annotations

from dataclasses import replace

import pytest

from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    create_non_fifo_temporal_arrival_bounded_session,
    restore_non_fifo_temporal_arrival_bounded_session,
    run_non_fifo_temporal_arrival_bounded_search,
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate

from .test_non_fifo_temporal_bounded_adapter import _planner, _request


def _arrival_certificate(planner, request, *, upper=(0.3, 0.25, 1.0, 1.0, 1.0, 1.0)):
    scope = planner.temporal_scope(request)
    nodes = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    return TemporalStateBoundCertificate.certified(
        scope,
        nodes,
        excluded_nodes=(),
        proof_digest="arrival-envelope-test-v1",
        arrival_upper_hours=dict(zip(nodes, upper, strict=True)),
    )


def test_arrival_envelope_matches_unbounded_route_and_prunes_late_label() -> None:
    request = _request()
    baseline = run_non_fifo_temporal_search(_planner(), request)
    probe = _planner()
    certificate = _arrival_certificate(probe, request)
    bounded = run_non_fifo_temporal_arrival_bounded_search(
        _planner(certificate=certificate),
        request,
        certificate,
    )

    assert baseline.planning_result is not None
    assert bounded.planning_result is not None
    assert bounded.semantic_digest == baseline.semantic_digest
    assert bounded.diagnostics.state_bound_rejected == 0
    assert bounded.diagnostics.state_bound_arrival_pruned > 0
    assert bounded.diagnostics.state_bound_pruned >= (
        bounded.diagnostics.state_bound_arrival_pruned
    )


def test_arrival_envelope_keeps_exact_boundary() -> None:
    request = _request()
    planner = _planner()
    certificate = _arrival_certificate(planner, request)
    assert certificate.arrival_bound_complete
    assert certificate.allows_state(
        (0, 1),
        request.departure_time.replace(minute=15),
        request.departure_time,
    )


def test_incomplete_arrival_envelope_is_rejected_before_search() -> None:
    request = _request()
    planner = _planner()
    complete = _arrival_certificate(planner, request)
    incomplete = replace(
        complete,
        arrival_upper_hours=complete.arrival_upper_hours[:-1],
    )
    planner.state_bound_certificate = incomplete

    with pytest.raises(
        NonFifoTemporalAdapterError,
        match="complete arrival envelope",
    ):
        run_non_fifo_temporal_arrival_bounded_search(planner, request, incomplete)

    # The ordinary explicit bound path remains available, but does not claim
    # arrival-level pruning for an incomplete envelope.
    result = run_non_fifo_temporal_bounded_search(planner, request, incomplete)
    assert result.diagnostics.state_bound_arrival_pruned == 0


def test_arrival_envelope_scope_mismatch_fails_closed_without_pruning() -> None:
    request = _request()
    probe = _planner()
    certificate = _arrival_certificate(probe, request)
    mismatched_scope = type(certificate.scope).from_mapping(
        {**certificate.scope.mapping, "arrival_bound_revision": "mismatch"}
    )
    mismatched = replace(certificate, scope=mismatched_scope)
    result = run_non_fifo_temporal_arrival_bounded_search(
        _planner(certificate=mismatched),
        request,
        mismatched,
    )

    assert result.reason == "state_bound_rejected"
    assert result.planning_result is None
    assert result.diagnostics.state_bound_pruned == 0
    assert result.diagnostics.state_bound_arrival_pruned == 0
    assert result.diagnostics.state_bound_rejected > 0


def test_arrival_envelope_checkpoint_restore_preserves_pruning() -> None:
    request = _request()
    probe = _planner()
    certificate = _arrival_certificate(probe, request)
    planner = _planner(certificate=certificate)
    full = run_non_fifo_temporal_arrival_bounded_search(planner, request, certificate)

    session = create_non_fifo_temporal_arrival_bounded_session(planner, request, certificate)
    assert session.advance(expansion_slice=2) is None
    checkpoint = session.checkpoint()
    restored = restore_non_fifo_temporal_arrival_bounded_session(
        planner,
        checkpoint,
        request,
        certificate,
    )
    while True:
        result = restored.advance(expansion_slice=2)
        if result is not None:
            break

    assert full.semantic_digest == result.semantic_digest
    assert full.diagnostics.state_bound_arrival_pruned == (
        result.diagnostics.state_bound_arrival_pruned
    )
    assert full.diagnostics.state_bound_pruned == result.diagnostics.state_bound_pruned
