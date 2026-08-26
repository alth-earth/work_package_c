"""JSON and GeoJSON serialization plus crash-safe same-directory writes."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RouteMetrics,
    RoutePlan,
    Waypoint,
)
from arctic_route_planning.domain.models import ObjectiveMode, PlanKind, ReplanReason

from .models import (
    SelectionRationale,
)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} is not valid ISO-8601: {value!r}") from exc


def route_plan_to_dict(plan: RoutePlan) -> dict[str, Any]:
    """Return the canonical JSON representation of a route plan."""

    return {
        "schema_version": plan.schema_version,
        "run_id": plan.run_id,
        "scenario_id": plan.scenario_id,
        "corridor_id": plan.corridor_id,
        "vessel_profile_id": plan.vessel_profile_id,
        "config_digest": plan.config_digest,
        "model_config_digest": plan.model_config_digest,
        "planner_config_digest": plan.planner_config_digest,
        "provenance": plan.provenance.value,
        "generation_id": plan.generation_id,
        "planning_request_id": plan.planning_request_id,
        "input_revision": plan.input_revision,
        "plan_id": plan.plan_id,
        "plan_version": plan.plan_version,
        "generated_at": _format_time(plan.generated_at),
        "as_of_time": _format_time(plan.as_of_time),
        "start_time": _format_time(plan.start_time),
        "objective_mode": plan.objective_mode.value,
        "plan_kind": plan.plan_kind.value,
        "waypoints": [
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": _format_time(waypoint.eta),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            }
            for waypoint in plan.waypoints
        ],
        "metrics": {
            "distance_km": plan.metrics.distance_km,
            "eta_hours": plan.metrics.eta_hours,
            "avg_risk": plan.metrics.avg_risk,
            "max_risk": plan.metrics.max_risk,
            "integrated_risk_hours": plan.metrics.integrated_risk_hours,
            "minimum_confidence": plan.metrics.minimum_confidence,
            "hard_constraint_violations": plan.metrics.hard_constraint_violations,
            "turn_count": plan.metrics.turn_count,
            "expanded_nodes": plan.metrics.expanded_nodes,
            "compute_ms": plan.metrics.compute_ms,
            "objective_cost": plan.metrics.objective_cost,
        },
        "replan_reasons": [reason.value for reason in plan.replan_reasons],
        "source_risk_ids": list(plan.source_risk_ids),
        "planner_version": plan.planner_version,
        "destination_reached": plan.destination_reached,
    }


def route_plan_from_dict(value: Mapping[str, Any]) -> RoutePlan:
    """Parse and validate the canonical JSON representation."""

    try:
        metrics_value = value["metrics"]
        raw_waypoints = value["waypoints"]
        if not isinstance(metrics_value, Mapping) or not isinstance(raw_waypoints, list):
            raise ValueError("metrics must be an object and waypoints must be an array")
        waypoints = tuple(
            Waypoint(
                longitude=float(item["longitude"]),
                latitude=float(item["latitude"]),
                eta=_parse_time(item["eta"], "waypoint.eta"),
                recommended_speed_mps=float(item["recommended_speed_mps"]),
            )
            for item in raw_waypoints
        )
        metrics = RouteMetrics(
            distance_km=float(metrics_value["distance_km"]),
            eta_hours=float(metrics_value["eta_hours"]),
            avg_risk=float(metrics_value["avg_risk"]),
            max_risk=float(metrics_value["max_risk"]),
            integrated_risk_hours=float(metrics_value["integrated_risk_hours"]),
            minimum_confidence=float(metrics_value["minimum_confidence"]),
            hard_constraint_violations=int(metrics_value["hard_constraint_violations"]),
            turn_count=int(metrics_value["turn_count"]),
            expanded_nodes=int(metrics_value["expanded_nodes"]),
            compute_ms=float(metrics_value["compute_ms"]),
            objective_cost=float(metrics_value["objective_cost"]),
        )
        destination_reached = value.get("destination_reached", True)
        if not isinstance(destination_reached, bool):
            raise ValueError("destination_reached must be a boolean")
        return RoutePlan(
            schema_version=str(value["schema_version"]),
            run_id=str(value["run_id"]),
            scenario_id=str(value["scenario_id"]),
            corridor_id=str(value["corridor_id"]),
            vessel_profile_id=str(value["vessel_profile_id"]),
            config_digest=str(value["config_digest"]),
            model_config_digest=str(value["model_config_digest"]),
            planner_config_digest=str(value["planner_config_digest"]),
            provenance=ProvenanceKind(str(value["provenance"])),
            generation_id=int(value["generation_id"]),
            planning_request_id=str(value["planning_request_id"]),
            input_revision=int(value["input_revision"]),
            plan_id=str(value["plan_id"]),
            plan_version=str(value["plan_version"]),
            generated_at=_parse_time(value["generated_at"], "generated_at"),
            as_of_time=_parse_time(value["as_of_time"], "as_of_time"),
            start_time=_parse_time(value["start_time"], "start_time"),
            objective_mode=ObjectiveMode(str(value["objective_mode"])),
            plan_kind=PlanKind(str(value["plan_kind"])),
            waypoints=waypoints,
            metrics=metrics,
            replan_reasons=tuple(
                ReplanReason(str(item)) for item in value.get("replan_reasons", ())
            ),
            source_risk_ids=tuple(str(item) for item in value["source_risk_ids"]),
            planner_version=str(value["planner_version"]),
            destination_reached=destination_reached,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"invalid route plan document: missing or malformed {exc}") from exc


def route_plan_to_geojson(plan: RoutePlan) -> dict[str, Any]:
    """Serialize a plan as a GeoJSON FeatureCollection."""

    document = route_plan_to_dict(plan)
    properties = {key: value for key, value in document.items() if key != "waypoints"}
    properties["waypoint_etas"] = [item["eta"] for item in document["waypoints"]]
    properties["recommended_speeds_mps"] = [
        item["recommended_speed_mps"] for item in document["waypoints"]
    ]
    route_feature = {
        "type": "Feature",
        "id": plan.plan_id,
        "geometry": {
            "type": "LineString",
            "coordinates": [[waypoint.longitude, waypoint.latitude] for waypoint in plan.waypoints],
        },
        "properties": properties,
    }
    waypoint_features = [
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
    return {"type": "FeatureCollection", "features": [route_feature, *waypoint_features]}


def route_plan_from_geojson(value: Mapping[str, Any]) -> RoutePlan:
    """Parse a GeoJSON document produced by :func:`route_plan_to_geojson`."""

    if value.get("type") != "FeatureCollection":
        raise ValueError("route GeoJSON must be a FeatureCollection")
    features = value.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("route GeoJSON must contain features")
    route = features[0]
    if not isinstance(route, Mapping):
        raise ValueError("first GeoJSON feature must be an object")
    geometry = route.get("geometry")
    if not isinstance(geometry, Mapping) or geometry.get("type") != "LineString":
        raise ValueError("first GeoJSON feature must be a LineString")
    properties = route.get("properties")
    coordinates = geometry.get("coordinates")
    if not isinstance(properties, Mapping) or not isinstance(coordinates, list):
        raise ValueError("malformed route GeoJSON feature")
    etas = properties.get("waypoint_etas")
    speeds = properties.get("recommended_speeds_mps")
    if not isinstance(etas, list) or not isinstance(speeds, list):
        raise ValueError("GeoJSON route must include waypoint ETA and speed arrays")
    if not (len(coordinates) == len(etas) == len(speeds)):
        raise ValueError("GeoJSON coordinate, ETA, and speed arrays must have equal lengths")
    document = dict(properties)
    document.pop("waypoint_etas", None)
    document.pop("recommended_speeds_mps", None)
    document["waypoints"] = [
        {
            "longitude": coordinate[0],
            "latitude": coordinate[1],
            "eta": eta,
            "recommended_speed_mps": speed,
        }
        for coordinate, eta, speed in zip(coordinates, etas, speeds, strict=True)
    ]
    return route_plan_from_dict(document)


def atomic_write_json(path: str | Path, value: Mapping[str, Any]) -> Path:
    """Atomically replace *path* with a UTF-8 JSON document."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except BaseException:
        with suppress(FileNotFoundError):
            temporary.unlink()
        raise
    return destination


def write_route_plan_json(path: str | Path, plan: RoutePlan) -> Path:
    return atomic_write_json(path, route_plan_to_dict(plan))


def write_route_plan_geojson(path: str | Path, plan: RoutePlan) -> Path:
    return atomic_write_json(path, route_plan_to_geojson(plan))


def selection_rationale_to_dict(rationale: SelectionRationale) -> dict[str, Any]:
    """Return the canonical JSON representation of a selection rationale."""

    t = rationale.tradeoffs
    return {
        "schema_version": rationale.schema_version,
        "run_id": rationale.run_id,
        "scenario_id": rationale.scenario_id,
        "corridor_id": rationale.corridor_id,
        "vessel_profile_id": rationale.vessel_profile_id,
        "config_digest": rationale.config_digest,
        "model_config_digest": rationale.model_config_digest,
        "planner_config_digest": rationale.planner_config_digest,
        "provenance": rationale.provenance,
        "generation_id": rationale.generation_id,
        "planning_request_id": rationale.planning_request_id,
        "input_revision": rationale.input_revision,
        "selected_plan_id": rationale.selected_plan_id,
        "baseline_plan_id": rationale.baseline_plan_id,
        "selected_objective": rationale.selected_objective.value,
        "baseline_objective": rationale.baseline_objective.value,
        "tradeoffs": {
            "delta_distance_km": t.delta_distance_km,
            "delta_eta_hours": t.delta_eta_hours,
            "delta_avg_risk": t.delta_avg_risk,
            "delta_max_risk": t.delta_max_risk,
            "delta_integrated_risk_hours": t.delta_integrated_risk_hours,
            "avg_risk_reduction_pct": t.avg_risk_reduction_pct,
            "max_risk_reduction_pct": t.max_risk_reduction_pct,
        },
        "summary_text": rationale.summary_text,
    }


def selection_rationale_from_dict(value: Mapping[str, Any]) -> SelectionRationale:
    """Parse and validate the canonical JSON selection rationale."""

    try:
        tradeoffs_value = value["tradeoffs"]
        if not isinstance(tradeoffs_value, Mapping):
            raise ValueError("tradeoffs must be an object")
        from arctic_route_planning.publishing.models import TradeoffDeltas

        tradeoffs = TradeoffDeltas(
            delta_distance_km=float(tradeoffs_value["delta_distance_km"]),
            delta_eta_hours=float(tradeoffs_value["delta_eta_hours"]),
            delta_avg_risk=float(tradeoffs_value["delta_avg_risk"]),
            delta_max_risk=float(tradeoffs_value["delta_max_risk"]),
            delta_integrated_risk_hours=float(
                tradeoffs_value["delta_integrated_risk_hours"]
            ),
            avg_risk_reduction_pct=float(tradeoffs_value["avg_risk_reduction_pct"]),
            max_risk_reduction_pct=float(tradeoffs_value["max_risk_reduction_pct"]),
        )
        return SelectionRationale(
            schema_version=str(value["schema_version"]),
            run_id=str(value["run_id"]),
            scenario_id=str(value["scenario_id"]),
            corridor_id=str(value["corridor_id"]),
            vessel_profile_id=str(value["vessel_profile_id"]),
            config_digest=str(value["config_digest"]),
            model_config_digest=str(value["model_config_digest"]),
            planner_config_digest=str(value["planner_config_digest"]),
            provenance=str(value["provenance"]),
            generation_id=int(value["generation_id"]),
            planning_request_id=str(value["planning_request_id"]),
            input_revision=int(value["input_revision"]),
            selected_plan_id=str(value["selected_plan_id"]),
            baseline_plan_id=str(value["baseline_plan_id"]),
            selected_objective=ObjectiveMode(str(value["selected_objective"])),
            baseline_objective=ObjectiveMode(str(value["baseline_objective"])),
            tradeoffs=tradeoffs,
            summary_text=str(value["summary_text"]),
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"invalid selection rationale document: missing or malformed {exc}"
        ) from exc


def write_selection_rationale_json(path: str | Path, rationale: SelectionRationale) -> Path:
    return atomic_write_json(path, selection_rationale_to_dict(rationale))
