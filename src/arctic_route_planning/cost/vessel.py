"""C-owned conversion of B environmental effects into final vessel speed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arctic_route_planning.domain.models import VesselModelConfig

KNOT_TO_KM_PER_HOUR = 1.852


class UnnavigableSpeedError(ValueError):
    """Raised when declared conditions put the vessel below safe steerage."""


@dataclass(frozen=True, slots=True)
class SpeedEstimate:
    speed_knots: float
    speed_km_per_hour: float
    environment_speed_factor: float
    relative_to_economic_speed: float


@dataclass(frozen=True, slots=True)
class VesselPerformanceModel:
    """Simple, replaceable demonstration performance model.

    B supplies environmental effect factors.  This C-side model applies those
    factors to a concrete vessel profile and makes the final navigability and
    effective-speed decision.  It intentionally does not infer speed loss from
    ``risk_score`` because that would double-count policy risk as physics.
    """

    economic_speed_knots: float
    minimum_steerage_speed_knots: float
    maximum_speed_knots: float
    minimum_speed_factor: float
    model_version: str = "demo-vessel-performance-v1"

    def __post_init__(self) -> None:
        if self.economic_speed_knots <= 0:
            raise ValueError("economic_speed_knots must be positive")
        if self.minimum_steerage_speed_knots <= 0:
            raise ValueError("minimum_steerage_speed_knots must be positive")
        if self.maximum_speed_knots < self.economic_speed_knots:
            raise ValueError("maximum_speed_knots must be >= economic_speed_knots")
        if self.minimum_steerage_speed_knots > self.maximum_speed_knots:
            raise ValueError("minimum steerage speed must not exceed maximum speed")
        if not 0 < self.minimum_speed_factor <= 1:
            raise ValueError("minimum_speed_factor must be in (0, 1]")

    @classmethod
    def from_configuration(
        cls,
        configuration: VesselModelConfig,
        *,
        model_version: str = "demo-vessel-performance-v1",
    ) -> VesselPerformanceModel:
        return cls(
            economic_speed_knots=configuration.economic_speed_knots,
            minimum_steerage_speed_knots=configuration.minimum_steerage_speed_knots,
            maximum_speed_knots=configuration.maximum_speed_knots,
            minimum_speed_factor=configuration.minimum_speed_factor,
            model_version=model_version,
        )

    def effective_speed(self, environment_speed_factor: float) -> SpeedEstimate:
        """Calculate final speed from a B-provided factor.

        Factors below the vessel's configured safe threshold, or resulting
        speeds below minimum steerage speed, are rejected instead of silently
        clamped upward.
        """

        if not 0 < environment_speed_factor <= 1:
            raise ValueError("environment_speed_factor must be in (0, 1]")
        if environment_speed_factor < self.minimum_speed_factor:
            raise UnnavigableSpeedError("environment speed factor is below the vessel minimum")
        speed_knots = min(
            self.maximum_speed_knots,
            self.economic_speed_knots * environment_speed_factor,
        )
        if speed_knots < self.minimum_steerage_speed_knots:
            raise UnnavigableSpeedError(
                "effective speed is below the vessel's minimum steerage speed"
            )
        return SpeedEstimate(
            speed_knots=speed_knots,
            speed_km_per_hour=speed_knots * KNOT_TO_KM_PER_HOUR,
            environment_speed_factor=environment_speed_factor,
            relative_to_economic_speed=speed_knots / self.economic_speed_knots,
        )
