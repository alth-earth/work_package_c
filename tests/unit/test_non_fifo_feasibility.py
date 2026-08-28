"""Adversarial finite-domain tests for the non-FIFO research reference."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoSearchStatus,
    NonFifoTransition,
    search_non_fifo,
)


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
