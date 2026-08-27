#!/usr/bin/env python3
"""Paired, isolated benchmark for the SMO-A* traversal cache.

The default planner remains the control.  This runner starts one fresh worker
process for each control/candidate cell, alternates the order of the two
cells, binds both workers to one CPU, and records identity/resource evidence
alongside the route semantics.  It deliberately bypasses formal ingress so
the search cost can be measured without re-running artifact publication.

Usage::

    UV_OFFLINE=1 .mamba-env/bin/uv run --locked python \
      scripts/benchmark_smo_astar.py \
      --commit /path/to/risk-window-commit.json \
      --start 5 7 --goal 26 2 --config-root configs \
      --repetitions 5 --output /tmp/smo-benchmark.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.config import load_planner_config, load_vessel_model_config
from arctic_route_planning.contracts import risk_frame_from_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler

OBJECTIVES = tuple(ObjectiveMode)


def _load_frames(commit_path: Path) -> tuple[object, ...]:
    """Load the complete committed window referenced by *commit_path*."""

    doc = json.loads(commit_path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != "bc.risk-window-commit.v1":
        raise ValueError(f"unsupported schema: {doc.get('schema_version')!r}")
    frames_dir = commit_path.parent.parent / "frames"
    frames = []
    for ref in doc["frames"]:
        risk_id = ref["risk_id"]
        frame_path = frames_dir / f"{risk_id}.json"
        frames.append(
            risk_frame_from_document(json.loads(frame_path.read_text(encoding="utf-8")))
        )
    return tuple(frames)


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "value"):
        enum_value = value.value
        if isinstance(enum_value, str):
            return enum_value
    return value


def _route_semantic_payload(result: Any) -> dict[str, object]:
    """Return only business route fields, excluding runtime counters/IDs."""

    return {
        "objective": result.objective.value,
        "nodes": [list(node) for node in result.nodes],
        "total_cost_hours": result.total_cost_hours,
        "distance_km": result.distance_km,
        "travel_hours": result.travel_hours,
        "average_risk": result.average_risk,
        "maximum_risk": result.maximum_risk,
        "minimum_confidence": result.minimum_confidence,
        "source_risk_ids": list(result.source_risk_ids),
        "steps": [
            {
                "node": list(step.node),
                "longitude": step.longitude,
                "latitude": step.latitude,
                "eta": step.eta.astimezone(UTC).isoformat(),
                "incoming_heading_degrees": step.incoming_heading_degrees,
                "recommended_speed_knots": step.recommended_speed_knots,
                "edge_distance_km": step.edge_distance_km,
                "edge_risk_score": step.edge_risk_score,
                "edge_maximum_risk": step.edge_maximum_risk,
                "edge_confidence": step.edge_confidence,
                "edge_cost": _jsonable(step.edge_cost),
                "source_risk_ids": list(step.source_risk_ids),
            }
            for step in result.steps
        ],
    }


def _route_record(result: Any) -> dict[str, object]:
    payload = _route_semantic_payload(result)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        "semantic_digest": hashlib.sha256(encoded).hexdigest(),
        "semantic": payload,
        "metrics": _jsonable(result.metrics),
    }


def _rss_peak_kib() -> int:
    import resource

    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value // 1024 if sys.platform == "darwin" else value


def _proc_swap_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmSwap:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _set_cpu(cpu: int | None) -> None:
    if cpu is None or not hasattr(os, "sched_setaffinity"):
        return
    os.sched_setaffinity(0, {cpu})


def _build_planner(
    frames: tuple[object, ...],
    *,
    config_root: Path,
    vessel_profile_id: str,
) -> TimeDependentAStar:
    sampler = RiskSampler(frames, max_frame_gap=None)
    planner_config = load_planner_config(config_root)
    vessel_config = load_vessel_model_config(config_root, vessel_profile_id)
    grid = RegularGrid.from_risk_frame(
        frames[0], allow_diagonal=planner_config.connectivity == 8
    )
    vessel = VesselPerformanceModel.from_configuration(vessel_config)
    return TimeDependentAStar(
        grid,
        sampler,
        vessel,
        planner_config=planner_config,
    )


def _worker(args: argparse.Namespace) -> int:
    _set_cpu(args.cpu)
    frames = _load_frames(args.commit)
    if not frames:
        raise ValueError("committed window has no frames")
    first_doc = json.loads(
        ((args.commit.parent.parent / "frames") / f"{frames[0].risk_id}.json").read_text(
            encoding="utf-8"
        )
    )
    vessel_profile_id = first_doc["vessel_profile_id"]
    planner = _build_planner(
        frames,
        config_root=args.config_root,
        vessel_profile_id=vessel_profile_id,
    )
    departure = datetime.fromisoformat(args.departure.replace("Z", "+00:00")).astimezone(UTC)
    order = tuple(ObjectiveMode(value) for value in args.objective_order)
    request = PlanningRequest(
        start=tuple(args.start),
        goal=tuple(args.goal),
        departure_time=departure,
        objective=order[0],
        max_expansions=args.max_expansions,
    )
    swap_before = _proc_swap_kib()
    started = time.perf_counter()
    results = planner.plan_candidates(
        request,
        objectives=order,
        shared_edge_evaluation=args.mode == "shared",
    )
    elapsed = time.perf_counter() - started
    swap_after = _proc_swap_kib()
    routes = {mode.value: _route_record(result) for mode, result in results.items()}
    payload = {
        "mode": args.mode,
        "objective_order": [mode.value for mode in order],
        "wall_seconds": elapsed,
        "peak_rss_kib": _rss_peak_kib(),
        "process_swap_before_kib": swap_before,
        "process_swap_after_kib": swap_after,
        "process_swap_delta_kib": (
            swap_after - swap_before
            if swap_before is not None and swap_after is not None
            else None
        ),
        "routes": routes,
        "traversal_cache": planner.traversal_cache_stats,
        "frame_count": len(frames),
        "risk_identity": _jsonable(planner.risk_identity),
    }
    args.worker_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _run_worker(
    *,
    args: argparse.Namespace,
    mode: str,
    order: tuple[str, ...],
    output_path: Path,
    cpu: int | None,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--mode",
        mode,
        "--commit",
        str(args.commit),
        "--config-root",
        str(args.config_root),
        "--start",
        str(args.start[0]),
        str(args.start[1]),
        "--goal",
        str(args.goal[0]),
        str(args.goal[1]),
        "--departure",
        args.departure,
        "--max-expansions",
        str(args.max_expansions),
        "--worker-output",
        str(output_path),
        "--cpu",
        str(cpu) if cpu is not None else "-1",
        "--objective-order",
        *order,
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=args.worker_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{mode} worker exceeded timeout={args.worker_timeout_seconds}s"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"{mode} worker failed with exit {completed.returncode}:\n"
            f"stdout={completed.stdout[-4000:]}\n"
            f"stderr={completed.stderr[-4000:]}"
        )
    return json.loads(output_path.read_text(encoding="utf-8"))


def _nearest_rank(values: list[float], quantile: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _git_metadata(repo_root: Path) -> dict[str, object]:
    def run(*command: str) -> str:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    lock_digest = hashlib.sha256((repo_root / "uv.lock").read_bytes()).hexdigest()
    script_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "git_sha": run("git", "rev-parse", "HEAD"),
        "git_dirty": bool(run("git", "status", "--porcelain")),
        "uv_lock_sha256": lock_digest,
        "runner_sha256": script_digest,
    }


def _validate_route_identity(
    baseline: dict[str, object],
    shared: dict[str, object],
) -> None:
    base_routes = baseline["routes"]
    shared_routes = shared["routes"]
    if not isinstance(base_routes, dict) or not isinstance(shared_routes, dict):
        raise ValueError("worker route payload is malformed")
    for objective in OBJECTIVES:
        base_digest = base_routes[objective.value]["semantic_digest"]
        shared_digest = shared_routes[objective.value]["semantic_digest"]
        if base_digest != shared_digest:
            raise ValueError(f"route semantic mismatch for {objective.value}")
    if baseline.get("risk_identity") != shared.get("risk_identity"):
        raise ValueError("control/candidate risk identity mismatch")


def _main(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        raise ValueError("--repetitions must be positive")
    if args.worker_timeout_seconds <= 0:
        raise ValueError("--worker-timeout-seconds must be positive")
    if len(args.objective_order) != len(OBJECTIVES):
        raise ValueError(f"--objective-order must contain {len(OBJECTIVES)} objectives")
    if set(args.objective_order) != {mode.value for mode in OBJECTIVES}:
        raise ValueError("--objective-order must contain each objective exactly once")
    if not args.departure:
        raise ValueError("--departure is required for reproducible identity")
    cpu = min(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    baseline_runs: list[dict[str, object]] = []
    shared_runs: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="smo-astar-workers-") as temp_dir:
        temp_root = Path(temp_dir)
        for repetition in range(args.repetitions):
            order = ("baseline", "shared") if repetition % 2 == 0 else ("shared", "baseline")
            pair: dict[str, dict[str, object]] = {}
            for mode in order:
                output_path = temp_root / f"r{repetition + 1}-{mode}.json"
                pair[mode] = _run_worker(
                    args=args,
                    mode=mode,
                    order=tuple(args.objective_order),
                    output_path=output_path,
                    cpu=cpu,
                )
            _validate_route_identity(pair["baseline"], pair["shared"])
            baseline_runs.append(pair["baseline"])
            shared_runs.append(pair["shared"])
            print(
                f"pair {repetition + 1}/{args.repetitions}: "
                f"baseline={pair['baseline']['wall_seconds']:.3f}s "
                f"shared={pair['shared']['wall_seconds']:.3f}s "
                f"hits={pair['shared']['traversal_cache']['hits']} route=PASS",
                flush=True,
            )

    baseline_walls = [float(run["wall_seconds"]) for run in baseline_runs]
    shared_walls = [float(run["wall_seconds"]) for run in shared_runs]
    baseline_median = statistics.median(baseline_walls)
    shared_median = statistics.median(shared_walls)
    improvement = (1.0 - shared_median / baseline_median) * 100.0
    total_hits = sum(int(run["traversal_cache"]["hits"]) for run in shared_runs)
    total_misses = sum(int(run["traversal_cache"]["misses"]) for run in shared_runs)
    hit_rate = (
        total_hits / (total_hits + total_misses) * 100.0
        if total_hits + total_misses
        else 0.0
    )
    baseline_p95 = _nearest_rank(baseline_walls, 0.95)
    shared_p95 = _nearest_rank(shared_walls, 0.95)
    rss_values = [
        float(shared_run["peak_rss_kib"]) / float(baseline_run["peak_rss_kib"])
        for baseline_run, shared_run in zip(baseline_runs, shared_runs, strict=True)
        if float(baseline_run["peak_rss_kib"]) > 0
    ]
    rss_ratio = statistics.median(rss_values) if rss_values else float("nan")
    swap_values = [
        run["process_swap_delta_kib"]
        for run in (*baseline_runs, *shared_runs)
    ]
    swap_measured_zero = all(value == 0 for value in swap_values)
    gate_checks = {
        "route_identity": True,
        "wall_improvement_ge_15pct": improvement >= 15.0,
        "p95_regression_le_5pct": (
            shared_p95 <= baseline_p95 * 1.05 if baseline_p95 == baseline_p95 else False
        ),
        "cache_hit_rate_ge_50pct": hit_rate >= 50.0,
        "rss_ratio_le_1_10": rss_ratio <= 1.10 if rss_ratio == rss_ratio else False,
        "swap_measured_and_zero": swap_measured_zero,
    }
    gate_verdict = "PASS" if all(gate_checks.values()) else "FAIL"
    payload = {
        "algorithm": "smo-astar",
        "status": "COMPLETED",
        "gate_verdict": gate_verdict,
        "gate_checks": gate_checks,
        "provenance": _git_metadata(Path(__file__).resolve().parents[1]),
        "platform": platform.platform(),
        "python": sys.version,
        "start": list(args.start),
        "goal": list(args.goal),
        "departure": args.departure,
        "config_root": str(args.config_root.resolve()),
        "objective_order": list(args.objective_order),
        "repetitions": args.repetitions,
        "cpu": cpu,
        "baseline_median_wall_seconds": baseline_median,
        "shared_median_wall_seconds": shared_median,
        "wall_time_improvement_pct": improvement,
        "baseline_p95_wall_seconds": baseline_p95,
        "shared_p95_wall_seconds": shared_p95,
        "rss_median_ratio": rss_ratio,
        "cache_hit_rate_pct": hit_rate,
        "cache_hits_total": total_hits,
        "cache_misses_total": total_misses,
        "route_identity": "PASS",
        "baseline_runs": baseline_runs,
        "shared_runs": shared_runs,
    }
    print(
        f"median improvement={improvement:+.2f}% hit_rate={hit_rate:.2f}% "
        f"rss_ratio={rss_ratio:.3f} gate={gate_verdict} "
        f"baseline_p95={baseline_p95:.3f}s shared_p95={shared_p95:.3f}s",
        flush=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"results written to {args.output}", flush=True)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", type=Path, required=True)
    parser.add_argument("--start", nargs=2, type=int, required=True)
    parser.add_argument("--goal", nargs=2, type=int, required=True)
    parser.add_argument("--config-root", type=Path, default=Path("configs"))
    parser.add_argument("--departure", default="2026-02-22T00:00:00Z")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument(
        "--worker-timeout-seconds",
        type=float,
        default=900.0,
        help="hard timeout for each isolated worker (default: 900s)",
    )
    parser.add_argument(
        "--objective-order",
        nargs=3,
        choices=tuple(mode.value for mode in OBJECTIVES),
        default=[mode.value for mode in OBJECTIVES],
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("baseline", "shared"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cpu", type=int, default=-1, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        args.cpu = None if args.cpu < 0 else args.cpu
        if args.mode is None or args.worker_output is None:
            raise ValueError("worker mode requires --mode and --worker-output")
        return _worker(args)
    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
