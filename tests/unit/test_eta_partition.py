"""Tests for sampler-derived ETA partition evidence."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.eta_interval import EtaInterval
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.planners.eta_partition import (
    EtaPartitionBoundaryEvidence,
    EvaluatorCertificateStatus,
    RiskEvaluatorCertificate,
    TemporalEtaPartitionEvaluator,
    partition_travel_domain,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _evaluator(*, factors: tuple[float, ...] = (0.8, 0.8, 0.8)) -> TemporalEtaIntervalEvaluator:
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            np.full((2, 2), 0.1),
            risk_id=f"risk-{index}",
            confidence=np.full((2, 2), 0.9),
            environment_speed_factor=np.full((2, 2), factor),
        )
        for index, factor in enumerate(factors)
    )
    sampler = RiskSampler(frames)
    request = type("Request", (), {"departure_time": T0, "maximum_risk": None})()
    return TemporalEtaIntervalEvaluator(
        sampler,
        VesselPerformanceModel(10.0, 5.0, 12.0, 0.2),
        request,
        {"edge_evaluator_digest": "explicit:partition-fixture"},
        edge_sample_points=(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0)),
        edge_distance_km=10.0,
    )


def test_certificate_is_derived_from_sampler_and_scope_bound() -> None:
    evaluator = _evaluator()
    certificate = RiskEvaluatorCertificate.from_sampler(evaluator.risk_sampler)

    assert certificate.status is EvaluatorCertificateStatus.CERTIFIED
    bound = certificate.bind_scope(evaluator.scope)
    assert certificate.permits(bound)
    assert not certificate.permits(evaluator.scope)
    assert bound.mapping["risk_interval_evaluator_digest"] == certificate.proof_digest


def test_partition_cuts_at_frame_boundary_and_certifies_one_unique_root() -> None:
    evaluator = _evaluator()
    domain = EtaInterval(0.1, 2.0)
    points = evaluator.edge_sample_points
    cuts = partition_travel_domain(evaluator.risk_sampler, T0, domain, points)

    assert cuts == (0.1, 1.0, 2.0)
    evidence = TemporalEtaPartitionEvaluator(evaluator).evaluate(T0, domain)

    assert evidence.boundaries == (1.0,)
    assert evidence.status == "PARTITION_CERTIFIED"
    assert evidence.certified_partition_count == 1
    assert evidence.coverage_ratio == 1.0
    assert evidence.permits_dominance is False


def test_negative_frame_speed_jump_is_reported_without_authorization() -> None:
    left = EtaInterval(0.8, 0.9)
    right = EtaInterval(0.4, 0.5)
    boundary = EtaPartitionBoundaryEvidence(
        boundary_hours=1.0,
        left_image=left,
        right_image=right,
        status="FIFO_VIOLATED",
        reason="negative_travel_operator_jump",
    )

    assert boundary.status == "FIFO_VIOLATED"
    assert boundary.reason == "negative_travel_operator_jump"


def test_scope_mismatch_is_fail_closed() -> None:
    evaluator = _evaluator()
    evidence = TemporalEtaPartitionEvaluator(evaluator).evaluate(
        T0,
        EtaInterval(0.1, 1.0),
        scope={"edge_evaluator_digest": "explicit:other"},
    )

    assert evidence.status == "UNCERTAIN"
    assert evidence.reason == "scope_mismatch"
    assert evidence.permits_dominance is False
