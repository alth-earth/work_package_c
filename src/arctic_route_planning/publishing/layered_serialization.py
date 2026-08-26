"""Strict JSON/GeoJSON codecs and semantic identities for CD v3."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from arctic_route_planning.contracts.layered import (
    ROUTE_PLAN_V3_SCHEMA_VERSION,
    FourLayerRoutePlanSet,
    LayerRouteBundle,
    PlanLayer,
    RoutePlanV3,
)
from arctic_route_planning.contracts.models import ProvenanceKind, RoutePlan
from arctic_route_planning.domain.models import ObjectiveMode, PlanKind, ReplanReason

from .models import ROUTE_PLAN_SCHEMA_VERSION
from .serialization import (
    _format_time,
    _parse_time,
    route_plan_from_dict,
    route_plan_to_dict,
)

_ROUTE_FIELDS = frozenset(
    {
        "schema_version",
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
        "plan_id",
        "plan_version",
        "generated_at",
        "as_of_time",
        "start_time",
        "objective_mode",
        "plan_kind",
        "waypoints",
        "metrics",
        "replan_reasons",
        "source_risk_ids",
        "planner_version",
        "destination_reached",
        "planning_layer",
        "layer_set_id",
        "focus_start_time",
        "focus_end_time",
        "reference_plan_id",
        "layer_goal_reached",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "distance_km",
        "eta_hours",
        "avg_risk",
        "max_risk",
        "integrated_risk_hours",
        "minimum_confidence",
        "hard_constraint_violations",
        "turn_count",
        "expanded_nodes",
        "compute_ms",
        "objective_cost",
    }
)
_WAYPOINT_FIELDS = frozenset(
    {"longitude", "latitude", "eta", "recommended_speed_mps"}
)
_SET_FIELDS = frozenset(
    {
        "schema_version",
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
        "layers",
    }
)


def route_plan_v3_to_dict(plan: RoutePlanV3) -> dict[str, Any]:
    document = route_plan_to_dict(_as_v2(plan))
    document.update(
        {
            "schema_version": ROUTE_PLAN_V3_SCHEMA_VERSION,
            "planning_layer": plan.planning_layer.value,
            "layer_set_id": plan.layer_set_id,
            "focus_start_time": _format_time(plan.focus_start_time),
            "focus_end_time": _format_time(plan.focus_end_time),
            "reference_plan_id": plan.reference_plan_id,
            "layer_goal_reached": plan.layer_goal_reached,
        }
    )
    return document


def route_plan_v3_from_dict(value: Mapping[str, Any]) -> RoutePlanV3:
    _require_exact_fields(value, _ROUTE_FIELDS, name="RoutePlanV3")
    if value["schema_version"] != ROUTE_PLAN_V3_SCHEMA_VERSION:
        raise ValueError(f"RoutePlanV3 schema_version must be {ROUTE_PLAN_V3_SCHEMA_VERSION}")
    _require_plain_int(value["generation_id"], name="generation_id")
    _require_plain_int(value["input_revision"], name="input_revision")
    _require_bool(value["destination_reached"], name="destination_reached")
    _require_bool(value["layer_goal_reached"], name="layer_goal_reached")
    for name in (
        "run_id",
        "scenario_id",
        "corridor_id",
        "vessel_profile_id",
        "config_digest",
        "model_config_digest",
        "planner_config_digest",
        "provenance",
        "plan_id",
        "plan_version",
        "planning_request_id",
        "objective_mode",
        "plan_kind",
        "planner_version",
        "planning_layer",
        "layer_set_id",
    ):
        if not isinstance(value[name], str):
            raise ValueError(f"{name} must be a string")
    if value["reference_plan_id"] is not None and not isinstance(
        value["reference_plan_id"], str
    ):
        raise ValueError("reference_plan_id must be a string or null")
    metrics = value["metrics"]
    waypoints = value["waypoints"]
    if not isinstance(metrics, Mapping):
        raise ValueError("metrics must be an object")
    _require_exact_fields(metrics, _METRIC_FIELDS, name="metrics")
    if not isinstance(waypoints, list):
        raise ValueError("waypoints must be an array")
    for index, waypoint in enumerate(waypoints):
        if not isinstance(waypoint, Mapping):
            raise ValueError(f"waypoints[{index}] must be an object")
        _require_exact_fields(waypoint, _WAYPOINT_FIELDS, name=f"waypoints[{index}]")

    v2_document = {key: item for key, item in value.items() if key not in _V3_ONLY_FIELDS}
    v2_document["schema_version"] = ROUTE_PLAN_SCHEMA_VERSION
    base = route_plan_from_dict(v2_document)
    plan = RoutePlanV3(
        **{field: getattr(base, field) for field in _ROUTE_BASE_FIELDS},
        schema_version=ROUTE_PLAN_V3_SCHEMA_VERSION,
        planning_layer=PlanLayer(str(value["planning_layer"])),
        layer_set_id=str(value["layer_set_id"]),
        focus_start_time=_parse_time(value["focus_start_time"], "focus_start_time"),
        focus_end_time=_parse_time(value["focus_end_time"], "focus_end_time"),
        reference_plan_id=(
            None if value["reference_plan_id"] is None else str(value["reference_plan_id"])
        ),
        layer_goal_reached=value["layer_goal_reached"],
    )
    expected_plan_id = f"route-v3-sha256-{route_plan_v3_semantic_digest(plan)}"
    if plan.plan_id != expected_plan_id:
        raise ValueError("RoutePlanV3 plan_id does not match canonical semantic content")
    return plan


def route_plan_v3_to_geojson(plan: RoutePlanV3) -> dict[str, Any]:
    document = route_plan_v3_to_dict(plan)
    properties = {key: value for key, value in document.items() if key != "waypoints"}
    properties["waypoint_etas"] = [item["eta"] for item in document["waypoints"]]
    properties["recommended_speeds_mps"] = [
        item["recommended_speed_mps"] for item in document["waypoints"]
    ]
    return {
        "type": "FeatureCollection",
        "features": [_route_feature(plan, properties), *_waypoint_features(plan)],
    }


def route_plan_v3_from_geojson(value: Mapping[str, Any]) -> RoutePlanV3:
    _require_exact_fields(value, frozenset({"type", "features"}), name="RoutePlanV3 GeoJSON")
    if value["type"] != "FeatureCollection":
        raise ValueError("RoutePlanV3 GeoJSON must be a FeatureCollection")
    features = value["features"]
    if not isinstance(features, list) or len(features) < 3:
        raise ValueError("RoutePlanV3 GeoJSON must contain a route and its waypoints")
    plan = _plan_from_route_feature(features[0])
    _validate_waypoint_features(plan, features[1:])
    return plan


def four_layer_route_plan_set_to_dict(plan_set: FourLayerRoutePlanSet) -> dict[str, Any]:
    return {
        "schema_version": plan_set.schema_version,
        "layer_set_id": plan_set.layer_set_id,
        "run_id": plan_set.run_id,
        "scenario_id": plan_set.scenario_id,
        "corridor_id": plan_set.corridor_id,
        "vessel_profile_id": plan_set.vessel_profile_id,
        "config_digest": plan_set.config_digest,
        "model_config_digest": plan_set.model_config_digest,
        "planner_config_digest": plan_set.planner_config_digest,
        "provenance": plan_set.provenance.value,
        "generation_id": plan_set.generation_id,
        "planning_request_id": plan_set.planning_request_id,
        "input_revision": plan_set.input_revision,
        "generated_at": _format_time(plan_set.generated_at),
        "as_of_time": _format_time(plan_set.as_of_time),
        "start_time": _format_time(plan_set.start_time),
        "plan_kind": plan_set.plan_kind.value,
        "replan_reasons": [reason.value for reason in plan_set.replan_reasons],
        "layers": [
            {
                "planning_layer": bundle.planning_layer.value,
                "plans": {
                    objective.value: route_plan_v3_to_dict(bundle.plans[objective])
                    for objective in ObjectiveMode
                },
            }
            for bundle in plan_set.layers
        ],
    }


def four_layer_route_plan_set_from_dict(value: Mapping[str, Any]) -> FourLayerRoutePlanSet:
    _require_exact_fields(value, _SET_FIELDS, name="FourLayerRoutePlanSet")
    _require_plain_int(value["generation_id"], name="generation_id")
    _require_plain_int(value["input_revision"], name="input_revision")
    for name in (
        "schema_version",
        "layer_set_id",
        "run_id",
        "scenario_id",
        "corridor_id",
        "vessel_profile_id",
        "config_digest",
        "model_config_digest",
        "planner_config_digest",
        "provenance",
        "planning_request_id",
        "plan_kind",
    ):
        if not isinstance(value[name], str):
            raise ValueError(f"{name} must be a string")
    if not isinstance(value["replan_reasons"], list):
        raise ValueError("replan_reasons must be an array")
    raw_layers = value["layers"]
    if not isinstance(raw_layers, list):
        raise ValueError("layers must be an array")
    layers: list[LayerRouteBundle] = []
    for index, raw_layer in enumerate(raw_layers):
        if not isinstance(raw_layer, Mapping):
            raise ValueError(f"layers[{index}] must be an object")
        _require_exact_fields(
            raw_layer,
            frozenset({"planning_layer", "plans"}),
            name=f"layers[{index}]",
        )
        raw_plans = raw_layer["plans"]
        if not isinstance(raw_plans, Mapping) or set(raw_plans) != {
            mode.value for mode in ObjectiveMode
        }:
            raise ValueError("each layer plans object must contain exactly three objectives")
        layers.append(
            LayerRouteBundle(
                planning_layer=PlanLayer(str(raw_layer["planning_layer"])),
                plans={
                    ObjectiveMode(name): route_plan_v3_from_dict(document)
                    for name, document in raw_plans.items()
                },
            )
        )
    first = layers[0].recommended if layers else None
    if first is None:
        raise ValueError("layers must not be empty")
    plan_set = FourLayerRoutePlanSet(
        schema_version=str(value["schema_version"]),
        layer_set_id=str(value["layer_set_id"]),
        run_id=str(value["run_id"]),
        scenario_id=str(value["scenario_id"]),
        corridor_id=str(value["corridor_id"]),
        vessel_profile_id=str(value["vessel_profile_id"]),
        config_digest=str(value["config_digest"]),
        model_config_digest=str(value["model_config_digest"]),
        planner_config_digest=str(value["planner_config_digest"]),
        provenance=ProvenanceKind(str(value["provenance"])),
        generation_id=value["generation_id"],
        planning_request_id=str(value["planning_request_id"]),
        input_revision=value["input_revision"],
        generated_at=_parse_time(value["generated_at"], "generated_at"),
        as_of_time=_parse_time(value["as_of_time"], "as_of_time"),
        start_time=_parse_time(value["start_time"], "start_time"),
        plan_kind=PlanKind(str(value["plan_kind"])),
        replan_reasons=tuple(ReplanReason(str(item)) for item in value["replan_reasons"]),
        layers=tuple(layers),
    )
    expected_layer_set_id = (
        "layer-set-sha256-"
        f"{four_layer_route_plan_set_semantic_digest(plan_set)}"
    )
    if plan_set.layer_set_id != expected_layer_set_id:
        raise ValueError(
            "FourLayerRoutePlanSet layer_set_id does not match canonical semantic content"
        )
    return plan_set


def four_layer_route_plan_set_to_geojson(plan_set: FourLayerRoutePlanSet) -> dict[str, Any]:
    document = four_layer_route_plan_set_to_dict(plan_set)
    properties = {key: item for key, item in document.items() if key != "layers"}
    features = []
    for bundle in plan_set.layers:
        for objective in ObjectiveMode:
            plan = bundle.plans[objective]
            plan_document = route_plan_v3_to_dict(plan)
            route_properties = {
                key: item for key, item in plan_document.items() if key != "waypoints"
            }
            route_properties["waypoint_etas"] = [
                item["eta"] for item in plan_document["waypoints"]
            ]
            route_properties["recommended_speeds_mps"] = [
                item["recommended_speed_mps"] for item in plan_document["waypoints"]
            ]
            features.append(_route_feature(plan, route_properties))
    return {"type": "FeatureCollection", "properties": properties, "features": features}


def four_layer_route_plan_set_from_geojson(
    value: Mapping[str, Any],
) -> FourLayerRoutePlanSet:
    _require_exact_fields(
        value,
        frozenset({"type", "properties", "features"}),
        name="FourLayerRoutePlanSet GeoJSON",
    )
    if value["type"] != "FeatureCollection":
        raise ValueError("FourLayerRoutePlanSet GeoJSON must be a FeatureCollection")
    properties = value["properties"]
    features = value["features"]
    if not isinstance(properties, Mapping) or not isinstance(features, list):
        raise ValueError("malformed FourLayerRoutePlanSet GeoJSON")
    if len(features) != 12:
        raise ValueError("FourLayerRoutePlanSet GeoJSON must contain twelve route features")
    plans = [_plan_from_route_feature(feature) for feature in features]
    layers = []
    for layer in PlanLayer:
        by_objective = {
            plan.objective_mode: plan for plan in plans if plan.planning_layer is layer
        }
        layers.append(LayerRouteBundle(layer, by_objective))
    document = dict(properties)
    document["layers"] = [
        {
            "planning_layer": bundle.planning_layer.value,
            "plans": {
                objective.value: route_plan_v3_to_dict(bundle.plans[objective])
                for objective in ObjectiveMode
            },
        }
        for bundle in layers
    ]
    return four_layer_route_plan_set_from_dict(document)


def route_plan_v3_semantic_digest(plan: RoutePlanV3) -> str:
    document = route_plan_v3_to_dict(plan)
    for key in ("plan_id", "layer_set_id", "planning_request_id", "generated_at"):
        document.pop(key)
    document["metrics"] = dict(document["metrics"])
    document["metrics"].pop("compute_ms")
    return _canonical_digest(document)


def four_layer_route_plan_set_semantic_digest(plan_set: FourLayerRoutePlanSet) -> str:
    document = four_layer_route_plan_set_to_dict(plan_set)
    for key in ("layer_set_id", "planning_request_id", "generated_at"):
        document.pop(key)
    for layer in document["layers"]:
        for plan in layer["plans"].values():
            for key in ("layer_set_id", "planning_request_id", "generated_at"):
                plan.pop(key)
            plan["metrics"] = dict(plan["metrics"])
            plan["metrics"].pop("compute_ms")
    return _canonical_digest(document)


def _as_v2(plan: RoutePlanV3) -> RoutePlan:
    return RoutePlan(
        **{field: getattr(plan, field) for field in _ROUTE_BASE_FIELDS},
        schema_version=ROUTE_PLAN_SCHEMA_VERSION,
    )


def _route_feature(plan: RoutePlanV3, properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": plan.plan_id,
        "geometry": {
            "type": "LineString",
            "coordinates": [[point.longitude, point.latitude] for point in plan.waypoints],
        },
        "properties": dict(properties),
    }


def _waypoint_features(plan: RoutePlanV3) -> list[dict[str, Any]]:
    return [
        {
            "type": "Feature",
            "id": f"{plan.plan_id}:waypoint:{index}",
            "geometry": {
                "type": "Point",
                "coordinates": [waypoint.longitude, waypoint.latitude],
            },
            "properties": {
                "feature_role": "waypoint",
                "plan_id": plan.plan_id,
                "index": index,
                "eta": _format_time(waypoint.eta),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            },
        }
        for index, waypoint in enumerate(plan.waypoints)
    ]


def _plan_from_route_feature(feature: object) -> RoutePlanV3:
    if not isinstance(feature, Mapping):
        raise ValueError("route feature must be an object")
    _require_exact_fields(
        feature,
        frozenset({"type", "id", "geometry", "properties"}),
        name="route feature",
    )
    if feature["type"] != "Feature":
        raise ValueError("route feature type must be Feature")
    geometry = feature["geometry"]
    properties = feature["properties"]
    if not isinstance(geometry, Mapping) or not isinstance(properties, Mapping):
        raise ValueError("malformed route feature")
    _require_exact_fields(
        geometry,
        frozenset({"type", "coordinates"}),
        name="route geometry",
    )
    if geometry["type"] != "LineString":
        raise ValueError("route geometry must be LineString")
    coordinates = geometry["coordinates"]
    etas = properties.get("waypoint_etas")
    speeds = properties.get("recommended_speeds_mps")
    if (
        not isinstance(coordinates, list)
        or not isinstance(etas, list)
        or not isinstance(speeds, list)
    ):
        raise ValueError("route feature waypoint arrays are malformed")
    if not (len(coordinates) == len(etas) == len(speeds)):
        raise ValueError("route feature waypoint arrays have different lengths")
    document = dict(properties)
    document.pop("waypoint_etas")
    document.pop("recommended_speeds_mps")
    document["waypoints"] = [
        {
            "longitude": coordinate[0],
            "latitude": coordinate[1],
            "eta": eta,
            "recommended_speed_mps": speed,
        }
        for coordinate, eta, speed in zip(coordinates, etas, speeds, strict=True)
    ]
    plan = route_plan_v3_from_dict(document)
    if feature["id"] != plan.plan_id:
        raise ValueError("route feature id does not match plan_id")
    return plan


def _validate_waypoint_features(plan: RoutePlanV3, features: list[object]) -> None:
    if len(features) != len(plan.waypoints):
        raise ValueError("RoutePlanV3 GeoJSON waypoint feature count is inconsistent")
    for index, (feature, waypoint) in enumerate(zip(features, plan.waypoints, strict=True)):
        if not isinstance(feature, Mapping):
            raise ValueError("waypoint feature must be an object")
        _require_exact_fields(
            feature,
            frozenset({"type", "id", "geometry", "properties"}),
            name=f"waypoint feature {index}",
        )
        geometry = feature["geometry"]
        properties = feature["properties"]
        if feature["type"] != "Feature" or not isinstance(geometry, Mapping):
            raise ValueError("malformed waypoint feature")
        if not isinstance(properties, Mapping):
            raise ValueError("malformed waypoint feature properties")
        _require_exact_fields(
            geometry,
            frozenset({"type", "coordinates"}),
            name=f"waypoint geometry {index}",
        )
        _require_exact_fields(
            properties,
            frozenset(
                {"feature_role", "plan_id", "index", "eta", "recommended_speed_mps"}
            ),
            name=f"waypoint properties {index}",
        )
        expected_id = f"{plan.plan_id}:waypoint:{index}"
        expected_coordinates = [waypoint.longitude, waypoint.latitude]
        if (
            feature["id"] != expected_id
            or geometry["type"] != "Point"
            or geometry["coordinates"] != expected_coordinates
            or properties["feature_role"] != "waypoint"
            or properties["plan_id"] != plan.plan_id
            or properties["index"] != index
            or properties["eta"] != _format_time(waypoint.eta)
            or properties["recommended_speed_mps"] != waypoint.recommended_speed_mps
        ):
            raise ValueError("waypoint feature does not match canonical route content")


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], *, name: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{name} fields mismatch: missing={missing}, extra={extra}")


def _require_plain_int(value: object, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer and not bool")


def _require_bool(value: object, *, name: str) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_V3_ONLY_FIELDS = frozenset(
    {
        "planning_layer",
        "layer_set_id",
        "focus_start_time",
        "focus_end_time",
        "reference_plan_id",
        "layer_goal_reached",
    }
)
_ROUTE_BASE_FIELDS = tuple(
    field
    for field in RoutePlan.__dataclass_fields__
    if field != "schema_version"
)


__all__ = [
    "four_layer_route_plan_set_from_dict",
    "four_layer_route_plan_set_from_geojson",
    "four_layer_route_plan_set_semantic_digest",
    "four_layer_route_plan_set_to_dict",
    "four_layer_route_plan_set_to_geojson",
    "route_plan_v3_from_dict",
    "route_plan_v3_from_geojson",
    "route_plan_v3_semantic_digest",
    "route_plan_v3_to_dict",
    "route_plan_v3_to_geojson",
]
