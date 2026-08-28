"""Adversarial finite-domain tests for the non-FIFO research reference."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoBusinessEvidence,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    NonFifoTransition,
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
