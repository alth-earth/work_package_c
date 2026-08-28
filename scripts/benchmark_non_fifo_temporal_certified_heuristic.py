#!/usr/bin/env python3
"""Synthetic gate for certified heuristic ordering on exact-arrival labels.

The runner compares the zero-heuristic non-FIFO adapter with an explicit
certificate that uses the reverse graph lower bound only to order the queue.
It never installs dominance or a state-bound certificate and therefore cannot
silently turn ordering evidence into label pruning.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_certified_heuristic_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_heuristic_bounds import (
    qualify_temporal_heuristic,
)
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-certified-heuristic.v1"
PROFILES = ("small", "medium", "stress")
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
MODES = ("certified", "scope_mismatch", "incomplete", "non_admissible", "unknown_evaluator")
DEFAULT_REPETITIONS = 2
DEFAULT_TIMEOUT_SECONDS = 300.0
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_certified_heuristic.py",
    "scripts/benchmark_temporal_dominance.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_heuristic_bounds.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "uv.lock",
)


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    base = _load_script("benchmark_temporal_dominance.py", "m8_resource_base")
    return base._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_swap = before.get("host_swap_pages") or {}
    after_swap = after.get("host_swap_pages") or {}
    if before_swap and after_swap and before_swap != after_swap:
        return False
    if (after.get("process_swap_kib") or 0) != (before.get("process_swap_kib") or 0):
        return False
    before_cpu = before.get("cpu_affinity")
    after_cpu = after.get("cpu_affinity")
    return isinstance(after_cpu, list) and after_cpu == before_cpu


def _topology_and_certificate(planner: Any, request: Any, mode: str) -> Any:
    scope = planner.temporal_scope(request)
    nodes = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=planner.grid.neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=planner.vessel_model.maximum_speed_knots * 1.852,
    )
    certificate = qualify_temporal_heuristic(
        scope=scope,
        topology=topology,
        cost_model=planner._cost_model(request.objective),
        objective=request.objective.value,
        expected_scope=scope,
    )
    if mode == "scope_mismatch":
        certificate = replace(
            certificate,
            scope=type(scope).from_mapping({**scope.mapping, "heuristic_revision": "mismatch"}),
        )
    elif mode == "incomplete":
        certificate = replace(
            certificate,
            objective_lower_hours=certificate.objective_lower_hours[:-1],
        )
    elif mode == "non_admissible":
        certificate = replace(certificate, admissible=False, reason="fixture_non_admissible")
    elif mode == "unknown_evaluator":
        certificate = replace(certificate, evaluator_digest="unknown:fixture")
    return topology, certificate


def _worker(profile_name: str, objective_name: str, mode: str, cpu: int) -> dict[str, Any]:
    _set_cpu(cpu)
    base = _load_script("benchmark_temporal_dominance.py", "m8_dominance_base")
    evidence = _load_script("benchmark_non_fifo_temporal_arrival_bound.py", "m8_arrival_base")
    objective = ObjectiveMode(objective_name)
    baseline_planner, baseline_request, _ = base._build_components(
        profile_name, objective, with_dominance=False
    )
    candidate_planner, candidate_request, _ = base._build_components(
        profile_name, objective, with_dominance=False
    )
    candidate_request = replace(candidate_request, use_heuristic=True)
    profile = base.SYNTHETIC_PROFILES[profile_name]
    topology, certificate = _topology_and_certificate(
        candidate_planner,
        candidate_request,
        mode,
    )
    candidate_planner.heuristic_certificate = certificate
    before = _resource_snapshot()
    started = perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    candidate = None
    adapter_error: str | None = None
    reference = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, baseline_request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline is not None and baseline.planning_result is not None:
        try:
            reference = base._reference_solution(
                baseline_planner.grid,
                baseline_request,
                profile,
                baseline_planner._cost_model(objective),
            )
        except Exception as error:  # pragma: no cover - evidence boundary
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        try:
            candidate = run_non_fifo_temporal_certified_heuristic_search(
                candidate_planner,
                candidate_request,
                certificate,
            )
        except NonFifoTemporalAdapterError as error:
            adapter_error = f"{type(error).__name__}: {error}"
    except Exception as error:  # pragma: no cover - worker boundary
        errors["candidate"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_route = evidence._route_payload(baseline, base)
    candidate_route = evidence._route_payload(candidate, base)
    baseline_match = (
        baseline_route is not None
        and reference is not None
        and evidence._semantic_matches(baseline_route, reference)
    )
    candidate_match = (
        candidate_route is not None
        and reference is not None
        and evidence._semantic_matches(candidate_route, reference)
    )
    diagnostics = None if candidate is None else base._jsonable(candidate.diagnostics)
    candidate_pruning = 0 if candidate is None else int(candidate.diagnostics.dominance_pruned)
    state_pruning = 0 if candidate is None else int(candidate.diagnostics.state_bound_pruned)
    resource_clean = _resource_clean(before, after)
    expected_rejection = mode != "certified"
    if mode == "certified":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and candidate is not None
            and candidate.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and candidate_match
            and baseline.semantic_digest == candidate.semantic_digest
            and candidate.diagnostics.heuristic_scope_match
            and candidate.diagnostics.heuristic_rejected == 0
            and candidate_pruning == 0
            and state_pruning == 0
            and resource_clean
        )
        status = "PASS" if passed else "FAIL"
    else:
        passed = (
            not errors
            and expected_rejection
            and adapter_error is not None
            and candidate is None
            and candidate_pruning == 0
            and state_pruning == 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    baseline_diag = None if baseline is None else base._jsonable(baseline.diagnostics)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile": profile_name,
        "objective": objective_name,
        "mode": mode,
        "baseline_status": None if baseline is None else baseline.status.value,
        "candidate_status": None if candidate is None else candidate.status.value,
        "adapter_error": adapter_error,
        "certificate_reason": certificate.reason,
        "certificate_usable": certificate.usable,
        "topology_usable": topology.usable,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "candidate_semantic_digest": None if candidate is None else candidate.semantic_digest,
        "semantic_match": baseline_match and candidate_match,
        "reference_match": baseline_match and candidate_match,
        "baseline_route": baseline_route,
        "candidate_route": candidate_route,
        "reference": reference,
        "baseline_diagnostics": baseline_diag,
        "diagnostics": diagnostics,
        "baseline_expanded_labels": 0 if baseline is None else baseline.diagnostics.expanded_labels,
        "candidate_expanded_labels": 0
        if candidate is None
        else candidate.diagnostics.expanded_labels,
        "baseline_queue_peak": 0 if baseline is None else baseline.diagnostics.queue_peak,
        "candidate_queue_peak": 0 if candidate is None else candidate.diagnostics.queue_peak,
        "heuristic_policy": None if candidate is None else candidate.diagnostics.heuristic_policy,
        "heuristic_scope_match": False
        if candidate is None
        else candidate.diagnostics.heuristic_scope_match,
        "heuristic_rejected": 0 if candidate is None else candidate.diagnostics.heuristic_rejected,
        "dominance_pruned": candidate_pruning,
        "state_bound_pruned": state_pruning,
        "certificate_digest": certificate.digest,
        "topology_digest": topology.digest,
        "compute_ms": (perf_counter() - started) * 1000.0,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "cpu": cpu,
        "reason": None if status in {"PASS", "REJECTED_FAIL_CLOSED"} else "heuristic gate failed",
        "production_candidate_enabled": False,
    }


def _worker_command(profile: str, objective: str, mode: str, cpu: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--profile",
        profile,
        "--objective",
        objective,
        "--mode",
        mode,
        "--cpu",
        str(cpu),
    ]


def _run_worker(
    profile: str,
    objective: str,
    mode: str,
    *,
    cpu: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            _worker_command(profile, objective, mode, cpu),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "TIMEOUT",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
        }
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": f"worker JSON decode failed: {error}",
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _identity(root: Path, repetitions: int, timeout_seconds: float, cpu: int) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
        "profiles": PROFILES,
        "objectives": OBJECTIVES,
        "modes": MODES,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "cpu": cpu,
        "search_limits": LIMITS,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(records: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    certified = [item for item in records if item.get("mode") == "certified"]
    rejected = [item for item in records if item.get("mode") != "certified"]
    certified_pass = bool(certified) and all(item.get("status") == "PASS" for item in certified)
    fail_closed = bool(rejected) and all(
        item.get("status") == "REJECTED_FAIL_CLOSED"
        and int(item.get("dominance_pruned", 0)) == 0
        and int(item.get("state_bound_pruned", 0)) == 0
        for item in rejected
    )
    groups: dict[tuple[str, str, str], set[tuple[Any, Any]]] = {}
    for item in records:
        key = (str(item.get("profile")), str(item.get("objective")), str(item.get("mode")))
        groups.setdefault(key, set()).add(
            (item.get("baseline_semantic_digest"), item.get("candidate_semantic_digest"))
        )
    deterministic = bool(records) and all(len(values) == 1 for values in groups.values())
    semantic = bool(certified) and all(item.get("semantic_match") is True for item in certified)
    status = (
        "TEMPORAL_CERTIFIED_HEURISTIC_MATRIX_PASS"
        if certified_pass and fail_closed and deterministic and semantic
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
    improvements = [
        {
            "profile": item.get("profile"),
            "objective": item.get("objective"),
            "repetition": item.get("repetition"),
            "expanded_delta": int(item.get("baseline_expanded_labels", 0))
            - int(item.get("candidate_expanded_labels", 0)),
            "queue_delta": int(item.get("baseline_queue_peak", 0))
            - int(item.get("candidate_queue_peak", 0)),
        }
        for item in certified
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "case_count": len(records),
        "expected_case_count": len(PROFILES)
        * len(OBJECTIVES)
        * len(MODES)
        * identity["repetitions"],
        "certified_case_count": len(certified),
        "rejected_case_count": len(rejected),
        "certified_cases_pass": certified_pass,
        "fail_closed": fail_closed,
        "deterministic": deterministic,
        "semantic_match": semantic,
        "certified_expansion_deltas": improvements,
        "candidate_enabled": False,
        "records": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--worker-timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--profile", choices=PROFILES)
    parser.add_argument("--objective", choices=OBJECTIVES)
    parser.add_argument("--mode", choices=MODES)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.worker:
        if not args.profile or not args.objective or not args.mode:
            raise SystemExit("worker requires --profile, --objective and --mode")
        print(json.dumps(_worker(args.profile, args.objective, args.mode, args.cpu)))
        return 0
    if args.output_dir is None:
        raise SystemExit("--output-dir is required")
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0.0 or args.cpu < -1:
        raise SystemExit("repetitions/timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"), text=True
    ).strip()
    if dirty:
        raise RuntimeError("certified-heuristic runner requires a clean implementation worktree")
    identity = _identity(root, args.repetitions, args.worker_timeout_seconds, args.cpu)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = output / "manifest.json"
    if manifest.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume")
        if json.loads(manifest.read_text(encoding="utf-8")).get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity mismatch")
    _atomic_json(
        manifest, {"schema_version": SCHEMA_VERSION, "status": "RUNNING", "identity": identity}
    )
    cases_path = output / "cases.jsonl"
    frontier = output / "resource-frontier.jsonl"
    heartbeat = output / "heartbeat.json"
    existing: dict[tuple[str, str, str, int], dict[str, Any]] = {}
    if args.resume and cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (
                record.get("profile"),
                record.get("objective"),
                record.get("mode"),
                record.get("repetition"),
            )
            if key in existing:
                raise RuntimeError("duplicate resume case")
            existing[key] = record
    records: list[dict[str, Any]] = []
    for profile in PROFILES:
        for objective in OBJECTIVES:
            for mode in MODES:
                for repetition in range(args.repetitions):
                    key = (profile, objective, mode, repetition)
                    record = existing.get(key)
                    if record is None:
                        record = _run_worker(
                            profile,
                            objective,
                            mode,
                            cpu=args.cpu,
                            timeout_seconds=args.worker_timeout_seconds,
                        )
                        record["repetition"] = repetition
                        _append_jsonl(cases_path, record)
                        _append_jsonl(frontier, record)
                    records.append(record)
                    _atomic_json(
                        heartbeat,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "RUNNING",
                            "experiment_id": identity["experiment_id"],
                            "completed_cases": len(records),
                            "expected_cases": len(PROFILES)
                            * len(OBJECTIVES)
                            * len(MODES)
                            * args.repetitions,
                        },
                    )
    summary = _summary(records, identity)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        manifest,
        {"schema_version": SCHEMA_VERSION, "status": summary["status"], "identity": identity},
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    _atomic_json(
        heartbeat,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "experiment_id": identity["experiment_id"],
            "completed_cases": len(records),
            "expected_cases": len(records),
        },
    )
    return 0 if summary["status"] == "TEMPORAL_CERTIFIED_HEURISTIC_MATRIX_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
