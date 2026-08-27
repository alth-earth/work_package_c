#!/usr/bin/env python3
"""Research-only paired M0 benchmark for the internal ARA* candidate.

This runner compares the unchanged control A* with the non-public ARA* module
on labelled synthetic profiles.  It records first-incumbent diagnostics and
persists complete pairs so an interrupted run cannot manufacture a paired
sample by combining workers from different attempts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import arctic_route_planning.profiling as synthetic_profiling
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners.ara_star import AnytimeRepairingAStar
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p3.2-ara-m0-benchmark.v1"
OBJECTIVES = tuple(ObjectiveMode)
MEMORY_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
EPSILON_SCHEDULE = (2.5, 2.0, 1.5, 1.0)
SYNTHETIC_PROFILES = {
    "small": SyntheticProfileConfig(rows=5, cols=7, frame_count=7),
    "medium": SyntheticProfileConfig(rows=9, cols=13, frame_count=13),
}


def _jsonable(value: object) -> object:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _route_payload(result: Any) -> dict[str, object]:
    payload = {
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
    return payload


def _route_record(result: Any) -> dict[str, object]:
    payload = _route_payload(result)
    return {
        "semantic_digest": hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "semantic": payload,
        "compute_ms": result.metrics.compute_ms,
        "expanded_states": result.metrics.expanded_states,
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
        digest.update(str(path.relative_to(root)).encode())
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
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _proc_swap_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _host_swap_pages() -> dict[str, int] | None:
    try:
        values = {}
        for line in Path("/proc/vmstat").read_text().splitlines():
            name, raw = line.split()
            if name in {"pswpin", "pswpout"}:
                values[name] = int(raw)
        return values if set(values) == {"pswpin", "pswpout"} else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _read_scalar(path: Path) -> int | str | None:
    try:
        value = path.read_text().strip()
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
            for key, value in (line.split() for line in path.read_text().splitlines())
        }
    except (FileNotFoundError, OSError, ValueError):
        return None


def _cgroup_snapshot() -> dict[str, object] | None:
    try:
        relative = None
        for line in Path("/proc/self/cgroup").read_text().splitlines():
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
    affinity = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    return {
        "process_swap_kib": _proc_swap_kib(),
        "host_swap_pages": _host_swap_pages(),
        "cgroup": _cgroup_snapshot(),
        "cpu_affinity": affinity,
    }


def _rss_peak_kib() -> int:
    import resource

    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value // 1024 if sys.platform == "darwin" else value


def _set_cpu(cpu: int | None) -> None:
    if cpu is not None and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, {cpu})


def _build_components(args: argparse.Namespace) -> tuple[TimeDependentAStar, PlanningRequest]:
    profile = SYNTHETIC_PROFILES[args.synthetic_profile]
    frames = synthetic_profiling._make_frames(profile)  # type: ignore[attr-defined]
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    planner = TimeDependentAStar(grid, sampler, vessel)
    request = PlanningRequest(
        start=(profile.rows // 2, 0),
        goal=(profile.rows // 2, profile.cols - 1),
        departure_time=frames[0].valid_time,
        objective=ObjectiveMode(args.objective_order[0]),
        maximum_elapsed=timedelta(hours=profile.frame_count - 1),
        maximum_risk=1.0,
        max_expansions=args.max_expansions,
    )
    return planner, request


def _worker(args: argparse.Namespace) -> int:
    _set_cpu(args.cpu)
    planner, request = _build_components(args)
    order = tuple(ObjectiveMode(value) for value in args.objective_order)
    resources_before = _resource_snapshot()
    started = time.perf_counter()
    routes: dict[str, object] = {}
    stage_diagnostics: dict[str, object] = {}
    if args.mode == "baseline":
        for objective in order:
            result = planner.plan(replace(request, objective=objective))
            routes[objective.value] = _route_record(result)
    else:
        candidate = AnytimeRepairingAStar(
            planner.grid,
            planner.risk_sampler,
            planner.vessel_model,
            planner_config=planner.planner_config,
        )
        for objective in order:
            objective_request = replace(request, objective=objective)
            ara_result = candidate.plan(objective_request, epsilon_schedule=EPSILON_SCHEDULE)
            final_stage = ara_result.final_result
            routes[objective.value] = _route_record(final_stage)
            stage_diagnostics[objective.value] = [
                {
                    "epsilon": stage.epsilon,
                    "total_cost_hours": stage.result.total_cost_hours,
                    "first_solution_cost_hours": stage.first_solution_cost_hours,
                    "first_solution_elapsed_ms": stage.first_solution_elapsed_ms,
                    "lower_bound_hours": stage.lower_bound_hours,
                    "observed_gap": stage.observed_gap,
                    "expanded_since_previous": stage.expanded_since_previous,
                }
                for stage in ara_result.stages
            ]
    elapsed = time.perf_counter() - started
    resources_after = _resource_snapshot()
    payload = {
        "mode": args.mode,
        "objective_order": [objective.value for objective in order],
        "wall_seconds": elapsed,
        "peak_rss_kib": _rss_peak_kib(),
        "resources_before": resources_before,
        "resources_after": resources_after,
        "routes": routes,
        "stage_diagnostics": stage_diagnostics,
        "input_identity": {
            "profile": args.synthetic_profile,
            "config": asdict(SYNTHETIC_PROFILES[args.synthetic_profile]),
        },
        "request": {
            "start": list(request.start),
            "goal": list(request.goal),
            "departure": request.departure_time.isoformat(),
            "maximum_elapsed_seconds": request.maximum_elapsed.total_seconds(),
            "maximum_risk": request.maximum_risk,
        },
    }
    _atomic_write_json(args.worker_output, payload)
    return 0


def _git_metadata(repo_root: Path) -> dict[str, object]:
    def run(*command: str) -> str:
        return subprocess.run(
            command, cwd=repo_root, check=True, capture_output=True, text=True
        ).stdout.strip()

    return {
        "git_sha": run("git", "rev-parse", "HEAD"),
        "git_dirty": bool(run("git", "status", "--porcelain")),
        "uv_lock_sha256": _sha256(repo_root / "uv.lock"),
        "runner_sha256": _sha256(Path(__file__)),
    }


def _identity(args: argparse.Namespace, repo_root: Path) -> dict[str, object]:
    git = _git_metadata(repo_root)
    if git["git_dirty"]:
        raise RuntimeError("evidence mode requires a clean worktree")
    identity: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "ara-star-internal",
        "git": git,
        "profile": args.synthetic_profile,
        "profile_config": asdict(SYNTHETIC_PROFILES[args.synthetic_profile]),
        "config_root": str(args.config_root.resolve()),
        "config_tree_sha256": _tree_digest(args.config_root),
        "objective_order": list(args.objective_order),
        "epsilon_schedule": list(EPSILON_SCHEDULE),
        "warmup_pairs": args.warmup_pairs,
        "repetitions": args.repetitions,
        "max_expansions": args.max_expansions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "strict_resources": args.strict_resources,
        "memory_limit_bytes": MEMORY_LIMIT_BYTES if args.strict_resources else None,
        "control_limitation": (
            "INHERITED_CONTROL_LIMITATION: approximate state graph; no general optimality claim"
        ),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    identity["identity_sha256"] = hashlib.sha256(encoded).hexdigest()
    return identity


def _pair_order(index: int) -> tuple[str, str]:
    return ("baseline", "ara") if index % 2 else ("ara", "baseline")


def _next_attempt(directory: Path, key: str) -> int:
    pattern = re.compile(rf"^{re.escape(key)}-attempt-(\d+)-(?:baseline|ara)\.json$")
    attempts = {
        int(match.group(1))
        for path in directory.glob(f"{key}-attempt-*-*.json")
        if (match := pattern.match(path.name)) is not None
    }
    return max(attempts, default=0) + 1


def _validate_pair(baseline: dict[str, object], ara: dict[str, object]) -> None:
    base_routes = baseline.get("routes")
    ara_routes = ara.get("routes")
    if not isinstance(base_routes, dict) or not isinstance(ara_routes, dict):
        raise ValueError("worker routes are malformed")
    for objective in OBJECTIVES:
        if (
            base_routes[objective.value]["semantic_digest"]
            != ara_routes[objective.value]["semantic_digest"]
        ):
            raise ValueError(f"epsilon=1 route mismatch for {objective.value}")


def _run_worker(
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
        "--synthetic-profile",
        args.synthetic_profile,
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
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=args.worker_timeout_seconds,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{mode} worker failed with exit {completed.returncode}: "
            f"stdout={completed.stdout[-2000:]} stderr={completed.stderr[-2000:]}"
        )
    return json.loads(output_path.read_text())


def _resource_failures(run: dict[str, object], strict: bool) -> list[str]:
    failures: list[str] = []
    before = run.get("resources_before")
    after = run.get("resources_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return ["resource snapshots missing"]
    if before.get("process_swap_kib") != after.get("process_swap_kib"):
        failures.append("process VmSwap changed")
    for name in ("pswpin", "pswpout"):
        before_pages = before.get("host_swap_pages")
        after_pages = after.get("host_swap_pages")
        if not isinstance(before_pages, dict) or not isinstance(after_pages, dict):
            failures.append("host swap counters not measured")
            break
        if after_pages.get(name) != before_pages.get(name):
            failures.append(f"host {name} changed")
    affinity = after.get("cpu_affinity")
    if not isinstance(affinity, list) or len(affinity) != 1:
        failures.append("worker affinity is not exactly one CPU")
    cgroup = after.get("cgroup")
    before_cgroup = before.get("cgroup")
    if not isinstance(cgroup, dict) or not isinstance(before_cgroup, dict):
        failures.append("cgroup metrics not measured")
    else:
        if cgroup.get("memory_swap_current") != 0:
            failures.append("cgroup memory.swap.current is not zero")
        for name in ("oom", "oom_kill"):
            before_events = before_cgroup.get("memory_events")
            after_events = cgroup.get("memory_events")
            if not isinstance(before_events, dict) or not isinstance(after_events, dict):
                failures.append("cgroup memory.events not measured")
                break
            if int(after_events.get(name, 0)) != int(before_events.get(name, 0)):
                failures.append(f"cgroup {name} changed")
        if strict:
            if cgroup.get("memory_max") != MEMORY_LIMIT_BYTES:
                failures.append("cgroup memory.max is not 4 GiB")
            if cgroup.get("memory_swap_max") != 0:
                failures.append("cgroup memory.swap.max is not zero")
    return failures


def _completed_indexes(cases: list[dict[str, object]], kind: str) -> set[int]:
    return {
        int(case["index"])
        for case in cases
        if case.get("kind") == kind and case.get("status") == "PASS"
    }


def _referenced(cases: list[dict[str, object]]) -> set[str]:
    result: set[str] = set()
    for case in cases:
        files = case.get("worker_files")
        if isinstance(files, dict):
            result.update(str(value) for value in files.values())
    return result


def _exclude_orphans(output_dir: Path, cases_path: Path) -> None:
    cases = _read_jsonl(cases_path)
    referenced = _referenced(cases)
    workers = output_dir / "workers"
    if not workers.exists():
        return
    for path in sorted(workers.glob("*.json")):
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


def _summarize(
    baseline_runs: list[dict[str, object]],
    ara_runs: list[dict[str, object]],
    *,
    strict_resources: bool,
) -> dict[str, object]:
    if len(baseline_runs) != len(ara_runs):
        raise ValueError("summary requires complete pairs")
    objective_summary: dict[str, object] = {}
    all_checks: dict[str, bool] = {}
    for objective in OBJECTIVES:
        base_routes = [run["routes"][objective.value] for run in baseline_runs]
        ara_routes = [run["routes"][objective.value] for run in ara_runs]
        stage_rows = [run["stage_diagnostics"][objective.value] for run in ara_runs]
        gaps = []
        first_times = []
        base_times = []
        monotonic = True
        for base_route, _ara_route, stages in zip(base_routes, ara_routes, stage_rows, strict=True):
            final_cost = float(base_route["semantic"]["total_cost_hours"])
            first_cost = float(stages[0]["first_solution_cost_hours"])
            gaps.append(first_cost / final_cost - 1.0 if final_cost > 0 else 0.0)
            first_times.append(float(stages[0]["first_solution_elapsed_ms"]))
            base_times.append(float(base_route["compute_ms"]))
            costs = [float(stage["total_cost_hours"]) for stage in stages]
            monotonic = monotonic and costs == sorted(costs, reverse=True)
        median_first = statistics.median(first_times)
        median_base = statistics.median(base_times)
        checks = {
            "epsilon_one_route_identity": all(
                base["semantic_digest"] == candidate["semantic_digest"]
                for base, candidate in zip(base_routes, ara_routes, strict=True)
            ),
            "stage_cost_monotonic": monotonic,
            "epsilon_2_5_first_solution_gap_le_10pct": max(gaps, default=float("inf")) <= 0.10,
            "first_solution_median_at_least_20pct_faster": median_first <= median_base * 0.80,
        }
        all_checks.update({f"{objective.value}.{name}": value for name, value in checks.items()})
        objective_summary[objective.value] = {
            "checks": checks,
            "first_solution_elapsed_median_ms": median_first,
            "control_compute_median_ms": median_base,
            "first_solution_improvement_pct": (1.0 - median_first / median_base) * 100.0
            if median_base > 0
            else float("nan"),
            "epsilon_2_5_first_solution_gap_max": max(gaps, default=float("nan")),
        }
    rss_ratios = [
        float(candidate["peak_rss_kib"]) / float(base["peak_rss_kib"])
        for base, candidate in zip(baseline_runs, ara_runs, strict=True)
        if float(base["peak_rss_kib"]) > 0
    ]
    resource_failures = [
        {"run_index": index, "failures": failures}
        for index, run in enumerate((*baseline_runs, *ara_runs), start=1)
        if (failures := _resource_failures(run, strict_resources))
    ]
    checks = {
        "planned_pairs_complete": True,
        "rss_ratio_le_1_10": statistics.median(rss_ratios) <= 1.10 if rss_ratios else False,
        "resources_pass": not resource_failures,
    }
    all_checks.update(checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "COMPLETED",
        "gate_verdict": "PASS" if all(all_checks.values()) else "FAIL",
        "gate_checks": all_checks,
        "objective_summary": objective_summary,
        "rss_median_ratio": statistics.median(rss_ratios) if rss_ratios else float("nan"),
        "resource_failures": resource_failures,
        "baseline_runs": baseline_runs,
        "ara_runs": ara_runs,
        "control_limitation": "INHERITED_CONTROL_LIMITATION",
    }


def _run_pair(
    args: argparse.Namespace,
    output_dir: Path,
    cases_path: Path,
    kind: str,
    index: int,
    cpu: int | None,
) -> dict[str, object]:
    directory = output_dir / ("warmups" if kind == "WARMUP" else "workers")
    directory.mkdir(parents=True, exist_ok=True)
    key = f"{kind.lower()}-{index:03d}"
    attempt = _next_attempt(directory, key)
    order = _pair_order(index)
    runs: dict[str, dict[str, object]] = {}
    paths: dict[str, str] = {}
    try:
        for mode in order:
            path = directory / f"{key}-attempt-{attempt:02d}-{mode}.json"
            runs[mode] = _run_worker(args, mode, path, cpu)
            paths[mode] = str(path.relative_to(output_dir))
        _validate_pair(runs["baseline"], runs["ara"])
    except Exception as exc:
        _append_jsonl(
            cases_path,
            {
                "kind": kind,
                "index": index,
                "attempt": attempt,
                "status": "ORPHANED_EXCLUDED" if 0 < len(paths) < 2 else "FAIL",
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
        "route_identity": "PASS",
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    _append_jsonl(cases_path, case)
    return case


def _manifest(identity: dict[str, object]) -> dict[str, object]:
    suffix = str(identity["identity_sha256"])[:12]
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": f"c-p3.2-ara-m0-{suffix}",
        "status": "PREPARED",
        "identity": identity,
        "completed_warmup_pairs": 0,
        "completed_timed_pairs": 0,
        "created_at": now,
        "updated_at": now,
    }


def _update_manifest(
    path: Path,
    manifest: dict[str, object],
    status: str,
    cases: list[dict[str, object]],
    summary: dict[str, object] | None = None,
    error: str | None = None,
) -> None:
    manifest["status"] = status
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    manifest["completed_warmup_pairs"] = len(_completed_indexes(cases, "WARMUP"))
    manifest["completed_timed_pairs"] = len(_completed_indexes(cases, "TIMED_PAIR"))
    if summary is not None:
        manifest["gate_verdict"] = summary["gate_verdict"]
        manifest["summary_sha256"] = _sha256(path.parent / "summary.json")
    if error is not None:
        manifest["error"] = error
    else:
        manifest.pop("error", None)
    _atomic_write_json(path, manifest)


def _run_evidence(args: argparse.Namespace) -> int:
    identity = _identity(args, Path(__file__).resolve().parents[1])
    output_dir = args.output_dir
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    if args.resume:
        if not manifest_path.exists():
            raise RuntimeError("--resume requires manifest.json")
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("identity") != identity:
            raise RuntimeError("resume identity does not match prepared experiment")
        _exclude_orphans(output_dir, cases_path)
    else:
        if output_dir.exists() and any(output_dir.iterdir()):
            raise RuntimeError("evidence output directory is not empty")
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest = _manifest(identity)
        _atomic_write_json(manifest_path, manifest)
    if manifest.get("status") == "COMPLETED" and (output_dir / "summary.json").exists():
        return 0
    cpu = min(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    try:
        cases = _read_jsonl(cases_path)
        _update_manifest(manifest_path, manifest, "RUNNING", cases)
        for index in range(1, args.warmup_pairs + 1):
            cases = _read_jsonl(cases_path)
            if index not in _completed_indexes(cases, "WARMUP"):
                _run_pair(args, output_dir, cases_path, "WARMUP", index, cpu)
                cases = _read_jsonl(cases_path)
                _update_manifest(manifest_path, manifest, "RUNNING", cases)
        for index in range(1, args.repetitions + 1):
            cases = _read_jsonl(cases_path)
            if index not in _completed_indexes(cases, "TIMED_PAIR"):
                _run_pair(args, output_dir, cases_path, "TIMED_PAIR", index, cpu)
                cases = _read_jsonl(cases_path)
                _update_manifest(manifest_path, manifest, "RUNNING", cases)
        cases = _read_jsonl(cases_path)
        timed = [
            case
            for case in cases
            if case.get("kind") == "TIMED_PAIR" and case.get("status") == "PASS"
        ]
        baseline_runs = [
            json.loads((output_dir / case["worker_files"]["baseline"]).read_text())
            for case in timed
        ]
        ara_runs = [
            json.loads((output_dir / case["worker_files"]["ara"]).read_text()) for case in timed
        ]
        summary = _summarize(baseline_runs, ara_runs, strict_resources=args.strict_resources)
        summary["experiment_id"] = manifest["experiment_id"]
        summary["identity"] = identity
        _atomic_write_json(output_dir / "summary.json", summary)
        _update_manifest(manifest_path, manifest, "COMPLETED", cases, summary=summary)
        print(
            f"ARA M0 gate={summary['gate_verdict']} rss_ratio={summary['rss_median_ratio']:.3f}",
            flush=True,
        )
        return 0
    except Exception as exc:
        cases = _read_jsonl(cases_path)
        _update_manifest(manifest_path, manifest, "FAIL", cases, error=str(exc))
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-profile", choices=tuple(SYNTHETIC_PROFILES), required=True)
    parser.add_argument("--config-root", type=Path, default=Path("configs"))
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--warmup-pairs", type=int, default=1)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument(
        "--objective-order",
        nargs=3,
        choices=tuple(mode.value for mode in OBJECTIVES),
        default=[mode.value for mode in OBJECTIVES],
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--strict-resources", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("baseline", "ara"), help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--cpu", type=int, default=-1, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.repetitions < 1 or args.warmup_pairs < 0 or args.worker_timeout_seconds <= 0:
        raise ValueError("invalid repetition, warmup, or timeout value")
    if args.worker:
        if args.mode is None or args.worker_output is None:
            raise ValueError("worker mode requires --mode and --worker-output")
        args.cpu = None if args.cpu < 0 else args.cpu
        return _worker(args)
    if args.output_dir is None:
        raise ValueError("parent mode requires --output-dir")
    return _run_evidence(args)


if __name__ == "__main__":
    raise SystemExit(main())
