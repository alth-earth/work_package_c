"""Immutable C -> D v3 contracts for atomic four-layer route publication."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RouteMetrics,
    RoutePlan,
    Waypoint,
)
from arctic_route_planning.domain.models import ObjectiveMode, PlanKind, ReplanReason
from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import ensure_utc

_ROUTE_V3_ID = re.compile(r"^route-v3-sha256-[0-9a-f]{64}$")
_LAYER_SET_ID = re.compile(r"^layer-set-sha256-[0-9a-f]{64}$")

ROUTE_PLAN_V3_SCHEMA_VERSION = "cd.route-plan.v3"
FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION = "cd.four-layer-route-plan-set.v3"

# Formal layer focus/cutoff windows in hours, counted from the request start
# time.  These are v3 contract architecture values shared by the four-layer
# orchestration and this module's semantic validation -- not planner tuning
# knobs, so they must never drift between the two consumers.
MAIN_CORRIDOR_START_OFFSET_HOURS = 24
MAIN_CORRIDOR_HOURS = 72
ROLLING_HOURS = 24
EXECUTABLE_HOURS = 6


class PlanLayer(StrEnum):
    FULL_VOYAGE = "full_voyage"
    MAIN_CORRIDOR = "main_corridor_24_72h"
    ROLLING = "rolling_0_24h"
    EXECUTABLE = "executable_0_6h"


@dataclass(frozen=True, slots=True)
class RoutePlanV3:
    """One objective route in one layer of an atomic v3 plan set."""

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
    planning_layer: PlanLayer
    layer_set_id: str
    focus_start_time: datetime
    focus_end_time: datetime
    reference_plan_id: str | None
    layer_goal_reached: bool
    destination_reached: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != ROUTE_PLAN_V3_SCHEMA_VERSION:
            raise ContractError(
                f"RoutePlanV3.schema_version 必须是 {ROUTE_PLAN_V3_SCHEMA_VERSION}"
            )
        try:
            layer = PlanLayer(self.planning_layer)
        except (TypeError, ValueError) as exc:
            raise ContractError("RoutePlanV3.planning_layer 不合法") from exc
        object.__setattr__(self, "planning_layer", layer)
        if _ROUTE_V3_ID.fullmatch(self.plan_id) is None:
            raise ContractError("RoutePlanV3.plan_id 必须是规范内容身份")
        if _LAYER_SET_ID.fullmatch(self.layer_set_id) is None:
            raise ContractError("RoutePlanV3.layer_set_id 必须是规范整组身份")
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or self.generation_id < 0
            or isinstance(self.input_revision, bool)
            or not isinstance(self.input_revision, int)
            or self.input_revision < 0
        ):
            raise ContractError("generation_id/input_revision 必须是非负整数且不能是 bool")
        if not isinstance(self.destination_reached, bool):
            raise ContractError("destination_reached 必须是 bool")
        if len(set(self.replan_reasons)) != len(self.replan_reasons):
            raise ContractError("replan_reasons 不得重复")
        focus_start = ensure_utc(self.focus_start_time, field="focus_start_time")
        focus_end = ensure_utc(self.focus_end_time, field="focus_end_time")
        object.__setattr__(self, "focus_start_time", focus_start)
        object.__setattr__(self, "focus_end_time", focus_end)
        if focus_end < focus_start:
            raise ContractError("关注时间窗结束不能早于开始")
        if focus_start < ensure_utc(self.start_time, field="start_time"):
            raise ContractError("关注时间窗不能早于路线 start_time")
        if layer is PlanLayer.FULL_VOYAGE:
            if self.reference_plan_id is not None:
                raise ContractError("全航程层不得引用另一计划")
            if not self.destination_reached:
                raise ContractError("全航程层必须到达业务终点")
        elif not self.reference_plan_id:
            raise ContractError("下层路线必须引用全航程推荐计划")
        elif _ROUTE_V3_ID.fullmatch(self.reference_plan_id) is None:
            raise ContractError("下层路线 reference_plan_id 必须是规范路线身份")
        if not isinstance(self.layer_goal_reached, bool):
            raise ContractError("layer_goal_reached 必须是 bool")

        # Reuse the frozen v2 semantic validator while keeping v2 parsing and
        # publication separate from v3 transport.
        RoutePlan(
            schema_version="cd.route-plan.v2",
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            corridor_id=self.corridor_id,
            vessel_profile_id=self.vessel_profile_id,
            config_digest=self.config_digest,
            model_config_digest=self.model_config_digest,
            planner_config_digest=self.planner_config_digest,
            provenance=self.provenance,
            generation_id=self.generation_id,
            plan_id=self.plan_id,
            plan_version=self.plan_version,
            planning_request_id=self.planning_request_id,
            input_revision=self.input_revision,
            generated_at=self.generated_at,
            as_of_time=self.as_of_time,
            start_time=self.start_time,
            objective_mode=self.objective_mode,
            plan_kind=self.plan_kind,
            waypoints=self.waypoints,
            metrics=self.metrics,
            replan_reasons=self.replan_reasons,
            source_risk_ids=self.source_risk_ids,
            planner_version=self.planner_version,
            destination_reached=self.destination_reached,
        )


@dataclass(frozen=True, slots=True)
class LayerRouteBundle:
    """The three objective routes for one planning layer."""

    planning_layer: PlanLayer
    plans: Mapping[ObjectiveMode, RoutePlanV3]

    def __post_init__(self) -> None:
        layer = PlanLayer(self.planning_layer)
        object.__setattr__(self, "planning_layer", layer)
        normalized = {ObjectiveMode(key): value for key, value in self.plans.items()}
        if set(normalized) != set(ObjectiveMode):
            raise ContractError("每个规划层必须恰好包含三种目标")
        if any(plan.planning_layer is not layer for plan in normalized.values()):
            raise ContractError("LayerRouteBundle 含不同 planning_layer")
        if any(plan.objective_mode is not objective for objective, plan in normalized.items()):
            raise ContractError("LayerRouteBundle 的键与路线 objective_mode 不一致")
        _require_same_plan_identity(tuple(normalized.values()))
        object.__setattr__(self, "plans", MappingProxyType(normalized))

    @property
    def recommended(self) -> RoutePlanV3:
        return self.plans[ObjectiveMode.RECOMMENDED]


@dataclass(frozen=True, slots=True)
class FourLayerRoutePlanSet:
    """Atomically published collection of four layers and twelve routes."""

    schema_version: str
    layer_set_id: str
    run_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
    provenance: ProvenanceKind
    generation_id: int
    planning_request_id: str
    input_revision: int
    generated_at: datetime
    as_of_time: datetime
    start_time: datetime
    plan_kind: PlanKind
    replan_reasons: tuple[ReplanReason, ...]
    layers: tuple[LayerRouteBundle, ...]

    def __post_init__(self) -> None:
        if self.schema_version != FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION:
            raise ContractError("FourLayerRoutePlanSet.schema_version 不合法")
        if _LAYER_SET_ID.fullmatch(self.layer_set_id) is None:
            raise ContractError("FourLayerRoutePlanSet.layer_set_id 必须是规范整组身份")
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or self.generation_id < 0
            or isinstance(self.input_revision, bool)
            or not isinstance(self.input_revision, int)
            or self.input_revision < 0
        ):
            raise ContractError("generation_id/input_revision 必须是非负整数且不能是 bool")
        ordered = tuple(self.layers)
        expected = tuple(PlanLayer)
        if tuple(bundle.planning_layer for bundle in ordered) != expected:
            raise ContractError("四层必须按正式顺序恰好各出现一次")
        plans = tuple(plan for bundle in ordered for plan in bundle.plans.values())
        if len(plans) != 12:
            raise ContractError("FourLayerRoutePlanSet 必须恰好包含十二条路线")
        if any(not plan.layer_goal_reached for plan in plans):
            raise ContractError("正式四层整组不得包含未到达分层目标的路线")
        _require_same_plan_identity(plans)
        for plan in plans:
            for field in (
                "layer_set_id",
                "run_id",
                "scenario_id",
                "corridor_id",
                "vessel_profile_id",
                "config_digest",
                "model_config_digest",
                "planner_config_digest",
                "provenance",
                "generation_id",
                "planning_request_id",
                "input_revision",
                "generated_at",
                "as_of_time",
                "start_time",
                "plan_kind",
                "replan_reasons",
            ):
                if getattr(plan, field) != getattr(self, field):
                    raise ContractError(f"v3 路线与整组 {field} 不一致")
        full_reference = ordered[0].recommended.plan_id
        if any(
            plan.reference_plan_id != full_reference
            for bundle in ordered[1:]
            for plan in bundle.plans.values()
        ):
            raise ContractError("所有下层路线必须引用全航程推荐路线")
        object.__setattr__(
            self,
            "generated_at",
            ensure_utc(self.generated_at, field="generated_at"),
        )
        object.__setattr__(self, "as_of_time", ensure_utc(self.as_of_time, field="as_of_time"))
        object.__setattr__(self, "start_time", ensure_utc(self.start_time, field="start_time"))
        object.__setattr__(self, "layers", ordered)
        _validate_layer_semantics(self)

    def bundle_for(self, layer: PlanLayer) -> LayerRouteBundle:
        selected = PlanLayer(layer)
        return next(bundle for bundle in self.layers if bundle.planning_layer is selected)

    @property
    def recommended(self) -> RoutePlanV3:
        return self.bundle_for(PlanLayer.FULL_VOYAGE).recommended


def _require_same_plan_identity(plans: tuple[RoutePlanV3, ...]) -> None:
    if not plans:
        raise ContractError("路线集合不能为空")
    first = plans[0]
    identity_fields = (
        "layer_set_id",
        "run_id",
        "scenario_id",
        "corridor_id",
        "vessel_profile_id",
        "config_digest",
        "model_config_digest",
        "planner_config_digest",
        "provenance",
        "generation_id",
        "planning_request_id",
        "input_revision",
        "generated_at",
        "as_of_time",
        "start_time",
        "plan_kind",
        "replan_reasons",
    )
    for plan in plans[1:]:
        if any(getattr(plan, field) != getattr(first, field) for field in identity_fields):
            raise ContractError("路线集合运行身份不一致")


def _validate_layer_semantics(plan_set: FourLayerRoutePlanSet) -> None:
    full = plan_set.bundle_for(PlanLayer.FULL_VOYAGE)
    reference = full.recommended
    reference_end = reference.waypoints[-1].eta
    expected_windows = {
        PlanLayer.FULL_VOYAGE: (plan_set.start_time, reference_end),
        PlanLayer.MAIN_CORRIDOR: (
            min(
                plan_set.start_time
                + timedelta(hours=MAIN_CORRIDOR_START_OFFSET_HOURS),
                reference_end,
            ),
            min(
                plan_set.start_time + timedelta(hours=MAIN_CORRIDOR_HOURS),
                reference_end,
            ),
        ),
        PlanLayer.ROLLING: (
            plan_set.start_time,
            min(plan_set.start_time + timedelta(hours=ROLLING_HOURS), reference_end),
        ),
        PlanLayer.EXECUTABLE: (
            plan_set.start_time,
            min(plan_set.start_time + timedelta(hours=EXECUTABLE_HOURS), reference_end),
        ),
    }
    for bundle in plan_set.layers:
        expected_start, expected_end = expected_windows[bundle.planning_layer]
        for plan in bundle.plans.values():
            if (
                plan.focus_start_time != expected_start
                or plan.focus_end_time != expected_end
            ):
                raise ContractError("分层关注时间窗与正式 24/72/6 h 语义不一致")

    for layer, cutoff in (
        (PlanLayer.MAIN_CORRIDOR, timedelta(hours=MAIN_CORRIDOR_HOURS)),
        (PlanLayer.ROLLING, timedelta(hours=ROLLING_HOURS)),
        (PlanLayer.EXECUTABLE, timedelta(hours=EXECUTABLE_HOURS)),
    ):
        anchor = _reference_anchor(reference, plan_set.start_time + cutoff)
        expected_location = (anchor.longitude, anchor.latitude)
        expected_destination_reached = expected_location == (
            reference.waypoints[-1].longitude,
            reference.waypoints[-1].latitude,
        )
        for plan in plan_set.bundle_for(layer).plans.values():
            actual = plan.waypoints[-1]
            if (actual.longitude, actual.latitude) != expected_location:
                raise ContractError("分层路线终点与全航程推荐线锚点不一致")
            if plan.destination_reached != expected_destination_reached:
                raise ContractError("分层路线 destination_reached 与业务终点语义不一致")


def _reference_anchor(reference: RoutePlanV3, cutoff: datetime) -> Waypoint:
    start = reference.waypoints[0]
    distinct = tuple(
        waypoint
        for waypoint in reference.waypoints[1:]
        if (waypoint.longitude, waypoint.latitude)
        != (start.longitude, start.latitude)
    )
    if not distinct:
        raise ContractError("layer_not_materializable: no non-start waypoint")
    if distinct[-1].eta <= cutoff:
        return distinct[-1]
    eligible = tuple(waypoint for waypoint in distinct if waypoint.eta <= cutoff)
    if not eligible:
        raise ContractError(
            "layer_not_materializable: no non-start waypoint at or before cutoff"
        )
    return eligible[-1]


__all__ = [
    "EXECUTABLE_HOURS",
    "FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION",
    "MAIN_CORRIDOR_HOURS",
    "MAIN_CORRIDOR_START_OFFSET_HOURS",
    "ROLLING_HOURS",
    "ROUTE_PLAN_V3_SCHEMA_VERSION",
    "FourLayerRoutePlanSet",
    "LayerRouteBundle",
    "PlanLayer",
    "RoutePlanV3",
]
