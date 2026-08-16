"""Strict ETA-aware sampling of immutable :class:`RiskFrame` objects.

The sampler performs *C-side sampling*, not B-side prediction.  It only
interpolates between two already published, compatible risk frames and never
extrapolates beyond the supplied window.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
