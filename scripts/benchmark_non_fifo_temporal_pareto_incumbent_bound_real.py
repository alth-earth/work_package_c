#!/usr/bin/env python3
"""Audit real-input incumbent-bound eligibility without fail-open pruning.

This is a C-internal research sidecar.  The default ``qualification`` mode
loads the frozen real RiskFrame/route-plan-set identity and exercises a
rejected certificate through the actual Pareto bridge.  It deliberately does
not start a long search when no independently produced proof-carrying bound
exists.  ``resource-frontier`` is reserved for an explicit per-objective
certificate directory; a certificate is never derived from a baseline or
reference result by this runner.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoParetoIncumbentBoundCertificate,
    NonFifoParetoIncumbentBoundStatus,
    NonFifoSearchStatus,
)
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    create_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)

SCHEMA_VERSION = "c.p0.2-nonfifo-pareto-incumbent-bound-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
MODES = ("qualification", "resource-frontier")
SEGMENTS = ("executable_0_6h", "rolling_0_24h")
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
PROOF_REQUIRED_REASON = "real_exact_goal_arrival_unproven"
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_incumbent_bound_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "uv.lock",
)


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_m24_real_point_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load frozen real-input point runner")
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
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("real incumbent-bound evidence contains a non-finite float")
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
        "dirty": bool(run("status", "--porcelain")),
    }


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
) -> tuple[Any, Any, Any, Any, Any]:
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    planner = point._build_planner(fixture, objective)
    planner.eta_policy = EtaRefinementPolicy(method="bounded")
    request = replace(
        point._request(fixture, objective),
        use_heuristic=False,
        cancel_check=None,
    )
    scope = planner.temporal_scope(request)
    return point, fixture, planner, request, scope


def _rejected_certificate(scope: Any, request: Any) -> NonFifoParetoIncumbentBoundCertificate:
    return NonFifoParetoIncumbentBoundCertificate.rejected(
        scope_digest=scope.digest,
        goal=(request.goal, None),
        objective_count=7,
        reason=PROOF_REQUIRED_REASON,
        proof_digest="real-input-incumbent-bound-proof-required-v1",
    )


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot(point: Any) -> dict[str, Any]:
    return point._resource_snapshot()


def _resource_clean(point: Any, before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(point._resource_clean(before, after))


def _resource_complete(before: dict[str, Any], after: dict[str, Any], cpu: int) -> bool:
    for snapshot in (before, after):
        if cpu >= 0 and snapshot.get("cpu_affinity") != [cpu]:
            return False
        cgroup = snapshot.get("cgroup") or {}
        if cgroup.get("memory_max") != 4 * 1024**3:
            return False
        if cgroup.get("memory_swap_max") != 0:
            return False
        if cgroup.get("memory_events") is None:
            return False
    return True


def _search_stats(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    raw = result.raw_result
    return {
        "expanded": raw.expanded,
        "generated": raw.generated,
        "queue_peak": raw.queue_peak,
        "edge_evaluations": raw.edge_evaluations,
        "pareto_pruned": raw.pareto_pruned,
        "search_limits": raw.search_limits,
        "pareto_pruning": raw.pareto_pruning,
        "incumbent_bound_digest": raw.incumbent_bound_digest,
        "incumbent_bound_pruned": raw.incumbent_bound_pruned,
        "incumbent_bound_rejected": raw.incumbent_bound_rejected,
    }


def _qualification_worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    _set_cpu(args.cpu)
    point = _load_point_runner()
    before = _resource_snapshot(point)
    fixture = None
    planner = None
    request = None
    scope = None
    certificate = None
    session = None
    checkpoint = None
    errors: dict[str, str] = {}
    try:
        _point, fixture, planner, request, scope = _context(args, ObjectiveMode(args.objective))
        certificate = _rejected_certificate(scope, request)
        session = create_non_fifo_temporal_pareto_session(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            incumbent_bound_certificate=certificate,
        )
        advanced = session.advance(expansion_slice=1)
        if advanced is None:
            checkpoint = session.checkpoint()
        else:
            checkpoint = {"terminal_before_checkpoint": True, "state": session.state}
    except Exception as error:  # pragma: no cover - worker boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot(point)
    rejection_reasons = (
        tuple(sorted(session.incumbent_bound_rejection_reasons.items()))
        if session is not None
        else ()
    )
    fail_closed = bool(
        not errors
        and certificate is not None
        and not certificate.usable
        and session is not None
        and not session.incumbent_bound_authorized
        and session.incumbent_bound_pruned == 0
        and session.identity.scope_digest == scope.digest
        and session.identity.incumbent_bound_digest == certificate.digest
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "qualification",
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "status": "REAL_INPUT_INCUMBENT_BOUND_UNCERTAIN" if fail_closed else "INVALID/FAIL",
        "reason": (
            PROOF_REQUIRED_REASON
            if fail_closed
            else "real incumbent-bound qualification failed"
        ),
        "proof_required": True,
        "candidate_started": False,
        "dominance_policy": "disabled",
        "eta_method": "bounded",
        "scope_digest": scope.digest if scope is not None else None,
        "goal": list(request.goal) if request is not None else None,
        "certificate": {
            "status": certificate.status.value if certificate is not None else None,
            "usable": certificate.usable if certificate is not None else False,
            "digest": certificate.digest if certificate is not None else None,
            "reason": certificate.reason if certificate is not None else None,
        },
        "incumbent_bound_authorized": (
            session.incumbent_bound_authorized if session is not None else False
        ),
        "incumbent_bound_pruned": (
            session.incumbent_bound_pruned if session is not None else 0
        ),
        "incumbent_bound_rejected": (
            session.incumbent_bound_rejected if session is not None else 0
        ),
        "incumbent_bound_rejection_reasons": rejection_reasons,
        "checkpoint": (
            {"digest": checkpoint.digest, "identity": checkpoint.pareto_checkpoint.identity.digest}
            if hasattr(checkpoint, "digest")
            else checkpoint
        ),
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(point, before, after),
        "resource_evidence_complete": _resource_complete(before, after, args.cpu),
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "errors": errors,
    }


def _tupleize(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_tupleize(item) for item in value)
    if isinstance(value, dict):
        return {key: _tupleize(item) for key, item in value.items()}
    return value


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("certificate timestamp must be an ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("certificate timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _load_certificate(path: Path) -> NonFifoParetoIncumbentBoundCertificate:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("incumbent-bound certificate must be a JSON object")
    raw_bounds = raw.get("state_lower_bounds", ())
    bounds: list[tuple[tuple[Any, datetime], tuple[datetime, tuple[float, ...]]]] = []
    for item in raw_bounds:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("certificate state_lower_bounds entry is invalid")
        state_raw, evidence_raw = item
        if not isinstance(state_raw, list) or len(state_raw) != 2:
            raise ValueError("certificate state key is invalid")
        if not isinstance(evidence_raw, list) or len(evidence_raw) != 2:
            raise ValueError("certificate evidence is invalid")
        node, arrival = _tupleize(state_raw[0]), _parse_time(state_raw[1])
        goal_arrival = _parse_time(evidence_raw[0])
        values = tuple(float(value) for value in evidence_raw[1])
        bounds.append(((node, arrival), (goal_arrival, values)))
    return NonFifoParetoIncumbentBoundCertificate(
        status=NonFifoParetoIncumbentBoundStatus(raw["status"]),
        scope_digest=str(raw["scope_digest"]),
        goal=_tupleize(raw["goal"]),
        objective_count=int(raw["objective_count"]),
        state_lower_bounds=tuple(bounds),
        coverage_complete=bool(raw["coverage_complete"]),
        evaluator_certified=bool(raw["evaluator_certified"]),
        proof_digest=str(raw["proof_digest"]),
        reason=raw.get("reason"),
        schema_version=str(raw.get("schema_version", "")),
        certificate_digest=str(raw.get("certificate_digest", "")),
    )


def _resource_worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    _set_cpu(args.cpu)
    point = _load_point_runner()
    before = _resource_snapshot(point)
    fixture = None
    baseline = None
    candidate = None
    certificate = None
    errors: dict[str, str] = {}
    try:
        _point, fixture, planner, request, scope = _context(args, ObjectiveMode(args.objective))
        certificate = _load_certificate(Path(args.certificate_dir) / f"{args.objective}.json")
        if not certificate.permits(
            scope_digest=scope.digest,
            goal=(request.goal, None),
            objective_count=7,
        ):
            raise ValueError("incumbent-bound certificate does not match real scope")
        baseline = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=False,
            skip_expected_rejections=True,
        )
        candidate = run_non_fifo_temporal_pareto_search(
            planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            incumbent_bound_certificate=certificate,
        )
    except Exception as error:  # pragma: no cover - long worker boundary
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot(point)
    semantic_match = bool(
        baseline is not None
        and candidate is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and candidate.status is NonFifoSearchStatus.GOAL_FOUND
        and baseline.frontier_digest == candidate.frontier_digest
        and baseline.semantic_digest == candidate.semantic_digest
    )
    fail_closed = bool(
        not errors
        and certificate is not None
        and candidate is not None
        and candidate.incumbent_bound_rejected == 0
        and candidate.incumbent_bound_pruned >= 0
    )
    status = "PASS" if semantic_match and fail_closed else "FAIL"
    if baseline is not None and candidate is not None and (
        baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT
        or candidate.status is NonFifoSearchStatus.RESOURCE_LIMIT
    ):
        status = "RESOURCE_LIMIT"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "resource-frontier",
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "status": status if not errors else "INVALID/FAIL",
        "candidate_started": candidate is not None,
        "certificate_digest": certificate.digest if certificate is not None else None,
        "semantic_match": semantic_match,
        "baseline_status": baseline.status.value if baseline is not None else None,
        "candidate_status": candidate.status.value if candidate is not None else None,
        "baseline_frontier_digest": baseline.frontier_digest if baseline is not None else None,
        "candidate_frontier_digest": candidate.frontier_digest if candidate is not None else None,
        "baseline_semantic_digest": baseline.semantic_digest if baseline is not None else None,
        "candidate_semantic_digest": candidate.semantic_digest if candidate is not None else None,
        "incumbent_bound_pruned": candidate.incumbent_bound_pruned if candidate is not None else 0,
        "incumbent_bound_rejected": (
            candidate.incumbent_bound_rejected if candidate is not None else 0
        ),
        "incumbent_bound_rejection_reasons": (
            candidate.incumbent_bound_rejection_reasons if candidate is not None else ()
        ),
        "search_stats": {
            "baseline": _search_stats(baseline),
            "candidate": _search_stats(candidate),
        },
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(point, before, after),
        "resource_evidence_complete": _resource_complete(before, after, args.cpu),
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "errors": errors,
    }


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "qualification":
        return _qualification_worker(args)
    return _resource_worker(args)


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
    repetitions = int(identity["repetitions"])
    expected = len(OBJECTIVES) * repetitions
    keys = [_case_key(case) for case in cases]
    complete = (
        len(cases) == expected
        and malformed == 0
        and None not in keys
        and len(set(keys)) == len(keys)
    )
    fail_closed = bool(cases) and all(
        case.get("incumbent_bound_pruned", 0) == 0
        and case.get("candidate_started") is False
        for case in cases
        if case.get("mode") == "qualification"
    )
    identity_clean = bool((identity.get("git") or {}).get("dirty") is False)
    if not complete or not identity_clean:
        status = "INVALID/PENDING"
    elif any(case.get("status") == "INVALID/FAIL" for case in cases):
        status = "INVALID/FAIL"
    elif identity.get("mode") == "qualification" and fail_closed:
        status = "REAL_INPUT_INCUMBENT_BOUND_UNCERTAIN"
    elif any(case.get("status") == "RESOURCE_LIMIT" for case in cases):
        status = "REAL_INPUT_INCUMBENT_BOUND_RESOURCE_FAIL"
    elif all(case.get("status") == "PASS" for case in cases):
        status = "READY_FOR_SEPARATE_REAL_INCUMBENT_BOUND_PLAN"
    else:
        status = "INVALID/FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "identity_clean": identity_clean,
        "fail_closed": fail_closed,
        "candidate_started": any(case.get("candidate_started") is True for case in cases),
        "incumbent_bound_pruned_total": sum(
            int(case.get("incumbent_bound_pruned", 0) or 0) for case in cases
        ),
        "candidate_authorized": False,
        "winter_authorized": False,
        "cases": cases,
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    point = _load_point_runner()
    scopes = {}
    for objective in OBJECTIVES:
        planner = point._build_planner(fixture, objective)
        planner.eta_policy = EtaRefinementPolicy(method="bounded")
        request = replace(
            point._request(fixture, objective),
            use_heuristic=False,
            cancel_check=None,
        )
        scopes[objective.value] = planner.temporal_scope(request).digest
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M24",
        "mode": args.mode,
        "purpose": "real_incumbent_bound_qualification_audit",
        "git": _git_identity(root),
        "implementation": {"files": files, "sha256": _digest(files)},
        "uv_lock": {"path": str(root / "uv.lock"), "sha256": _sha256(root / "uv.lock")},
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
        "objectives": [objective.value for objective in OBJECTIVES],
        "segment": args.segment,
        "scope_digests": scopes,
        "eta_method": "bounded",
        "dominance_policy": "disabled",
        "bound_policy": "exact_state_exact_goal_arrival_only",
        "proof_required_reason": PROOF_REQUIRED_REASON,
        "search_limits": LIMITS,
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "certificate_dir": str(args.certificate_dir) if args.certificate_dir else None,
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
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
            raise RuntimeError("another M24 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
) -> list[str]:
    command = [
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
    if args.certificate_dir is not None:
        command.extend(("--certificate-dir", str(Path(args.certificate_dir).resolve())))
    return command


def _run_child(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, heartbeat: Path
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    started = time.time()
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(
        mode="w+", encoding="utf-8"
    ) as stderr_file:
        process = subprocess.Popen(
            _child_command(args, objective, repetition),
            stdout=stdout_file,
            stderr=stderr_file,
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
                stdout_file.seek(0)
                stderr_file.seek(0)
                return {
                    "schema_version": SCHEMA_VERSION,
                    "mode": args.mode,
                    "objective": objective.value,
                    "repetition": repetition,
                    "status": "INVALID/FAIL",
                    "reason": "worker_timeout",
                    "candidate_started": False,
                    "incumbent_bound_pruned": 0,
                    "stdout": stdout_file.read()[-4000:],
                    "stderr": stderr_file.read()[-4000:],
                }
            time.sleep(0.2)
        process.wait()
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": args.mode,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/FAIL",
            "reason": "worker_nonzero",
            "candidate_started": False,
            "incumbent_bound_pruned": 0,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": args.mode,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/FAIL",
            "reason": "worker_invalid_json",
            "candidate_started": False,
            "incumbent_bound_pruned": 0,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
    if not isinstance(value, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return value


def _run_parent(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    if args.mode == "resource-frontier" and args.certificate_dir is None:
        raise SystemExit("resource-frontier requires --certificate-dir")
    root = Path(__file__).resolve().parents[1]
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M24 real evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.resume:
                raise RuntimeError("experiment already exists; use --resume")
            if previous.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity does not match")
        evidence_files = (
            "manifest.json",
            "cases.jsonl",
            "comparison-summary.json",
            "heartbeat.json",
            "ALL_DONE/STOPPED_HARD",
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": evidence_files,
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        cases, malformed = _read_jsonl(cases_path) if args.resume else ([], 0)
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
                    completed.add(key)
        except (KeyboardInterrupt, SystemExit) as error:
            stopped = {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "reason": f"runner interrupted: {type(error).__name__}",
                "cases": cases,
            }
            _atomic_json(output / "comparison-summary.json", stopped)
            manifest.update({"status": "STOPPED_HARD", "summary": stopped})
            _atomic_json(manifest_path, manifest)
            (output / "STOPPED_HARD").write_text(stopped["reason"] + "\n", encoding="utf-8")
            return 2
        summary = _summary(cases, identity, malformed)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {
                "status": summary["status"],
                "summary": {
                    key: value for key, value in summary.items() if key != "cases"
                },
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
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["status"] == "REAL_INPUT_INCUMBENT_BOUND_UNCERTAIN" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=MODES, default="qualification")
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--certificate-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--objective",
        choices=tuple(item.value for item in OBJECTIVES),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--repetition", type=int, default=1, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
