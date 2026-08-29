"""Fail-closed tests for the environmental speed lower-time envelope."""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.temporal_environment_envelope import (
    EnvironmentalSpeedEnvelopeStatus,
    qualify_environmental_speed_envelope,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _sampler(*, hard_mask: np.ndarray | None = None, with_speed: bool = True) -> RiskSampler:
    shape = (2, 2)
    risk = np.zeros(shape, dtype=np.float32)
    factor = np.full(shape, 0.5, dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=index),
            risk,
            risk_id=f"environment-envelope-{index}",
            hard_mask=hard_mask,
            environment_speed_factor=factor if with_speed else None,
            latitudes=(0.0, 1.0),
            longitudes=(0.0, 1.0),
        )
        for index in (0, 1, 2)
    )
    return RiskSampler(frames)


def _model() -> VesselPerformanceModel:
    return VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )


def _scope() -> TemporalScope:
    return TemporalScope.from_mapping({"edge_evaluator_digest": "explicit:edge-v1"})


def _edges() -> tuple[tuple[tuple[int, int], tuple[int, int], float, tuple[GeoPoint, ...]], ...]:
    points = (GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0))
    return (((0, 0), (0, 1), 10.0, points),)


def test_environmental_upper_factor_produces_conservative_lower_time() -> None:
    sampler = _sampler()
    evidence = qualify_environmental_speed_envelope(
        risk_sampler=sampler,
        vessel_model=_model(),
        scope=_scope(),
        expected_scope=_scope(),
        departure_lower=T0,
        horizon_hours=1.0,
        edges=_edges(),
        universe_nodes=((0, 0), (0, 1)),
        evaluator_certified=True,
    )

    assert evidence.status is EnvironmentalSpeedEnvelopeStatus.CERTIFIED
    assert evidence.usable
    assert evidence.coverage_complete
    assert evidence.covered_edge_count == 1
    lower = evidence.edge_lower_map[((0, 0), (0, 1))]
    expected = 10.0 / _model().effective_speed(0.5).speed_km_per_hour
    assert lower <= expected
    assert lower > 0.0
    assert evidence.proof_digest and evidence.digest


def test_scope_or_evaluator_failure_never_authorizes_bounds() -> None:
    sampler = _sampler()
    mismatch = qualify_environmental_speed_envelope(
        risk_sampler=sampler,
        vessel_model=_model(),
        scope=TemporalScope.from_mapping({"edge_evaluator_digest": "drift"}),
        expected_scope=_scope(),
        departure_lower=T0,
        horizon_hours=1.0,
        edges=_edges(),
        evaluator_certified=True,
    )
    unknown = qualify_environmental_speed_envelope(
        risk_sampler=sampler,
        vessel_model=_model(),
        scope=TemporalScope.from_mapping({"edge_evaluator_digest": "unknown:mutable"}),
        departure_lower=T0,
        horizon_hours=1.0,
        edges=_edges(),
        evaluator_certified=True,
    )

    assert mismatch.status is EnvironmentalSpeedEnvelopeStatus.REJECTED
    assert not mismatch.usable and mismatch.edge_lower_map == {}
    assert unknown.reason == "unknown_evaluator"
    assert not unknown.usable


def test_missing_speed_or_hard_mask_is_retained_as_rejected_edge() -> None:
    missing = qualify_environmental_speed_envelope(
        risk_sampler=_sampler(with_speed=False),
        vessel_model=_model(),
        scope=_scope(),
        departure_lower=T0,
        horizon_hours=1.0,
        edges=_edges(),
        evaluator_certified=True,
    )
    blocked = qualify_environmental_speed_envelope(
        risk_sampler=_sampler(hard_mask=np.ones((2, 2), dtype=np.bool_)),
        vessel_model=_model(),
        scope=_scope(),
        departure_lower=T0,
        horizon_hours=1.0,
        edges=_edges(),
        evaluator_certified=True,
    )

    assert missing.status is EnvironmentalSpeedEnvelopeStatus.REJECTED
    assert blocked.status is EnvironmentalSpeedEnvelopeStatus.REJECTED
    assert missing.edge_lower_map == blocked.edge_lower_map == {}
    assert blocked.edge_evidence[0].hard_mask_possible
