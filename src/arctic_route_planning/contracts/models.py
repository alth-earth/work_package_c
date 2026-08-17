"""Immutable contract models at the BC and CD boundaries."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import numpy as np
import xarray as xr
from arctic_route_contracts import ScenarioMode

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

_CONTRACT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_RUN_ID = re.compile(
    r"^run-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


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
        if self.data_id is not None and (
            not isinstance(self.data_id, str) or not self.data_id.strip()
        ):
            raise ContractError("source.data_id 必须是非空字符串或 null")
        if self.issue_time is not None:
            object.__setattr__(
                self, "issue_time", _require_utc(self.issue_time, field="source.issue_time")
            )
        if self.valid_time is not None:
            object.__setattr__(
                self, "valid_time", _require_utc(self.valid_time, field="source.valid_time")
            )
        if self.checksum is not None:
            _validate_digest(self.checksum, field="source.checksum")


@dataclass(frozen=True, slots=True)
class RiskFrame:
    schema_version: str
    risk_id: str
    run_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    generation_id: int
    valid_time: datetime
    as_of_time: datetime
    generated_at: datetime
    model_version: str
    payload: xr.Dataset
    source_summary: tuple[SourceReference, ...]
    provenance: ProvenanceKind

    def __post_init__(self) -> None:
        try:
            provenance = ProvenanceKind(self.provenance)
        except (TypeError, ValueError) as exc:
            raise ContractError("RiskFrame.provenance 不合法") from exc
        object.__setattr__(self, "provenance", provenance)
        if self.schema_version != "bc.risk-frame.v2":
            raise ContractError("RiskFrame.schema_version 必须是 bc.risk-frame.v2")
        for name in ("risk_id", "model_version"):
            if not getattr(self, name).strip():
                raise ContractError(f"{name} 不能为空")
        _validate_run_id(self.run_id)
        for name in ("scenario_id", "corridor_id", "vessel_profile_id"):
            _validate_contract_id(getattr(self, name), field=name)
        _validate_digest(self.config_digest, field="config_digest")
        _validate_digest(self.model_config_digest, field="model_config_digest")
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or self.generation_id < 0
        ):
            raise ContractError("generation_id 必须是非负整数且不能是 bool")
        valid = _require_utc(self.valid_time, field="valid_time")
        as_of = _require_utc(self.as_of_time, field="as_of_time")
        generated = _require_utc(self.generated_at, field="generated_at")
        object.__setattr__(self, "valid_time", valid)
        object.__setattr__(self, "as_of_time", as_of)
        object.__setattr__(self, "generated_at", generated)
        if not self.source_summary:
            raise ContractError("source_summary 不能为空")
        if provenance is ProvenanceKind.FORMAL:
            for source in self.source_summary:
                missing = tuple(
                    name
                    for name in ("data_id", "issue_time", "valid_time", "checksum")
                    if getattr(source, name) is None
                )
                if missing:
                    raise ContractError("正式 RiskFrame 的来源必须携带 " + ", ".join(missing))
                assert source.issue_time is not None
                if source.issue_time > as_of:
                    raise ContractError("正式 RiskFrame 包含 as_of_time 之后才发布的来源")
        payload = _validated_payload(self.payload)
        if provenance is ProvenanceKind.FORMAL and "environment_speed_factor" not in payload:
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
    run_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
    provenance: ProvenanceKind
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
        try:
            provenance = ProvenanceKind(self.provenance)
        except (TypeError, ValueError) as exc:
            raise ContractError("RoutePlan.provenance 不合法") from exc
        object.__setattr__(self, "provenance", provenance)
        if self.schema_version != "cd.route-plan.v2":
            raise ContractError("RoutePlan.schema_version 必须是 cd.route-plan.v2")
        for name in (
            "run_id",
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
        _validate_digest(self.model_config_digest, field="model_config_digest")
        _validate_digest(self.planner_config_digest, field="planner_config_digest")
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
    run_id: str
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
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
        if not self.run_id.strip():
            raise ContractError("run_id 不能为空")
        _validate_digest(self.config_digest, field="config_digest")
        _validate_digest(self.model_config_digest, field="model_config_digest")
        _validate_digest(self.planner_config_digest, field="planner_config_digest")
        if self.generation_id < 0 or self.input_revision < 0:
            raise ContractError("generation_id 和 input_revision 不能为负")
        if not self.planning_request_id.strip():
            raise ContractError("planning_request_id 不能为空")
        as_of = ensure_utc(self.as_of_time, field="as_of_time")
        start_time = ensure_utc(self.start_time, field="start_time")
        if self.scenario.mode is ScenarioMode.FROZEN_FORECAST and as_of > start_time:
            raise ContractError("frozen_forecast 的 as_of_time 不能晚于 start_time")
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
    supported = {*required, "environment_speed_factor", "hard_reason"}
    extra = sorted(set(payload.data_vars) - supported)
    if extra:
        raise ContractError(f"RiskFrame.payload 含 v2 未声明变量: {', '.join(extra)}")
    if "latitude" not in payload.coords or "longitude" not in payload.coords:
        raise ContractError("RiskFrame.payload 必须使用 latitude/longitude 坐标")
    extra_coordinates = sorted(set(payload.coords) - {"latitude", "longitude"})
    if extra_coordinates:
        raise ContractError(
            "RiskFrame.payload 含 v2 未声明坐标: " + ", ".join(extra_coordinates)
        )
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
    risk = np.asarray(payload["risk_score"].values, dtype=np.float64)
    level = np.asarray(payload["risk_level"].values)
    hard = np.asarray(payload["hard_mask"].values)
    confidence = np.asarray(payload["confidence"].values, dtype=np.float64)
    if hard.dtype != np.bool_:
        raise ContractError("hard_mask 必须是 bool")
    if not np.issubdtype(level.dtype, np.integer):
        raise ContractError("risk_level 必须是整数")
    if np.any(~np.isfinite(confidence)) or np.any((confidence < 0) | (confidence > 1)):
        raise ContractError("confidence 必须是 [0, 1] 内有限值")
    invalid_risk = ~np.isfinite(risk)
    if np.any(invalid_risk & ~hard & (confidence > 0)):
        raise ContractError("未知风险必须通过 hard_mask 或 confidence=0 显式表达")
    finite_risk = risk[np.isfinite(risk)]
    if finite_risk.size and np.any((finite_risk < 0) | (finite_risk > 1)):
        raise ContractError("risk_score 必须位于 [0, 1]")
    expected_level = np.full(risk.shape, 5, dtype=np.int64)
    finite = np.isfinite(risk)
    expected_level[finite] = np.minimum(
        5,
        np.floor(risk[finite] * 5.0).astype(np.int64) + 1,
    )
    if np.any(level != expected_level):
        raise ContractError(
            "risk_level 必须按 min(5, floor(risk_score*5)+1) 派生；未知风险必须为 5"
        )
    if payload.attrs.get("crs") != "EPSG:4326":
        raise ContractError("RiskFrame.payload.attrs['crs'] 必须是 EPSG:4326")
    if "environment_speed_factor" in payload:
        factor = payload["environment_speed_factor"]
        if factor.dims != expected_dims or factor.shape != expected_shape:
            raise ContractError("environment_speed_factor 必须是 latitude×longitude 二维网格")
        factor_values = np.asarray(factor.values, dtype=np.float64)
        if np.any(~np.isfinite(factor_values)) or np.any(
            (factor_values <= 0) | (factor_values > 1)
        ):
            raise ContractError("environment_speed_factor 必须位于 (0, 1]")
    if "hard_reason" in payload:
        reason = payload["hard_reason"]
        if reason.dims != expected_dims or reason.shape != expected_shape:
            raise ContractError("hard_reason 必须是 latitude×longitude 二维网格")
        reason_values = np.asarray(reason.values)
        if reason_values.dtype.kind not in {"U", "S"}:
            raise ContractError("hard_reason 必须是字符串网格")
        allowed = {"NONE", "LAND", "DATA_UNAVAILABLE", "OTHER"}
        unknown = sorted(set(reason_values.ravel()) - allowed)
        if unknown:
            raise ContractError(f"hard_reason 含未声明取值: {unknown}")
        if np.any(~hard & (reason_values != "NONE")):
            raise ContractError("hard_reason 非 NONE 的单元格必须 hard_mask=true")
        if np.any(hard & (reason_values == "NONE")):
            raise ContractError("hard_mask=true 的单元格必须给出非 NONE 的 hard_reason")
    frozen = payload.copy(deep=True)
    for variable in (*frozen.data_vars, *frozen.coords):
        values = frozen[variable].data
        if isinstance(values, np.ndarray):
            values.flags.writeable = False
    return frozen


def _validate_digest(value: str, *, field: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ContractError(f"{field} 必须是小写 SHA-256")


def _validate_contract_id(value: str, *, field: str) -> None:
    if not isinstance(value, str) or _CONTRACT_ID.fullmatch(value) is None:
        raise ContractError(f"{field} 必须匹配 [a-z0-9][a-z0-9_-]{{0,127}}")


def _validate_run_id(value: str) -> None:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise ContractError("run_id 必须使用 run-<UUID> 形式")
    try:
        UUID(value.removeprefix("run-"))
    except ValueError as exc:
        raise ContractError("run_id 必须使用 run-<UUID> 形式") from exc


def _require_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{field} 必须携带 UTC 时区")
    if value.utcoffset().total_seconds() != 0:
        raise ContractError(f"{field} 必须使用 UTC，不能隐式转换其他时区")
    return ensure_utc(value, field=field)
