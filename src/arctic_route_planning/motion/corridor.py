"""Continuous envelope proof inside a declared piecewise-constant raster model."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from arctic_route_planning.contracts.route_motion import CONTINUOUS_RASTER_MODEL_SCOPE

_SEA = frozenset({"SEA", "WATER", "OCEAN", "VALID_SEA", "VALID_WATER"})


def evaluate_continuous_raster_model_corridor(
    raster_metadata: Mapping[str, Any],
    raster_cells: Mapping[Any, Any],
    span_convex_hulls: Sequence[Sequence[Sequence[float]]],
    *,
    expansion_m: float,
    compute_clearance: bool = True,
    clearance_hull_count: int | None = None,
) -> dict[str, Any]:
    """Prove expanded path hulls lie in sea cells of one regular raster model.

    Cubic B-spline spans lie inside their control-point convex hull; unchanged
    path sections lie in the convex hull of each pair of adjacent samples. A
    complete regular grid tiles its declared rectangle continuously.  The
    proof enumerates only cells that actually intersect the buffered convex
    hull (or great-circle line segment) and requires every such cell to be
    covered and SEA.  It is not a claim about sub-cell coastlines, navigational
    charts, bathymetry, or UKC.
    """

    base: dict[str, Any] = {
        "accepted": False,
        "complete": False,
        "method": CONTINUOUS_RASTER_MODEL_SCOPE,
        "continuous_containment_proved": False,
        "continuous_containment_scope": CONTINUOUS_RASTER_MODEL_SCOPE,
        "navigation_grade": False,
        "bathymetry_checked": False,
        "ukc_checked": False,
        "expansion_m": expansion_m,
        "enumerated_cells": [],
        "land_cells": [],
        "unknown_cells": [],
        "missing_coverage_cells": [],
        "span_cell_counts": [],
        "span_safe_clearance_m": [],
        "containment_test": "convex_hull_rectangle_distance_with_buffer",
    }
    if not isinstance(raster_metadata, Mapping) or not isinstance(raster_cells, Mapping):
        return {**base, "reason": "invalid_raster_input"}
    parsed = _regular_grid(raster_metadata)
    if parsed is None:
        return {**base, "reason": "invalid_regular_raster_metadata"}
    origin_x, origin_y, cell_width, cell_height, rows, columns = parsed
    if raster_metadata.get("coverage_complete") is not True:
        return {**base, "reason": "raster_coverage_incomplete"}
    raster_digest = raster_metadata.get("raster_digest")
    if not isinstance(raster_digest, str) or re.fullmatch(r"[0-9a-f]{64}", raster_digest) is None:
        return {**base, "reason": "missing_raster_digest"}
    coordinate_frame = raster_metadata.get("coordinate_frame")
    if coordinate_frame not in {
        "local_equirectangular_east_north_m",
        "c_local_equirectangular_east_north_m",
    }:
        return {**base, "reason": "unsupported_coordinate_frame"}
    if isinstance(expansion_m, bool) or not isinstance(expansion_m, (int, float)):
        return {**base, "reason": "invalid_expansion"}
    expansion = float(expansion_m)
    if not math.isfinite(expansion) or expansion < 0.0:
        return {**base, "reason": "invalid_expansion"}
    hulls: list[tuple[tuple[float, float], ...]] = []
    hull_bboxes = []
    for hull in span_convex_hulls:
        points = _convex_hull(hull)
        bbox = _hull_bbox(points) if points is not None else None
        if points is None or bbox is None:
            return {**base, "reason": "invalid_span_convex_hull"}
        hulls.append(points)
        hull_bboxes.append(
            (bbox[0] - expansion, bbox[1] - expansion,
             bbox[2] + expansion, bbox[3] + expansion)
        )
    if not hull_bboxes:
        return {**base, "reason": "missing_span_convex_hulls"}
    grid_bounds = (
        origin_x, origin_y,
        origin_x + columns * cell_width,
        origin_y + rows * cell_height,
    )
    if any(
        bbox[0] < grid_bounds[0] or bbox[1] < grid_bounds[1]
        or bbox[2] > grid_bounds[2] or bbox[3] > grid_bounds[3]
        for bbox in hull_bboxes
    ):
        return {**base, "reason": "expanded_hull_outside_raster_extent"}

    enumerated: set[tuple[int, int]] = set()
    span_cell_counts: list[int] = []
    for hull, (lower_x, lower_y, upper_x, upper_y) in zip(
        hulls, hull_bboxes, strict=True
    ):
        columns_touched = _closed_index_range(
            lower_x, upper_x, origin=origin_x, size=cell_width, count=columns
        )
        rows_touched = _closed_index_range(
            lower_y, upper_y, origin=origin_y, size=cell_height, count=rows
        )
        span_cells = {
            (row, column)
            for row in rows_touched
            for column in columns_touched
            if _polygon_rectangle_distance(
                hull,
                _cell_rectangle(
                    row,
                    column,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    cell_width=cell_width,
                    cell_height=cell_height,
                ),
            ) <= expansion + 1.0e-7
        }
        span_cell_counts.append(len(span_cells))
        enumerated.update(span_cells)
    for key in sorted(enumerated):
        value = raster_cells.get(key)
        if value is None:
            base["missing_coverage_cells"].append(list(key))
            continue
        status, covered = _cell(value)
        if not covered:
            base["missing_coverage_cells"].append(list(key))
        elif status == "LAND":
            base["land_cells"].append(list(key))
        elif status != "SEA":
            base["unknown_cells"].append(list(key))
    base["enumerated_cells"] = [list(key) for key in sorted(enumerated)]
    base["span_cell_counts"] = span_cell_counts
    if clearance_hull_count is not None and (
        isinstance(clearance_hull_count, bool)
        or not isinstance(clearance_hull_count, int)
        or clearance_hull_count < 0
        or clearance_hull_count > len(hulls)
    ):
        return {**base, "reason": "invalid_clearance_hull_count"}
    clearance_hulls = (
        hulls if clearance_hull_count is None else hulls[:clearance_hull_count]
    )
    span_clearances = (
        _minimum_safe_clearances_m(
            clearance_hulls,
            raster_cells,
            origin_x=origin_x,
            origin_y=origin_y,
            cell_width=cell_width,
            cell_height=cell_height,
            rows=rows,
            columns=columns,
        )
        if compute_clearance
        else ()
    )
    minimum_clearance = (
        min(span_clearances) if span_clearances and all(
            value is not None for value in span_clearances
        ) else None
    )
    accepted = bool(enumerated) and not any(
        base[name]
        for name in ("land_cells", "unknown_cells", "missing_coverage_cells")
    )
    base.update(
        {
            "accepted": accepted,
            "complete": accepted,
            "coverage_complete": accepted,
            "hard_mask_envelope_complete": accepted,
            "continuous_containment_proved": accepted,
            "raster_resolution_containment_proved": accepted,
            "regular_grid_tiling_proved": True,
            "span_safe_clearance_m": list(span_clearances),
            "raster_digest": raster_digest,
            "coordinate_frame": coordinate_frame,
            "minimum_safe_clearance_m": minimum_clearance,
            "reason": None if accepted else "raster_cell_not_continuous_sea",
        }
    )
    return base


def _regular_grid(value: Mapping[str, Any]) -> tuple[float, float, float, float, int, int] | None:
    try:
        origin_x = float(value["origin_x_m"])
        origin_y = float(value["origin_y_m"])
        default_size = value.get("cell_size_m")
        width = float(value.get("cell_width_m", default_size))
        height = float(value.get("cell_height_m", default_size))
        rows = int(value["rows"])
        columns = int(value["cols"])
    except (KeyError, TypeError, ValueError):
        return None
    numeric = (origin_x, origin_y, width, height)
    if any(not math.isfinite(item) for item in numeric) or width <= 0 or height <= 0:
        return None
    if rows <= 0 or columns <= 0:
        return None
    return origin_x, origin_y, width, height, rows, columns


def _hull_bbox(value: Sequence[Sequence[float]]) -> tuple[float, float, float, float] | None:
    try:
        points = tuple((float(point[0]), float(point[1])) for point in value)
    except (TypeError, ValueError, IndexError):
        return None
    if len(points) < 2 or any(not all(math.isfinite(item) for item in point) for point in points):
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _convex_hull(value: Sequence[Sequence[float]]) -> tuple[tuple[float, float], ...] | None:
    """Normalize a Bezier control polygon to its convex hull."""

    try:
        points = sorted({(float(point[0]), float(point[1])) for point in value})
    except (TypeError, ValueError, IndexError):
        return None
    if len(points) < 2 or any(not all(math.isfinite(item) for item in point) for point in points):
        return None
    if len(points) == 2:
        return tuple(points)

    def cross(origin, first, second):
        return (
            (first[0] - origin[0]) * (second[1] - origin[1])
            - (first[1] - origin[1]) * (second[0] - origin[0])
        )

    lower: list[tuple[float, float]] = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 1.0e-9:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 1.0e-9:
            upper.pop()
        upper.append(point)
    result = tuple(lower[:-1] + upper[:-1])
    return result if len(result) >= 2 else None


def _cell_rectangle(
    row: int,
    column: int,
    *,
    origin_x: float,
    origin_y: float,
    cell_width: float,
    cell_height: float,
) -> tuple[tuple[float, float], ...]:
    lower_x = origin_x + column * cell_width
    lower_y = origin_y + row * cell_height
    upper_x = lower_x + cell_width
    upper_y = lower_y + cell_height
    return (
        (lower_x, lower_y),
        (upper_x, lower_y),
        (upper_x, upper_y),
        (lower_x, upper_y),
    )


def _point_to_segment_distance(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    denominator = dx * dx + dy * dy
    if denominator <= 1.0e-18:
        return math.hypot(point[0] - first[0], point[1] - first[1])
    fraction = max(
        0.0,
        min(
            1.0,
            ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
            / denominator,
        ),
    )
    return math.hypot(
        point[0] - first[0] - fraction * dx,
        point[1] - first[1] - fraction * dy,
    )


def _segments_intersect(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
    fourth: tuple[float, float],
) -> bool:
    def orientation(a, b, c):
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(value) <= 1.0e-9:
            return 0
        return 1 if value > 0.0 else -1

    def on_segment(a, b, c):
        return (
            min(a[0], b[0]) - 1.0e-9 <= c[0] <= max(a[0], b[0]) + 1.0e-9
            and min(a[1], b[1]) - 1.0e-9 <= c[1] <= max(a[1], b[1]) + 1.0e-9
        )

    one = orientation(first, second, third)
    two = orientation(first, second, fourth)
    three = orientation(third, fourth, first)
    four = orientation(third, fourth, second)
    if one == 0 and on_segment(first, second, third):
        return True
    if two == 0 and on_segment(first, second, fourth):
        return True
    if three == 0 and on_segment(third, fourth, first):
        return True
    if four == 0 and on_segment(third, fourth, second):
        return True
    return one != two and three != four


def _point_in_convex_hull(
    point: tuple[float, float], hull: Sequence[tuple[float, float]]
) -> bool:
    if len(hull) == 2:
        return _point_to_segment_distance(point, hull[0], hull[1]) <= 1.0e-9
    signs = []
    for first, second in zip(hull, (*hull[1:], hull[0]), strict=True):
        value = (second[0] - first[0]) * (point[1] - first[1]) - (
            second[1] - first[1]
        ) * (point[0] - first[0])
        if abs(value) > 1.0e-9:
            signs.append(value > 0.0)
    return not signs or all(sign == signs[0] for sign in signs)


def _polygon_rectangle_distance(
    hull: Sequence[tuple[float, float]],
    rectangle: Sequence[tuple[float, float]],
) -> float:
    """Return the distance between a convex hull/segment and a cell."""

    hull_edges = tuple(zip(hull, (*hull[1:], hull[0]), strict=True))
    rectangle_edges = tuple(
        zip(rectangle, (*rectangle[1:], rectangle[0]), strict=True)
    )
    if any(
        _segments_intersect(first, second, third, fourth)
        for first, second in hull_edges
        for third, fourth in rectangle_edges
    ):
        return 0.0
    if any(_point_in_convex_hull(point, hull) for point in rectangle):
        return 0.0
    lower_x = min(point[0] for point in rectangle)
    upper_x = max(point[0] for point in rectangle)
    lower_y = min(point[1] for point in rectangle)
    upper_y = max(point[1] for point in rectangle)
    if any(
        lower_x - 1.0e-9 <= point[0] <= upper_x + 1.0e-9
        and lower_y - 1.0e-9 <= point[1] <= upper_y + 1.0e-9
        for point in hull
    ):
        return 0.0
    distances = [
        _point_to_segment_distance(point, edge[0], edge[1])
        for point in hull
        for edge in rectangle_edges
    ]
    distances.extend(
        _point_to_segment_distance(point, edge[0], edge[1])
        for point in rectangle
        for edge in hull_edges
    )
    return min(distances) if distances else math.inf


def _closed_index_range(
    lower: float,
    upper: float,
    *,
    origin: float,
    size: float,
    count: int,
) -> range:
    """Return every closed regular cell touched by one closed interval."""

    first = math.floor((math.nextafter(lower, -math.inf) - origin) / size)
    last = math.floor((upper - origin) / size)
    return range(max(0, first), min(count - 1, last) + 1)


def _minimum_safe_clearance_m(
    hulls: Sequence[Sequence[tuple[float, float]]],
    raster_cells: Mapping[Any, Any],
    *,
    origin_x: float,
    origin_y: float,
    cell_width: float,
    cell_height: float,
    rows: int,
    columns: int,
) -> float | None:
    """Return the smallest conservative clearance over all span hulls."""

    values = _minimum_safe_clearances_m(
        hulls,
        raster_cells,
        origin_x=origin_x,
        origin_y=origin_y,
        cell_width=cell_width,
        cell_height=cell_height,
        rows=rows,
        columns=columns,
    )
    if not values or any(value is None for value in values):
        return None
    return min(values)


def _minimum_safe_clearances_m(
    hulls: Sequence[Sequence[tuple[float, float]]],
    raster_cells: Mapping[Any, Any],
    *,
    origin_x: float,
    origin_y: float,
    cell_width: float,
    cell_height: float,
    rows: int,
    columns: int,
) -> tuple[float | None, ...]:
    """Return one conservative clearance for each actual span hull.

    A single global minimum is insufficient for the adaptive trust gate: a
    safe span in open water must not inherit the clearance of a different
    span near a channel boundary.  The cell enumeration is conservative and
    exact for the declared piecewise-constant raster model.  Missing or
    unknown cells are obstacles for clearance purposes, just as they are for
    the containment gate.
    """

    grid_bounds = (
        origin_x,
        origin_y,
        origin_x + columns * cell_width,
        origin_y + rows * cell_height,
    )
    values: list[float | None] = []
    for hull in hulls:
        bbox = _hull_bbox(hull)
        if bbox is None:
            values.append(None)
            continue
        lower_x, lower_y, upper_x, upper_y = bbox
        boundary_clearance = min(
            lower_x - grid_bounds[0],
            lower_y - grid_bounds[1],
            grid_bounds[2] - upper_x,
            grid_bounds[3] - upper_y,
        )
        if boundary_clearance < 0.0:
            values.append(None)
            continue
        minimum = boundary_clearance
        # The raster boundary is itself a conservative obstacle.  Therefore
        # a non-sea cell whose rectangle is farther from the hull's bounding
        # box than that boundary cannot be the nearest obstacle.  Restricting
        # the exact polygon/rectangle test to this closed neighbourhood keeps
        # per-span clearance exact while avoiding an O(hulls * raster_cells)
        # scan for every candidate edge.
        candidate_columns = _closed_index_range(
            lower_x - minimum,
            upper_x + minimum,
            origin=origin_x,
            size=cell_width,
            count=columns,
        )
        candidate_rows = _closed_index_range(
            lower_y - minimum,
            upper_y + minimum,
            origin=origin_y,
            size=cell_height,
            count=rows,
        )
        for row in candidate_rows:
            for column in candidate_columns:
                status, covered = _cell(raster_cells.get((row, column)))
                if covered and status == "SEA":
                    continue
                rectangle = _cell_rectangle(
                    row,
                    column,
                    origin_x=origin_x,
                    origin_y=origin_y,
                    cell_width=cell_width,
                    cell_height=cell_height,
                )
                # The rectangle-bbox lower bound is cheap and lets us skip
                # exact edge tests that cannot improve the current minimum.
                rectangle_bbox = _hull_bbox(rectangle)
                if rectangle_bbox is None:
                    continue
                bbox_distance = _bbox_distance(bbox, rectangle_bbox)
                if bbox_distance > minimum + 1.0e-7:
                    continue
                minimum = min(minimum, _polygon_rectangle_distance(hull, rectangle))
                if minimum <= 0.0:
                    break
            if minimum <= 0.0:
                break
        values.append(
            minimum if math.isfinite(minimum) and minimum >= 0.0 else None
        )
    return tuple(values)


def _bbox_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """Return the Euclidean distance between two axis-aligned boxes."""

    if first[2] < second[0]:
        dx = second[0] - first[2]
    elif second[2] < first[0]:
        dx = first[0] - second[2]
    else:
        dx = 0.0
    if first[3] < second[1]:
        dy = second[1] - first[3]
    elif second[3] < first[1]:
        dy = first[1] - second[3]
    else:
        dy = 0.0
    return math.hypot(dx, dy)


def _cell(value: Any) -> tuple[str, bool]:
    covered = True
    status = value
    if isinstance(value, Mapping):
        covered = value.get("coverage_complete", value.get("covered", False)) is True
        status = value.get("status", value.get("classification"))
    normalized = str(status).upper() if status is not None else "UNKNOWN"
    if normalized in _SEA:
        return "SEA", covered
    if normalized in {"LAND", "COAST", "OBSTACLE"}:
        return "LAND", covered
    return "UNKNOWN", covered


__all__ = ["evaluate_continuous_raster_model_corridor"]
