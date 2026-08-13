from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.contracts import ProvenanceKind
from arctic_route_planning.risk import (
    IncompatibleRiskFramesError,
    RiskCoverageError,
    RiskOutOfBoundsError,
    RiskSampler,
)

from .factories import CONFIG_DIGEST, T0, make_frame


def test_exact_and_bilinear_spatial_sampling() -> None:
    frame = make_frame(
        T0,
        np.array([[0.0, 0.2], [0.4, 0.6]], dtype=np.float32),
        risk_id="risk-0",
        confidence=np.array([[0.9, 0.8], [0.7, 0.6]], dtype=np.float32),
    )
    sampler = RiskSampler((frame,))

    exact = sampler.sample(T0, 0.0, 0.0)
    middle = sampler.sample(T0, 0.5, 0.5)

    assert exact.risk_score == pytest.approx(0.0)
    assert exact.confidence == pytest.approx(0.9)
    assert middle.risk_score == pytest.approx(0.3)
    assert middle.confidence == pytest.approx(0.6)
    assert middle.risk_level == 2


def test_temporal_sampling_is_linear_but_confidence_and_mask_are_conservative() -> None:
    lower = make_frame(
        T0,
        np.full((2, 2), 0.2, dtype=np.float32),
        risk_id="risk-lower",
        confidence=np.full((2, 2), 0.9, dtype=np.float32),
        environment_speed_factor=np.full((2, 2), 0.8, dtype=np.float32),
    )
    upper_hard = np.zeros((2, 2), dtype=np.bool_)
    upper_hard[0, 0] = True
    upper = make_frame(
        T0 + timedelta(hours=1),
        np.full((2, 2), 0.8, dtype=np.float32),
        risk_id="risk-upper",
        confidence=np.full((2, 2), 0.6, dtype=np.float32),
        hard_mask=upper_hard,
        environment_speed_factor=np.full((2, 2), 0.5, dtype=np.float32),
    )
    sample = RiskSampler((upper, lower)).sample(
        T0 + timedelta(minutes=30),
        0.0,
        0.0,
    )

    assert sample.risk_score == pytest.approx(0.5)
    assert sample.confidence == pytest.approx(0.6)
    assert sample.hard_mask is True
    assert sample.environment_speed_factor == pytest.approx(0.5)
    assert sample.source_risk_ids == ("risk-lower", "risk-upper")


def test_sampling_never_extrapolates_or_leaves_the_grid() -> None:
    frame = make_frame(T0, np.zeros((2, 2)), risk_id="risk-0")
    sampler = RiskSampler((frame,))

    with pytest.raises(RiskCoverageError):
        sampler.sample(T0 + timedelta(seconds=1), 0.0, 0.0)
    with pytest.raises(RiskOutOfBoundsError):
        sampler.sample(T0, 2.0, 0.0)


def test_frame_gap_limit_is_enforced_at_the_requested_eta() -> None:
    lower = make_frame(T0, np.zeros((2, 2)), risk_id="risk-lower")
    upper = make_frame(
        T0 + timedelta(hours=4),
        np.zeros((2, 2)),
        risk_id="risk-upper",
    )
    sampler = RiskSampler((lower, upper), max_frame_gap=timedelta(hours=2))

    with pytest.raises(RiskCoverageError, match="exceeding"):
        sampler.sample(T0 + timedelta(hours=1), 0.0, 0.0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scenario_id", "another-scenario"),
        ("corridor_id", "another-corridor"),
        ("vessel_profile_id", "another-vessel"),
        ("config_digest", "1" * 64),
        ("generation_id", 4),
        ("model_version", "risk-model-v2"),
    ],
)
def test_identity_mismatch_is_rejected(field: str, value: object) -> None:
    lower = make_frame(T0, np.zeros((2, 2)), risk_id="risk-lower")
    upper = make_frame(
        T0 + timedelta(hours=1),
        np.zeros((2, 2)),
        risk_id="risk-upper",
    )
    incompatible = replace(upper, **{field: value})

    with pytest.raises(IncompatibleRiskFramesError):
        RiskSampler((lower, incompatible))


def test_coordinate_grid_mismatch_is_rejected() -> None:
    lower = make_frame(T0, np.zeros((2, 2)), risk_id="risk-lower")
    upper = make_frame(
        T0 + timedelta(hours=1),
        np.zeros((2, 2)),
        risk_id="risk-upper",
        longitudes=(0.0, 2.0),
    )

    with pytest.raises(IncompatibleRiskFramesError):
        RiskSampler((lower, upper))


def test_mixed_provenance_window_is_rejected() -> None:
    lower = make_frame(T0, np.zeros((2, 2)), risk_id="risk-lower")
    upper = replace(
        make_frame(
            T0 + timedelta(hours=1),
            np.zeros((2, 2)),
            risk_id="risk-upper",
        ),
        provenance=ProvenanceKind.LEGACY_UNVERIFIED,
    )

    with pytest.raises(IncompatibleRiskFramesError, match="identity"):
        RiskSampler((lower, upper))


def test_expected_identity_can_fence_a_planning_request() -> None:
    frame = make_frame(T0, np.zeros((2, 2)), risk_id="risk-0")
    identity = RiskSampler((frame,)).identity
    wrong = replace(identity, config_digest="f" * 64)

    with pytest.raises(IncompatibleRiskFramesError):
        RiskSampler((frame,), expected_identity=wrong)

    assert identity.config_digest == CONFIG_DIGEST
