#!/usr/bin/env python3
"""Synthetic gate for the composed arrival-envelope/heuristic research path.

The runner keeps three searches distinct: an unbounded exact-arrival control,
an arrival-envelope-only search, and the composed search.  The independent
fixture oracle remains zero-heuristic and exact-arrival; it is never used to
inject a route or a bound into a candidate.  All proof mechanisms are explicit
and the production planner is untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_arrival_bounded_search,
    run_non_fifo_temporal_composed_bound_heuristic_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_heuristic_bounds import (
    TemporalHeuristicCertificate,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-composed-bound.v1"
PROFILES = ("small", "medium", "stress")
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
MODES = (
    "certified",
    "state_incomplete",
    "heuristic_incomplete",
    "scope_mismatch",
    "unknown_evaluator",
    "non_admissible",
    "cancelled",
    "resource_limit",
)
REJECTED_MODES = {
    "state_incomplete",
    "heuristic_incomplete",
    "scope_mismatch",
    "unknown_evaluator",
    "non_admissible",
}
CONTROL_MODES = {"cancelled", "resource_limit"}
DEFAULT_REPETITIONS = 1
DEFAULT_TIMEOUT_SECONDS = 300.0
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_composed_bound_heuristic.py",
    "scripts/benchmark_non_fifo_temporal_arrival_bound.py",
    "scripts/benchmark_non_fifo_temporal_certified_heuristic.py",
    "scripts/benchmark_temporal_dominance.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
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
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
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
    base = _load_script("benchmark_temporal_dominance.py", "m10_resource_base")
    return base._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    before_swap = before.get("host_swap_pages") or {}
    after_swap = after.get("host_swap_pages") or {}
    if before_swap and after_swap and before_swap != after_swap:
        return False
    if (after.get("process_swap_kib") or 0) != (before.get("process_swap_kib") or 0):
        return False
    affinity = after.get("cpu_affinity")
    return isinstance(affinity, list) and len(affinity) == 1


def _state_certificate(
    arrival_module: Any,
    planner: Any,
    request: Any,
    profile: str,
    objective: str,
):
    certificate = arrival_module._certificate(
        planner,
        request,
        profile,
        objective,
        mode="certified",
    )
    if not isinstance(certificate, TemporalStateBoundCertificate):
        raise RuntimeError("arrival runner returned an unexpected certificate")
    return certificate


def _heuristic_certificate(
    heuristic_module: Any,
    planner: Any,
    request: Any,
    mode: str,
) -> tuple[Any, TemporalHeuristicCertificate]:
    topology, certificate = heuristic_module._topology_and_certificate(
        planner,
        request,
        "certified",
    )
    if not isinstance(certificate, TemporalHeuristicCertificate):
        raise RuntimeError("heuristic runner returned an unexpected certificate")
    if mode == "heuristic_incomplete":
        certificate = replace(
            certificate,
            objective_lower_hours=certificate.objective_lower_hours[:-1],
        )
    elif mode == "scope_mismatch":
        certificate = replace(
            certificate,
            scope=TemporalScope.from_mapping(
                {**certificate.scope.mapping, "composed_scope_revision": "mismatch"}
            ),
        )
    elif mode == "unknown_evaluator":
        certificate = replace(certificate, evaluator_digest="unknown:composed-fixture")
    elif mode == "non_admissible":
        certificate = replace(certificate, admissible=False, reason="fixture_non_admissible")
    return topology, certificate


def _route_payload(result: Any, base: Any) -> dict[str, Any] | None:
    if result is None or result.planning_result is None:
        return None
    return base._route_payload(result)


def _worker(profile_name: str, objective_name: str, mode: str, cpu: int) -> dict[str, Any]:
    _set_cpu(cpu)
    base = _load_script("benchmark_temporal_dominance.py", "m10_synthetic_base")
    arrival_module = _load_script(
        "benchmark_non_fifo_temporal_arrival_bound.py",
        "m10_arrival_base",
    )
    heuristic_module = _load_script(
        "benchmark_non_fifo_temporal_certified_heuristic.py",
        "m10_heuristic_base",
    )
    objective = ObjectiveMode(objective_name)
    profile = base.SYNTHETIC_PROFILES[profile_name]
    baseline_planner, baseline_request, _ = base._build_components(
        profile_name,
        objective,
        with_dominance=False,
    )
    arrival_planner, arrival_request, _ = base._build_components(
        profile_name,
        objective,
        with_dominance=False,
    )
    candidate_planner, candidate_request, _ = base._build_components(
        profile_name,
        objective,
        with_dominance=False,
    )
    candidate_request = replace(candidate_request, use_heuristic=True)
    state_bound = _state_certificate(
        arrival_module,
        arrival_planner,
        arrival_request,
        profile_name,
        objective_name,
    )
    candidate_state_bound = _state_certificate(
        arrival_module,
        candidate_planner,
        candidate_request,
        profile_name,
        objective_name,
    )
    topology, heuristic = _heuristic_certificate(
        heuristic_module,
        candidate_planner,
        candidate_request,
        mode,
    )
    if mode == "state_incomplete":
        candidate_state_bound = replace(
            candidate_state_bound,
            arrival_upper_hours=candidate_state_bound.arrival_upper_hours[:-1],
        )
    candidate_planner.state_bound_certificate = candidate_state_bound
    candidate_planner.heuristic_certificate = heuristic
    arrival_planner.state_bound_certificate = state_bound
    if mode == "resource_limit":
        candidate_planner.limits = replace(candidate_planner.limits, max_expansions=1)
        # The limits digest is part of scope, so rebuild both certificates after
        # the deliberate resource fixture mutation.
        candidate_state_bound = _state_certificate(
            arrival_module,
            candidate_planner,
            candidate_request,
            profile_name,
            objective_name,
        )
        topology, heuristic = _heuristic_certificate(
            heuristic_module,
            candidate_planner,
            candidate_request,
            "certified",
        )
        candidate_planner.state_bound_certificate = candidate_state_bound
        candidate_planner.heuristic_certificate = heuristic
    if mode == "cancelled":
        candidate_request = replace(candidate_request, cancel_check=lambda: True)

    before = _resource_snapshot()
    started = perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    arrival_bound = None
    candidate = None
    adapter_error: str | None = None
    reference = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, baseline_request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    try:
        arrival_bound = run_non_fifo_temporal_arrival_bounded_search(
            arrival_planner,
            arrival_request,
            state_bound,
        )
    except Exception as error:  # pragma: no cover - worker boundary
        errors["arrival_bound"] = f"{type(error).__name__}: {error}"
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
        candidate = run_non_fifo_temporal_composed_bound_heuristic_search(
            candidate_planner,
            candidate_request,
            candidate_state_bound,
            heuristic,
        )
    except NonFifoTemporalAdapterError as error:
        adapter_error = f"{type(error).__name__}: {error}"
    except Exception as error:  # pragma: no cover - worker boundary
        errors["candidate"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_route = _route_payload(baseline, base)
    arrival_route = _route_payload(arrival_bound, base)
    candidate_route = _route_payload(candidate, base)
    semantic = arrival_module._semantic_matches
    baseline_match = reference is not None and semantic(baseline_route, reference)
    arrival_match = reference is not None and semantic(arrival_route, reference)
    candidate_match = reference is not None and semantic(candidate_route, reference)
    candidate_diagnostics = (
        None
        if candidate is None or candidate.diagnostics is None
        else base._jsonable(candidate.diagnostics)
    )
    arrival_diagnostics = (
        None if arrival_bound is None else base._jsonable(arrival_bound.diagnostics)
    )
    candidate_diag = None if candidate is None else candidate.diagnostics
    state_pruned = 0 if candidate_diag is None else int(candidate_diag.state_bound_pruned)
    candidate_arrival_pruned = (
        0 if candidate_diag is None else int(candidate_diag.state_bound_arrival_pruned)
    )
    arrival_pruned = (
        0
        if arrival_bound is None
        else int(arrival_bound.diagnostics.state_bound_arrival_pruned)
    )
    heuristic_rejected = (
        0 if candidate_diag is None else int(candidate_diag.heuristic_rejected)
    )
    state_rejected = 0 if candidate_diag is None else int(candidate_diag.state_bound_rejected)
    resource_clean = _resource_clean(before, after)
    if mode == "certified":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and arrival_bound is not None
            and arrival_bound.status is NonFifoSearchStatus.GOAL_FOUND
            and candidate is not None
            and candidate.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and arrival_match
            and candidate_match
            and baseline.semantic_digest == arrival_bound.semantic_digest
            and baseline.semantic_digest == candidate.semantic_digest
            and candidate_diag is not None
            and candidate_diag.heuristic_scope_match
            and heuristic_rejected == 0
            and state_rejected == 0
            and arrival_pruned > 0
            and candidate_diag.dominance_pruned == 0
            and resource_clean
        )
        status = "PASS" if passed else "FAIL"
    elif mode in REJECTED_MODES:
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and arrival_bound is not None
            and arrival_bound.status is NonFifoSearchStatus.GOAL_FOUND
            and adapter_error is not None
            and candidate is None
            and state_pruned == 0
            and heuristic_rejected == 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    else:
        expected_status = (
            NonFifoSearchStatus.CANCELLED
            if mode == "cancelled"
            else NonFifoSearchStatus.RESOURCE_LIMIT
        )
        passed = (
            not errors
            and candidate is not None
            and candidate.status is expected_status
            and state_pruned == 0
            and (candidate_diag is None or candidate_diag.dominance_pruned == 0)
            and resource_clean
        )
        status = "EXPECTED_CONTROL" if passed else "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile": profile_name,
        "objective": objective_name,
        "mode": mode,
        "baseline_status": None if baseline is None else baseline.status.value,
        "arrival_bound_status": None
        if arrival_bound is None
        else arrival_bound.status.value,
        "candidate_status": None if candidate is None else candidate.status.value,
        "adapter_error": adapter_error,
        "errors": errors,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "arrival_bound_semantic_digest": None
        if arrival_bound is None
        else arrival_bound.semantic_digest,
        "candidate_semantic_digest": None if candidate is None else candidate.semantic_digest,
        "reference_match": baseline_match and arrival_match and candidate_match,
        "baseline_match": baseline_match,
        "arrival_bound_match": arrival_match,
        "candidate_match": candidate_match,
        "baseline_route": baseline_route,
        "arrival_bound_route": arrival_route,
        "candidate_route": candidate_route,
        "reference_oracle": reference,
        "reference_oracle_kind": "independent-zero-heuristic-exact-arrival",
        "state_bound_certificate_digest": candidate_state_bound.digest,
        "state_bound_complete": candidate_state_bound.arrival_bound_complete,
        "heuristic_certificate_digest": heuristic.digest,
        "heuristic_usable": heuristic.usable,
        "topology_digest": topology.digest,
        "state_bound_pruned": state_pruned,
        "arrival_bound_pruned": arrival_pruned,
        "candidate_arrival_bound_pruned": candidate_arrival_pruned,
        "state_bound_rejected": state_rejected,
        "heuristic_rejected": heuristic_rejected,
        "dominance_pruned": 0 if candidate_diag is None else int(candidate_diag.dominance_pruned),
        "baseline_diagnostics": None
        if baseline is None
        else base._jsonable(baseline.diagnostics),
        "arrival_bound_diagnostics": arrival_diagnostics,
        "candidate_diagnostics": candidate_diagnostics,
        "compute_ms": (perf_counter() - started) * 1000.0,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "cpu": cpu,
        "reason": (
            None
            if status in {"PASS", "REJECTED_FAIL_CLOSED", "EXPECTED_CONTROL"}
            else "composed gate failed"
        ),
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
            "arrival_bound_pruned": 0,
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
            "arrival_bound_pruned": 0,
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
            "arrival_bound_pruned": 0,
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
        "state_bound_policy": "arrival-envelope-only",
        "heuristic_policy": "certified-ordering-only",
        "oracle": "independent-zero-heuristic-exact-arrival",
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(records: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    certified = [item for item in records if item.get("mode") == "certified"]
    rejected = [item for item in records if item.get("mode") in REJECTED_MODES]
    controls = [item for item in records if item.get("mode") in CONTROL_MODES]
    certified_pass = bool(certified) and all(item.get("status") == "PASS" for item in certified)
    fail_closed = bool(rejected) and all(
        item.get("status") == "REJECTED_FAIL_CLOSED"
        and int(item.get("state_bound_pruned", 0)) == 0
        and int(item.get("candidate_arrival_bound_pruned", 0)) == 0
        and int(item.get("dominance_pruned", 0)) == 0
        for item in rejected
    )
    controls_safe = bool(controls) and all(
        item.get("status") == "EXPECTED_CONTROL"
        and int(item.get("state_bound_pruned", 0)) == 0
        and int(item.get("dominance_pruned", 0)) == 0
        and int(item.get("candidate_arrival_bound_pruned", 0)) == 0
        for item in controls
    )
    groups: dict[tuple[str, str, str], set[tuple[Any, Any, Any]]] = {}
    for item in records:
        key = (str(item.get("profile")), str(item.get("objective")), str(item.get("mode")))
        groups.setdefault(key, set()).add(
            (
                item.get("baseline_semantic_digest"),
                item.get("arrival_bound_semantic_digest"),
                item.get("candidate_semantic_digest"),
            )
        )
    deterministic = bool(records) and all(len(values) == 1 for values in groups.values())
    status = (
        "TEMPORAL_COMPOSED_BOUND_MATRIX_PASS"
        if certified_pass
        and fail_closed
        and controls_safe
        and deterministic
        and sum(int(item.get("state_bound_pruned", 0)) for item in certified) > 0
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
        "control_case_count": len(controls),
        "certified_cases_pass": certified_pass,
        "fail_closed": fail_closed,
        "control_modes_safe": controls_safe,
        "deterministic": deterministic,
        "observed_state_bound_pruning": sum(
            int(item.get("state_bound_pruned", 0)) for item in certified
        ),
        "observed_arrival_bound_pruning": sum(
            int(item.get("arrival_bound_pruned", 0)) for item in certified
        ),
        "rejected_pruning_total": sum(
            int(item.get("state_bound_pruned", 0))
            + int(item.get("candidate_arrival_bound_pruned", 0))
            + int(item.get("dominance_pruned", 0))
            for item in rejected + controls
        ),
        "production_candidate_enabled": False,
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
        ("git", "-C", str(root), "status", "--porcelain"),
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError("composed-bound runner requires a clean implementation worktree")
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
        manifest,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "production_candidate_enabled": False,
        },
    )
    heartbeat = output / "heartbeat.json"
    cases_path = output / "cases.jsonl"
    frontier_path = output / "resource-frontier.jsonl"
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
    expected = len(PROFILES) * len(OBJECTIVES) * len(MODES) * args.repetitions
    _atomic_json(
        heartbeat,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "completed_cases": 0,
            "expected_cases": expected,
        },
    )
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
                        _append_jsonl(frontier_path, record)
                    records.append(record)
                    _atomic_json(
                        heartbeat,
                        {
                            "schema_version": SCHEMA_VERSION,
                            "status": "RUNNING",
                            "experiment_id": identity["experiment_id"],
                            "completed_cases": len(records),
                            "expected_cases": expected,
                        },
                    )
    summary = _summary(records, identity)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        manifest,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "production_candidate_enabled": False,
        },
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    _atomic_json(
        heartbeat,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "experiment_id": identity["experiment_id"],
            "completed_cases": len(records),
            "expected_cases": expected,
        },
    )
    return 0 if summary["status"] == "TEMPORAL_COMPOSED_BOUND_MATRIX_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
