#!/usr/bin/env python3
"""Audit resumability of the C-internal actual temporal session.

This runner is deliberately narrower than the historical real-input adapter
runner.  It compares a one-shot exact-arrival session with a session that is
paused, checkpointed, restored, and then completed under the same identity.
The cancelled mode exercises the same boundary without producing a partial
route.  No dominance, heuristic, or state-bound certificate is accepted.

The runner is evidence infrastructure only.  It is not imported by the
formal planner and it never authorizes a production candidate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.contracts import risk_frame_content_digest
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    NonFifoTemporalResearchCheckpoint,
    create_non_fifo_temporal_session,
    restore_non_fifo_temporal_session,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity

SCHEMA_VERSION = "c.p0.2-temporal-session-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
MODES = ("one_shot", "slice_restore", "cancelled")
SEGMENTS = ("executable_0_6h", "rolling_0_24h")
TERMINAL_STATUSES = {status.value for status in NonFifoSearchStatus}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_session.py",
    "scripts/benchmark_non_fifo_temporal_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/time_dependent_astar.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
)


@dataclass(frozen=True, slots=True)
class _WorkerInputs:
    risk_window_commit: Path
    route_plan_set: Path
    config_root: Path
    segment: str
    objective: str
    repetition: int
    mode: str
    cpu: int
    slice_expansions: int


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_real_point_runner_for_session", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the frozen real-input fixture loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "total_seconds") and callable(value.total_seconds):
        return value.total_seconds()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if is_dataclass(value):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        raise ValueError("evidence contains a non-finite float")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
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
    records: list[dict[str, Any]] = []
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
            records.append(value)
        else:
            malformed += 1
    return records, malformed


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
    point = _load_point_runner()
    return point._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    point = _load_point_runner()
    return bool(point._resource_clean(before, after))


def _resource_evidence_complete(record: dict[str, Any], cpu: int) -> bool:
    before = record.get("resources_before") or {}
    after = record.get("resources_after") or {}
    if before.get("cpu_affinity") is None or after.get("cpu_affinity") is None:
        return False
    if cpu >= 0 and (before.get("cpu_affinity") != [cpu] or after.get("cpu_affinity") != [cpu]):
        return False
    # The parent driver may run without systemd/cgroup delegation.  Treat that
    # as incomplete evidence rather than silently claiming a qualified point.
    for snapshot in (before, after):
        cgroup = snapshot.get("cgroup") or {}
        if cgroup.get("memory_events") is None:
            return False
    return True


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
    if args.mode not in MODES:
        raise ValueError(f"worker mode must be one of {MODES}")
    return _WorkerInputs(
        risk_window_commit=Path(args.risk_window_commit).resolve(),
        route_plan_set=Path(args.route_plan_set).resolve(),
        config_root=Path(args.config_root).resolve(),
        segment=args.segment,
        objective=args.objective,
        repetition=args.repetition,
        mode=args.mode,
        cpu=args.cpu,
        slice_expansions=args.slice_expansions,
    )


def _identity(point: Any, fixture: Any, planner: Any, request: Any) -> TemporalSessionIdentity:
    return TemporalSessionIdentity.from_planner(
        planner,
        request,
        input_revision=0,
        risk_window_content_digest=fixture.commit["content_digest"],
        risk_window_commit_id=fixture.commit["commit_id"],
    )


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    inputs = _make_inputs(args)
    _set_cpu_affinity(inputs.cpu)
    started = time.perf_counter()
    before = _resource_snapshot()
    point = None
    fixture = None
    identity = None
    result = None
    planner_error = None
    semantic = None
    reference = None
    reference_match = None
    checkpoint_meta: dict[str, Any] = {}
    initial_diagnostics = None
    try:
        point = _load_point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(inputs.objective)
        planner = point._build_planner(fixture, objective)
        request = point._request(fixture, objective)
        # PlanningRequest is frozen/slots; replace is intentionally imported
        # lazily to keep this runner's public surface minimal.
        from dataclasses import replace

        request = replace(request, use_heuristic=False)
        identity = _identity(point, fixture, planner, request)
        if inputs.mode == "one_shot":
            result = run_non_fifo_temporal_search(planner, request, identity=identity)
        else:
            research_session = create_non_fifo_temporal_session(
                planner,
                request,
                identity=identity,
            )
            initial = research_session.advance(expansion_slice=inputs.slice_expansions)
            initial_diagnostics = _jsonable(research_session.session.context.diagnostics.freeze())
            if initial is not None:
                checkpoint_meta = {
                    "reached": False,
                    "state": research_session.state,
                    "reason": "slice_completed_without_pause",
                }
                raise RuntimeError("session slice did not pause before terminal result")
            checkpoint = research_session.checkpoint()
            if not isinstance(checkpoint, NonFifoTemporalResearchCheckpoint):
                raise RuntimeError("adapter did not return its checkpoint type")
            checkpoint_meta = {
                "reached": True,
                "state": checkpoint.session_checkpoint.state,
                "digest": checkpoint.digest,
                "session_checkpoint_digest": checkpoint.session_checkpoint.digest,
                "session_id": research_session.session_id,
            }
            if inputs.mode == "slice_restore":
                restored = restore_non_fifo_temporal_session(
                    planner,
                    checkpoint,
                    request,
                    identity=identity,
                )
                checkpoint_meta["restored_session_id"] = restored.session_id
                result = restored.run()
            else:
                cancelled_request = replace(request, cancel_check=lambda: True)
                restored = restore_non_fifo_temporal_session(
                    planner,
                    checkpoint,
                    cancelled_request,
                    identity=identity,
                )
                checkpoint_meta["restored_session_id"] = restored.session_id
                result = restored.advance(expansion_slice=inputs.slice_expansions)
                if result is None:
                    raise RuntimeError("cancelled session unexpectedly paused without a result")
    except NonFifoTemporalAdapterError as error:
        planner_error = {"type": type(error).__name__, "message": str(error)}
    except Exception as error:  # pragma: no cover - defensive worker boundary
        planner_error = {"type": type(error).__name__, "message": str(error)}

    if result is not None and result.status is NonFifoSearchStatus.GOAL_FOUND:
        try:
            semantic = point._route_semantic(result)
            reference = point._reference_search(planner, request)
            reference_match = point._reference_matches(semantic, reference)
        except Exception as error:  # reference failure remains explicit evidence
            reference_match = False
            planner_error = planner_error or {
                "type": type(error).__name__,
                "message": str(error),
                "phase": "reference",
            }
    after = _resource_snapshot()
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    status = result.status.value if result is not None else "INVALID/PENDING"
    unexpected_pruning = bool(
        diagnostics
        and any(
            diagnostics.get(name, 0)
            for name in (
                "dominance_checks",
                "dominance_pruned",
                "state_bound_checks",
                "state_bound_pruned",
            )
        )
    )
    if inputs.mode == "cancelled" and status != NonFifoSearchStatus.CANCELLED.value:
        checkpoint_meta["unexpected_cancel_status"] = True
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": inputs.mode,
        "adapter_mode": "non_fifo_zero_heuristic_v2",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "input": getattr(fixture, "input_name", None),
        "segment": inputs.segment,
        "objective": inputs.objective,
        "repetition": inputs.repetition,
        "status": status,
        "session_identity": identity.digest if identity is not None else None,
        "session_id": (
            result.session_id if result is not None else checkpoint_meta.get("session_id")
        ),
        "restored_session_id": checkpoint_meta.get("restored_session_id"),
        "semantic": semantic,
        "semantic_digest": result.semantic_digest if result is not None else None,
        "reference": reference,
        "reference_match": reference_match,
        "checkpoint": checkpoint_meta,
        "initial_diagnostics": initial_diagnostics,
        "unexpected_pruning": unexpected_pruning,
        "reason": result.reason if result is not None else None,
        "error_type": result.error_type if result is not None else None,
        "error_message": result.error_message if result is not None else None,
        "planner_error": planner_error,
        "diagnostics": diagnostics,
        "compute_ms": (
            result.planning_result.metrics.compute_ms
            if result is not None and result.planning_result is not None
            else None
        ),
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(before, after),
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {"files": files, "sha256": _digest(files)}


def _experiment_identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": _implementation_identity(root),
        "uv_lock": {"path": str(root / "uv.lock"), "sha256": _sha256(root / "uv.lock")},
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
            "frame_digests": [risk_frame_content_digest(frame) for frame in fixture.frames],
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
        "modes": list(args.modes),
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "slice_expansions": args.slice_expansions,
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
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    mode: str,
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
        "--mode",
        mode,
        "--slice-expansions",
        str(args.slice_expansions),
        "--cpu",
        str(args.cpu),
    ]


def _run_child(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    mode: str,
    heartbeat: Path,
) -> dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition, mode),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
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
                "mode": mode,
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
                "mode": mode,
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
            "mode": mode,
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
            "mode": mode,
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


def _record_key(record: dict[str, Any]) -> tuple[str, int, str] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    mode = record.get("mode")
    if not isinstance(objective, str) or not isinstance(repetition, int) or mode not in MODES:
        return None
    return objective, repetition, mode


def _load_resume_cases(output: Path, experiment_id: str) -> tuple[list[dict[str, Any]], int]:
    records, malformed = _read_jsonl(output / "cases.jsonl")
    by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if key is None or record.get("schema_version") != SCHEMA_VERSION:
            continue
        if record.get("experiment_id") not in (None, experiment_id):
            raise RuntimeError("resume evidence belongs to another experiment identity")
        if (
            record.get("dominance_policy") != "disabled"
            or record.get("state_bound_policy") != "absent"
        ):
            raise RuntimeError("resume evidence violates the adapter safety fence")
        if key in by_key:
            raise RuntimeError("resume evidence contains duplicate complete worker records")
        by_key[key] = record
    return list(by_key.values()), malformed


def _pair_ok(group: dict[str, dict[str, Any]]) -> bool:
    one = group.get("one_shot")
    restored = group.get("slice_restore")
    cancelled = group.get("cancelled")
    if one is None or restored is None or cancelled is None:
        return False
    if one.get("session_identity") != restored.get("session_identity"):
        return False
    if restored.get("checkpoint", {}).get("reached") is not True:
        return False
    if restored.get("restored_session_id") != restored.get("session_identity"):
        return False
    if one.get("status") != restored.get("status"):
        return False
    if one.get("status") not in {
        NonFifoSearchStatus.GOAL_FOUND.value,
        NonFifoSearchStatus.EXHAUSTED.value,
        NonFifoSearchStatus.RESOURCE_LIMIT.value,
    }:
        return False
    if one.get("status") == NonFifoSearchStatus.GOAL_FOUND.value:
        if one.get("semantic_digest") != restored.get("semantic_digest"):
            return False
        if one.get("reference_match") is not True or restored.get("reference_match") is not True:
            return False
    elif one.get("semantic") is not None or restored.get("semantic") is not None:
        return False
    if cancelled.get("status") != NonFifoSearchStatus.CANCELLED.value:
        return False
    if cancelled.get("semantic") is not None or cancelled.get("reference") is not None:
        return False
    return cancelled.get("checkpoint", {}).get("reached") is True


def _summary(cases: list[dict[str, Any]], args: argparse.Namespace, ignored: int) -> dict[str, Any]:
    expected = len(OBJECTIVES) * len(args.modes) * args.repetitions
    groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for case in cases:
        key = _record_key(case)
        if key is not None:
            groups[(key[0], key[1])][key[2]] = case
    matrix_complete = tuple(args.modes) == MODES
    complete = (
        matrix_complete
        and len(cases) == expected
        and len(groups) == len(OBJECTIVES) * args.repetitions
    )
    pair_results = [_pair_ok(group) for group in groups.values()]
    deterministic_by_cell: dict[str, bool] = {}
    for objective in OBJECTIVES:
        for mode in args.modes:
            selected = [
                case
                for case in cases
                if case.get("objective") == objective.value and case.get("mode") == mode
            ]
            signatures = [
                (
                    case.get("status"),
                    case.get("semantic_digest"),
                    case.get("reference_match"),
                )
                for case in selected
            ]
            deterministic_by_cell[f"{objective.value}:{mode}"] = (
                args.repetitions >= 2
                and len(signatures) == args.repetitions
                and len(set(map(repr, signatures))) == 1
            )
    deterministic = bool(deterministic_by_cell) and all(deterministic_by_cell.values())
    semantic_failure = any(
        case.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and case.get("reference_match") is not True
        for case in cases
    )
    unexpected_pruning = any(case.get("unexpected_pruning") for case in cases)
    resource_clean = all(case.get("resource_clean") is True for case in cases)
    resource_complete = all(_resource_evidence_complete(case, args.cpu) for case in cases)
    all_terminal = all(
        case.get("status") in TERMINAL_STATUSES | {"TIMEOUT", "INVALID/PENDING"} for case in cases
    )
    if semantic_failure or unexpected_pruning:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif not complete or not all_terminal:
        status = "INVALID/PENDING"
    elif not all(pair_results) or not deterministic:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif not resource_clean or not resource_complete:
        status = "REAL_INPUT_SESSION_RESOURCE_FAIL"
    else:
        status = "READY_FOR_P0.2-REAL-SESSION-RECOVERY-REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "expected_case_count": expected,
        "case_count": len(cases),
        "ignored_incomplete_records": ignored,
        "complete": complete,
        "matrix_complete": matrix_complete,
        "pair_count": len(pair_results),
        "all_pairs_equivalent": bool(pair_results) and all(pair_results),
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "all_resource_clean": resource_clean,
        "resource_evidence_complete": resource_complete,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "candidate_authorized": False,
        "winter_authorized": False,
        "cases": cases,
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
            raise RuntimeError("another temporal-session runner owns this output") from error
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
    identity = _experiment_identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("temporal-session evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
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
                "session-checkpoints.jsonl",
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
                objective_order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in objective_order:
                    for mode in args.modes:
                        key = (objective.value, repetition, mode)
                        if key in completed:
                            continue
                        record = _run_child(args, objective, repetition, mode, heartbeat)
                        record.setdefault("schema_version", SCHEMA_VERSION)
                        record.setdefault("mode", mode)
                        record["experiment_id"] = identity["experiment_id"]
                        if _record_key(record) != key:
                            raise RuntimeError("worker returned a mismatched case identity")
                        cases.append(record)
                        _append_jsonl(output / "cases.jsonl", record)
                        if record.get("checkpoint", {}).get("reached"):
                            _append_jsonl(
                                output / "session-checkpoints.jsonl",
                                {
                                    "experiment_id": identity["experiment_id"],
                                    "objective": objective.value,
                                    "repetition": repetition,
                                    "mode": mode,
                                    "checkpoint": record["checkpoint"],
                                },
                            )
                        completed.add(key)
                        _atomic_json(
                            heartbeat,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "RUNNING",
                                "updated_at": datetime.now(UTC),
                                "completed_cases": len(cases),
                                "expected_cases": (
                                    len(OBJECTIVES) * len(args.modes) * args.repetitions
                                ),
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
        summary = _summary(cases, args, ignored)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {
                "status": summary["status"],
                "summary": summary,
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
            },
        )
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if summary["status"]
        in {
            "READY_FOR_P0.2-REAL-SESSION-RECOVERY-REVIEW",
            "REAL_INPUT_SESSION_RESOURCE_FAIL",
        }
        else 2
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="executable_0_6h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--slice-expansions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.mode is None:
            raise SystemExit("worker requires --mode")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    if (
        args.repetitions < 1
        or args.slice_expansions < 1
        or args.worker_timeout_seconds <= 0
        or args.cpu < -1
    ):
        raise SystemExit(
            "repetitions/slice/timeout must be positive and cpu must be -1 or non-negative"
        )
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
