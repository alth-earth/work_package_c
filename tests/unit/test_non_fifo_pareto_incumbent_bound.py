"""Tests for the fail-closed exact-arrival incumbent-bound sidecar."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoParetoIncumbentBoundCertificate,
    NonFifoParetoIncumbentBoundStatus,
    NonFifoParetoSessionIdentityMismatch,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    create_non_fifo_pareto_session,
    restore_non_fifo_pareto_session,
    search_non_fifo_pareto,
)

T0 = datetime(2026, 8, 29, tzinfo=UTC)
SCOPE = "scope-m23"


def _transition(
    arrival: datetime, hours: float, costs: tuple[float, ...]
) -> NonFifoParetoTransition:
    return NonFifoParetoTransition(arrival + timedelta(hours=hours), costs)


def _late_branch_evaluator(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
    edges = {
        ("start", "direct"): (1.0, (1.0, 1.0)),
        ("direct", "goal"): (1.0, (1.0, 1.0)),
        ("start", "hub"): (1.0, (3.0, 3.0)),
        ("hub", "late"): (0.5, (1.0, 1.0)),
        ("late", "goal"): (0.5, (1.0, 1.0)),
    }
    hours, costs = edges[(start, end)]
    return _transition(arrival, hours, costs)


def _late_branch_certificate() -> NonFifoParetoIncumbentBoundCertificate:
    return NonFifoParetoIncumbentBoundCertificate.certified(
        scope_digest=SCOPE,
        goal="goal",
        objective_count=2,
        state_lower_bounds={
            ("direct", T0 + timedelta(hours=1)): (
                T0 + timedelta(hours=2),
                (0.0, 0.0),
            ),
            ("hub", T0 + timedelta(hours=1)): (
                T0 + timedelta(hours=2),
                (0.0, 0.0),
            ),
            ("goal", T0 + timedelta(hours=2)): (
                T0 + timedelta(hours=2),
                (0.0, 0.0),
            ),
            ("late", T0 + timedelta(hours=1.5)): (
                T0 + timedelta(hours=2),
                (0.0, 0.0),
            )
        },
        proof_digest="m23-late-branch-v1",
    )


def _run_late_branch(certificate=None):
    graph = {
        "start": ("direct", "hub"),
        "direct": ("goal",),
        "hub": ("late",),
        "late": ("goal",),
        "goal": (),
    }
    return search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=_late_branch_evaluator,
        objective_count=2,
        fixture_digest="fixture-m23",
        config_digest="config-m23",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )


def test_certified_bound_prunes_only_a_new_state_and_preserves_frontier() -> None:
    baseline = _run_late_branch()
    bounded = _run_late_branch(_late_branch_certificate())

    assert baseline.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.status is NonFifoSearchStatus.GOAL_FOUND
    assert bounded.incumbent_bound_pruned == 1
    assert bounded.incumbent_bound_rejected == 0
    assert bounded.incumbent_bound_digest == _late_branch_certificate().digest
    assert bounded.expanded < baseline.expanded
    assert [label.path for label in bounded.goal_frontier] == [
        ("start", "direct", "goal")
    ]
    assert [label.path for label in baseline.goal_frontier] == [
        ("start", "direct", "goal")
    ]


def test_different_exact_goal_arrival_is_never_compared() -> None:
    graph = {"start": ("early", "late"), "early": ("goal",), "late": ("goal",), "goal": ()}

    def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
        if (start, end) == ("start", "early"):
            return _transition(arrival, 1.0, (1.0, 1.0))
        if (start, end) == ("start", "late"):
            return _transition(arrival, 2.0, (0.1, 0.1))
        return _transition(arrival, 1.0, (1.0, 1.0))

    certificate = NonFifoParetoIncumbentBoundCertificate.certified(
        scope_digest=SCOPE,
        goal="goal",
        objective_count=2,
        state_lower_bounds={
            ("late", T0 + timedelta(hours=2)): (
                T0 + timedelta(hours=3),
                (0.0, 0.0),
            )
        },
        proof_digest="m23-arrival-separation-v1",
    )
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=evaluate,
        objective_count=2,
        fixture_digest="fixture-m23-arrivals",
        config_digest="config-m23",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.incumbent_bound_pruned == 0
    assert {label.arrival_time for label in result.goal_frontier} == {
        T0 + timedelta(hours=2),
        T0 + timedelta(hours=3),
    }


def test_scope_mismatch_is_fail_closed_and_default_is_unchanged() -> None:
    certificate = NonFifoParetoIncumbentBoundCertificate.rejected(
        scope_digest="other-scope",
        goal="goal",
        objective_count=2,
        reason="scope_mismatch",
    )
    result = _run_late_branch(certificate)
    default = _run_late_branch()

    assert result.status is NonFifoSearchStatus.GOAL_FOUND
    assert result.incumbent_bound_pruned == 0
    assert result.incumbent_bound_rejected >= 1
    assert result.incumbent_bound_rejection_reasons == (("scope_mismatch", 1),)
    assert default.incumbent_bound_digest == "non-fifo-pareto-incumbent-bound-disabled"
    assert default.incumbent_bound_pruned == 0
    assert [label.path for label in result.goal_frontier] == [
        label.path for label in default.goal_frontier
    ]


def test_checkpoint_binds_incumbent_bound_digest() -> None:
    certificate = _late_branch_certificate()
    graph = {
        "start": ("direct", "hub"),
        "direct": ("goal",),
        "hub": ("late",),
        "late": ("goal",),
        "goal": (),
    }
    session = create_non_fifo_pareto_session(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=_late_branch_evaluator,
        objective_count=2,
        fixture_digest="fixture-m23",
        config_digest="config-m23",
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )
    assert session.advance(expansion_slice=1) is None
    checkpoint = session.checkpoint()
    assert checkpoint.identity.incumbent_bound_digest == certificate.digest

    changed = NonFifoParetoIncumbentBoundCertificate.certified(
        scope_digest=SCOPE,
        goal="goal",
        objective_count=2,
        state_lower_bounds={
            ("late", T0 + timedelta(hours=1.5)): (
                T0 + timedelta(hours=2),
                (1.0, 1.0),
            )
        },
        proof_digest="m23-late-branch-changed",
    )
    with pytest.raises(NonFifoParetoSessionIdentityMismatch, match="digest"):
        restore_non_fifo_pareto_session(
            checkpoint,
            neighbors=graph.__getitem__,
            evaluate_edge=_late_branch_evaluator,
            incumbent_bound_certificate=changed,
        )


def test_rejected_certificate_never_prunes_cancelled_or_failed_search() -> None:
    certificate = NonFifoParetoIncumbentBoundCertificate(
        status=NonFifoParetoIncumbentBoundStatus.REJECTED,
        scope_digest=SCOPE,
        goal="goal",
        objective_count=2,
        state_lower_bounds=(),
        coverage_complete=False,
        evaluator_certified=False,
        proof_digest="m23-rejected",
        reason="evaluator_failure",
    )
    graph = {"start": ("goal",), "goal": ()}
    result = search_non_fifo_pareto(
        start="start",
        goal="goal",
        departure_time=T0,
        neighbors=graph.__getitem__,
        evaluate_edge=lambda *_: (_ for _ in ()).throw(RuntimeError("boom")),
        objective_count=2,
        cancel_check=lambda: True,
        scope_digest=SCOPE,
        incumbent_bound_certificate=certificate,
    )
    assert result.status is NonFifoSearchStatus.CANCELLED
    assert result.incumbent_bound_pruned == 0
    assert result.incumbent_bound_rejected >= 1
