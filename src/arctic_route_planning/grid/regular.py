"""Rectilinear latitude/longitude grid primitives used by the planner."""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from math import asin, atan2, ceil, cos, degrees, radians, sin, sqrt
from typing import TYPE_CHECKING

import numpy as np

from arctic_route_planning.domain.models import GeoPoint

if TYPE_CHECKING:
    from arctic_route_planning.contracts.models import RiskFrame

EARTH_RADIUS_KM = 6_371.0088
Node = tuple[int, int]


@dataclass(frozen=True, slots=True)
class SnapResult:
    """Result of an explicitly requested nearest-navigable-node operation."""

    node: Node
    point: GeoPoint
    adjustment_km: float


@dataclass(frozen=True, slots=True)
class RegularGrid:
    """A strictly monotonic rectilinear geographic grid."""

    latitudes: tuple[float, ...]
    longitudes: tuple[float, ...]
    allow_diagonal: bool = True

    def __post_init__(self) -> None:
        if not self.latitudes or not self.longitudes:
            raise ValueError("grid axes must not be empty")
        if not _strictly_monotonic(self.latitudes):
            raise ValueError("latitudes must be finite and strictly monotonic")
        if not _strictly_monotonic(self.longitudes):
            raise ValueError("longitudes must be finite and strictly monotonic")

    @classmethod
    def from_risk_frame(cls, frame: RiskFrame, *, allow_diagonal: bool = True) -> RegularGrid:
        payload = frame.payload
        return cls(
            latitudes=tuple(float(value) for value in payload.coords["latitude"].values),
            longitudes=tuple(float(value) for value in payload.coords["longitude"].values),
            allow_diagonal=allow_diagonal,
        )

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.latitudes), len(self.longitudes)

    def contains(self, node: Node) -> bool:
        row, column = node
        return 0 <= row < len(self.latitudes) and 0 <= column < len(self.longitudes)

    def point(self, node: Node) -> GeoPoint:
        if not self.contains(node):
            raise IndexError(f"node {node} is outside grid shape {self.shape}")
        row, column = node
        return GeoPoint(longitude=self.longitudes[column], latitude=self.latitudes[row])

    def nearest_node(self, point: GeoPoint) -> Node:
        """Return the geometrically nearest node without checking navigability."""

        row = min(
            range(len(self.latitudes)),
            key=lambda index: abs(self.latitudes[index] - point.latitude),
        )
        column = min(
            range(len(self.longitudes)),
            key=lambda index: abs(self.longitudes[index] - point.longitude),
        )
        return row, column

    def neighbors(self, node: Node) -> Iterator[Node]:
        if not self.contains(node):
            raise IndexError(f"node {node} is outside grid shape {self.shape}")
        row, column = node
        offsets = (
            (-1, -1),
            (-1, 0),
            (-1, 1),
            (0, -1),
            (0, 1),
            (1, -1),
            (1, 0),
            (1, 1),
        )
        for row_offset, column_offset in offsets:
            if not self.allow_diagonal and row_offset and column_offset:
                continue
            neighbor = row + row_offset, column + column_offset
            if self.contains(neighbor):
                yield neighbor

    def distance_km(self, start: Node, end: Node) -> float:
        return haversine_km(self.point(start), self.point(end))

    def heading_degrees(self, start: Node, end: Node) -> float:
        return initial_bearing_degrees(self.point(start), self.point(end))

    def edge_sample_points(
        self,
        start: Node,
        end: Node,
        *,
        minimum_samples: int = 3,
        max_spacing_km: float | None = None,
    ) -> tuple[GeoPoint, ...]:
        """Return endpoint-inclusive points for conservative edge inspection."""

        if end not in set(self.neighbors(start)):
            raise ValueError("edge endpoints must be neighboring nodes")
        if minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2")
        count = minimum_samples
        if max_spacing_km is not None:
            if max_spacing_km <= 0:
                raise ValueError("max_spacing_km must be positive")
            count = max(count, ceil(self.distance_km(start, end) / max_spacing_km) + 1)
        start_point = self.point(start)
        end_point = self.point(end)
        return tuple(
            GeoPoint(
                longitude=start_point.longitude
                + (end_point.longitude - start_point.longitude) * index / (count - 1),
                latitude=start_point.latitude
                + (end_point.latitude - start_point.latitude) * index / (count - 1),
            )
            for index in range(count)
        )

    def connected_component(self, start: Node, hard_mask: np.ndarray) -> frozenset[Node]:
        """Return the navigable component containing ``start``."""

        mask = self._validated_mask(hard_mask)
        if not self.contains(start) or bool(mask[start]):
            return frozenset()
        seen = {start}
        queue: deque[Node] = deque((start,))
        while queue:
            node = queue.popleft()
            for neighbor in self.neighbors(node):
                if neighbor not in seen and not bool(mask[neighbor]):
                    seen.add(neighbor)
                    queue.append(neighbor)
        return frozenset(seen)

    def snap_to_navigable(
        self,
        point: GeoPoint,
        hard_mask: np.ndarray,
        *,
        max_adjustment_km: float,
        required_component: Collection[Node] | None = None,
    ) -> SnapResult:
        """Explicitly snap to a navigable node and report the adjustment.

        The planner never invokes this method implicitly.  Callers must choose
        a finite distance limit, and may constrain the result to a previously
        computed connected component.
        """

        if max_adjustment_km < 0:
            raise ValueError("max_adjustment_km must be non-negative")
        mask = self._validated_mask(hard_mask)
        # Broadcast to full grid coordinate arrays for vectorized haversine.
        lat_grid, lon_grid = np.meshgrid(self.latitudes, self.longitudes, indexing="ij")
        distances = _haversine_km_vec(
            point.latitude,
            point.longitude,
            lat_grid,
            lon_grid,
        )
        # Permitted (row, col) set becomes a boolean mask the same shape.
        permitted = np.ones(self.shape, dtype=bool)
        if required_component is not None:
            permitted = np.zeros(self.shape, dtype=bool)
            for row, col in required_component:
                permitted[row, col] = True
        eligible = (~mask) & permitted & (distances <= max_adjustment_km)
        if not eligible.any():
            raise ValueError("no navigable node exists within max_adjustment_km")
        # Tie-break matches the original ``min(key=(distance, (row, col)))``:
        # primary distance, then row (latitude index), then column.
        row_idx, col_idx = np.where(eligible)
        eligible_distances = distances[eligible]
        order = np.lexsort((col_idx, row_idx, eligible_distances))
        chosen = order[0]
        node = (int(row_idx[chosen]), int(col_idx[chosen]))
        distance = float(eligible_distances[chosen])
        return SnapResult(node=node, point=self.point(node), adjustment_km=distance)

    def _validated_mask(self, hard_mask: np.ndarray) -> np.ndarray:
        mask = np.asarray(hard_mask)
        if mask.shape != self.shape:
            raise ValueError(f"hard_mask shape {mask.shape} does not match grid {self.shape}")
        if mask.dtype != np.bool_:
            raise TypeError("hard_mask must have boolean dtype")
        return mask


def haversine_km(start: GeoPoint, end: GeoPoint) -> float:
    """Great-circle distance in kilometres."""

    lat1 = radians(start.latitude)
    lat2 = radians(end.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(end.longitude - start.longitude)
    haversine = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(haversine)))


def _haversine_km_vec(
    lat0: float,
    lon0: float,
    lat_grid: np.ndarray,
    lon_grid: np.ndarray,
) -> np.ndarray:
    """Vectorized great-circle distance from one point to every grid node."""

    lat1 = radians(lat0)
    lats = np.radians(lat_grid)
    delta_lat = lats - lat1
    delta_lon = np.radians(lon_grid - lon0)
    haversine = np.sin(delta_lat / 2) ** 2 + np.cos(lat1) * np.cos(lats) * np.sin(
        delta_lon / 2
    ) ** 2
    return 2.0 * EARTH_RADIUS_KM * np.arcsin(np.minimum(1.0, np.sqrt(haversine)))


def initial_bearing_degrees(start: GeoPoint, end: GeoPoint) -> float:
    """Initial great-circle bearing in degrees clockwise from north."""

    lat1 = radians(start.latitude)
    lat2 = radians(end.latitude)
    delta_lon = radians(end.longitude - start.longitude)
    x = sin(delta_lon) * cos(lat2)
    y = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(delta_lon)
    return (degrees(atan2(x, y)) + 360.0) % 360.0


def heading_change_degrees(previous: float | None, current: float) -> float:
    if previous is None:
        return 0.0
    difference = abs((current - previous) % 360.0)
    return min(difference, 360.0 - difference)


def _strictly_monotonic(values: tuple[float, ...]) -> bool:
    array = np.asarray(values, dtype=float)
    if not np.all(np.isfinite(array)):
        return False
    differences = np.diff(array)
    return bool(len(array) == 1 or np.all(differences > 0) or np.all(differences < 0))
