#!/usr/bin/env python3
"""Synthetic proof matrix for the explicit non-FIFO state-bound adapter.

The runner is separate from the real-input adapter benchmark.  It exercises
the actual ``TemporalSession`` and edge evaluator on finite synthetic
profiles, compares both paths with the independent zero-heuristic oracle,
and requires certified cases to prune at least one newly generated label.
Scope-mismatched certificates are run as a fail-closed control and must prune
nothing.  This is a C-internal research artifact; it never changes the
production planner or enables a default policy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_bounds import TemporalStateBoundCertificate
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-adapter-bound.v1"
PROFILES = ("small", "medium", "stress")
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
MODES = ("certified", "scope_mismatch")
DEFAULT_REPETITIONS = 2
DEFAULT_TIMEOUT_SECONDS = 300.0
IMPLEMENTATION_FILES = (
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
        return {
            field: _jsonable(item)
            for field, item in asdict(value).items()
        }
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


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    base = _load_script("benchmark_temporal_dominance.py", "c_bound_resource_base")
    return base._resource_snapshot()


def _route_payload(result: Any, base: Any) -> dict[str, Any] | None:
    if result is None or result.planning_result is None:
        return None
    return base._route_payload(result)


def _nodes(planner: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    )


def _corridor_nodes(planner: Any) -> tuple[tuple[int, int], ...]:
    _, columns = planner.grid.shape
    return tuple(
        node
        for node in _nodes(planner)
        if node[0] == 0 or node[1] == columns - 1
    )


def _certificate(
    planner: Any,
    request: Any,
    profile: str,
    objective: str,
    *,
    scope_mismatch: bool = False,
) -> TemporalStateBoundCertificate:
    scope = planner.temporal_scope(request)
    if scope_mismatch:
        scope = TemporalScope.from_mapping(
            {**scope.mapping, "bound_scope_revision": "mismatch"}
        )
    nodes = _nodes(planner)
    allowed = _corridor_nodes(planner)
    excluded = tuple(node for node in nodes if node not in set(allowed))
    proof_digest = _digest(
        {
            "schema_version": SCHEMA_VERSION,
            "profile": profile,
            "objective": objective,
            "scope": scope.digest,
            "universe": nodes,
            "allowed": allowed,
            "excluded": excluded,
            "proof": "finite-corridor-v1",
        }
    )
    return TemporalStateBoundCertificate.certified(
        scope,
        allowed,
        excluded_nodes=excluded,
        proof_digest=proof_digest,
    )


def _semantic_matches(route: dict[str, Any] | None, reference: dict[str, Any]) -> bool:
    if route is None:
        return False
    return (
        route.get("nodes") == reference.get("nodes")
        and [step.get("eta") for step in route.get("steps", [])]
        == reference.get("arrival_times")
        and [step.get("incoming_heading_degrees") for step in route.get("steps", [])]
        == reference.get("headings")
        and abs(
            float(route.get("total_cost_hours", float("nan")))
            - float(reference.get("total_cost_hours", float("nan")))
        ) <= 1e-9
    )


def _worker(profile: str, objective_name: str, mode: str, cpu: int) -> dict[str, Any]:
    _set_cpu(cpu)
    base = _load_script("benchmark_temporal_dominance.py", "c_bound_synthetic_base")
    objective = ObjectiveMode(objective_name)
    baseline_planner, request, _ = base._build_components(
        profile,
        objective,
        with_dominance=False,
    )
    candidate_planner, _, _ = base._build_components(
        profile,
        objective,
        with_dominance=False,
    )
    certificate = _certificate(
        candidate_planner,
        request,
        profile,
        objective_name,
        scope_mismatch=mode == "scope_mismatch",
    )
    candidate_planner.state_bound_certificate = certificate
    before = _resource_snapshot()
    started = perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    bounded = None
    reference = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline is not None and baseline.planning_result is not None:
        try:
            reference = base._reference_solution(
                baseline_planner.grid,
                request,
                base.SYNTHETIC_PROFILES[profile],
                baseline_planner._cost_model(objective),
            )
        except Exception as error:  # pragma: no cover - worker boundary
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        bounded = run_non_fifo_temporal_bounded_search(
            candidate_planner,
            request,
            certificate,
        )
    except Exception as error:  # pragma: no cover - worker boundary
        errors["bounded"] = f"{type(error).__name__}: {error}"
    elapsed_ms = (perf_counter() - started) * 1000.0
    after = _resource_snapshot()
    baseline_route = _route_payload(baseline, base)
    bounded_route = _route_payload(bounded, base)
    baseline_match = (
        reference is not None and _semantic_matches(baseline_route, reference)
    )
    bounded_match = (
        reference is not None and _semantic_matches(bounded_route, reference)
    )
    diagnostics = None if bounded is None else _jsonable(bounded.diagnostics)
    pruned = 0 if bounded is None or bounded.diagnostics is None else int(
        bounded.diagnostics.state_bound_pruned
    )
    rejected = 0 if bounded is None or bounded.diagnostics is None else int(
        bounded.diagnostics.state_bound_rejected
    )
    resource_clean = (after.get("vmswap_kib") or 0) == 0
    if mode == "certified":
        passed = (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline_match
            and bounded_match
            and bounded.semantic_digest == baseline.semantic_digest
            and pruned > 0
            and rejected == 0
            and resource_clean
        )
        status = "PASS" if passed else "FAIL"
    else:
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
            and rejected > 0
            and resource_clean
        )
        status = "REJECTED_FAIL_CLOSED" if passed else "FAIL"
    reason = None
    if errors:
        reason = "; ".join(f"{key}={value}" for key, value in sorted(errors.items()))
    elif not baseline_match:
        reason = "baseline/reference semantic mismatch"
    elif mode == "certified" and not bounded_match:
        reason = "bounded/reference semantic mismatch"
    elif mode == "certified" and pruned == 0:
        reason = "certified bound observed no pruning"
    elif mode == "scope_mismatch" and pruned != 0:
        reason = "scope mismatch pruned labels"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "profile": profile,
        "objective": objective_name,
        "mode": mode,
        "semantic_match": baseline_match and (bounded_match if mode == "certified" else True),
        "reference_match": baseline_match and (bounded_match if mode == "certified" else True),
        "baseline_status": None if baseline is None else baseline.status.value,
        "bounded_status": None if bounded is None else bounded.status.value,
        "bounded_reason": None if bounded is None else bounded.reason,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "bounded_semantic_digest": None if bounded is None else bounded.semantic_digest,
        "reference": reference,
        "baseline_route": baseline_route,
        "bounded_route": bounded_route,
        "state_bound_policy": "certified" if mode == "certified" else "scope-mismatch",
        "state_bound_certificate_digest": certificate.digest,
        "state_bound_proof_digest": certificate.proof_digest,
        "state_bound_checks": 0 if bounded is None else int(bounded.diagnostics.state_bound_checks),
        "state_bound_pruned": pruned,
        "state_bound_rejected": rejected,
        "diagnostics": diagnostics,
        "compute_ms": elapsed_ms,
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
        }
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "profile": profile,
            "objective": objective,
            "mode": mode,
            "reason": f"worker JSON decode failed: {error}",
            "state_bound_pruned": 0,
        }
    return value


def _implementation_identity(root: Path) -> dict[str, str]:
    return {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}


def _identity(
    root: Path,
    profiles: tuple[str, ...],
    repetitions: int,
    timeout_seconds: float,
    cpu: int,
) -> dict[str, Any]:
    implementation = _implementation_identity(root)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "implementation": implementation,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "modes": MODES,
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "cpu": cpu,
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
    }
    payload["implementation_sha256"] = _digest(implementation)
    payload["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(payload)[:16]}"
    return payload


def _summary(records: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    certified = [item for item in records if item.get("mode") == "certified"]
    rejected = [item for item in records if item.get("mode") == "scope_mismatch"]
    certified_pass = bool(certified) and all(item.get("status") == "PASS" for item in certified)
    rejected_pass = bool(rejected) and all(
        item.get("status") == "REJECTED_FAIL_CLOSED" for item in rejected
    )
    digest_groups: dict[tuple[str, str, str], set[tuple[Any, Any]]] = {}
    for item in certified:
        key = (str(item.get("profile")), str(item.get("objective")), str(item.get("mode")))
        digest_groups.setdefault(key, set()).add(
            (item.get("baseline_semantic_digest"), item.get("bounded_semantic_digest"))
        )
    deterministic = bool(records) and all(
        item.get("deterministic_probe") is True for item in records
    ) and bool(digest_groups) and all(len(values) == 1 for values in digest_groups.values())
    fail_closed = bool(rejected) and all(
        int(item.get("state_bound_pruned", 0)) == 0 for item in rejected
    )
    summary_status = (
        "TEMPORAL_ADAPTER_STATE_BOUND_MATRIX_PASS"
        if certified_pass and rejected_pass and deterministic and fail_closed
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": summary_status,
        "experiment_id": identity["experiment_id"],
        "profiles": identity["profiles"],
        "objectives": identity["objectives"],
        "modes": identity["modes"],
        "case_count": len(records),
        "certified_case_count": len(certified),
        "scope_mismatch_case_count": len(rejected),
        "certified_cases_pass": certified_pass,
        "fail_closed": fail_closed,
        "deterministic": deterministic,
        "semantic_match": bool(records)
        and all(item.get("semantic_match") is True for item in certified),
        "observed_certified_pruning": sum(
            int(item.get("state_bound_pruned", 0)) for item in certified
        ),
        "rejected_pruning_total": sum(
            int(item.get("state_bound_pruned", 0)) for item in rejected
        ),
        "records": records,
        "production_candidate_enabled": False,
        "next_action": "keep default disabled; plan separate resource-bound review",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
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
    profiles = tuple(args.profiles)
    identity = _identity(
        root,
        profiles,
        args.repetitions,
        args.worker_timeout_seconds,
        args.cpu,
    )
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"),
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError("synthetic bound runner requires a clean implementation worktree")
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
            "state_bound_mode": "explicit-certified-only",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
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
    expected = len(profiles) * len(OBJECTIVES) * len(MODES) * args.repetitions
    for profile in profiles:
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
                    records.append(record)
                    _atomic_json(
                        heartbeat,
                        {
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "profile": profile,
                            "objective": objective,
                            "mode": mode,
                            "repetition": repetition,
                            "completed_cases": len(records),
                            "expected_cases": expected,
                        },
                    )
    summary = _summary(records, identity)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(output / "manifest.json", {
        "schema_version": SCHEMA_VERSION,
        "status": summary["status"],
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "summary": summary,
        "dominance_policy": "disabled",
        "state_bound_mode": "explicit-certified-only",
    })
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    _write_jsonl(output / "resource-frontier.jsonl", records)
    _write_jsonl(output / "fifo-scan.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _write_jsonl(output / "eta-interval.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _write_text(output / "ALL_DONE", summary["status"] + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "TEMPORAL_ADAPTER_STATE_BOUND_MATRIX_PASS" else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
