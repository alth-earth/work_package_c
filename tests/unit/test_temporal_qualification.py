"""P0.1 finite FIFO qualification and fail-closed dominance checks."""

from __future__ import annotations

from datetime import timedelta

import pytest

from arctic_route_planning.planners.temporal_qualification import (
    DominanceMode,
    FifoCertificate,
    FifoStatus,
    TemporalDominanceCertificate,
    TemporalDominancePolicy,
    qualify_fifo,
)
from arctic_route_planning.planners.temporal_session import (
    TemporalSessionIdentity,
)
from arctic_route_planning.planners.time_dependent_astar import PlanningRequest

from .factories import T0
from .test_temporal_label_astar import _planner


def test_fifo_certificate_is_finite_and_scope_bound() -> None:
    scope = {"risk_frame_content_digest": "a" * 64, "generation_id": 4}
    certificate = qualify_fifo(
        ("edge-1", "edge-2"),
        (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )

    assert certificate.status is FifoStatus.FIFO_CERTIFIED
    assert certificate.usable
    assert certificate.probes_evaluated == 6
    assert certificate.minimum_slack_seconds == pytest.approx(3600.0)
    assert certificate.scope.matches(scope)
    assert certificate.digest == certificate.certificate_digest


def test_non_fifo_certificate_keeps_a_counterexample() -> None:
    def arrival(_edge, departure):
        return T0 + timedelta(hours=3) if departure == T0 else T0 + timedelta(hours=2)

    certificate = qualify_fifo(
        ("shock",),
        (T0, T0 + timedelta(hours=1)),
        arrival,
        scope={"fixture": "non-fifo"},
    )

    assert certificate.status is FifoStatus.FIFO_VIOLATED
    assert not certificate.usable
    assert certificate.counterexample is not None
    assert certificate.reason == "later_departure_arrives_earlier"
    assert certificate.counterexample.slack_seconds < 0


@pytest.mark.parametrize(
    ("edges", "times", "reason"),
    (((), (T0, T0 + timedelta(hours=1)), "empty_edge_domain"),
     (("edge",), (T0,), "insufficient_probe_times")),
)
def test_incomplete_probe_domain_is_uncertain(edges, times, reason) -> None:
    certificate = qualify_fifo(
        edges,
        times,
        lambda _edge, departure: departure + timedelta(hours=1),
    )
    assert certificate.status is FifoStatus.FIFO_UNCERTAIN
    assert certificate.reason == reason


def test_evaluation_failure_is_uncertain_and_fail_closed() -> None:
    certificate = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, _departure: None,
    )
    assert certificate.status is FifoStatus.FIFO_UNCERTAIN
    assert certificate.reason == "evaluation_failed:ValueError"


def test_hand_constructed_incomplete_certified_record_is_not_usable() -> None:
    certificate = FifoCertificate(
        status=FifoStatus.FIFO_CERTIFIED,
        scope={"fixture": "incomplete"},
        edge_ids=("edge",),
        probe_times=(),
        tolerance_seconds=0.0,
        probes_evaluated=0,
    )
    assert not certificate.usable


def test_dominance_requires_fifo_suffix_and_coverage() -> None:
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope={"objective": "fastest"},
    )
    incomplete = TemporalDominanceCertificate.from_fifo(
        fifo,
        suffix_monotone=False,
        coverage_complete=True,
    )
    assert incomplete.fifo_certificate.usable
    assert not incomplete.usable
    assert not TemporalDominancePolicy.certified_only(incomplete).permits(fifo.scope)

    complete = TemporalDominanceCertificate.from_fifo(
        fifo,
        suffix_monotone=True,
        coverage_complete=True,
    )
    policy = TemporalDominancePolicy.certified_only(complete)
    assert policy.mode is DominanceMode.CERTIFIED_ONLY
    assert policy.permits(fifo.scope)
    assert not policy.permits({"objective": "low_risk"})


def test_planner_default_policy_is_disabled_and_scope_contains_input_identity() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    assert planner.dominance_policy.mode is DominanceMode.NONE
    scope = planner.temporal_scope(request)
    assert scope.mapping["generation_id"] == planner.risk_identity.generation_id
    assert scope.mapping["objective"] == request.objective.value
    result = planner.plan(request)
    assert result.diagnostics.dominance_policy == "none"
    assert result.diagnostics.dominance_pruned == 0


def test_certified_dominance_prunes_only_a_later_same_heading_label() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )
    planner.dominance_policy = TemporalDominancePolicy.certified_only(
        TemporalDominanceCertificate.from_fifo(
            fifo,
            suffix_monotone=True,
            coverage_complete=True,
        )
    )
    context = planner._new_execution_context()
    context.dominance_scope = scope
    labels = {((1, 1), (0, 1), T0): 1.0}

    assert planner._should_prune_dominated_label(
        ((1, 1), (0, 1), T0 + timedelta(minutes=10)),
        1.0,
        labels,
        request,
        context=context,
    )
    assert not planner._should_prune_dominated_label(
        ((1, 1), (1, 0), T0 + timedelta(minutes=10)),
        1.0,
        labels,
        request,
        context=context,
    )
    assert context.diagnostics.dominance_pruned == 1


def test_certified_policy_is_visible_in_candidate_diagnostics() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )
    planner.dominance_policy = TemporalDominancePolicy.certified_only(
        TemporalDominanceCertificate.from_fifo(
            fifo,
            suffix_monotone=True,
            coverage_complete=True,
        )
    )

    result = planner.plan(request)

    assert result.diagnostics.fifo_status == "FIFO_CERTIFIED"
    assert result.diagnostics.dominance_policy == "certified_only"
    assert result.diagnostics.dominance_scope_match


def test_non_fifo_policy_stays_exact_label_and_records_rejection() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    fifo = qualify_fifo(
        ("shock",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: T0 + timedelta(hours=3)
        if departure == T0
        else T0 + timedelta(hours=2),
        scope=scope,
    )
    planner.dominance_policy = TemporalDominancePolicy.certified_only(
        TemporalDominanceCertificate.from_fifo(
            fifo,
            suffix_monotone=True,
            coverage_complete=True,
        )
    )
    context = planner._new_execution_context()
    context.dominance_scope = scope

    assert not planner._should_prune_dominated_label(
        ((1, 1), (0, 1), T0 + timedelta(minutes=10)),
        1.0,
        {((1, 1), (0, 1), T0): 1.0},
        request,
        context=context,
    )
    assert context.diagnostics.dominance_scope_match is False
    assert context.diagnostics.dominance_rejected == 1


def test_unknown_evaluator_identity_is_fail_closed() -> None:
    class MutableEvaluator:
        def __call__(self, *_args):
            return None

    planner = _planner(edge_evaluator=MutableEvaluator())
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)

    assert scope.mapping["edge_evaluator_digest"].startswith("unknown:")
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )
    certificate = TemporalDominanceCertificate.from_fifo(
        fifo,
        suffix_monotone=True,
        coverage_complete=True,
    )
    assert fifo.usable
    assert not certificate.usable

    planner.dominance_policy = TemporalDominancePolicy.certified_only(certificate)
    context = planner._new_execution_context()
    context.dominance_scope = scope
    assert not planner._authorize_dominance(context, request)
    assert context.diagnostics.dominance_rejection_reasons == {"unknown_evaluator": 1}


def test_session_identity_binds_the_dominance_policy_digest() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    disabled = TemporalSessionIdentity.from_planner(planner, request)

    scope = planner.temporal_scope(request)
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )
    planner.dominance_policy = TemporalDominancePolicy.certified_only(
        TemporalDominanceCertificate.from_fifo(
            fifo,
            suffix_monotone=True,
            coverage_complete=True,
        )
    )
    enabled = TemporalSessionIdentity.from_planner(planner, request)

    assert disabled.dominance_policy_digest != enabled.dominance_policy_digest
    assert disabled.session_id != enabled.session_id
