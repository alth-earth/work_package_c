#!/usr/bin/env python3
"""Research-only real 24h frontier equivalence under a state-bound certificate.

This runner closes the evidence gap between the M18 resource observation and
the M20/M21 complete-frontier checks.  For one frozen real input it executes a
certified, no-Pareto-pruning reference sidecar and a certified Pareto sidecar,
then compares their complete goal frontiers.  An independent scalar
zero-heuristic Dijkstra is used only for selected-route business evidence.

The state bound, Pareto policy, and all resource limits are explicit research
inputs.  No production planner, public contract, candidate, or Winter path is
modified or authorized by this script.
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
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-state-bound-frontier-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
MODES = ("one_shot", "slice_restore")
SEGMENTS = ("rolling_0_24h",)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
TERMINAL_STATUSES = {status.value for status in NonFifoSearchStatus}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_frontier_state_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto_state_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto_reference_24h.py",
    "scripts/benchmark_non_fifo_temporal_pareto_frontier_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
)


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load audited runner {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _m18() -> Any:
    return _load_script("benchmark_non_fifo_temporal_pareto_state_bound_real.py", "c_m22_m18")


def _m19() -> Any:
    return _load_script("benchmark_non_fifo_temporal_pareto_reference_24h.py", "c_m22_m19")


def _m21() -> Any:
    return _load_script("benchmark_non_fifo_temporal_pareto_frontier_real.py", "c_m22_m21")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "total_seconds") and callable(value.total_seconds):
        return value.total_seconds()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        raise ValueError("non-finite real frontier evidence")
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        raise RuntimeError("a fixed CPU is required for real evidence")
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _resource_snapshot(m18: Any) -> dict[str, Any]:
    return m18._resource_snapshot()


def _resource_clean(m18: Any, before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(m18._resource_clean(before, after))


def _resource_evidence_complete(before: dict[str, Any], after: dict[str, Any], cpu: int) -> bool:
    for snapshot in (before, after):
        if snapshot.get("cpu_affinity") != [cpu]:
            return False
        cgroup = snapshot.get("cgroup") or {}
        if cgroup.get("memory_max") != 4 * 1024**3:
            return False
        if cgroup.get("memory_swap_max") != 0:
            return False
        if cgroup.get("memory_swap_current") not in (0, None):
            return False
        if cgroup.get("memory_events") is None:
            return False
    return True


def _route_payload(route: Any, m18: Any) -> dict[str, Any]:
    return m18._route_payload(route)


def _frontier_digest(frontier: list[dict[str, Any]]) -> str:
    tokens = tuple(
        sorted(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in frontier
        )
    )
    return _digest({"schema_version": "c.p0.2-m22-frontier-labels.v1", "labels": tokens})


def _certificate_payload(certificate: Any) -> dict[str, Any]:
    return {
        "digest": certificate.digest,
        "usable": certificate.usable,
        "complete": certificate.complete,
        "status": certificate.status.value,
        "scope_digest": certificate.scope_digest,
        "session_identity_digest": certificate.session_identity_digest,
        "comparison_identity_digest": certificate.comparison_identity_digest,
        "policy_digest": certificate.policy_digest,
        "frontier_digest": certificate.frontier_digest,
        "frontier_count": certificate.frontier_count,
        "goal_label_count": certificate.goal_label_count,
        "rejection_reason": certificate.rejection_reason,
    }


def _run_policy(
    planner: Any,
    request: Any,
    certificate: Any,
    *,
    pareto_pruning: bool,
    mode: str,
    slice_expansions: int,
) -> tuple[Any, Any, dict[str, Any]]:
    session = create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=pareto_pruning,
        skip_expected_rejections=True,
        state_bound_certificate=certificate,
    )
    checkpoint: dict[str, Any] = {"reached": False, "mode": mode}
    if mode == "one_shot":
        result = session.run()
    elif mode == "slice_restore":
        initial = session.advance(expansion_slice=slice_expansions)
        if initial is not None:
            result = initial
            checkpoint.update({"terminal_before_checkpoint": True, "state": session.state})
        else:
            saved = session.checkpoint()
            checkpoint.update(
                {
                    "reached": True,
                    "digest": saved.digest,
                    "session_id": session.session_id,
                    "state": session.state,
                }
            )
            session = restore_non_fifo_temporal_pareto_session(
                planner,
                request,
                saved,
                skip_expected_rejections=True,
                state_bound_certificate=certificate,
            )
            checkpoint["restored_session_id"] = session.session_id
            result = session.run()
    else:  # pragma: no cover - parser guards this
        raise ValueError(f"unsupported mode: {mode}")
    certificate_result = session.frontier_certificate
    return result, certificate_result, checkpoint


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
    }


def _diagnostic_value(diagnostics: Any, name: str, m18: Any) -> int:
    return m18._diagnostic_value(diagnostics, name)


def _policy_record(
    result: Any,
    certificate: Any,
    m18: Any,
) -> dict[str, Any]:
    selected = _route_payload(result.selected, m18) if result.selected is not None else None
    frontier = [_route_payload(route, m18) for route in result.frontier]
    diagnostics = _jsonable(result.diagnostics)
    return {
        "status": result.status.value,
        "semantic": selected,
        "semantic_digest": result.semantic_digest,
        "frontier": frontier,
        "frontier_digest": _frontier_digest(frontier),
        "certificate": _certificate_payload(certificate),
        "search_stats": _search_stats(result),
        "diagnostics": diagnostics,
        "state_bound_checks": _diagnostic_value(diagnostics, "state_bound_checks", m18),
        "state_bound_pruned": _diagnostic_value(diagnostics, "state_bound_pruned", m18),
        "state_bound_arrival_pruned": _diagnostic_value(
            diagnostics, "state_bound_arrival_pruned", m18
        ),
        "state_bound_rejected": _diagnostic_value(diagnostics, "state_bound_rejected", m18),
        "dominance_pruned": _diagnostic_value(diagnostics, "dominance_pruned", m18),
        "pareto_pruned": _diagnostic_value(diagnostics, "pareto_pruned", m18),
        "session_id": result.session_id,
        "scope_digest": result.scope_digest,
        "reason": result.reason,
        "evaluator_errors": list(result.evaluator_errors),
    }


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    m18 = _m18()
    m19 = _m19()
    m21 = _m21()
    _set_cpu(args.cpu)
    before = _resource_snapshot(m18)
    fixture = None
    baseline = None
    candidate = None
    baseline_certificate = None
    candidate_certificate = None
    reference = None
    checkpoint: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        point = m18._point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(args.objective)
        baseline_planner = point._build_planner(fixture, objective)
        baseline_planner.eta_policy = m18.EtaRefinementPolicy(method="bounded")
        baseline_request = m18._request(point, fixture, objective)
        bound_planner, bound_request, _topology, corridor = m18._certificate(
            point, fixture, objective
        )
        if baseline_request != bound_request:
            raise RuntimeError("reference/bound request identity diverged")
        if (
            baseline_planner.temporal_scope(baseline_request).digest
            != corridor.certificate.scope.digest
        ):
            raise RuntimeError("reference/bound scope identity diverged")
        baseline, baseline_certificate, baseline_checkpoint = _run_policy(
            baseline_planner,
            baseline_request,
            corridor.certificate,
            pareto_pruning=False,
            mode=args.mode,
            slice_expansions=args.slice_expansions,
        )
        candidate, candidate_certificate, candidate_checkpoint = _run_policy(
            bound_planner,
            bound_request,
            corridor.certificate,
            pareto_pruning=True,
            mode=args.mode,
            slice_expansions=args.slice_expansions,
        )
        checkpoint = {"baseline": baseline_checkpoint, "candidate": candidate_checkpoint}
        if baseline.status is NonFifoSearchStatus.GOAL_FOUND:
            reference = m19._reference_search(
                baseline_planner, baseline_request, corridor.certificate
            )
    except NonFifoTemporalParetoError as error:
        errors["identity"] = f"{type(error).__name__}: {error}"
    except Exception as error:  # pragma: no cover - worker boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot(m18)

    baseline_record = (
        _policy_record(baseline, baseline_certificate, m18) if baseline is not None else None
    )
    candidate_record = (
        _policy_record(candidate, candidate_certificate, m18) if candidate is not None else None
    )
    comparison = None
    if baseline_record is not None and candidate_record is not None:
        baseline_for_compare = {
            "objective": args.objective,
            "repetition": args.repetition,
            "policy": "baseline",
            "status": baseline_record["status"],
            "frontier": baseline_record["frontier"],
            "frontier_digest": baseline_record["frontier_digest"],
            "scope_digest": baseline_record["scope_digest"],
            "frontier_certificate": baseline_record["certificate"],
        }
        candidate_for_compare = {
            "objective": args.objective,
            "repetition": args.repetition,
            "policy": "pareto",
            "status": candidate_record["status"],
            "frontier": candidate_record["frontier"],
            "frontier_digest": candidate_record["frontier_digest"],
            "scope_digest": candidate_record["scope_digest"],
            "frontier_certificate": candidate_record["certificate"],
        }
        comparison = m21._frontier_comparison(candidate_for_compare, baseline_for_compare)

    reference_route = reference.route if reference is not None else None
    baseline_reference_match = bool(
        baseline_record is not None
        and baseline_record["semantic"] is not None
        and reference_route is not None
        and m19._reference_matches(baseline_record["semantic"], reference_route)
    )
    candidate_reference_match = bool(
        candidate_record is not None
        and candidate_record["semantic"] is not None
        and reference_route is not None
        and m19._reference_matches(candidate_record["semantic"], reference_route)
    )
    certificate_usable = bool(
        baseline_record is not None
        and candidate_record is not None
        and baseline_record["certificate"]["usable"] is True
        and candidate_record["certificate"]["usable"] is True
    )
    unexpected_pruning = bool(
        baseline_record is not None
        and candidate_record is not None
        and (
            baseline_record["dominance_pruned"] > 0
            or candidate_record["dominance_pruned"] > 0
            or baseline_record["state_bound_rejected"] > 0
            or candidate_record["state_bound_rejected"] > 0
        )
    )
    state_bound_pruning_observed = bool(
        candidate_record is not None and candidate_record["state_bound_pruned"] > 0
    )
    resource_clean = _resource_clean(m18, before, after)
    resource_evidence_complete = _resource_evidence_complete(before, after, args.cpu)
    reference_status = reference.status if reference is not None else None
    resource_limited = bool(
        (baseline is not None and baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT)
        or (candidate is not None and candidate.status is NonFifoSearchStatus.RESOURCE_LIMIT)
        or reference_status == "REFERENCE_RESOURCE_LIMIT"
    )
    semantic_match = bool(
        baseline_record is not None
        and candidate_record is not None
        and baseline_record["status"] == NonFifoSearchStatus.GOAL_FOUND.value
        and candidate_record["status"] == NonFifoSearchStatus.GOAL_FOUND.value
        and baseline_record["semantic_digest"] == candidate_record["semantic_digest"]
    )
    comparison_accepted = bool(
        comparison is not None and comparison.get("accepted_frontier_match") is True
    )
    all_success_semantics = bool(
        not errors
        and certificate_usable
        and reference_status == "GOAL_FOUND"
        and baseline_reference_match
        and candidate_reference_match
        and semantic_match
        and comparison_accepted
        and not unexpected_pruning
        and state_bound_pruning_observed
    )
    if all_success_semantics:
        status = "PASS"
        reason = None
    elif resource_limited and not errors and not unexpected_pruning:
        status = "RESOURCE_LIMIT"
        reason = "frozen search limit reached"
    else:
        status = "FAIL"
        reason = "certified state-bound frontier semantic gate failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "mode": args.mode,
        "repetition": args.repetition,
        "status": status,
        "reason": reason,
        "baseline": baseline_record,
        "candidate": candidate_record,
        "reference": reference_route,
        "reference_status": reference_status,
        "reference_error": reference.error if reference is not None else None,
        "reference_stats": reference.stats if reference is not None else None,
        "reference_rejection_reasons": reference.rejection_reasons if reference is not None else {},
        "comparison": comparison,
        "semantic_match": semantic_match,
        "baseline_reference_match": baseline_reference_match,
        "candidate_reference_match": candidate_reference_match,
        "certificate_usable": certificate_usable,
        "state_bound_pruning_observed": state_bound_pruning_observed,
        "unexpected_pruning": unexpected_pruning,
        "resource_limited": resource_limited,
        "checkpoint": checkpoint,
        "errors": errors,
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
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
    m18 = _m18()
    point = m18._point_runner()
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    scopes: dict[str, str] = {}
    certificates: dict[str, str] = {}
    for objective in OBJECTIVES:
        planner, request, _topology, corridor = m18._certificate(point, fixture, objective)
        scopes[objective.value] = planner.temporal_scope(request).digest
        certificates[objective.value] = corridor.certificate.digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M22",
        "purpose": "real_24h_state_bound_pareto_frontier_equivalence",
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
        "modes": list(MODES),
        "repetitions": args.repetitions,
        "slice_expansions": args.slice_expansions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "eta_method": "bounded",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "state_bound_certificate_digests": certificates,
        "scope_digests": scopes,
        "search_limits": LIMITS,
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _record_key(record: Mapping[str, Any]) -> tuple[str, int, str] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    mode = record.get("mode")
    if not isinstance(objective, str) or objective not in {item.value for item in OBJECTIVES}:
        return None
    if not isinstance(repetition, int) or mode not in MODES:
        return None
    return objective, repetition, mode


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, mode: str
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
        "--segment",
        args.segment,
        "--output-dir",
        str(Path(args.output_dir).resolve()),
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--mode",
        mode,
        "--slice-expansions",
        str(args.slice_expansions),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
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
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    started = time.time()
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file,
    ):
            try:
                process = subprocess.Popen(
                    _child_command(args, objective, repetition, mode),
                    stdout=stdout_file,
                    stderr=stderr_file,
                    text=True,
                    env=env,
                )
            except OSError as error:
                return {
                    "schema_version": SCHEMA_VERSION,
                    "objective": objective.value,
                    "repetition": repetition,
                    "mode": mode,
                    "status": "INVALID/PENDING",
                    "reason": f"worker spawn failed: {type(error).__name__}: {error}",
                    "resource_clean": False,
                    "resource_evidence_complete": False,
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
                        "mode": mode,
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
                        "objective": objective.value,
                        "repetition": repetition,
                        "mode": mode,
                        "status": "TIMEOUT",
                        "reason": "worker_timeout",
                        "stdout": stdout_file.read()[-4000:],
                        "stderr": stderr_file.read()[-4000:],
                        "resource_clean": False,
                        "resource_evidence_complete": False,
                    }
                time.sleep(1.0)
            process.wait()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": mode,
            "status": "INVALID/PENDING",
            "reason": "worker exited non-zero",
            "returncode": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": mode,
            "status": "INVALID/PENDING",
            "reason": "worker did not emit one JSON object",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _summary(
    cases: list[dict[str, Any]], identity: dict[str, Any], malformed: int
) -> dict[str, Any]:
    repetitions = int(identity["repetitions"])
    expected = len(OBJECTIVES) * len(MODES) * repetitions
    keys = [_record_key(case) for case in cases]
    complete = (
        len(cases) == expected
        and malformed == 0
        and None not in keys
        and len(set(keys)) == len(keys)
    )
    resource_limited = [
        case for case in cases if case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
    ]
    hard_failed = [case for case in cases if case.get("status") in {"FAIL", "INVALID/PENDING"}]
    semantic_ok = bool(cases) and all(
        case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
        or (
            case.get("semantic_match") is True
            and case.get("baseline_reference_match") is True
            and case.get("candidate_reference_match") is True
        )
        for case in cases
    )
    frontier_ok = bool(cases) and all(
        case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
        or (
            isinstance(case.get("comparison"), Mapping)
            and case["comparison"].get("accepted_frontier_match") is True
        )
        for case in cases
    )
    certificate_ok = bool(cases) and all(
        case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
        or case.get("certificate_usable") is True
        for case in cases
    )
    fail_closed = not any(case.get("unexpected_pruning") is True for case in cases)
    pruning_total = sum(
        int((case.get("candidate") or {}).get("state_bound_pruned", 0) or 0) for case in cases
    )
    pruning_observed = pruning_total > 0
    resource_clean = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    resource_evidence = bool(cases) and all(
        case.get("resource_evidence_complete") is True for case in cases
    )
    deterministic_by_objective: dict[str, bool] = {}
    for objective in OBJECTIVES:
        selected = [case for case in cases if case.get("objective") == objective.value]
        signatures = {
            (
                case.get("status"),
                (case.get("baseline") or {}).get("semantic_digest"),
                (case.get("candidate") or {}).get("semantic_digest"),
                (case.get("baseline") or {}).get("frontier_digest"),
                (case.get("candidate") or {}).get("frontier_digest"),
                (case.get("candidate") or {}).get("state_bound_pruned"),
            )
            for case in selected
        }
        deterministic_by_objective[objective.value] = (
            len(selected) == len(MODES) * repetitions and len(signatures) == 1
        )
    deterministic = bool(deterministic_by_objective) and all(deterministic_by_objective.values())
    all_case_gates = bool(cases) and all(
        case.get("status") in {"PASS", "RESOURCE_LIMIT", "TIMEOUT"} for case in cases
    )
    if not complete:
        status = "INVALID/PENDING"
    elif (
        hard_failed
        or not all_case_gates
        or not semantic_ok
        or not frontier_ok
        or not certificate_ok
        or not fail_closed
    ):
        status = "NO_FRONTIER_PROOF/FAIL"
    elif resource_limited:
        status = "REAL_INPUT_24H_STATE_BOUND_FRONTIER_RESOURCE_FAIL"
    elif not pruning_observed:
        status = "NO_FRONTIER_PROOF/FAIL"
    elif not deterministic or not resource_clean or not resource_evidence:
        status = "REAL_INPUT_24H_STATE_BOUND_FRONTIER_INCONCLUSIVE"
    else:
        status = "READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "all_case_gates": all_case_gates,
        "semantic_match": semantic_ok,
        "frontier_equivalence": frontier_ok,
        "certificate_usable": certificate_ok,
        "fail_closed": fail_closed,
        "deterministic": deterministic,
        "deterministic_by_objective": deterministic_by_objective,
        "resource_limited_case_count": len(resource_limited),
        "hard_failure_case_count": len(hard_failed),
        "all_resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence,
        "observed_state_bound_pruning": pruning_total,
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
            raise RuntimeError("another M22 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _run_parent(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.slice_expansions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions/slice/timeout must be positive")
    root = Path(__file__).resolve().parents[1]
    m18 = _m18()
    point = m18._point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M22 real evidence requires a clean implementation worktree")
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
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": (
                "manifest.json",
                "cases.jsonl",
                "frontier-comparison.jsonl",
                "resource-frontier.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ),
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        cases, malformed = _read_jsonl(cases_path) if args.resume else ([], 0)
        completed = {_record_key(case) for case in cases}
        completed.discard(None)
        expected = len(OBJECTIVES) * len(MODES) * args.repetitions
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
                    for mode in MODES:
                        key = (objective.value, repetition, mode)
                        if key in completed:
                            continue
                        record = _run_child(args, objective, repetition, mode, heartbeat)
                        record.setdefault("schema_version", SCHEMA_VERSION)
                        record["experiment_id"] = identity["experiment_id"]
                        if _record_key(record) != key:
                            raise RuntimeError("worker returned mismatched case identity")
                        cases.append(record)
                        _append_jsonl(cases_path, record)
                        _append_jsonl(output / "resource-frontier.jsonl", record)
                        if record.get("comparison") is not None:
                            _append_jsonl(
                                output / "frontier-comparison.jsonl", record["comparison"]
                            )
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
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return (
            0
            if summary["status"] == "READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW"
            else 2
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="rolling_0_24h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--slice-expansions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--objective", choices=tuple(item.value for item in OBJECTIVES), help=argparse.SUPPRESS
    )
    parser.add_argument("--repetition", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=MODES, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None or args.mode is None:
            raise SystemExit("worker requires --objective and --mode")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    if args.cpu < 0:
        raise SystemExit("cpu must be non-negative")
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
