"""P2 contract tests for exact-goal monotonic reuse.

The tests lock the internal API to ``certify_session``/``try_reuse`` and keep
the explicit ``reuse_or_plan`` control fallback separate from a reuse miss.
They do not provide compatibility adapters: a changed API must fail loudly so
the implementation and its contract can be updated together.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.errors import PlanningCancelled
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners._archive.temporal_reuse import (
    TemporalGoalCertificate,
    TemporalOpenTermination,
    TemporalReuseCertificateError,
    TemporalReuseReason,
    TemporalReuseStatus,
    certify_goal,
    certify_session,
    reuse_or_plan,
    try_reuse,
)
from arctic_route_planning.planners._archive.temporal_session import TemporalSessionState
from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar
from arctic_route_planning.planners.time_dependent_astar import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame
from .test_temporal_label_astar import _planner
from .test_temporal_session import _mapped_edge, _scripted_edge


def _request(
    *,
    objective: ObjectiveMode = ObjectiveMode.RECOMMENDED,
    maximum_elapsed: timedelta | None = timedelta(hours=8),
    maximum_risk: float | None = 1.0,
    cancel_check=None,
    start=(1, 0),
    goal=(1, 3),
    use_heuristic: bool = False,
) -> PlanningRequest:
    return PlanningRequest(
        start=start,
        goal=goal,
        departure_time=T0,
        objective=objective,
        maximum_elapsed=maximum_elapsed,
        maximum_risk=maximum_risk,
        cancel_check=cancel_check,
        use_heuristic=use_heuristic,
    )


def _finish(planner: TemporalLabelAStar, request: PlanningRequest):
    session = planner.create_session(request)
    result = None
    while result is None:
        result = planner.advance_session(session)
    assert session.state is TemporalSessionState.GOAL_CERTIFIED
    return session, result


def _semantic_result(result):
    planning_result = result.planning_result
    return (
        planning_result.nodes,
        planning_result.total_cost_hours,
        tuple(step.eta for step in planning_result.steps),
        tuple(step.source_risk_ids for step in planning_result.steps),
        planning_result.distance_km,
        planning_result.travel_hours,
        planning_result.average_risk,
        planning_result.maximum_risk,
        planning_result.minimum_confidence,
    )


def _certificate_bounds(certificate):
    assert certificate.upper_bound is not None
    assert certificate.epsilon is not None
    assert certificate.lower_bound is not None
    return certificate.upper_bound, certificate.lower_bound, certificate.epsilon


def _certificate_reason(certificate) -> str:
    return certificate.open_termination.value


def _certified(session):
    return certify_session(session)


def _reuse(certified, planner, request):
    return try_reuse(certified, planner, request)


def _result(outcome):
    assert outcome.result is not None
    return outcome.result


def _assert_hit(outcome, *, monotonic: bool = False) -> None:
    assert outcome.hit
    assert outcome.reused
    assert outcome.status is (
        TemporalReuseStatus.HIT_MONOTONIC if monotonic else TemporalReuseStatus.HIT_EXACT
    )


def _assert_miss(outcome, reason: TemporalReuseReason) -> None:
    assert outcome.status is TemporalReuseStatus.MISS_INCOMPATIBLE
    assert outcome.used_search is False
    assert outcome.reason is reason


def _risk_planner(risk_value: float) -> TemporalLabelAStar:
    shape = (3, 4)
    risk = np.full(shape, risk_value, dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=offset),
            risk,
            risk_id=f"reuse-risk-{offset}",
            latitudes=(0.0, 0.05, 0.10),
            longitudes=(0.0, 0.05, 0.10, 0.15),
        )
        for offset in (0, 1, 3)
    )
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    sampler = RiskSampler(frames)
    vessel = VesselPerformanceModel(10.0, 2.0, 12.0, 0.2)
    return TemporalLabelAStar(grid, sampler, vessel)


def test_open_bound_certificate_is_recomputed_after_goal_incumbent() -> None:
    def durations(start, end, _departure_time):
        return {
            ((0, 0), (0, 1)): 0.1,
            ((0, 1), (0, 2)): 5.0,
            ((0, 0), (1, 0)): 0.1,
            ((1, 0), (1, 1)): 0.1,
            ((1, 1), (1, 2)): 0.1,
            ((1, 2), (0, 2)): 0.1,
        }.get((start, end), 10.0)

    planner = _planner(rows=2, columns=3, allow_diagonal=False)
    planner._injected_edge_evaluator = _mapped_edge(planner, durations)
    request = _request(start=(0, 0), goal=(0, 2))
    session, _ = _finish(planner, request)
    observed = certify_goal(session)
    assert observed.open_termination is TemporalOpenTermination.OPEN_BOUND
    upper, lower, epsilon = _certificate_bounds(observed)
    assert upper <= lower + epsilon


def test_open_empty_certificate_is_distinct_from_open_bound() -> None:
    planner = _planner(rows=2, columns=2, allow_diagonal=False)

    def direct_only(start, end, departure_time, previous_heading, request, cost_model):
        if (start, end) != ((0, 0), (0, 1)):
            from arctic_route_planning.planners.temporal_label_astar import _RejectedEdge

            raise _RejectedEdge("hard")
        return _scripted_edge(planner, 0.25)(
            start,
            end,
            departure_time,
            previous_heading,
            request,
            cost_model,
        )

    planner._injected_edge_evaluator = direct_only
    session, result = _finish(planner, _request(start=(0, 0), goal=(0, 1)))

    certificate = certify_goal(session)
    assert certificate.open_termination is TemporalOpenTermination.OPEN_EMPTY
    assert _semantic_result(result)[0] == ((0, 0), (0, 1))
    assert certificate.lower_bound is None
    assert certificate.status.value == "CERTIFIED_REUSABLE"


def test_certificate_epsilon_is_explicit_and_satisfies_boundary() -> None:
    planner = _planner()
    session, _ = _finish(planner, _request())
    certificate = certify_goal(session)
    upper, lower, epsilon = _certificate_bounds(certificate)

    assert epsilon >= 0.0
    assert upper <= lower + epsilon
    assert certificate.state_digest
    assert certificate.route_digest


def test_stale_frontier_entries_do_not_change_certificate_lower_bound() -> None:
    def durations(start, end, _departure_time):
        return {
            ((0, 0), (0, 1)): 0.1,
            ((0, 1), (0, 2)): 5.0,
            ((0, 0), (1, 0)): 0.1,
            ((1, 0), (1, 1)): 0.1,
            ((1, 1), (1, 2)): 0.1,
            ((1, 2), (0, 2)): 0.1,
        }.get((start, end), 10.0)

    planner = _planner(rows=2, columns=3, allow_diagonal=False)
    planner._injected_edge_evaluator = _mapped_edge(planner, durations)
    session, _ = _finish(planner, _request(start=(0, 0), goal=(0, 2)))
    before = certify_goal(session)
    checkpoint = before.checkpoint
    assert checkpoint is not None
    assert checkpoint.queue
    stale = list(checkpoint.queue[0])
    stale[0] -= 100.0
    stale[1] += 1.0
    tampered_queue = (tuple(stale), *checkpoint.queue)
    tampered = replace(checkpoint, queue=tampered_queue, state_digest="")
    after = TemporalGoalCertificate._from_checkpoint(tampered, keep_snapshot=True)
    assert after.open_termination is before.open_termination
    assert after.lower_bound == pytest.approx(before.lower_bound)


def test_tampered_certificate_or_checkpoint_is_never_a_reuse_hit() -> None:
    planner = _planner()
    request = _request()
    session, _ = _finish(planner, request)
    certified = _certified(session)
    certificate = certified.certificate
    original_upper = certificate.upper_bound
    object.__setattr__(certificate, "upper_bound", float("inf"))
    with pytest.raises(TemporalReuseCertificateError, match="certificate digest mismatch"):
        certified.assert_valid()
    object.__setattr__(certificate, "upper_bound", original_upper)

    checkpoint = certified.checkpoint
    object.__setattr__(checkpoint, "state_digest", "0" * 64)
    outcome = _reuse(certified, planner, request)
    _assert_miss(outcome, TemporalReuseReason.CERTIFICATE_INVALID)


@pytest.mark.parametrize(
    ("label", "target_request"),
    (
        ("exact", _request()),
        ("tighter_horizon", _request(maximum_elapsed=timedelta(hours=2))),
        ("tighter_risk", _request(maximum_risk=0.0)),
        (
            "tighter_both",
            _request(maximum_elapsed=timedelta(hours=2), maximum_risk=0.0),
        ),
    ),
)
def test_exact_or_monotonically_tighter_constraints_hit(label, target_request) -> None:
    planner = _planner()
    source_request = _request(maximum_elapsed=timedelta(hours=8), maximum_risk=1.0)
    source, source_result = _finish(planner, source_request)
    certified = _certified(source)
    before = (
        source.context.diagnostics.expanded_labels,
        source.context.diagnostics.edge_evaluations,
    )

    outcome = _reuse(certified, planner, target_request)

    _assert_hit(outcome, monotonic=label != "exact")
    assert _semantic_result(_result(outcome)) == _semantic_result(source_result)
    assert (
        source.context.diagnostics.expanded_labels,
        source.context.diagnostics.edge_evaluations,
    ) == before


@pytest.mark.parametrize(
    ("label", "source_request", "target_request"),
    (
        (
            "looser_horizon",
            _request(maximum_elapsed=timedelta(hours=2)),
            _request(maximum_elapsed=timedelta(hours=8)),
        ),
        (
            "looser_risk",
            _request(maximum_risk=0.0),
            _request(maximum_risk=1.0),
        ),
        (
            "looser_both",
            _request(maximum_elapsed=timedelta(hours=2), maximum_risk=0.0),
            _request(maximum_elapsed=timedelta(hours=8), maximum_risk=1.0),
        ),
        (
            "incompatible_objective",
            _request(objective=ObjectiveMode.RECOMMENDED),
            _request(objective=ObjectiveMode.FASTEST),
        ),
        (
            "route_violates_horizon",
            _request(maximum_elapsed=timedelta(hours=8)),
            _request(maximum_elapsed=timedelta(minutes=1)),
        ),
    ),
)
def test_looser_incompatible_or_unsatisfied_constraints_miss(
    label,
    source_request,
    target_request,
) -> None:
    planner = _planner()
    source, _ = _finish(planner, source_request)
    outcome = _reuse(_certified(source), planner, target_request)

    assert not outcome.hit, label
    expected = {
        "looser_horizon": TemporalReuseReason.CONSTRAINT_WIDENING,
        "looser_risk": TemporalReuseReason.CONSTRAINT_WIDENING,
        "looser_both": TemporalReuseReason.CONSTRAINT_WIDENING,
        "incompatible_objective": TemporalReuseReason.IDENTITY_MISMATCH,
        "route_violates_horizon": TemporalReuseReason.ROUTE_VIOLATES_TARGET,
    }[label]
    _assert_miss(outcome, expected)


def test_result_that_violates_target_risk_constraint_misses() -> None:
    planner = _risk_planner(0.4)
    source_request = _request(maximum_elapsed=timedelta(hours=8), maximum_risk=0.5)
    target_request = _request(maximum_elapsed=timedelta(hours=8), maximum_risk=0.1)
    source, _ = _finish(planner, source_request)

    outcome = _reuse(_certified(source), planner, target_request)

    _assert_miss(outcome, TemporalReuseReason.ROUTE_VIOLATES_TARGET)


def test_objective_sessions_are_isolated_for_reuse() -> None:
    planner = _planner()
    bundle = planner.create_session_bundle(_request())
    recommended = bundle[ObjectiveMode.RECOMMENDED]
    fastest = bundle[ObjectiveMode.FASTEST]
    planner.advance_session(recommended)
    planner.advance_session(fastest)
    planner.advance_session(recommended)
    planner.advance_session(fastest)

    while recommended.state is not TemporalSessionState.GOAL_CERTIFIED:
        planner.advance_session(recommended)
    while fastest.state is not TemporalSessionState.GOAL_CERTIFIED:
        planner.advance_session(fastest)

    outcome = _reuse(
        _certified(recommended),
        planner,
        _request(objective=ObjectiveMode.FASTEST),
    )
    _assert_miss(outcome, TemporalReuseReason.IDENTITY_MISMATCH)
    assert recommended.identity.objective is ObjectiveMode.RECOMMENDED
    assert fastest.identity.objective is ObjectiveMode.FASTEST


def test_cancel_is_propagated_and_never_control_fallback(monkeypatch) -> None:
    planner = _planner()
    source, _ = _finish(planner, _request())
    called = False

    def control_must_not_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("control fallback was invoked after cancellation")

    monkeypatch.setattr(planner, "plan", control_must_not_run)
    with pytest.raises(PlanningCancelled):
        reuse_or_plan(
            _certified(source),
            planner,
            _request(cancel_check=lambda: True),
        )
    assert called is False


def test_candidate_failure_has_explicit_control_fallback() -> None:
    candidate = _planner()
    control = TimeDependentAStar(candidate.grid, candidate.risk_sampler, candidate.vessel_model)
    outcome = reuse_or_plan(
        None,
        candidate,
        _request(),
        fallback_planner=control,
    )

    assert outcome.status is TemporalReuseStatus.FALLBACK_CONTROL
    assert outcome.used_search is True
    assert outcome.fallback_reason == TemporalReuseReason.NO_CERTIFICATE.value
    assert outcome.result is not None


def test_reuse_does_not_touch_formal_publication_or_ingress() -> None:
    source = TemporalLabelAStar.__module__
    assert source == "arctic_route_planning.planners.temporal_label_astar"
    assert not hasattr(TemporalLabelAStar, "execute")
    assert not hasattr(TemporalLabelAStar, "execute_four_layer")
