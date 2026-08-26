"""P1 resumable-session invariants for the internal temporal candidate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.errors import PlanningCancelled
from arctic_route_planning.planners._archive.temporal_session import (
    TemporalSessionIdentity,
    TemporalSessionIdentityMismatch,
    TemporalSessionRestoreError,
    TemporalSessionState,
)
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalSearchLimitExceeded,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.time_dependent_astar import PlanningRequest
from arctic_route_planning.risk import RiskSampler

from .factories import T0
from .test_temporal_label_astar import _mapped_edge, _planner, _scripted_edge


def _request(*, cancel_check=None, objective=ObjectiveMode.RECOMMENDED):
    return PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=objective,
        cancel_check=cancel_check,
    )


def _discrete(result):
    metrics = result.planning_result.metrics
    return (
        result.nodes,
        result.planning_result.total_cost_hours,
        tuple(step.eta for step in result.steps),
        metrics.expanded_states,
        metrics.generated_states,
        metrics.heap_pushes,
        metrics.heap_pops,
        metrics.stale_pops,
    )


def test_one_shot_and_one_expansion_slices_are_semantically_identical() -> None:
    first_planner = _planner()
    first = first_planner.plan(_request())

    second_planner = _planner()
    session = second_planner.create_session(_request())
    result = None
    while result is None:
        result = second_planner.advance_session(session, expansion_slice=1)

    assert session.state is TemporalSessionState.GOAL_CERTIFIED
    assert _discrete(result) == _discrete(first)


def test_checkpoint_preserves_stale_queue_and_restores_without_restarting() -> None:
    baseline_planner = _planner()
    baseline_planner._injected_edge_evaluator = _scripted_edge(baseline_planner, 0.25)
    baseline = baseline_planner.plan(_request())

    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(planner, 0.25)
    session = planner.create_session(_request())
    assert planner.advance_session(session, expansion_slice=1) is None
    checkpoint = planner.checkpoint_session(session)
    assert checkpoint.queue
    assert checkpoint.state is TemporalSessionState.PAUSED
    restored = planner.restore_session(checkpoint, request=_request())
    result = planner.advance_session(restored)
    assert result is not None
    assert restored.context.diagnostics.edge_evaluations == (
        baseline.diagnostics.edge_evaluations
    )
    assert result.planning_result.metrics.compute_ms > 0


def test_identity_mismatch_does_not_mutate_original_session() -> None:
    planner = _planner()
    session = planner.create_session(_request())
    planner.advance_session(session, expansion_slice=1)
    checkpoint = planner.checkpoint_session(session)
    bad = replace(checkpoint.identity, input_revision=checkpoint.identity.input_revision + 1)

    with pytest.raises(TemporalSessionIdentityMismatch):
        planner.restore_session(checkpoint, request=_request(), identity=bad)
    assert session.state is TemporalSessionState.PAUSED
    assert planner.checkpoint_session(session).state_digest == checkpoint.state_digest


def test_restore_recomputes_current_planner_fence_even_with_explicit_identity() -> None:
    planner = _planner()
    session = planner.create_session(_request())
    planner.advance_session(session, expansion_slice=1)
    checkpoint = planner.checkpoint_session(session)
    planner._injected_edge_evaluator = _scripted_edge(planner, 0.5)

    with pytest.raises(TemporalSessionIdentityMismatch):
        planner.restore_session(
            checkpoint,
            request=_request(),
            identity=checkpoint.identity,
        )
    assert session.state is TemporalSessionState.PAUSED


def test_restore_rejects_changed_current_risk_window_content() -> None:
    planner = _planner()
    session = planner.create_session(_request())
    planner.advance_session(session, expansion_slice=1)
    checkpoint = planner.checkpoint_session(session)
    changed_frames = list(planner.risk_sampler.frames)
    changed_frames[0] = replace(changed_frames[0], risk_id="risk-content-changed")
    planner.risk_sampler = RiskSampler(tuple(changed_frames))

    with pytest.raises(TemporalSessionIdentityMismatch):
        planner.restore_session(checkpoint, request=_request())


def test_committed_window_identity_pair_must_be_content_addressed() -> None:
    planner = _planner()

    with pytest.raises(TemporalSessionIdentityMismatch):
        TemporalSessionIdentity.from_planner(
            planner,
            _request(),
            risk_window_content_digest="a" * 64,
            risk_window_commit_id="risk-window-sha256-" + "b" * 64,
        )


def test_bundle_has_three_objective_scoped_mutable_states() -> None:
    planner = _planner()
    bundle = planner.create_session_bundle(_request())

    assert set(bundle.sessions) == set(ObjectiveMode)
    assert len({id(session.context) for session in bundle.sessions.values()}) == 3
    assert len({id(session.labels) for session in bundle.sessions.values()}) == 3
    assert {session.request.objective for session in bundle.sessions.values()} == set(ObjectiveMode)
    with pytest.raises(TypeError):
        bundle.sessions[ObjectiveMode.RECOMMENDED] = bundle[ObjectiveMode.FASTEST]  # type: ignore[index]


def test_checkpoint_drops_process_local_cancel_callback() -> None:
    planner = _planner()
    session = planner.create_session(_request(cancel_check=lambda: False))
    planner.advance_session(session, expansion_slice=1)

    checkpoint = planner.checkpoint_session(session)

    assert checkpoint.request.cancel_check is None


@pytest.mark.parametrize(
    "state",
    (
        TemporalSessionState.GOAL_CERTIFIED,
        TemporalSessionState.EXHAUSTED,
        TemporalSessionState.CANCELLED,
        TemporalSessionState.FAILED,
    ),
)
def test_all_terminal_checkpoint_states_are_not_restorable(state) -> None:
    planner = _planner()
    session = planner.create_session(_request())
    planner.advance_session(session, expansion_slice=1)
    checkpoint = replace(
        planner.checkpoint_session(session),
        state=state,
        state_digest="",
    )

    with pytest.raises(TemporalSessionRestoreError):
        planner.restore_session(checkpoint, request=_request())


def test_restore_rejects_tampered_checkpoint_state_digest() -> None:
    planner = _planner()
    session = planner.create_session(_request())
    planner.advance_session(session, expansion_slice=1)
    checkpoint = planner.checkpoint_session(session)
    object.__setattr__(checkpoint, "state_digest", "0" * 64)

    with pytest.raises(TemporalSessionRestoreError, match="state digest mismatch"):
        planner.restore_session(checkpoint, request=_request())


def test_cancelled_session_is_terminal_and_not_restorable() -> None:
    cancelled = [False]
    planner = _planner()
    session = planner.create_session(_request(cancel_check=lambda: cancelled[0]))
    cancelled[0] = True

    with pytest.raises(PlanningCancelled):
        planner.advance_session(session, expansion_slice=1)
    assert session.state is TemporalSessionState.CANCELLED
    with pytest.raises(TemporalSessionRestoreError):
        planner.restore_session(
            planner.checkpoint_session(session),
            request=_request(cancel_check=lambda: cancelled[0]),
        )


def test_expansion_limit_is_cumulative_across_slices() -> None:
    planner = _planner(limits=TemporalSearchLimits(max_expansions=2))
    session = planner.create_session(_request())
    assert planner.advance_session(session, expansion_slice=1) is None
    assert planner.advance_session(session, expansion_slice=1) is None
    with pytest.raises(TemporalSearchLimitExceeded):
        planner.advance_session(session, expansion_slice=1)
    assert session.state is TemporalSessionState.FAILED
    assert session.context.diagnostics.expanded_labels == 3


@pytest.mark.parametrize(
    ("limits", "message"),
    (
        (TemporalSearchLimits(max_labels=6), "labels=6"),
        (TemporalSearchLimits(max_queue=5), "queue=5"),
        (TemporalSearchLimits(max_edge_evaluations=6), "edge evaluations=6"),
    ),
)
def test_non_expansion_limits_survive_checkpoint_restore(limits, message) -> None:
    planner = _planner(limits=limits)
    session = planner.create_session(_request())
    assert planner.advance_session(session, expansion_slice=1) is None
    restored = planner.restore_session(
        planner.checkpoint_session(session),
        request=_request(),
    )

    with pytest.raises(TemporalSearchLimitExceeded, match=message):
        planner.advance_session(restored, expansion_slice=1)
    assert restored.state is TemporalSessionState.FAILED


def test_checkpoint_digest_is_deterministic_and_keeps_microsecond_arrivals() -> None:
    planner = _planner()
    planner._injected_edge_evaluator = _scripted_edge(planner, 0.000001)
    request = _request()
    session = planner.create_session(request)
    planner.advance_session(session, expansion_slice=1)
    first = planner.checkpoint_session(session)
    second = planner.checkpoint_session(session)
    assert first.state_digest == second.state_digest
    assert first.queue == second.queue
    assert any(entry[-2].microsecond == T0.microsecond + 3_600 for entry in first.queue)


def test_sliced_restore_keeps_goal_lower_bound_certificate_semantics() -> None:
    def durations(start, end, _departure_time):
        return {
            ((0, 0), (0, 1)): 0.1,
            ((0, 1), (0, 2)): 5.0,
            ((0, 0), (1, 0)): 0.1,
            ((1, 0), (1, 1)): 0.1,
            ((1, 1), (1, 2)): 0.1,
            ((1, 2), (0, 2)): 0.1,
        }.get((start, end), 10.0)

    request = PlanningRequest(start=(0, 0), goal=(0, 2), departure_time=T0)
    baseline_planner = _planner(rows=2, columns=3, allow_diagonal=False)
    baseline_planner._injected_edge_evaluator = _mapped_edge(baseline_planner, durations)
    baseline = baseline_planner.plan(request)

    planner = _planner(rows=2, columns=3, allow_diagonal=False)
    planner._injected_edge_evaluator = _mapped_edge(planner, durations)
    session = planner.create_session(request)
    result = None
    while result is None:
        result = planner.advance_session(session, expansion_slice=1)
        if result is None:
            session = planner.restore_session(
                planner.checkpoint_session(session),
                request=request,
            )

    assert result.nodes == baseline.nodes
    assert result.total_cost_hours == pytest.approx(baseline.total_cost_hours)
    assert result.nodes == ((0, 0), (1, 0), (1, 1), (1, 2), (0, 2))
