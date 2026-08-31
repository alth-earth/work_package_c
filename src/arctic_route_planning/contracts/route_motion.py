"""Formal C -> D route-motion contract for engineering simulation.

The contract is a sibling of ``RoutePlanV3``.  It never changes authoritative
waypoints, ETA, metrics, or plan identities.  A consumer may use a qualified
``CURVE`` record for presentation motion and must otherwise use the published
raw route/timeline.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from arctic_route_planning.contracts.layered import PlanLayer
from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import ensure_utc

ROUTE_MOTION_SET_SCHEMA_VERSION = "cd.route-motion-set.v1"
ROUTE_MOTION_PROFILE_SCHEMA_VERSION = "c.route-motion-vessel-profile.v1"
ROUTE_MOTION_INTERPOLATION = "linear_time_between_producer_motion_samples"
CONTINUOUS_RASTER_MODEL_SCOPE = "CONTINUOUS_IN_DECLARED_RASTER_MODEL"

_MOTION_SET_ID = re.compile(r"^route-motion-set-sha256-[0-9a-f]{64}$")
_PLAN_ID = re.compile(r"^route-v3-sha256-[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class RouteMotionMode(StrEnum):
    CURVE = "CURVE"
    RAW_PASSTHROUGH = "RAW_PASSTHROUGH"


def _finite(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} 必须是有限数值")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{field} 必须是有限数值")
    return result


def _digest(value: str, field: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContractError(f"{field} 必须是小写 SHA-256")


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class MotionSample:
    longitude: float
    latitude: float
    eta: datetime
    course_degrees: float
    speed_knots: float

    def __post_init__(self) -> None:
        longitude = _finite(self.longitude, "MotionSample.longitude")
        latitude = _finite(self.latitude, "MotionSample.latitude")
        course = _finite(self.course_degrees, "MotionSample.course_degrees")
        speed = _finite(self.speed_knots, "MotionSample.speed_knots")
        if not -180.0 <= longitude <= 180.0 or not -90.0 <= latitude <= 90.0:
            raise ContractError("MotionSample 坐标越界")
        if not 0.0 <= course < 360.0 or speed < 0.0:
            raise ContractError("MotionSample 航向或速度不合法")
        object.__setattr__(self, "longitude", longitude)
        object.__setattr__(self, "latitude", latitude)
        object.__setattr__(self, "course_degrees", course)
        object.__setattr__(self, "speed_knots", speed)
        object.__setattr__(self, "eta", ensure_utc(self.eta, field="MotionSample.eta"))


@dataclass(frozen=True, slots=True)
class WaypointMotionAnchor:
    waypoint_index: int
    eta: datetime
    motion_sample_index: int
    arc_length_m: float

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.waypoint_index, self.motion_sample_index)
        ):
            raise ContractError("WaypointMotionAnchor 索引必须是非负整数")
        arc_length = _finite(self.arc_length_m, "WaypointMotionAnchor.arc_length_m")
        if arc_length < 0.0:
            raise ContractError("WaypointMotionAnchor.arc_length_m 不能为负")
        object.__setattr__(self, "arc_length_m", arc_length)
        object.__setattr__(
            self, "eta", ensure_utc(self.eta, field="WaypointMotionAnchor.eta")
        )


@dataclass(frozen=True, slots=True)
class RouteMotionQualification:
    result: str
    risk_rechecked: bool
    hard_mask_rechecked: bool
    coverage_complete: bool
    eta_anchors_preserved: bool
    speed_checked: bool
    curvature_checked: bool
    corridor_checked: bool
    manoeuvring_checked: bool
    corridor_proof_scope: str
    evidence_kind: str
    real_vessel_calibrated: bool
    details_digest: str

    def __post_init__(self) -> None:
        if self.result not in {"QUALIFIED_ENGINEERING_REFERENCE", "RAW_FALLBACK"}:
            raise ContractError("RouteMotionQualification.result 不合法")
        for name in (
            "risk_rechecked",
            "hard_mask_rechecked",
            "coverage_complete",
            "eta_anchors_preserved",
            "speed_checked",
            "curvature_checked",
            "corridor_checked",
            "manoeuvring_checked",
            "real_vessel_calibrated",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"RouteMotionQualification.{name} 必须是 bool")
        _digest(self.details_digest, "RouteMotionQualification.details_digest")
        if self.real_vessel_calibrated:
            raise ContractError("工程参考船模不得标记为实船校准")

    @property
    def curve_qualified(self) -> bool:
        return (
            self.result == "QUALIFIED_ENGINEERING_REFERENCE"
            and all(
                (
                    self.risk_rechecked,
                    self.hard_mask_rechecked,
                    self.coverage_complete,
                    self.eta_anchors_preserved,
                    self.speed_checked,
                    self.curvature_checked,
                    self.corridor_checked,
                    self.manoeuvring_checked,
                )
            )
            and self.corridor_proof_scope == CONTINUOUS_RASTER_MODEL_SCOPE
            and self.evidence_kind == "FORMULA_DERIVED_ENGINEERING_REFERENCE"
            and not self.real_vessel_calibrated
        )


@dataclass(frozen=True, slots=True)
class RouteMotionRecord:
    planning_layer: PlanLayer
    plan_id: str
    raw_route_digest: str
    mode: RouteMotionMode
    fallback_reason: str | None
    curve_digest: str
    motion_digest: str
    interpolation: str
    waypoint_anchors: tuple[WaypointMotionAnchor, ...]
    motion_samples: tuple[MotionSample, ...]
    qualification: RouteMotionQualification

    def __post_init__(self) -> None:
        object.__setattr__(self, "planning_layer", PlanLayer(self.planning_layer))
        object.__setattr__(self, "mode", RouteMotionMode(self.mode))
        if _PLAN_ID.fullmatch(self.plan_id) is None:
            raise ContractError("RouteMotionRecord.plan_id 不是 RoutePlanV3 身份")
        for name in ("raw_route_digest", "curve_digest", "motion_digest"):
            _digest(getattr(self, name), f"RouteMotionRecord.{name}")
        if self.interpolation != ROUTE_MOTION_INTERPOLATION:
            raise ContractError("RouteMotionRecord.interpolation 不合法")
        if len(self.motion_samples) < 2 or len(self.waypoint_anchors) < 2:
            raise ContractError("RouteMotionRecord 至少需要两个样本和航点锚")
        if any(
            current.eta <= previous.eta
            for previous, current in zip(
                self.motion_samples, self.motion_samples[1:], strict=False
            )
        ):
            raise ContractError("RouteMotionRecord.motion_samples ETA 必须严格递增")
        if tuple(anchor.waypoint_index for anchor in self.waypoint_anchors) != tuple(
            range(len(self.waypoint_anchors))
        ):
            raise ContractError("RouteMotionRecord waypoint anchors 必须完整有序")
        if any(
            anchor.motion_sample_index >= len(self.motion_samples)
            for anchor in self.waypoint_anchors
        ):
            raise ContractError("RouteMotionRecord waypoint anchor 越界")
        if any(
            current.motion_sample_index <= previous.motion_sample_index
            for previous, current in zip(
                self.waypoint_anchors, self.waypoint_anchors[1:], strict=False
            )
        ):
            raise ContractError("RouteMotionRecord waypoint anchors 必须严格前进")
        if any(
            anchor.eta != self.motion_samples[anchor.motion_sample_index].eta
            for anchor in self.waypoint_anchors
        ):
            raise ContractError("RouteMotionRecord 航点 ETA 未绑定 motion sample")
        curve_payload = [
            [sample.longitude, sample.latitude] for sample in self.motion_samples
        ]
        motion_payload = [
            {
                "lon": sample.longitude,
                "lat": sample.latitude,
                "eta": _iso(sample.eta),
                "course_degrees": sample.course_degrees,
                "speed_knots": sample.speed_knots,
            }
            for sample in self.motion_samples
        ]
        if self.curve_digest != _canonical_digest(curve_payload):
            raise ContractError("RouteMotionRecord.curve_digest 与样本几何不一致")
        if self.motion_digest != _canonical_digest(motion_payload):
            raise ContractError("RouteMotionRecord.motion_digest 与运动样本不一致")
        if self.mode is RouteMotionMode.CURVE:
            if self.fallback_reason is not None or not self.qualification.curve_qualified:
                raise ContractError("CURVE record 必须完整通过工程资格")
        elif not self.fallback_reason or self.qualification.result != "RAW_FALLBACK":
            raise ContractError("RAW_PASSTHROUGH 必须给出明确 fallback reason")


@dataclass(frozen=True, slots=True)
class RouteMotionSet:
    schema_version: str
    motion_set_id: str
    layer_set_id: str
    run_id: str
    scenario_id: str
    corridor_id: str
    generation_id: int
    input_revision: int
    risk_window_id: str
    risk_window_digest: str
    vessel_profile_id: str
    vessel_profile_version: str
    vessel_profile_digest: str
    motion_profile_id: str
    motion_profile_digest: str
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
    producer_digest: str
    generated_at: datetime
    records: tuple[RouteMotionRecord, ...]

    def __post_init__(self) -> None:
        if self.schema_version != ROUTE_MOTION_SET_SCHEMA_VERSION:
            raise ContractError("RouteMotionSet.schema_version 不合法")
        if _MOTION_SET_ID.fullmatch(self.motion_set_id) is None:
            raise ContractError("RouteMotionSet.motion_set_id 不是规范身份")
        for name in (
            "layer_set_id",
            "run_id",
            "scenario_id",
            "corridor_id",
            "risk_window_id",
            "vessel_profile_id",
            "vessel_profile_version",
            "motion_profile_id",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ContractError(f"RouteMotionSet.{name} 不能为空")
        for name in (
            "risk_window_digest",
            "vessel_profile_digest",
            "motion_profile_digest",
            "config_digest",
            "model_config_digest",
            "planner_config_digest",
            "producer_digest",
        ):
            _digest(getattr(self, name), f"RouteMotionSet.{name}")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.generation_id, self.input_revision)
        ):
            raise ContractError("RouteMotionSet generation/input revision 不合法")
        object.__setattr__(
            self, "generated_at", ensure_utc(self.generated_at, field="generated_at")
        )
        expected_layers = tuple(PlanLayer)
        if tuple(record.planning_layer for record in self.records) != expected_layers:
            raise ContractError("RouteMotionSet 必须按固定顺序包含四层 recommended record")
        if len({record.plan_id for record in self.records}) != 4:
            raise ContractError("RouteMotionSet 的四个 plan_id 必须唯一")


__all__ = [
    "CONTINUOUS_RASTER_MODEL_SCOPE",
    "ROUTE_MOTION_INTERPOLATION",
    "ROUTE_MOTION_PROFILE_SCHEMA_VERSION",
    "ROUTE_MOTION_SET_SCHEMA_VERSION",
    "MotionSample",
    "RouteMotionMode",
    "RouteMotionQualification",
    "RouteMotionRecord",
    "RouteMotionSet",
    "WaypointMotionAnchor",
]
