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
) -> dict[str, Any]:
    """Prove expanded path hulls lie in sea cells of one regular raster model.

    Cubic B-spline spans lie inside their control-point convex hull; unchanged
    path sections lie in the convex hull of each pair of adjacent samples. A
    complete regular grid tiles its declared rectangle continuously, so
    requiring every cell touched by each expanded hull bbox to be covered and
    SEA proves containment in that *declared raster model*. It is not a claim
    about sub-cell coastlines, navigational charts, bathymetry, or UKC.
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
    hull_bboxes = []
    for hull in span_convex_hulls:
        bbox = _hull_bbox(hull)
        if bbox is None:
            return {**base, "reason": "invalid_span_convex_hull"}
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
    for lower_x, lower_y, upper_x, upper_y in hull_bboxes:
        columns_touched = _closed_index_range(
            lower_x, upper_x, origin=origin_x, size=cell_width, count=columns
        )
        rows_touched = _closed_index_range(
            lower_y, upper_y, origin=origin_y, size=cell_height, count=rows
        )
        enumerated.update(
            (row, column)
            for row in rows_touched
            for column in columns_touched
        )
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
            "raster_digest": raster_digest,
            "coordinate_frame": coordinate_frame,
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
