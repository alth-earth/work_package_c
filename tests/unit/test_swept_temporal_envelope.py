from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from arctic_route_planning.risk import RiskSampler

from .factories import make_frame


def _sampler(*, hard_mask: np.ndarray | None = None) -> RiskSampler:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return RiskSampler(tuple(
        make_frame(
            start + timedelta(hours=index),
            np.full((3, 3), 0.2, dtype=np.float32),
            risk_id=f"risk-{index}",
            hard_mask=hard_mask,
            environment_speed_factor=np.full((3, 3), 0.8, dtype=np.float32),
            latitudes=(0.0, 1.0, 2.0),
            longitudes=(0.0, 1.0, 2.0),
        )
        for index in range(3)
    ))


def test_public_interval_api_and_swept_envelope_cover_every_frame_boundary() -> None:
    sampler = _sampler()
    start = sampler.start_time
    end = sampler.end_time
    interval = sampler.sample_interval(start, end, 0.5, 0.5)
    assert interval.coverage_complete
    assert interval.covered_frame_boundaries == tuple(frame.valid_time for frame in sampler.frames)

    envelope = sampler.sample_swept_temporal_envelope(
        (
            {"lon": 0.1, "lat": 0.5, "eta": start},
            {"lon": 1.9, "lat": 1.5, "eta": end},
        ),
        sample_spacing_m=100_000.0,
    )
    assert envelope.usable
    assert not envelope.hard_mask_possible
    assert envelope.covered_frame_boundaries == tuple(
        frame.valid_time for frame in sampler.frames
    )
    assert set(envelope.source_risk_ids) == {f"risk-{index}" for index in range(3)}


def test_public_interval_api_fails_closed_on_malformed_coordinates() -> None:
    sampler = _sampler()
    interval = sampler.sample_interval(
        sampler.start_time,
        sampler.end_time,
        "not-a-longitude",
        0.5,
    )

    assert not interval.usable
    assert not interval.coverage_complete
    assert interval.hard_mask_possible
    assert interval.failure_reason == "invalid_interval_input"


def test_swept_envelope_fails_closed_on_hard_cells_and_fast_screening() -> None:
    hard = np.zeros((3, 3), dtype=np.bool_)
    hard[1, 1] = True
    sampler = _sampler(hard_mask=hard)
    start = sampler.start_time
    end = sampler.end_time
    envelope = sampler.sample_swept_temporal_envelope(
        (
            {"lon": 0.0, "lat": 1.0, "eta": start},
            {"lon": 2.0, "lat": 1.0, "eta": end},
        ),
        sample_spacing_m=200_000.0,
    )
    assert envelope.coverage_complete
    assert envelope.hard_mask_possible

    fast = sampler.sample_swept_temporal_envelope(
        (
            {"lon": 0.0, "lat": 1.0, "eta": start},
            {"lon": 2.0, "lat": 1.0, "eta": end},
        ),
        sample_spacing_m=200_000.0,
        fail_fast=True,
    )
    assert not fast.usable
    assert fast.hard_mask_possible


def test_swept_envelope_fails_closed_on_out_of_window_eta() -> None:
    sampler = _sampler()
    assert not sampler.sample_swept_temporal_envelope(
        (
            {"lon": 0.1, "lat": 0.5, "eta": sampler.start_time},
            {
                "lon": 1.9,
                "lat": 1.5,
                "eta": sampler.end_time + timedelta(minutes=1),
            },
        ),
        sample_spacing_m=100_000.0,
    ).usable


def test_swept_envelope_fails_closed_when_a_frame_gap_exceeds_policy() -> None:
    sampler = RiskSampler(
        tuple(
            make_frame(
                datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=index),
                np.full((3, 3), 0.2, dtype=np.float32),
                risk_id=f"gap-risk-{index}",
                environment_speed_factor=np.full((3, 3), 0.8, dtype=np.float32),
                latitudes=(0.0, 1.0, 2.0),
                longitudes=(0.0, 1.0, 2.0),
            )
            for index in (0, 2)
        ),
        max_frame_gap=timedelta(hours=1),
    )
    assert not sampler.sample_swept_temporal_envelope(
        (
            {"lon": 0.1, "lat": 0.5, "eta": sampler.start_time},
            {"lon": 1.9, "lat": 1.5, "eta": sampler.end_time},
        ),
        sample_spacing_m=100_000.0,
    ).usable
