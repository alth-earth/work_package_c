#!/usr/bin/env python3
"""Research-only composition of audited state and terminal bounds.

The topological arrival envelope is the only certificate used to bound the
real 24-hour search space.  The selected-route terminal certificate is then
installed on the candidate side only, where it can reject a newly generated
label after an observed terminal label has a strictly better conservative
completion cost.  Both sides retain the existing Pareto pruning policy so the
comparison isolates the additional terminal rule without recreating the
known unbounded frontier.  This runner is deliberately a diagnostic: it
never claims a complete frontier for the candidate and cannot enable a
production planner, candidate, or Winter experiment.
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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    TEMPORAL_PARETO_COMPONENTS,
    run_non_fifo_temporal_pareto_search,
)

SCHEMA_VERSION = "c.p0.2-nonfifo-composed-terminal-bound-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
SEGMENTS = ("rolling_0_24h",)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_composed_terminal_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_selected_route_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto_state_bound_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "uv.lock",
)


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load audited runner {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _m25() -> Any:
    return _load_script(
        "benchmark_non_fifo_temporal_selected_route_bound_real.py",
        "c_m26_m25_selected_route",
    )


def _m18() -> Any:
    return _load_script(
        "benchmark_non_fifo_temporal_pareto_state_bound_real.py",
        "c_m26_m18_state_bound",
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _jsonable(getattr(value, name))
            for name in value.__dataclass_fields__
        }
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


def _context(
    args: argparse.Namespace, objective: ObjectiveMode
) -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Load one frozen fixture and the already-audited arrival certificate."""

    point = _m25()._load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    state_runner = _m18()
    planner, request, topology, corridor = state_runner._certificate(
        point, fixture, objective
    )
    scope = planner.temporal_scope(request)
    certificate = corridor.certificate
    if not certificate.usable or not certificate.arrival_bound_complete:
        raise RuntimeError("composed state-bound certificate is incomplete")
    if certificate.scope.digest != scope.digest:
        raise RuntimeError("composed state-bound scope identity diverged")
    return point, fixture, planner, request, scope, certificate, topology


def _diagnostic_int(diagnostics: Any, name: str) -> int:
    if diagnostics is None:
        return 0
    value = diagnostics.get(name, 0) if isinstance(diagnostics, Mapping) else getattr(diagnostics, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bound_record(certificate: Any, corridor: Any) -> dict[str, Any]:
    return {
        "digest": certificate.digest,
        "scope_digest": certificate.scope.digest,
        "proof_digest": corridor.proof_digest,
        "usable": certificate.usable,
        "arrival_bound_complete": certificate.arrival_bound_complete,
        "allowed_node_count": corridor.allowed_count,
        "excluded_node_count": corridor.excluded_count,
        "arrival_upper_bound_count": len(certificate.arrival_upper_hours),
        "projected_label_reduction": corridor.projected_label_reduction,
    }


def _route_record(result: Any, m25: Any) -> dict[str, Any] | None:
    if result is None or result.selected is None:
        return None
    return m25._route_record(result)


def _stats(result: Any, m25: Any) -> dict[str, Any] | None:
    return None if result is None else m25._stats(result)


def _worker_record(args: argparse.Namespace) -> dict[str, Any]:
    m25 = _m25()
    started = time.perf_counter()
    point = None
    fixture = None
    baseline = None
    selected = None
    state_certificate = None
    terminal_certificate = None
    corridor = None
    topology = None
    errors: dict[str, str] = {}
    before = m25._resource_snapshot()
    try:
        point, fixture, planner, request, scope, state_certificate, topology = _context(
            args, ObjectiveMode(args.objective)
        )
        # M25's geometric lower bound remains selection-only.  It is composed
        # with, never substituted for, the independently audited state bound.
        terminal_certificate, terminal_record = m25._terminal_certificate(
            planner, request, scope
        )
        baseline = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            state_bound_certificate=state_certificate,
        )
        selected = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            state_bound_certificate=state_certificate,
            incumbent_bound_certificate=terminal_certificate,
        )
    except Exception as error:  # pragma: no cover - child boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
        terminal_record = None
    after = m25._resource_snapshot()
    baseline_diagnostics = (
        m25._jsonable(baseline.diagnostics) if baseline is not None else None
    )
    selected_diagnostics = (
        m25._jsonable(selected.diagnostics) if selected is not None else None
    )
    baseline_state_pruned = _diagnostic_int(baseline_diagnostics, "state_bound_pruned")
    selected_state_pruned = _diagnostic_int(selected_diagnostics, "state_bound_pruned")
    baseline_state_rejected = _diagnostic_int(
        baseline_diagnostics, "state_bound_rejected"
    )
    selected_state_rejected = _diagnostic_int(
        selected_diagnostics, "state_bound_rejected"
    )
    terminal_pruned = selected.incumbent_bound_pruned if selected is not None else 0
    terminal_rejected = selected.incumbent_bound_rejected if selected is not None else 0
    semantic_match = bool(
        baseline is not None
        and selected is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and selected.status is NonFifoSearchStatus.GOAL_FOUND
        and baseline.semantic_digest == selected.semantic_digest
        and not selected.frontier_complete
        and selected.selection_only
    )
    state_bound_ok = bool(
        state_certificate is not None
        and state_certificate.usable
        and state_certificate.scope.digest == (scope.digest if scope is not None else None)
        and baseline_state_rejected == 0
        and selected_state_rejected == 0
        and selected_state_pruned > 0
    )
    terminal_bound_ok = bool(
        terminal_certificate is not None
        and terminal_certificate.usable
        and terminal_certificate.scope_digest == (scope.digest if scope is not None else None)
        and terminal_rejected == 0
        and selected is not None
        and selected.selection_only
    )
    resource_clean = m25._resource_clean(before, after)
    resource_evidence_complete = point is not None and point._resource_evidence_complete(
        {"resources_before": before, "resources_after": after}, cpu=args.cpu
    )
    resource_limited = bool(
        (baseline is not None and baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT)
        or (selected is not None and selected.status is NonFifoSearchStatus.RESOURCE_LIMIT)
    )
    if errors:
        status = "INVALID/FAIL"
    elif resource_limited:
        status = "RESOURCE_LIMIT"
    elif semantic_match and state_bound_ok and terminal_bound_ok and resource_clean:
        status = "READY_FOR_COMPOSED_BOUND_REVIEW"
    else:
        status = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "mode": args.mode,
        "status": status,
        "semantic_match": semantic_match,
        "state_bound_ok": state_bound_ok,
        "terminal_bound_ok": terminal_bound_ok,
        "state_bound_pruned": selected_state_pruned,
        "state_bound_pruned_baseline": baseline_state_pruned,
        "state_bound_rejected": selected_state_rejected,
        "terminal_bound_pruned": terminal_pruned,
        "terminal_bound_rejected": terminal_rejected,
        "terminal_pruning_observed": terminal_pruned > 0,
        "selection_only": selected.selection_only if selected is not None else False,
        "frontier_complete": selected.frontier_complete if selected is not None else False,
        "baseline_status": baseline.status.value if baseline is not None else None,
        "selected_status": selected.status.value if selected is not None else None,
        "baseline_semantic_digest": baseline.semantic_digest if baseline is not None else None,
        "selected_semantic_digest": selected.semantic_digest if selected is not None else None,
        "baseline": _route_record(baseline, m25),
        "selected": _route_record(selected, m25),
        "baseline_search_stats": _stats(baseline, m25),
        "selected_search_stats": _stats(selected, m25),
        "baseline_diagnostics": baseline_diagnostics,
        "selected_diagnostics": selected_diagnostics,
        "state_bound_certificate": (
            _bound_record(state_certificate, corridor)
            if state_certificate is not None and corridor is not None
            else None
        ),
        "terminal_bound_certificate": terminal_record,
        "topology_digest": topology.proof_digest if topology is not None else None,
        "errors": errors,
        "reason": (
            "frozen search limit reached"
            if resource_limited
            else None
            if status == "READY_FOR_COMPOSED_BOUND_REVIEW"
            else "composed bound semantic or certificate gate failed"
        ),
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "resource_classification": (
            "QUALIFIED"
            if resource_evidence_complete and resource_clean
            else "INCONCLUSIVE_CGROUP_BOUNDARY"
            if not resource_evidence_complete
            else "RESOURCE_FAIL"
        ),
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "terminal_bound_policy": "selected-route-terminal-lexicographic-v1",
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    point = _m25()._load_point_runner()
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    scopes: dict[str, str] = {}
    state_certificates: dict[str, str] = {}
    terminal_certificates: dict[str, str] = {}
    for objective in OBJECTIVES:
        local_args = _fixture_args(args)
        local_args.objective = objective.value
        _point, _fixture, planner, request, scope, state_certificate, _topology = _context(
            args, objective
        )
        scopes[objective.value] = scope.digest
        state_certificates[objective.value] = state_certificate.digest
        terminal_certificates[objective.value] = _m25()._terminal_certificate(
            planner, request, scope
        )[0].digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M26",
        "mode": args.mode,
        "git": _git_identity(root),
        "implementation": {"files": files, "sha256": _digest(files)},
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
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
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "scope_digests": scopes,
        "state_bound_certificate_digests": state_certificates,
        "terminal_bound_certificate_digests": terminal_certificates,
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "eta_method": "bounded",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "terminal_bound_policy": "selected-route-terminal-lexicographic-v1",
        "selection_only": True,
        "search_limits": LIMITS,
        "production_candidate_enabled": False,
        "winter_enabled": False,
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
            raise RuntimeError("another M26 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _case_key(record: Mapping[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or objective not in {item.value for item in OBJECTIVES}:
        return None
    if not isinstance(repetition, int) or repetition < 1:
        return None
    return objective, repetition


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
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    started = time.time()
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
            stdout, stderr = process.communicate()
            return {
                "schema_version": SCHEMA_VERSION,
                "objective": objective.value,
                "repetition": repetition,
                "mode": args.mode,
                "status": "RESOURCE_LIMIT",
                "reason": "worker_timeout",
                "semantic_match": False,
                "state_bound_pruned": 0,
                "terminal_bound_pruned": 0,
                "stdout": stdout[-2000:],
                "stderr": stderr[-2000:],
                "resource_evidence_complete": False,
            }
        time.sleep(0.2)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": args.mode,
            "status": "INVALID/FAIL",
            "reason": "worker_nonzero",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": args.mode,
            "status": "INVALID/FAIL",
            "reason": "worker_invalid_json",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    if not isinstance(value, dict):
        raise RuntimeError("M26 worker emitted a non-object JSON record")
    return value


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
    invalid = any(case.get("status") == "INVALID/FAIL" for case in cases)
    limited = any(case.get("status") == "RESOURCE_LIMIT" for case in cases)
    ready = bool(cases) and all(
        case.get("status") == "READY_FOR_COMPOSED_BOUND_REVIEW"
        and case.get("semantic_match") is True
        and case.get("state_bound_ok") is True
        and case.get("terminal_bound_ok") is True
        and case.get("selection_only") is True
        and case.get("frontier_complete") is False
        for case in cases
    )
    if not complete or not identity_clean:
        status = "INVALID/PENDING"
    elif invalid:
        status = "INVALID/FAIL"
    elif limited:
        status = "REAL_COMPOSED_BOUND_RESOURCE_FAIL"
    elif ready:
        status = (
            "NO_ADDITIONAL_TERMINAL_PRUNING"
            if sum(int(case.get("terminal_bound_pruned", 0) or 0) for case in cases) == 0
            else "READY_FOR_COMPOSED_BOUND_REVIEW"
        )
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
        "semantic_all_match": bool(cases) and all(case.get("semantic_match") is True for case in cases),
        "state_bound_pruned_total": sum(
            int(case.get("state_bound_pruned", 0) or 0) for case in cases
        ),
        "terminal_bound_pruned_total": sum(
            int(case.get("terminal_bound_pruned", 0) or 0) for case in cases
        ),
        "candidate_authorized": False,
        "winter_authorized": False,
        "cases": cases,
    }


def _run_parent(args: argparse.Namespace) -> int:
    if args.output_dir is None:
        raise SystemExit("--output-dir is required")
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    root = Path(__file__).resolve().parents[1]
    point = _m25()._load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M26 real evidence requires a clean implementation worktree")
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
                "updated_at": datetime.now(UTC),
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
        return 0 if summary["status"] in {
            "READY_FOR_COMPOSED_BOUND_REVIEW",
            "NO_ADDITIONAL_TERMINAL_PRUNING",
        } else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("composed-bound",), default="composed-bound")
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
