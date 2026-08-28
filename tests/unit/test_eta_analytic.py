"""Tests for mechanically derived ETA and FIFO proof evidence."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.eta_analytic import (
    NavigabilityStatus,
    SlopeInterval,
    derive_operator_sensitivity,
    qualify_analytic_eta,
)
from arctic_route_planning.planners.eta_interval import EtaInterval, EtaIntervalStatus
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.planners.temporal_qualification import FifoStatus
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _scope() -> dict[str, str]:
    return {
        "edge_evaluator_digest": "certified:edge-v1",
        "evaluator_certification": "certified:c.temporal-evaluator.v1",
    }


def _vessel() -> VesselPerformanceModel:
    return VesselPerformanceModel(10.0, 5.0, 12.0, 0.2)


def test_zero_factor_slope_derives_unique_root_and_fifo() -> None:
    departure, travel, contraction = derive_operator_sensitivity(
        edge_distance_km=10.0,
        vessel_model=_vessel(),
        speed_factor_slope=SlopeInterval(0.0, 0.0),
    )
    certificate = qualify_analytic_eta(
        domain=EtaInterval(0.1, 0.8),
        image=EtaInterval(0.4, 0.6),
        scope=_scope(),
        policy_digest="policy-v1",
        partition_digest="partition-v1",
        coverage_complete=True,
        evaluator_certified=True,
        continuity_certified=True,
        navigation=NavigabilityStatus.ALWAYS_NAVIGABLE,
        phi_departure_slope=departure,
        phi_travel_slope=travel,
    )

    assert contraction == 0.0
    assert certificate.root_status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert certificate.root_authorized
    assert certificate.fifo_status is FifoStatus.FIFO_CERTIFIED
    assert certificate.fifo_authorized


def test_unique_root_without_fifo_slope_is_not_dominance_authorized() -> None:
    certificate = qualify_analytic_eta(
        domain=EtaInterval(0.1, 0.8),
        image=EtaInterval(0.3, 0.7),
        scope=_scope(),
        policy_digest="policy-v1",
        partition_digest="partition-v1",
        coverage_complete=True,
        evaluator_certified=True,
        continuity_certified=True,
        navigation=NavigabilityStatus.ALWAYS_NAVIGABLE,
        phi_departure_slope=SlopeInterval(-0.2, 0.2),
        phi_travel_slope=SlopeInterval(-0.9, 0.9),
    )

    assert certificate.root_status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert certificate.root_authorized
    assert certificate.fifo_status is FifoStatus.FIFO_UNCERTAIN
    assert not certificate.fifo_authorized
    assert certificate.fifo_reason == "arrival_operator_monotonicity_unproven"


def test_negative_arrival_slope_is_a_fifo_violation_not_a_root_failure() -> None:
    certificate = qualify_analytic_eta(
        domain=EtaInterval(0.1, 0.8),
        image=EtaInterval(0.3, 0.7),
        scope=_scope(),
        policy_digest="policy-v1",
        partition_digest="partition-v1",
        coverage_complete=True,
        evaluator_certified=True,
        continuity_certified=True,
        navigation=NavigabilityStatus.ALWAYS_NAVIGABLE,
        phi_departure_slope=SlopeInterval(-4.0, -3.0),
        phi_travel_slope=SlopeInterval(-0.2, 0.2),
    )

    assert certificate.root_status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert certificate.root_authorized
    assert certificate.fifo_status is FifoStatus.FIFO_VIOLATED
    assert not certificate.fifo_authorized


@pytest.mark.parametrize(
    ("navigation", "expected_reason"),
    [
        (NavigabilityStatus.TRANSITION_OR_UNKNOWN, "navigability_transition_or_unknown"),
        (NavigabilityStatus.ALWAYS_NAVIGABLE, "evaluator_not_certified"),
    ],
)
def test_analytic_proof_fail_closed_for_unknown_navigation_or_evaluator(
    navigation: NavigabilityStatus,
    expected_reason: str,
) -> None:
    certificate = qualify_analytic_eta(
        domain=EtaInterval(0.1, 0.8),
        image=EtaInterval(0.3, 0.7),
        scope=_scope(),
        policy_digest="policy-v1",
        partition_digest="partition-v1",
        coverage_complete=True,
        evaluator_certified=navigation is NavigabilityStatus.TRANSITION_OR_UNKNOWN,
        continuity_certified=True,
        navigation=navigation,
        phi_departure_slope=SlopeInterval(0.0, 0.0),
        phi_travel_slope=SlopeInterval(0.0, 0.0),
    )

    assert not certificate.fifo_authorized
    assert certificate.reason == expected_reason


def test_evaluator_derives_certification_without_caller_proof_flags() -> None:
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            np.full((2, 2), 0.1),
            risk_id=f"risk-{index}",
            confidence=np.full((2, 2), 0.9),
            environment_speed_factor=np.full((2, 2), 0.8),
        )
        for index in range(3)
    )
    sampler = RiskSampler(frames)
    scope = {
        "edge_evaluator_digest": "certified:edge-v1",
        "evaluator_certification": "certified:c.temporal-evaluator.v1",
    }
    evaluator = TemporalEtaIntervalEvaluator(
        sampler,
        _vessel(),
        type("Request", (), {"departure_time": T0, "maximum_risk": None})(),
        scope,
        edge_sample_points=(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0)),
        edge_distance_km=10.0,
    )

    evidence = evaluator.evaluate_analytic(T0, EtaInterval(0.1, 0.8))

    assert evidence.analytic_certificate is not None
    assert evidence.analytic_certificate.root_status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert evidence.fifo_status == FifoStatus.FIFO_CERTIFIED.value
    assert evidence.permits_dominance


def test_varying_frame_speed_factor_is_not_promoted_to_continuity_proof() -> None:
    lower = make_frame(
        T0,
        np.full((2, 2), 0.1),
        risk_id="risk-lower",
        confidence=np.full((2, 2), 0.9),
        environment_speed_factor=np.full((2, 2), 0.8),
    )
    upper = make_frame(
        T0 + timedelta(hours=1),
        np.full((2, 2), 0.1),
        risk_id="risk-upper",
        confidence=np.full((2, 2), 0.9),
        environment_speed_factor=np.full((2, 2), 0.7),
    )
    evaluator = TemporalEtaIntervalEvaluator(
        RiskSampler((lower, upper)),
        _vessel(),
        type("Request", (), {"departure_time": T0, "maximum_risk": None})(),
        _scope(),
        edge_sample_points=(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0)),
        edge_distance_km=10.0,
    )

    evidence = evaluator.evaluate_analytic(T0, EtaInterval(0.1, 0.8))

    assert evidence.analytic_certificate is not None
    assert not evidence.analytic_certificate.fifo_authorized
    assert evidence.analytic_certificate.reason == "continuity_not_certified"
