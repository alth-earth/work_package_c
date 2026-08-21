from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from arctic_route_planning.risk import ExperimentalRiskSampler, SampleCacheMode

from .factories import T0, make_frame


def _frames():
    return tuple(
        make_frame(
            T0 + timedelta(hours=index),
            np.full((2, 2), 0.1 + index * 0.1, dtype=np.float32),
            risk_id=f"risk-{index}",
        )
        for index in range(2)
    )


def test_shadow_mode_measures_exact_reuse_without_returning_cached_values() -> None:
    sampler = ExperimentalRiskSampler(_frames(), mode=SampleCacheMode.SHADOW)

    first = sampler.sample(T0, 0.0, 0.0)
    second = sampler.sample(T0, 0.0, 0.0)

    assert first == second
    assert first is not second
    assert sampler.experiment_stats == {
        "status": "EXPERIMENTAL",
        "mode": "shadow",
        "key_semantics": [
            "risk_window_fingerprint",
            "risk_layer",
            "requested_valid_time",
            "longitude_ieee754_bits",
            "latitude_ieee754_bits",
        ],
        "window_scope": "one_immutable_risk_sampler",
        "window_fingerprint": sampler.experiment_stats["window_fingerprint"],
        "total_requests": 2,
        "underlying_samples": 2,
        "unique_samples": 1,
        "shadow_reuses": 1,
        "reuse_ratio": 0.5,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_evictions": 0,
        "cache_entries": 0,
        "cache_capacity": 50_000,
        "production_default_changed": False,
    }


def test_bounded_lru_is_exact_and_evicts_oldest_entry() -> None:
    sampler = ExperimentalRiskSampler(
        _frames(),
        mode=SampleCacheMode.BOUNDED_LRU,
        capacity=1,
    )

    first = sampler.sample(T0, 0.0, 0.0)
    cached = sampler.sample(T0, 0.0, 0.0)
    sampler.sample(T0, 1.0, 1.0)
    repeated_after_eviction = sampler.sample(T0, 0.0, 0.0)

    assert cached is first
    assert repeated_after_eviction == first
    assert repeated_after_eviction is not first
    assert sampler.experiment_stats["cache_hits"] == 1
    assert sampler.experiment_stats["cache_misses"] == 3
    assert sampler.experiment_stats["cache_evictions"] == 2
    assert sampler.experiment_stats["cache_entries"] == 1


def test_off_mode_always_delegates_to_canonical_sampler() -> None:
    sampler = ExperimentalRiskSampler(_frames())

    first = sampler.sample(T0, 0.0, 0.0)
    second = sampler.sample(T0, 0.0, 0.0)

    assert first == second
    assert first is not second
    assert sampler.experiment_stats["mode"] == "off"
    assert sampler.experiment_stats["underlying_samples"] == 2
    assert sampler.experiment_stats["unique_samples"] is None


def test_bounded_lru_does_not_round_time_or_coordinates() -> None:
    sampler = ExperimentalRiskSampler(
        _frames(),
        mode=SampleCacheMode.BOUNDED_LRU,
        capacity=8,
    )

    sampler.sample(T0, 0.0, 0.0)
    sampler.sample(T0 + timedelta(microseconds=1), 0.0, 0.0)
    sampler.sample(T0, np.nextafter(0.0, 1.0), 0.0)

    assert sampler.experiment_stats["cache_hits"] == 0
    assert sampler.experiment_stats["cache_misses"] == 3


def test_failed_sample_is_not_cached() -> None:
    sampler = ExperimentalRiskSampler(
        _frames(),
        mode=SampleCacheMode.BOUNDED_LRU,
        capacity=8,
    )

    with pytest.raises(ValueError, match="timezone-aware UTC"):
        sampler.sample(datetime(2026, 8, 15), 0.0, 0.0)
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        sampler.sample(datetime(2026, 8, 15), 0.0, 0.0)

    assert sampler.experiment_stats["cache_entries"] == 0
    assert sampler.experiment_stats["underlying_samples"] == 2
