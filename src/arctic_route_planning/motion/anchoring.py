"""Stable formal facade for authoritative waypoint ETA anchoring helpers."""

from arctic_route_planning.research.route_smoothing import (
    _anchor_indices as find_anchor_indices,
)
from arctic_route_planning.research.route_smoothing import (
    _path_metric as path_metric,
)
from arctic_route_planning.research.route_smoothing import (
    _time_at_distance as time_at_distance,
)

__all__ = ["find_anchor_indices", "path_metric", "time_at_distance"]
