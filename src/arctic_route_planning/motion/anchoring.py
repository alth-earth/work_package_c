"""Stable formal helpers for authoritative waypoint ETA anchoring.

The legacy nearest-sample helper remains available for compatibility.  Formal
motion qualification uses the projection API below so a skipped waypoint is
represented by an exact monotone point on the candidate curve rather than by
whichever 250 m sample happens to be closest.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from arctic_route_planning.research.route_smoothing import (
    _anchor_indices as find_anchor_indices,
)
from arctic_route_planning.research.route_smoothing import _path_metric
from arctic_route_planning.research.route_smoothing import (
    _path_metric as path_metric,
)
from arctic_route_planning.research.route_smoothing import (
    _time_at_distance as time_at_distance,
)

Coordinate = tuple[float, float]


@dataclass(frozen=True, slots=True)
class AnchorProjection:
    """One monotone orthogonal projection of a raw waypoint onto a curve."""

    waypoint_index: int
    segment_index: int
    fraction: float
    arc_length_m: float
    lateral_distance_m: float


def project_waypoint_anchors(
    raw_points: Sequence[Coordinate],
    path_points: Sequence[Coordinate],
) -> tuple[AnchorProjection, ...] | None:
    """Project every raw waypoint onto an ordered path in a local metric.

    The search is monotone in path arc length and permits multiple skipped
    waypoints on the same curve segment.  It returns ``None`` for a
    malformed/degenerate path or when two successive ETA anchors cannot be
    placed at strictly increasing arc positions.  The returned distances are
    local-frame distances; callers may use the segment/fraction pair to
    insert exact geographic samples.
    """

    if len(raw_points) < 2 or len(path_points) < 2:
        return None
    try:
        frame, local_path, path_distances = _path_metric(tuple(path_points))
        local_raw = tuple(frame.to_local(point) for point in raw_points)
    except (IndexError, TypeError, ValueError):
        return None
    projections: list[AnchorProjection] = []
    search_segment = 0
    for raw_index, raw_point in enumerate(local_raw):
        if raw_index == 0:
            projections.append(AnchorProjection(raw_index, 0, 0.0, 0.0, 0.0))
            search_segment = 0
            continue
        if raw_index == len(local_raw) - 1:
            last = len(local_path) - 1
            if search_segment > last - 1:
                return None
            distance = path_distances[-1]
            lateral = math.hypot(
                raw_point[0] - local_path[-1][0],
                raw_point[1] - local_path[-1][1],
            )
            projections.append(
                AnchorProjection(raw_index, last - 1, 1.0, distance, lateral)
            )
            break

        last_segment = len(local_path) - 2
        if search_segment > last_segment:
            return None
        best: AnchorProjection | None = None
        best_distance = math.inf
        for segment_index in range(search_segment, last_segment + 1):
            first = local_path[segment_index]
            second = local_path[segment_index + 1]
            dx = second[0] - first[0]
            dy = second[1] - first[1]
            denominator = dx * dx + dy * dy
            if denominator <= 1.0e-18 or not math.isfinite(denominator):
                return None
            fraction = max(
                0.0,
                min(
                    1.0,
                    ((raw_point[0] - first[0]) * dx + (raw_point[1] - first[1]) * dy)
                    / denominator,
                ),
            )
            projected = (first[0] + fraction * dx, first[1] + fraction * dy)
            lateral = math.hypot(
                raw_point[0] - projected[0], raw_point[1] - projected[1]
            )
            arc = path_distances[segment_index] + fraction * math.sqrt(denominator)
            if lateral < best_distance - 1.0e-9:
                best_distance = lateral
                best = AnchorProjection(
                    raw_index, segment_index, fraction, arc, lateral
                )
        if best is None:
            return None
        if projections and best.arc_length_m <= projections[-1].arc_length_m + 1.0e-6:
            return None
        projections.append(best)
        search_segment = best.segment_index
    return tuple(projections)

__all__ = [
    "AnchorProjection",
    "find_anchor_indices",
    "path_metric",
    "project_waypoint_anchors",
    "time_at_distance",
]
