#!/usr/bin/env python3
"""Paired comparison of the C planning algorithm against conventional baselines.

This runner exists to produce *scientific comparison evidence* for external
presentation.  It is deliberately **not** a promotion gate: it does not relax,
reinterpret or replace any frozen M0/M1/M2 threshold, does not change the
production planner default, and does not write formal latest / replanning
baseline / frozen artifacts.

Compared algorithms all share the **same** risk sampler, grid, vessel model,
edge evaluator, time bucketing, hard-mask and fail-closed semantics.  The only
thing that varies is the search strategy, so any observed difference is
attributable to the algorithm and not to a weaker model.

  * ``time_dependent_astar``  - the C production planner (admissible heuristic).
  * ``dijkstra``              - the identical time-expanded search with the
                                heuristic disabled (``use_heuristic=False``).
                                This is the classic uninformed baseline; it is
                                optimal on the same graph, so equal cost proves
                                the heuristic never sacrifices optimality.
  * ``static_field``          - the same A* planner run on a *frozen* risk field
                                (every frame replaced by the departure frame).
                                This represents the conventional practice of
                                planning against current conditions only.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning import profiling as synthetic_profiling
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.algorithm-comparison.v1"
REPO_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVES = tuple(ObjectiveMode)
ALGORITHMS = ("time_dependent_astar", "dijkstra", "static_field")

SYNTHETIC_PROFILES: dict[str, SyntheticProfileConfig] = {
    "small": SyntheticProfileConfig(rows=5, cols=7, frame_count=7),
    "medium": SyntheticProfileConfig(rows=9, cols=13, frame_count=13),
    "large": SyntheticProfileConfig(rows=13, cols=21, frame_count=25),
    "stress": SyntheticProfileConfig(rows=17, cols=29, frame_count=37),
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_metadata() -> dict[str, object]:
    def run(*command: str) -> str:
        try:
            return subprocess.run(
                command, cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout.strip()
        except Exception:  # pragma: no cover - metadata only
            return ""

    return {
        "commit": run("git", "rev-parse", "HEAD"),
        "branch": run("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(run("git", "status", "--porcelain")),
    }


def _rss_peak_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except Exception:  # pragma: no cover
        return None
    return None


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def _vessel() -> VesselPerformanceModel:
    return VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )


def _build_synthetic(
    profile_name: str,
) -> tuple[list[Any], tuple[int, int], tuple[int, int], timedelta, dict[str, Any]]:
    profile = SYNTHETIC_PROFILES[profile_name]
    frames = synthetic_profiling._make_frames(profile)  # type: ignore[attr-defined]
    start = (profile.rows // 2, 0)
    goal = (profile.rows // 2, profile.cols - 1)
    horizon = timedelta(hours=profile.frame_count - 1)
    identity = {
        "kind": "synthetic",
        "profile": profile_name,
        "config": asdict(profile),
    }
    return frames, start, goal, horizon, identity


def _load_fixture_runner() -> Any:
    path = REPO_ROOT / "scripts" / "benchmark_temporal_dominance_real.py"
    spec = importlib.util.spec_from_file_location("c_algorithm_comparison_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen real-input fixture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_real(
    commit_path: Path, route_path: Path, config_root: Path, segment: str
) -> tuple[list[Any], tuple[int, int], tuple[int, int], timedelta, dict[str, Any], dict[str, Any]]:
    runner = _load_fixture_runner()
    args = argparse.Namespace(
        risk_window_commit=str(commit_path),
        route_plan_set=str(route_path),
        config_root=str(config_root),
        segment=segment,
    )
    fixture = runner._load_fixture(args)  # type: ignore[attr-defined]
    frames = list(fixture.frames)
    horizon = timedelta(hours=24) if "24h" in segment else timedelta(hours=6)
    identity = {
        "kind": "real",
        "input_name": fixture.input_name,
        "commit_path": str(commit_path),
        "commit_sha256": _sha256(commit_path),
        "content_digest": fixture.commit["content_digest"],
        "route_plan_set": str(route_path),
        "route_plan_set_sha256": _sha256(route_path),
        "config_root": str(config_root),
        "segment": segment,
        "frame_count": len(frames),
    }
    # Reuse the frozen real-input configuration so the comparison uses the same
    # vessel model, grid, time bucket, edge sampling and frame-gap policy that
    # the production planner uses on this input.
    from arctic_route_planning.cost import VesselPerformanceModel as _VPM

    real_context = {
        "grid": fixture.grid,
        "vessel": _VPM.from_configuration(fixture.vessel_config),
        "time_bucket": timedelta(minutes=fixture.planner_config.time_bucket_minutes),
        "edge_sample_count": fixture.planner_config.edge_sample_count,
        "max_frame_gap": timedelta(minutes=fixture.planner_config.max_risk_frame_gap_minutes),
        "departure": fixture.departure,
    }
    return frames, fixture.start, fixture.goal, horizon, identity, real_context


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
def _run_one(
    frames: list[Any],
    start: tuple[int, int],
    goal: tuple[int, int],
    horizon: timedelta,
    objective: ObjectiveMode,
    algorithm: str,
    max_expansions: int,
    real_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single (objective, algorithm) cell and return comparable metrics."""
    source = frames
    if algorithm == "static_field":
        source = [replace(frame, payload=frames[0].payload) for frame in frames]

    if real_context is not None:
        sampler = RiskSampler(source, max_frame_gap=real_context["max_frame_gap"])
        grid = real_context["grid"]
        vessel = real_context["vessel"]
        departure = real_context["departure"]
        extra: dict[str, Any] = {
            "time_bucket_size": real_context["time_bucket"],
            "edge_sample_count": real_context["edge_sample_count"],
        }
    else:
        sampler = RiskSampler(source)
        grid = RegularGrid.from_risk_frame(source[0], allow_diagonal=False)
        vessel = _vessel()
        departure = source[0].valid_time
        extra = {}

    planner = TimeDependentAStar(grid, sampler, vessel)

    request = PlanningRequest(
        start=start,
        goal=goal,
        departure_time=departure,
        objective=objective,
        maximum_elapsed=horizon,
        maximum_risk=1.0,
        max_expansions=max_expansions,
        use_heuristic=algorithm != "dijkstra",
        **extra,
    )

    started = time.perf_counter()
    result = planner.plan(request)
    wall_ms = (time.perf_counter() - started) * 1000.0

    metrics = result.metrics
    steps = result.steps
    avg_risk = sum(step.edge_risk_score for step in steps) / max(len(steps), 1)
    max_risk = max((step.edge_maximum_risk for step in steps), default=0.0)

    return {
        "algorithm": algorithm,
        "objective": objective.value,
        "wall_ms": wall_ms,
        "compute_ms": metrics.compute_ms,
        "expanded_states": metrics.expanded_states,
        "generated_states": metrics.generated_states,
        "unique_states": metrics.unique_states,
        "heap_pushes": metrics.heap_pushes,
        "heap_pops": metrics.heap_pops,
        "queue_peak": metrics.queue_peak,
        "peak_rss_kib": _rss_peak_kib(),
        "route": {
            "total_cost_hours": result.total_cost_hours,
            "distance_km": result.distance_km,
            "travel_hours": result.travel_hours,
            "step_count": len(steps),
            "average_edge_risk": avg_risk,
            "maximum_edge_risk": max_risk,
            "nodes": [list(node) for node in result.nodes],
        },
    }


def _compare_pair(ours: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Relative advantage of ``ours`` over ``baseline`` (positive = ours better)."""

    def pct(key: str) -> float | None:
        base = baseline[key]
        if not base:
            return None
        return 100.0 * (1.0 - ours[key] / base)

    return {
        "expansion_reduction_pct": pct("expanded_states"),
        "generated_reduction_pct": pct("generated_states"),
        "heap_push_reduction_pct": pct("heap_pushes"),
        "wall_speedup": (baseline["wall_ms"] / ours["wall_ms"] if ours["wall_ms"] else None),
        "cost_delta_pct": (
            100.0
            * (ours["route"]["total_cost_hours"] - baseline["route"]["total_cost_hours"])
            / baseline["route"]["total_cost_hours"]
            if baseline["route"]["total_cost_hours"]
            else None
        ),
        "identical_route": ours["route"]["nodes"] == baseline["route"]["nodes"],
        "cost_identical": (
            abs(ours["route"]["total_cost_hours"] - baseline["route"]["total_cost_hours"]) < 1e-9
        ),
    }


# --------------------------------------------------------------------------- #
# driver
# --------------------------------------------------------------------------- #
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic-profile", choices=tuple(SYNTHETIC_PROFILES))
    source.add_argument("--real-commit", type=Path, help="bc.risk-window-commit.v1 path")
    parser.add_argument("--real-route-plan-set", type=Path)
    parser.add_argument("--real-segment", default="rolling_0_24h")
    parser.add_argument("--config-root", type=Path, default=REPO_ROOT / "configs")
    parser.add_argument("--objective", action="append", choices=[o.value for o in OBJECTIVES])
    parser.add_argument("--algorithm", action="append", choices=ALGORITHMS)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    objectives = tuple(ObjectiveMode(o) for o in (args.objective or [o.value for o in OBJECTIVES]))
    algorithms = tuple(args.algorithm or ALGORITHMS)

    real_context: dict[str, Any] | None = None
    if args.synthetic_profile:
        frames, start, goal, horizon, identity = _build_synthetic(args.synthetic_profile)
        label = args.synthetic_profile
    else:
        if not args.real_route_plan_set:
            parser_error = "--real-route-plan-set is required with --real-commit"
            print(parser_error, file=sys.stderr)
            return 2
        frames, start, goal, horizon, identity, real_context = _build_real(
            args.real_commit, args.real_route_plan_set, args.config_root, args.real_segment
        )
        label = f"real-{args.real_segment}"

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    raw: list[dict[str, Any]] = []
    for _ in range(args.warmup):
        _run_one(
            frames,
            start,
            goal,
            horizon,
            objectives[0],
            algorithms[0],
            args.max_expansions,
            real_context,
        )

    for repetition in range(args.repetitions):
        for objective in objectives:
            for algorithm in algorithms:
                record = _run_one(
                    frames,
                    start,
                    goal,
                    horizon,
                    objective,
                    algorithm,
                    args.max_expansions,
                    real_context,
                )
                record["repetition"] = repetition
                raw.append(record)

    # aggregate median per (objective, algorithm)
    summary_cells: list[dict[str, Any]] = []
    for objective in objectives:
        for algorithm in algorithms:
            cells = [
                r for r in raw if r["objective"] == objective.value and r["algorithm"] == algorithm
            ]
            summary_cells.append(
                {
                    "objective": objective.value,
                    "algorithm": algorithm,
                    "samples": len(cells),
                    "wall_ms_median": statistics.median(c["wall_ms"] for c in cells),
                    "compute_ms_median": statistics.median(c["compute_ms"] for c in cells),
                    "expanded_states_median": statistics.median(
                        c["expanded_states"] for c in cells
                    ),
                    "generated_states_median": statistics.median(
                        c["generated_states"] for c in cells
                    ),
                    "heap_pushes_median": statistics.median(c["heap_pushes"] for c in cells),
                    "queue_peak_median": statistics.median(c["queue_peak"] for c in cells),
                    "total_cost_hours_median": statistics.median(
                        c["route"]["total_cost_hours"] for c in cells
                    ),
                    "distance_km_median": statistics.median(
                        c["route"]["distance_km"] for c in cells
                    ),
                    "travel_hours_median": statistics.median(
                        c["route"]["travel_hours"] for c in cells
                    ),
                    "average_edge_risk_median": statistics.median(
                        c["route"]["average_edge_risk"] for c in cells
                    ),
                    "maximum_edge_risk_median": statistics.median(
                        c["route"]["maximum_edge_risk"] for c in cells
                    ),
                }
            )

    comparisons: list[dict[str, Any]] = []
    for objective in objectives:
        ours_cells = [
            c
            for c in summary_cells
            if c["objective"] == objective.value and c["algorithm"] == "time_dependent_astar"
        ]
        if not ours_cells:
            continue
        ours = ours_cells[0]
        for algorithm in algorithms:
            if algorithm == "time_dependent_astar":
                continue
            base_cells = [
                c
                for c in summary_cells
                if c["objective"] == objective.value and c["algorithm"] == algorithm
            ]
            if not base_cells:
                continue
            comparisons.append(
                {
                    "objective": objective.value,
                    "baseline": algorithm,
                    "expansion_reduction_pct": (
                        100.0
                        * (
                            1.0
                            - ours["expanded_states_median"]
                            / base_cells[0]["expanded_states_median"]
                        )
                        if base_cells[0]["expanded_states_median"]
                        else None
                    ),
                    "generated_reduction_pct": (
                        100.0
                        * (
                            1.0
                            - ours["generated_states_median"]
                            / base_cells[0]["generated_states_median"]
                        )
                        if base_cells[0]["generated_states_median"]
                        else None
                    ),
                    "wall_speedup": (
                        base_cells[0]["wall_ms_median"] / ours["wall_ms_median"]
                        if ours["wall_ms_median"]
                        else None
                    ),
                    "cost_delta_pct": (
                        100.0
                        * (
                            ours["total_cost_hours_median"]
                            - base_cells[0]["total_cost_hours_median"]
                        )
                        / base_cells[0]["total_cost_hours_median"]
                        if base_cells[0]["total_cost_hours_median"]
                        else None
                    ),
                    "cost_identical": (
                        abs(
                            ours["total_cost_hours_median"]
                            - base_cells[0]["total_cost_hours_median"]
                        )
                        < 1e-9
                    ),
                    "average_risk_delta_pct": (
                        100.0
                        * (
                            ours["average_edge_risk_median"]
                            - base_cells[0]["average_edge_risk_median"]
                        )
                        / base_cells[0]["average_edge_risk_median"]
                        if base_cells[0]["average_edge_risk_median"]
                        else None
                    ),
                }
            )

    document = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "label": label,
        "git": _git_metadata(),
        "python": platform.python_version(),
        "input_identity": identity,
        "request": {
            "start": list(start),
            "goal": list(goal),
            "horizon_hours": horizon.total_seconds() / 3600.0,
            "departure": frames[0].valid_time.isoformat(),
            "frame_count": len(frames),
        },
        "parameters": {
            "repetitions": args.repetitions,
            "warmup": args.warmup,
            "max_expansions": args.max_expansions,
            "objectives": [o.value for o in objectives],
            "algorithms": list(algorithms),
        },
        "summary": summary_cells,
        "comparisons": comparisons,
        "raw": raw,
    }
    (output_dir / "comparison.json").write_text(
        json.dumps(_jsonable(document), indent=2), encoding="utf-8"
    )
    (output_dir / "comparison-summary.json").write_text(
        json.dumps(_jsonable({k: v for k, v in document.items() if k != "raw"}), indent=2),
        encoding="utf-8",
    )

    print(f"[{label}] comparison written to {output_dir / 'comparison.json'}")
    for row in comparisons:
        print(
            f"  {row['objective']:12s} vs {row['baseline']:14s} "
            f"expansion -{row['expansion_reduction_pct']:.1f}%  "
            f"speedup {row['wall_speedup']:.2f}x  "
            f"cost_identical={row['cost_identical']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
