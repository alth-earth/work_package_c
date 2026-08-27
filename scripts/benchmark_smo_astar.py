#!/usr/bin/env python3
"""Paired, resumable benchmark for the SMO-A* traversal cache.

The default planner remains the control. Evidence mode persists every worker
and complete pair before advancing, rejects resume identity drift, and records
process, host, and cgroup resource evidence. Legacy ``--output`` mode remains
available for one-shot diagnostics but is not admissible P3.2 gate evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import arctic_route_planning.profiling as synthetic_profiling
from arctic_route_planning.config import load_planner_config, load_vessel_model_config
from arctic_route_planning.contracts import risk_frame_from_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p3.2-smo-benchmark.v1"
DIAGNOSTIC_SCHEMA_VERSION = "c.p3.3-smo-diagnostic.v1"
OBJECTIVES = tuple(ObjectiveMode)
MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
SYNTHETIC_PROFILES = {
    "small": SyntheticProfileConfig(rows=5, cols=7, frame_count=7),
    "medium": SyntheticProfileConfig(rows=9, cols=13, frame_count=13),
}


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
        frames.append(risk_frame_from_document(json.loads(frame_path.read_text(encoding="utf-8"))))
    return tuple(frames)


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "value"):
        enum_value = value.value
        if isinstance(enum_value, str):
            return enum_value
    return value


def _route_semantic_payload(result: Any) -> dict[str, object]:
    """Return business route fields, excluding runtime counters and IDs."""

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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _append_jsonl(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(document, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


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


def _host_swap_pages() -> dict[str, int] | None:
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/vmstat").read_text(encoding="utf-8").splitlines():
            name, raw_value = line.split()
            if name in {"pswpin", "pswpout"}:
                values[name] = int(raw_value)
        return values if set(values) == {"pswpin", "pswpout"} else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _read_scalar(path: Path) -> int | str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value == "max":
        return value
    try:
        return int(value)
    except ValueError:
        return None


def _read_key_values(path: Path) -> dict[str, int] | None:
    try:
        return {
            key: int(value)
            for key, value in (
                line.split() for line in path.read_text(encoding="utf-8").splitlines()
            )
        }
    except (FileNotFoundError, OSError, ValueError):
        return None


def _cgroup_snapshot() -> dict[str, object] | None:
    try:
        relative = None
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            hierarchy, controllers, candidate = line.split(":", 2)
            if hierarchy == "0" and controllers == "":
                relative = candidate.lstrip("/")
                break
        if relative is None:
            return None
        root = Path("/sys/fs/cgroup") / relative
        return {
            "path": f"/{relative}",
            "memory_current": _read_scalar(root / "memory.current"),
            "memory_peak": _read_scalar(root / "memory.peak"),
            "memory_max": _read_scalar(root / "memory.max"),
            "memory_swap_current": _read_scalar(root / "memory.swap.current"),
            "memory_swap_max": _read_scalar(root / "memory.swap.max"),
            "memory_events": _read_key_values(root / "memory.events"),
        }
    except (FileNotFoundError, OSError, ValueError):
        return None


def _resource_snapshot() -> dict[str, object]:
    affinity = None
    if hasattr(os, "sched_getaffinity"):
        affinity = sorted(os.sched_getaffinity(0))
    return {
        "process_swap_kib": _proc_swap_kib(),
        "host_swap_pages": _host_swap_pages(),
        "cgroup": _cgroup_snapshot(),
        "cpu_affinity": affinity,
    }


def _set_cpu(cpu: int | None) -> None:
    if cpu is None or not hasattr(os, "sched_setaffinity"):
        return
    os.sched_setaffinity(0, {cpu})


def _build_commit_components(
    args: argparse.Namespace,
) -> tuple[TimeDependentAStar, PlanningRequest, dict[str, object]]:
    if args.commit is None or args.start is None or args.goal is None or not args.departure:
        raise ValueError("commit input requires --start, --goal, and --departure")
    frames = _load_frames(args.commit)
    if not frames:
        raise ValueError("committed window has no frames")
    frame_path = args.commit.parent.parent / "frames" / f"{frames[0].risk_id}.json"
    first_doc = json.loads(frame_path.read_text(encoding="utf-8"))
    planner_config = load_planner_config(args.config_root)
    vessel_config = load_vessel_model_config(
        args.config_root,
        first_doc["vessel_profile_id"],
    )
    sampler = RiskSampler(frames, max_frame_gap=None)
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=planner_config.connectivity == 8)
    vessel = VesselPerformanceModel.from_configuration(vessel_config)
    planner = TimeDependentAStar(
        grid,
        sampler,
        vessel,
        planner_config=planner_config,
    )
    departure = datetime.fromisoformat(args.departure.replace("Z", "+00:00")).astimezone(UTC)
    request = PlanningRequest(
        start=tuple(args.start),
        goal=tuple(args.goal),
        departure_time=departure,
        objective=ObjectiveMode(args.objective_order[0]),
        max_expansions=args.max_expansions,
    )
    identity = {
        "kind": "committed_window",
        "commit_path": str(args.commit.resolve()),
        "commit_sha256": _sha256(args.commit),
        "frame_count": len(frames),
    }
    return planner, request, identity


def _build_synthetic_components(
    args: argparse.Namespace,
) -> tuple[TimeDependentAStar, PlanningRequest, dict[str, object]]:
    if args.synthetic_profile is None:
        raise ValueError("synthetic profile is required")
    profile = SYNTHETIC_PROFILES[args.synthetic_profile]
    frames = synthetic_profiling._make_frames(profile)  # type: ignore[attr-defined]
    planner_config = PlannerConfig(connectivity=4, edge_sample_count=3)
    sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    planner = TimeDependentAStar(
        grid,
        sampler,
        vessel,
        planner_config=planner_config,
    )
    request = PlanningRequest(
        start=(profile.rows // 2, 0),
        goal=(profile.rows // 2, profile.cols - 1),
        departure_time=frames[0].valid_time,
        objective=ObjectiveMode(args.objective_order[0]),
        maximum_elapsed=timedelta(hours=profile.frame_count - 1),
        maximum_risk=1.0,
        max_expansions=args.max_expansions,
        time_bucket_size=timedelta(minutes=planner_config.time_bucket_minutes),
        edge_sample_count=planner_config.edge_sample_count,
    )
    profile_document = asdict(profile)
    identity = {
        "kind": "synthetic",
        "profile": args.synthetic_profile,
        "profile_config": profile_document,
        "profile_sha256": hashlib.sha256(
            json.dumps(profile_document, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "frame_count": len(frames),
    }
    return planner, request, identity


def _worker(args: argparse.Namespace) -> int:
    _set_cpu(args.cpu)
    if args.commit is not None:
        planner, request, input_identity = _build_commit_components(args)
    else:
        planner, request, input_identity = _build_synthetic_components(args)
    order = tuple(ObjectiveMode(value) for value in args.objective_order)
    resources_before = _resource_snapshot()
    started = time.perf_counter()
    results = planner.plan_candidates(
        request,
        objectives=order,
        shared_edge_evaluation=args.mode == "shared",
        traversal_cache_diagnostics=args.diagnostic,
    )
    elapsed = time.perf_counter() - started
    resources_after = _resource_snapshot()
    routes = {mode.value: _route_record(result) for mode, result in results.items()}
    payload = {
        "mode": args.mode,
        "objective_order": [mode.value for mode in order],
        "wall_seconds": elapsed,
        "peak_rss_kib": _rss_peak_kib(),
        "resources_before": resources_before,
        "resources_after": resources_after,
        "routes": routes,
        "traversal_cache": planner.traversal_cache_stats,
        "traversal_cache_diagnostics": planner.traversal_cache_diagnostics,
        "input_identity": input_identity,
        "risk_identity": _jsonable(planner.risk_identity),
        "request": {
            "start": list(request.start),
            "goal": list(request.goal),
            "departure": request.departure_time.isoformat(),
            "maximum_elapsed_seconds": (
                request.maximum_elapsed.total_seconds()
                if request.maximum_elapsed is not None
                else None
            ),
            "maximum_risk": request.maximum_risk,
        },
    }
    _atomic_write_json(args.worker_output, payload)
    return 0


def _input_command_args(args: argparse.Namespace) -> list[str]:
    if args.commit is not None:
        return [
            "--commit",
            str(args.commit),
            "--start",
            str(args.start[0]),
            str(args.start[1]),
            "--goal",
            str(args.goal[0]),
            str(args.goal[1]),
            "--departure",
            args.departure,
        ]
    return ["--synthetic-profile", args.synthetic_profile]


def _run_worker(
    *,
    args: argparse.Namespace,
    mode: str,
    output_path: Path,
    cpu: int | None,
) -> dict[str, object]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--mode",
        mode,
        *_input_command_args(args),
        "--config-root",
        str(args.config_root),
        "--max-expansions",
        str(args.max_expansions),
        "--worker-output",
        str(output_path),
        "--cpu",
        str(cpu) if cpu is not None else "-1",
        "--objective-order",
        *args.objective_order,
    ]
    if args.diagnostic:
        command.extend(("--diagnostic", "--gate-profile", "diagnostic"))
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

    return {
        "git_sha": run("git", "rev-parse", "HEAD"),
        "git_dirty": bool(run("git", "status", "--porcelain")),
        "uv_lock_sha256": _sha256(repo_root / "uv.lock"),
        "runner_sha256": _sha256(Path(__file__)),
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
    if baseline.get("input_identity") != shared.get("input_identity"):
        raise ValueError("control/candidate input identity mismatch")
    if baseline.get("request") != shared.get("request"):
        raise ValueError("control/candidate request mismatch")


def _resource_delta(run: dict[str, object]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    before = run.get("resources_before")
    after = run.get("resources_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False, ["resource snapshots missing"]
    process_before = before.get("process_swap_kib")
    process_after = after.get("process_swap_kib")
    if process_before is None or process_after is None:
        failures.append("process VmSwap not measured")
    elif int(process_after) - int(process_before) != 0:
        failures.append("process VmSwap changed")
    host_before = before.get("host_swap_pages")
    host_after = after.get("host_swap_pages")
    if not isinstance(host_before, dict) or not isinstance(host_after, dict):
        failures.append("host swap counters not measured")
    else:
        for name in ("pswpin", "pswpout"):
            if int(host_after[name]) - int(host_before[name]) != 0:
                failures.append(f"host {name} changed")
    affinity = after.get("cpu_affinity")
    if not isinstance(affinity, list) or len(affinity) != 1:
        failures.append("worker affinity is not exactly one CPU")
    cgroup_before = before.get("cgroup")
    cgroup_after = after.get("cgroup")
    if not isinstance(cgroup_before, dict) or not isinstance(cgroup_after, dict):
        failures.append("cgroup metrics not measured")
    else:
        if cgroup_after.get("memory_swap_current") != 0:
            failures.append("cgroup memory.swap.current is not zero")
        events_before = cgroup_before.get("memory_events")
        events_after = cgroup_after.get("memory_events")
        if not isinstance(events_before, dict) or not isinstance(events_after, dict):
            failures.append("cgroup memory.events not measured")
        else:
            for name in ("oom", "oom_kill"):
                if int(events_after.get(name, 0)) - int(events_before.get(name, 0)) != 0:
                    failures.append(f"cgroup {name} changed")
    return not failures, failures


def _strict_cgroup_limits(run: dict[str, object]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    after = run.get("resources_after")
    cgroup = after.get("cgroup") if isinstance(after, dict) else None
    if not isinstance(cgroup, dict):
        return False, ["strict cgroup metrics missing"]
    memory_max = cgroup.get("memory_max")
    if not isinstance(memory_max, int) or memory_max > MEMORY_LIMIT_BYTES:
        failures.append("cgroup memory.max is missing or exceeds 4 GiB")
    if cgroup.get("memory_swap_max") != 0:
        failures.append("cgroup memory.swap.max is not zero")
    return not failures, failures


def _pair_order(index: int) -> tuple[str, str]:
    return ("baseline", "shared") if index % 2 == 1 else ("shared", "baseline")


def _case_key(kind: str, index: int) -> str:
    return f"{kind.lower()}-{index:03d}"


def _completed_indexes(cases: list[dict[str, object]], kind: str) -> set[int]:
    return {
        int(case["index"])
        for case in cases
        if case.get("kind") == kind and case.get("status") == "PASS"
    }


def _referenced_worker_files(cases: list[dict[str, object]]) -> set[str]:
    referenced: set[str] = set()
    for case in cases:
        worker_files = case.get("worker_files")
        if isinstance(worker_files, dict):
            referenced.update(str(value) for value in worker_files.values())
    return referenced


def _exclude_unreferenced_workers(output_dir: Path, cases_path: Path) -> None:
    cases = _read_jsonl(cases_path)
    referenced = _referenced_worker_files(cases)
    for directory in (output_dir / "workers", output_dir / "warmups"):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            relative = str(path.relative_to(output_dir))
            if relative in referenced:
                continue
            _append_jsonl(
                cases_path,
                {
                    "kind": "RECOVERY",
                    "status": "ORPHANED_EXCLUDED",
                    "reason": "unreferenced worker from interrupted pair",
                    "worker_files": {"orphan": relative},
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
            referenced.add(relative)


def _next_attempt(directory: Path, key: str) -> int:
    pattern = re.compile(rf"^{re.escape(key)}-attempt-(\d+)-(?:baseline|shared)\.json$")
    attempts = {
        int(match.group(1))
        for path in directory.glob(f"{key}-attempt-*-*.json")
        if (match := pattern.match(path.name)) is not None
    }
    return max(attempts, default=0) + 1


def _run_pair(
    *,
    args: argparse.Namespace,
    output_dir: Path,
    cases_path: Path,
    kind: str,
    index: int,
    cpu: int | None,
) -> dict[str, object]:
    directory = output_dir / ("warmups" if kind == "WARMUP" else "workers")
    directory.mkdir(parents=True, exist_ok=True)
    key = _case_key(kind, index)
    attempt = _next_attempt(directory, key)
    order = _pair_order(index)
    runs: dict[str, dict[str, object]] = {}
    paths: dict[str, str] = {}
    try:
        for mode in order:
            path = directory / f"{key}-attempt-{attempt:02d}-{mode}.json"
            runs[mode] = _run_worker(
                args=args,
                mode=mode,
                output_path=path,
                cpu=cpu,
            )
            paths[mode] = str(path.relative_to(output_dir))
        _validate_route_identity(runs["baseline"], runs["shared"])
    except Exception as exc:
        _append_jsonl(
            cases_path,
            {
                "kind": kind,
                "index": index,
                "attempt": attempt,
                "status": ("ORPHANED_EXCLUDED" if 0 < len(paths) < len(order) else "FAIL"),
                "reason": str(exc),
                "execution_order": list(order),
                "worker_files": paths,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    case = {
        "kind": kind,
        "index": index,
        "attempt": attempt,
        "status": "PASS",
        "execution_order": list(order),
        "worker_files": paths,
        "baseline_wall_seconds": runs["baseline"]["wall_seconds"],
        "shared_wall_seconds": runs["shared"]["wall_seconds"],
        "shared_cache": runs["shared"]["traversal_cache"],
        "route_identity": "PASS",
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _append_jsonl(cases_path, case)
    return case


def _load_completed_runs(
    output_dir: Path,
    cases: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    baseline_runs = []
    shared_runs = []
    timed = sorted(
        (
            case
            for case in cases
            if case.get("kind") == "TIMED_PAIR" and case.get("status") == "PASS"
        ),
        key=lambda case: int(case["index"]),
    )
    for case in timed:
        worker_files = case["worker_files"]
        baseline_runs.append(
            json.loads((output_dir / worker_files["baseline"]).read_text(encoding="utf-8"))
        )
        shared_runs.append(
            json.loads((output_dir / worker_files["shared"]).read_text(encoding="utf-8"))
        )
    return baseline_runs, shared_runs


def _summarize_cache_diagnostics(
    shared_runs: list[dict[str, object]],
) -> dict[str, object]:
    """Aggregate opt-in exact-key diagnostics without treating them as a gate."""

    records = [
        run.get("traversal_cache_diagnostics")
        for run in shared_runs
        if isinstance(run.get("traversal_cache_diagnostics"), dict)
    ]
    if not records or not all(record.get("enabled") is True for record in records):
        return {
            "enabled": False,
            "runs": len(records),
            "reason": "worker diagnostics were not enabled",
        }

    def total(name: str) -> int:
        return sum(int(record.get(name, 0)) for record in records)

    def median(name: str) -> float:
        values = [float(record.get(name, 0)) for record in records]
        return statistics.median(values) if values else 0.0

    exact_lookups = total("exact_key_lookups")
    exact_hits = total("exact_key_hits")
    objective: dict[str, dict[str, float | int]] = {}
    for mode in OBJECTIVES:
        name = mode.value
        lookups = sum(
            int((record.get("objective") or {}).get(name, {}).get("lookups", 0))
            for record in records
        )
        hits = sum(
            int((record.get("objective") or {}).get(name, {}).get("hits", 0))
            for record in records
        )
        misses = sum(
            int((record.get("objective") or {}).get(name, {}).get("misses", 0))
            for record in records
        )
        objective[name] = {
            "lookups": lookups,
            "hits": hits,
            "misses": misses,
            "hit_rate_pct": (hits / lookups * 100.0) if lookups else 0.0,
        }
    return {
        "enabled": True,
        "runs": len(records),
        "exact_key_lookups_total": exact_lookups,
        "exact_key_hits_total": exact_hits,
        "exact_key_misses_total": total("exact_key_misses"),
        "exact_key_hit_rate_pct": (
            exact_hits / exact_lookups * 100.0 if exact_lookups else 0.0
        ),
        "unique_exact_keys_median": median("unique_exact_keys"),
        "unique_physical_edges_median": median("unique_physical_edges"),
        "physical_edge_reuse_lookups_total": total("physical_edge_reuse_lookups"),
        "time_variant_exact_misses_total": total("time_variant_exact_misses"),
        "time_variant_unique_keys_median": median("time_variant_unique_keys"),
        "estimated_shallow_bytes_median": median("estimated_shallow_bytes"),
        "peak_estimated_shallow_bytes_median": median("peak_estimated_shallow_bytes"),
        "objective": objective,
        "per_run": records,
    }


def _summarize_runs(
    baseline_runs: list[dict[str, object]],
    shared_runs: list[dict[str, object]],
    *,
    gate_profile: str,
    repetitions: int,
    strict_resources: bool,
) -> dict[str, object]:
    if len(baseline_runs) != repetitions or len(shared_runs) != repetitions:
        raise ValueError("summary requires every planned paired run")
    baseline_walls = [float(run["wall_seconds"]) for run in baseline_runs]
    shared_walls = [float(run["wall_seconds"]) for run in shared_runs]
    baseline_median = statistics.median(baseline_walls)
    shared_median = statistics.median(shared_walls)
    improvement = (1.0 - shared_median / baseline_median) * 100.0
    baseline_p95 = _nearest_rank(baseline_walls, 0.95)
    shared_p95 = _nearest_rank(shared_walls, 0.95)
    total_hits = sum(int(run["traversal_cache"]["hits"]) for run in shared_runs)
    total_misses = sum(int(run["traversal_cache"]["misses"]) for run in shared_runs)
    hit_rate = (
        total_hits / (total_hits + total_misses) * 100.0 if total_hits + total_misses else 0.0
    )
    rss_ratios = [
        float(shared["peak_rss_kib"]) / float(baseline["peak_rss_kib"])
        for baseline, shared in zip(baseline_runs, shared_runs, strict=True)
        if float(baseline["peak_rss_kib"]) > 0
    ]
    rss_ratio = statistics.median(rss_ratios) if rss_ratios else float("nan")
    resource_failures: list[dict[str, object]] = []
    for run_index, run in enumerate((*baseline_runs, *shared_runs), start=1):
        resource_ok, failures = _resource_delta(run)
        if strict_resources:
            limits_ok, limit_failures = _strict_cgroup_limits(run)
            resource_ok = resource_ok and limits_ok
            failures.extend(limit_failures)
        if not resource_ok:
            resource_failures.append({"run_index": run_index, "failures": failures})
    checks = {
        "planned_pairs_complete": len(baseline_runs) == repetitions,
        "route_identity": True,
        "rss_ratio_le_1_10": rss_ratio <= 1.10 if math.isfinite(rss_ratio) else False,
        "resources_pass": not resource_failures,
    }
    if gate_profile == "m0":
        checks["median_wall_regression_le_5pct"] = shared_median <= baseline_median * 1.05
    elif gate_profile == "m1":
        checks.update(
            {
                "wall_improvement_ge_15pct": improvement >= 15.0,
                "p95_regression_le_5pct": shared_p95 <= baseline_p95 * 1.05,
                "cache_hit_rate_ge_50pct": hit_rate >= 50.0,
            }
        )
    elif gate_profile == "diagnostic":
        # Diagnostic runs are evidence-bearing only for semantic/resource
        # integrity.  Their timing, hit-rate, and RSS values are explanatory
        # observations and do not silently become a promotion gate.
        checks.pop("rss_ratio_le_1_10")
    else:  # pragma: no cover - argparse and validation fence this.
        raise ValueError(f"unsupported gate profile: {gate_profile}")
    cache_diagnostics = _summarize_cache_diagnostics(shared_runs)
    gate_pass = all(checks.values())
    gate_verdict = (
        ("DIAGNOSTIC_PASS" if gate_pass else "DIAGNOSTIC_FAIL")
        if gate_profile == "diagnostic"
        else ("PASS" if gate_pass else "FAIL")
    )
    return {
        "status": "COMPLETED",
        "gate_profile": gate_profile,
        "gate_verdict": gate_verdict,
        "evidence_admissibility": (
            "P3.3_DIAGNOSTIC_ONLY"
            if gate_profile == "diagnostic"
            else "P3.2_GATE_EVIDENCE"
        ),
        "gate_checks": checks,
        "repetitions": repetitions,
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
        "resource_failures": resource_failures,
        "cache_diagnostics": cache_diagnostics,
        "baseline_runs": baseline_runs,
        "shared_runs": shared_runs,
    }


def _frozen_identity(args: argparse.Namespace, repo_root: Path) -> dict[str, object]:
    git = _git_metadata(repo_root)
    if git["git_dirty"]:
        raise RuntimeError("evidence mode requires a clean worktree")
    input_identity: dict[str, object]
    if args.commit is not None:
        input_identity = {
            "kind": "committed_window",
            "path": str(args.commit.resolve()),
            "sha256": _sha256(args.commit),
        }
    else:
        profile = asdict(SYNTHETIC_PROFILES[args.synthetic_profile])
        input_identity = {
            "kind": "synthetic",
            "profile": args.synthetic_profile,
            "config": profile,
            "sha256": hashlib.sha256(
                json.dumps(profile, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
    request = {
        "start": list(args.start) if args.start is not None else None,
        "goal": list(args.goal) if args.goal is not None else None,
        "departure": args.departure,
        "objective_order": list(args.objective_order),
        "max_expansions": args.max_expansions,
    }
    identity = {
        "schema_version": (
            DIAGNOSTIC_SCHEMA_VERSION if args.diagnostic else SCHEMA_VERSION
        ),
        "algorithm": "smo-astar",
        "run_kind": "diagnostic" if args.diagnostic else "benchmark",
        "git": git,
        "input": input_identity,
        "config_root": str(args.config_root.resolve()),
        "config_tree_sha256": _tree_digest(args.config_root),
        "request": request,
        "gate_profile": args.gate_profile,
        "warmup_pairs": args.warmup_pairs,
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "strict_resources": args.strict_resources,
        "memory_limit_bytes": MEMORY_LIMIT_BYTES if args.strict_resources else None,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    identity["identity_sha256"] = hashlib.sha256(encoded).hexdigest()
    return identity


def _validate_resume_identity(
    recorded: dict[str, object],
    current: dict[str, object],
) -> None:
    if recorded != current:
        raise RuntimeError("resume identity does not match the prepared experiment")


def _manifest_document(identity: dict[str, object]) -> dict[str, object]:
    suffix = str(identity["identity_sha256"])[:12]
    diagnostic = identity.get("run_kind") == "diagnostic"
    return {
        "schema_version": identity["schema_version"],
        "experiment_id": (
            f"c-p3.3-smo-diagnostic-{suffix}"
            if diagnostic
            else f"c-p3.2-smo-{suffix}"
        ),
        "evidence_admissibility": (
            "P3.3_DIAGNOSTIC_ONLY"
            if diagnostic
            else "P3.2_GATE_EVIDENCE"
        ),
        "status": "PREPARED",
        "identity": identity,
        "completed_warmup_pairs": 0,
        "completed_timed_pairs": 0,
        "production_published": False,
        "formal_latest_store_written": False,
        "frozen_artifact_written": False,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _update_manifest(
    path: Path,
    manifest: dict[str, object],
    *,
    status: str,
    cases: list[dict[str, object]] | None = None,
    error: str | None = None,
    summary: dict[str, object] | None = None,
) -> None:
    manifest["status"] = status
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    if cases is not None:
        manifest["completed_warmup_pairs"] = len(_completed_indexes(cases, "WARMUP"))
        manifest["completed_timed_pairs"] = len(_completed_indexes(cases, "TIMED_PAIR"))
    if error is not None:
        manifest["error"] = error
    else:
        manifest.pop("error", None)
    if summary is not None:
        manifest["gate_verdict"] = summary["gate_verdict"]
        manifest["summary_sha256"] = _sha256(path.parent / "summary.json")
    _atomic_write_json(path, manifest)


def _prepare_evidence(
    args: argparse.Namespace,
    identity: dict[str, object],
) -> tuple[Path, Path, dict[str, object]]:
    output_dir = args.output_dir
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    if args.resume:
        if not manifest_path.exists():
            raise RuntimeError("--resume requires an existing manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_resume_identity(manifest["identity"], identity)
        _exclude_unreferenced_workers(output_dir, cases_path)
        return manifest_path, cases_path, manifest
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError("evidence output directory already exists and is not empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = _manifest_document(identity)
    _atomic_write_json(manifest_path, manifest)
    return manifest_path, cases_path, manifest


def _run_evidence(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    identity = _frozen_identity(args, repo_root)
    manifest_path, cases_path, manifest = _prepare_evidence(args, identity)
    if manifest.get("status") == "COMPLETED" and (args.output_dir / "summary.json").exists():
        print(f"experiment already complete: {args.output_dir}", flush=True)
        return 0
    cpu = min(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    try:
        cases = _read_jsonl(cases_path)
        _update_manifest(manifest_path, manifest, status="RUNNING", cases=cases)
        for index in range(1, args.warmup_pairs + 1):
            cases = _read_jsonl(cases_path)
            if index in _completed_indexes(cases, "WARMUP"):
                continue
            _run_pair(
                args=args,
                output_dir=args.output_dir,
                cases_path=cases_path,
                kind="WARMUP",
                index=index,
                cpu=cpu,
            )
            cases = _read_jsonl(cases_path)
            _update_manifest(manifest_path, manifest, status="RUNNING", cases=cases)
            print(f"warmup pair {index}/{args.warmup_pairs}: PASS", flush=True)
        for index in range(1, args.repetitions + 1):
            cases = _read_jsonl(cases_path)
            if index in _completed_indexes(cases, "TIMED_PAIR"):
                continue
            case = _run_pair(
                args=args,
                output_dir=args.output_dir,
                cases_path=cases_path,
                kind="TIMED_PAIR",
                index=index,
                cpu=cpu,
            )
            cases = _read_jsonl(cases_path)
            _update_manifest(manifest_path, manifest, status="RUNNING", cases=cases)
            print(
                f"pair {index}/{args.repetitions}: "
                f"baseline={float(case['baseline_wall_seconds']):.3f}s "
                f"shared={float(case['shared_wall_seconds']):.3f}s "
                f"hits={case['shared_cache']['hits']} route=PASS",
                flush=True,
            )
        cases = _read_jsonl(cases_path)
        baseline_runs, shared_runs = _load_completed_runs(args.output_dir, cases)
        summary = _summarize_runs(
            baseline_runs,
            shared_runs,
            gate_profile=args.gate_profile,
            repetitions=args.repetitions,
            strict_resources=args.strict_resources,
        )
        summary.update(
            {
                "schema_version": SCHEMA_VERSION,
                "experiment_id": manifest["experiment_id"],
                "identity": identity,
            }
        )
        _atomic_write_json(args.output_dir / "summary.json", summary)
        _update_manifest(
            manifest_path,
            manifest,
            status="COMPLETED",
            cases=cases,
            summary=summary,
        )
        print(
            f"median improvement={summary['wall_time_improvement_pct']:+.2f}% "
            f"hit_rate={summary['cache_hit_rate_pct']:.2f}% "
            f"rss_ratio={summary['rss_median_ratio']:.3f} "
            f"gate={summary['gate_verdict']}",
            flush=True,
        )
        print(f"evidence written to {args.output_dir}", flush=True)
        return 0
    except KeyboardInterrupt:
        cases = _read_jsonl(cases_path)
        _update_manifest(manifest_path, manifest, status="ABORTED", cases=cases)
        raise
    except Exception as exc:
        cases = _read_jsonl(cases_path)
        _update_manifest(
            manifest_path,
            manifest,
            status="FAIL",
            cases=cases,
            error=str(exc),
        )
        raise


def _run_legacy(args: argparse.Namespace) -> int:
    import tempfile

    cpu = min(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    baseline_runs: list[dict[str, object]] = []
    shared_runs: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="smo-astar-workers-") as temp_dir:
        root = Path(temp_dir)
        for index in range(1, args.repetitions + 1):
            pair: dict[str, dict[str, object]] = {}
            for mode in _pair_order(index):
                pair[mode] = _run_worker(
                    args=args,
                    mode=mode,
                    output_path=root / f"r{index}-{mode}.json",
                    cpu=cpu,
                )
            _validate_route_identity(pair["baseline"], pair["shared"])
            baseline_runs.append(pair["baseline"])
            shared_runs.append(pair["shared"])
    summary = _summarize_runs(
        baseline_runs,
        shared_runs,
        gate_profile=args.gate_profile or "m1",
        repetitions=args.repetitions,
        strict_resources=False,
    )
    summary["evidence_admissibility"] = "LEGACY_ONE_SHOT_NOT_P3_2_EVIDENCE"
    if args.output is not None:
        _atomic_write_json(args.output, summary)
    return 0


def _validate_args(args: argparse.Namespace) -> None:
    if args.repetitions < 1:
        raise ValueError("--repetitions must be positive")
    if args.warmup_pairs < 0:
        raise ValueError("--warmup-pairs must not be negative")
    if args.worker_timeout_seconds <= 0:
        raise ValueError("--worker-timeout-seconds must be positive")
    if len(args.objective_order) != len(OBJECTIVES):
        raise ValueError(f"--objective-order must contain {len(OBJECTIVES)} objectives")
    if set(args.objective_order) != {mode.value for mode in OBJECTIVES}:
        raise ValueError("--objective-order must contain each objective exactly once")
    if args.commit is not None and (args.start is None or args.goal is None or not args.departure):
        raise ValueError("commit input requires --start, --goal, and --departure")
    if args.output_dir is not None and args.gate_profile is None:
        raise ValueError("evidence mode requires --gate-profile")
    if args.diagnostic and args.output_dir is None and not args.worker:
        raise ValueError("--diagnostic requires evidence mode with --output-dir")
    if args.diagnostic and args.gate_profile != "diagnostic":
        raise ValueError("--diagnostic requires --gate-profile diagnostic")
    if args.gate_profile == "diagnostic" and not args.diagnostic:
        raise ValueError("diagnostic gate profile requires --diagnostic")
    if args.diagnostic and args.commit is not None:
        raise ValueError("P3.3 diagnostics require a synthetic profile")
    if (
        args.diagnostic
        and not args.worker
        and (args.warmup_pairs != 1 or args.repetitions != 3)
    ):
        raise ValueError("P3.3 diagnostics require exactly 1 warmup pair and 3 repetitions")
    if args.output_dir is None and args.output is None and not args.worker:
        raise ValueError("parent mode requires --output-dir or --output")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--commit", type=Path)
    inputs.add_argument("--synthetic-profile", choices=tuple(SYNTHETIC_PROFILES))
    parser.add_argument("--start", nargs=2, type=int)
    parser.add_argument("--goal", nargs=2, type=int)
    parser.add_argument("--config-root", type=Path, default=Path("configs"))
    parser.add_argument("--departure")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--warmup-pairs", type=int, default=0)
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
    parser.add_argument("--gate-profile", choices=("m0", "m1", "diagnostic"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--strict-resources", action="store_true")
    parser.add_argument("--output", type=Path, help="legacy one-shot summary path")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="enable P3.3 synthetic exact-key diagnostics (evidence mode only)",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("baseline", "shared"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cpu", type=int, default=-1, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    _validate_args(args)
    if args.worker:
        args.cpu = None if args.cpu < 0 else args.cpu
        if args.mode is None or args.worker_output is None:
            raise ValueError("worker mode requires --mode and --worker-output")
        return _worker(args)
    if args.output_dir is not None:
        return _run_evidence(args)
    return _run_legacy(args)


if __name__ == "__main__":
    raise SystemExit(main())
