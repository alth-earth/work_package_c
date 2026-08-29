#!/usr/bin/env python3
"""Research-only Pareto ordering diagnostic with a certified heuristic.

The runner compares the existing selected-route terminal-bound session with a
second session that uses the same state/terminal certificates and an explicit
``TemporalHeuristicCertificate`` only to order labels.  No label is removed by
the heuristic, temporal dominance remains disabled, and the result is never a
production planner or a complete-frontier performance claim.
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
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    _heuristic_policy_digest,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_heuristic_bounds import (
    qualify_temporal_heuristic,
)

SCHEMA_VERSION = os.environ.get(
    "C_PARETO_SCHEMA_VERSION", "c.p0.2-nonfifo-pareto-heuristic-real.v1"
)
MILESTONE = os.environ.get("C_PARETO_MILESTONE", "P0.2-M27")
OBJECTIVES = tuple(ObjectiveMode)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_heuristic_real.py",
    "scripts/benchmark_non_fifo_temporal_composed_terminal_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_selected_route_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto_state_bound_real.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_heuristic_bounds.py",
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


def _m26() -> Any:
    return _load_script(
        "benchmark_non_fifo_temporal_composed_terminal_bound_real.py",
        "c_m27_m26_composed_terminal",
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
) -> tuple[Any, Any, Any, Any, Any, Any, Any, Any]:
    point, fixture, planner, request, scope, state_certificate, topology = _m26()._context(
        args, objective
    )
    heuristic = qualify_temporal_heuristic(
        scope=scope,
        topology=topology,
        cost_model=planner._cost_model(objective),
        objective=objective.value,
        expected_scope=scope,
    )
    if not heuristic.usable:
        raise RuntimeError(f"heuristic certificate is unusable: {heuristic.reason}")
    return point, fixture, planner, request, scope, state_certificate, topology, heuristic


def _diagnostic_int(diagnostics: Any, name: str) -> int:
    if diagnostics is None:
        return 0
    value = (
        diagnostics.get(name, 0)
        if isinstance(diagnostics, Mapping)
        else getattr(diagnostics, name, 0)
    )
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _route_record(result: Any, m26: Any) -> dict[str, Any] | None:
    if result is None or result.selected is None:
        return None
    return m26._m25()._route_record(result)


def _stats(result: Any, m26: Any) -> dict[str, Any] | None:
    return None if result is None else m26._m25()._stats(result)


def _set_cpu(cpu: int) -> dict[str, Any]:
    try:
        os.sched_setaffinity(0, {cpu})
        return {"requested": cpu, "applied": sorted(os.sched_getaffinity(0))}
    except (AttributeError, OSError) as error:
        return {"requested": cpu, "applied": None, "error": f"{type(error).__name__}: {error}"}


def _worker_record(args: argparse.Namespace) -> dict[str, Any]:
    m26 = _m26()
    m25 = m26._m25()
    started = time.perf_counter()
    before = m25._resource_snapshot()
    point = fixture = None
    baseline = ordered = None
    scope = state_certificate = topology = heuristic = None
    terminal_certificate = None
    terminal_record = None
    errors: dict[str, str] = {}
    cpu_evidence = _set_cpu(args.cpu)
    try:
        (
            point,
            fixture,
            planner,
            request,
            scope,
            state_certificate,
            topology,
            heuristic,
        ) = _context(args, ObjectiveMode(args.objective))
        terminal_certificate, terminal_record = m25._terminal_certificate(
            planner, request, scope
        )
        # Both runs use the same selected-route terminal certificate.  The
        # only candidate difference is certified queue ordering.
        baseline = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            state_bound_certificate=state_certificate,
            incumbent_bound_certificate=terminal_certificate,
        )
        heuristic_ordering = getattr(args, "heuristic_ordering", "always")
        ordered = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            state_bound_certificate=state_certificate,
            incumbent_bound_certificate=terminal_certificate,
            heuristic_certificate=heuristic,
            heuristic_ordering=heuristic_ordering,
        )
    except Exception as error:  # pragma: no cover - child boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = m25._resource_snapshot()
    baseline_diagnostics = m25._jsonable(baseline.diagnostics) if baseline is not None else None
    ordered_diagnostics = m25._jsonable(ordered.diagnostics) if ordered is not None else None
    semantic_match = bool(
        baseline is not None
        and ordered is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and ordered.status is NonFifoSearchStatus.GOAL_FOUND
        and baseline.semantic_digest == ordered.semantic_digest
        and baseline.selection_only
        and ordered.selection_only
        and not baseline.frontier_complete
        and not ordered.frontier_complete
    )
    heuristic_ok = bool(
        heuristic is not None
        and heuristic.usable
        and scope is not None
        and heuristic.scope.digest == scope.digest
        and _diagnostic_int(ordered_diagnostics, "heuristic_rejected") == 0
        and bool(ordered_diagnostics)
        and ordered_diagnostics.get("heuristic_policy")
        == ("certified" if heuristic_ordering == "always" else "certified-after-goal")
        and ordered_diagnostics.get("heuristic_scope_match") is True
        and ordered is not None
        and ordered.raw_result.priority_policy_digest
        == _heuristic_policy_digest(heuristic, heuristic_ordering)
    )
    terminal_pruned = ordered.incumbent_bound_pruned if ordered is not None else 0
    terminal_rejected = ordered.incumbent_bound_rejected if ordered is not None else 0
    state_pruned = _diagnostic_int(ordered_diagnostics, "state_bound_pruned")
    state_rejected = _diagnostic_int(ordered_diagnostics, "state_bound_rejected")
    resource_clean = m25._resource_clean(before, after)
    resource_evidence_complete = point is not None and point._resource_evidence_complete(
        {"resources_before": before, "resources_after": after}, cpu=args.cpu
    )
    resource_limited = bool(
        (baseline is not None and baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT)
        or (ordered is not None and ordered.status is NonFifoSearchStatus.RESOURCE_LIMIT)
    )
    if errors:
        status = "INVALID/FAIL"
    elif resource_limited:
        status = "RESOURCE_LIMIT"
    elif semantic_match and heuristic_ok and terminal_rejected == 0 and state_rejected == 0:
        status = "READY_FOR_HEURISTIC_REVIEW"
    else:
        status = "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "status": status,
        "semantic_match": semantic_match,
        "heuristic_ok": heuristic_ok,
        "state_bound_pruned": state_pruned,
        "state_bound_rejected": state_rejected,
        "terminal_bound_pruned": terminal_pruned,
        "terminal_bound_rejected": terminal_rejected,
        "terminal_pruning_observed": terminal_pruned > 0,
        "baseline_status": baseline.status.value if baseline is not None else None,
        "ordered_status": ordered.status.value if ordered is not None else None,
        "baseline_semantic_digest": baseline.semantic_digest if baseline is not None else None,
        "ordered_semantic_digest": ordered.semantic_digest if ordered is not None else None,
        "baseline": _route_record(baseline, m26),
        "ordered": _route_record(ordered, m26),
        "baseline_search_stats": _stats(baseline, m26),
        "ordered_search_stats": _stats(ordered, m26),
        "baseline_diagnostics": baseline_diagnostics,
        "ordered_diagnostics": ordered_diagnostics,
        "state_bound_certificate_digest": (
            state_certificate.digest if state_certificate is not None else None
        ),
        "heuristic_certificate_digest": heuristic.digest if heuristic is not None else None,
        "heuristic_ordering": getattr(args, "heuristic_ordering", "always"),
        "terminal_bound_certificate": terminal_record,
        "topology_digest": topology.proof_digest if topology is not None else None,
        "errors": errors,
        "reason": (
            "frozen search limit reached"
            if resource_limited
            else None
            if status == "READY_FOR_HEURISTIC_REVIEW"
            else "heuristic ordering or semantic gate failed"
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
        "cpu_affinity": cpu_evidence,
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "terminal_bound_policy": "selected-route-terminal-lexicographic-v1",
        "priority_policy": (
            "certified-total-equivalent-hours-lower-bound-v1"
            if getattr(args, "heuristic_ordering", "always") == "always"
            else "certified-goal-gated-total-equivalent-hours-lower-bound-v1"
        ),
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


def _identity(
    args: argparse.Namespace,
    fixture: Any,
    root: Path,
    objectives: tuple[ObjectiveMode, ...],
) -> dict[str, Any]:
    point = _m26()._m25()._load_point_runner()
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    scopes: dict[str, str] = {}
    state_certificates: dict[str, str] = {}
    heuristic_certificates: dict[str, str] = {}
    terminal_certificates: dict[str, str] = {}
    for objective in objectives:
        (
            _point,
            _fixture,
            planner,
            request,
            scope,
            state_certificate,
            _topology,
            heuristic,
        ) = _context(args, objective)
        scopes[objective.value] = scope.digest
        state_certificates[objective.value] = state_certificate.digest
        heuristic_certificates[objective.value] = heuristic.digest
        terminal_certificates[objective.value] = _m26()._m25()._terminal_certificate(
            planner, request, scope
        )[0].digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": MILESTONE,
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
            "frame_digests": [
                point.risk_frame_content_digest(frame) for frame in fixture.frames
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
        "scope_digests": scopes,
        "state_bound_certificate_digests": state_certificates,
        "heuristic_certificate_digests": heuristic_certificates,
        "terminal_bound_certificate_digests": terminal_certificates,
        "objectives": [objective.value for objective in objectives],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "eta_method": "bounded",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "terminal_bound_policy": "selected-route-terminal-lexicographic-v1",
        "priority_policy": (
            "certified-total-equivalent-hours-lower-bound-v1"
            if getattr(args, "heuristic_ordering", "always") == "always"
            else "certified-goal-gated-total-equivalent-hours-lower-bound-v1"
        ),
        "heuristic_ordering": getattr(args, "heuristic_ordering", "always"),
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
            raise RuntimeError("another M27 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _case_key(record: Mapping[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or objective not in {
        item.value for item in OBJECTIVES
    }:
        return None
    if not isinstance(repetition, int) or repetition < 1:
        return None
    return objective, repetition


def _selected_objectives(raw: str) -> tuple[ObjectiveMode, ...]:
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not values:
        raise SystemExit("--objectives must name at least one objective")
    try:
        selected = tuple(ObjectiveMode(value) for value in values)
    except ValueError as error:
        raise SystemExit(f"invalid --objectives value: {raw}") from error
    if len(set(selected)) != len(selected):
        raise SystemExit("--objectives must not contain duplicates")
    return selected


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
        "--heuristic-ordering",
        getattr(args, "heuristic_ordering", "always"),
    ]


def _run_child(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, heartbeat: Path
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "src") + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    started = time.time()
    process = subprocess.Popen(
        _child_command(args, objective, repetition),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
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
                "heuristic_ok": False,
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
        raise RuntimeError("M27 worker emitted a non-object JSON record")
    return value


def _summary(
    cases: list[dict[str, Any]], identity: Mapping[str, Any], malformed: int
) -> dict[str, Any]:
    expected = len(identity["objectives"]) * int(identity["repetitions"])
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
        case.get("status") == "READY_FOR_HEURISTIC_REVIEW"
        and case.get("semantic_match") is True
        and case.get("heuristic_ok") is True
        for case in cases
    )
    if not complete or not identity_clean:
        status = "INVALID/PENDING"
    elif invalid:
        status = "INVALID/FAIL"
    elif limited:
        status = "REAL_HEURISTIC_RESOURCE_FAIL"
    elif ready:
        status = (
            "NO_HEURISTIC_TERMINAL_GAIN"
            if sum(int(case.get("terminal_bound_pruned", 0) or 0) for case in cases) == 0
            else "READY_FOR_SEPARATE_HEURISTIC_REVIEW"
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
        "semantic_all_match": bool(cases)
        and all(case.get("semantic_match") is True for case in cases),
        "heuristic_all_authorized": bool(cases)
        and all(case.get("heuristic_ok") is True for case in cases),
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
    objectives = _selected_objectives(args.objectives)
    root = Path(__file__).resolve().parents[1]
    point = _m26()._m25()._load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root, objectives)
    if identity["git"]["dirty"]:
        raise RuntimeError("M27 real evidence requires a clean implementation worktree")
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
        expected = len(objectives) * args.repetitions
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
                order = objectives if repetition % 2 else tuple(reversed(objectives))
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
        manifest.update({"status": summary["status"], "summary": summary})
        _atomic_json(manifest_path, manifest)
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    return 0 if summary["status"] not in {"INVALID/PENDING", "INVALID/FAIL", "FAIL"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--mode", default="resource-frontier")
    parser.add_argument("--risk-window-commit", required=True)
    parser.add_argument("--route-plan-set", required=True)
    parser.add_argument("--config-root", required=True)
    parser.add_argument("--segment", default="rolling_0_24h")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=[item.value for item in OBJECTIVES])
    parser.add_argument("--objectives", default=",".join(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument(
        "--heuristic-ordering",
        choices=("always", "after_goal"),
        default="always",
        help="apply the certified queue priority always or only after the first goal",
    )
    args = parser.parse_args()
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_worker_record(args), ensure_ascii=False, sort_keys=True))
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
