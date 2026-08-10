"""Immutable contract models at the BC and CD boundaries."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import numpy as np
import xarray as xr

from arctic_route_planning.domain.models import (
    GeoPoint,
    ObjectiveMode,
    PlanKind,
    ReplanReason,
    ScenarioDefinition,
    VesselProfile,
)
from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import ensure_utc


class ProvenanceKind(StrEnum):
    FORMAL = "formal"
    LEGACY_UNVERIFIED = "legacy_unverified"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True, slots=True)
class GridDefinition:
    """Stable identity for one rectilinear RiskFrame grid."""

    grid_id: str
    coordinate_digest: str
    crs: str
    shape: tuple[int, int]
    y_dim: str = "latitude"
    x_dim: str = "longitude"


@dataclass(frozen=True, slots=True)
class SourceReference:
    source_id: str
    data_id: str | None
    issue_time: datetime | None
    valid_time: datetime | None
    version: str
    quality_flag: str
    checksum: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip() or not self.version.strip() or not self.quality_flag.strip():
            raise ContractError("来源 ID、版本和质量标记不能为空")
        if self.issue_time is not None:
            object.__setattr__(
                self, "issue_time", ensure_utc(self.issue_time, field="source.issue_time")
            )
        if self.valid_time is not None:
            object.__setattr__(
                self, "valid_time", ensure_utc(self.valid_time, field="source.valid_time")
            )
        if self.checksum is not None:
            _validate_digest(self.checksum, field="source.checksum")


@dataclass(frozen=True, slots=True)
class RiskFrame:
    schema_version: str
    risk_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    generation_id: int
    valid_time: datetime
    as_of_time: datetime
    generated_at: datetime
    model_version: str
    payload: xr.Dataset
    source_summary: tuple[SourceReference, ...]
    provenance: ProvenanceKind

    def __post_init__(self) -> None:
        if self.schema_version != "bc.risk-frame.v1":
            raise ContractError("RiskFrame.schema_version 必须是 bc.risk-frame.v1")
        for name in (
            "risk_id",
            "scenario_id",
            "corridor_id",
            "vessel_profile_id",
            "model_version",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"{name} 不能为空")
        _validate_digest(self.config_digest, field="config_digest")
        if self.generation_id < 0:
            raise ContractError("generation_id 不能为负")
        valid = ensure_utc(self.valid_time, field="valid_time")
        as_of = ensure_utc(self.as_of_time, field="as_of_time")
        generated = ensure_utc(self.generated_at, field="generated_at")
        object.__setattr__(self, "valid_time", valid)
        object.__setattr__(self, "as_of_time", as_of)
        object.__setattr__(self, "generated_at", generated)
        if not self.source_summary:
            raise ContractError("source_summary 不能为空")
        if self.provenance is ProvenanceKind.FORMAL:
            for source in self.source_summary:
                if source.issue_time is None:
                    raise ContractError("正式 RiskFrame 的来源必须携带 issue_time")
                if source.issue_time > as_of:
                    raise ContractError("正式 RiskFrame 包含 as_of_time 之后才发布的来源")
        payload = _validated_payload(self.payload)
        if self.provenance is ProvenanceKind.FORMAL and "environment_speed_factor" not in payload:
            raise ContractError("正式 RiskFrame 必须携带 environment_speed_factor")
        object.__setattr__(self, "payload", payload)

    @property
    def grid(self) -> GridDefinition:
        latitude = np.asarray(self.payload["latitude"].values, dtype="<f8")
        longitude = np.asarray(self.payload["longitude"].values, dtype="<f8")
        hasher = hashlib.sha256()
        hasher.update(latitude.tobytes(order="C"))
        hasher.update(longitude.tobytes(order="C"))
        coordinate_digest = hasher.hexdigest()
        return GridDefinition(
            grid_id=str(self.payload.attrs.get("grid_id", f"epsg4326-{coordinate_digest[:16]}")),
            coordinate_digest=coordinate_digest,
            crs=str(self.payload.attrs["crs"]),
            shape=(latitude.size, longitude.size),
        )


@dataclass(frozen=True, slots=True)
class RiskSample:
    risk_score: float
    risk_level: int
    hard_mask: bool
    confidence: float
    source_risk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.risk_score) or not 0 <= self.risk_score <= 1:
            raise ContractError("RiskSample.risk_score 必须位于 [0, 1]")
        if not 1 <= self.risk_level <= 5:
            raise ContractError("RiskSample.risk_level 必须位于 [1, 5]")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ContractError("RiskSample.confidence 必须位于 [0, 1]")
        if not self.source_risk_ids:
            raise ContractError("RiskSample.source_risk_ids 不能为空")


@dataclass(frozen=True, slots=True)
class Waypoint:
    longitude: float
    latitude: float
    eta: datetime
    recommended_speed_mps: float

    def __post_init__(self) -> None:
        GeoPoint(self.longitude, self.latitude)
        object.__setattr__(self, "eta", ensure_utc(self.eta, field="waypoint.eta"))
        if not math.isfinite(self.recommended_speed_mps) or self.recommended_speed_mps <= 0:
            raise ContractError("recommended_speed_mps 必须是正有限值")


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    distance_km: float
    eta_hours: float
    avg_risk: float
    max_risk: float
    integrated_risk_hours: float
    minimum_confidence: float
    hard_constraint_violations: int
    turn_count: int
    expanded_nodes: int
    compute_ms: float
    objective_cost: float

    def __post_init__(self) -> None:
        finite_nonnegative = (
            self.distance_km,
            self.eta_hours,
            self.avg_risk,
            self.max_risk,
            self.integrated_risk_hours,
            self.minimum_confidence,
            self.compute_ms,
            self.objective_cost,
        )
        if any(not math.isfinite(value) or value < 0 for value in finite_nonnegative):
            raise ContractError("路线指标必须是非负有限值")
        if self.avg_risk > 1 or self.max_risk > 1 or self.minimum_confidence > 1:
            raise ContractError("风险和置信度指标必须位于 [0, 1]")
        if self.avg_risk > self.max_risk:
            raise ContractError("avg_risk 不能大于 max_risk")
        if self.hard_constraint_violations != 0:
            raise ContractError("发布路线不得包含硬约束违规")
        if self.turn_count < 0 or self.expanded_nodes < 0:
            raise ContractError("计数指标不能为负")


@dataclass(frozen=True, slots=True)
class RoutePlan:
    schema_version: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    generation_id: int
    plan_id: str
    plan_version: str
    planning_request_id: str
    input_revision: int
    generated_at: datetime
    as_of_time: datetime
    start_time: datetime
    objective_mode: ObjectiveMode
    plan_kind: PlanKind
    waypoints: tuple[Waypoint, ...]
    metrics: RouteMetrics
    replan_reasons: tuple[ReplanReason, ...]
    source_risk_ids: tuple[str, ...]
    planner_version: str
    destination_reached: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != "cd.route-plan.v1":
            raise ContractError("RoutePlan.schema_version 必须是 cd.route-plan.v1")
        for name in (
            "scenario_id",
            "corridor_id",
            "vessel_profile_id",
            "plan_id",
            "plan_version",
            "planning_request_id",
            "planner_version",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"{name} 不能为空")
        _validate_digest(self.config_digest, field="config_digest")
        if self.generation_id < 0 or self.input_revision < 0:
            raise ContractError("generation_id 和 input_revision 不能为负")
        for name in ("generated_at", "as_of_time", "start_time"):
            object.__setattr__(self, name, ensure_utc(getattr(self, name), field=name))
        if len(self.waypoints) < 2:
            raise ContractError("RoutePlan 至少需要起点和终点两个航点")
        if self.waypoints[0].eta != self.start_time:
            raise ContractError("首航点 ETA 必须等于 start_time")
        if any(a.eta >= b.eta for a, b in zip(self.waypoints, self.waypoints[1:], strict=False)):
            raise ContractError("航点 ETA 必须严格递增")
        actual_eta = (self.waypoints[-1].eta - self.start_time).total_seconds() / 3600
        tolerance = max(1 / 3600, max(actual_eta, self.metrics.eta_hours) * 1e-6)
        if abs(actual_eta - self.metrics.eta_hours) > tolerance:
            raise ContractError("RouteMetrics.eta_hours 与航点 ETA 不一致")
        if not self.source_risk_ids:
            raise ContractError("source_risk_ids 不能为空")


@dataclass(frozen=True, slots=True)
class PlanRequest:
    scenario: ScenarioDefinition
    vessel: VesselProfile
    config_digest: str
    generation_id: int
    planning_request_id: str
    input_revision: int
    as_of_time: datetime
    start_time: datetime
    start: GeoPoint
    destination: GeoPoint
    objective_mode: ObjectiveMode
    plan_kind: PlanKind = PlanKind.INITIAL
    replan_reasons: tuple[ReplanReason, ...] = ()

    def __post_init__(self) -> None:
        _validate_digest(self.config_digest, field="config_digest")
        if self.generation_id < 0 or self.input_revision < 0:
            raise ContractError("generation_id 和 input_revision 不能为负")
        if not self.planning_request_id.strip():
            raise ContractError("planning_request_id 不能为空")
        as_of = ensure_utc(self.as_of_time, field="as_of_time")
        start_time = ensure_utc(self.start_time, field="start_time")
        if as_of > start_time:
            raise ContractError("as_of_time 不能晚于 start_time")
        object.__setattr__(self, "as_of_time", as_of)
        object.__setattr__(self, "start_time", start_time)
        if self.start == self.destination:
            raise ContractError("起点和终点不能相同")


def _validated_payload(payload: xr.Dataset) -> xr.Dataset:
    if not isinstance(payload, xr.Dataset):
        raise ContractError("RiskFrame.payload 必须是 xarray.Dataset")
    required = ("risk_score", "risk_level", "hard_mask", "confidence")
    missing = [name for name in required if name not in payload.data_vars]
    if missing:
        raise ContractError(f"RiskFrame.payload 缺少变量: {', '.join(missing)}")
    if "latitude" not in payload.coords or "longitude" not in payload.coords:
        raise ContractError("RiskFrame.payload 必须使用 latitude/longitude 坐标")
    latitude = np.asarray(payload["latitude"].values)
    longitude = np.asarray(payload["longitude"].values)
    if latitude.ndim != 1 or longitude.ndim != 1 or latitude.size < 2 or longitude.size < 2:
        raise ContractError("latitude/longitude 必须是一维且至少包含两个点")
    if not np.all(np.isfinite(latitude)) or not np.all(np.isfinite(longitude)):
        raise ContractError("latitude/longitude 不得含非有限值")
    if np.any((latitude < -90) | (latitude > 90)) or np.any((longitude < -180) | (longitude > 180)):
        raise ContractError("latitude/longitude 必须位于 EPSG:4326 合法范围")
    if not np.all(np.diff(latitude) > 0) or not np.all(np.diff(longitude) > 0):
        raise ContractError("latitude/longitude 必须严格递增")
    expected_dims = ("latitude", "longitude")
    expected_shape = (latitude.size, longitude.size)
    for name in required:
        if payload[name].dims != expected_dims or payload[name].shape != expected_shape:
            raise ContractError(f"{name} 必须是 latitude×longitude 二维网格")
    risk = np.asarray(payload["risk_score"].values)
    level = np.asarray(payload["risk_level"].values)
    hard = np.asarray(payload["hard_mask"].values)
    confidence = np.asarray(payload["confidence"].values)
    if hard.dtype != np.bool_:
        raise ContractError("hard_mask 必须是 bool")
    if not np.issubdtype(level.dtype, np.integer):
        raise ContractError("risk_level 必须是整数")
    if np.any((level < 1) | (level > 5)):
        raise ContractError("risk_level 必须位于 [1, 5]")
    if np.any(~np.isfinite(confidence)) or np.any((confidence < 0) | (confidence > 1)):
        raise ContractError("confidence 必须是 [0, 1] 内有限值")
    invalid_risk = ~np.isfinite(risk)
    if np.any(invalid_risk & ~hard & (confidence > 0)):
        raise ContractError("未知风险必须通过 hard_mask 或 confidence=0 显式表达")
    finite_risk = risk[np.isfinite(risk)]
    if finite_risk.size and np.any((finite_risk < 0) | (finite_risk > 1)):
        raise ContractError("risk_score 必须位于 [0, 1]")
    if payload.attrs.get("crs") != "EPSG:4326":
        raise ContractError("RiskFrame.payload.attrs['crs'] 必须是 EPSG:4326")
    if "environment_speed_factor" in payload:
        factor = payload["environment_speed_factor"]
        if factor.dims != expected_dims or factor.shape != expected_shape:
            raise ContractError("environment_speed_factor 必须是 latitude×longitude 二维网格")
        factor_values = np.asarray(factor.values)
        if np.any(~np.isfinite(factor_values)) or np.any(
            (factor_values <= 0) | (factor_values > 1)
        ):
            raise ContractError("environment_speed_factor 必须位于 (0, 1]")
    frozen = payload.copy(deep=True)
    for variable in (*frozen.data_vars, *frozen.coords):
        values = frozen[variable].data
        if isinstance(values, np.ndarray):
            values.flags.writeable = False
    return frozen


def _validate_digest(value: str, *, field: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError(f"{field} 必须是小写 SHA-256")
