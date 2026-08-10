"""Immutable shared facts and package-local planning configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import ensure_utc


class CalibrationStatus(StrEnum):
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
class GeoPoint:
    longitude: float
    latitude: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ContractError("longitude 必须是 [-180, 180] 内有限值")
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ContractError("latitude 必须是 [-90, 90] 内有限值")


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    schema_version: str
    scenario_id: str
    version: str
    corridor_id: str
    display_name: str
    start: GeoPoint
    destination: GeoPoint
    bbox: tuple[float, float, float, float]
    crs: str
    simulation_start: datetime
    simulation_end: datetime
    dataset_snapshot_id: str
    default_vessel_profile_id: str
    random_seed: int = 0

    def __post_init__(self) -> None:
        if self.schema_version != "scenario.v1":
            raise ContractError("ScenarioDefinition.schema_version 必须是 scenario.v1")
        for field_name in (
            "scenario_id",
            "version",
            "corridor_id",
            "display_name",
            "dataset_snapshot_id",
            "default_vessel_profile_id",
        ):
            if not getattr(self, field_name).strip():
                raise ContractError(f"{field_name} 不能为空")
        west, south, east, north = self.bbox
        if not all(math.isfinite(value) for value in self.bbox):
            raise ContractError("bbox 必须全部为有限值")
        if not west < east or not south < north:
            raise ContractError("bbox 必须满足 west < east 且 south < north")
        for name, point in (("start", self.start), ("destination", self.destination)):
            if not west <= point.longitude <= east or not south <= point.latitude <= north:
                raise ContractError(f"{name} 必须位于 bbox 内")
        if self.crs != "EPSG:4326":
            raise ContractError("v1 场景当前只接受 EPSG:4326")
        start = ensure_utc(self.simulation_start, field="simulation_start")
        end = ensure_utc(self.simulation_end, field="simulation_end")
        if end <= start:
            raise ContractError("simulation_end 必须晚于 simulation_start")
        object.__setattr__(self, "simulation_start", start)
        object.__setattr__(self, "simulation_end", end)


@dataclass(frozen=True, slots=True)
class VesselProfile:
    schema_version: str
    vessel_profile_id: str
    version: str
    display_name: str
    calibration_status: CalibrationStatus
    ice_class: str
    load_condition: str
    draft_m: float
    under_keel_clearance_m: float
    min_speed_knots: float
    cruise_speed_knots: float
    max_speed_knots: float
    min_speed_factor: float
    turn_radius_m: float
    source_notes: str

    def __post_init__(self) -> None:
        if self.schema_version != "vessel-profile.v1":
            raise ContractError("VesselProfile.schema_version 必须是 vessel-profile.v1")
        for field_name in (
            "vessel_profile_id",
            "version",
            "display_name",
            "ice_class",
            "load_condition",
            "source_notes",
        ):
            if not getattr(self, field_name).strip():
                raise ContractError(f"{field_name} 不能为空")
        positive = {
            "draft_m": self.draft_m,
            "under_keel_clearance_m": self.under_keel_clearance_m,
            "min_speed_knots": self.min_speed_knots,
            "cruise_speed_knots": self.cruise_speed_knots,
            "max_speed_knots": self.max_speed_knots,
            "turn_radius_m": self.turn_radius_m,
        }
        if any(not math.isfinite(value) or value <= 0 for value in positive.values()):
            raise ContractError("船舶尺寸、速度和转弯半径必须为正有限值")
        if not self.min_speed_knots <= self.cruise_speed_knots <= self.max_speed_knots:
            raise ContractError("船速必须满足 min <= cruise <= max")
        if not math.isfinite(self.min_speed_factor) or not 0 < self.min_speed_factor <= 1:
            raise ContractError("min_speed_factor 必须位于 (0, 1]")


@dataclass(frozen=True, slots=True)
class RunContext:
    schema_version: str
    run_id: str
    scenario_id: str
    scenario_version: str
    vessel_profile_id: str
    vessel_profile_version: str
    config_digest: str

    def __post_init__(self) -> None:
        if self.schema_version != "run-context.v1":
            raise ContractError("RunContext.schema_version 必须是 run-context.v1")
        if any(
            not getattr(self, name).strip()
            for name in (
                "run_id",
                "scenario_id",
                "scenario_version",
                "vessel_profile_id",
                "vessel_profile_version",
            )
        ):
            raise ContractError("RunContext 标识和版本不能为空")
        _validate_digest(self.config_digest)


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
    max_search_hours: int = 168
    max_risk_frame_gap_minutes: int = 180
    minimum_confidence: float = 0.10
    allow_waiting: bool = False
    fastest: CostWeights = field(default_factory=lambda: CostWeights(1.0, 0.20, 0.05, 0.05, 0.20))
    low_risk: CostWeights = field(default_factory=lambda: CostWeights(0.45, 2.50, 0.05, 0.08, 0.80))
    recommended: CostWeights = field(
        default_factory=lambda: CostWeights(0.85, 1.30, 0.08, 0.10, 0.45)
    )

    def __post_init__(self) -> None:
        if self.schema_version != "planner-config.v1":
            raise ContractError("PlannerConfig.schema_version 必须是 planner-config.v1")
        if self.time_bucket_minutes <= 0 or self.max_search_hours <= 0:
            raise ContractError("时间桶和搜索时域必须为正数")
        if self.connectivity not in (4, 8):
            raise ContractError("connectivity 只能是 4 或 8")
        if self.edge_sample_count < 3 or self.edge_sample_count % 2 == 0:
            raise ContractError("edge_sample_count 必须是不小于 3 的奇数")
        if self.max_risk_frame_gap_minutes <= 0:
            raise ContractError("max_risk_frame_gap_minutes 必须为正数")
        if not 0 <= self.minimum_confidence <= 1:
            raise ContractError("minimum_confidence 必须位于 [0, 1]")
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

    def __post_init__(self) -> None:
        if self.schema_version != "replanning-config.v1":
            raise ContractError("ReplanningConfig.schema_version 必须是 replanning-config.v1")
        if self.minimum_interval_minutes < 0:
            raise ContractError("minimum_interval_minutes 不能为负")
        for name in ("route_switch_gain_threshold", "risk_trigger_threshold", "hysteresis"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ContractError(f"{name} 必须位于 [0, 1]")
        if not math.isfinite(self.deviation_trigger_km) or self.deviation_trigger_km <= 0:
            raise ContractError("deviation_trigger_km 必须为正有限值")


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError("config_digest 必须是小写 SHA-256")
