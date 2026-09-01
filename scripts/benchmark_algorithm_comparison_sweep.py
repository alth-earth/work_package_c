#!/usr/bin/env python3
"""Drive the expanded-sample algorithm-comparison sweep.

The frozen real fixture hard-codes one origin/destination pair and one departure
time per window, which caps the real evidence at two windows and four
effectively independent routes.  This driver turns each frozen window into many
independent planning cases along two axes:

  * **origin/destination axis** - corridor pairs sampled inside the navigable
    component of the departure frame, covering short / medium / long voyages;
  * **departure-time axis**    - the same corridor replanned at later offsets
    inside the 145-frame window, so a different weather stretch is encountered.

Cases are executed by ``benchmark_algorithm_comparison.py`` child processes, each
pinned to its own core, so wall-clock ratios stay comparable while several cases
run in parallel.  A case whose origin cannot reach its destination inside the
horizon is fail-closed by the planner; the driver records it and keeps going,
because silently dropping it would inflate the apparent success rate.

This driver is presentation evidence only.  It does not relax or reinterpret any
frozen gate, does not change the production planner, and does not write formal
latest / replanning baseline / frozen artifacts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import deque
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "benchmark_algorithm_comparison.py"
SCHEMA_VERSION = "c.algorithm-comparison-sweep.v1"

WINDOWS = {
    "holdout": {
        "commit": REPO_ROOT.parent
        / ".runtime/experiments/winter-b-validation-holdout-total-20260826/risk-store/commits",
        "route": REPO_ROOT.parent
        / ".runtime/experiments/winter-c-validation-holdout-total-20260826/"
        / "winter-four-layer-route-plan-set-v3.json",
    },
    "development": {
        "commit": REPO_ROOT.parent
        / ".runtime/experiments/winter-b-validation-development-total-20260826/risk-store/commits",
        "route": REPO_ROOT.parent
        / ".runtime/experiments/winter-c-validation-development-total-20260826/"
        / "winter-four-layer-route-plan-set-v3.json",
    },
}

# Short / medium / long target path lengths, expressed in **grid hops**.  The
# frozen real grid is 8-connected, so a hop is not the Manhattan distance; at
# this resolution one hop is roughly 45 km, i.e. about 2.4 h of steaming, which
# puts the 24 h horizon at roughly nine hops.
LENGTH_BUCKETS = {"short": (4, 5), "medium": (6, 7), "long": (8, 9)}
MAX_HORIZON_HOURS = 24.0
MIN_ROW_SEPARATION = 3


@dataclass(frozen=True, slots=True)
class Case:
    window: str
    case_id: str
    start: tuple[int, int]
    goal: tuple[int, int]
    departure_offset_hours: float
    axis: str
    length_bucket: str | None
    hops: int = 0


# --------------------------------------------------------------------------- #
# frozen input access
# --------------------------------------------------------------------------- #
def _load_fixture_runner() -> Any:
    path = REPO_ROOT / "scripts" / "benchmark_temporal_dominance_real.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("c_sweep_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen real-input fixture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(window: str) -> Any:
    commit_path = _commit_path(window)
    return _load_fixture_runner()._load_fixture(
        argparse.Namespace(
            risk_window_commit=str(commit_path),
            route_plan_set=str(WINDOWS[window]["route"]),
            config_root=str(REPO_ROOT / "configs"),
            segment="rolling_0_24h",
        )
    )


def _navigable_nodes(fixture: Any) -> set[tuple[int, int]]:
    mask = np.asarray(
        fixture.frames[0].payload["hard_mask"].transpose("latitude", "longitude").values,
        dtype=bool,
    )
    return fixture.grid.connected_component(fixture.start, mask)


def _step_distance(
    node: tuple[int, int], navigable: set[tuple[int, int]], grid: Any
) -> dict[tuple[int, int], int]:
    """Breadth-first hop count inside the navigable component."""
    distances: dict[tuple[int, int], int] = {node: 0}
    queue = deque([node])
    while queue:
        current = queue.popleft()
        for neighbour in grid.neighbors(current):
            if neighbour in navigable and neighbour not in distances:
                distances[neighbour] = distances[current] + 1
                queue.append(neighbour)
    return distances


# --------------------------------------------------------------------------- #
# case construction
# --------------------------------------------------------------------------- #
def _build_od_cases(
    window: str,
    navigable: set[tuple[int, int]],
    grid: Any,
    per_bucket: int,
    axis: str,
    departure_offset_hours: float,
    starts: list[tuple[int, int]],
) -> list[Case]:
    cases: list[Case] = []
    for start in starts:
        if start not in navigable:
            continue
        distances = _step_distance(start, navigable, grid)
        for bucket, (low, high) in LENGTH_BUCKETS.items():
            candidates = sorted(
                node
                for node, hops in distances.items()
                if low <= hops <= high and abs(node[0] - start[0]) >= MIN_ROW_SEPARATION
            )
            if not candidates:
                continue
            # Deterministic spread across the candidate list so the sampled
            # corridors cover the whole corridor rather than one neighbourhood.
            stride = max(1, len(candidates) // per_bucket)
            picked = candidates[::stride][:per_bucket]
            for goal in picked:
                suffix = (
                    "" if departure_offset_hours == 0 else f"-t{int(departure_offset_hours):03d}"
                )
                cases.append(
                    Case(
                        window=window,
                        case_id=f"{window}-od-{start[0]}x{start[1]}-to-{goal[0]}x{goal[1]}{suffix}",
                        start=start,
                        goal=goal,
                        departure_offset_hours=departure_offset_hours,
                        axis=axis,
                        length_bucket=bucket,
                        hops=distances[goal],
                    )
                )
    return cases


def _plan_cases(args: argparse.Namespace) -> list[Case]:
    cases: list[Case] = []
    for window in args.window:
        fixture = _fixture(window)
        navigable = _navigable_nodes(fixture)
        grid = fixture.grid
        starts = [(5, 7), (9, 3), (13, 8), (17, 5), (21, 2)]
        canonical_hops = _step_distance(fixture.start, navigable, grid).get(fixture.goal, 0)
        # Anchor case: the frozen corridor exactly as the published single-case
        # evidence used it, re-measured under the pinned sweep protocol so the
        # expanded sample cannot drift away from the published numbers.
        cases.append(
            Case(
                window=window,
                case_id=f"{window}-canonical",
                start=fixture.start,
                goal=fixture.goal,
                departure_offset_hours=0.0,
                axis="canonical",
                length_bucket=None,
                hops=canonical_hops,
            )
        )
        for offset in args.departure_offset_hours:
            if offset == 0:
                continue
            cases.append(
                Case(
                    window=window,
                    case_id=f"{window}-canonical-t{int(offset):03d}",
                    start=fixture.start,
                    goal=fixture.goal,
                    departure_offset_hours=offset,
                    axis="canonical_time",
                    length_bucket=None,
                    hops=canonical_hops,
                )
            )
        cases.extend(
            _build_od_cases(
                window,
                navigable,
                grid,
                per_bucket=args.od_per_bucket,
                axis="od_pair",
                departure_offset_hours=0.0,
                starts=starts,
            )
        )
        for offset in args.departure_offset_hours:
            if offset == 0:
                continue
            cases.extend(
                _build_od_cases(
                    window,
                    navigable,
                    grid,
                    per_bucket=args.temporal_per_bucket,
                    axis="departure_time",
                    departure_offset_hours=offset,
                    starts=[(5, 7), (13, 8)],
                )
            )
    return cases


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _commit_path(window: str) -> Path:
    commit_dir = Path(WINDOWS[window]["commit"])
    return sorted(commit_dir.glob("risk-window-sha256-*.json"))[0]


def _run_case(
    case: Case,
    output_root: Path,
    cpu: int,
    repetitions: int,
    warmup: int,
    max_expansions: int,
) -> dict[str, Any]:
    case_dir = output_root / "cases" / case.case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(RUNNER),
        "--real-commit",
        str(_commit_path(case.window)),
        "--real-route-plan-set",
        str(WINDOWS[case.window]["route"]),
        "--config-root",
        str(REPO_ROOT / "configs"),
        "--real-segment",
        "rolling_0_24h",
        "--real-start",
        f"{case.start[0]},{case.start[1]}",
        "--real-goal",
        f"{case.goal[0]},{case.goal[1]}",
        "--real-horizon-hours",
        str(MAX_HORIZON_HOURS),
        "--real-departure-offset-hours",
        str(case.departure_offset_hours),
        "--algorithm",
        "time_dependent_astar",
        "--algorithm",
        "dijkstra",
        "--algorithm",
        "static_field",
        "--algorithm",
        "risk_blind",
        "--case-id",
        case.case_id,
        "--repetitions",
        str(repetitions),
        "--warmup",
        str(warmup),
        "--max-expansions",
        str(max_expansions),
        "--cpu",
        str(cpu),
        "--output-dir",
        str(case_dir),
    ]
    started = datetime.now(UTC)
    process = subprocess.run(command, capture_output=True, text=True, check=False)
    finished = datetime.now(UTC)
    artefact = case_dir / "comparison-summary.json"
    status = "ok"
    failure_count = 0
    if process.returncode != 0 or not artefact.is_file():
        status = "ERROR"
    else:
        document = json.loads(artefact.read_text(encoding="utf-8"))
        status = str(document.get("status", "ok"))
        failure_count = len(document.get("failures", []))
    return {
        "case_id": case.case_id,
        "window": case.window,
        "axis": case.axis,
        "length_bucket": case.length_bucket,
        "start": list(case.start),
        "goal": list(case.goal),
        "grid_hops": case.hops,
        "departure_offset_hours": case.departure_offset_hours,
        "cpu": cpu,
        "status": status,
        "failure_count": failure_count,
        "returncode": process.returncode,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 2),
        "stdout_tail": process.stdout[-2000:],
        "stderr_tail": process.stderr[-2000:] if process.returncode != 0 else "",
    }


def _worker(payload: dict[str, Any]) -> dict[str, Any]:
    case = Case(**payload["case"])
    return _run_case(
        case,
        Path(payload["output_root"]),
        payload["cpu"],
        payload["repetitions"],
        payload["warmup"],
        payload["max_expansions"],
    )


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", action="append", choices=sorted(WINDOWS))
    parser.add_argument(
        "--od-per-bucket",
        type=int,
        default=4,
        help="OD pairs per (start, length bucket) on the origin/destination axis",
    )
    parser.add_argument(
        "--temporal-per-bucket",
        type=int,
        default=1,
        help="OD pairs per (start, length bucket) on each extra departure offset",
    )
    parser.add_argument(
        "--departure-offset-hours",
        type=float,
        action="append",
        default=None,
        help="extra departure offsets inside the 145-frame window; repeatable",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--first-cpu", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if not args.window:
        args.window = sorted(WINDOWS)
    if args.departure_offset_hours is None:
        args.departure_offset_hours = []
    cases = _plan_cases(args)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    if args.dry_run:
        for case in cases:
            print(
                f"{case.case_id:44s} axis={case.axis:15s} bucket={case.length_bucket!s:6s} "
                f"start={case.start} goal={case.goal} hops={case.hops} "
                f"offset={case.departure_offset_hours}h"
            )
        print(f"{len(cases)} cases planned")
        return 0

    output_root: Path = args.output_dir
    (output_root / "cases").mkdir(parents=True, exist_ok=True)
    payloads = [
        {
            "case": {
                "window": case.window,
                "case_id": case.case_id,
                "start": case.start,
                "goal": case.goal,
                "departure_offset_hours": case.departure_offset_hours,
                "axis": case.axis,
                "length_bucket": case.length_bucket,
                "hops": case.hops,
            },
            "output_root": str(output_root),
            "cpu": args.first_cpu + (index % max(args.workers, 1)),
            "repetitions": args.repetitions,
            "warmup": args.warmup,
            "max_expansions": args.max_expansions,
        }
        for index, case in enumerate(cases)
    ]

    records: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        futures = [pool.submit(_worker, payload) for payload in payloads]
        for position, future in enumerate(as_completed(futures), start=1):
            record = future.result()
            records.append(record)
            print(
                f"[{position:3d}/{len(cases)}] {record['case_id']:44s} "
                f"status={record['status']:7s} failures={record['failure_count']} "
                f"{record['duration_seconds']:6.1f}s",
                flush=True,
            )
    records.sort(key=lambda item: item["case_id"])

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "runner_schema": "c.algorithm-comparison.v3",
        "windows": sorted(set(args.window)),
        "departure_offset_hours": sorted(set(args.departure_offset_hours)),
        "od_per_bucket": args.od_per_bucket,
        "temporal_per_bucket": args.temporal_per_bucket,
        "repetitions": args.repetitions,
        "warmup": args.warmup,
        "max_expansions": args.max_expansions,
        "workers": args.workers,
        "first_cpu": args.first_cpu,
        "max_horizon_hours": MAX_HORIZON_HOURS,
        "length_buckets": LENGTH_BUCKETS,
        "planned_case_count": len(cases),
        "recorded_case_count": len(records),
        "ok_case_count": sum(1 for item in records if item["status"] == "ok"),
        "partial_case_count": sum(1 for item in records if item["status"] == "partial"),
        "error_case_count": sum(1 for item in records if item["status"] == "ERROR"),
        "cases": records,
    }
    (output_root / "sweep-manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"sweep written to {output_root / 'sweep-manifest.json'} "
        f"({manifest['ok_case_count']} ok / {manifest['partial_case_count']} partial / "
        f"{manifest['error_case_count']} error)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
