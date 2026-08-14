from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from arctic_route_planning.config import load_configuration
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.grid import RegularGrid

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


def test_endpoint_mapping_filters_allowed_regions_before_nearest_choice() -> None:
    configuration = load_configuration(
        CONFIG_ROOT,
        "murmansk_dikson_july_2026_retrospective_v1",
    )
    grid = RegularGrid(
        latitudes=(69.0, 69.15, 71.0, 73.55, 74.0),
        longitudes=(33.0, 33.6, 50.0, 80.4, 82.0),
    )
    mapping = map_corridor_endpoints(
        configuration,
        grid,
        hard_mask=np.zeros(grid.shape, dtype=np.bool_),
        max_adjustment_km=50.0,
    )

    assert mapping.start.node == (1, 1)
    assert mapping.goal.node == (3, 3)
    assert mapping.connected_component_size == 25
    assert mapping.to_document()["connectivity_verified"] is True


def test_main_corridor_one_degree_grid_fails_when_allowed_region_has_no_node() -> None:
    configuration = load_configuration(
        CONFIG_ROOT,
        "murmansk_dikson_july_2026_retrospective_v1",
    )
    # This is the grid currently produced by B's cover_bbox_endpoints_v1
    # policy for the 67.5..75.0 latitude extent and a nominal 1-degree step.
    grid = RegularGrid(
        latitudes=tuple(np.linspace(67.5, 75.0, 9)),
        longitudes=tuple(np.linspace(30.0, 85.0, 56)),
    )

    with pytest.raises(ValueError, match="allowed_region_has_no_grid_node"):
        map_corridor_endpoints(
            configuration,
            grid,
            hard_mask=np.zeros(grid.shape, dtype=np.bool_),
            max_adjustment_km=150.0,
        )


def test_main_corridor_half_degree_grid_maps_both_allowed_regions() -> None:
    configuration = load_configuration(
        CONFIG_ROOT,
        "murmansk_dikson_july_2026_retrospective_v1",
    )
    grid = RegularGrid(
        latitudes=tuple(np.arange(67.5, 75.0 + 0.25, 0.5)),
        longitudes=tuple(np.arange(30.0, 85.0 + 0.25, 0.5)),
    )

    mapping = map_corridor_endpoints(
        configuration,
        grid,
        hard_mask=np.zeros(grid.shape, dtype=np.bool_),
        max_adjustment_km=150.0,
    )

    assert mapping.start.resolved.longitude in {33.5, 34.0}
    assert mapping.start.resolved.latitude == 69.0
    assert mapping.goal.resolved.longitude == 80.5
    assert mapping.goal.resolved.latitude == 73.5
    assert mapping.start.allowed_region_verified
    assert mapping.goal.allowed_region_verified


def test_endpoint_mapping_rejects_goal_outside_start_component() -> None:
    configuration = load_configuration(
        CONFIG_ROOT,
        "murmansk_dikson_july_2026_retrospective_v1",
    )
    grid = RegularGrid(
        latitudes=(69.15, 70.0, 71.0, 72.0, 73.55),
        longitudes=(33.6, 50.0, 60.0, 70.0, 80.4),
    )
    mask = np.zeros(grid.shape, dtype=np.bool_)
    mask[2, :] = True

    with pytest.raises(ValueError, match="start connected component"):
        map_corridor_endpoints(
            configuration,
            grid,
            hard_mask=mask,
            max_adjustment_km=150.0,
        )
