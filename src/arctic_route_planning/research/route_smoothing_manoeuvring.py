"""Synthetic, uncalibrated manoeuvring envelopes for route-smoothing research.

This module intentionally does not modify the formal vessel model.  It only
checks a sampled curve against explicit assumptions for a bulk-carrier-sized
synthetic vessel.  Speeds are converted to metres per second before applying
the pointwise formulas:

``R_min(v) = max(2_000 m, v / omega_max, v**2 / a_y_max)``
``yaw_rate = v * kappa``
``lateral_acceleration = v**2 * kappa``

The resulting evidence is never a real-vessel calibration or production
qualification.  Invalid input returns a fail-closed evidence object rather
than being silently repaired.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

KNOT_TO_MPS = 0.5144444444444445
BASE_MIN_RADIUS_M = 2_000.0
SYNTHETIC_UNCALIBRATED = "SYNTHETIC_UNCALIBRATED"
SYNTHETIC_ONLY = "SYNTHETIC_ONLY"
NO_PRODUCTION_QUALIFICATION = "NO_PRODUCTION_QUALIFICATION"

_SCENARIO_LIMITS = {
    "conservative": (0.15, 0.02),
    "nominal": (0.25, 0.04),
    "permissive": (0.35, 0.06),
}
_SPEED_UNITS = {
    "m/s": 1.0,
    "mps": 1.0,
    "knots": KNOT_TO_MPS,
    "knot": KNOT_TO_MPS,
    "kt": KNOT_TO_MPS,
}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _series(values: Sequence[float], name: str) -> tuple[float, ...]:
    if values is None or isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a one-dimensional numeric sequence")
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a one-dimensional numeric sequence") from exc
    if not raw_values:
        raise ValueError(f"{name} must not be empty")
    result = tuple(_finite(value, f"{name}[{index}]") for index, value in enumerate(raw_values))
    if any(value < 0.0 for value in result):
        raise ValueError(f"{name} must not contain negative values")
    return result


def _normalise_speed_unit(speed_unit: str) -> tuple[str, float]:
    if not isinstance(speed_unit, str):
        raise ValueError("speed_unit must be 'm/s' or 'knots'")
    key = speed_unit.strip().lower()
    try:
        factor = _SPEED_UNITS[key]
    except KeyError as exc:
        raise ValueError("speed_unit must be 'm/s' or 'knots'") from exc
    canonical = "m/s" if factor == 1.0 else "knots"
    return canonical, factor


@dataclass(frozen=True, slots=True)
class SyntheticManoeuvringEnvelope:
    """One explicit synthetic envelope; values are not vessel calibration."""

    scenario: str = "conservative"
    max_yaw_rate_deg_s: float | None = None
    max_lateral_acceleration_m_s2: float | None = None
    base_min_radius_m: float = BASE_MIN_RADIUS_M

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, str):
            raise ValueError("scenario must be a string")
        scenario = self.scenario.strip().lower()
        if scenario not in _SCENARIO_LIMITS:
            raise ValueError("scenario must be conservative, nominal or permissive")
        default_yaw, default_acceleration = _SCENARIO_LIMITS[scenario]
        yaw = default_yaw if self.max_yaw_rate_deg_s is None else _finite(
            self.max_yaw_rate_deg_s, "max_yaw_rate_deg_s"
        )
        acceleration = (
            default_acceleration
            if self.max_lateral_acceleration_m_s2 is None
            else _finite(self.max_lateral_acceleration_m_s2, "max_lateral_acceleration_m_s2")
        )
        base_radius = _finite(self.base_min_radius_m, "base_min_radius_m")
        if yaw <= 0.0 or acceleration <= 0.0 or base_radius <= 0.0:
            raise ValueError("manoeuvring limits and base radius must be positive")
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "max_yaw_rate_deg_s", yaw)
        object.__setattr__(self, "max_lateral_acceleration_m_s2", acceleration)
        object.__setattr__(self, "base_min_radius_m", base_radius)

    @classmethod
    def conservative(cls) -> SyntheticManoeuvringEnvelope:
        return cls("conservative")

    @classmethod
    def nominal(cls) -> SyntheticManoeuvringEnvelope:
        return cls("nominal")

    @classmethod
    def permissive(cls) -> SyntheticManoeuvringEnvelope:
        return cls("permissive")

    @classmethod
    def for_scenario(cls, scenario: str) -> SyntheticManoeuvringEnvelope:
        return cls(scenario)

    @property
    def max_yaw_rate_rad_s(self) -> float:
        return math.radians(self.max_yaw_rate_deg_s)

    @property
    def calibration_status(self) -> str:
        return SYNTHETIC_UNCALIBRATED

    @property
    def qualification_label(self) -> str:
        return SYNTHETIC_UNCALIBRATED

    @property
    def labels(self) -> tuple[str, ...]:
        return SYNTHETIC_UNCALIBRATED, SYNTHETIC_ONLY, NO_PRODUCTION_QUALIFICATION

    def minimum_allowed_radius_m(self, speed_m_s: float) -> float:
        """Return the pointwise radius floor for a speed in metres/second."""

        speed = _finite(speed_m_s, "speed_m_s")
        if speed < 0.0:
            raise ValueError("speed_m_s must not be negative")
        result = max(
            self.base_min_radius_m,
            speed / self.max_yaw_rate_rad_s,
            speed**2 / self.max_lateral_acceleration_m_s2,
        )
        if not math.isfinite(result):
            raise ValueError("minimum radius is non-finite")
        return result

    # Short aliases keep the formula discoverable without changing its units.
    def minimum_radius_m(self, speed_m_s: float) -> float:
        return self.minimum_allowed_radius_m(speed_m_s)

    def radius_floor_m(self, speed_m_s: float) -> float:
        return self.minimum_allowed_radius_m(speed_m_s)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "base_min_radius_m": self.base_min_radius_m,
            "max_yaw_rate_deg_s": self.max_yaw_rate_deg_s,
            "max_yaw_rate_rad_s": self.max_yaw_rate_rad_s,
            "max_lateral_acceleration_m_s2": self.max_lateral_acceleration_m_s2,
            "calibration_status": self.calibration_status,
            "labels": list(self.labels),
            "units": {
                "speed": "m/s",
                "curvature": "1/m",
                "yaw_rate": "rad/s and deg/s",
                "lateral_acceleration": "m/s^2",
                "radius": "m",
            },
        }

    def _failed(self, reason: str, *, speed_unit: str = "m/s") -> ManoeuvringEvidence:
        evidence = {
            "accepted": False,
            "status": "FAIL_CLOSED",
            "failure_reason": reason,
            "scenario": self.scenario,
            "calibration_status": self.calibration_status,
            "labels": list(self.labels),
            "production_qualified": False,
            "speed_unit": speed_unit,
            "envelope": self.as_dict(),
        }
        return ManoeuvringEvidence(
            accepted=False,
            status="FAIL_CLOSED",
            scenario=self.scenario,
            speed_unit=speed_unit,
            curvatures_m_inv=(),
            speeds_m_s=(),
            minimum_allowed_radii_m=(),
            actual_radii_m=(),
            yaw_rates_rad_s=(),
            yaw_rates_deg_s=(),
            lateral_accelerations_m_s2=(),
            violating_indices=(),
            failure_reasons=(reason,),
            labels=self.labels,
            calibration_status=self.calibration_status,
            production_qualified=False,
            evidence=evidence,
        )

    def evaluate(
        self,
        curvature_m_inv: Sequence[float],
        speeds: Sequence[float],
        *,
        speed_unit: str = "m/s",
    ) -> ManoeuvringEvidence:
        """Evaluate paired curvature and speed samples fail-closed.

        ``curvature_m_inv`` is non-negative curvature in ``1/m``.  ``speeds``
        is interpreted in ``m/s`` by default or in knots when
        ``speed_unit='knots'``.  A shape, finiteness, unit or sign error is a
        failed evaluation, never an implicit broadcast or clipping operation.
        """

        try:
            canonical_unit, speed_factor = _normalise_speed_unit(speed_unit)
            curvatures = _series(curvature_m_inv, "curvature_m_inv")
            raw_speeds = _series(speeds, "speeds")
        except ValueError as exc:
            return self._failed(str(exc), speed_unit=str(speed_unit))
        if len(curvatures) != len(raw_speeds):
            return self._failed(
                "curvature and speed arrays must have equal shape", speed_unit=canonical_unit
            )

        speeds_m_s = tuple(value * speed_factor for value in raw_speeds)
        if any(not math.isfinite(value) for value in speeds_m_s):
            return self._failed("converted speeds are non-finite", speed_unit=canonical_unit)
        minimum_radii = tuple(self.minimum_allowed_radius_m(value) for value in speeds_m_s)
        actual_radii = tuple(
            math.inf if curvature == 0.0 else 1.0 / curvature for curvature in curvatures
        )
        yaw_rates_rad_s = tuple(
            speed * curvature
            for speed, curvature in zip(speeds_m_s, curvatures, strict=True)
        )
        yaw_rates_deg_s = tuple(math.degrees(value) for value in yaw_rates_rad_s)
        lateral_accelerations = tuple(
            speed**2 * curvature for speed, curvature in zip(speeds_m_s, curvatures, strict=True)
        )
        violating_indices = tuple(
            index
            for index, (actual, required, yaw, acceleration) in enumerate(
                zip(
                    actual_radii,
                    minimum_radii,
                    yaw_rates_rad_s,
                    lateral_accelerations,
                    strict=True,
                )
            )
            if actual + 1.0e-9 < required
            or yaw > self.max_yaw_rate_rad_s + 1.0e-12
            or acceleration > self.max_lateral_acceleration_m_s2 + 1.0e-12
        )
        accepted = not violating_indices
        failure_reasons = () if accepted else ("pointwise_manoeuvring_limit_exceeded",)
        evidence = {
            "accepted": accepted,
            "status": "PASS" if accepted else "FAIL_CLOSED",
            "scenario": self.scenario,
            "speed_unit_input": canonical_unit,
            "units": self.as_dict()["units"],
            "sample_count": len(curvatures),
            "minimum_allowed_radius_m": list(minimum_radii),
            "actual_radius_m": [value if math.isfinite(value) else None for value in actual_radii],
            "yaw_rate_rad_s": list(yaw_rates_rad_s),
            "yaw_rate_deg_s": list(yaw_rates_deg_s),
            "lateral_acceleration_m_s2": list(lateral_accelerations),
            "violating_indices": list(violating_indices),
            "calibration_status": self.calibration_status,
            "labels": list(self.labels),
            "production_qualified": False,
        }
        return ManoeuvringEvidence(
            accepted=accepted,
            status="PASS" if accepted else "FAIL_CLOSED",
            scenario=self.scenario,
            speed_unit=canonical_unit,
            curvatures_m_inv=curvatures,
            speeds_m_s=speeds_m_s,
            minimum_allowed_radii_m=minimum_radii,
            actual_radii_m=actual_radii,
            yaw_rates_rad_s=yaw_rates_rad_s,
            yaw_rates_deg_s=yaw_rates_deg_s,
            lateral_accelerations_m_s2=lateral_accelerations,
            violating_indices=violating_indices,
            failure_reasons=failure_reasons,
            labels=self.labels,
            calibration_status=self.calibration_status,
            production_qualified=False,
            evidence=evidence,
        )

    def evaluate_pointwise(
        self,
        curvature_m_inv: Sequence[float],
        speeds: Sequence[float],
        *,
        speed_unit: str = "m/s",
    ) -> ManoeuvringEvidence:
        return self.evaluate(curvature_m_inv, speeds, speed_unit=speed_unit)

    def validate(
        self,
        curvature_m_inv: Sequence[float],
        speeds: Sequence[float],
        *,
        speed_unit: str = "m/s",
    ) -> ManoeuvringEvidence:
        return self.evaluate(curvature_m_inv, speeds, speed_unit=speed_unit)


@dataclass(frozen=True, slots=True)
class ManoeuvringEvidence:
    """Immutable pointwise evidence and explicit qualification labels."""

    accepted: bool
    status: str
    scenario: str
    speed_unit: str
    curvatures_m_inv: tuple[float, ...]
    speeds_m_s: tuple[float, ...]
    minimum_allowed_radii_m: tuple[float, ...]
    actual_radii_m: tuple[float, ...]
    yaw_rates_rad_s: tuple[float, ...]
    yaw_rates_deg_s: tuple[float, ...]
    lateral_accelerations_m_s2: tuple[float, ...]
    violating_indices: tuple[int, ...]
    failure_reasons: tuple[str, ...]
    labels: tuple[str, ...]
    calibration_status: str
    production_qualified: bool
    evidence: Mapping[str, Any]

    @property
    def research_eligible(self) -> bool:
        return self.accepted

    @property
    def minimum_allowed_radius_m(self) -> float:
        return max(self.minimum_allowed_radii_m, default=math.nan)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "scenario": self.scenario,
            "speed_unit": self.speed_unit,
            "curvatures_m_inv": list(self.curvatures_m_inv),
            "speeds_m_s": list(self.speeds_m_s),
            "minimum_allowed_radii_m": list(self.minimum_allowed_radii_m),
            "actual_radii_m": [
                value if math.isfinite(value) else None for value in self.actual_radii_m
            ],
            "yaw_rates_rad_s": list(self.yaw_rates_rad_s),
            "yaw_rates_deg_s": list(self.yaw_rates_deg_s),
            "lateral_accelerations_m_s2": list(self.lateral_accelerations_m_s2),
            "violating_indices": list(self.violating_indices),
            "failure_reasons": list(self.failure_reasons),
            "labels": list(self.labels),
            "calibration_status": self.calibration_status,
            "production_qualified": self.production_qualified,
            "research_eligible": self.research_eligible,
            "evidence": dict(self.evidence),
        }


def evaluate_synthetic_manoeuvring_envelope(
    curvature_m_inv: Sequence[float],
    speeds: Sequence[float],
    envelope: SyntheticManoeuvringEnvelope | None = None,
    *,
    speed_unit: str = "m/s",
) -> ManoeuvringEvidence:
    """Functional wrapper around :meth:`SyntheticManoeuvringEnvelope.evaluate`."""

    selected = envelope if envelope is not None else SyntheticManoeuvringEnvelope.conservative()
    if not isinstance(selected, SyntheticManoeuvringEnvelope):
        raise TypeError("envelope must be a SyntheticManoeuvringEnvelope")
    return selected.evaluate(curvature_m_inv, speeds, speed_unit=speed_unit)


__all__ = [
    "BASE_MIN_RADIUS_M",
    "KNOT_TO_MPS",
    "NO_PRODUCTION_QUALIFICATION",
    "SYNTHETIC_ONLY",
    "SYNTHETIC_UNCALIBRATED",
    "ManoeuvringEvidence",
    "SyntheticManoeuvringEnvelope",
    "evaluate_synthetic_manoeuvring_envelope",
]
