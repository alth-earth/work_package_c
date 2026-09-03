"""Strict ETA-aware sampling of immutable :class:`RiskFrame` objects.

The sampler performs *C-side sampling*, not B-side prediction.  It only
interpolates between two already published, compatible risk frames and never
extrapolates beyond the supplied window.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from math import nextafter
from typing import TYPE_CHECKING, Any

import numpy as np

from arctic_route_planning.contracts.models import ProvenanceKind

from .errors import (
    IncompatibleRiskFramesError,
    RiskCoverageError,
    RiskOutOfBoundsError,
    RiskSamplingError,
)

if TYPE_CHECKING:
    from arctic_route_planning.contracts.models import RiskFrame


@dataclass(frozen=True, slots=True)
class RiskIdentity:
    """Identity fields that must remain constant throughout one planning run."""

    run_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    provenance: ProvenanceKind
    generation_id: int
    model_version: str
    grid_id: str
    coordinate_digest: str


@dataclass(frozen=True, slots=True)
class SampledRisk:
    """Risk values at one exact point and ETA."""

    sampled_at: datetime
    longitude: float
    latitude: float
    risk_score: float
    risk_level: int
    hard_mask: bool
    confidence: float
    environment_speed_factor: float
    source_risk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RiskIntervalSample:
    """Conservative C-internal risk envelope over one time interval.

    This is deliberately not a BC/CD contract model.  It is an evidence
    carrier for the ETA qualification sidecar.  A failed or incomplete
    envelope remains an object with ``coverage_complete=False`` and an
    explicit ``failure_reason`` so callers cannot accidentally substitute a
    safe value or turn an exception into a navigable interval.
    """

    start: datetime
    end: datetime
    longitude: float
    latitude: float
    risk_lower: float | None
    risk_upper: float | None
    confidence_lower: float | None
    environment_speed_factor_lower: float | None
    environment_speed_factor_upper: float | None
    hard_mask_possible: bool
    source_risk_ids: tuple[str, ...]
    covered_frame_times: tuple[datetime, ...]
    coverage_complete: bool
    evaluator_digest: str
    failure_reason: str | None = None
    schema_version: str = "c.risk-interval-sample.v1"
    confidence_upper: float | None = None
    risk_slope_lower: float | None = None
    risk_slope_upper: float | None = None
    environment_speed_factor_slope_lower: float | None = None
    environment_speed_factor_slope_upper: float | None = None
    effective_confidence_lower: float | None = None
    effective_confidence_upper: float | None = None
    effective_environment_speed_factor_lower: float | None = None
    effective_environment_speed_factor_upper: float | None = None
    navigability_status: str = "TRANSITION_OR_UNKNOWN"

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, field="interval.start"))
        object.__setattr__(self, "end", _utc(self.end, field="interval.end"))
        if self.start > self.end:
            raise ValueError("interval.start must not be after interval.end")
        if not np.isfinite(self.longitude) or not np.isfinite(self.latitude):
            raise ValueError("interval coordinates must be finite")
        if self.schema_version != "c.risk-interval-sample.v1":
            raise ValueError("unsupported risk interval sample schema")
        if self.navigability_status not in {
            "ALWAYS_NAVIGABLE",
            "ALWAYS_BLOCKED",
            "TRANSITION_OR_UNKNOWN",
        }:
            raise ValueError("unsupported interval navigability status")
        if not isinstance(self.hard_mask_possible, bool):
            raise ValueError("hard_mask_possible must be boolean")
        if not isinstance(self.coverage_complete, bool):
            raise ValueError("coverage_complete must be boolean")
        if not self.evaluator_digest or not isinstance(self.evaluator_digest, str):
            raise ValueError("evaluator_digest must be non-empty")
        object.__setattr__(
            self,
            "covered_frame_times",
            tuple(_utc(value, field="covered_frame_time") for value in self.covered_frame_times),
        )
        object.__setattr__(self, "source_risk_ids", tuple(self.source_risk_ids))
        if self.coverage_complete:
            numeric = (
                self.risk_lower,
                self.risk_upper,
                self.confidence_lower,
                self.confidence_upper,
                self.environment_speed_factor_lower,
                self.environment_speed_factor_upper,
            )
            if any(value is None or not np.isfinite(value) for value in numeric):
                raise ValueError("complete risk interval samples require finite bounds")
            assert self.risk_lower is not None
            assert self.risk_upper is not None
            assert self.environment_speed_factor_lower is not None
            assert self.environment_speed_factor_upper is not None
            if self.risk_lower > self.risk_upper:
                raise ValueError("risk interval lower bound must not exceed upper bound")
            if self.environment_speed_factor_lower > self.environment_speed_factor_upper:
                raise ValueError("speed-factor interval lower bound must not exceed upper bound")
            slopes = (
                self.risk_slope_lower,
                self.risk_slope_upper,
                self.environment_speed_factor_slope_lower,
                self.environment_speed_factor_slope_upper,
            )
            if any(value is not None and not np.isfinite(value) for value in slopes):
                raise ValueError("complete interval slope evidence must be finite")
            if (
                self.risk_slope_lower is not None
                and self.risk_slope_upper is not None
                and self.risk_slope_lower > self.risk_slope_upper
            ):
                raise ValueError("risk slope lower bound must not exceed upper bound")
            if (
                self.environment_speed_factor_slope_lower is not None
                and self.environment_speed_factor_slope_upper is not None
                and self.environment_speed_factor_slope_lower
                > self.environment_speed_factor_slope_upper
            ):
                raise ValueError("speed-factor slope lower bound must not exceed upper bound")
            effective = (
                self.effective_confidence_lower,
                self.effective_confidence_upper,
                self.effective_environment_speed_factor_lower,
                self.effective_environment_speed_factor_upper,
            )
            if any(value is None or not np.isfinite(value) for value in effective):
                raise ValueError("complete interval samples require effective bounds")
            if (
                self.effective_confidence_lower > self.effective_confidence_upper
                or self.effective_environment_speed_factor_lower
                > self.effective_environment_speed_factor_upper
            ):
                raise ValueError("effective interval lower bound must not exceed upper bound")

    @property
    def reason(self) -> str | None:
        """Compatibility alias used by qualification evidence serializers."""

        return self.failure_reason

    @property
    def usable(self) -> bool:
        return self.coverage_complete and self.failure_reason is None

    @property
    def covered_frame_boundaries(self) -> tuple[datetime, ...]:
        """Name the same evidence in the terminology used by ETA proofs."""

        return self.covered_frame_times


@dataclass(frozen=True, slots=True)
class SweptTemporalEnvelope:
    """Fail-closed evidence for a moving geographic polyline.

    ``RiskIntervalSample`` is a fixed-coordinate temporal envelope.  This
    carrier combines those envelopes with exact samples on a spatially
    densified path.  It is intentionally C-internal: it proves only the
    published RiskWindow/raster model and does not claim chart or UKC safety.
    """

    sampled_risks: tuple[SampledRisk, ...]
    interval_samples: tuple[RiskIntervalSample, ...]
    sample_spacing_m: float
    coverage_complete: bool
    hard_mask_possible: bool
    max_risk_upper: float | None
    integrated_risk_hours: float | None
    minimum_environment_speed_factor: float | None
    source_risk_ids: tuple[str, ...]
    covered_frame_boundaries: tuple[datetime, ...]
    failure_reason: str | None = None
    schema_version: str = "c.route-motion-swept-temporal-envelope.v1"
    swept_cell_keys: tuple[tuple[int, int], ...] = ()

    @property
    def usable(self) -> bool:
        return self.coverage_complete and self.failure_reason is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sample_count": len(self.sampled_risks),
            "interval_count": len(self.interval_samples),
            "sample_spacing_m": self.sample_spacing_m,
            "coverage_complete": self.coverage_complete,
            "hard_mask_possible": self.hard_mask_possible,
            "max_risk_upper": self.max_risk_upper,
            "integrated_risk_hours": self.integrated_risk_hours,
            "minimum_environment_speed_factor": self.minimum_environment_speed_factor,
            "source_risk_ids": list(self.source_risk_ids),
            "covered_frame_boundaries": [
                value.astimezone(UTC).isoformat().replace("+00:00", "Z")
                for value in self.covered_frame_boundaries
            ],
            "swept_cell_count": len(self.swept_cell_keys),
            "swept_cell_keys": [list(value) for value in self.swept_cell_keys],
            "failure_reason": self.failure_reason,
        }


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RiskSamplingError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise RiskSamplingError(f"{field} must use UTC")
    return value.astimezone(UTC)


def _identity(frame: Any) -> RiskIdentity:
    grid = frame.grid
    return RiskIdentity(
        run_id=frame.run_id,
        scenario_id=frame.scenario_id,
        corridor_id=frame.corridor_id,
        vessel_profile_id=frame.vessel_profile_id,
        config_digest=frame.config_digest,
        model_config_digest=frame.model_config_digest,
        provenance=frame.provenance,
        generation_id=frame.generation_id,
        model_version=frame.model_version,
        grid_id=grid.grid_id,
        coordinate_digest=grid.coordinate_digest,
    )


class RiskSampler:
    """Sample a compatible sequence of risk frames in space and time.

    ``risk_score`` is bilinearly sampled in space and linearly interpolated in
    time.  ``confidence`` and optional speed factors use the most conservative
    contributing value.  ``hard_mask`` is a logical OR across every
    contributing spatial cell and, between frames, across both bracketing
    frames.
    """

    _CACHE_ENTRY_LIMIT = 65_536

    def __init__(
        self,
        frames: Sequence[RiskFrame],
        *,
        expected_identity: RiskIdentity | None = None,
        max_frame_gap: timedelta | None = None,
    ) -> None:
        if not frames:
            raise RiskCoverageError("at least one RiskFrame is required")
        if max_frame_gap is not None and max_frame_gap <= timedelta(0):
            raise ValueError("max_frame_gap must be positive")

        self._frames = tuple(sorted(frames, key=lambda frame: frame.valid_time))
        self._max_frame_gap = max_frame_gap
        self._identity = _identity(self._frames[0])
        if expected_identity is not None and self._identity != expected_identity:
            raise IncompatibleRiskFramesError(
                "risk window identity does not match the requested planning context"
            )
        self._validate_window()
        self._valid_times = [frame.valid_time for frame in self._frames]
        self._latitudes = np.asarray(self._frames[0].payload.coords["latitude"].values, dtype=float)
        self._longitudes = np.asarray(
            self._frames[0].payload.coords["longitude"].values, dtype=float
        )
        self._arrays: tuple[dict[str, np.ndarray | None], ...] = tuple(
            self._frame_arrays(frame) for frame in self._frames
        )
        # Frame values depend only on the spatial coordinate and immutable
        # frame.  Swept-edge qualification revisits the same raw segments
        # across layers/objectives, so memoization avoids repeating bilinear
        # contributor lookup without changing any sampling or fail-closed
        # rule.  The interval and point caches stay separate because interval
        # sampling is intentionally stricter about missing risk.
        self._sample_frame_cache: dict[tuple[int, float, float], _FrameSample] = {}
        self._sample_frame_interval_cache: dict[tuple[int, float, float], _FrameSample] = {}
        self._contributors_cache: dict[
            tuple[float, float], tuple[tuple[int, int, float], ...]
        ] = {}
        self._interval_cache: dict[
            tuple[datetime, datetime, float, float], RiskIntervalSample
        ] = {}

    @property
    def frames(self) -> tuple[RiskFrame, ...]:
        return self._frames

    @property
    def identity(self) -> RiskIdentity:
        return self._identity

    @property
    def start_time(self) -> datetime:
        return self._frames[0].valid_time

    @property
    def end_time(self) -> datetime:
        return self._frames[-1].valid_time

    @property
    def as_of_times(self) -> tuple[datetime, ...]:
        """Return the knowledge cutoffs represented in this immutable window."""

        return tuple(frame.as_of_time for frame in self._frames)

    def assert_identity(self, expected: RiskIdentity) -> None:
        """Fail fast if this window belongs to another planning context."""

        if self._identity != expected:
            raise IncompatibleRiskFramesError(
                "risk window identity does not match the requested planning context"
            )

    def sample(
        self,
        sampled_at: datetime,
        longitude: float,
        latitude: float,
    ) -> SampledRisk:
        """Return a strict spatial-temporal sample at ``sampled_at``.

        The requested time must be exactly on a frame or bracketed by two
        compatible frames.  Extrapolation and silent holding are intentionally
        unsupported.
        """

        sampled_at = _utc(sampled_at, field="sampled_at")
        lower, upper = self._bracket(sampled_at)
        lower_values = self._sample_frame(lower, longitude, latitude)

        if lower == upper:
            return SampledRisk(
                sampled_at=sampled_at,
                longitude=float(longitude),
                latitude=float(latitude),
                risk_score=lower_values.risk_score,
                risk_level=_risk_level(lower_values.risk_score),
                hard_mask=lower_values.hard_mask,
                confidence=lower_values.confidence,
                environment_speed_factor=lower_values.environment_speed_factor,
                source_risk_ids=(self._frames[lower].risk_id,),
            )

        gap = self._frames[upper].valid_time - self._frames[lower].valid_time
        if self._max_frame_gap is not None and gap > self._max_frame_gap:
            raise RiskCoverageError(
                f"bracketing RiskFrames are {gap} apart, exceeding {self._max_frame_gap}"
            )
        upper_values = self._sample_frame(upper, longitude, latitude)
        fraction = (
            sampled_at - self._frames[lower].valid_time
        ).total_seconds() / gap.total_seconds()
        risk_score = _lerp(lower_values.risk_score, upper_values.risk_score, fraction)
        return SampledRisk(
            sampled_at=sampled_at,
            longitude=float(longitude),
            latitude=float(latitude),
            risk_score=risk_score,
            risk_level=_risk_level(risk_score),
            hard_mask=lower_values.hard_mask or upper_values.hard_mask,
            confidence=min(lower_values.confidence, upper_values.confidence),
            environment_speed_factor=min(
                lower_values.environment_speed_factor,
                upper_values.environment_speed_factor,
            ),
            source_risk_ids=(self._frames[lower].risk_id, self._frames[upper].risk_id),
        )

    def sample_interval(
        self,
        start: datetime,
        end: datetime,
        longitude: float,
        latitude: float,
    ) -> RiskIntervalSample:
        """Return the named, read-only temporal envelope API.

        The implementation remains the same conservative primitive used by
        the historical ETA sidecar.  Exposing it under a public name makes it
        possible for formal motion qualification to state exactly which
        evaluator produced its evidence without granting callers a guessed or
        extrapolated value.
        """

        try:
            interval_start = _utc(start, field="interval.start")
            interval_end = _utc(end, field="interval.end")
            longitude_value = float(longitude)
            latitude_value = float(latitude)
        except (RiskSamplingError, TypeError, ValueError):
            # The public API is a read-only evidence primitive.  Invalid
            # input must become an explicitly unusable interval, including
            # malformed coordinates; it must never escape as a partially
            # handled TypeError/ValueError that a caller could mistake for a
            # missing optional check.
            return self._failed_interval(
                start=start,
                end=end,
                longitude=_safe_float(longitude),
                latitude=_safe_float(latitude),
                reason="invalid_interval_input",
            )
        cache_key = (interval_start, interval_end, longitude_value, latitude_value)
        cached = self._interval_cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._sample_interval(*cache_key)
        self._bounded_cache_put(self._interval_cache, cache_key, result)
        return result

    def sample_swept_temporal_envelope(
        self,
        samples: Sequence[Any],
        *,
        sample_spacing_m: float = 1_000.0,
        fail_fast: bool = False,
    ) -> SweptTemporalEnvelope:
        """Envelope a moving path using spatial cells and every ETA interval.

        The input accepts objects exposing ``longitude``, ``latitude`` and
        ``eta`` (including formal ``MotionSample``) or mappings with ``lon``/
        ``lat``/``eta`` keys.  Each segment is densified at the requested
        maximum spacing on the great circle.  Exact samples catch spatial
        hard cells; one interval envelope is then evaluated for every swept
        spatial contributor over the ETA range in which that contributor is
        touched, including every RiskFrame boundary.  Any malformed point,
        missing frame, out-of-bounds
        coordinate, or failed interval causes a fail-closed result.
        ``fail_fast`` is an evaluation optimization for screening candidate
        edges.  It may return as soon as a sampled point or interval is hard,
        unknown, or incomplete; it never converts that state into a usable
        envelope.  The default remains a complete envelope for qualification
        evidence and risk comparison.
        """

        if (
            isinstance(sample_spacing_m, bool)
            or not isinstance(sample_spacing_m, (int, float))
            or not np.isfinite(sample_spacing_m)
            or sample_spacing_m <= 0.0
        ):
            return self._failed_swept_envelope(
                float(sample_spacing_m) if isinstance(sample_spacing_m, (int, float)) else 0.0,
                "invalid_sample_spacing",
            )
        if not isinstance(fail_fast, bool):
            return self._failed_swept_envelope(
                float(sample_spacing_m), "invalid_fail_fast_flag"
            )
        try:
            path = tuple(_moving_point(value) for value in samples)
        except (AttributeError, TypeError, ValueError, RiskSamplingError) as error:
            return self._failed_swept_envelope(float(sample_spacing_m), f"invalid_path:{error}")
        if len(path) < 2:
            return self._failed_swept_envelope(float(sample_spacing_m), "insufficient_path_points")
        if any(current[2] <= previous[2] for previous, current in pairwise(path)):
            return self._failed_swept_envelope(float(sample_spacing_m), "non_monotonic_path_eta")

        densified: list[tuple[float, float, datetime]] = []
        try:
            for index, (left, right) in enumerate(pairwise(path)):
                start_lon, start_lat, start_time = left
                end_lon, end_lat, end_time = right
                distance = _great_circle_distance_m(
                    (start_lon, start_lat), (end_lon, end_lat)
                )
                if distance <= 1.0e-9:
                    raise ValueError("moving path contains a zero-length segment")
                count = max(1, int(np.ceil(distance / float(sample_spacing_m))))
                duration = end_time - start_time
                fractions = {offset / count for offset in range(count + 1)}
                # Uniform samples are not sufficient to establish a swept
                # cell proof: a long sample interval can jump over a narrow
                # hard/unknown cell.  Add every rectilinear RiskFrame grid
                # crossing to the great-circle validation lattice.
                fractions.update(
                    self._great_circle_grid_crossings(
                        (start_lon, start_lat),
                        (end_lon, end_lat),
                    )
                )
                for fraction in sorted(fractions):
                    if index > 0 and fraction <= 1.0e-12:
                        continue
                    longitude, latitude = _great_circle_interpolate(
                        (start_lon, start_lat), (end_lon, end_lat), fraction
                    )
                    densified.append(
                        (longitude, latitude, start_time + duration * fraction)
                    )
        except Exception as error:
            return self._failed_swept_envelope(
                float(sample_spacing_m), f"swept_geometry_failure:{type(error).__name__}"
            )

        sampled: list[SampledRisk] = []
        swept_cells: set[tuple[int, int]] = set()
        cell_time_ranges: dict[tuple[int, int], list[datetime]] = {}
        intervals: list[RiskIntervalSample] = []
        try:
            for longitude, latitude, sampled_at in densified:
                cell_keys = tuple(
                    (row, column)
                    for row, column, weight in self._contributors(longitude, latitude)
                    if weight > 0.0
                )
                if not cell_keys:
                    raise RiskOutOfBoundsError(
                        f"({longitude}, {latitude}) is outside the RiskFrame grid"
                    )
                for cell_key in cell_keys:
                    swept_cells.add(cell_key)
                    time_range = cell_time_ranges.setdefault(
                        cell_key, [sampled_at, sampled_at]
                    )
                    time_range[0] = min(time_range[0], sampled_at)
                    time_range[1] = max(time_range[1], sampled_at)
                value = self.sample(sampled_at, longitude, latitude)
                sampled.append(value)
                if fail_fast and value.hard_mask:
                    return self._failed_swept_envelope(
                        float(sample_spacing_m), "hard_mask_or_unknown_point"
                    )
            # A route can contain thousands of 250 m motion samples but only
            # a small number of RiskFrame spatial cells.  Prove the temporal
            # envelope once per actually swept cell, over the ETA range in
            # which the path touches that cell.  Bilinear interpolation is a
            # convex combination of the four cell contributors, so checking
            # each contributor at every covered RiskFrame boundary is a
            # conservative continuous-cell proof.  The point samples above
            # remain the source for the route's exact sampled-risk integral.
            for row, column in sorted(swept_cells):
                start_time, end_time = cell_time_ranges[(row, column)]
                interval = self.sample_interval(
                    start_time,
                    end_time,
                    float(self._longitudes[column]),
                    float(self._latitudes[row]),
                )
                intervals.append(interval)
                if fail_fast and (not interval.usable or interval.hard_mask_possible):
                    return self._failed_swept_envelope(
                        float(sample_spacing_m),
                        interval.failure_reason or "hard_mask_or_unknown_cell",
                    )
        except Exception as error:  # formal qualification must never fail open
            return self._failed_swept_envelope(
                float(sample_spacing_m), f"swept_sampling_failure:{type(error).__name__}"
            )

        complete = bool(sampled) and all(value.coverage_complete for value in intervals)
        hard_possible = any(value.hard_mask for value in sampled) or any(
            value.hard_mask_possible for value in intervals
        )
        upper_values = [
            float(value.risk_score) for value in sampled
        ] + [
            float(value.risk_upper)
            for value in intervals
            if value.risk_upper is not None
        ]
        speeds = [
            float(value.environment_speed_factor) for value in sampled
        ] + [
            float(value.environment_speed_factor_lower)
            for value in intervals
            if value.environment_speed_factor_lower is not None
        ]
        integrated = _integrated_sampled_risk(sampled) if len(sampled) >= 2 else None
        source_ids_in_order: list[str] = []
        for value in sampled:
            source_ids_in_order.extend(value.source_risk_ids)
        for value in intervals:
            source_ids_in_order.extend(value.source_risk_ids)
        source_ids = tuple(dict.fromkeys(source_ids_in_order))
        boundaries = tuple(sorted({
            boundary
            for value in intervals
            for boundary in value.covered_frame_boundaries
        }))
        failure = None
        if not complete:
            failure = next(
                (value.failure_reason for value in intervals if value.failure_reason),
                "swept_temporal_coverage_incomplete",
            )
        return SweptTemporalEnvelope(
            sampled_risks=tuple(sampled),
            interval_samples=tuple(intervals),
            sample_spacing_m=float(sample_spacing_m),
            coverage_complete=complete,
            hard_mask_possible=hard_possible,
            max_risk_upper=max(upper_values) if upper_values else None,
            integrated_risk_hours=integrated,
            minimum_environment_speed_factor=min(speeds) if speeds else None,
            source_risk_ids=source_ids,
            covered_frame_boundaries=boundaries,
            failure_reason=failure,
            swept_cell_keys=tuple(sorted(swept_cells)),
        )

    # A concise alias used by the motion producer and available to callers
    # that describe this operation as a swept-cell envelope.
    swept_temporal_envelope = sample_swept_temporal_envelope

    def _great_circle_grid_crossings(
        self,
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> tuple[float, ...]:
        """Return great-circle fractions at every crossed RiskFrame grid line."""

        fractions: set[float] = set()
        for axis_index, axis in enumerate((self._longitudes, self._latitudes)):
            start_value = first[axis_index]
            end_value = second[axis_index]
            lower = min(start_value, end_value)
            upper = max(start_value, end_value)
            if upper - lower <= 1.0e-12:
                continue
            # Only grid lines inside this segment can be crossed.  The old
            # implementation iterated over every latitude and longitude in
            # the raster for every edge, which made exhaustive any-angle
            # screening unnecessarily expensive without adding evidence.
            # ``ordered`` also keeps the lookup correct for descending input
            # axes; the bisection below still uses the geographic coordinate
            # values and therefore does not depend on axis order.
            ordered = axis if axis[0] < axis[-1] else axis[::-1]
            first_index = int(np.searchsorted(ordered, lower, side="right"))
            last_index = int(np.searchsorted(ordered, upper, side="left"))
            for target in ordered[first_index:last_index]:
                increasing = end_value > start_value
                left = 0.0
                right = 1.0
                for _ in range(48):
                    middle = (left + right) * 0.5
                    point = _great_circle_interpolate(first, second, middle)
                    value = point[axis_index]
                    if (value < target) == increasing:
                        left = middle
                    else:
                        right = middle
                fractions.add((left + right) * 0.5)
        return tuple(sorted(fractions))

    def _grid_cells_at(self, longitude: float, latitude: float) -> tuple[tuple[int, int], ...]:
        """Return all grid-neighbour cells touched by a validation point."""

        def axis_indices(axis: np.ndarray, target: float) -> tuple[int, ...]:
            ascending = bool(axis[0] < axis[-1])
            ordered = axis if ascending else axis[::-1]
            tolerance = 1.0e-10
            if target < ordered[0] - tolerance or target > ordered[-1] + tolerance:
                return ()
            upper = int(np.searchsorted(ordered, target, side="left"))
            exact_index = None
            for candidate in (upper - 1, upper):
                if 0 <= candidate < len(ordered) and abs(
                    float(ordered[candidate]) - target
                ) <= tolerance:
                    exact_index = candidate
                    break
            if exact_index is not None:
                center = exact_index
                candidates = {center - 1, center, center + 1}
            else:
                upper = int(np.searchsorted(ordered, target, side="right"))
                candidates = {upper - 1, upper}
            valid = {index for index in candidates if 0 <= index < len(axis)}
            if not ascending:
                valid = {len(axis) - 1 - index for index in valid}
            return tuple(sorted(valid))

        rows = axis_indices(self._latitudes, latitude)
        columns = axis_indices(self._longitudes, longitude)
        return tuple((row, column) for row in rows for column in columns)

    def _sample_interval(
        self,
        start: datetime,
        end: datetime,
        longitude: float,
        latitude: float,
    ) -> RiskIntervalSample:
        """Return a conservative envelope for all times in ``[start, end]``.

        The formal :meth:`sample` implementation intentionally remains the
        single-point path used by production planning.  This private method
        is only consumed by the C ETA research sidecar.  It enumerates every
        RiskFrame boundary touched by the requested interval and uses the
        existing spatial contributors at each boundary.  Because the current
        temporal interpolation is linear between compatible frames, endpoint
        extrema enclose every interior value.  Any missing/invalid evidence
        is represented as an incomplete sample rather than a guessed value.
        """

        try:
            interval_start = _utc(start, field="interval.start")
            interval_end = _utc(end, field="interval.end")
        except RiskSamplingError as error:
            return self._failed_interval(
                start=start,
                end=end,
                longitude=longitude,
                latitude=latitude,
                reason=f"invalid_time:{error}",
            )
        if interval_start > interval_end:
            return self._failed_interval(
                start=interval_start,
                end=interval_end,
                longitude=longitude,
                latitude=latitude,
                reason="invalid_interval_order",
            )
        if not np.isfinite(longitude) or not np.isfinite(latitude):
            return self._failed_interval(
                start=interval_start,
                end=interval_end,
                longitude=float(longitude) if np.isfinite(longitude) else 0.0,
                latitude=float(latitude) if np.isfinite(latitude) else 0.0,
                reason="non_finite_coordinate",
            )

        try:
            lower_index, _ = self._bracket(interval_start)
            _, upper_index = self._bracket(interval_end)
            # A bracket at a frame boundary returns the same index.  The
            # range therefore includes every frame whose value can affect the
            # linear interpolation over the requested interval.
            frame_indices = range(lower_index, upper_index + 1)
            frame_times = tuple(self._frames[index].valid_time for index in frame_indices)
            if not frame_times:
                raise RiskCoverageError("interval has no covered RiskFrame")
            if self._max_frame_gap is not None:
                for left in range(lower_index, upper_index):
                    right = left + 1
                    gap = self._frames[right].valid_time - self._frames[left].valid_time
                    if gap > self._max_frame_gap:
                        raise RiskCoverageError(
                            f"interval crosses RiskFrame gap {gap}, exceeding {self._max_frame_gap}"
                        )
            contributors = self._contributors(longitude, latitude)
            values = tuple(
                self._sample_frame_interval(
                    index, longitude, latitude, contributors=contributors
                )
                for index in frame_indices
            )
        except (RiskCoverageError, RiskOutOfBoundsError, RiskSamplingError) as error:
            return self._failed_interval(
                start=interval_start,
                end=interval_end,
                longitude=float(longitude),
                latitude=float(latitude),
                reason=f"{type(error).__name__}:{error}",
            )
        except Exception as error:  # evaluator boundary must never fail open
            return self._failed_interval(
                start=interval_start,
                end=interval_end,
                longitude=float(longitude),
                latitude=float(latitude),
                reason=f"evaluator_failure:{type(error).__name__}",
            )

        # The frame values are sufficient for the whole-window envelope, but
        # a partitioned ETA proof may ask for a strict sub-interval inside a
        # frame bracket.  Include the interpolated values at both requested
        # endpoints so a threshold crossing can be isolated without carrying
        # an unnecessarily wide frame-endpoint envelope.
        risk_values = tuple(value.risk_score for value in values)
        endpoint_risks = (
            self._interpolated_risk_from_frame_values(
                interval_start, values, frame_indices
            ),
            self._interpolated_risk_from_frame_values(
                interval_end, values, frame_indices
            ),
        )
        risk_values += endpoint_risks
        # ``sample`` takes the minimum confidence and speed factor of the two
        # frames in each bracket.  Keep the historical raw extrema above for
        # compatibility, and carry tighter effective bracket extrema for the
        # partitioned proof sidecar.
        if len(values) == 1:
            effective_confidence = (values[0].confidence,)
            effective_speed = (values[0].environment_speed_factor,)
        else:
            effective_confidence = tuple(
                min(left.confidence, right.confidence) for left, right in pairwise(values)
            )
            effective_speed = tuple(
                min(left.environment_speed_factor, right.environment_speed_factor)
                for left, right in pairwise(values)
            )
        raw_confidence_values = tuple(value.confidence for value in values)
        raw_speed_values = tuple(value.environment_speed_factor for value in values)
        risk_slope_lower, risk_slope_upper = self._interval_slope_bounds(
            values, frame_indices, variable="risk_score"
        )
        # The formal sampler takes the conservative minimum of the two frame
        # speed factors in each bracket.  That is constant inside a frame
        # segment; any change at a frame boundary is handled as a continuity
        # failure by the analytic ETA sidecar rather than interpolated away.
        speed_slope_lower, speed_slope_upper = self._interval_slope_bounds(
            values, frame_indices, variable="environment_speed_factor"
        )
        navigability_status = self._interval_navigability(values)
        source_ids = tuple(dict.fromkeys(self._frames[index].risk_id for index in frame_indices))
        return RiskIntervalSample(
            start=interval_start,
            end=interval_end,
            longitude=float(longitude),
            latitude=float(latitude),
            risk_lower=_outward_lower(min(risk_values), floor=0.0),
            risk_upper=_outward_upper(max(risk_values), ceiling=1.0),
            confidence_lower=_outward_lower(min(raw_confidence_values), floor=0.0),
            confidence_upper=_outward_upper(max(raw_confidence_values), ceiling=1.0),
            environment_speed_factor_lower=_outward_lower(min(raw_speed_values), floor=0.0),
            environment_speed_factor_upper=_outward_upper(max(raw_speed_values), ceiling=1.0),
            hard_mask_possible=any(value.hard_mask for value in values),
            source_risk_ids=source_ids,
            covered_frame_times=tuple(self._frames[index].valid_time for index in frame_indices),
            coverage_complete=True,
            evaluator_digest=self.interval_evaluator_digest,
            risk_slope_lower=risk_slope_lower,
            risk_slope_upper=risk_slope_upper,
            effective_confidence_lower=_outward_lower(min(effective_confidence), floor=0.0),
            effective_confidence_upper=_outward_upper(max(effective_confidence), ceiling=1.0),
            effective_environment_speed_factor_lower=_outward_lower(
                min(effective_speed), floor=0.0
            ),
            effective_environment_speed_factor_upper=_outward_upper(
                max(effective_speed), ceiling=1.0
            ),
            environment_speed_factor_slope_lower=speed_slope_lower,
            environment_speed_factor_slope_upper=speed_slope_upper,
            navigability_status=navigability_status,
        )

    def _interpolated_risk(
        self,
        sampled_at: datetime,
        longitude: float,
        latitude: float,
    ) -> float:
        """Return strict linear risk at an interval endpoint.

        This helper is used only by the private interval sidecar.  It mirrors
        :meth:`sample`'s temporal rule while retaining the interval sampler's
        fail-closed handling for non-finite risk values.
        """

        lower, upper = self._bracket(sampled_at)
        lower_value = self._sample_frame_interval(lower, longitude, latitude)
        if lower == upper:
            return lower_value.risk_score
        upper_value = self._sample_frame_interval(upper, longitude, latitude)
        gap = self._frames[upper].valid_time - self._frames[lower].valid_time
        fraction = (
            sampled_at - self._frames[lower].valid_time
        ).total_seconds() / gap.total_seconds()
        return _lerp(lower_value.risk_score, upper_value.risk_score, fraction)

    def _interpolated_risk_from_frame_values(
        self,
        sampled_at: datetime,
        values: Sequence[_FrameSample],
        frame_indices: Sequence[int],
    ) -> float:
        """Interpolate from already sampled frame values without resampling.

        ``_sample_interval`` has already evaluated every frame that brackets
        its requested interval.  Reusing those values preserves the same
        linear-time result while avoiding two complete spatial evaluations per
        interval endpoint.
        """

        lower, upper = self._bracket(sampled_at)
        first_index = frame_indices[0]
        lower_value = values[lower - first_index]
        if lower == upper:
            return lower_value.risk_score
        upper_value = values[upper - first_index]
        gap = self._frames[upper].valid_time - self._frames[lower].valid_time
        fraction = (
            sampled_at - self._frames[lower].valid_time
        ).total_seconds() / gap.total_seconds()
        return _lerp(lower_value.risk_score, upper_value.risk_score, fraction)

    def _interval_slope_bounds(
        self,
        values: Sequence[_FrameSample],
        frame_indices: Iterable[int],
        *,
        variable: str,
    ) -> tuple[float, float]:
        indices = tuple(frame_indices)
        slopes: list[float] = []
        if len(indices) <= 1:
            return 0.0, 0.0
        if variable == "environment_speed_factor":
            # ``sample`` takes the minimum of the bracketing frame values, so
            # its speed factor is constant within each frame segment.  A
            # change between adjacent segments is a boundary discontinuity,
            # not an interpolated slope.
            return 0.0, 0.0
        for left_position, left_index in enumerate(indices[:-1]):
            right_index = indices[left_position + 1]
            gap_hours = (
                self._frames[right_index].valid_time - self._frames[left_index].valid_time
            ).total_seconds() / 3600.0
            if gap_hours <= 0.0 or not np.isfinite(gap_hours):
                raise RiskSamplingError("RiskFrame times must increase strictly")
            left_value = float(getattr(values[left_position], variable))
            right_value = float(getattr(values[left_position + 1], variable))
            slopes.append((right_value - left_value) / gap_hours)
        return _outward_lower(min(slopes)), _outward_upper(max(slopes))

    @staticmethod
    def _interval_navigability(values: Sequence[_FrameSample]) -> str:
        if not values:
            return "TRANSITION_OR_UNKNOWN"
        masks = tuple(value.hard_mask for value in values)
        if all(masks):
            return "ALWAYS_BLOCKED"
        if not any(masks):
            return "ALWAYS_NAVIGABLE"
        return "TRANSITION_OR_UNKNOWN"

    @property
    def interval_evaluator_digest(self) -> str:
        """Stable identity for this sidecar's interpolation/evaluator rules."""

        cached = getattr(self, "_interval_evaluator_digest", None)
        if isinstance(cached, str):
            return cached
        payload = {
            "schema_version": "c.risk-interval-sample.v1",
            "sampler": "RiskSampler.linear-time-bilinear-space.v1",
            "identity": self.identity,
            "valid_times": tuple(self._valid_times),
            "max_frame_gap_seconds": (
                self._max_frame_gap.total_seconds() if self._max_frame_gap is not None else None
            ),
        }
        encoded = json.dumps(
            _jsonable_for_digest(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        # The digest is an identity of the immutable sampler, not of an
        # individual interval.  Cache it once: a swept envelope may contain
        # thousands of interval samples, and recomputing the JSON digest for
        # every one dominated exhaustive any-angle screening.
        self._interval_evaluator_digest = digest
        return digest

    def _sample_frame_interval(
        self,
        frame_index: int,
        longitude: float,
        latitude: float,
        *,
        contributors: tuple[tuple[int, int, float], ...] | None = None,
    ) -> _FrameSample:
        """Sample one frame while refusing the normal hard-mask placeholder."""

        arrays = self._arrays[frame_index]
        cache_key = (frame_index, float(longitude), float(latitude))
        cached = self._sample_frame_interval_cache.get(cache_key)
        if cached is not None:
            return cached
        if contributors is None:
            contributors = self._contributors(longitude, latitude)
        confidence_values = _array_values(
            arrays["confidence"], contributors, variable="confidence", finite=True
        )
        hard_values = _array_values(
            arrays["hard_mask"], contributors, variable="hard_mask", finite=False
        )
        risk_values = _array_values(
            arrays["risk_score"], contributors, variable="risk_score", finite=True
        )
        # The formal contract requires this variable for formal frames.  The
        # interval primitive is stricter for all provenance kinds: a missing
        # optional factor is unknown, not a declaration of calm water.
        if arrays["environment_speed_factor"] is None:
            raise RiskSamplingError("environment_speed_factor is missing")
        speed_values = _array_values(
            arrays["environment_speed_factor"],
            contributors,
            variable="environment_speed_factor",
            finite=True,
        )
        factors = tuple(value for value, _ in speed_values)
        if any(value <= 0.0 or value > 1.0 for value in factors):
            raise RiskSamplingError("environment speed factors must be finite in (0, 1]")
        hard_mask = any(bool(value) for value, _ in hard_values)
        result = _FrameSample(
            risk_score=float(sum(value * weight for value, weight in risk_values)),
            confidence=float(min(value for value, _ in confidence_values)),
            hard_mask=hard_mask,
            environment_speed_factor=float(min(factors)),
        )
        self._bounded_cache_put(self._sample_frame_interval_cache, cache_key, result)
        return result

    def _contributors(
        self,
        longitude: float,
        latitude: float,
    ) -> tuple[tuple[int, int, float], ...]:
        key = (float(longitude), float(latitude))
        cached = self._contributors_cache.get(key)
        if cached is not None:
            return cached
        lat_weights = _axis_weights(self._latitudes, key[1], axis="latitude")
        lon_weights = _axis_weights(self._longitudes, key[0], axis="longitude")
        result = tuple(
            (lat_index, lon_index, lat_weight * lon_weight)
            for lat_index, lat_weight in lat_weights
            for lon_index, lon_weight in lon_weights
            if lat_weight * lon_weight > 0.0
        )
        self._bounded_cache_put(self._contributors_cache, key, result)
        return result

    @classmethod
    def _bounded_cache_put(
        cls, cache: dict[Any, Any], key: Any, value: Any
    ) -> None:
        """Keep repeated route checks bounded without changing cache semantics."""

        if key not in cache and len(cache) >= cls._CACHE_ENTRY_LIMIT:
            cache.pop(next(iter(cache)))
        cache[key] = value

    def _failed_interval(
        self,
        *,
        start: datetime,
        end: datetime,
        longitude: float,
        latitude: float,
        reason: str,
    ) -> RiskIntervalSample:
        safe_start = _safe_utc(start)
        safe_end = _safe_utc(end)
        if safe_start > safe_end:
            safe_start, safe_end = safe_end, safe_start
        return RiskIntervalSample(
            start=safe_start,
            end=safe_end,
            longitude=float(longitude),
            latitude=float(latitude),
            risk_lower=None,
            risk_upper=None,
            confidence_lower=None,
            environment_speed_factor_lower=None,
            environment_speed_factor_upper=None,
            hard_mask_possible=True,
            source_risk_ids=(),
            covered_frame_times=(),
            coverage_complete=False,
            evaluator_digest=self.interval_evaluator_digest,
            failure_reason=reason,
        )

    @staticmethod
    def _failed_swept_envelope(sample_spacing_m: float, reason: str) -> SweptTemporalEnvelope:
        return SweptTemporalEnvelope(
            sampled_risks=(),
            interval_samples=(),
            sample_spacing_m=sample_spacing_m,
            coverage_complete=False,
            hard_mask_possible=True,
            max_risk_upper=None,
            integrated_risk_hours=None,
            minimum_environment_speed_factor=None,
            source_risk_ids=(),
            covered_frame_boundaries=(),
            failure_reason=reason,
        )

    def _validate_window(self) -> None:
        reference = self._frames[0]
        reference_identity = _identity(reference)
        reference_grid = reference.grid
        previous_time: datetime | None = None
        for frame in self._frames:
            valid_time = _utc(frame.valid_time, field="RiskFrame.valid_time")
            if previous_time is not None and valid_time == previous_time:
                raise IncompatibleRiskFramesError(
                    f"duplicate RiskFrame valid_time: {valid_time.isoformat()}"
                )
            previous_time = valid_time
            if _identity(frame) != reference_identity:
                raise IncompatibleRiskFramesError(
                    "cannot interpolate across scenario, corridor, generation, vessel, "
                    "configuration, model, provenance, or grid identity"
                )
            grid = frame.grid
            if (
                grid.crs != reference_grid.crs
                or grid.shape != reference_grid.shape
                or grid.y_dim != reference_grid.y_dim
                or grid.x_dim != reference_grid.x_dim
            ):
                raise IncompatibleRiskFramesError("RiskFrame grid definitions are incompatible")
            self._validate_payload(frame.payload)

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        required = {"risk_score", "risk_level", "hard_mask", "confidence"}
        missing = required.difference(payload.data_vars)
        if missing:
            raise IncompatibleRiskFramesError(
                f"RiskFrame payload is missing variables: {sorted(missing)}"
            )
        if "latitude" not in payload.coords or "longitude" not in payload.coords:
            raise IncompatibleRiskFramesError(
                "RiskFrame payload must expose latitude and longitude coordinates"
            )
        latitudes = np.asarray(payload.coords["latitude"].values, dtype=float)
        longitudes = np.asarray(payload.coords["longitude"].values, dtype=float)
        if latitudes.ndim != 1 or longitudes.ndim != 1:
            raise IncompatibleRiskFramesError("only rectilinear 1-D coordinates are supported")
        if not _strictly_monotonic(latitudes) or not _strictly_monotonic(longitudes):
            raise IncompatibleRiskFramesError("grid coordinates must be strictly monotonic")

    def _frame_arrays(self, frame: RiskFrame) -> dict[str, np.ndarray | None]:
        payload = frame.payload
        extracted: dict[str, np.ndarray | None] = {
            "risk_score": np.asarray(
                payload["risk_score"].transpose("latitude", "longitude").values,
                dtype=float,
            ),
            "hard_mask": np.asarray(
                payload["hard_mask"].transpose("latitude", "longitude").values,
                dtype=bool,
            ),
            "confidence": np.asarray(
                payload["confidence"].transpose("latitude", "longitude").values,
                dtype=float,
            ),
            "environment_speed_factor": None,
        }
        if "environment_speed_factor" in payload.data_vars:
            extracted["environment_speed_factor"] = np.asarray(
                payload["environment_speed_factor"].transpose("latitude", "longitude").values,
                dtype=float,
            )
        for name, array in extracted.items():
            if array is not None and array.shape != (
                self._latitudes.size,
                self._longitudes.size,
            ):
                raise IncompatibleRiskFramesError(
                    f"RiskFrame payload {name} grid does not match coordinate grid"
                )
        return extracted

    def _bracket(self, sampled_at: datetime) -> tuple[int, int]:
        if sampled_at < self.start_time or sampled_at > self.end_time:
            raise RiskCoverageError(
                f"{sampled_at.isoformat()} is outside risk coverage "
                f"[{self.start_time.isoformat()}, {self.end_time.isoformat()}]"
            )
        index = bisect.bisect_left(self._valid_times, sampled_at)
        if index < len(self._valid_times) and self._valid_times[index] == sampled_at:
            return index, index
        return index - 1, index

    def _sample_frame(self, frame_index: int, longitude: float, latitude: float) -> _FrameSample:
        arrays = self._arrays[frame_index]
        cache_key = (frame_index, float(longitude), float(latitude))
        cached = self._sample_frame_cache.get(cache_key)
        if cached is not None:
            return cached
        lat_weights = _axis_weights(self._latitudes, float(latitude), axis="latitude")
        lon_weights = _axis_weights(self._longitudes, float(longitude), axis="longitude")
        contributors = tuple(
            (lat_index, lon_index, lat_weight * lon_weight)
            for lat_index, lat_weight in lat_weights
            for lon_index, lon_weight in lon_weights
            if lat_weight * lon_weight > 0.0
        )
        confidence_values = _array_values(
            arrays["confidence"], contributors, variable="confidence", finite=True
        )
        hard_values = _array_values(
            arrays["hard_mask"], contributors, variable="hard_mask", finite=False
        )
        risk_values = _array_values(
            arrays["risk_score"], contributors, variable="risk_score", finite=False
        )

        confidence = min(value for value, _ in confidence_values)
        hard_mask = any(bool(value) for value, _ in hard_values)
        if any(not np.isfinite(value) for value, _ in risk_values):
            if not hard_mask:
                raise RiskSamplingError(
                    "unknown risk at a navigable point cannot be treated as safe"
                )
            # The point is hard-blocked, so any edge through it will be
            # rejected by hard_mask before this risk_score is read.  1.0 is
            # a conservative placeholder rather than a measured value.
            risk_score = 1.0
        else:
            risk_score = sum(value * weight for value, weight in risk_values)
        speed_factor = _speed_factor(arrays, contributors)
        result = _FrameSample(
            risk_score=float(risk_score),
            confidence=float(confidence),
            hard_mask=hard_mask,
            environment_speed_factor=speed_factor,
        )
        self._bounded_cache_put(self._sample_frame_cache, cache_key, result)
        return result


@dataclass(frozen=True, slots=True)
class _FrameSample:
    risk_score: float
    confidence: float
    hard_mask: bool
    environment_speed_factor: float


def _strictly_monotonic(values: np.ndarray) -> bool:
    if len(values) < 1 or not np.all(np.isfinite(values)):
        return False
    differences = np.diff(values)
    return bool(np.all(differences > 0) or np.all(differences < 0))


def _axis_weights(
    coordinates: np.ndarray,
    target: float,
    *,
    axis: str,
) -> tuple[tuple[int, float], ...]:
    if not np.isfinite(target):
        raise RiskOutOfBoundsError(f"{axis} must be finite")
    ascending = coordinates[0] < coordinates[-1] if len(coordinates) > 1 else True
    ordered = coordinates if ascending else coordinates[::-1]
    tolerance = 1e-10
    if target < ordered[0] - tolerance or target > ordered[-1] + tolerance:
        raise RiskOutOfBoundsError(f"{axis}={target} is outside [{ordered[0]}, {ordered[-1]}]")
    upper = int(np.searchsorted(ordered, target, side="left"))
    exact_index = None
    for candidate in (upper - 1, upper):
        if 0 <= candidate < len(ordered) and abs(
            float(ordered[candidate]) - target
        ) <= tolerance:
            exact_index = candidate
            break
    if exact_index is not None:
        ordered_index = exact_index
        index = ordered_index if ascending else len(coordinates) - 1 - ordered_index
        return ((index, 1.0),)
    upper = int(np.searchsorted(ordered, target, side="right"))
    lower = upper - 1
    fraction = float((target - ordered[lower]) / (ordered[upper] - ordered[lower]))
    lower_index = lower if ascending else len(coordinates) - 1 - lower
    upper_index = upper if ascending else len(coordinates) - 1 - upper
    return ((lower_index, 1.0 - fraction), (upper_index, fraction))


def _array_values(
    data: np.ndarray | None,
    contributors: Iterable[tuple[int, int, float]],
    *,
    variable: str,
    finite: bool = True,
) -> tuple[tuple[float, float], ...]:
    if data is None:
        raise RiskSamplingError(f"{variable} contains a missing/non-finite sample")
    result: list[tuple[float, float]] = []
    for lat_index, lon_index, weight in contributors:
        value = data[lat_index, lon_index]
        if finite and not np.isfinite(value):
            raise RiskSamplingError(f"{variable} contains a missing/non-finite sample")
        result.append((float(value), weight))
    return tuple(result)


def _speed_factor(
    arrays: dict[str, np.ndarray | None],
    contributors: tuple[tuple[int, int, float], ...],
) -> float:
    """Read the optional canonical B-provided environmental effect.

    A missing v1 effect means "no declared effect", not inferred
    risk-to-speed coupling.  Component factors require a future contract
    version instead of being interpreted implicitly by the planner core.
    """

    data = arrays.get("environment_speed_factor")
    if data is None:
        return 1.0
    values = _array_values(data, contributors, variable="environment_speed_factor", finite=True)
    factor = min(value for value, _ in values)
    if not np.isfinite(factor) or factor <= 0.0 or factor > 1.0:
        raise RiskSamplingError("environment speed factors must be finite in (0, 1]")
    return float(factor)


def _lerp(lower: float, upper: float, fraction: float) -> float:
    return float(lower + fraction * (upper - lower))


def _risk_level(risk_score: float) -> int:
    return min(5, max(1, int(risk_score * 5.0) + 1))


def _outward_lower(value: float, *, floor: float | None = None) -> float:
    rounded = nextafter(float(value), float("-inf"))
    return max(floor, rounded) if floor is not None else rounded


def _outward_upper(value: float, *, ceiling: float | None = None) -> float:
    rounded = nextafter(float(value), float("inf"))
    return min(ceiling, rounded) if ceiling is not None else rounded


def _safe_utc(value: Any) -> datetime:
    """Keep failed interval evidence serializable even for malformed input."""

    if isinstance(value, datetime):
        try:
            return _utc(value, field="interval")
        except RiskSamplingError:
            pass
    return datetime(1970, 1, 1, tzinfo=UTC)


def _safe_float(value: Any) -> float:
    """Return a finite placeholder for malformed interval evidence."""

    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _jsonable_for_digest(value: Any) -> Any:
    if isinstance(value, datetime):
        return _safe_utc(value).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, ProvenanceKind):
        return value.value
    if isinstance(value, RiskIdentity):
        return {name: _jsonable_for_digest(getattr(value, name)) for name in value.__slots__}
    if isinstance(value, tuple):
        return [_jsonable_for_digest(item) for item in value]
    if isinstance(value, list):
        return [_jsonable_for_digest(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable_for_digest(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _moving_point(value: Any) -> tuple[float, float, datetime]:
    if isinstance(value, Mapping):
        longitude = value.get("lon", value.get("longitude"))
        latitude = value.get("lat", value.get("latitude"))
        sampled_at = value.get("eta", value.get("sampled_at"))
    else:
        longitude = getattr(value, "longitude", None)
        latitude = getattr(value, "latitude", None)
        sampled_at = getattr(value, "eta", getattr(value, "sampled_at", None))
    if isinstance(longitude, bool) or isinstance(latitude, bool):
        raise ValueError("moving path coordinates must be numeric")
    longitude = float(longitude)
    latitude = float(latitude)
    if (
        not np.isfinite(longitude)
        or not np.isfinite(latitude)
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
    ):
        raise ValueError("moving path coordinate is outside the geographic domain")
    try:
        eta = _utc(sampled_at, field="moving path eta")
    except (AttributeError, TypeError, RiskSamplingError) as error:
        raise ValueError("moving path eta must be timezone-aware UTC") from error
    return longitude, latitude, eta


def _great_circle_distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(np.radians, first)
    lon2, lat2 = map(np.radians, second)
    delta_lat = lat2 - lat1
    delta_lon = (lon2 - lon1 + np.pi) % (2.0 * np.pi) - np.pi
    haversine = (
        np.sin(delta_lat / 2.0) ** 2
        + np.cos(lat1) * np.cos(lat2) * np.sin(delta_lon / 2.0) ** 2
    )
    return float(2.0 * 6_371_008.8 * np.arcsin(min(1.0, math.sqrt(max(0.0, float(haversine))))))


def _great_circle_interpolate(
    first: tuple[float, float], second: tuple[float, float], fraction: float
) -> tuple[float, float]:
    if fraction == 0.0:
        return first
    if fraction == 1.0:
        return second
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    left = (
        math.cos(lat1) * math.cos(lon1),
        math.cos(lat1) * math.sin(lon1),
        math.sin(lat1),
    )
    right = (
        math.cos(lat2) * math.cos(lon2),
        math.cos(lat2) * math.sin(lon2),
        math.sin(lat2),
    )
    angle = math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(left, right, strict=True)))))
    if angle <= 1.0e-12:
        return first
    sine = math.sin(angle)
    weight_left = math.sin((1.0 - fraction) * angle) / sine
    weight_right = math.sin(fraction * angle) / sine
    vector = tuple(
        weight_left * left[index] + weight_right * right[index] for index in range(3)
    )
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1.0e-15 or not math.isfinite(norm):
        raise ValueError("great-circle interpolation became degenerate")
    vector = tuple(value / norm for value in vector)
    return math.degrees(math.atan2(vector[1], vector[0])), math.degrees(
        math.atan2(vector[2], math.hypot(vector[0], vector[1]))
    )


def _integrated_sampled_risk(values: Sequence[SampledRisk]) -> float:
    return float(sum(
        (left.risk_score + right.risk_score) * 0.5
        * (right.sampled_at - left.sampled_at).total_seconds() / 3600.0
        for left, right in pairwise(values)
    ))


__all__ = [
    "RiskIdentity",
    "RiskIntervalSample",
    "RiskSampler",
    "SampledRisk",
    "SweptTemporalEnvelope",
]
