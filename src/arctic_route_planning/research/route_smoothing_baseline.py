"""R0.2 geometry-only baseline statistics for an authoritative waypoint route."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

from .route_smoothing import (
    Coordinate,
    RouteSmoothingPolicy,
    _canonical_digest,
    _Frame,
    _norm,
    _route_member,
    _route_waypoint_record,
    _sub,
    _unit,
)

BASELINE_SCHEMA_VERSION = "c.research-route-smoothing-baseline.v1"


def _wrap_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def _bearing(vector: Coordinate) -> float:
    return (math.degrees(math.atan2(vector[0], vector[1])) + 360.0) % 360.0


def _angle_bin(angle_deg: float) -> str:
    if angle_deg <= 15.0:
        return "0-15deg"
    if angle_deg <= 45.0:
        return "15-45deg"
    if angle_deg <= 90.0:
        return "45-90deg"
    return ">90deg"


def build_route_geometry_baseline(
    route: Any,
    *,
    policy: RouteSmoothingPolicy | None = None,
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return reproducible corner and roughness facts without changing a route."""

    chosen_policy = policy or RouteSmoothingPolicy()
    context_values = dict(context or {})
    route_id = _route_member(route, "plan_id") or _route_member(route, "route_id")
    route_id = str(route_id) if route_id is not None else None
    values = _route_member(route, "waypoints")
    records = (
        tuple(_route_waypoint_record(value) for value in values)
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
        else ()
    )
    if not records or any(record is None for record in records):
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "status": "FALLBACK",
            "route_id": route_id,
            "fallback_reason": "invalid_route_waypoints",
            "corners": [],
        }
    typed_records = tuple(record for record in records if record is not None)
    raw_points = tuple(record[0] for record in typed_records)
    if len(raw_points) < 2:
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "status": "FALLBACK",
            "route_id": route_id,
            "fallback_reason": "insufficient_points",
            "corners": [],
        }
    frame = _Frame(
        lon0=raw_points[0][0],
        lat0_rad=math.radians(raw_points[0][1]),
        cos_lat0=math.cos(math.radians(raw_points[0][1])),
    )
    if abs(frame.cos_lat0) < 1e-6:
        return {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "status": "FALLBACK",
            "route_id": route_id,
            "fallback_reason": "invalid_local_frame",
            "corners": [],
        }
    local_points = tuple(frame.to_local(point) for point in raw_points)
    leg_lengths = tuple(_norm(_sub(end, start)) for start, end in pairwise(local_points))
    total_distance_m = sum(leg_lengths)
    corners: list[dict[str, Any]] = []
    for index in range(1, len(local_points) - 1):
        incoming_vector = _sub(local_points[index], local_points[index - 1])
        outgoing_vector = _sub(local_points[index + 1], local_points[index])
        incoming = _unit(incoming_vector)
        outgoing = _unit(outgoing_vector)
        if incoming is None or outgoing is None:
            corners.append(
                {
                    "corner_index": index,
                    "classification": "INVALID_GEOMETRY",
                    "reason": "zero_length_leg",
                }
            )
            continue
        cosine = max(-1.0, min(1.0, incoming[0] * outgoing[0] + incoming[1] * outgoing[1]))
        angle = math.degrees(math.acos(cosine))
        is_corner = angle >= chosen_policy.corner_angle_threshold_deg
        incoming_bearing = _bearing(incoming)
        outgoing_bearing = _bearing(outgoing)
        delta_heading = _wrap_degrees(outgoing_bearing - incoming_bearing)
        trim = chosen_policy.minimum_radius_m * math.tan(math.radians(angle) / 2.0)
        available_trim = chosen_policy.max_trim_fraction * min(
            leg_lengths[index - 1], leg_lengths[index]
        )
        geometric_max_radius = (
            available_trim / math.tan(angle / 2.0)
            if angle > 0 and angle < math.radians(179.0)
            else 0.0
        )
        corner_risk = context_values.get("risk_by_corner", {}).get(str(index), {})
        if not isinstance(corner_risk, Mapping):
            corner_risk = {}
        eligible = (
            angle >= chosen_policy.corner_angle_threshold_deg
            and angle < 179.0
            and trim <= available_trim
        )
        corners.append(
            {
                "corner_index": index,
                "classification": "CORNER_PRESENT" if is_corner else "STRAIGHT_CONTINUATION",
                "incoming_bearing_deg": incoming_bearing,
                "outgoing_bearing_deg": outgoing_bearing,
                "delta_heading_deg": delta_heading,
                "turn_angle_deg": angle,
                "angle_bin": _angle_bin(angle),
                "incoming_leg_m": leg_lengths[index - 1],
                "outgoing_leg_m": leg_lengths[index],
                "reference_turn_arc_m": chosen_policy.minimum_radius_m * math.radians(angle),
                "minimum_radius_trim_m": trim,
                "available_trim_m": available_trim,
                "geometric_max_radius_m": geometric_max_radius,
                "eligible_by_geometry": eligible,
                "local_space_sufficient": eligible,
                "hard_mask_distance_m": corner_risk.get("hard_mask_distance_m"),
                "hard_mask_status": corner_risk.get("hard_mask_status", "NOT_EVALUATED"),
                "risk_value": corner_risk.get("risk_value"),
                "risk_level": corner_risk.get("risk_level"),
                "risk_status": corner_risk.get("risk_status", "NOT_EVALUATED"),
                "coverage_status": corner_risk.get("coverage_status", "NOT_EVALUATED"),
            }
        )
    valid_corners = [item for item in corners if item["classification"] == "CORNER_PRESENT"]
    for left, right in pairwise(valid_corners):
        if right["corner_index"] != left["corner_index"] + 1:
            continue
        shared_length = leg_lengths[left["corner_index"]]
        conflict = (
            float(left["minimum_radius_trim_m"]) + float(right["minimum_radius_trim_m"])
            > shared_length * chosen_policy.maximum_overlap_fraction
        )
        left["adjacent_corner_conflict"] = conflict
        right["adjacent_corner_conflict"] = conflict
    absolute_turns = [abs(float(item["delta_heading_deg"])) for item in valid_corners]
    average_adjacent_leg_m = [
        (float(item["incoming_leg_m"]) + float(item["outgoing_leg_m"])) / 2.0
        for item in valid_corners
    ]
    roughness_values = [
        angle / (length / 1000.0)
        for angle, length in zip(absolute_turns, average_adjacent_leg_m, strict=True)
        if length > 0
    ]
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "status": "PASS",
        "route_id": route_id,
        "route_identity": context_values.get("route_identity", {"route_id": route_id}),
        "route_semantic_digest": context_values.get("route_semantic_digest"),
        "input_identity": context_values.get("input_identity"),
        "vessel_profile_id": context_values.get("vessel_profile_id"),
        "risk_frame_identity": context_values.get("risk_frame_identity"),
        "raw_route_digest": _canonical_digest([list(point) for point in raw_points]),
        "waypoint_count": len(raw_points),
        "leg_count": len(leg_lengths),
        "total_distance_km": total_distance_m / 1000.0,
        "minimum_radius_m": chosen_policy.minimum_radius_m,
        "corner_count": len(valid_corners),
        "eligible_corner_count": sum(
            bool(item.get("eligible_by_geometry")) for item in valid_corners
        ),
        "angle_bins": dict(Counter(item["angle_bin"] for item in valid_corners)),
        "maximum_turn_angle_deg": max(absolute_turns, default=0.0),
        "total_absolute_turn_deg": sum(absolute_turns),
        "maximum_turn_deg_per_km_of_adjacent_leg": max(roughness_values, default=0.0),
        "risk_evidence": context_values.get(
            "risk_evidence",
            {
                "status": "NOT_EVALUATED",
                "coverage_complete": False,
                "reason": "R0.2 geometry-only baseline",
            },
        ),
        "hard_mask_evidence": context_values.get(
            "hard_mask_evidence",
            {"status": "NOT_EVALUATED", "violations": None},
        ),
        "coverage_evidence": context_values.get(
            "coverage_evidence",
            {"status": "NOT_EVALUATED", "complete": False},
        ),
        "corners": corners,
    }
    payload["baseline_digest"] = _canonical_digest(payload)
    return payload


def _read_route(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read route JSON {path}: {exc}") from exc
    if isinstance(value, dict) and isinstance(value.get("route"), dict):
        return value["route"]
    if isinstance(value, dict) and isinstance(value.get("routes"), list):
        routes = value["routes"]
        if routes and isinstance(routes[0], dict):
            return routes[0]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arctic_route_planning.research.route_smoothing_baseline"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-radius-m", type=float, default=2_000.0)
    parser.add_argument(
        "--context",
        type=Path,
        default=None,
        help="optional JSON evidence context; omitted means risk/hard-mask NOT_EVALUATED",
    )
    args = parser.parse_args(argv)
    context = None
    if args.context is not None:
        context = json.loads(args.context.read_text(encoding="utf-8"))
        if not isinstance(context, dict):
            raise ValueError("baseline context must be a JSON object")
    baseline = build_route_geometry_baseline(
        _read_route(args.input),
        policy=RouteSmoothingPolicy(minimum_radius_m=args.minimum_radius_m),
        context=context,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(baseline, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        f"wrote {args.output} status={baseline.get('status')} "
        f"corners={baseline.get('corner_count', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
