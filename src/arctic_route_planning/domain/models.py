"""Immutable shared facts and package-local planning configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from arctic_route_contracts import (
    CalibrationStatus as CalibrationStatus,
)
from arctic_route_contracts import (
    CorridorDefinition as CorridorDefinition,
)
from arctic_route_contracts import (
    GeoPoint as GeoPoint,
)
from arctic_route_contracts import (
    RunContext as RunContext,
)
from arctic_route_contracts import (
    ScenarioDefinition as ScenarioDefinition,
)
from arctic_route_contracts import (
    VesselProfile as VesselProfile,
)

from arctic_route_planning.errors import ContractError


class ModelCalibrationStatus(StrEnum):
    DEMO_UNVALIDATED = "demo_unvalidated"
    CALIBRATED = "calibrated"


class ObjectiveMode(StrEnum):
    FASTEST = "fastest"
    LOW_RISK = "low_risk"
    RECOMMENDED = "recommended"


class PlanKind(StrEnum):
    INITIAL = "initial"
    REPLANNED = "replanned"


class ReplanReason(StrEnum):
    TIME = "time"
    DATA = "data"
    RISK = "risk"
    DEVIATION = "deviation"
    EVENT = "event"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class CostWeights:
    travel_time: float
    risk: float
    distance: float
    turn: float
    uncertainty: float

    def __post_init__(self) -> None:
        values = (
            self.travel_time,
            self.risk,
            self.distance,
            self.turn,
            self.uncertainty,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ContractError("成本权重必须是非负有限值")
        if not any(value > 0 for value in values):
            raise ContractError("至少一个成本权重必须大于 0")


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    schema_version: str = "planner-config.v1"
    time_bucket_minutes: int = 60
    connectivity: int = 8
    edge_sample_count: int = 3
    # This is a safety ceiling, not the requested voyage horizon.  Each run
    # supplies its own horizon from the shared Scenario/RunContext.
    max_search_hours: int = 216
    max_risk_frame_gap_minutes: int = 180
    minimum_confidence: float = 0.10
    allow_waiting: bool = False
    fastest: CostWeights = field(default_factory=lambda: CostWeights(1.0, 0.20, 0.05, 0.05, 0.20))
    low_risk: CostWeights = field(default_factory=lambda: CostWeights(0.45, 2.50, 0.05, 0.08, 0.80))
    recommended: CostWeights = field(
        default_factory=lambda: CostWeights(0.85, 1.30, 0.08, 0.10, 0.45)
    )
    # Optional operational planning reserve.  This deliberately applies only
    # to the planner's ETA/recommended-speed estimate; B's environmental
    # factor, the vessel maximum, and motion qualification limits remain the
    # declared physical values.
    operational_speed_reserve_fraction: float = 0.0

    def __post_init__(self) -> None:
        if self.schema_version != "planner-config.v1":
            raise ContractError("PlannerConfig.schema_version 必须是 planner-config.v1")
        if self.time_bucket_minutes <= 0 or self.max_search_hours <= 0:
            raise ContractError("时间桶和搜索时域必须为正数")
        if self.max_search_hours > 216:
            raise ContractError("C 当前正式搜索硬上限为 216 小时")
        if self.connectivity not in (4, 8):
            raise ContractError("connectivity 只能是 4 或 8")
        if self.edge_sample_count < 3 or self.edge_sample_count % 2 == 0:
            raise ContractError("edge_sample_count 必须是不小于 3 的奇数")
        if self.max_risk_frame_gap_minutes <= 0:
            raise ContractError("max_risk_frame_gap_minutes 必须为正数")
        if not 0 <= self.minimum_confidence <= 1:
            raise ContractError("minimum_confidence 必须位于 [0, 1]")
        if (
            isinstance(self.operational_speed_reserve_fraction, bool)
            or not math.isfinite(self.operational_speed_reserve_fraction)
            or not 0.0 <= self.operational_speed_reserve_fraction <= 0.10
        ):
            raise ContractError(
                "operational_speed_reserve_fraction 必须位于 [0, 0.10]"
            )
        if self.allow_waiting:
            raise ContractError("v1 尚未实现等待动作，allow_waiting 必须为 false")

    def weights_for(self, mode: ObjectiveMode) -> CostWeights:
        return {
            ObjectiveMode.FASTEST: self.fastest,
            ObjectiveMode.LOW_RISK: self.low_risk,
            ObjectiveMode.RECOMMENDED: self.recommended,
        }[mode]


@dataclass(frozen=True, slots=True)
class ReplanningConfig:
    schema_version: str = "replanning-config.v1"
    minimum_interval_minutes: int = 60
    route_switch_gain_threshold: float = 0.05
    risk_trigger_threshold: float = 0.65
    deviation_trigger_km: float = 10.0
    hysteresis: float = 0.03
    # ``None`` preserves the historical RouteSwitchGate behavior, where the
    # risk hysteresis is also the tolerated regression.  A configured value is
    # an explicit maximum increase in candidate max risk; zero is strict
    # non-degradation for controlled replay scenarios.
    maximum_risk_regression_tolerance: float | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "replanning-config.v1":
            raise ContractError("ReplanningConfig.schema_version 必须是 replanning-config.v1")
        if self.minimum_interval_minutes < 0:
            raise ContractError("minimum_interval_minutes 不能为负")
        for name in ("route_switch_gain_threshold", "risk_trigger_threshold", "hysteresis"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ContractError(f"{name} 必须位于 [0, 1]")
        if self.maximum_risk_regression_tolerance is not None and (
            isinstance(self.maximum_risk_regression_tolerance, bool)
            or not math.isfinite(self.maximum_risk_regression_tolerance)
            or not 0.0 <= self.maximum_risk_regression_tolerance <= 1.0
        ):
            raise ContractError("maximum_risk_regression_tolerance 必须位于 [0, 1]")
        if not math.isfinite(self.deviation_trigger_km) or self.deviation_trigger_km <= 0:
            raise ContractError("deviation_trigger_km 必须为正有限值")


@dataclass(frozen=True, slots=True)
class VesselModelConfig:
    """C-owned, unvalidated performance assumptions for one shared vessel fact record."""

    schema_version: str
    vessel_profile_id: str
    vessel_profile_version: str
    economic_speed_knots: float
    minimum_steerage_speed_knots: float
    maximum_speed_knots: float
    minimum_speed_factor: float
    turn_radius_m: float
    under_keel_clearance_m: float
    bathymetry_hard_constraint_enabled: bool
    calibration_status: ModelCalibrationStatus
    source_notes: str

    def __post_init__(self) -> None:
        if self.schema_version != "c.vessel-model-config.v1":
            raise ContractError(
                "VesselModelConfig.schema_version 必须是 c.vessel-model-config.v1"
            )
        for name in (
            "vessel_profile_id",
            "vessel_profile_version",
            "source_notes",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"{name} 不能为空")
        positive = (
            self.economic_speed_knots,
            self.minimum_steerage_speed_knots,
            self.maximum_speed_knots,
            self.turn_radius_m,
            self.under_keel_clearance_m,
        )
        if any(not math.isfinite(value) or value <= 0 for value in positive):
            raise ContractError("C 船速、转弯半径和净空假设必须为正有限值")
        if not (
            self.minimum_steerage_speed_knots
            <= self.economic_speed_knots
            <= self.maximum_speed_knots
        ):
            raise ContractError("C 船速必须满足 minimum <= economic <= maximum")
        if not math.isfinite(self.minimum_speed_factor) or not 0 < self.minimum_speed_factor <= 1:
            raise ContractError("minimum_speed_factor 必须位于 (0, 1]")
        if self.bathymetry_hard_constraint_enabled:
            raise ContractError("当前研究基线保留水深接口，但不得启用核心硬约束")
