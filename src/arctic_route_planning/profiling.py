"""Synthetic component profiler kept outside authoritative planning semantics."""

from __future__ import annotations

import cProfile
import hashlib
import json
import pstats
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_planning.contracts import ProvenanceKind, RiskFrame, SourceReference
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler

_T0 = datetime(2026, 2, 15, tzinfo=UTC)
_RUN_ID = "run-00000000-0000-4000-8000-000000000099"


@dataclass(frozen=True, slots=True)
class SyntheticProfileConfig:
    rows: int = 9
    cols: int = 13
    frame_count: int = 13
    spacing_degrees: float = 0.05

    def __post_init__(self) -> None:
        if self.rows < 3 or self.cols < 3 or self.frame_count < 2:
            raise ValueError("synthetic profile dimensions are too small")
        if self.spacing_degrees <= 0:
            raise ValueError("synthetic profile spacing must be positive")


def profile_synthetic_three_objective_planning(
    config: SyntheticProfileConfig | None = None,
) -> dict[str, Any]:
    """Profile real planner code on a labelled synthetic fixture."""

    config = config or SyntheticProfileConfig()
    frames = _make_frames(config)
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    planner = TimeDependentAStar(grid, sampler, vessel)
    request = PlanningRequest(
        start=(config.rows // 2, 0),
        goal=(config.rows // 2, config.cols - 1),
        departure_time=_T0,
        maximum_elapsed=timedelta(hours=config.frame_count - 1),
    )
    profiler = cProfile.Profile()
    profiler.enable()
    results = planner.plan_candidates(request)
    profiler.disable()
    stats = pstats.Stats(profiler)
    categories = {
        "risk_sampling": _category(
            stats,
            lambda filename, name: "/risk/sampler.py" in filename and name == "sample",
        ),
        "edge_traversal": _category(stats, lambda _filename, name: name == "_evaluate_edge"),
        "heuristic": _category(stats, lambda _filename, name: name == "_heuristic"),
        "objective_calculation": _category(
            stats,
            lambda filename, name: filename.endswith("/cost/model.py")
            and name in {"evaluate", "lower_bound"},
        ),
    }
    result_summaries = {
        objective.value: {
            "route_digest": _route_digest(result.nodes, result.distance_km, result.travel_hours),
            "nodes": len(result.nodes),
            "distance_km": round(result.distance_km, 9),
            "travel_hours": round(result.travel_hours, 9),
            "expanded_states": result.metrics.expanded_states,
            "generated_states": result.metrics.generated_states,
        }
        for objective, result in results.items()
    }
    return {
        "schema_version": "c.synthetic-component-profile.v1",
        "status": "EXPERIMENTAL",
        "fixture_provenance": "synthetic",
        "authoritative_route": False,
        "config": {
            "rows": config.rows,
            "cols": config.cols,
            "frame_count": config.frame_count,
            "spacing_degrees": config.spacing_degrees,
        },
        "total_profiled_seconds": round(stats.total_tt, 9),
        "categories": categories,
        "results": result_summaries,
        "edge_geometry_cache": planner.edge_geometry_cache_stats,
        "timings_are_overlapping": True,
    }


def _category(
    stats: pstats.Stats,
    predicate: Any,
) -> dict[str, int | float]:
    primitive_calls = 0
    total_calls = 0
    self_seconds = 0.0
    cumulative_seconds = 0.0
    for (filename, _line, name), values in stats.stats.items():
        if not predicate(filename, name):
            continue
        cc, nc, tt, ct, _callers = values
        primitive_calls += cc
        total_calls += nc
        self_seconds += tt
        cumulative_seconds += ct
    return {
        "primitive_calls": primitive_calls,
        "total_calls": total_calls,
        "self_seconds": round(self_seconds, 9),
        "cumulative_seconds": round(cumulative_seconds, 9),
    }


def _make_frames(config: SyntheticProfileConfig) -> tuple[RiskFrame, ...]:
    latitudes = np.linspace(70.0, 70.0 + (config.rows - 1) * config.spacing_degrees, config.rows)
    longitudes = np.linspace(20.0, 20.0 + (config.cols - 1) * config.spacing_degrees, config.cols)
    lat_mesh, lon_mesh = np.meshgrid(latitudes, longitudes, indexing="ij")
    frames: list[RiskFrame] = []
    source = SourceReference(
        source_id="synthetic-profile",
        data_id=None,
        issue_time=None,
        valid_time=None,
        version="v1",
        quality_flag="synthetic",
    )
    for index in range(config.frame_count):
        risk = np.clip(
            0.1
            + 0.03 * np.sin(np.radians(lat_mesh * 3 + index))
            + 0.02 * np.cos(np.radians(lon_mesh * 2 - index)),
            0.0,
            1.0,
        ).astype(np.float32)
        payload = xr.Dataset(
            {
                "risk_score": (("latitude", "longitude"), risk),
                "risk_level": (
                    ("latitude", "longitude"),
                    (np.floor(risk * 5) + 1).astype(np.uint8),
                ),
                "hard_mask": (
                    ("latitude", "longitude"),
                    np.zeros(risk.shape, dtype=np.bool_),
                ),
                "confidence": (
                    ("latitude", "longitude"),
                    np.full(risk.shape, 0.9, dtype=np.float32),
                ),
                "environment_speed_factor": (
                    ("latitude", "longitude"),
                    np.full(risk.shape, 0.9, dtype=np.float32),
                ),
            },
            coords={"latitude": latitudes, "longitude": longitudes},
            attrs={"crs": "EPSG:4326", "grid_id": "synthetic-profile-grid"},
        )
        frames.append(
            RiskFrame(
                schema_version="bc.risk-frame.v2",
                risk_id=f"synthetic-profile-risk-{index:02d}",
                run_id=_RUN_ID,
                scenario_id="synthetic-profile-scenario",
                corridor_id="synthetic-profile-corridor",
                vessel_profile_id="synthetic-profile-vessel",
                config_digest="0" * 64,
                model_config_digest="1" * 64,
                generation_id=0,
                valid_time=_T0 + timedelta(hours=index),
                as_of_time=_T0,
                generated_at=_T0,
                model_version="synthetic-profile-v1",
                payload=payload,
                source_summary=(source,),
                provenance=ProvenanceKind.SYNTHETIC,
            )
        )
    return tuple(frames)


def _route_digest(
    nodes: tuple[tuple[int, int], ...], distance_km: float, travel_hours: float
) -> str:
    payload = json.dumps(
        {
            "nodes": nodes,
            "distance_km": round(distance_km, 12),
            "travel_hours": round(travel_hours, 12),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
