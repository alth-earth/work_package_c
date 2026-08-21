"""Experimental observability and bounded caching for exact risk samples.

This module is deliberately outside the production ingress path.  It wraps the
canonical :class:`RiskSampler` without changing interpolation, hard-mask, or
risk-level semantics and is enabled only by explicit benchmark configuration.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections import OrderedDict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .sampler import RiskIdentity, RiskSampler, SampledRisk

if TYPE_CHECKING:
    from arctic_route_planning.contracts.models import RiskFrame


class SampleCacheMode(StrEnum):
    """Explicit experiment modes; production callers use ``OFF``."""

    OFF = "off"
    SHADOW = "shadow"
    BOUNDED_LRU = "bounded_lru"


SampleKey = tuple[str, str, datetime, bytes, bytes]


class ExperimentalRiskSampler(RiskSampler):
    """Observe or cache exact successful ``RiskSampler.sample`` requests.

    ``SHADOW`` records exact reuse but always delegates to the canonical
    sampler. ``BOUNDED_LRU`` returns only previously successful, byte-for-byte
    equivalent :class:`SampledRisk` values and has a fixed entry bound.
    """

    def __init__(
        self,
        frames: Sequence[RiskFrame],
        *,
        mode: SampleCacheMode | str = SampleCacheMode.OFF,
        capacity: int = 50_000,
        risk_layer: str = "total_risk",
        expected_identity: RiskIdentity | None = None,
        max_frame_gap: timedelta | None = None,
    ) -> None:
        super().__init__(
            frames,
            expected_identity=expected_identity,
            max_frame_gap=max_frame_gap,
        )
        self._mode = SampleCacheMode(mode)
        if capacity <= 0:
            raise ValueError("sample cache capacity must be positive")
        if not risk_layer:
            raise ValueError("risk_layer cannot be empty")
        self._capacity = capacity
        self._risk_layer = risk_layer
        self._window_fingerprint = hashlib.sha256(
            json.dumps(
                [
                    {
                        "risk_id": frame.risk_id,
                        "valid_time": frame.valid_time.isoformat(),
                    }
                    for frame in self.frames
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self._cache: OrderedDict[SampleKey, SampledRisk] = OrderedDict()
        self._shadow_seen: set[SampleKey] = set()
        self._total_requests = 0
        self._underlying_samples = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_evictions = 0
        self._shadow_reuses = 0

    @property
    def experiment_stats(self) -> dict[str, Any]:
        """Return JSON-ready counters without exposing mutable cache state."""

        unique_samples = (
            len(self._shadow_seen) if self._mode is SampleCacheMode.SHADOW else None
        )
        reuse_ratio = (
            self._shadow_reuses / self._total_requests
            if self._mode is SampleCacheMode.SHADOW and self._total_requests
            else None
        )
        return {
            "status": "EXPERIMENTAL",
            "mode": self._mode.value,
            "key_semantics": [
                "risk_window_fingerprint",
                "risk_layer",
                "requested_valid_time",
                "longitude_ieee754_bits",
                "latitude_ieee754_bits",
            ],
            "window_scope": "one_immutable_risk_sampler",
            "window_fingerprint": self._window_fingerprint,
            "total_requests": self._total_requests,
            "underlying_samples": self._underlying_samples,
            "unique_samples": unique_samples,
            "shadow_reuses": self._shadow_reuses,
            "reuse_ratio": reuse_ratio,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_evictions": self._cache_evictions,
            "cache_entries": len(self._cache),
            "cache_capacity": self._capacity,
            "production_default_changed": False,
        }

    def sample(
        self,
        sampled_at: datetime,
        longitude: float,
        latitude: float,
    ) -> SampledRisk:
        self._total_requests += 1
        if self._mode is SampleCacheMode.OFF:
            self._underlying_samples += 1
            return super().sample(sampled_at, longitude, latitude)
        if sampled_at.tzinfo is None or sampled_at.utcoffset() is None:
            self._underlying_samples += 1
            return super().sample(sampled_at, longitude, latitude)
        if sampled_at.utcoffset() != timedelta(0):
            self._underlying_samples += 1
            return super().sample(sampled_at, longitude, latitude)
        key = (
            self._window_fingerprint,
            self._risk_layer,
            sampled_at.astimezone(UTC),
            struct.pack("!d", float(longitude)),
            struct.pack("!d", float(latitude)),
        )

        if self._mode is SampleCacheMode.BOUNDED_LRU:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache_hits += 1
                self._cache.move_to_end(key)
                return cached
            self._cache_misses += 1

        self._underlying_samples += 1
        sampled = super().sample(sampled_at, longitude, latitude)

        if self._mode is SampleCacheMode.SHADOW:
            if key in self._shadow_seen:
                self._shadow_reuses += 1
            else:
                self._shadow_seen.add(key)
        elif self._mode is SampleCacheMode.BOUNDED_LRU:
            self._cache[key] = sampled
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)
                self._cache_evictions += 1
        return sampled
