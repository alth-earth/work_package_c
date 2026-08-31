"""Strict JSON codec and canonical identity for ``cd.route-motion-set.v1``."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from arctic_route_planning.contracts.layered import PlanLayer
from arctic_route_planning.contracts.route_motion import (
    ROUTE_MOTION_SET_SCHEMA_VERSION,
    MotionSample,
    RouteMotionMode,
    RouteMotionQualification,
    RouteMotionRecord,
    RouteMotionSet,
    WaypointMotionAnchor,
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{field} must be RFC3339 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{field} must be RFC3339 UTC") from exc
    return parsed.astimezone(UTC)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def route_motion_set_to_dict(value: RouteMotionSet) -> dict[str, Any]:
    return {
        "schema_version": value.schema_version,
        "motion_set_id": value.motion_set_id,
        "layer_set_id": value.layer_set_id,
        "run_id": value.run_id,
        "scenario_id": value.scenario_id,
        "corridor_id": value.corridor_id,
        "generation_id": value.generation_id,
        "input_revision": value.input_revision,
        "risk_window_id": value.risk_window_id,
        "risk_window_digest": value.risk_window_digest,
        "vessel_profile_id": value.vessel_profile_id,
        "vessel_profile_version": value.vessel_profile_version,
        "vessel_profile_digest": value.vessel_profile_digest,
        "motion_profile_id": value.motion_profile_id,
        "motion_profile_digest": value.motion_profile_digest,
        "config_digest": value.config_digest,
        "model_config_digest": value.model_config_digest,
        "planner_config_digest": value.planner_config_digest,
        "producer_digest": value.producer_digest,
        "generated_at": _iso(value.generated_at),
        "records": [_record_to_dict(record) for record in value.records],
    }


def _record_to_dict(record: RouteMotionRecord) -> dict[str, Any]:
    return {
        "planning_layer": record.planning_layer.value,
        "plan_id": record.plan_id,
        "raw_route_digest": record.raw_route_digest,
        "mode": record.mode.value,
        "fallback_reason": record.fallback_reason,
        "curve_digest": record.curve_digest,
        "motion_digest": record.motion_digest,
        "interpolation": record.interpolation,
        "waypoint_anchors": [
            {
                "waypoint_index": anchor.waypoint_index,
                "eta": _iso(anchor.eta),
                "motion_sample_index": anchor.motion_sample_index,
                "arc_length_m": anchor.arc_length_m,
            }
            for anchor in record.waypoint_anchors
        ],
        "motion_samples": [
            {
                "lon": sample.longitude,
                "lat": sample.latitude,
                "eta": _iso(sample.eta),
                "course_degrees": sample.course_degrees,
                "speed_knots": sample.speed_knots,
            }
            for sample in record.motion_samples
        ],
        "qualification": {
            "result": record.qualification.result,
            "risk_rechecked": record.qualification.risk_rechecked,
            "hard_mask_rechecked": record.qualification.hard_mask_rechecked,
            "coverage_complete": record.qualification.coverage_complete,
            "eta_anchors_preserved": record.qualification.eta_anchors_preserved,
            "speed_checked": record.qualification.speed_checked,
            "curvature_checked": record.qualification.curvature_checked,
            "corridor_checked": record.qualification.corridor_checked,
            "manoeuvring_checked": record.qualification.manoeuvring_checked,
            "corridor_proof_scope": record.qualification.corridor_proof_scope,
            "evidence_kind": record.qualification.evidence_kind,
            "real_vessel_calibrated": record.qualification.real_vessel_calibrated,
            "details_digest": record.qualification.details_digest,
        },
    }


def route_motion_set_semantic_digest(value: RouteMotionSet) -> str:
    document = route_motion_set_to_dict(value)
    document.pop("motion_set_id", None)
    return canonical_sha256(document)


def route_motion_set_from_dict(value: Mapping[str, Any]) -> RouteMotionSet:
    expected = {
        "schema_version", "motion_set_id", "layer_set_id", "run_id", "scenario_id",
        "corridor_id", "generation_id", "input_revision", "risk_window_id",
        "risk_window_digest", "vessel_profile_id", "vessel_profile_version",
        "vessel_profile_digest", "motion_profile_id", "motion_profile_digest",
        "config_digest", "model_config_digest", "planner_config_digest",
        "producer_digest", "generated_at", "records",
    }
    if set(value) != expected:
        raise ValueError("RouteMotionSet fields differ from v1")
    if value["schema_version"] != ROUTE_MOTION_SET_SCHEMA_VERSION:
        raise ValueError("unsupported RouteMotionSet schema")
    records = value["records"]
    if not isinstance(records, list):
        raise ValueError("RouteMotionSet.records must be an array")
    result = RouteMotionSet(
        schema_version=str(value["schema_version"]),
        motion_set_id=str(value["motion_set_id"]),
        layer_set_id=str(value["layer_set_id"]),
        run_id=str(value["run_id"]),
        scenario_id=str(value["scenario_id"]),
        corridor_id=str(value["corridor_id"]),
        generation_id=_plain_int(value["generation_id"], "generation_id"),
        input_revision=_plain_int(value["input_revision"], "input_revision"),
        risk_window_id=str(value["risk_window_id"]),
        risk_window_digest=str(value["risk_window_digest"]),
        vessel_profile_id=str(value["vessel_profile_id"]),
        vessel_profile_version=str(value["vessel_profile_version"]),
        vessel_profile_digest=str(value["vessel_profile_digest"]),
        motion_profile_id=str(value["motion_profile_id"]),
        motion_profile_digest=str(value["motion_profile_digest"]),
        config_digest=str(value["config_digest"]),
        model_config_digest=str(value["model_config_digest"]),
        planner_config_digest=str(value["planner_config_digest"]),
        producer_digest=str(value["producer_digest"]),
        generated_at=_parse_time(value["generated_at"], "generated_at"),
        records=tuple(_record_from_dict(item) for item in records),
    )
    expected_id = "route-motion-set-sha256-" + route_motion_set_semantic_digest(result)
    if result.motion_set_id != expected_id:
        raise ValueError("motion_set_id does not match canonical content")
    return result


def _record_from_dict(value: Any) -> RouteMotionRecord:
    if not isinstance(value, Mapping):
        raise ValueError("RouteMotionRecord must be an object")
    expected = {
        "planning_layer", "plan_id", "raw_route_digest", "mode", "fallback_reason",
        "curve_digest", "motion_digest", "interpolation", "waypoint_anchors",
        "motion_samples", "qualification",
    }
    if set(value) != expected:
        raise ValueError("RouteMotionRecord fields differ from v1")
    anchors = value["waypoint_anchors"]
    samples = value["motion_samples"]
    qualification = value["qualification"]
    if not isinstance(anchors, list) or not isinstance(samples, list):
        raise ValueError("motion samples and anchors must be arrays")
    if not isinstance(qualification, Mapping):
        raise ValueError("qualification must be an object")
    qualification_fields = {
        "result", "risk_rechecked", "hard_mask_rechecked", "coverage_complete",
        "eta_anchors_preserved", "speed_checked", "curvature_checked",
        "corridor_checked", "manoeuvring_checked", "corridor_proof_scope",
        "evidence_kind", "real_vessel_calibrated", "details_digest",
    }
    if set(qualification) != qualification_fields:
        raise ValueError("qualification fields differ from v1")
    return RouteMotionRecord(
        planning_layer=PlanLayer(str(value["planning_layer"])),
        plan_id=str(value["plan_id"]),
        raw_route_digest=str(value["raw_route_digest"]),
        mode=RouteMotionMode(str(value["mode"])),
        fallback_reason=(
            None if value["fallback_reason"] is None else str(value["fallback_reason"])
        ),
        curve_digest=str(value["curve_digest"]),
        motion_digest=str(value["motion_digest"]),
        interpolation=str(value["interpolation"]),
        waypoint_anchors=tuple(
            WaypointMotionAnchor(
                waypoint_index=_plain_int(item["waypoint_index"], "waypoint_index"),
                eta=_parse_time(item["eta"], "anchor.eta"),
                motion_sample_index=_plain_int(
                    item["motion_sample_index"], "motion_sample_index"
                ),
                arc_length_m=item["arc_length_m"],
            )
            for item in anchors
            if isinstance(item, Mapping)
        ),
        motion_samples=tuple(
            MotionSample(
                longitude=item["lon"],
                latitude=item["lat"],
                eta=_parse_time(item["eta"], "sample.eta"),
                course_degrees=item["course_degrees"],
                speed_knots=item["speed_knots"],
            )
            for item in samples
            if isinstance(item, Mapping)
        ),
        qualification=RouteMotionQualification(
            result=str(qualification["result"]),
            risk_rechecked=_plain_bool(qualification["risk_rechecked"], "risk_rechecked"),
            hard_mask_rechecked=_plain_bool(
                qualification["hard_mask_rechecked"], "hard_mask_rechecked"
            ),
            coverage_complete=_plain_bool(
                qualification["coverage_complete"], "coverage_complete"
            ),
            eta_anchors_preserved=_plain_bool(
                qualification["eta_anchors_preserved"], "eta_anchors_preserved"
            ),
            speed_checked=_plain_bool(qualification["speed_checked"], "speed_checked"),
            curvature_checked=_plain_bool(
                qualification["curvature_checked"], "curvature_checked"
            ),
            corridor_checked=_plain_bool(
                qualification["corridor_checked"], "corridor_checked"
            ),
            manoeuvring_checked=_plain_bool(
                qualification["manoeuvring_checked"], "manoeuvring_checked"
            ),
            corridor_proof_scope=str(qualification["corridor_proof_scope"]),
            evidence_kind=str(qualification["evidence_kind"]),
            real_vessel_calibrated=_plain_bool(
                qualification["real_vessel_calibrated"], "real_vessel_calibrated"
            ),
            details_digest=str(qualification["details_digest"]),
        ),
    )


def _plain_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _plain_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


__all__ = [
    "canonical_sha256",
    "route_motion_set_from_dict",
    "route_motion_set_semantic_digest",
    "route_motion_set_to_dict",
]
