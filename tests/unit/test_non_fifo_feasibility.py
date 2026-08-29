"""Adversarial finite-domain tests for the non-FIFO research reference."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoBusinessEvidence,
    NonFifoFrontierCertificateError,
    NonFifoFrontierComparisonStatus,
    NonFifoParetoFrontierCertificate,
    NonFifoParetoLabel,
    NonFifoParetoSessionIdentityMismatch,
    NonFifoParetoSessionRestoreError,
    NonFifoParetoSessionState,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    NonFifoTransition,
    certify_non_fifo_pareto_frontier,
    compare_non_fifo_pareto_frontiers,
    create_non_fifo_pareto_session,
    restore_non_fifo_pareto_session,
    search_non_fifo,
    search_non_fifo_pareto,
)

_REFERENCE_SPEC = spec_from_file_location(
    "c_non_fifo_reference_oracle",
    Path(__file__).parents[1].joinpath("reference_temporal_oracle.py"),
)
if _REFERENCE_SPEC is None or _REFERENCE_SPEC.loader is None:
    raise RuntimeError("unable to load the reference temporal oracle")
_REFERENCE = module_from_spec(_REFERENCE_SPEC)
sys.modules[_REFERENCE_SPEC.name] = _REFERENCE
_REFERENCE_SPEC.loader.exec_module(_REFERENCE)
OracleEdge = _REFERENCE.OracleEdge
ReferenceTemporalOracle = _REFERENCE.ReferenceTemporalOracle

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _transition(hours: float, cost: float = 1.0) -> NonFifoTransition:
    return NonFifoTransition(T0 + timedelta(hours=hours), cost)


def test_non_fifo_keeps_two_exact_arrivals_at_the_same_node() -> None:
    graph = {
        "start": ("early", "late"),
        "early": ("goal",),
        "late": ("goal",),
        "goal": (),
    }

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoTransition:
        if (start, end) == ("start", "early"):
            return NonFifoTransition(arrival + timedelta(hours=1), 1.0)
        if (start, end) == ("start", "late"):
            return NonFifoTransition(arrival + timedelta(hours=2), 1.0)
        if end == "goal":
            # The later exact arrival gets a much faster non-FIFO suffix.
            return NonFifoTransition(
                arrival + timedelta(hours=5 if arrival < T0 + timedelta(hours=2) else 0.1),
                5.0 if arrival < T0 + timedelta(hours=2) else 0.1,
            )
        raise AssertionError((start, end))

    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.label is not None
    assert result.label.path == ("start", "late", "goal")
    assert sum(label.node == "late" for label in result.labels) == 1


def test_same_bucket_different_exact_eta_labels_are_both_retained() -> None:
    graph = {"start": ("a", "b"), "a": ("join",), "b": ("join",), "join": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoTransition:
        if start == "start" and end == "a":
            return _transition(1.1)
        if start == "start" and end == "b":
            return _transition(1.9)
        return NonFifoTransition(arrival, 1.0)

    result = search_non_fifo(
        start="start",
        goal="join",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
    )

    join_labels = [label for label in result.labels if label.node == "join"]
    assert len(join_labels) == 2
    assert {label.arrival_time for label in join_labels} == {
        T0 + timedelta(hours=1.1),
        T0 + timedelta(hours=1.9),
    }


def test_cycle_hits_frozen_label_limit_instead_of_looping() -> None:
    graph = {"start": ("cycle",), "cycle": ("cycle", "goal"), "goal": ()}

    def evaluate(_start: str, end: str, arrival: datetime) -> NonFifoTransition:
        return NonFifoTransition(
            arrival + timedelta(hours=1),
            0.0 if end == "cycle" else 10.0,
        )

    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        max_labels=4,
    )

    assert result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert result.reason == "search_limit_exceeded"


def test_evaluator_failure_is_explicit_and_not_a_partial_route() -> None:
    def evaluate(_start: str, _end: str, _arrival: datetime) -> NonFifoTransition:
        raise RuntimeError("hard mask")

    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=evaluate,
    )

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert result.label is None
    assert result.evaluator_errors == ("RuntimeError:hard mask",)


def test_cancellation_is_observed_before_expansion() -> None:
    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: (),
        evaluate_edge=lambda *_args: pytest.fail("must not evaluate"),
        cancel_check=lambda: True,
    )

    assert result.status is NonFifoSearchStatus.CANCELLED
    assert result.reason == "cancelled"


def test_non_fifo_rejects_naive_non_positive_arrival() -> None:
    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=lambda *_args: NonFifoTransition(T0 - timedelta(seconds=1), 1.0),
    )

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert any("arrival_before_departure" in error for error in result.evaluator_errors)


def test_same_exact_arrival_replaces_only_a_more_expensive_label() -> None:
    graph = {"start": ("join", "join"), "join": ()}
    calls = 0

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoTransition:
        nonlocal calls
        calls += 1
        return NonFifoTransition(arrival + timedelta(hours=1), 2.0 if calls == 1 else 1.0)

    result = search_non_fifo(
        start="start",
        goal="join",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.label is not None
    assert result.label.cost == 1.0
    assert len([label for label in result.labels if label.node == "join"]) == 1


def test_maximum_elapsed_is_a_explicit_no_route_boundary() -> None:
    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=lambda *_args: _transition(2.0),
        maximum_elapsed=timedelta(hours=1),
    )

    assert result.status is NonFifoSearchStatus.EXHAUSTED
    assert result.label is None
    assert result.reason == "no_route"


def test_pareto_keeps_later_exact_arrival_with_better_non_fifo_suffix() -> None:
    graph = {
        "start": ("early", "late"),
        "early": ("goal",),
        "late": ("goal",),
        "goal": (),
    }

    def evaluate(start: str, end: str, arrival: datetime):
        if (start, end) == ("start", "early"):
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))
        if (start, end) == ("start", "late"):
            return NonFifoParetoTransition(arrival + timedelta(hours=2), (5.0, 5.0))
        if end == "goal":
            suffix = 0.1 if arrival >= T0 + timedelta(hours=2) else 5.0
            return NonFifoParetoTransition(
                arrival + timedelta(hours=suffix),
                (suffix, suffix),
            )
        raise AssertionError((start, end))

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.label is not None
    assert result.label.path == ("start", "late", "goal")
    assert result.label.costs == pytest.approx((5.1, 5.1))
    assert {label.arrival_time for label in result.labels if label.node == "late"} == {
        T0 + timedelta(hours=2)
    }

    repeat = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )
    assert repeat.semantic_digest == result.semantic_digest
    assert repeat.generated == result.generated
    assert repeat.pareto_pruned == result.pareto_pruned


def test_pareto_prunes_only_new_dominated_label_at_same_exact_state() -> None:
    graph = {"start": ("join", "join"), "join": ()}
    calls = 0

    def evaluate(_start: str, _end: str, arrival: datetime):
        nonlocal calls
        calls += 1
        costs = (1.0, 5.0) if calls == 1 else (2.0, 6.0)
        return NonFifoParetoTransition(arrival + timedelta(hours=1), costs)

    result = search_non_fifo_pareto(
        start="start",
        goal="join",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.pareto_pruned == 1
    assert len([label for label in result.labels if label.node == "join"]) == 1
    assert result.label is not None
    assert result.label.costs == pytest.approx((1.0, 5.0))


def test_pareto_keeps_equal_cost_paths_for_auditability() -> None:
    graph = {"start": ("left", "right"), "left": ("join",), "right": ("join",), "join": ()}

    def evaluate(start: str, end: str, arrival: datetime):
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    result = search_non_fifo_pareto(
        start="start",
        goal="join",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )

    join_labels = [label for label in result.labels if label.node == "join"]
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert len(join_labels) == 2
    assert result.pareto_pruned == 0


def test_pareto_never_cross_prunes_different_exact_arrivals() -> None:
    graph = {"start": ("join", "join"), "join": ("goal",), "goal": ()}
    calls = 0

    def evaluate(start: str, end: str, arrival: datetime):
        nonlocal calls
        if start == "start":
            calls += 1
            hours = 1.0 if calls == 1 else 2.0
            costs = (1.0, 1.0) if hours == 1.0 else (2.0, 2.0)
            return NonFifoParetoTransition(arrival + timedelta(hours=hours), costs)
        if end == "goal":
            suffix = 5.0 if arrival < T0 + timedelta(hours=2) else 0.1
            return NonFifoParetoTransition(
                arrival + timedelta(hours=suffix),
                (suffix, suffix),
            )
        raise AssertionError((start, end))

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
    )

    join_labels = [label for label in result.labels if label.node == "join"]
    assert len(join_labels) == 2
    assert result.pareto_pruned == 0
    assert len(result.goal_frontier) == 2
    assert result.label is not None
    assert result.label.path == ("start", "join", "goal")
    assert result.label.costs == pytest.approx((2.1, 2.1))


def test_pareto_scalar_mode_matches_independent_non_fifo_oracle() -> None:
    graph = {
        "s": ("u", "x"),
        "x": ("u",),
        "u": ("g",),
        "g": (),
    }

    def evaluate(start: str, end: str, arrival: datetime):
        if (start, end) in (("s", "u"), ("s", "x")):
            return NonFifoTransition(arrival + timedelta(minutes=30), 0.5)
        if (start, end) == ("x", "u"):
            return NonFifoTransition(arrival + timedelta(minutes=30), 0.5)
        if (start, end) == ("u", "g"):
            if arrival >= T0 + timedelta(hours=1):
                return NonFifoTransition(arrival + timedelta(minutes=30), 0.5)
            return NonFifoTransition(arrival + timedelta(hours=3), 3.0)
        raise AssertionError((start, end))

    result = search_non_fifo_pareto(
        start="s",
        goal="g",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
    )

    oracle = ReferenceTemporalOracle(
        graph.__getitem__,
        lambda state, end: OracleEdge(
            evaluate(state[0], end, state[2]).arrival_time,
            evaluate(state[0], end, state[2]).cost,
        ),
    ).search("s", "g", T0)

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.label is not None
    assert result.label.path == oracle.nodes
    assert result.label.arrival_time == oracle.arrival_times[-1]
    assert result.label.costs[0] == pytest.approx(oracle.total_cost)


def test_pareto_resource_limit_and_cancellation_never_return_partial_route() -> None:
    graph = {"start": ("a", "b"), "a": ("goal",), "b": ("goal",), "goal": ()}

    def evaluate(_start: str, _end: str, arrival: datetime):
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    limited = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=False,
        max_labels=2,
    )
    assert limited.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert limited.label is None
    assert limited.reason == "search_limit_exceeded"

    cancelled = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        cancel_check=lambda: True,
    )
    assert cancelled.status is NonFifoSearchStatus.CANCELLED
    assert cancelled.label is None
    assert cancelled.reason == "cancelled"


def test_pareto_evaluator_failure_is_fail_closed() -> None:
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=lambda *_args: NonFifoTransition(T0, 1.0),
    )

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert result.label is None
    assert any("arrival_not_strictly_later" in error for error in result.evaluator_errors)


def test_pareto_cycle_hits_frozen_label_limit_instead_of_looping() -> None:
    graph = {"start": ("cycle",), "cycle": ("cycle", "goal"), "goal": ()}

    def evaluate(_start: str, end: str, arrival: datetime):
        hours = 1.0 if end == "cycle" else 2.0
        return NonFifoParetoTransition(arrival + timedelta(hours=hours), (0.0, 0.0))

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        max_labels=4,
    )

    assert result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert result.label is None
    assert result.reason == "search_limit_exceeded"


def test_business_evidence_is_preserved_and_digest_bound() -> None:
    evidence = NonFifoBusinessEvidence(
        speed_knots=10.5,
        risk_score=0.25,
        maximum_risk=0.4,
        confidence=0.9,
        source_ids=("risk-a", "risk-b"),
    )

    def evaluate(_start: str, _end: str, arrival: datetime):
        return NonFifoParetoTransition(
            arrival + timedelta(hours=1),
            (1.0, 2.0),
            business=evidence,
        )

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=evaluate,
        objective_count=2,
    )
    repeat = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=evaluate,
        objective_count=2,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.label is not None
    assert result.label.business_evidence == (evidence,)
    assert result.semantic_digest == repeat.semantic_digest
    assert result.edge_evaluations == repeat.edge_evaluations == 1


def test_edge_evaluation_limit_is_explicit_and_fail_closed() -> None:
    graph = {"start": ("a", "b"), "a": (), "b": ()}

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=lambda _start, _end, arrival: NonFifoTransition(
            arrival + timedelta(hours=1), 1.0
        ),
        max_edge_evaluations=1,
    )

    assert result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert result.label is None
    assert result.edge_evaluations == 2
    assert result.reason == "search_limit_exceeded"


def test_scalar_edge_evaluation_limit_is_reported() -> None:
    graph = {"start": ("a", "b"), "a": (), "b": ()}

    result = search_non_fifo(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=lambda _start, _end, arrival: NonFifoTransition(
            arrival + timedelta(hours=1), 1.0
        ),
        max_edge_evaluations=1,
    )

    assert result.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert result.label is None
    assert result.edge_evaluations == 2
    assert result.reason == "search_limit_exceeded"


def test_hard_mask_business_evidence_is_not_a_successful_edge() -> None:
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=lambda _start, _end, arrival: NonFifoTransition(
            arrival + timedelta(hours=1),
            1.0,
            business=NonFifoBusinessEvidence(hard_mask=True),
        ),
    )

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert result.label is None
    assert result.evaluator_errors == ("NonFifoEvaluationError:hard_mask",)


def test_pareto_frontier_retains_non_dominated_labels_at_one_exact_arrival() -> None:
    graph = {"start": ("left", "right"), "left": ("goal",), "right": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (0.0, 0.0))
        costs = (1.0, 4.0) if start == "left" else (4.0, 1.0)
        return NonFifoParetoTransition(T0 + timedelta(hours=2), costs)

    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert len(result.goal_frontier) == 2
    assert {label.costs for label in result.goal_frontier} == {(1.0, 4.0), (4.0, 1.0)}
    assert result.frontier_digest == result.pareto_frontier_digest


def test_pareto_frontier_digest_binds_policy_and_search_limits() -> None:
    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))
    common = {
        "start": "start",
        "goal": "goal",
        "departure_time": T0,
        "neighbors": lambda _node: ("goal",),
        "evaluate_edge": evaluate,
        "objective_count": 2,
    }
    baseline = search_non_fifo_pareto(**common)
    pruned_policy = search_non_fifo_pareto(**common, pareto_pruning=True)
    changed_limit = search_non_fifo_pareto(**common, max_queue=49_999)

    assert baseline.frontier_digest != pruned_policy.frontier_digest
    assert baseline.frontier_digest != changed_limit.frontier_digest
    assert baseline.frontier_digest == baseline.pareto_frontier_digest


def test_pareto_neighbor_order_is_canonical_for_deterministic_evidence() -> None:
    graph = {"start": ("right", "left"), "left": ("goal",), "right": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    first = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
    )
    reversed_graph = {key: tuple(reversed(value)) for key, value in graph.items()}
    second = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=reversed_graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
    )

    assert first.semantic_digest == second.semantic_digest
    assert first.frontier_digest == second.frontier_digest
    assert first.generated == second.generated


def test_pareto_priority_ordering_preserves_complete_frontier() -> None:
    graph = {
        "start": ("left", "right"),
        "left": ("goal",),
        "right": ("goal",),
        "goal": (),
    }

    def evaluate(start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (0.0, 0.0))
        costs = (1.0, 4.0) if start == "left" else (4.0, 1.0)
        return NonFifoParetoTransition(arrival + timedelta(hours=1), costs)

    def priority(label: NonFifoParetoLabel) -> float:
        # Ordering evidence only: the graph's lower bound is zero at the
        # source and one hour at every intermediate node.
        return label.costs[0] + (1.0 if label.node != "goal" else 0.0)

    baseline = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )
    ordered = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
        priority=priority,
        priority_policy_digest="fixture-certified-priority-v1",
    )

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert ordered.status is NonFifoSearchStatus.GOAL_FOUND
    assert ordered.goal_frontier == baseline.goal_frontier
    assert ordered.frontier_digest != baseline.frontier_digest
    assert ordered.priority_policy_digest == "fixture-certified-priority-v1"
    assert ordered.expanded == baseline.expanded
    assert ordered.generated == baseline.generated


def test_pareto_priority_checkpoint_binds_callback_and_policy() -> None:
    graph = {"start": ("branch", "goal"), "branch": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        hours = 2.0 if start == "start" and end == "branch" else 1.0
        costs = (2.0, 2.0) if hours == 2.0 else (1.0, 1.0)
        return NonFifoParetoTransition(arrival + timedelta(hours=hours), costs)

    def priority(label: NonFifoParetoLabel) -> float:
        return label.costs[0]

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        priority=priority,
        priority_policy_digest="fixture-priority-v1",
        fixture_digest="priority-checkpoint-fixture",
    )
    # The goal is consumed in this slice; static priority sessions keep the
    # pre-goal phase for backwards-compatible checkpoint semantics.
    assert session.advance(expansion_slice=2) is None
    checkpoint = session.checkpoint()
    restored = restore_non_fifo_pareto_session(
        checkpoint,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        priority=priority,
    )
    result = restored.run()
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.priority_policy_digest == "fixture-priority-v1"

    def changed_priority(label: NonFifoParetoLabel) -> float:
        return label.costs[0] + 1.0

    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="priority callback"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=graph.__getitem__,
            evaluate_edge=evaluate,
            priority=changed_priority,
        )


def test_goal_gated_priority_rekeys_only_after_first_goal_and_preserves_frontier() -> None:
    graph = {"start": ("branch", "goal"), "branch": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start" and end == "goal":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))
        if start == "start" and end == "branch":
            return NonFifoParetoTransition(arrival + timedelta(hours=2), (2.0, 2.0))
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    def post_goal_priority(label: NonFifoParetoLabel) -> float:
        # Once a goal has been seen, make the pending branch the first queued
        # item.  This is deliberately an ordering-only fixture.
        return 0.0 if label.node == "branch" else label.costs[0]

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
        priority_after_goal=post_goal_priority,
        priority_policy_digest="fixture-goal-gated-priority-v1",
    )
    assert session.advance(expansion_slice=1) is None
    before_goal = session.checkpoint()
    assert before_goal.priority_phase == "pre_goal"
    assert not before_goal.goals

    # The direct goal is popped next under the historical key.  Re-keying is
    # then visible in the paused checkpoint without removing the branch label.
    assert session.advance(expansion_slice=1) is None
    after_goal = session.checkpoint()
    assert after_goal.priority_phase == "post_goal"
    assert len(after_goal.goals) == 1
    assert len(after_goal.queue) == 1
    assert after_goal.queue[0][0][0] == pytest.approx(0.0)

    restored = restore_non_fifo_pareto_session(
        after_goal,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        priority_after_goal=post_goal_priority,
    )
    result = restored.run()
    baseline = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        pareto_pruning=True,
    )
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.goal_frontier == baseline.goal_frontier
    assert result.priority_policy_digest == "fixture-goal-gated-priority-v1"


def test_goal_gated_priority_checkpoint_binds_post_goal_callback() -> None:
    graph = {"start": ("goal",), "goal": ()}

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    def post_goal_priority(label: NonFifoParetoLabel) -> float:
        return label.costs[0]

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        priority_after_goal=post_goal_priority,
        priority_policy_digest="fixture-goal-gated-checkpoint-v1",
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()

    restored = restore_non_fifo_pareto_session(
        checkpoint,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        priority_after_goal=post_goal_priority,
    )
    assert restored.run().status is NonFifoSearchStatus.GOAL_FOUND

    def changed_post_goal_priority(label: NonFifoParetoLabel) -> float:
        return label.costs[0] + 1.0

    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="post-goal priority"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=graph.__getitem__,
            evaluate_edge=evaluate,
            priority_after_goal=changed_post_goal_priority,
        )


def test_pareto_failed_result_has_no_frontier_or_partial_route() -> None:
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=lambda *_args: (_ for _ in ()).throw(RuntimeError("broken evaluator")),
        objective_count=2,
    )

    assert result.status is NonFifoSearchStatus.EVALUATOR_FAILURE
    assert result.goal_labels == ()
    assert result.goal_frontier == ()
    assert result.label is None
    assert result.semantic_digest is None
    assert result.frontier_digest


def test_pareto_session_slice_restore_matches_one_shot_frontier() -> None:
    graph = {
        "start": ("left", "right"),
        "left": ("goal",),
        "right": ("goal",),
        "goal": (),
    }

    def neighbours(node: str) -> tuple[str, ...]:
        return graph[node]

    def evaluate(start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (0.0, 0.0))
        costs = (1.0, 4.0) if start == "left" else (4.0, 1.0)
        return NonFifoParetoTransition(T0 + timedelta(hours=2), costs)

    common = {
        "start": "start",
        "goal": "goal",
        "departure_time": T0,
        "neighbors": neighbours,
        "evaluate_edge": evaluate,
        "objective_count": 2,
        "pareto_pruning": True,
        "fixture_digest": "m12-fixture",
    }
    one_shot = search_non_fifo_pareto(**common)
    session = create_non_fifo_pareto_session(**common)
    assert session.advance(expansion_slice=1) is None
    assert session.state is NonFifoParetoSessionState.PAUSED
    checkpoint = session.checkpoint()
    restored = restore_non_fifo_pareto_session(
        checkpoint,
        neighbors=neighbours,
        evaluate_edge=evaluate,
    )
    restored_result = restored.run()

    assert restored_result.status is NonFifoSearchStatus.GOAL_FOUND
    assert restored_result.semantic_digest == one_shot.semantic_digest
    assert restored_result.frontier_digest == one_shot.frontier_digest
    assert restored_result.goal_frontier == one_shot.goal_frontier
    assert restored_result.expanded == one_shot.expanded
    assert restored_result.generated == one_shot.generated
    assert restored_result.pareto_pruned == one_shot.pareto_pruned


def test_pareto_session_slice_only_reaches_terminal_result() -> None:
    graph = {"start": ("middle",), "middle": ("goal",), "goal": ()}

    def neighbours(node: str) -> tuple[str, ...]:
        return graph[node]

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbours,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="m12-slice-fixture",
    )
    result = None
    pauses = 0
    while result is None:
        result = session.advance(expansion_slice=1)
        if result is None:
            pauses += 1
            assert session.state is NonFifoParetoSessionState.PAUSED

    assert pauses >= 1
    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert session.state is NonFifoParetoSessionState.GOAL_FOUND
    assert result.label is not None


def test_pareto_session_restore_rejects_identity_and_callback_drift() -> None:
    graph = {"start": ("goal",), "goal": ()}

    def neighbours(node: str) -> tuple[str, ...]:
        return graph[node]

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=neighbours,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="m12-drift-fixture",
    )
    checkpoint = session.checkpoint()
    changed_policy = replace(checkpoint.identity, pareto_pruning=True)
    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="identity mismatch"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=neighbours,
            evaluate_edge=evaluate,
            identity=changed_policy,
        )

    def changed_evaluate(
        _start: str, _end: str, arrival: datetime
    ) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (2.0, 2.0))

    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="evaluator callback"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=neighbours,
            evaluate_edge=changed_evaluate,
        )

    changed_limit = replace(checkpoint.identity, max_queue=checkpoint.identity.max_queue + 1)
    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="identity mismatch"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=neighbours,
            evaluate_edge=evaluate,
            identity=changed_limit,
        )

    changed_config = replace(checkpoint.identity, config_digest="m12-config-drift")
    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="identity mismatch"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=neighbours,
            evaluate_edge=evaluate,
            identity=changed_config,
        )


def test_pareto_session_checkpoint_and_terminal_restore_are_fail_closed() -> None:
    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=evaluate,
        objective_count=2,
        cancel_check=lambda: True,
        fixture_digest="m12-cancel-fixture",
    )
    cancelled = session.advance()
    assert cancelled is not None
    assert cancelled.status is NonFifoSearchStatus.CANCELLED
    assert cancelled.label is None
    assert cancelled.goal_frontier == ()
    with pytest.raises(NonFifoParetoSessionRestoreError, match="READY or PAUSED"):
        session.checkpoint()

    ready = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=lambda _node: ("goal",),
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="m12-checkpoint-fixture",
    )
    checkpoint = ready.checkpoint()
    with pytest.raises(NonFifoParetoSessionRestoreError, match="state digest"):
        replace(checkpoint, expanded=checkpoint.expanded + 1)


def test_frontier_certificate_and_independent_comparison_cover_all_goal_labels() -> None:
    graph = {"start": ("left", "right"), "left": ("goal",), "right": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if start == "start":
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (0.0, 0.0))
        costs = (1.0, 4.0) if start == "left" else (4.0, 1.0)
        return NonFifoParetoTransition(T0 + timedelta(hours=2), costs)

    common = {
        "start": "start",
        "goal": "goal",
        "departure_time": T0,
        "neighbors": graph.__getitem__,
        "evaluate_edge": evaluate,
        "objective_count": 2,
        "fixture_digest": "m20-frontier-fixture",
        "config_digest": "m20-config",
    }
    candidate_session = create_non_fifo_pareto_session(**common, pareto_pruning=True)
    reference_session = create_non_fifo_pareto_session(**common, pareto_pruning=False)
    candidate = candidate_session.run()
    reference = reference_session.run()

    candidate_certificate = certify_non_fifo_pareto_frontier(
        candidate, identity=candidate_session.identity, scope_digest="m20-scope"
    )
    reference_certificate = NonFifoParetoFrontierCertificate.from_result(
        reference, identity=reference_session.identity, scope_digest="m20-scope"
    )
    assert candidate_certificate.usable
    assert reference_certificate.usable
    assert candidate_certificate.frontier_count == 2
    assert candidate_certificate.frontier_digest == reference_certificate.frontier_digest

    comparison = compare_non_fifo_pareto_frontiers(
        candidate,
        reference,
        candidate_identity=candidate_session.identity,
        reference_identity=reference_session.identity,
        candidate_scope_digest="m20-scope",
        reference_scope_digest="m20-scope",
    )
    assert comparison.status is NonFifoFrontierComparisonStatus.MATCH
    assert comparison.matched
    assert comparison.missing_label_digests == ()
    assert comparison.unexpected_label_digests == ()
    assert comparison.digest


def test_frontier_certificate_is_fail_closed_for_resource_and_evaluator_results() -> None:
    graph = {"start": ("goal",), "goal": ()}

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0))

    limited_session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        max_expansions=1,
        fixture_digest="m20-resource-fixture",
    )
    limited = limited_session.run()
    certificate = certify_non_fifo_pareto_frontier(
        limited, identity=limited_session.identity, scope_digest="m20-scope"
    )
    assert limited.status is NonFifoSearchStatus.RESOURCE_LIMIT
    assert not certificate.usable
    assert "status:RESOURCE_LIMIT" in (certificate.rejection_reason or "")
    with pytest.raises(NonFifoFrontierCertificateError, match="RESOURCE_LIMIT"):
        certificate.assert_usable()

    broken_session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=lambda *_args: (_ for _ in ()).throw(RuntimeError("broken")),
        objective_count=2,
        fixture_digest="m20-evaluator-fixture",
    )
    broken = broken_session.run()
    broken_certificate = certify_non_fifo_pareto_frontier(
        broken, identity=broken_session.identity, scope_digest="m20-scope"
    )
    assert not broken_certificate.usable
    assert "evaluator_errors" in (broken_certificate.rejection_reason or "")


def test_frontier_comparison_rejects_scope_identity_and_label_drift() -> None:
    graph = {"start": ("goal",), "goal": ()}

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 2.0))

    def changed_evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 3.0))

    candidate_session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="m20-common-fixture",
        config_digest="m20-common-config",
    )
    reference_session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=changed_evaluate,
        objective_count=2,
        fixture_digest="m20-common-fixture",
        config_digest="m20-common-config",
    )
    candidate = candidate_session.run()
    reference = reference_session.run()
    mismatch = compare_non_fifo_pareto_frontiers(
        candidate,
        reference,
        candidate_identity=candidate_session.identity,
        reference_identity=reference_session.identity,
        candidate_scope_digest="m20-scope",
        reference_scope_digest="m20-scope",
    )
    assert mismatch.status is NonFifoFrontierComparisonStatus.FRONTIER_MISMATCH
    assert mismatch.missing_label_digests and mismatch.unexpected_label_digests

    scope_mismatch = compare_non_fifo_pareto_frontiers(
        candidate,
        candidate,
        candidate_identity=candidate_session.identity,
        reference_identity=candidate_session.identity,
        candidate_scope_digest="m20-scope-a",
        reference_scope_digest="m20-scope-b",
    )
    assert scope_mismatch.status is NonFifoFrontierComparisonStatus.IDENTITY_MISMATCH
    assert not scope_mismatch.matched

    changed_identity = replace(candidate_session.identity, max_queue=49_999)
    identity_mismatch = compare_non_fifo_pareto_frontiers(
        candidate,
        candidate,
        candidate_identity=candidate_session.identity,
        reference_identity=changed_identity,
        candidate_scope_digest="m20-scope",
        reference_scope_digest="m20-scope",
    )
    assert identity_mismatch.status is NonFifoFrontierComparisonStatus.IDENTITY_MISMATCH


def test_frontier_certificate_digest_tamper_is_rejected() -> None:
    graph = {"start": ("goal",), "goal": ()}

    def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
        return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0,))

    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        fixture_digest="m20-tamper-fixture",
    )
    result = session.run()
    certificate = certify_non_fifo_pareto_frontier(
        result, identity=session.identity, scope_digest="m20-scope"
    )
    with pytest.raises(NonFifoFrontierCertificateError, match="digest mismatch"):
        replace(certificate, certificate_digest="tampered")
