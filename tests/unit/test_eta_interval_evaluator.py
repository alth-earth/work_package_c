"""Adversarial tests for the C-internal ETA interval evaluator."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import PlannerConfig
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.eta_interval import EtaInterval, EtaIntervalStatus
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _evaluator(
    *,
    lower_factor: float = 0.8,
    upper_factor: float = 0.8,
    risk: float = 0.1,
    confidence: float = 0.9,
    hard_mask: np.ndarray | None = None,
    maximum_risk: float | None = None,
    scope: dict[str, str] | None = None,
    evaluator_certified: bool = True,
    continuity_certified: bool = True,
    contraction_bound: float | None = 0.2,
    **kwargs: object,
) -> TemporalEtaIntervalEvaluator:
    lower = make_frame(
        T0,
        np.full((2, 2), risk),
        risk_id="risk-lower",
        confidence=np.full((2, 2), confidence),
        hard_mask=hard_mask,
        environment_speed_factor=np.full((2, 2), lower_factor),
    )
    upper = make_frame(
        T0 + timedelta(hours=1),
        np.full((2, 2), risk),
        risk_id="risk-upper",
        confidence=np.full((2, 2), confidence),
        environment_speed_factor=np.full((2, 2), upper_factor),
    )
    upper_2 = make_frame(
        T0 + timedelta(hours=2),
        np.full((2, 2), risk),
        risk_id="risk-upper-2",
        confidence=np.full((2, 2), confidence),
        environment_speed_factor=np.full((2, 2), upper_factor),
    )
    request = type(
        "Request",
        (),
        {"departure_time": T0, "maximum_risk": maximum_risk},
    )()
    return TemporalEtaIntervalEvaluator(
        RiskSampler((lower, upper, upper_2)),
        VesselPerformanceModel(10.0, 5.0, 12.0, 0.2),
        request,
        scope or {"edge_evaluator_digest": "explicit:fixture-edge-v1"},
        edge_sample_points=(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0)),
        edge_distance_km=10.0,
        planner_config=PlannerConfig(),
        evaluator_certified=evaluator_certified,
        continuity_certified=continuity_certified,
        contraction_bound=contraction_bound,
        **kwargs,
    )


def test_unique_root_requires_certified_contraction_and_binds_scope_policy() -> None:
    evaluator = _evaluator()
    evidence = evaluator.evaluate(T0, EtaInterval(0.1, 0.8))

    assert evidence.status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert evidence.image is not None
    assert evidence.certificate is not None
    assert evidence.authorization_usable
    assert evidence.permits_dominance
    assert evidence.certificate.policy_digest == evidence.policy_digest
    assert evidence.certificate.partition_digest is not None


def test_nonunique_endpoint_diagnostic_never_permits_dominance() -> None:
    from arctic_route_planning.planners.eta_interval import qualify_eta_interval

    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda domain: domain,
        scope={"edge_evaluator_digest": "explicit:fixture"},
        coverage_complete=True,
        evaluator_certified=True,
        continuity_certified=True,
        endpoint_residuals=(-1.0, 1.0),
    )

    assert certificate.status is EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
    assert certificate.usable
    assert not certificate.authorization_usable
    assert not certificate.permits({"edge_evaluator_digest": "explicit:fixture"})


def test_frame_boundary_is_an_explicit_uncertain_partition_without_continuity_proof() -> None:
    evaluator = _evaluator(continuity_certified=False)
    evidence = evaluator.evaluate(T0 + timedelta(minutes=30), EtaInterval(0.1, 0.8))

    assert evidence.partition_boundaries
    assert evidence.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    assert not evidence.authorization_usable
    assert evidence.certificate is None or not evidence.certificate.authorization_usable


def test_hard_mask_threshold_and_missing_evaluator_are_fail_closed() -> None:
    hard_mask = np.zeros((2, 2), dtype=np.bool_)
    hard_mask[0, 0] = True
    masked = _evaluator(hard_mask=hard_mask).evaluate(T0, EtaInterval(0.1, 0.8))
    risk = _evaluator(maximum_risk=0.05).evaluate(T0, EtaInterval(0.1, 0.8))
    unknown = _evaluator(
        scope={"edge_evaluator_digest": "unknown:fixture"},
    ).evaluate(T0, EtaInterval(0.1, 0.8))

    assert masked.reason == "hard_mask_discontinuity"
    assert masked.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    assert risk.reason == "risk_threshold_crossing"
    assert not risk.authorization_usable
    assert unknown.reason == "unknown_evaluator_identity"
    assert unknown.status is EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE


def test_scope_and_policy_digest_mismatch_is_not_reusable() -> None:
    evaluator = _evaluator()
    wrong_scope = evaluator.evaluate(
        T0,
        EtaInterval(0.1, 0.8),
        scope={"edge_evaluator_digest": "explicit:other"},
    )
    policy_scope = {
        "edge_evaluator_digest": "explicit:fixture-edge-v1",
        "eta_policy_digest": "wrong-policy",
    }
    policy_evaluator = _evaluator(scope=policy_scope)
    wrong_policy = policy_evaluator.evaluate(T0, EtaInterval(0.1, 0.8))

    assert wrong_scope.reason == "scope_mismatch"
    assert not wrong_scope.authorization_usable
    assert wrong_policy.reason == "policy_digest_mismatch"
    assert not wrong_policy.authorization_usable
