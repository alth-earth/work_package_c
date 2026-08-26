#!/usr/bin/env python3
"""Benchmark SMO-A* (shared-memoization) vs baseline A* on Winter data.

Compares plan_candidates(shared_edge_evaluation=True) against the default
plan_candidates(shared_edge_evaluation=False) on real Winter risk frames.
Verifies route identity and measures wall-time, cache statistics, and RSS.

Usage:
    cd work_package_c
    UV_OFFLINE=1 .mamba-env/bin/uv run --locked python scripts/benchmark_smo_astar.py \
        --commit  /path/to/risk-window-commit.json \
        --start 5 7 --goal 26 2
"""
from __future__ import annotations

import argparse
import gc
import json
import platform
import resource
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from arctic_route_planning.contracts import risk_frame_from_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler


def _load_frames(commit_path: Path) -> tuple[object, ...]:
    """Load RiskFrames from a risk-window-commit and its frame store."""
    doc = json.loads(commit_path.read_text(encoding="utf-8"))
    schema = doc.get("schema_version", "")
    if schema != "bc.risk-window-commit.v1":
        raise ValueError(f"unsupported schema: {schema}")
    # The commit stores frame references (risk_id); actual frame data
    # lives in sibling ../frames/<risk_id>.json files.
    frames_dir = commit_path.parent.parent / "frames"
    frames = []
    for ref in doc["frames"]:
        risk_id = ref["risk_id"]
        frame_path = frames_dir / f"{risk_id}.json"
        frame_doc = json.loads(frame_path.read_text(encoding="utf-8"))
        frames.append(risk_frame_from_document(frame_doc))
    return tuple(frames)


def _route_digest(result) -> str:
    nodes = ",".join(f"{r},{c}" for r, c in result.nodes)
    eta_str = "|".join(s.eta.isoformat() for s in result.steps)
    return f"{nodes};{eta_str};{result.total_cost_hours:.10f}"


def _rss_kib() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_maxrss


def _run_one(
    planner: TimeDependentAStar,
    request: PlanningRequest,
    *,
    shared: bool,
) -> dict:
    gc.collect()
    rss_before = _rss_kib()
    t0 = time.perf_counter()
    results = planner.plan_candidates(request, shared_edge_evaluation=shared)
    elapsed = time.perf_counter() - t0
    rss_after = _rss_kib()

    digests = {m: _route_digest(r) for m, r in results.items()}
    total_hits = sum(r.metrics.traversal_cache_hits for r in results.values())
    total_misses = sum(r.metrics.traversal_cache_misses for r in results.values())
    total_expanded = sum(r.metrics.expanded_states for r in results.values())
    total_compute_ms = sum(r.metrics.compute_ms for r in results.values())

    return {
        "shared": shared,
        "wall_seconds": elapsed,
        "total_compute_ms": total_compute_ms,
        "total_expanded": total_expanded,
        "cache_hits": total_hits,
        "cache_misses": total_misses,
        "rss_kib_before": rss_before,
        "rss_kib_after": rss_after,
        "digests": digests,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", type=Path, required=True, help="risk-window-commit JSON")
    ap.add_argument("--start", nargs=2, type=int, required=True, help="start node row col")
    ap.add_argument("--goal", nargs=2, type=int, required=True, help="goal node row col")
    ap.add_argument("--repetitions", type=int, default=3, help="repetitions per mode")
    ap.add_argument("--output", type=Path, default=None, help="output JSON")
    args = ap.parse_args()

    frames = _load_frames(args.commit)
    print(f"Loaded {len(frames)} frames from {args.commit.name}")

    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    planner = TimeDependentAStar(grid, sampler, vessel, planner_config=PlannerConfig())

    departure = datetime(2026, 2, 22, 0, 0, tzinfo=UTC)
    request = PlanningRequest(
        start=tuple(args.start),
        goal=tuple(args.goal),
        departure_time=departure,
        objective=ObjectiveMode.RECOMMENDED,
        max_expansions=250_000,
    )

    print(f"Start={request.start} Goal={request.goal} Departure={departure.isoformat()}")
    print(f"Repetitions: {args.repetitions}")
    print()

    baseline_runs = []
    shared_runs = []

    for i in range(args.repetitions):
        print(f"--- Repetition {i + 1}/{args.repetitions} ---")
        r_base = _run_one(planner, request, shared=False)
        baseline_runs.append(r_base)
        print(f"  Baseline:  {r_base['wall_seconds']:.3f}s  expanded={r_base['total_expanded']}")

        r_shared = _run_one(planner, request, shared=True)
        shared_runs.append(r_shared)
        print(
            f"  SMO-A*:    {r_shared['wall_seconds']:.3f}s"
            f"  expanded={r_shared['total_expanded']}"
            f"  hits={r_shared['cache_hits']}"
            f"  misses={r_shared['cache_misses']}"
        )

        # Route identity check
        for mode in ObjectiveMode:
            if r_base["digests"][mode] != r_shared["digests"][mode]:
                print(f"  ** ROUTE MISMATCH for {mode}! **")
                print(f"     baseline: {r_base['digests'][mode][:120]}")
                print(f"     shared:   {r_shared['digests'][mode][:120]}")
                return 1
        print(f"  Route identity: PASS (all {len(ObjectiveMode)} objectives)")

    # Summary
    from statistics import mean, median

    base_walls = [r["wall_seconds"] for r in baseline_runs]
    shared_walls = [r["wall_seconds"] for r in shared_runs]
    base_median = median(base_walls)
    shared_median = median(shared_walls)
    improvement_pct = (1.0 - shared_median / base_median) * 100.0 if base_median > 0 else 0.0

    total_hits = shared_runs[-1]["cache_hits"]
    total_misses = shared_runs[-1]["cache_misses"]
    _total_ops = total_hits + total_misses
    hit_rate = (total_hits / _total_ops * 100.0) if _total_ops > 0 else 0.0

    print()
    print("=" * 70)
    print("SMO-A* Benchmark Summary")
    print("=" * 70)
    print(f"Baseline A* median wall: {base_median:.3f}s  (mean {mean(base_walls):.3f}s)")
    print(f"SMO-A*     median wall: {shared_median:.3f}s  (mean {mean(shared_walls):.3f}s)")
    print(f"Wall-time improvement:   {improvement_pct:+.2f}%")
    _ops = total_hits + total_misses
    print(f"Cache hit rate:          {hit_rate:.1f}%  ({total_hits} hits / {_ops} total)")
    print("Route identity:          PASS (all repetitions)")
    print(f"Baseline RSS:            {baseline_runs[-1]['rss_kib_after']} KiB")
    print(f"SMO-A* RSS:              {shared_runs[-1]['rss_kib_after']} KiB")
    _expanded_match = baseline_runs[-1]["total_expanded"] == shared_runs[-1]["total_expanded"]
    print(f"Expanded states match:   {_expanded_match}")
    print()

    if args.output:
        output = {
            "algorithm": "smo-astar",
            "baseline_median_wall_seconds": base_median,
            "shared_median_wall_seconds": shared_median,
            "wall_time_improvement_pct": improvement_pct,
            "cache_hit_rate_pct": hit_rate,
            "cache_hits": total_hits,
            "cache_misses": total_misses,
            "route_identity": "PASS",
            "baseline_runs": [
                {k: v for k, v in r.items() if k != "digests"}
                for r in baseline_runs
            ],
            "shared_runs": [
                {k: v for k, v in r.items() if k != "digests"}
                for r in shared_runs
            ],
            "platform": platform.platform(),
            "python": sys.version,
            "frame_count": len(frames),
            "start": list(args.start),
            "goal": list(args.goal),
        }
        args.output.write_text(json.dumps(output, indent=2))
        print(f"Results written to {args.output}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
