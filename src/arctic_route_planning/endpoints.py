"""Auditable public endpoint mapping for formal planning callers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from arctic_route_planning.config import PlanningConfiguration
from arctic_route_planning.contracts import RiskFrame
from arctic_route_planning.domain import GeoPoint
from arctic_route_planning.grid import Node, RegularGrid, SnapResult, haversine_km


@dataclass(frozen=True, slots=True)
class MappedEndpoint:
    """One bounded, navigable and allowed-region endpoint resolution."""

    node: Node
    requested: GeoPoint
    resolved: GeoPoint
    adjustment_km: float
    max_adjustment_km: float
    allowed_region_verified: bool = True

    def to_document(self) -> dict[str, Any]:
        return {
            "node": list(self.node),
            "requested": [self.requested.longitude, self.requested.latitude],
            "resolved": [self.resolved.longitude, self.resolved.latitude],
            "adjustment_km": self.adjustment_km,
            "max_adjustment_km": self.max_adjustment_km,
            "snap_applied": self.adjustment_km > 1e-9,
            "allowed_region_verified": self.allowed_region_verified,
        }


@dataclass(frozen=True, slots=True)
class EndpointMapping:
    """Pair of endpoint resolutions proven to share a navigable component."""

    start: MappedEndpoint
    goal: MappedEndpoint
    connected_component_size: int
    grid_shape: tuple[int, int]

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": "c.endpoint-mapping.v1",
            "start": self.start.to_document(),
            "goal": self.goal.to_document(),
            "connected_component_size": self.connected_component_size,
            "grid_shape": list(self.grid_shape),
            "hard_mask_verified": True,
            "connectivity_verified": True,
        }


def map_corridor_endpoints(
    configuration: PlanningConfiguration,
    frame_or_grid: RiskFrame | RegularGrid,
    *,
    max_adjustment_km: float,
    hard_mask: np.ndarray | None = None,
) -> EndpointMapping:
    """Map corridor endpoints with explicit distance and navigation checks.

    A :class:`RiskFrame` supplies both the grid and its hard mask.  A caller
    that already owns a :class:`RegularGrid` must pass the corresponding mask
    explicitly so that no endpoint can be mapped without the navigation audit.
    """

    if not math.isfinite(max_adjustment_km) or max_adjustment_km < 0:
        raise ValueError("max_adjustment_km must be finite and non-negative")
    if isinstance(frame_or_grid, RiskFrame):
        if hard_mask is not None:
            raise ValueError("hard_mask must not be supplied with a RiskFrame")
        grid = RegularGrid.from_risk_frame(
            frame_or_grid,
            allow_diagonal=configuration.planner.connectivity == 8,
        )
        mask = np.asarray(frame_or_grid.payload["hard_mask"].values, dtype=np.bool_)
    elif isinstance(frame_or_grid, RegularGrid):
        grid = frame_or_grid
        if hard_mask is None:
            raise ValueError("hard_mask is required with a RegularGrid")
        mask = np.asarray(hard_mask)
        if grid.allow_diagonal != (configuration.planner.connectivity == 8):
            raise ValueError("grid connectivity does not match PlanningConfiguration")
    else:
        raise TypeError("frame_or_grid must be RiskFrame or RegularGrid")
    if mask.shape != grid.shape:
        raise ValueError(f"hard_mask shape {mask.shape} does not match grid {grid.shape}")
    if mask.dtype != np.bool_:
        raise TypeError("hard_mask must have boolean dtype")

    start_requested = GeoPoint(
        configuration.corridor.start.longitude,
        configuration.corridor.start.latitude,
    )
    goal_requested = GeoPoint(
        configuration.corridor.destination.longitude,
        configuration.corridor.destination.latitude,
    )
    start_snap = _snap_inside_allowed_region(
        grid,
        start_requested,
        mask,
        configuration.corridor.start_allowed_region,
        max_adjustment_km=max_adjustment_km,
        role="start",
    )
    component = grid.connected_component(start_snap.node, mask)
    if not component:
        raise ValueError("mapped start has no navigable connected component")
    try:
        goal_snap = _snap_inside_allowed_region(
            grid,
            goal_requested,
            mask,
            configuration.corridor.destination_allowed_region,
            max_adjustment_km=max_adjustment_km,
            role="goal",
            required_component=component,
        )
    except ValueError as exc:
        raise ValueError(
            "start endpoint mapped by "
            f"{start_snap.adjustment_km:.3f} km, but goal mapping failed: {exc}"
        ) from exc
    if start_snap.node == goal_snap.node:
        raise ValueError("bounded endpoint mapping resolved start and goal to one node")

    return EndpointMapping(
        start=_mapped_endpoint(start_requested, start_snap, max_adjustment_km),
        goal=_mapped_endpoint(goal_requested, goal_snap, max_adjustment_km),
        connected_component_size=len(component),
        grid_shape=grid.shape,
    )


def _snap_inside_allowed_region(
    grid: RegularGrid,
    requested: GeoPoint,
    hard_mask: np.ndarray,
    region: Any,
    *,
    max_adjustment_km: float,
    role: str,
    required_component: frozenset[Node] | None = None,
) -> SnapResult:
    candidates: list[tuple[float, Node]] = []
    region_node_count = 0
    navigable_region_node_count = 0
    for row in range(grid.shape[0]):
        for column in range(grid.shape[1]):
            node = (row, column)
            point = grid.point(node)
            if not region.contains(point):
                continue
            region_node_count += 1
            if bool(hard_mask[node]):
                continue
            navigable_region_node_count += 1
            if required_component is not None and node not in required_component:
                continue
            distance = haversine_km(requested, point)
            if distance <= max_adjustment_km:
                candidates.append((distance, node))
    if region_node_count == 0:
        raise ValueError(
            "allowed_region_has_no_grid_node: "
            f"{role}_allowed_region contains no grid node"
        )
    if navigable_region_node_count == 0:
        raise ValueError(
            "allowed_region_has_no_navigable_grid_node: "
            f"{role}_allowed_region contains no navigable grid node"
        )
    if not candidates:
        qualifier = " in the start connected component" if required_component else ""
        raise ValueError(
            "allowed_region_has_no_reachable_grid_node: "
            f"{role}_allowed_region has no navigable node{qualifier} "
            "within max_adjustment_km"
        )
    distance, node = min(candidates, key=lambda item: (item[0], item[1]))
    return SnapResult(node=node, point=grid.point(node), adjustment_km=distance)


def _mapped_endpoint(
    requested: GeoPoint,
    resolved: SnapResult,
    max_adjustment_km: float,
) -> MappedEndpoint:
    return MappedEndpoint(
        node=resolved.node,
        requested=requested,
        resolved=resolved.point,
        adjustment_km=resolved.adjustment_km,
        max_adjustment_km=max_adjustment_km,
    )


__all__ = ["EndpointMapping", "MappedEndpoint", "map_corridor_endpoints"]
