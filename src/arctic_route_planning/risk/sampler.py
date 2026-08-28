"""Strict ETA-aware sampling of immutable :class:`RiskFrame` objects.

The sampler performs *C-side sampling*, not B-side prediction.  It only
interpolates between two already published, compatible risk frames and never
extrapolates beyond the supplied window.
"""

from __future__ import annotations

import bisect
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, field="interval.start"))
        object.__setattr__(self, "end", _utc(self.end, field="interval.end"))
        if self.start > self.end:
            raise ValueError("interval.start must not be after interval.end")
        if not np.isfinite(self.longitude) or not np.isfinite(self.latitude):
            raise ValueError("interval coordinates must be finite")
        if self.schema_version != "c.risk-interval-sample.v1":
            raise ValueError("unsupported risk interval sample schema")
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
        self._latitudes = np.asarray(
            self._frames[0].payload.coords["latitude"].values, dtype=float
        )
        self._longitudes = np.asarray(
            self._frames[0].payload.coords["longitude"].values, dtype=float
        )
        self._arrays: tuple[dict[str, np.ndarray | None], ...] = tuple(
            self._frame_arrays(frame) for frame in self._frames
        )

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
                for left, right in zip(
                    frame_indices,
                    range(lower_index + 1, upper_index + 1),
                    strict=True,
                ):
                    gap = self._frames[right].valid_time - self._frames[left].valid_time
                    if gap > self._max_frame_gap:
                        raise RiskCoverageError(
                            f"interval crosses RiskFrame gap {gap}, exceeding {self._max_frame_gap}"
                        )
            values = tuple(
                self._sample_frame_interval(index, longitude, latitude)
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

        risk_values = tuple(value.risk_score for value in values)
        confidence_values = tuple(value.confidence for value in values)
        speed_values = tuple(value.environment_speed_factor for value in values)
        source_ids = tuple(
            dict.fromkeys(self._frames[index].risk_id for index in frame_indices)
        )
        return RiskIntervalSample(
            start=interval_start,
            end=interval_end,
            longitude=float(longitude),
            latitude=float(latitude),
            risk_lower=_outward_lower(min(risk_values), floor=0.0),
            risk_upper=_outward_upper(max(risk_values), ceiling=1.0),
            confidence_lower=_outward_lower(min(confidence_values), floor=0.0),
            environment_speed_factor_lower=_outward_lower(min(speed_values), floor=0.0),
            environment_speed_factor_upper=_outward_upper(max(speed_values), ceiling=1.0),
            hard_mask_possible=any(value.hard_mask for value in values),
            source_risk_ids=source_ids,
            covered_frame_times=tuple(self._frames[index].valid_time for index in frame_indices),
            coverage_complete=True,
            evaluator_digest=self.interval_evaluator_digest,
        )

    @property
    def interval_evaluator_digest(self) -> str:
        """Stable identity for this sidecar's interpolation/evaluator rules."""

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
        return hashlib.sha256(encoded).hexdigest()

    def _sample_frame_interval(
        self,
        frame_index: int,
        longitude: float,
        latitude: float,
    ) -> _FrameSample:
        """Sample one frame while refusing the normal hard-mask placeholder."""

        arrays = self._arrays[frame_index]
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
        return _FrameSample(
            risk_score=float(sum(value * weight for value, weight in risk_values)),
            confidence=float(min(value for value, _ in confidence_values)),
            hard_mask=hard_mask,
            environment_speed_factor=float(min(factors)),
        )

    def _contributors(
        self,
        longitude: float,
        latitude: float,
    ) -> tuple[tuple[int, int, float], ...]:
        lat_weights = _axis_weights(self._latitudes, float(latitude), axis="latitude")
        lon_weights = _axis_weights(self._longitudes, float(longitude), axis="longitude")
        return tuple(
            (lat_index, lon_index, lat_weight * lon_weight)
            for lat_index, lat_weight in lat_weights
            for lon_index, lon_weight in lon_weights
            if lat_weight * lon_weight > 0.0
        )

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
                payload["environment_speed_factor"]
                .transpose("latitude", "longitude")
                .values,
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
        return _FrameSample(
            risk_score=float(risk_score),
            confidence=float(confidence),
            hard_mask=hard_mask,
            environment_speed_factor=speed_factor,
        )


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
    exact = np.flatnonzero(np.isclose(ordered, target, rtol=0.0, atol=tolerance))
    if exact.size:
        ordered_index = int(exact[0])
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
    values = _array_values(
        data, contributors, variable="environment_speed_factor", finite=True
    )
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


__all__ = ["RiskIdentity", "RiskIntervalSample", "RiskSampler", "SampledRisk"]
