#!/usr/bin/env python3
"""Synthetic proof matrix for the explicit arrival-aware state bound.

This runner is deliberately separate from the M5 node-corridor runner.  It
uses the real finite temporal session, an independent exact-arrival oracle and
the explicit arrival-bounded adapter.  A complete per-node upper envelope may
discard only newly generated labels that are provably too late to reach the
goal.  Scope-mismatch and incomplete-envelope modes must fail closed without
arrival pruning.  No production planner or default policy is changed.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.cost import KNOT_TO_KM_PER_HOUR
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    NonFifoTemporalAdapterError,
    run_non_fifo_temporal_arrival_bounded_search,
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-arrival-bound.v1"
PROFILES = ("small", "medium", "stress")
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
MODES = ("certified", "scope_mismatch", "incomplete")
DEFAULT_REPETITIONS = 2
DEFAULT_TIMEOUT_SECONDS = 300.0
BOUND_METHOD = "synthetic-max-speed-arrival-envelope-v1"
BOUND_EVALUATOR = "certified:synthetic-distance-max-speed-v1"
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_arrival_bound.py",
    "scripts/benchmark_non_fifo_temporal_bound.py",
    "scripts/benchmark_temporal_dominance.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
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
    if is_dataclass(value):
        return {field: _jsonable(item) for field, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    base = _load_script("benchmark_temporal_dominance.py", "c_m6_resource_base")
    return base._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if (after.get("process_swap_kib") or 0) != 0:
        return False
    before_host = before.get("host_swap_pages") or {}
    after_host = after.get("host_swap_pages") or {}
    if (
        before_host
        and after_host
        and any(after_host.get(key, 0) != before_host.get(key, 0) for key in before_host)
    ):
        return False
    affinity = after.get("cpu_affinity")
    return isinstance(affinity, list) and len(affinity) == 1


def _nodes(planner: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )


def _certificate(
    planner: Any,
    request: Any,
    profile: str,
    objective: str,
    *,
    mode: str,
) -> Any:
    scope = planner.temporal_scope(request)
    if mode == "scope_mismatch":
        scope = TemporalScope.from_mapping({**scope.mapping, "arrival_bound_revision": "mismatch"})
    nodes = _nodes(planner)
    max_speed = planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR
    forward = {node: planner.grid.distance_km(request.start, node) / max_speed for node in nodes}
    reverse = {node: planner.grid.distance_km(node, request.goal) / max_speed for node in nodes}
    horizon = request.maximum_elapsed.total_seconds() / 3600.0
    evidence = AdmissibleBoundEvidence(
        scope=scope,
        method=BOUND_METHOD,
        evaluator_digest=BOUND_EVALUATOR,
        proof_digest=_digest(
            {
                "profile": profile,
                "objective": objective,
                "scope": scope.digest,
                "nodes": nodes,
                "forward": forward,
                "reverse": reverse,
                "horizon": horizon,
                "limits": LIMITS,
            }
        ),
        admissible=True,
        coverage_complete=True,
    )
    corridor = derive_temporal_corridor(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon,
        objective=objective,
        bound_evidence=evidence,
        include_arrival_upper_bounds=True,
    )
    if not corridor.certificate.usable or not corridor.certificate.arrival_bound_complete:
        raise RuntimeError("synthetic arrival corridor did not produce a complete certificate")
    if mode == "incomplete":
        return replace(
            corridor.certificate,
            arrival_upper_hours=corridor.certificate.arrival_upper_hours[:-1],
        )
    return corridor.certificate


def _route_payload(result: Any, base: Any) -> dict[str, Any] | None:
    if result is None or result.planning_result is None:
        return None
    return base._route_payload(result)


def _semantic_matches(route: dict[str, Any] | None, reference: dict[str, Any]) -> bool:
    if route is None:
        return False
    return (
        route.get("nodes") == reference.get("nodes")
        and [step.get("eta") for step in route.get("steps", [])] == reference.get("arrival_times")
        and [step.get("incoming_heading_degrees") for step in route.get("steps", [])]
        == reference.get("headings")
        and abs(
            float(route.get("total_cost_hours", float("nan")))
            - float(reference.get("total_cost_hours", float("nan")))
        )
        <= 1e-9
    )


def _worker(profile_name: str, objective_name: str, mode: str, cpu: int) -> dict[str, Any]:
    _set_cpu(cpu)
    base = _load_script("benchmark_temporal_dominance.py", "c_m6_synthetic_base")
    objective = ObjectiveMode(objective_name)
    baseline_planner, request, _ = base._build_components(
        profile_name,
        objective,
        with_dominance=False,
    )
    candidate_planner, _, _ = base._build_components(
        profile_name,
        objective,
        with_dominance=False,
    )
    certificate = _certificate(
        candidate_planner,
        request,
        profile_name,
        objective_name,
        mode=mode,
    )
    candidate_planner.state_bound_certificate = certificate
    before = _resource_snapshot()
    started = perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    bounded = None
    reference = None
    adapter_error = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline is not None and baseline.planning_result is not None:
        try:
            reference = base._reference_solution(
                baseline_planner.grid,
                request,
                base.SYNTHETIC_PROFILES[profile_name],
                baseline_planner._cost_model(objective),
            )
        except Exception as error:  # pragma: no cover - evidence boundary
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        if mode == "incomplete":
            try:
                run_non_fifo_temporal_arrival_bounded_search(
                    candidate_planner,
                    request,
                    certificate,
                )
            except NonFifoTemporalAdapterError as error:
                adapter_error = f"{type(error).__name__}: {error}"
            bounded = run_non_fifo_temporal_bounded_search(
                candidate_planner,
                request,
                certificate,
            )
        else:
            bounded = run_non_fifo_temporal_arrival_bounded_search(
                candidate_planner,
                request,
                certificate,
            )
    except Exception as error:  # pragma: no cover - worker boundary
        errors["bounded"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_route = _route_payload(baseline, base)
    bounded_route = _route_payload(bounded, base)
    baseline_match = reference is not None and _semantic_matches(baseline_route, reference)
    bounded_match = reference is not None and _semantic_matches(bounded_route, reference)
    diagnostics = None if bounded is None else _jsonable(bounded.diagnostics)
    pruned = (
        0
        if bounded is None or bounded.diagnostics is None
        else int(bounded.diagnostics.state_bound_pruned)
    )
    arrival_pruned = (
        0
        if bounded is None or bounded.diagnostics is None
        else int(bounded.diagnostics.state_bound_arrival_pruned)
    )
    rejected = (
        0
        if bounded is None or bounded.diagnostics is None
        else int(bounded.diagnostics.state_bound_rejected)
    )
    resource_clean = _resource_clean(before, after)
    semantic_match = baseline_match and (
        bounded_match and baseline.semantic_digest == bounded.semantic_digest
        if mode == "certified"
        else bounded_match
        if mode == "incomplete"
        else True
    )
    if mode == "certified":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.GOAL_FOUND
            and semantic_match
            and arrival_pruned > 0
            and rejected == 0
            and resource_clean
        )
        status = "PASS" if passed else "FAIL"
    elif mode == "scope_mismatch":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.EVALUATOR_FAILURE
            and bounded.reason == "state_bound_rejected"
            and bounded_route is None
            and pruned == 0
            and arrival_pruned == 0
            and rejected > 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    else:
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
    reason = (
        None
        if status in {"PASS", "REJECTED_FAIL_CLOSED"}
        else (
            "; ".join(f"{key}={value}" for key, value in sorted(errors.items()))
            or "arrival-bound semantic/fail-closed/resource gate failed"
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile": profile_name,
        "objective": objective_name,
        "mode": mode,
        "semantic_match": semantic_match,
        "reference_match": baseline_match and (bounded_match if mode != "scope_mismatch" else True),
        "baseline_status": None if baseline is None else baseline.status.value,
        "bounded_status": None if bounded is None else bounded.status.value,
        "bounded_reason": None if bounded is None else bounded.reason,
        "adapter_error": adapter_error,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "bounded_semantic_digest": None if bounded is None else bounded.semantic_digest,
        "baseline_route": baseline_route,
        "bounded_route": bounded_route,
        "reference": reference,
        "state_bound_policy": "arrival-envelope" if mode == "certified" else mode,
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
        "deterministic_probe": True,
        "reason": reason,
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
            "status": "ERROR",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
            "state_bound_pruned": 0,
            "state_bound_arrival_pruned": 0,
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": f"worker JSON decode failed: {error}",
            "state_bound_pruned": 0,
            "state_bound_arrival_pruned": 0,
        }


def _identity(root: Path, repetitions: int, timeout_seconds: float, cpu: int) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "implementation": implementation,
        "profiles": PROFILES,
        "objectives": OBJECTIVES,
        "modes": MODES,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "cpu": cpu,
        "search_limits": LIMITS,
    }
    payload["implementation_sha256"] = _digest(implementation)
    payload["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(payload)[:16]}"
    return payload


def _summary(records: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    certified = [item for item in records if item.get("mode") == "certified"]
    rejected = [item for item in records if item.get("mode") in {"scope_mismatch", "incomplete"}]
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
        for item in certified + [item for item in rejected if item.get("mode") == "incomplete"]
    )
    status = (
        "TEMPORAL_ARRIVAL_BOUND_MATRIX_PASS"
        if certified_pass and fail_closed and deterministic and semantic
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "profiles": identity["profiles"],
        "objectives": identity["objectives"],
        "modes": identity["modes"],
        "case_count": len(records),
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
        "records": records,
        "production_candidate_enabled": False,
        "next_action": "keep default disabled; review arrival-envelope resource evidence",
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
        ("git", "-C", str(root), "status", "--porcelain"),
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError(
            "synthetic arrival-bound runner requires a clean implementation worktree"
        )
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity mismatch")
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "dominance_policy": "disabled",
            "state_bound_mode": "explicit-arrival-envelope-only",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
    frontier_path = output / "resource-frontier.jsonl"
    existing = {
        (
            item.get("profile"),
            item.get("objective"),
            item.get("mode"),
            int(item.get("repetition", -1)),
        ): item
        for item in _read_jsonl(cases_path)
        if isinstance(item.get("repetition"), int)
    }
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
                            frontier_path,
                            {
                                "profile": profile,
                                "objective": objective,
                                "mode": mode,
                                "repetition": repetition,
                                "state_bound_checks": record.get("state_bound_checks", 0),
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
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(records),
                            "expected_cases": (
                                len(PROFILES) * len(OBJECTIVES) * len(MODES) * args.repetitions
                            ),
                        },
                    )
    summary = _summary(records, identity)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
        },
    )
    _atomic_json(output / "fifo-scan.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
    _atomic_json(output / "eta-interval.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
    marker = (
        "ALL_DONE" if summary["status"] == "TEMPORAL_ARRIVAL_BOUND_MATRIX_PASS" else "STOPPED_HARD"
    )
    _atomic_json(
        output / marker,
        {"status": summary["status"], "experiment_id": identity["experiment_id"]},
    )
    return 0 if summary["status"] == "TEMPORAL_ARRIVAL_BOUND_MATRIX_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
