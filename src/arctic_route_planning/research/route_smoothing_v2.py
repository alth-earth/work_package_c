"""Route-level assembly for the research-only multi-span smoothing sidecar v2.

The formal route and the existing v1 sidecar are intentionally untouched.
This module assembles locally G2 four-span cubic B-splines into a derived
research geometry and exposes a fail-closed candidate callback.  A local G2
proof is not promoted to a full-route G2 claim when any raw corner remains.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from .route_smoothing import (
    CandidateDecision,
    RouteSmoothingPolicy,
    _candidate_for_corner,
    _canonical_digest,
    _distance_to_polyline,
    _Frame,
    _normalise_points,
    _radius_candidates,
)
from .route_smoothing_multispan import KNOT_VECTOR, LocalCornerCurve, build_local_corner_curve

Coordinate = tuple[float, float]

GEOMETRY_SCHEMA_VERSION = "c.research-route-smoothing.v2"
SIDECAR_SCHEMA_VERSION = "c.research-route-smoothing-sidecar.v2"
POLICY = "authoritative_waypoints_adaptive_multispan_cubic_bspline_research_only"


@dataclass(frozen=True, slots=True)
class MultiSpanRouteSegment:
    """One locally G2 corner candidate accepted by all supplied validators."""

    corner_index: int
    turn_angle_deg: float
    selected_radius_m: float
    trim_m: float
    curve: LocalCornerCurve
    maximum_deviation_m: float
    validator_evidence: Mapping[str, Any] | None = None

    @property
    def samples(self) -> tuple[Coordinate, ...]:
        return self.curve.samples

    @property
    def curvatures_m_inv(self) -> tuple[float, ...]:
        return self.curve.curvatures_m_inv

    @property
    def minimum_radius_m(self) -> float:
        return self.curve.minimum_radius_m

    @property
    def span_convex_hulls_m(self) -> tuple[tuple[Coordinate, ...], ...]:
        controls = self.curve.control_points
        return tuple(tuple(controls[index : index + 4]) for index in range(4))

    def to_dict(self) -> dict[str, Any]:
        return {
            "corner_index": self.corner_index,
            "turn_angle_deg": self.turn_angle_deg,
            "selected_radius_m": self.selected_radius_m,
            "trim_m": self.trim_m,
            "minimum_radius_m": self.minimum_radius_m,
            "maximum_deviation_m": self.maximum_deviation_m,
            "degree": 3,
            "span_count": 4,
            "knot_vector": list(KNOT_VECTOR),
            "control_points_m": [list(point) for point in self.curve.control_points],
            "span_convex_hulls_m": [
                [list(point) for point in hull] for hull in self.span_convex_hulls_m
            ],
            "parameters": list(self.curve.parameters),
            "samples_m": [list(point) for point in self.samples],
            "curvatures_m_inv": list(self.curvatures_m_inv),
            "g2_evidence": self.curve.evidence.to_dict(),
            "validator_evidence": (
                dict(self.validator_evidence) if self.validator_evidence is not None else None
            ),
        }


MultiSpanCandidateValidator = Callable[
    [MultiSpanRouteSegment, tuple[Coordinate, ...]], CandidateDecision
]


@dataclass(frozen=True, slots=True)
class MultiSpanRouteResult:
    status: str
    points: tuple[Coordinate, ...]
    raw_points: tuple[Coordinate, ...]
    curvatures_m_inv: tuple[float, ...]
    segments: tuple[MultiSpanRouteSegment, ...]
    rejected_corners: tuple[dict[str, Any], ...]
    fallback_reason: str | None
    raw_route_digest: str
    curve_digest: str
    full_route_g2_claimed: bool = False

    @property
    def applied(self) -> bool:
        return self.status == "ACCEPTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": GEOMETRY_SCHEMA_VERSION,
            "policy": POLICY,
            "status": self.status,
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "raw_route_digest": self.raw_route_digest,
            "curve_digest": self.curve_digest,
            "raw_points": [list(point) for point in self.raw_points],
            "points": [list(point) for point in self.points],
            "curvatures_m_inv": list(self.curvatures_m_inv),
            "segments": [segment.to_dict() for segment in self.segments],
            "rejected_corners": [dict(value) for value in self.rejected_corners],
            "full_route_g2_claimed": self.full_route_g2_claimed,
        }


def _fallback(
    raw_points: tuple[Coordinate, ...],
    reason: str,
    rejected: Sequence[Mapping[str, Any]] = (),
) -> MultiSpanRouteResult:
    digest = _canonical_digest([list(point) for point in raw_points])
    return MultiSpanRouteResult(
        status="FALLBACK",
        points=raw_points,
        raw_points=raw_points,
        curvatures_m_inv=tuple(0.0 for _ in raw_points),
        segments=(),
        rejected_corners=tuple(dict(value) for value in rejected),
        fallback_reason=reason,
        raw_route_digest=digest,
        curve_digest=digest,
    )


def _sample_count(trim_m: float, policy: RouteSmoothingPolicy) -> int:
    estimated_length = max(policy.sample_spacing_m, 2.0 * trim_m)
    count = math.ceil(estimated_length / policy.sample_spacing_m) + 1
    return max(policy.minimum_curve_samples, min(policy.maximum_curve_samples, count))


def build_multispan_route_smoothing(
    points: Sequence[Mapping[str, Any]],
    *,
    policy: RouteSmoothingPolicy | None = None,
    candidate_validator: MultiSpanCandidateValidator | None = None,
    turn_direction_safe: bool = False,
) -> MultiSpanRouteResult:
    """Build deterministic geometry without mutating the route.

    The default retains the frozen R1 research-sidecar control construction.
    The formal motion facade opts into ``turn_direction_safe`` so its producer
    geometry rejects local inflections before any contract artifact is built.
    """

    chosen = policy or RouteSmoothingPolicy()
    raw_points = _normalise_points(points)
    if raw_points is None:
        return _fallback((), "invalid_coordinate")
    if len(raw_points) < 3:
        return _fallback(raw_points, "insufficient_points")
    if any(first == second for first, second in pairwise(raw_points)):
        return _fallback(raw_points, "duplicate_point")
    frame = _Frame(
        lon0=raw_points[0][0],
        lat0_rad=math.radians(raw_points[0][1]),
        cos_lat0=math.cos(math.radians(raw_points[0][1])),
    )
    if abs(frame.cos_lat0) < 1.0e-6:
        return _fallback(raw_points, "invalid_local_frame")
    local = tuple(frame.to_local(point) for point in raw_points)
    candidates = tuple(
        candidate
        for index in range(1, len(local) - 1)
        if (candidate := _candidate_for_corner(local, index, chosen)) is not None
    )
    if not candidates:
        return _fallback(raw_points, "no_eligible_corner")

    accepted: list[MultiSpanRouteSegment] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        selected: MultiSpanRouteSegment | None = None
        last_reason = "all_radius_candidates_rejected"
        for radius in _radius_candidates(candidate, chosen):
            try:
                curve = build_local_corner_curve(
                    local[candidate.index - 1],
                    local[candidate.index],
                    local[candidate.index + 1],
                    radius_m=radius,
                    sample_count=_sample_count(
                        radius * math.tan(candidate.angle_rad / 2.0), chosen
                    ),
                    turn_direction_safe=turn_direction_safe,
                )
            except ValueError:
                last_reason = "geometry_constraint"
                continue
            maximum_deviation = max(
                _distance_to_polyline(point, local) for point in curve.samples
            )
            segment = MultiSpanRouteSegment(
                corner_index=candidate.index,
                turn_angle_deg=math.degrees(candidate.angle_rad),
                selected_radius_m=radius,
                trim_m=curve.trim_m,
                curve=curve,
                maximum_deviation_m=maximum_deviation,
            )
            decision = (
                CandidateDecision(True)
                if candidate_validator is None
                else candidate_validator(segment, local)
            )
            if not isinstance(decision, CandidateDecision):
                return _fallback(raw_points, "invalid_validator_decision", rejected)
            if decision.accepted:
                selected = MultiSpanRouteSegment(
                    corner_index=segment.corner_index,
                    turn_angle_deg=segment.turn_angle_deg,
                    selected_radius_m=segment.selected_radius_m,
                    trim_m=segment.trim_m,
                    curve=segment.curve,
                    maximum_deviation_m=segment.maximum_deviation_m,
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

    blocked: set[int] = set()
    for first, second in pairwise(accepted):
        if second.corner_index != first.corner_index + 1:
            continue
        shared_length = math.dist(
            local[first.corner_index], local[second.corner_index]
        )
        if first.trim_m + second.trim_m > shared_length * chosen.maximum_overlap_fraction:
            blocked.update((first.corner_index, second.corner_index))
    if blocked:
        rejected.extend(
            {
                "corner_index": index,
                "reason": "joint_window_required_and_failed_closed",
            }
            for index in sorted(blocked)
        )
        accepted = [value for value in accepted if value.corner_index not in blocked]
    if not accepted:
        return _fallback(raw_points, "all_curves_rejected", rejected)

    assembled: list[Coordinate] = [local[0]]
    assembled_curvatures: list[float] = [0.0]
    last_raw_index = 0
    for segment in accepted:
        raw_slice = local[last_raw_index + 1 : segment.corner_index]
        assembled.extend(raw_slice)
        assembled_curvatures.extend(0.0 for _ in raw_slice)
        assembled.extend(segment.samples)
        assembled_curvatures.extend(segment.curvatures_m_inv)
        last_raw_index = segment.corner_index
    raw_tail = local[last_raw_index + 1 :]
    assembled.extend(raw_tail)
    assembled_curvatures.extend(0.0 for _ in raw_tail)
    if len(assembled) > chosen.maximum_route_points:
        return _fallback(raw_points, "route_point_limit", rejected)
    output = tuple(frame.to_geo(point) for point in assembled)
    raw_digest = _canonical_digest([list(point) for point in raw_points])
    payload = {
        "raw_route_digest": raw_digest,
        "points": [list(point) for point in output],
        "segments": [segment.to_dict() for segment in accepted],
    }
    return MultiSpanRouteResult(
        status="ACCEPTED",
        points=output,
        raw_points=raw_points,
        curvatures_m_inv=tuple(assembled_curvatures),
        segments=tuple(accepted),
        rejected_corners=tuple(rejected),
        fallback_reason=None,
        raw_route_digest=raw_digest,
        curve_digest=_canonical_digest(payload),
        full_route_g2_claimed=False,
    )


__all__ = [
    "GEOMETRY_SCHEMA_VERSION",
    "POLICY",
    "SIDECAR_SCHEMA_VERSION",
    "MultiSpanCandidateValidator",
    "MultiSpanRouteResult",
    "MultiSpanRouteSegment",
    "build_multispan_route_smoothing",
]
