"""Versioned formula-derived engineering vessel profile for route motion."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from arctic_route_planning.contracts.route_motion import (
    ROUTE_MOTION_PROFILE_SCHEMA_VERSION,
)
from arctic_route_planning.publishing.route_motion_serialization import canonical_sha256

KNOT_TO_MPS = 0.5144444444444445


@dataclass(frozen=True, slots=True)
class EngineeringRouteMotionProfile:
    schema_version: str = ROUTE_MOTION_PROFILE_SCHEMA_VERSION
    profile_id: str = "nordic_odyssey_formula_reference_v1"
    vessel_profile_id: str = "nordic_odyssey_reference_v1"
    vessel_profile_version: str = "1.0.0"
    length_m: float = 225.0
    beam_m: float = 32.31
    draft_m: float = 14.08
    economic_speed_knots: float = 10.0
    maximum_speed_knots: float = 15.7
    minimum_steerage_speed_knots: float = 3.0
    base_minimum_radius_m: float = 2_000.0
    maximum_yaw_rate_deg_s: float = 0.15
    maximum_lateral_acceleration_m_s2: float = 0.02
    primary_corridor_margin_m: float = 500.0
    evidence_kind: str = "FORMULA_DERIVED_ENGINEERING_REFERENCE"
    real_vessel_calibrated: bool = False
    bathymetry_hard_constraint_enabled: bool = False
    source_notes: str = (
        "Public bulk-carrier scale plus explicit conservative engineering assumptions; "
        "not a manoeuvring booklet, full-scale trial, navigation certification, or UKC proof."
    )

    def __post_init__(self) -> None:
        if self.schema_version != ROUTE_MOTION_PROFILE_SCHEMA_VERSION:
            raise ValueError("unsupported route-motion vessel profile schema")
        if self.evidence_kind != "FORMULA_DERIVED_ENGINEERING_REFERENCE":
            raise ValueError("route-motion profile evidence kind is invalid")
        if self.real_vessel_calibrated or self.bathymetry_hard_constraint_enabled:
            raise ValueError("engineering reference profile cannot claim calibration or UKC")
        positive = (
            self.length_m, self.beam_m, self.draft_m, self.economic_speed_knots,
            self.maximum_speed_knots, self.minimum_steerage_speed_knots,
            self.base_minimum_radius_m, self.maximum_yaw_rate_deg_s,
            self.maximum_lateral_acceleration_m_s2, self.primary_corridor_margin_m,
        )
        if any(not math.isfinite(value) or value <= 0.0 for value in positive):
            raise ValueError("route-motion vessel parameters must be positive and finite")
        if not (
            self.minimum_steerage_speed_knots
            <= self.economic_speed_knots
            <= self.maximum_speed_knots
        ):
            raise ValueError("route-motion vessel speed ordering is invalid")

    @property
    def digest(self) -> str:
        return canonical_sha256(asdict(self))

    def minimum_radius_m(self, speed_knots: float) -> float:
        if isinstance(speed_knots, bool) or not math.isfinite(speed_knots) or speed_knots < 0:
            raise ValueError("speed_knots must be finite and non-negative")
        speed_m_s = speed_knots * KNOT_TO_MPS
        yaw_rate_rad_s = math.radians(self.maximum_yaw_rate_deg_s)
        return max(
            self.base_minimum_radius_m,
            speed_m_s / yaw_rate_rad_s,
            speed_m_s**2 / self.maximum_lateral_acceleration_m_s2,
        )

    def corridor_buffer_m(
        self,
        *,
        position_error_m: float,
        transform_error_m: float,
        chord_error_m: float,
    ) -> float:
        errors = (position_error_m, transform_error_m, chord_error_m)
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value < 0.0
            for value in errors
        ):
            raise ValueError("corridor errors must be finite and non-negative")
        return max(
            self.primary_corridor_margin_m,
            self.beam_m / 2.0 + sum(errors),
        )

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["profile_digest"] = self.digest
        value["minimum_radius_formula"] = "max(base_radius, v/omega_max, v^2/a_lat_max)"
        value["units"] = {
            "length_beam_draft_radius_margin": "m",
            "speed": "knots",
            "yaw_rate": "deg/s",
            "lateral_acceleration": "m/s^2",
        }
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EngineeringRouteMotionProfile:
        """Load and verify the exact versioned engineering profile artifact."""

        base_fields = set(cls.__dataclass_fields__)
        expected = base_fields | {
            "profile_digest", "minimum_radius_formula", "units",
        }
        if set(value) != expected:
            raise ValueError("route-motion profile fields differ from v1")
        payload = {name: value[name] for name in base_fields}
        profile = cls(**payload)
        if value["profile_digest"] != profile.digest:
            raise ValueError("route-motion profile digest does not match content")
        if value["minimum_radius_formula"] != (
            "max(base_radius, v/omega_max, v^2/a_lat_max)"
        ):
            raise ValueError("route-motion minimum radius formula differs from v1")
        if value["units"] != {
            "length_beam_draft_radius_margin": "m",
            "speed": "knots",
            "yaw_rate": "deg/s",
            "lateral_acceleration": "m/s^2",
        }:
            raise ValueError("route-motion profile units differ from v1")
        return profile


__all__ = ["KNOT_TO_MPS", "EngineeringRouteMotionProfile"]
