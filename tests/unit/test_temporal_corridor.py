"""Tests for proof-carrying temporal corridor derivation."""

from __future__ import annotations

from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope


def _scope() -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "certified:corridor-edge-v1",
            "bound_evaluator_digest": "certified:corridor-bound-v1",
            "objective": "fastest",
        }
    )


def _bounds(
    rows: int = 5,
    cols: int = 5,
) -> tuple[
    tuple[tuple[int, int], ...],
    dict[tuple[int, int], int],
    dict[tuple[int, int], int],
]:
    universe = tuple((row, col) for row in range(rows) for col in range(cols))
    goal = (rows - 1, cols - 1)
    # Leave one certified shortest-path corridor (top row, then final
    # column) and add an admissible synthetic detour penalty to the other
    # cells.  This gives the proof a measurable reduction without injecting
    # a route into the planner.
    forward = {
        (row, col): row + col + (2 if row > 0 and col < cols - 1 else 0) for row, col in universe
    }
    reverse = {
        (row, col): goal[0] - row + goal[1] - col + (2 if row > 0 and col < cols - 1 else 0)
        for row, col in universe
    }
    return universe, forward, reverse


def _evidence(scope: TemporalScope, *, admissible: bool = True) -> AdmissibleBoundEvidence:
    return AdmissibleBoundEvidence(
        scope=scope,
        method="manhattan-optimistic-time-v1",
        evaluator_digest="certified:corridor-bound-v1",
        proof_digest="fixture-proof-v1",
        admissible=admissible,
        coverage_complete=True,
    )


def test_derived_corridor_excludes_only_provably_unreachable_states() -> None:
    universe, forward, reverse = _bounds()
    scope = _scope()
    evidence = derive_temporal_corridor(
        scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(4, 4),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=8.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
        generated_nodes=universe,
    )

    assert evidence.certificate.usable
    assert evidence.certificate.allows((0, 0))
    assert evidence.certificate.allows((4, 4))
    assert evidence.excluded_count > 0
    assert evidence.projected_label_reduction is not None
    assert evidence.projected_label_reduction > 0.2
    assert evidence.proof_digest
    assert not evidence.certificate.arrival_bound_complete

    arrival_evidence = derive_temporal_corridor(
        scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(4, 4),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=8.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
        include_arrival_upper_bounds=True,
    )
    assert arrival_evidence.certificate.arrival_bound_complete
    assert set(arrival_evidence.arrival_upper_bounds) == set(
        arrival_evidence.certificate.arrival_upper_hours
    )


def test_missing_bound_proof_and_scope_mismatch_are_rejected() -> None:
    universe, forward, reverse = _bounds()
    scope = _scope()
    missing = derive_temporal_corridor(
        scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(4, 4),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=8.0,
        objective="fastest",
        bound_evidence=_evidence(scope, admissible=False),
    )
    mismatch = derive_temporal_corridor(
        scope={**scope.mapping, "goal": (1, 1)},
        expected_scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(4, 4),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=8.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
    )

    assert not missing.certificate.usable
    assert missing.certificate.reason == "bound_evidence_not_admissible_or_complete"
    assert not mismatch.certificate.usable
    assert mismatch.certificate.reason == "scope_mismatch"


def test_invalid_lower_bound_never_fails_open() -> None:
    universe, forward, reverse = _bounds()
    scope = _scope()
    forward[(2, 2)] = float("nan")

    evidence = derive_temporal_corridor(
        scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(4, 4),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=8.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
    )

    assert not evidence.certificate.usable
    assert evidence.certificate.allowed_nodes == ()
    assert evidence.reason.startswith("invalid_bound_evidence:")


def test_complete_edge_envelope_is_scope_bound_and_digest_visible() -> None:
    universe = ((0, 0), (0, 1), (1, 1))
    edges = {
        (0, 0): ((0, 1),),
        (0, 1): ((1, 1),),
        (1, 1): (),
    }
    forward = {(0, 0): 0.0, (0, 1): 1.0, (1, 1): 2.0}
    reverse = {(0, 0): 2.0, (0, 1): 1.0, (1, 1): 0.0}
    edge_lower = {
        ((0, 0), (0, 1)): 1.0,
        ((0, 1), (1, 1)): 1.0,
    }
    scope = _scope()

    evidence = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(1, 1),
        neighbors=lambda node: edges[node],
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=2.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
        include_arrival_upper_bounds=True,
        edge_lower_hours=edge_lower,
        edge_bound_complete=True,
    )

    assert evidence.certificate.usable
    assert evidence.certificate.edge_bound_complete
    assert evidence.certificate.edge_bound_digest
    assert evidence.edge_lower_hours == tuple((*edge, value) for edge, value in edge_lower.items())


def test_incomplete_edge_envelope_is_rejected_without_authorization() -> None:
    universe = ((0, 0), (0, 1), (1, 1))
    scope = _scope()
    evidence = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=universe,
        start=(0, 0),
        goal=(1, 1),
        neighbors=lambda node: {
            (0, 0): ((0, 1),),
            (0, 1): ((1, 1),),
            (1, 1): (),
        }[node],
        forward_lower_hours={node: 0.0 for node in universe},
        reverse_lower_hours={node: 0.0 for node in universe},
        horizon_hours=2.0,
        objective="fastest",
        bound_evidence=_evidence(scope),
        include_arrival_upper_bounds=True,
        edge_lower_hours={((0, 0), (0, 1)): 1.0},
        edge_bound_complete=True,
    )

    assert not evidence.certificate.usable
    assert evidence.reason == "invalid_edge_bound:ValueError"
