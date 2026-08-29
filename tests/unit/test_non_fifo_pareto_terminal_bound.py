"""Tests for the explicit selected-route terminal-bound research mode."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoParetoSessionIdentityMismatch,
    NonFifoParetoTerminalBoundCertificate,
    NonFifoParetoTerminalBoundStatus,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    certify_non_fifo_pareto_frontier,
    create_non_fifo_pareto_session,
    restore_non_fifo_pareto_session,
    search_non_fifo_pareto,
)

T0 = datetime(2026, 8, 29, tzinfo=UTC)
SCOPE = "terminal-bound-test-scope"


def _edge(arrival: datetime, hours: float, *costs: float) -> NonFifoParetoTransition:
    return NonFifoParetoTransition(
        arrival_time=arrival + timedelta(hours=hours),
        costs=tuple(costs),
    )


def _graph_neighbors(graph: dict[str, tuple[str, ...]]):
    def neighbors(node: str) -> tuple[str, ...]:
        return graph[node]

    return neighbors


def _terminal_graph():
    graph = {
        "start": ("a", "b"),
        "a": ("goal",),
        "b": ("goal",),
        "goal": (),
    }

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        costs = {
            ("start", "a"): (1.0, 0.0),
            ("start", "b"): (3.0, 0.0),
            ("a", "goal"): (1.0, 0.0),
            ("b", "goal"): (1.0, 0.0),
        }[(start, end)]
        return _edge(arrival, 1.0, *costs)

    return graph, _graph_neighbors(graph), evaluate


def _certificate(nodes: tuple[str, ...], *, scope: str = SCOPE):
    return NonFifoParetoTerminalBoundCertificate.certified(
        scope_digest=scope,
        goal="goal",
        objective_count=2,
        node_lower_bounds={node: (0.0, 0.0) for node in nodes},
        proof_digest="terminal-bound-proof-v1",
    )


def test_terminal_bound_prunes_worse_new_goal_label_but_keeps_selected_route() -> None:
    graph, neighbors, evaluate = _terminal_graph()
    baseline = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
    )
    certificate = _certificate(tuple(graph))
    selected = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert selected.status is NonFifoSearchStatus.GOAL_FOUND
    assert selected.selection_only is True
    assert selected.frontier_complete is False
    assert selected.incumbent_bound_pruned == 1
    assert selected.semantic_digest == baseline.semantic_digest
    assert [label.path for label in baseline.goal_labels] == [
        ("start", "a", "goal"),
        ("start", "b", "goal"),
    ]
    assert [label.path for label in selected.goal_frontier] == [("start", "a", "goal")]


def test_terminal_bound_keeps_equal_cost_and_lower_cost_later_arrival_labels() -> None:
    graph = {"start": ("early", "late"), "early": ("goal",), "late": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if (start, end) == ("start", "early"):
            return _edge(arrival, 1.0, 1.0, 0.0)
        if (start, end) == ("start", "late"):
            return _edge(arrival, 2.0, 0.5, 0.0)
        return _edge(arrival, 1.0, 1.0, 0.0)

    certificate = _certificate(tuple(graph))
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=_graph_neighbors(graph),
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-arrival-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.incumbent_bound_pruned == 0
    assert {label.arrival_time for label in result.goal_frontier} == {
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=3),
    }

    # Equal vectors are retained for deterministic tie-breaking and audit.
    equal = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=_graph_neighbors(
            {"start": ("a", "b"), "a": ("goal",), "b": ("goal",), "goal": ()}
        ),
        evaluate_edge=lambda start, end, arrival: _edge(arrival, 1.0, 1.0, 0.0),
        objective_count=2,
        fixture_digest="terminal-equal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=_certificate(("start", "a", "b", "goal")),
    )
    assert equal.incumbent_bound_pruned == 0
    assert len(equal.goal_frontier) == 2


def test_terminal_bound_scope_mismatch_is_fail_closed() -> None:
    _graph, neighbors, evaluate = _terminal_graph()
    rejected = NonFifoParetoTerminalBoundCertificate.rejected(
        scope_digest="other-scope",
        goal="goal",
        objective_count=2,
        reason="scope_mismatch",
    )
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=rejected,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.incumbent_bound_pruned == 0
    assert result.incumbent_bound_rejection_reasons == (("scope_mismatch", 1),)
    assert result.selection_only is True
    assert result.frontier_complete is False


def test_terminal_bound_checkpoint_binds_digest_and_restores_selection_mode() -> None:
    graph, neighbors, evaluate = _terminal_graph()
    certificate = _certificate(tuple(graph))
    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert checkpoint.identity.incumbent_bound_digest == certificate.digest
    restored = restore_non_fifo_pareto_session(
        checkpoint,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        incumbent_bound_certificate=certificate,
    )
    result = restored.run()
    assert result.selection_only is True
    assert result.frontier_complete is False
    assert result.incumbent_bound_pruned == 1

    changed = NonFifoParetoTerminalBoundCertificate.certified(
        scope_digest=SCOPE,
        goal="goal",
        objective_count=2,
        node_lower_bounds={node: (0.0, 0.0) for node in graph},
        proof_digest="changed-proof",
    )
    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="digest"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=neighbors,
            evaluate_edge=evaluate,
            incumbent_bound_certificate=changed,
        )


def test_frontier_certificate_rejects_selection_only_result() -> None:
    graph, neighbors, evaluate = _terminal_graph()
    certificate = _certificate(tuple(graph))
    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbors,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="terminal-fixture",
        config_digest="terminal-config",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )
    result = session.run()
    frontier_certificate = certify_non_fifo_pareto_frontier(
        result,
        identity=session.identity,
        scope_digest=SCOPE,
    )
    assert frontier_certificate.usable is False
    assert frontier_certificate.rejection_reason == "frontier_incomplete_by_policy"


def test_terminal_certificate_rejects_non_finite_or_wrong_status() -> None:
    with pytest.raises(ValueError, match="lower bounds"):
        NonFifoParetoTerminalBoundCertificate.certified(
            scope_digest=SCOPE,
            goal="goal",
            objective_count=1,
            node_lower_bounds={"start": (float("inf"),)},
            proof_digest="bad-proof",
        )
    rejected = NonFifoParetoTerminalBoundCertificate.rejected(
        scope_digest=SCOPE,
        goal="goal",
        objective_count=1,
        reason="coverage_incomplete",
    )
    assert rejected.status is NonFifoParetoTerminalBoundStatus.REJECTED
    assert rejected.usable is False
