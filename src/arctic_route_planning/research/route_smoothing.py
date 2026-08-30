"""Fail-closed, research-only local cubic B-spline route smoothing.

The formal C route remains a waypoint polyline.  This module creates an
optional derived geometry for an experiment.  A one-span cubic B-spline is
evaluated in its equivalent cubic Bezier basis for each local corner; the
research distinction is that the radius is selected from a bounded candidate
set and every candidate can be rejected by a caller-owned safety validator.

The validator is intentionally a callback.  C owns the geometry, while the
caller owns RiskFrame identity, hard-mask semantics, coverage and ETA
evaluation.  A missing validator means geometry-only diagnostics and must not
be interpreted as navigability evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

EARTH_RADIUS_M = 6_371_008.8
SCHEMA_VERSION = "c.research-route-smoothing.v1"
POLICY = "authoritative_waypoints_adaptive_local_cubic_bspline_research_only"
SIDECAR_SCHEMA_VERSION = "c.research-route-smoothing-sidecar.v1"
SIDECAR_POLICY = "authoritative_waypoints_adaptive_local_cubic_bspline_motion_research_only"

Coordinate = tuple[float, float]
ValidationCallback = Callable[
    [tuple[Coordinate, ...], float],
    "CandidateDecision",
]


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    """Decision returned by the caller's safety/time validator."""

    accepted: bool
    reason: str | None = None
    evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("CandidateDecision.accepted must be bool")
        if not self.accepted and not self.reason:
            raise ValueError("rejected candidates require a reason")


@dataclass(frozen=True, slots=True)
class RouteSmoothingPolicy:
    """Bounded policy for one research smoothing run.

    ``minimum_radius_m`` is a lower bound, not the radius that must be used.
    The largest feasible radius in the deterministic candidate set is chosen.
    """

    minimum_radius_m: float = 2_000.0
    corner_angle_threshold_deg: float = 1.0
    max_trim_fraction: float = 0.45
    maximum_overlap_fraction: float = 0.90
    sample_spacing_m: float = 250.0
    minimum_curve_samples: int = 17
    maximum_curve_samples: int = 257
    radius_trials: int = 65
    maximum_route_points: int = 10_000

    def __post_init__(self) -> None:
        numeric_positive = (
            self.minimum_radius_m,
            self.sample_spacing_m,
        )
        if any(not math.isfinite(value) or value <= 0 for value in numeric_positive):
            raise ValueError("radius and sample spacing must be positive finite values")
        if not 0 <= self.corner_angle_threshold_deg < 179:
            raise ValueError("corner angle threshold must be in [0, 179)")
        if not 0 < self.max_trim_fraction < 0.5:
            raise ValueError("max_trim_fraction must be in (0, 0.5)")
        if not 0 < self.maximum_overlap_fraction <= 1:
            raise ValueError("maximum_overlap_fraction must be in (0, 1]")
        if self.minimum_curve_samples < 4:
            raise ValueError("minimum_curve_samples must be at least 4")
        if self.maximum_curve_samples < self.minimum_curve_samples:
            raise ValueError("maximum_curve_samples must not be smaller than minimum")
        if self.radius_trials < 2:
            raise ValueError("radius_trials must be at least 2")
        if self.maximum_route_points < 2:
            raise ValueError("maximum_route_points must be at least 2")


@dataclass(frozen=True, slots=True)
class CurveSegment:
    """One accepted local curve replacing a raw waypoint corner."""

    corner_index: int
    turn_angle_deg: float
    radius_m: float
    trim_m: float
    control_points_m: tuple[Coordinate, ...]
    samples: tuple[Coordinate, ...]
    minimum_radius_m: float
    maximum_deviation_m: float
    validator_evidence: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "corner_index": self.corner_index,
            "turn_angle_deg": self.turn_angle_deg,
            "radius_m": self.radius_m,
            "trim_m": self.trim_m,
            "control_points_m": [list(point) for point in self.control_points_m],
            "samples": [list(point) for point in self.samples],
            "minimum_radius_m": self.minimum_radius_m,
            "maximum_deviation_m": self.maximum_deviation_m,
            "validator_evidence": (
                dict(self.validator_evidence) if self.validator_evidence is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class RouteSmoothingResult:
    """Serializable research geometry and its fail-closed diagnostics."""

    status: str
    points: tuple[Coordinate, ...]
    raw_points: tuple[Coordinate, ...]
    segments: tuple[CurveSegment, ...]
    rejected_corners: tuple[dict[str, Any], ...]
    geometry_only: bool
    fallback_reason: str | None
    raw_route_digest: str
    curve_digest: str

    def __post_init__(self) -> None:
        if self.status not in {"ACCEPTED", "FALLBACK"}:
            raise ValueError("unsupported route smoothing status")
        if self.status == "ACCEPTED" and (len(self.points) < 2 or len(self.raw_points) < 2):
            raise ValueError("an accepted route smoothing result needs at least two points")
        if self.status == "FALLBACK" and not self.fallback_reason:
            raise ValueError("fallback results require a reason")
        if self.status == "ACCEPTED" and not self.segments:
            raise ValueError("accepted results require at least one curve segment")

    @property
    def applied(self) -> bool:
        return self.status == "ACCEPTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy": POLICY,
            "status": self.status,
            "applied": self.applied,
            "geometry_only": self.geometry_only,
            "fallback_reason": self.fallback_reason,
            "raw_route_digest": self.raw_route_digest,
            "curve_digest": self.curve_digest,
            "raw_points": [list(point) for point in self.raw_points],
            "points": [list(point) for point in self.points],
            "segments": [segment.to_dict() for segment in self.segments],
            "rejected_corners": [dict(item) for item in self.rejected_corners],
        }


@dataclass(frozen=True, slots=True)
class _Frame:
    lon0: float
    lat0_rad: float
    cos_lat0: float

    def to_local(self, point: Coordinate) -> Coordinate:
        delta_lon = _wrap_radians(math.radians(point[0] - self.lon0))
        return (
            EARTH_RADIUS_M * delta_lon * self.cos_lat0,
            EARTH_RADIUS_M * (math.radians(point[1]) - self.lat0_rad),
        )

    def to_geo(self, point: Coordinate) -> Coordinate:
        lon = self.lon0 + math.degrees(point[0] / (EARTH_RADIUS_M * self.cos_lat0))
        lat = math.degrees(self.lat0_rad + point[1] / EARTH_RADIUS_M)
        return (((lon + 180.0) % 360.0) - 180.0, lat)


@dataclass(frozen=True, slots=True)
class _Candidate:
    index: int
    angle_rad: float
    incoming: Coordinate
    outgoing: Coordinate
    incoming_length_m: float
    outgoing_length_m: float
    maximum_radius_m: float


def _finite_coordinate(value: Any) -> Coordinate | None:
    if not isinstance(value, Mapping):
        return None
    lon_value = value.get("lon", value.get("longitude"))
    lat_value = value.get("lat", value.get("latitude"))
    if isinstance(lon_value, bool) or isinstance(lat_value, bool):
        return None
    try:
        lon = float(lon_value)
        lat = float(lat_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(lon) or not math.isfinite(lat):
        return None
    if not -180.0 <= lon <= 180.0 or not -90.0 <= lat <= 90.0:
        return None
    return lon, lat


def _normalise_points(points: Sequence[Mapping[str, Any]]) -> tuple[Coordinate, ...] | None:
    result = tuple(_finite_coordinate(point) for point in points)
    if any(point is None for point in result):
        return None
    return tuple(point for point in result if point is not None)


def _wrap_radians(value: float) -> float:
    return (value + math.pi) % (2.0 * math.pi) - math.pi


def _sub(a: Coordinate, b: Coordinate) -> Coordinate:
    return a[0] - b[0], a[1] - b[1]


def _add(a: Coordinate, b: Coordinate) -> Coordinate:
    return a[0] + b[0], a[1] + b[1]


def _mul(a: Coordinate, scalar: float) -> Coordinate:
    return a[0] * scalar, a[1] * scalar


def _norm(a: Coordinate) -> float:
    return math.hypot(a[0], a[1])


def _dot(a: Coordinate, b: Coordinate) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Coordinate, b: Coordinate) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _unit(a: Coordinate) -> Coordinate | None:
    length = _norm(a)
    return _mul(a, 1.0 / length) if length > 1e-9 else None


def _distance_to_segment(point: Coordinate, start: Coordinate, end: Coordinate) -> float:
    segment = _sub(end, start)
    length_squared = _dot(segment, segment)
    if length_squared <= 1e-18:
        return _norm(_sub(point, start))
    fraction = max(0.0, min(1.0, _dot(_sub(point, start), segment) / length_squared))
    return _norm(_sub(point, _add(start, _mul(segment, fraction))))


def _distance_to_polyline(point: Coordinate, polyline: Sequence[Coordinate]) -> float:
    return min(
        _distance_to_segment(point, start, end)
        for start, end in pairwise(polyline)
    )


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _route_member(route: Any, name: str, default: Any = None) -> Any:
    if isinstance(route, Mapping):
        return route.get(name, default)
    return getattr(route, name, default)


def _parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _route_waypoint_record(value: Any) -> tuple[Coordinate, datetime] | None:
    if isinstance(value, Mapping):
        coordinate = _finite_coordinate(value)
        eta_value = value.get("eta")
    else:
        coordinate = _finite_coordinate(
            {
                "longitude": getattr(value, "longitude", None),
                "latitude": getattr(value, "latitude", None),
            }
        )
        eta_value = getattr(value, "eta", None)
    eta = _parse_utc(eta_value)
    if coordinate is None or eta is None:
        return None
    return coordinate, eta


def _path_metric(
    points: Sequence[Coordinate],
) -> tuple[_Frame, tuple[Coordinate, ...], tuple[float, ...]]:
    frame = _Frame(
        lon0=points[0][0],
        lat0_rad=math.radians(points[0][1]),
        cos_lat0=math.cos(math.radians(points[0][1])),
    )
    local_points = tuple(frame.to_local(point) for point in points)
    distances = [0.0]
    for first, second in pairwise(local_points):
        distance = _norm(_sub(second, first))
        if not math.isfinite(distance) or distance <= 1e-9:
            raise ValueError("route contains a zero-length curve segment")
        distances.append(distances[-1] + distance)
    return frame, local_points, tuple(distances)


def _anchor_indices(
    raw_points: Sequence[Coordinate],
    path_points: Sequence[Coordinate],
) -> tuple[int, ...] | None:
    frame, local_raw, _ = _path_metric(raw_points)
    local_path = tuple(frame.to_local(point) for point in path_points)
    anchors: list[int] = []
    search_start = 0
    for raw_index, raw_point in enumerate(local_raw):
        last_search_index = len(local_path) - (len(local_raw) - raw_index - 1) - 1
        forced_index = (
            0
            if raw_index == 0
            else len(local_path) - 1
            if raw_index == len(local_raw) - 1
            else None
        )
        best_index = forced_index
        if best_index is None:
            best_distance = math.inf
            for path_index in range(search_start, last_search_index + 1):
                distance = _norm(_sub(local_path[path_index], raw_point))
                if distance < best_distance:
                    best_distance = distance
                    best_index = path_index
        if (
            best_index is None
            or best_index < search_start
            or best_index > last_search_index
        ):
            return None
        anchors.append(best_index)
        search_start = best_index
    if any(current <= previous for previous, current in pairwise(anchors)):
        return None
    return tuple(anchors)


def _time_at_distance(
    distance_m: float,
    anchor_distances_m: Sequence[float],
    anchor_times: Sequence[datetime],
) -> datetime:
    if distance_m <= anchor_distances_m[0]:
        return anchor_times[0]
    for index in range(len(anchor_distances_m) - 1):
        start_distance = anchor_distances_m[index]
        end_distance = anchor_distances_m[index + 1]
        if distance_m <= end_distance:
            fraction = (distance_m - start_distance) / (end_distance - start_distance)
            duration = anchor_times[index + 1] - anchor_times[index]
            return anchor_times[index] + duration * fraction
    return anchor_times[-1]


def _bezier_point(controls: Sequence[Coordinate], t: float) -> Coordinate:
    p0, p1, p2, p3 = controls
    one_minus = 1.0 - t
    return (
        one_minus**3 * p0[0]
        + 3.0 * one_minus**2 * t * p1[0]
        + 3.0 * one_minus * t**2 * p2[0]
        + t**3 * p3[0],
        one_minus**3 * p0[1]
        + 3.0 * one_minus**2 * t * p1[1]
        + 3.0 * one_minus * t**2 * p2[1]
        + t**3 * p3[1],
    )


def _bezier_first(controls: Sequence[Coordinate], t: float) -> Coordinate:
    p0, p1, p2, p3 = controls
    one_minus = 1.0 - t
    return _add(
        _add(
            _mul(_sub(p1, p0), 3.0 * one_minus**2),
            _mul(_sub(p2, p1), 6.0 * one_minus * t),
        ),
        _mul(_sub(p3, p2), 3.0 * t**2),
    )


def _bezier_second(controls: Sequence[Coordinate], t: float) -> Coordinate:
    p0, p1, p2, p3 = controls
    return _add(
        _mul(_add(_sub(p2, _mul(p1, 2.0)), p0), 6.0 * (1.0 - t)),
        _mul(_add(_sub(p3, _mul(p2, 2.0)), p1), 6.0 * t),
    )


def _radius_at(controls: Sequence[Coordinate], t: float) -> float:
    first = _bezier_first(controls, t)
    speed = _norm(first)
    if speed <= 1e-9 or not math.isfinite(speed):
        return 0.0
    curvature = abs(_cross(first, _bezier_second(controls, t))) / speed**3
    return math.inf if curvature <= 1e-12 else 1.0 / curvature


def _candidate_for_corner(
    local_points: Sequence[Coordinate],
    index: int,
    policy: RouteSmoothingPolicy,
) -> _Candidate | None:
    incoming_vector = _sub(local_points[index], local_points[index - 1])
    outgoing_vector = _sub(local_points[index + 1], local_points[index])
    incoming = _unit(incoming_vector)
    outgoing = _unit(outgoing_vector)
    incoming_length = _norm(incoming_vector)
    outgoing_length = _norm(outgoing_vector)
    if incoming is None or outgoing is None:
        return None
    angle = math.acos(max(-1.0, min(1.0, _dot(incoming, outgoing))))
    if math.degrees(angle) < policy.corner_angle_threshold_deg or angle >= math.radians(179.0):
        return None
    maximum_radius = (
        policy.max_trim_fraction * min(incoming_length, outgoing_length) / math.tan(angle / 2.0)
    )
    if not math.isfinite(maximum_radius) or maximum_radius < policy.minimum_radius_m:
        return None
    return _Candidate(
        index=index,
        angle_rad=angle,
        incoming=incoming,
        outgoing=outgoing,
        incoming_length_m=incoming_length,
        outgoing_length_m=outgoing_length,
        maximum_radius_m=maximum_radius,
    )


def _radius_candidates(candidate: _Candidate, policy: RouteSmoothingPolicy) -> tuple[float, ...]:
    high = candidate.maximum_radius_m
    low = policy.minimum_radius_m
    values = tuple(
        high - (high - low) * index / (policy.radius_trials - 1)
        for index in range(policy.radius_trials)
    )
    return tuple(sorted({round(value, 6) for value in values if value >= low}, reverse=True))


def _curve_for_radius(
    local_points: Sequence[Coordinate],
    candidate: _Candidate,
    radius_m: float,
    policy: RouteSmoothingPolicy,
) -> CurveSegment | None:
    vertex = local_points[candidate.index]
    trim = radius_m * math.tan(candidate.angle_rad / 2.0)
    if trim > policy.max_trim_fraction * min(
        candidate.incoming_length_m,
        candidate.outgoing_length_m,
    ) + 1e-6:
        return None
    tangent_radius = trim / math.tan(candidate.angle_rad / 2.0)
    handle = (4.0 / 3.0) * tangent_radius * math.tan(candidate.angle_rad / 4.0)
    entry = _sub(vertex, _mul(candidate.incoming, trim))
    exit = _add(vertex, _mul(candidate.outgoing, trim))
    controls = (
        entry,
        _add(entry, _mul(candidate.incoming, handle)),
        _sub(exit, _mul(candidate.outgoing, handle)),
        exit,
    )
    control_length = sum(
        _norm(_sub(controls[index + 1], controls[index])) for index in range(3)
    )
    sample_count = max(
        policy.minimum_curve_samples,
        min(policy.maximum_curve_samples, math.ceil(control_length / policy.sample_spacing_m) + 1),
    )
    samples = tuple(
        _bezier_point(controls, index / (sample_count - 1))
        for index in range(sample_count)
    )
    minimum_radius = min(
        _radius_at(controls, index / (sample_count - 1)) for index in range(sample_count)
    )
    maximum_deviation = max(_distance_to_polyline(point, local_points) for point in samples)
    if (
        not math.isfinite(minimum_radius)
        or minimum_radius + 1e-6 < radius_m * 0.90
        or not math.isfinite(maximum_deviation)
    ):
        return None
    return CurveSegment(
        corner_index=candidate.index,
        turn_angle_deg=math.degrees(candidate.angle_rad),
        radius_m=radius_m,
        trim_m=trim,
        control_points_m=controls,
        samples=samples,
        minimum_radius_m=minimum_radius,
        maximum_deviation_m=maximum_deviation,
    )


def _fallback(
    raw_points: tuple[Coordinate, ...],
    reason: str,
    rejected: Sequence[dict[str, Any]] = (),
    *,
    geometry_only: bool,
) -> RouteSmoothingResult:
    raw_serialized = [list(point) for point in raw_points]
    raw_digest = _canonical_digest(raw_serialized)
    return RouteSmoothingResult(
        status="FALLBACK",
        points=raw_points,
        raw_points=raw_points,
        segments=(),
        rejected_corners=tuple(dict(item) for item in rejected),
        geometry_only=geometry_only,
        fallback_reason=reason,
        raw_route_digest=raw_digest,
        curve_digest=raw_digest,
    )


def build_route_smoothing(
    points: Sequence[Mapping[str, Any]],
    *,
    policy: RouteSmoothingPolicy | None = None,
    validator: ValidationCallback | None = None,
) -> RouteSmoothingResult:
    """Build a local adaptive smoothing result without changing the route.

    The optional validator receives local-metre curve samples and the tested
    radius.  It must return ``CandidateDecision(accepted=False, reason=...)``
    for missing/unknown safety evidence.  Without a validator the result is
    explicitly marked ``geometry_only=True``.
    """

    chosen_policy = policy or RouteSmoothingPolicy()
    if not isinstance(points, Sequence) or isinstance(points, (str, bytes)):
        return _fallback((), "invalid_points", geometry_only=validator is None)
    raw_points = _normalise_points(points)
    if raw_points is None:
        return _fallback((), "invalid_coordinate", geometry_only=validator is None)
    if len(raw_points) < 3:
        return _fallback(raw_points, "insufficient_points", geometry_only=validator is None)
    if any(first == second for first, second in pairwise(raw_points)):
        return _fallback(raw_points, "duplicate_point", geometry_only=validator is None)

    frame = _Frame(
        lon0=raw_points[0][0],
        lat0_rad=math.radians(raw_points[0][1]),
        cos_lat0=math.cos(math.radians(raw_points[0][1])),
    )
    if abs(frame.cos_lat0) < 1e-6:
        return _fallback(raw_points, "invalid_local_frame", geometry_only=validator is None)
    local_points = tuple(frame.to_local(point) for point in raw_points)
    candidates = tuple(
        candidate
        for index in range(1, len(local_points) - 1)
        if (candidate := _candidate_for_corner(local_points, index, chosen_policy)) is not None
    )
    if not candidates:
        return _fallback(raw_points, "no_eligible_corner", geometry_only=validator is None)

    accepted: list[CurveSegment] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        selected: CurveSegment | None = None
        last_reason = "all_radius_candidates_rejected"
        for radius in _radius_candidates(candidate, chosen_policy):
            curve = _curve_for_radius(local_points, candidate, radius, chosen_policy)
            if curve is None:
                last_reason = "geometry_constraint"
                continue
            if validator is None:
                decision = CandidateDecision(accepted=True)
            else:
                try:
                    decision = validator(curve.samples, radius)
                except Exception:
                    return _fallback(
                        raw_points,
                        "validator_error",
                        rejected,
                        geometry_only=False,
                    )
                if not isinstance(decision, CandidateDecision):
                    return _fallback(
                        raw_points,
                        "invalid_validator_decision",
                        rejected,
                        geometry_only=False,
                    )
            if decision.accepted and accepted and candidate.index == accepted[-1].corner_index + 1:
                shared_length = _norm(
                    _sub(local_points[candidate.index], local_points[accepted[-1].corner_index])
                )
                if accepted[-1].trim_m + curve.trim_m > (
                    shared_length * chosen_policy.maximum_overlap_fraction
                ):
                    last_reason = "adjacent_curve_overlap"
                    continue
            if decision.accepted:
                selected = CurveSegment(
                    corner_index=curve.corner_index,
                    turn_angle_deg=curve.turn_angle_deg,
                    radius_m=curve.radius_m,
                    trim_m=curve.trim_m,
                    control_points_m=curve.control_points_m,
                    samples=curve.samples,
                    minimum_radius_m=curve.minimum_radius_m,
                    maximum_deviation_m=curve.maximum_deviation_m,
                    validator_evidence=decision.evidence,
                )
                break
            last_reason = decision.reason or "validator_rejected"
        if selected is None:
            rejected.append(
                {
                    "corner_index": candidate.index,
                    "turn_angle_deg": math.degrees(candidate.angle_rad),
                    "maximum_radius_m": candidate.maximum_radius_m,
                    "reason": last_reason,
                }
            )
        else:
            accepted.append(selected)

    accepted.sort(key=lambda item: item.corner_index)
    blocked: set[int] = set()
    for first, second in pairwise(accepted):
        if second.corner_index != first.corner_index + 1:
            continue
        shared_length = _norm(
            _sub(local_points[second.corner_index], local_points[first.corner_index])
        )
        if first.trim_m + second.trim_m > shared_length * chosen_policy.maximum_overlap_fraction:
            blocked.update((first.corner_index, second.corner_index))
    if blocked:
        for index in sorted(blocked):
            rejected.append(
                {
                    "corner_index": index,
                    "reason": "adjacent_curve_overlap",
                }
            )
        accepted = [segment for segment in accepted if segment.corner_index not in blocked]

    if not accepted:
        return _fallback(
            raw_points,
            "all_curves_rejected",
            rejected,
            geometry_only=validator is None,
        )

    display_local: list[Coordinate] = [local_points[0]]
    last_raw_index = 0
    for segment in accepted:
        for raw_index in range(last_raw_index + 1, segment.corner_index):
            display_local.append(local_points[raw_index])
        display_local.extend(segment.samples)
        last_raw_index = segment.corner_index
    display_local.extend(local_points[last_raw_index + 1 :])
    if len(display_local) > chosen_policy.maximum_route_points:
        return _fallback(
            raw_points,
            "route_point_limit",
            rejected,
            geometry_only=validator is None,
        )
    output_points = tuple(frame.to_geo(point) for point in display_local)
    raw_digest = _canonical_digest([list(point) for point in raw_points])
    curve_payload = {
        "raw_route_digest": raw_digest,
        "points": [list(point) for point in output_points],
        "segments": [segment.to_dict() for segment in accepted],
    }
    return RouteSmoothingResult(
        status="ACCEPTED",
        points=output_points,
        raw_points=raw_points,
        segments=tuple(accepted),
        rejected_corners=tuple(rejected),
        geometry_only=validator is None,
        fallback_reason=None,
        raw_route_digest=raw_digest,
        curve_digest=_canonical_digest(curve_payload),
    )


def build_route_smoothing_sidecar(
    route: Any,
    *,
    experiment_id: str,
    policy: RouteSmoothingPolicy | None = None,
    validator: ValidationCallback | None = None,
    input_identity: Mapping[str, Any] | None = None,
    radius_sensitivity_m: Sequence[float] = (),
) -> dict[str, Any]:
    """Create an explicit research sidecar with ETA-parameterized curve samples.

    The sidecar is derived from an authoritative route object or route mapping;
    it does not mutate or replace that route.  ``validator`` is deliberately
    caller-owned because only the caller can bind RiskFrame, hard-mask,
    coverage and vessel semantics.  Without it, the result is geometry-only.
    Consumers must require ``status == ACCEPTED`` and independently verify the
    authoritative route digest before using ``motion_samples``.
    """

    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("experiment_id must be a non-empty string")
    if input_identity is not None and not isinstance(input_identity, Mapping):
        raise TypeError("input_identity must be a mapping when supplied")
    sensitivity_values = tuple(float(value) for value in radius_sensitivity_m)
    if any(not math.isfinite(value) or value <= 0 for value in sensitivity_values):
        raise ValueError("radius_sensitivity_m must contain positive finite values")
    if len(set(sensitivity_values)) != len(sensitivity_values):
        raise ValueError("radius_sensitivity_m must not contain duplicates")
    chosen_policy = policy or RouteSmoothingPolicy()

    route_id = _route_member(route, "plan_id") or _route_member(route, "route_id")
    route_id = str(route_id) if route_id is not None else None

    def fallback_sidecar(
        reason: str,
        *,
        raw_points: Sequence[Coordinate] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SIDECAR_SCHEMA_VERSION,
            "policy": SIDECAR_POLICY,
            "status": "FALLBACK",
            "applied": False,
            "research_only": True,
            "authoritative_semantics_unchanged": True,
            "experiment_id": experiment_id,
            "route_id": route_id,
            "fallback_reason": reason,
            "motion_samples": [],
            "anchor_samples": [],
            "input_identity": dict(input_identity) if input_identity is not None else None,
        }
        if raw_points:
            payload["raw_route_digest"] = _canonical_digest(
                [list(point) for point in raw_points]
            )
        payload["sidecar_digest"] = _canonical_digest(payload)
        return payload

    waypoint_values = _route_member(route, "waypoints")
    if not isinstance(waypoint_values, Sequence) or isinstance(waypoint_values, (str, bytes)):
        return fallback_sidecar("invalid_route_waypoints")

    records = tuple(_route_waypoint_record(value) for value in waypoint_values)
    if any(record is None for record in records):
        return fallback_sidecar("invalid_route_waypoint_coordinate_or_eta")
    typed_records = tuple(record for record in records if record is not None)
    raw_points = tuple(record[0] for record in typed_records)
    raw_times = tuple(record[1] for record in typed_records)
    if len(raw_points) < 2 or any(current <= previous for previous, current in pairwise(raw_times)):
        return fallback_sidecar(
            "invalid_route_eta_sequence",
            raw_points=raw_points,
        )

    raw_mapping = [{"lon": point[0], "lat": point[1]} for point in raw_points]
    result = build_route_smoothing(raw_mapping, policy=chosen_policy, validator=validator)
    sidecar: dict[str, Any] = {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "policy": SIDECAR_POLICY,
        "status": result.status,
        "applied": result.applied,
        "research_only": True,
        "authoritative_semantics_unchanged": True,
        "experiment_id": experiment_id,
        "route_id": route_id,
        "fallback_reason": result.fallback_reason,
        "raw_route_digest": result.raw_route_digest,
        "curve_digest": result.curve_digest,
        "curve_model": {
            "degree": 3,
            "basis": "clamped_cubic_bspline_equivalent_bezier_one_span",
            "coordinate_frame": "local_equirectangular_east_north_m",
            "origin": {"lon": raw_points[0][0], "lat": raw_points[0][1]},
            "earth_radius_m": EARTH_RADIUS_M,
            "knot_vector": [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
        },
        "authoritative_route": {
            "route_id": route_id,
            "route_digest": result.raw_route_digest,
            "route_digest_scope": "waypoint_coordinates_only",
            "waypoint_count": len(raw_points),
            "waypoints": [
                {"lon": point[0], "lat": point[1], "eta": _format_utc(eta)}
                for point, eta in zip(raw_points, raw_times, strict=True)
            ],
        },
        "geometry": result.to_dict(),
        "input_identity": dict(input_identity) if input_identity is not None else None,
        "validation": {
            "mode": "CALLER_SUPPLIED" if validator is not None else "GEOMETRY_ONLY",
            "risk_rechecked": False,
            "hard_mask_rechecked": False,
            "coverage_complete": False,
            "resource_evidence_complete": False,
            "production_qualified": False,
        },
        "parameterization": {
            "method": "linear_time_between_monotonic_arc_length_waypoint_anchors",
            "sample_count": 0,
            "anchor_count": len(raw_points),
            "anchor_indices": [],
        },
        "motion_samples": [],
        "anchor_samples": [],
    }

    if result.applied:
        try:
            frame, _, path_distances = _path_metric(result.points)
            del frame
            anchors = _anchor_indices(raw_points, result.points)
        except (ValueError, TypeError):
            anchors = None
            path_distances = ()
        if anchors is None or len(path_distances) != len(result.points):
            sidecar["status"] = "FALLBACK"
            sidecar["applied"] = False
            sidecar["fallback_reason"] = "non_monotonic_curve_anchors"
            sidecar["motion_samples"] = []
            sidecar["anchor_samples"] = []
        else:
            anchor_distances = tuple(path_distances[index] for index in anchors)
            samples = [
                {
                    "lon": point[0],
                    "lat": point[1],
                    "eta": _format_utc(_time_at_distance(distance, anchor_distances, raw_times)),
                }
                for point, distance in zip(result.points, path_distances, strict=True)
            ]
            sidecar["parameterization"] = {
                "method": "linear_time_between_monotonic_arc_length_waypoint_anchors",
                "sample_count": len(samples),
                "anchor_count": len(anchors),
                "anchor_indices": list(anchors),
                "path_length_m": path_distances[-1],
                "anchor_distances_m": list(anchor_distances),
            }
            sidecar["motion_samples"] = samples
            sidecar["anchor_samples"] = [
                {
                    "waypoint_index": index,
                    "sample_index": sample_index,
                    "eta": _format_utc(eta),
                    "distance_m": anchor_distances[index],
                }
                for index, (sample_index, eta) in enumerate(
                    zip(anchors, raw_times, strict=True)
                )
            ]

    if sensitivity_values:
        sensitivity = []
        for minimum_radius_m in sensitivity_values:
            sensitivity_policy = replace(
                chosen_policy,
                minimum_radius_m=minimum_radius_m,
            )
            scenario = build_route_smoothing(
                raw_mapping,
                policy=sensitivity_policy,
                validator=validator,
            )
            sensitivity.append(
                {
                    "minimum_radius_m": minimum_radius_m,
                    "status": scenario.status,
                    "applied": scenario.applied,
                    "fallback_reason": scenario.fallback_reason,
                    "selected_radius_m": [
                        segment.radius_m for segment in scenario.segments
                    ],
                    "curve_digest": scenario.curve_digest,
                }
            )
        sidecar["radius_sensitivity"] = {
            "selection": "largest_feasible_radius_per_minimum_radius_scenario",
            "scenarios": sensitivity,
        }
    sidecar["sidecar_digest"] = _canonical_digest(sidecar)
    return sidecar


__all__ = [
    "CandidateDecision",
    "CurveSegment",
    "RouteSmoothingPolicy",
    "RouteSmoothingResult",
    "build_route_smoothing",
    "build_route_smoothing_sidecar",
]
