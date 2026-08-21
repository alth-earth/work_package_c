"""Experimental BC coupling measurements over public RiskFrame contracts."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts import HOURLY_RISK_INTERVAL, RiskFrame
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig, VesselModelConfig
from arctic_route_planning.grid import Node, RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler


class PeakRssSampler:
    """Sample current RSS during one bounded planning operation."""

    def __init__(self, *, interval_seconds: float = 0.02) -> None:
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._peak_kib = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> PeakRssSampler:
        self._sample()
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join()
        self._sample()

    @property
    def peak_kib(self) -> int:
        return self._peak_kib

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._sample()

    def _sample(self) -> None:
        try:
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    self._peak_kib = max(self._peak_kib, int(line.split()[1]))
                    return
        except (FileNotFoundError, OSError, ValueError):
            return


def benchmark_planning_on_risk_frames(
    frames: Sequence[RiskFrame],
    *,
    start: Node,
    goal: Node,
    planner_config: PlannerConfig,
    vessel_config: VesselModelConfig,
    objective: ObjectiveMode = ObjectiveMode.RECOMMENDED,
    max_expansions: int = 250_000,
) -> dict[str, Any]:
    """Measure one real C search without modifying B or C business semantics."""

    if len(frames) < 2:
        raise ValueError("BC coupling benchmark requires at least two RiskFrames")
    sampler = RiskSampler(frames, max_frame_gap=HOURLY_RISK_INTERVAL)
    grid = RegularGrid.from_risk_frame(
        frames[0],
        allow_diagonal=planner_config.connectivity == 8,
    )
    planner = TimeDependentAStar(
        grid,
        sampler,
        VesselPerformanceModel.from_configuration(vessel_config),
        planner_config=planner_config,
    )
    request = PlanningRequest(
        start=start,
        goal=goal,
        departure_time=frames[0].valid_time,
        objective=objective,
        time_bucket_size=timedelta(minutes=planner_config.time_bucket_minutes),
        edge_sample_count=planner_config.edge_sample_count,
        maximum_elapsed=frames[-1].valid_time - frames[0].valid_time,
        max_expansions=max_expansions,
        use_heuristic=True,
    )
    started = time.perf_counter()
    with PeakRssSampler() as memory:
        result = planner.plan(request)
    elapsed = time.perf_counter() - started
    route_payload = {
        "nodes": result.nodes,
        "distance_km": round(result.distance_km, 12),
        "travel_hours": round(result.travel_hours, 12),
        "average_risk": round(result.average_risk, 12),
        "maximum_risk": round(result.maximum_risk, 12),
    }
    route_digest = hashlib.sha256(
        json.dumps(route_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "status": "SUCCESS",
        "objective": objective.value,
        "grid_rows": grid.shape[0],
        "grid_cols": grid.shape[1],
        "grid_nodes": grid.shape[0] * grid.shape[1],
        "risk_frame_count": len(frames),
        "planning_seconds": round(elapsed, 6),
        "peak_sampled_rss_kib": memory.peak_kib,
        "expanded_states": result.metrics.expanded_states,
        "generated_states": result.metrics.generated_states,
        "queue_peak": result.metrics.queue_peak,
        "route_nodes": len(result.nodes),
        "distance_km": result.distance_km,
        "travel_hours": result.travel_hours,
        "average_risk": result.average_risk,
        "maximum_risk": result.maximum_risk,
        "route_digest": route_digest,
        "edge_geometry_cache": planner.edge_geometry_cache_stats,
        "source_schema_versions": sorted({frame.schema_version for frame in frames}),
        "source_provenance": sorted({frame.provenance.value for frame in frames}),
    }
