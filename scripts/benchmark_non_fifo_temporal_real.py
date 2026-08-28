#!/usr/bin/env python3
"""Research-only real-input evidence for the non-FIFO temporal adapter.

This runner is deliberately separate from the historical P0.1 qualification
runners.  It exercises the explicit ``non_fifo_temporal_adapter`` against the
already frozen 145-frame windows, with exact-arrival/zero-heuristic search and
all temporal dominance and state-bound paths disabled.  A successful record is
correctness evidence only; it never authorizes a production planner or a
candidate feature.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import resource
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts import risk_frame_content_digest
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity

SCHEMA_VERSION = "c.p0.2-temporal-adapter-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
SEGMENTS = {"executable_0_6h", "rolling_0_24h"}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/time_dependent_astar.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
)
TERMINAL_STATUSES = {item.value for item in NonFifoSearchStatus}


@dataclass(frozen=True, slots=True)
class _WorkerInputs:
    risk_window_commit: Path
    route_plan_set: Path
    config_root: Path
    segment: str
    objective: str
    repetition: int
    cpu: int


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_real_point_runner_for_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the frozen real-input fixture loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "total_seconds") and callable(value.total_seconds):
        return value.total_seconds()
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _jsonable(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
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


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    values: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict):
            values.append(value)
        else:
            malformed += 1
    return values, malformed


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _set_cpu_affinity(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    host_swap: dict[str, int] = {}
    try:
        for line in Path("/proc/vmstat").read_text().splitlines():
            name, raw = line.split()
            if name in {"pswpin", "pswpout"}:
                host_swap[name] = int(raw)
    except (FileNotFoundError, OSError, ValueError):
        host_swap = {}
    process_swap = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                process_swap = int(line.split()[1])
                break
    except (FileNotFoundError, OSError, ValueError):
        pass
    cgroup = None
    try:
        relative = next(
            candidate.lstrip("/")
            for hierarchy, controllers, candidate in (
                line.split(":", 2) for line in Path("/proc/self/cgroup").read_text().splitlines()
            )
            if hierarchy == "0" and controllers == ""
        )
        root = Path("/sys/fs/cgroup") / relative

        def scalar(name: str) -> int | str | None:
            try:
                value = (root / name).read_text().strip()
            except OSError:
                return None
            if value == "max":
                return value
            try:
                return int(value)
            except ValueError:
                return None

        try:
            events = {
                key: int(value)
                for key, value in (
                    line.split() for line in (root / "memory.events").read_text().splitlines()
                )
            }
        except (OSError, ValueError):
            events = None
        cgroup = {
            "path": f"/{relative}",
            "memory_current": scalar("memory.current"),
            "memory_peak": scalar("memory.peak"),
            "memory_max": scalar("memory.max"),
            "memory_swap_current": scalar("memory.swap.current"),
            "memory_swap_max": scalar("memory.swap.max"),
            "memory_events": events,
        }
    except (FileNotFoundError, OSError, ValueError, StopIteration):
        pass
    return {
        "process_swap_kib": process_swap,
        "host_swap_pages": host_swap or None,
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "cgroup": cgroup,
    }


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_swap = before.get("process_swap_kib")
    after_swap = after.get("process_swap_kib")
    if before_swap is not None and after_swap is not None and after_swap > before_swap:
        return False
    before_host = before.get("host_swap_pages")
    after_host = after.get("host_swap_pages")
    if before_host and after_host and before_host != after_host:
        return False
    for snapshot in (before, after):
        cgroup = snapshot.get("cgroup") or {}
        events = cgroup.get("memory_events") or {}
        if any(events.get(key, 0) > 0 for key in ("oom", "oom_kill", "oom_group_kill")):
            return False
        swap = cgroup.get("memory_swap_current")
        if isinstance(swap, int) and swap > 0:
            return False
    return True


def _resource_evidence_complete(record: dict[str, Any], cpu: int) -> bool:
    before = record.get("resources_before") or {}
    after = record.get("resources_after") or {}
    if before.get("cpu_affinity") is None or after.get("cpu_affinity") is None:
        return False
    if cpu >= 0 and (
        before.get("cpu_affinity") != [cpu] or after.get("cpu_affinity") != [cpu]
    ):
        return False
    return before.get("cgroup") is not None and after.get("cgroup") is not None


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _make_inputs(args: argparse.Namespace) -> _WorkerInputs:
    if args.objective is None:
        raise ValueError("worker requires --objective")
    return _WorkerInputs(
        risk_window_commit=Path(args.risk_window_commit).resolve(),
        route_plan_set=Path(args.route_plan_set).resolve(),
        config_root=Path(args.config_root).resolve(),
        segment=args.segment,
        objective=args.objective,
        repetition=args.repetition,
        cpu=args.cpu,
    )


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    _set_cpu_affinity(args.cpu)
    inputs = _make_inputs(args)
    started = time.perf_counter()
    before = _resource_snapshot()
    result = None
    semantic = None
    reference = None
    reference_match = None
    planner_error = None
    reference_error = None
    session_identity = None
    try:
        point = _load_point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(inputs.objective)
        planner = point._build_planner(fixture, objective)
        request = replace(point._request(fixture, objective), use_heuristic=False)
        identity = TemporalSessionIdentity.from_planner(
            planner,
            request,
            input_revision=0,
            risk_window_content_digest=fixture.commit["content_digest"],
            risk_window_commit_id=fixture.commit["commit_id"],
        )
        session_identity = identity.digest
        result = run_non_fifo_temporal_search(planner, request, identity=identity)
        if result.status is NonFifoSearchStatus.GOAL_FOUND:
            semantic = point._route_semantic(result)
            try:
                reference = point._reference_search(planner, request)
                reference_match = point._reference_matches(semantic, reference)
            except Exception as error:  # reference failure is evidence, not a route success
                reference_error = {"type": type(error).__name__, "message": str(error)}
        else:
            semantic = None
    except NonFifoTemporalAdapterError as error:
        planner_error = {"type": type(error).__name__, "message": str(error)}
    except Exception as error:  # pragma: no cover - defensive worker boundary
        planner_error = {"type": type(error).__name__, "message": str(error)}
    after = _resource_snapshot()
    wall_seconds = time.perf_counter() - started
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    status = result.status.value if result is not None else "INVALID/PENDING"
    unexpected_pruning = bool(
        diagnostics
        and (
            diagnostics.get("dominance_pruned", 0)
            or diagnostics.get("state_bound_pruned", 0)
            or diagnostics.get("dominance_checks", 0)
            or diagnostics.get("state_bound_checks", 0)
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "real-adapter",
        "input": getattr(locals().get("fixture", None), "input_name", None),
        "segment": inputs.segment,
        "objective": inputs.objective,
        "repetition": inputs.repetition,
        "adapter_mode": "non_fifo_zero_heuristic_v2",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "status": status,
        "session_identity": session_identity,
        "session_id": result.session_id if result is not None else None,
        "semantic": semantic,
        "semantic_digest": result.semantic_digest if result is not None else None,
        "reference": reference,
        "reference_match": reference_match,
        "unexpected_pruning": unexpected_pruning,
        "reason": result.reason if result is not None else None,
        "error_type": result.error_type if result is not None else None,
        "error_message": result.error_message if result is not None else None,
        "planner_error": planner_error,
        "reference_error": reference_error,
        "diagnostics": diagnostics,
        "compute_ms": (
            result.planning_result.metrics.compute_ms
            if result is not None and result.planning_result is not None
            else None
        ),
        "wall_seconds": wall_seconds,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(before, after),
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {"files": files, "sha256": _canonical_digest(files)}


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": _implementation_identity(root),
        "uv_lock": {
            "path": str((root / "uv.lock").resolve()),
            "sha256": _sha256(root / "uv.lock"),
        },
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
            "frame_identities": [
                {
                    "risk_id": frame.risk_id,
                    "valid_time": frame.valid_time,
                    "generation_id": frame.generation_id,
                    "content_digest": risk_frame_content_digest(frame),
                }
                for frame in fixture.frames
            ],
        },
        "route_plan_set": {
            "path": str(fixture.route_plan_path),
            "sha256": _sha256(fixture.route_plan_path),
        },
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "adapter_mode": "non_fifo_zero_heuristic_v2",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
        "cpu": args.cpu,
    }


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--risk-window-commit",
        str(Path(args.risk_window_commit).resolve()),
        "--route-plan-set",
        str(Path(args.route_plan_set).resolve()),
        "--config-root",
        str(Path(args.config_root).resolve()),
        "--output-dir",
        str(Path(args.output_dir).resolve()),
        "--segment",
        args.segment,
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
    ]


def _run_child(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    heartbeat: Path,
) -> dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "real-adapter",
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "planner_error": {"type": type(error).__name__, "message": str(error)},
        }
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
            stdout, stderr = process.communicate()
            return {
                "schema_version": SCHEMA_VERSION,
                "mode": "real-adapter",
                "objective": objective.value,
                "repetition": repetition,
                "status": "TIMEOUT",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "wall_seconds": elapsed,
            }
        time.sleep(1.0)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "real-adapter",
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "returncode": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        record = {
            "schema_version": SCHEMA_VERSION,
            "mode": "real-adapter",
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "reason": "worker did not emit one JSON document",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _record_key(record: dict[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or not isinstance(repetition, int):
        return None
    return objective, repetition


def _load_resume_cases(output: Path, experiment_id: str) -> tuple[list[dict[str, Any]], int]:
    records, malformed = _read_jsonl(output / "resource-frontier.jsonl")
    if not records:
        records, malformed = _read_jsonl(output / "cases.jsonl")
    by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if key is None or record.get("schema_version") != SCHEMA_VERSION:
            continue
        if record.get("experiment_id") not in (None, experiment_id):
            raise RuntimeError("resume evidence belongs to another experiment identity")
        if record.get("mode") != "real-adapter":
            raise RuntimeError("resume evidence has an unexpected mode")
        if (
            record.get("dominance_policy") != "disabled"
            or record.get("state_bound_policy") != "absent"
        ):
            raise RuntimeError("resume evidence violates the adapter safety fence")
        if key in by_key:
            raise RuntimeError("resume evidence contains duplicate complete worker records")
        by_key[key] = record
    return list(by_key.values()), malformed


def _metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for objective in OBJECTIVES:
        selected = [case for case in cases if case.get("objective") == objective.value]
        statuses = Counter(str(case.get("status")) for case in selected)
        digests = [case.get("semantic_digest") for case in selected if case.get("semantic_digest")]
        diagnostics = [case.get("diagnostics") or {} for case in selected]
        values[objective.value] = {
            "case_count": len(selected),
            "statuses": dict(statuses),
            "semantic_digests": digests,
            "deterministic": len(digests) >= 2 and len(set(digests)) == 1,
            "expanded_labels": sum(int(item.get("expanded_labels", 0)) for item in diagnostics),
            "generated_labels": sum(int(item.get("generated_labels", 0)) for item in diagnostics),
            "queue_peak": max((int(item.get("queue_peak", 0)) for item in diagnostics), default=0),
            "edge_evaluations": sum(int(item.get("edge_evaluations", 0)) for item in diagnostics),
            "resource_limits": sum(
                1 for case in selected if case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
            ),
        }
    return values


def _summary(
    cases: list[dict[str, Any]],
    *,
    repetitions: int,
    cpu: int,
    ignored_records: int,
) -> dict[str, Any]:
    expected = len(OBJECTIVES) * repetitions
    complete = len(cases) == expected and all(
        _record_key(case) is not None and case.get("status") in TERMINAL_STATUSES | {
            "TIMEOUT",
            "INVALID/PENDING",
        }
        for case in cases
    )
    semantic_failure = any(
        case.get("status") == "GOAL_FOUND"
        and (case.get("reference_match") is not True or case.get("unexpected_pruning"))
        for case in cases
    )
    identity_failure = any(case.get("status") == "INVALID/PENDING" for case in cases)
    goals = [case for case in cases if case.get("status") == "GOAL_FOUND"]
    resources_clean = all(case.get("resource_clean") is True for case in cases)
    resources_complete = all(_resource_evidence_complete(case, cpu) for case in cases)
    if semantic_failure:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif not complete or identity_failure:
        status = "INVALID/PENDING"
    elif len(goals) != expected or not resources_clean or not resources_complete:
        status = "REAL_INPUT_ADAPTER_RESOURCE_FAIL"
    elif repetitions < 2 or any(not value["deterministic"] for value in _metrics(cases).values()):
        status = "REAL_INPUT_ADAPTER_PARTIAL"
    else:
        status = "READY_FOR_P0.2-ADAPTER_REAL-EVIDENCE_REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "real-adapter",
        "status": status,
        "expected_case_count": expected,
        "case_count": len(cases),
        "ignored_incomplete_records": ignored_records,
        "all_goal_found": len(goals) == expected,
        "all_reference_match": bool(cases) and all(
            case.get("status") != "GOAL_FOUND" or case.get("reference_match") is True
            for case in cases
        ),
        "all_resource_clean": resources_clean,
        "resource_evidence_complete": resources_complete,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "metrics": _metrics(cases),
        "cases": cases,
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "candidate_authorized": False,
        "winter_authorized": False,
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
            raise RuntimeError("another real-adapter runner already owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("real adapter evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_canonical_digest(identity)[:16]}"
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.resume:
                raise RuntimeError("experiment already exists; use --resume to continue")
            if previous.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity does not match prepared experiment")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": (
                "manifest.json",
                "cases.jsonl",
                "resource-frontier.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ),
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        heartbeat = output / "heartbeat.json"
        cases, ignored = (
            _load_resume_cases(output, identity["experiment_id"])
            if args.resume
            else ([], 0)
        )
        completed = {key for case in cases if (key := _record_key(case)) is not None}
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    key = (objective.value, repetition)
                    if key in completed:
                        continue
                    record = _run_child(args, objective, repetition, heartbeat)
                    record.setdefault("schema_version", SCHEMA_VERSION)
                    record.setdefault("mode", "real-adapter")
                    record["experiment_id"] = identity["experiment_id"]
                    if (
                        record.get("objective") != objective.value
                        or record.get("repetition") != repetition
                    ):
                        raise RuntimeError("worker returned a mismatched case identity")
                    cases.append(record)
                    _append_jsonl(output / "resource-frontier.jsonl", record)
                    _append_jsonl(output / "cases.jsonl", record)
                    completed.add(key)
                    _atomic_json(
                        heartbeat,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(cases),
                            "expected_cases": len(OBJECTIVES) * args.repetitions,
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
            manifest.update(
                {
                    "status": "STOPPED_HARD",
                    "summary": summary,
                    "completed_at": datetime.now(UTC),
                }
            )
            _atomic_json(manifest_path, manifest)
            _atomic_json(
                heartbeat,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "STOPPED_HARD",
                    "updated_at": datetime.now(UTC),
                },
            )
            (output / "STOPPED_HARD").write_text(summary["reason"] + "\n", encoding="utf-8")
            return 2
        summary = _summary(
            cases,
            repetitions=args.repetitions,
            cpu=args.cpu,
            ignored_records=ignored,
        )
        _atomic_json(output / "comparison-summary.json", summary)
        final_status = summary["status"]
        manifest.update(
            {
                "status": final_status,
                "summary": summary,
                "completed_at": datetime.now(UTC),
            }
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": final_status,
                "updated_at": datetime.now(UTC),
            },
        )
        (output / "ALL_DONE").write_text(final_status + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if final_status
        in {
            "READY_FOR_P0.2-ADAPTER_REAL-EVIDENCE_REVIEW",
            "REAL_INPUT_ADAPTER_PARTIAL",
            "REAL_INPUT_ADAPTER_RESOURCE_FAIL",
        }
        else 2
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(sorted(SEGMENTS)), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("repetitions/timeout must be positive and cpu must be -1 or non-negative")
    if args.worker:
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
