#!/usr/bin/env python3
"""Research-only selected-route terminal-bound qualification.

The terminal bound is intentionally narrower than a Pareto-frontier proof.  It
may discard a newly generated label only when an already observed terminal
label is lexicographically better than the label's conservative completion
bound.  The runner compares that selected route with a dominance-disabled
exact-arrival search and never enables the production planner or candidate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import resource
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoParetoTerminalBoundCertificate,
    NonFifoSearchStatus,
)
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    TEMPORAL_PARETO_COMPONENTS,
    run_non_fifo_temporal_pareto_search,
)

SCHEMA_VERSION = "c.p0.2-nonfifo-selected-route-bound-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
SEGMENTS = ("executable_0_6h", "rolling_0_24h")
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_selected_route_bound_real.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/time_dependent_astar.py",
    "uv.lock",
)


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_m25_real_point_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen real-input point runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("selected-route evidence contains a non-finite float")
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _context(args: argparse.Namespace, objective: ObjectiveMode) -> tuple[Any, Any, Any, Any, Any]:
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    planner = point._build_planner(fixture, objective)
    planner.eta_policy = EtaRefinementPolicy(method="bounded")
    request = replace(point._request(fixture, objective), use_heuristic=False, cancel_check=None)
    scope = planner.temporal_scope(request)
    return point, fixture, planner, request, scope


def _terminal_certificate(planner: Any, request: Any, scope: Any) -> tuple[Any, dict[str, Any]]:
    """Build a geometric lower-bound certificate for every finite state."""

    cost_model = planner._cost_model(ObjectiveMode(request.objective))
    bounds: dict[Any, tuple[float, ...]] = {}
    rows, columns = planner.grid.shape
    direction_codes = {
        (neighbor[0] - row, neighbor[1] - column)
        for row in range(rows)
        for column in range(columns)
        for neighbor in planner.grid.neighbors((row, column))
    }
    for row in range(rows):
        for column in range(columns):
            node = (row, column)
            first = cost_model.lower_bound(planner.grid.distance_km(node, request.goal))
            vector = (first, *(0.0 for _ in range(len(TEMPORAL_PARETO_COMPONENTS) - 1)))
            for heading in (None, *sorted(direction_codes)):
                bounds[(node, heading)] = vector
    proof_digest = _digest(
        {
            "schema": SCHEMA_VERSION,
            "scope_digest": scope.digest,
            "goal": request.goal,
            "objective": ObjectiveMode(request.objective),
            "bounds": bounds,
            "selection_only": True,
        }
    )
    certificate = NonFifoParetoTerminalBoundCertificate.certified(
        scope_digest=scope.digest,
        goal=(request.goal, None),
        objective_count=len(TEMPORAL_PARETO_COMPONENTS),
        node_lower_bounds=bounds,
        proof_digest=proof_digest,
    )
    return certificate, {
        "digest": certificate.digest,
        "proof_digest": proof_digest,
        "state_count": len(bounds),
        "scope_digest": scope.digest,
        "selection_only": True,
    }


def _resource_snapshot() -> dict[str, Any]:
    process_swap = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                process_swap = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass
    host_swap = {}
    try:
        for line in Path("/proc/vmstat").read_text().splitlines():
            key, raw = line.split()
            if key in {"pswpin", "pswpout"}:
                host_swap[key] = int(raw)
    except (OSError, ValueError):
        host_swap = {}
    return {
        "process_swap_kib": process_swap,
        "host_swap_pages": host_swap or None,
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
    }


def _resource_clean(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    process_ok = not (
        before.get("process_swap_kib") is not None
        and after.get("process_swap_kib") is not None
        and after["process_swap_kib"] > before["process_swap_kib"]
    )
    before_host = before.get("host_swap_pages") or {}
    after_host = after.get("host_swap_pages") or {}
    host_ok = not before_host or before_host == after_host
    return process_ok and host_ok


def _set_cpu(cpu: int) -> None:
    if cpu >= 0:
        if not hasattr(os, "sched_setaffinity"):
            raise RuntimeError("fixed CPU evidence is unavailable")
        os.sched_setaffinity(0, {cpu})


def _route_record(result: Any) -> dict[str, Any] | None:
    route = result.selected
    if route is None:
        return None
    return {
        "nodes": [list(node) for node in route.nodes],
        "arrival_times": [
            value.astimezone(UTC).isoformat(timespec="microseconds")
            for value in route.arrival_times
        ],
        "costs": route.costs,
        "semantic_digest": route.semantic_digest,
        "steps": [
            {
                "eta": step.eta.astimezone(UTC).isoformat(timespec="microseconds"),
                "speed_knots": step.speed_knots,
                "risk_score": step.risk_score,
                "maximum_risk": step.maximum_risk,
                "confidence": step.confidence,
                "source_risk_ids": step.source_risk_ids,
            }
            for step in route.steps
        ],
    }


def _worker_record(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    _set_cpu(args.cpu)
    before = _resource_snapshot()
    fixture = None
    certificate = None
    baseline = None
    selected = None
    errors: dict[str, str] = {}
    certificate_record: dict[str, Any] | None = None
    try:
        _point, fixture, planner, request, scope = _context(args, ObjectiveMode(args.objective))
        certificate, certificate_record = _terminal_certificate(planner, request, scope)
        baseline = run_non_fifo_temporal_pareto_search(
            planner, request, pareto_pruning=False, skip_expected_rejections=True
        )
        selected = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=False,
            skip_expected_rejections=True,
            incumbent_bound_certificate=certificate,
        )
    except Exception as error:  # pragma: no cover - process boundary
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_route = _route_record(baseline) if baseline is not None else None
    selected_route = _route_record(selected) if selected is not None else None
    semantic_match = bool(
        baseline is not None
        and selected is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and selected.status is NonFifoSearchStatus.GOAL_FOUND
        and baseline.semantic_digest == selected.semantic_digest
        and selected.selection_only
        and not selected.frontier_complete
    )
    bound_authorized = bool(
        selected is not None and selected.incumbent_bound_rejected == 0 and selected.selection_only
    )
    resource_clean = _resource_clean(before, after)
    if errors:
        status = "INVALID/FAIL"
    elif (
        baseline is not None
        and selected is not None
        and (
            baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT
            or selected.status is NonFifoSearchStatus.RESOURCE_LIMIT
        )
    ):
        status = "RESOURCE_LIMIT"
    elif semantic_match and bound_authorized and resource_clean:
        status = "READY_FOR_SELECTED_ROUTE_BOUND_REVIEW"
    else:
        status = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "status": status,
        "baseline_status": baseline.status.value if baseline is not None else None,
        "selected_status": selected.status.value if selected is not None else None,
        "baseline_reason": baseline.reason if baseline is not None else None,
        "selected_reason": selected.reason if selected is not None else None,
        "semantic_match": semantic_match,
        "bound_authorized": bound_authorized,
        "selection_only": selected.selection_only if selected is not None else False,
        "frontier_complete": selected.frontier_complete if selected is not None else False,
        "incumbent_bound_pruned": selected.incumbent_bound_pruned if selected is not None else 0,
        "incumbent_bound_rejected": (
            selected.incumbent_bound_rejected if selected is not None else 0
        ),
        "incumbent_bound_rejection_reasons": (
            selected.incumbent_bound_rejection_reasons if selected is not None else ()
        ),
        "certificate": certificate_record,
        "baseline": baseline_route,
        "selected": selected_route,
        "search_stats": {
            "baseline": _stats(baseline),
            "selected": _stats(selected),
        },
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "errors": errors,
    }


def _stats(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    raw = result.raw_result
    return {
        "expanded": raw.expanded,
        "generated": raw.generated,
        "queue_peak": raw.queue_peak,
        "edge_evaluations": raw.edge_evaluations,
        "pareto_pruned": raw.pareto_pruned,
        "incumbent_bound_pruned": raw.incumbent_bound_pruned,
        "incumbent_bound_rejected": raw.incumbent_bound_rejected,
        "frontier_complete": raw.frontier_complete,
        "selection_only": raw.selection_only,
        "search_limits": raw.search_limits,
    }


class _RunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> _RunnerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            raise RuntimeError("another M25 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    point = _load_point_runner()
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    scope_digests = {}
    for objective in OBJECTIVES:
        planner = point._build_planner(fixture, objective)
        planner.eta_policy = EtaRefinementPolicy(method="bounded")
        request = replace(
            point._request(fixture, objective), use_heuristic=False, cancel_check=None
        )
        scope_digests[objective.value] = planner.temporal_scope(request).digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M25",
        "mode": args.mode,
        "git": _git_identity(root),
        "implementation": {"files": files, "sha256": _digest(files)},
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "commit_id": fixture.commit["commit_id"],
            "content_digest": fixture.commit["content_digest"],
            "frame_count": len(fixture.frames),
            "frame_digests": [point.risk_frame_content_digest(frame) for frame in fixture.frames],
        },
        "route_plan_set": {
            "path": str(fixture.route_plan_path),
            "sha256": _sha256(fixture.route_plan_path),
        },
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "scope_digests": scope_digests,
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "bound_policy": "selected_route_terminal_lexicographic",
        "selection_only": True,
        "search_limits": LIMITS,
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _case_key(record: Mapping[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or objective not in {item.value for item in OBJECTIVES}:
        return None
    if not isinstance(repetition, int) or repetition < 1:
        return None
    return objective, repetition


def _summary(
    cases: list[dict[str, Any]], identity: Mapping[str, Any], malformed: int
) -> dict[str, Any]:
    expected = len(OBJECTIVES) * int(identity["repetitions"])
    keys = [_case_key(case) for case in cases]
    complete = (
        len(cases) == expected
        and malformed == 0
        and None not in keys
        and len(set(keys)) == len(keys)
    )
    identity_clean = (identity.get("git") or {}).get("dirty") is False
    all_ready = (
        complete
        and identity_clean
        and all(
            case.get("status") == "READY_FOR_SELECTED_ROUTE_BOUND_REVIEW"
            and case.get("semantic_match") is True
            and case.get("selection_only") is True
            and case.get("frontier_complete") is False
            for case in cases
        )
    )
    if not complete or not identity_clean:
        status = "INVALID/PENDING"
    elif any(case.get("status") == "INVALID/FAIL" for case in cases):
        status = "INVALID/FAIL"
    elif any(case.get("status") == "RESOURCE_LIMIT" for case in cases):
        status = "REAL_SELECTED_ROUTE_BOUND_RESOURCE_FAIL"
    elif all_ready:
        status = "READY_FOR_SEPARATE_SELECTED_ROUTE_BOUND_PLAN"
    else:
        status = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity.get("experiment_id"),
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "identity_clean": identity_clean,
        "selection_only": True,
        "candidate_authorized": False,
        "winter_authorized": False,
        "incumbent_bound_pruned_total": sum(
            int(case.get("incumbent_bound_pruned", 0) or 0) for case in cases
        ),
        "cases": cases,
    }


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--mode",
        args.mode,
        "--risk-window-commit",
        str(Path(args.risk_window_commit).resolve()),
        "--route-plan-set",
        str(Path(args.route_plan_set).resolve()),
        "--config-root",
        str(Path(args.config_root).resolve()),
        "--segment",
        args.segment,
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--cpu",
        str(args.cpu),
    ]


def _run_child(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, heartbeat: Path
) -> dict[str, Any]:
    started = time.time()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        _child_command(args, objective, repetition),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    while process.poll() is None:
        elapsed = time.time() - started
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "updated_at": datetime.now(UTC),
                "pid": process.pid,
                "objective": objective.value,
                "repetition": repetition,
                "elapsed_seconds": elapsed,
            },
        )
        if elapsed > args.worker_timeout_seconds:
            process.kill()
            process.wait()
            return {
                "schema_version": SCHEMA_VERSION,
                "objective": objective.value,
                "repetition": repetition,
                "status": "RESOURCE_LIMIT",
                "selected_status": "RESOURCE_LIMIT",
                "reason": "worker_timeout",
                "semantic_match": False,
                "selection_only": True,
                "frontier_complete": False,
                "incumbent_bound_pruned": 0,
            }
        time.sleep(0.2)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/FAIL",
            "reason": "worker_nonzero",
            "stderr": stderr[-4000:],
            "stdout": stdout[-4000:],
        }
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/FAIL",
            "reason": "worker_invalid_json",
            "stderr": stderr[-4000:],
            "stdout": stdout[-4000:],
        }
    if not isinstance(value, dict):
        raise RuntimeError("selected-route worker emitted a non-object JSON record")
    return value


def _run_parent(args: argparse.Namespace) -> int:
    if args.output_dir is None:
        raise SystemExit("--output-dir is required")
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    root = Path(__file__).resolve().parents[1]
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M25 real evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    output = args.output_dir.resolve()
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            if not args.resume:
                raise RuntimeError("experiment already exists; use --resume")
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if previous.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity does not match")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": [
                "manifest.json",
                "cases.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ],
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        cases: list[dict[str, Any]] = []
        malformed = 0
        if args.resume and cases_path.exists():
            for line in cases_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if isinstance(value, dict):
                    cases.append(value)
                else:
                    malformed += 1
        completed = {_case_key(case) for case in cases}
        completed.discard(None)
        expected = len(OBJECTIVES) * args.repetitions
        heartbeat = output / "heartbeat.json"
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "completed_cases": len(cases),
                "expected_cases": expected,
            },
        )
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    key = (objective.value, repetition)
                    if key in completed:
                        continue
                    record = _run_child(args, objective, repetition, heartbeat)
                    record.setdefault("schema_version", SCHEMA_VERSION)
                    record["experiment_id"] = identity["experiment_id"]
                    if _case_key(record) != key:
                        raise RuntimeError("worker returned mismatched case identity")
                    cases.append(record)
                    _append_jsonl(cases_path, record)
                    completed.add(key)
                    _atomic_json(
                        heartbeat,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(cases),
                            "expected_cases": expected,
                            "last_case": key,
                        },
                    )
        except (KeyboardInterrupt, SystemExit) as error:
            summary = {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "reason": f"runner interrupted: {type(error).__name__}",
                "cases": cases,
            }
            _atomic_json(output / "comparison-summary.json", summary)
            manifest.update({"status": "STOPPED_HARD", "summary": summary})
            _atomic_json(manifest_path, manifest)
            (output / "STOPPED_HARD").write_text(summary["reason"] + "\n", encoding="utf-8")
            return 2
        summary = _summary(cases, identity, malformed)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {
                "status": summary["status"],
                "summary": {key: value for key, value in summary.items() if key != "cases"},
                "completed_at": datetime.now(UTC),
            }
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "updated_at": datetime.now(UTC),
                "completed_cases": len(cases),
                "expected_cases": expected,
            },
        )
        marker = output / "ALL_DONE"
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        with marker.open("a", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["status"] == "READY_FOR_SEPARATE_SELECTED_ROUTE_BOUND_PLAN" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("selected-route",), default="selected-route")
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--objective", choices=tuple(item.value for item in OBJECTIVES), help=argparse.SUPPRESS
    )
    parser.add_argument("--repetition", type=int, default=1, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_jsonable(_worker_record(args)), ensure_ascii=False, sort_keys=True))
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
