"""P0.1 finite FIFO qualification and fail-closed dominance checks."""

from __future__ import annotations

from datetime import timedelta

import pytest

from arctic_route_planning.planners.temporal_bounds import (
    TemporalStateBoundCertificate,
    TemporalStateBoundStatus,
    qualify_state_bound,
)
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
    TemporalSessionIdentityMismatch,
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


@pytest.mark.parametrize("flag", ("suffix_monotone", "coverage_complete"))
def test_dominance_rejects_non_qualifying_certificate_and_records_reason(flag: str) -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    fifo = qualify_fifo(
        ("edge",),
        (T0, T0 + timedelta(hours=1)),
        lambda _edge, departure: departure + timedelta(hours=1),
        scope=scope,
    )
    certificate = TemporalDominanceCertificate.from_fifo(
        fifo,
        suffix_monotone=flag != "suffix_monotone",
        coverage_complete=flag != "coverage_complete",
    )
    planner.dominance_policy = TemporalDominancePolicy.certified_only(certificate)
    context = planner._new_execution_context()
    context.dominance_scope = scope

    assert not planner._authorize_dominance(context, request)
    expected_reason = (
        "suffix_not_monotone" if flag == "suffix_monotone" else "coverage_incomplete"
    )
    assert context.diagnostics.dominance_rejection_reasons == {expected_reason: 1}


@pytest.mark.parametrize(
    "scope_field",
    (
        "risk_frame_content_digest",
        "risk_identity_digest",
        "generation_id",
        "input_revision",
        "grid_digest",
        "vessel_model_digest",
        "planner_config_digest",
        "eta_policy_digest",
        "search_limits_digest",
        "edge_evaluator_digest",
        "objective",
        "start",
        "goal",
        "departure_time",
        "maximum_elapsed_seconds",
        "maximum_risk",
        "time_bucket_seconds",
        "edge_sample_count",
        "edge_ids",
        "probe_times",
    ),
)
def test_dominance_scope_mismatch_is_rejected_for_each_identity_field(
    scope_field: str,
) -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(
        request,
        edge_ids=("edge",),
        probe_times=(T0, T0 + timedelta(hours=1)),
    )
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
        scope=scope,
    )
    policy = TemporalDominancePolicy.certified_only(certificate)
    mismatched = dict(scope.mapping)
    mismatched[scope_field] = f"mismatch:{scope_field}"

    assert scope.matches(scope)
    assert not policy.permits(mismatched)


def test_dominance_does_not_mutate_existing_labels_when_pruning_new_label() -> None:
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
    original_labels = dict(labels)

    assert planner._should_prune_dominated_label(
        ((1, 1), (0, 1), T0 + timedelta(minutes=10)),
        1.0,
        labels,
        request,
        context=context,
    )
    assert labels == original_labels


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


def test_checkpoint_restore_rejects_dominance_policy_change() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    session = planner.create_session(request)
    assert planner.advance_session(session, expansion_slice=1) is None
    checkpoint = planner.checkpoint_session(session)

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

    with pytest.raises(TemporalSessionIdentityMismatch, match="identity fence"):
        planner.restore_session(checkpoint, request=request)


def test_state_bound_is_disabled_by_default_and_binds_identity() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    identity = TemporalSessionIdentity.from_planner(planner, request)
    result = planner.plan(request)

    assert identity.state_bound_policy_digest == "temporal-state-bound-disabled"
    assert result.diagnostics.state_bound_checks == 0
    assert result.diagnostics.state_bound_pruned == 0


def test_certified_state_bound_prunes_only_newly_generated_nodes() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    planner.state_bound_certificate = TemporalStateBoundCertificate.certified(
        scope,
        allowed_nodes=((1, 0), (1, 1), (1, 2), (1, 3)),
        proof_digest="corridor-proof-v1",
    )
    context = planner._new_execution_context()

    assert planner._authorize_state_bound(context, request)
    assert not planner._should_prune_state_bound(
        ((1, 1), (0, 1), T0 + timedelta(minutes=10)),
        request,
        context=context,
    )
    assert planner._should_prune_state_bound(
        ((0, 1), (0, 1), T0 + timedelta(minutes=10)),
        request,
        context=context,
    )
    assert context.diagnostics.state_bound_checks == 2
    assert context.diagnostics.state_bound_pruned == 1
    assert context.diagnostics.state_bound_rejected == 0


def test_state_bound_scope_mismatch_is_fail_closed_and_recorded() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    mismatched = dict(scope.mapping)
    mismatched["goal"] = (0, 3)
    planner.state_bound_certificate = TemporalStateBoundCertificate.certified(
        mismatched,
        allowed_nodes=((1, 0), (1, 1), (1, 2), (1, 3)),
        proof_digest="corridor-proof-v1",
    )
    context = planner._new_execution_context()

    assert not planner._authorize_state_bound(context, request)
    assert context.diagnostics.state_bound_rejected == 1
    assert context.diagnostics.state_bound_rejection_reasons == {"scope_mismatch": 1}
    assert not planner._should_prune_state_bound(
        ((0, 1), (0, 1), T0 + timedelta(minutes=10)),
        request,
        context=context,
    )


def test_checkpoint_restore_rejects_state_bound_policy_change() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    session = planner.create_session(request)
    assert planner.advance_session(session, expansion_slice=1) is None
    checkpoint = planner.checkpoint_session(session)

    planner.state_bound_certificate = TemporalStateBoundCertificate.certified(
        planner.temporal_scope(request),
        allowed_nodes=((1, 0), (1, 1), (1, 2), (1, 3)),
        proof_digest="corridor-proof-v1",
    )

    with pytest.raises(TemporalSessionIdentityMismatch, match="identity fence"):
        planner.restore_session(checkpoint, request=request)


def test_state_bound_qualification_derives_excluded_nodes_and_proof_scope() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    universe = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    certificate = qualify_state_bound(
        scope,
        ((1, 0), (1, 1), (1, 2), (1, 3)),
        universe_nodes=universe,
        exclusion_proof=True,
        proof_digest="corridor-proof-v2",
        coverage_complete=True,
        evaluator_certified=True,
    )

    assert certificate.status is TemporalStateBoundStatus.CERTIFIED
    assert certificate.usable
    assert certificate.excluded_nodes
    assert set(certificate.allowed_nodes).isdisjoint(certificate.excluded_nodes)
    assert certificate.permits(scope)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    (
        ({"exclusion_proof": False, "coverage_complete": True, "evaluator_certified": True}, "missing_exclusion_proof"),
        ({"exclusion_proof": True, "coverage_complete": False, "evaluator_certified": True}, "coverage_incomplete"),
        ({"exclusion_proof": True, "coverage_complete": True, "evaluator_certified": False}, "unknown_evaluator"),
        ({"exclusion_proof": True, "coverage_complete": True, "evaluator_certified": True}, "missing_proof_digest"),
    ),
)
def test_state_bound_qualification_rejects_incomplete_proof(kwargs, reason: str) -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    universe = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    certificate = qualify_state_bound(
        planner.temporal_scope(request),
        ((1, 0), (1, 1)),
        universe_nodes=universe,
        proof_digest=None if reason == "missing_proof_digest" else "proof",
        **kwargs,
    )

    assert certificate.status is TemporalStateBoundStatus.REJECTED
    assert not certificate.usable
    assert certificate.reason == reason


def test_state_bound_identity_digest_changes_when_proof_changes() -> None:
    planner = _planner()
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)
    scope = planner.temporal_scope(request)
    universe = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    first = qualify_state_bound(
        scope,
        ((1, 0), (1, 1)),
        universe_nodes=universe,
        exclusion_proof=True,
        proof_digest="proof-a",
        coverage_complete=True,
        evaluator_certified=True,
    )
    second = qualify_state_bound(
        scope,
        ((1, 0), (1, 1)),
        universe_nodes=universe,
        exclusion_proof=True,
        proof_digest="proof-b",
        coverage_complete=True,
        evaluator_certified=True,
    )

    assert first.digest != second.digest
