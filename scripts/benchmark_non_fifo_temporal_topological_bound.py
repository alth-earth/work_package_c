#!/usr/bin/env python3
"""Synthetic gate for graph-topological arrival-envelope certificates.

The runner is a C-internal research sidecar.  It computes graph shortest-path
lower bounds with the same finite adjacency as the synthetic planner, then
feeds the resulting complete envelope to the explicit arrival-bounded adapter.
Incomplete, scope-mismatched, and failed topology evidence must never prune.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_arrival_bounded_search,
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_corridor import derive_temporal_corridor
from arctic_route_planning.planners.temporal_qualification import TemporalScope
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-topological-bound.v1"
PROFILES = ("small", "medium", "stress")
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
MODES = ("certified", "scope_mismatch", "incomplete", "adjacency_failure")
DEFAULT_REPETITIONS = 2
DEFAULT_TIMEOUT_SECONDS = 300.0
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}

IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_topological_bound.py",
    "scripts/benchmark_non_fifo_temporal_arrival_bound.py",
    "scripts/benchmark_temporal_dominance.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
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
    base = _load_script("benchmark_temporal_dominance.py", "m7_resource_base")
    return base._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if (after.get("process_swap_kib") or 0) != 0:
        return False
    before_host = before.get("host_swap_pages") or {}
    after_host = after.get("host_swap_pages") or {}
    if before_host and after_host and before_host != after_host:
        return False
    affinity = after.get("cpu_affinity")
    return isinstance(affinity, list) and len(affinity) == 1


def _certificate(
    planner: Any,
    request: Any,
    objective: str,
    mode: str,
) -> TemporalStateBoundCertificate:
    scope = planner.temporal_scope(request)
    nodes = tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )
    neighbors = planner.grid.neighbors
    if mode == "adjacency_failure":

        def neighbors(node: Any) -> tuple[Any, ...]:
            if node == nodes[1]:
                return ((999, 999),)
            return tuple(planner.grid.neighbors(node))

    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=planner.vessel_model.maximum_speed_knots * 1.852,
    )
    if not topology.usable:
        return TemporalStateBoundCertificate(
            scope=scope,
            allowed_nodes=(),
            excluded_nodes=(),
            status="REJECTED",
            reason=topology.reason,
            proof_digest=topology.proof_digest,
            coverage_complete=False,
            evaluator_certified=False,
        )
    evidence = topology.as_admissible_bound_evidence()
    corridor = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        forward_lower_hours=topology.forward_map,
        reverse_lower_hours=topology.reverse_map,
        horizon_hours=request.maximum_elapsed.total_seconds() / 3600.0,
        objective=objective,
        bound_evidence=evidence,
        generated_nodes=nodes,
        include_arrival_upper_bounds=True,
    )
    certificate = corridor.certificate
    if mode == "scope_mismatch":
        mismatched_scope = TemporalScope.from_mapping(
            {**scope.mapping, "topological_bound_revision": "mismatch"}
        )
        certificate = TemporalStateBoundCertificate(
            scope=mismatched_scope,
            allowed_nodes=certificate.allowed_nodes,
            excluded_nodes=certificate.excluded_nodes,
            exclusion_proof=certificate.exclusion_proof,
            proof_digest=certificate.proof_digest,
            status=certificate.status,
            reason=certificate.reason,
            coverage_complete=certificate.coverage_complete,
            evaluator_certified=certificate.evaluator_certified,
            arrival_upper_hours=certificate.arrival_upper_hours,
        )
    elif mode == "incomplete":
        certificate = TemporalStateBoundCertificate(
            scope=certificate.scope,
            allowed_nodes=certificate.allowed_nodes,
            excluded_nodes=certificate.excluded_nodes,
            exclusion_proof=certificate.exclusion_proof,
            proof_digest=certificate.proof_digest,
            status=certificate.status,
            reason=certificate.reason,
            coverage_complete=certificate.coverage_complete,
            evaluator_certified=certificate.evaluator_certified,
            arrival_upper_hours=certificate.arrival_upper_hours[:-1],
        )
    return certificate


def _worker(profile_name: str, objective_name: str, mode: str, cpu: int) -> dict[str, Any]:
    _set_cpu(cpu)
    base = _load_script("benchmark_non_fifo_temporal_arrival_bound.py", "m7_arrival_base")
    point = _load_script("benchmark_temporal_dominance.py", "m7_dominance_base")
    objective = ObjectiveMode(objective_name)
    baseline_planner, request, _ = point._build_components(
        profile_name, objective, with_dominance=False
    )
    candidate_planner, _, _ = point._build_components(profile_name, objective, with_dominance=False)
    certificate = _certificate(candidate_planner, request, objective_name, mode)
    candidate_planner.state_bound_certificate = certificate
    before = _resource_snapshot()
    started = perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    bounded = None
    reference = None
    adapter_error = None
    certificate_rejection_reason = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline is not None and baseline.planning_result is not None:
        try:
            reference = point._reference_solution(
                baseline_planner.grid,
                request,
                point.SYNTHETIC_PROFILES[profile_name],
                baseline_planner._cost_model(objective),
            )
        except Exception as error:  # pragma: no cover - evidence boundary
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        if mode == "incomplete":
            try:
                run_non_fifo_temporal_arrival_bounded_search(
                    candidate_planner, request, certificate
                )
            except NonFifoTemporalAdapterError as error:
                adapter_error = f"{type(error).__name__}: {error}"
            bounded = run_non_fifo_temporal_bounded_search(candidate_planner, request, certificate)
        else:
            try:
                bounded = run_non_fifo_temporal_arrival_bounded_search(
                    candidate_planner, request, certificate
                )
            except NonFifoTemporalAdapterError as error:
                # A rejected certificate is expected for the topology-failure
                # modes.  Preserve the explicit rejection without treating it
                # as a worker crash or allowing any pruning.
                adapter_error = f"{type(error).__name__}: {error}"
                certificate_rejection_reason = certificate.reason or "certificate_rejected"
    except Exception as error:  # pragma: no cover - worker boundary
        errors["bounded"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_route = None if baseline is None else base._route_payload(baseline, point)
    bounded_route = None if bounded is None else base._route_payload(bounded, point)
    baseline_match = (
        baseline_route is not None
        and reference is not None
        and base._semantic_matches(baseline_route, reference)
    )
    bounded_match = (
        bounded_route is not None
        and reference is not None
        and base._semantic_matches(bounded_route, reference)
    )
    diagnostics = None if bounded is None else base._jsonable(bounded.diagnostics)
    pruned = 0 if bounded is None else int(bounded.diagnostics.state_bound_pruned)
    arrival_pruned = 0 if bounded is None else int(bounded.diagnostics.state_bound_arrival_pruned)
    rejected = (
        int(bounded.diagnostics.state_bound_rejected)
        if bounded is not None
        else (1 if certificate_rejection_reason is not None else 0)
    )
    resource_clean = _resource_clean(before, after)
    if mode == "certified":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and bounded_match
            and baseline.semantic_digest == bounded.semantic_digest
            and arrival_pruned > 0
            and rejected == 0
            and resource_clean
        )
        status = "PASS" if passed else "FAIL"
    elif mode == "incomplete":
        passed = (
            not errors
            and adapter_error is not None
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and bounded_match
            and baseline.semantic_digest == bounded.semantic_digest
            and arrival_pruned == 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    else:
        rejected_adapter = bounded is None and adapter_error is not None
        rejected_result = (
            bounded is not None
            and bounded.status is NonFifoSearchStatus.EVALUATOR_FAILURE
            and bounded.reason == "state_bound_rejected"
            and bounded_route is None
            and rejected > 0
        )
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and (rejected_adapter or rejected_result)
            and bounded_route is None
            and pruned == 0
            and arrival_pruned == 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile": profile_name,
        "objective": objective_name,
        "mode": mode,
        "baseline_status": None if baseline is None else baseline.status.value,
        "bounded_status": None if bounded is None else bounded.status.value,
        "bounded_reason": None if bounded is None else bounded.reason,
        "adapter_error": adapter_error,
        "certificate_rejection_reason": certificate_rejection_reason,
        "semantic_match": baseline_match and bounded_match,
        "reference_match": baseline_match and bounded_match,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "bounded_semantic_digest": None if bounded is None else bounded.semantic_digest,
        "baseline_route": baseline_route,
        "bounded_route": bounded_route,
        "reference": reference,
        "state_bound_policy": "graph-topological-arrival-envelope" if mode == "certified" else mode,
        "state_bound_certificate_digest": certificate.digest,
        "state_bound_checks": 0 if bounded is None else int(bounded.diagnostics.state_bound_checks),
        "state_bound_pruned": pruned,
        "state_bound_arrival_pruned": arrival_pruned,
        "state_bound_rejected": rejected,
        "diagnostics": diagnostics,
        "compute_ms": (perf_counter() - started) * 1000.0,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "cpu": cpu,
        "reason": None
        if status in {"PASS", "REJECTED_FAIL_CLOSED"}
        else "topological bound gate failed",
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
            "state_bound_pruned": 0,
            "state_bound_arrival_pruned": 0,
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
            "state_bound_pruned": 0,
            "state_bound_arrival_pruned": 0,
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
            "state_bound_pruned": 0,
            "state_bound_arrival_pruned": 0,
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
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(records: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    certified = [item for item in records if item.get("mode") == "certified"]
    rejected = [item for item in records if item.get("mode") != "certified"]
    certified_pass = bool(certified) and all(item.get("status") == "PASS" for item in certified)
    fail_closed = bool(rejected) and all(
        item.get("status") == "REJECTED_FAIL_CLOSED"
        and int(item.get("state_bound_pruned", 0)) == 0
        and int(item.get("state_bound_arrival_pruned", 0)) == 0
        for item in rejected
    )
    groups: dict[tuple[str, str, str], set[tuple[Any, Any]]] = {}
    for item in records:
        key = (str(item.get("profile")), str(item.get("objective")), str(item.get("mode")))
        groups.setdefault(key, set()).add(
            (item.get("baseline_semantic_digest"), item.get("bounded_semantic_digest"))
        )
    deterministic = bool(records) and all(len(values) == 1 for values in groups.values())
    semantic = bool(records) and all(
        item.get("semantic_match") is True
        for item in certified + [item for item in records if item.get("mode") == "incomplete"]
    )
    status = (
        "TEMPORAL_TOPOLOGICAL_BOUND_MATRIX_PASS"
        if certified_pass and fail_closed and deterministic and semantic
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
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
        "observed_arrival_pruning": sum(
            int(item.get("state_bound_arrival_pruned", 0)) for item in certified
        ),
        "rejected_pruning_total": sum(int(item.get("state_bound_pruned", 0)) for item in rejected),
        "production_candidate_enabled": False,
        "records": records,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--worker-timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--cpu", type=int, default=-1)
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
    identity = _identity(root, args.repetitions, args.worker_timeout_seconds, args.cpu)
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"), text=True
    ).strip()
    if dirty:
        raise RuntimeError("topological-bound runner requires a clean implementation worktree")
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
    heartbeat = output / "heartbeat.json"
    cases_path = output / "cases.jsonl"
    frontier = output / "resource-frontier.jsonl"
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
                        record.update(
                            {
                                "experiment_id": identity["experiment_id"],
                                "profile": profile,
                                "objective": objective,
                                "mode": mode,
                                "repetition": repetition,
                            }
                        )
                        _append_jsonl(cases_path, record)
                        _append_jsonl(
                            frontier,
                            {
                                "profile": profile,
                                "objective": objective,
                                "mode": mode,
                                "repetition": repetition,
                                "state_bound_pruned": record.get("state_bound_pruned", 0),
                                "state_bound_arrival_pruned": record.get(
                                    "state_bound_arrival_pruned", 0
                                ),
                                "resources_before": record.get("resources_before"),
                                "resources_after": record.get("resources_after"),
                            },
                        )
                    records.append(record)
                    _atomic_json(
                        heartbeat,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
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
    _atomic_json(
        heartbeat,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "updated_at": datetime.now(UTC),
        },
    )
    marker = (
        "ALL_DONE"
        if summary["status"] == "TEMPORAL_TOPOLOGICAL_BOUND_MATRIX_PASS"
        else "STOPPED_HARD"
    )
    _atomic_json(
        output / marker, {"status": summary["status"], "experiment_id": identity["experiment_id"]}
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "records"},
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] == "TEMPORAL_TOPOLOGICAL_BOUND_MATRIX_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
