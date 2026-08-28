#!/usr/bin/env python3
"""Test-only real-input replay for a certified temporal corridor.

The runner is intentionally separate from the projection runner.  It builds a
scope-matched ``TemporalStateBoundCertificate`` from the finite-grid maximum
speed proof, runs baseline/reference/candidate in one process at a time, and
compares every candidate business field with the baseline and the independent
zero-heuristic reference.  The certificate is injected only into this
research runner; the production planner and ingress remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.cost.vessel import KNOT_TO_KM_PER_HOUR
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)

SCHEMA_VERSION = "c.p0.1-temporal-corridor-pruning-real.v1"
SEGMENTS = {"executable_0_6h": timedelta(hours=6)}
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
SEARCH_LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
BOUND_METHOD = "geodesic-max-effective-speed-v1"
BOUND_EVALUATOR_DIGEST = "certified:geodesic-max-effective-speed-v1"
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_corridor_real.py",
    "scripts/benchmark_temporal_corridor_pruning_real.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/cost/model.py",
    "src/arctic_route_planning/cost/vessel.py",
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
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


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


def _resource_snapshot(module: Any) -> dict[str, Any]:
    return module._resource_snapshot()


def _complete_grid_nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    """Return the full finite grid used by the projection certificate.

    The dominance real runner intentionally exposes only its qualification
    helpers and does not define a projection-specific ``_nodes`` function.
    Test-only pruning must nevertheless use the same complete universe as the
    projection runner, including nodes that are blocked at departure but may
    become available in later frames.
    """

    return tuple(
        (row, column)
        for row in range(fixture.grid.shape[0])
        for column in range(fixture.grid.shape[1])
    )


def _build_certificate(
    module: Any, fixture: Any, objective_name: str
) -> tuple[Any, Any, Any, tuple[Any, ...]]:
    objective = ObjectiveMode(objective_name)
    planner = module._build_planner(fixture, objective)
    request = module._request(fixture, objective)
    nodes = _complete_grid_nodes(fixture)
    scope = planner.temporal_scope(request)
    max_speed = planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR
    forward = {node: fixture.grid.distance_km(fixture.start, node) / max_speed for node in nodes}
    reverse = {node: fixture.grid.distance_km(node, fixture.goal) / max_speed for node in nodes}
    horizon = SEGMENTS[fixture.segment].total_seconds() / 3600.0
    evidence = AdmissibleBoundEvidence(
        scope=scope,
        method=BOUND_METHOD,
        evaluator_digest=BOUND_EVALUATOR_DIGEST,
        proof_digest=module._canonical_digest(
            {
                "method": BOUND_METHOD,
                "max_speed_km_per_hour": max_speed,
                "scope": scope.digest,
                "universe": nodes,
                "forward": forward,
                "reverse": reverse,
                "horizon_hours": horizon,
                "search_limits": SEARCH_LIMITS,
            }
        ),
        admissible=True,
        coverage_complete=True,
    )
    corridor = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon,
        objective=objective_name,
        bound_evidence=evidence,
        generated_nodes=nodes,
    )
    return planner, request, corridor, nodes


def _diagnostics(result: Any) -> dict[str, Any] | None:
    return None if result is None else _jsonable(result.diagnostics)


def _run_objective(
    base: Any, corridor_module: Any, fixture: Any, objective_name: str, cpu: int
) -> dict[str, Any]:
    baseline_planner, request, corridor, nodes = _build_certificate(base, fixture, objective_name)
    candidate_planner = base._build_planner(fixture, ObjectiveMode(objective_name))
    candidate_planner.state_bound_certificate = corridor.certificate
    if not corridor.certificate.usable:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "mode": "test-only-pruning",
            "input": fixture.input_name,
            "segment": fixture.segment,
            "objective": objective_name,
            "dominance_policy": "disabled",
            "state_bound_authorized": False,
            "state_bound_pruned": 0,
            "reason": corridor.reason or "corridor certificate unusable",
            "semantic_match": False,
            "deterministic": False,
        }
    started = time.perf_counter()
    before = _resource_snapshot(corridor_module)
    baseline_result = None
    candidate_result = None
    candidate_repeat = None
    reference = None
    errors: dict[str, str] = {}
    try:
        baseline_result = baseline_planner.plan(request)
    except Exception as error:
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline_result is not None:
        try:
            reference = base._reference_search(baseline_planner, request)
        except Exception as error:
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        candidate_result = candidate_planner.plan(request)
    except Exception as error:
        errors["candidate"] = f"{type(error).__name__}: {error}"
    if candidate_result is not None:
        try:
            repeat_planner = base._build_planner(fixture, ObjectiveMode(objective_name))
            repeat_planner.state_bound_certificate = corridor.certificate
            candidate_repeat = repeat_planner.plan(request)
        except Exception as error:
            errors["candidate_repeat"] = f"{type(error).__name__}: {error}"
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    after = _resource_snapshot(corridor_module)
    baseline_semantic = None if baseline_result is None else base._route_semantic(baseline_result)
    candidate_semantic = (
        None if candidate_result is None else base._route_semantic(candidate_result)
    )
    repeat_semantic = None if candidate_repeat is None else base._route_semantic(candidate_repeat)
    baseline_match = (
        baseline_semantic is not None
        and reference is not None
        and base._reference_matches(baseline_semantic, reference)
    )
    candidate_match = (
        candidate_semantic is not None
        and reference is not None
        and base._reference_matches(candidate_semantic, reference)
    )
    repeat_match = candidate_semantic is not None and repeat_semantic == candidate_semantic
    candidate_diagnostics = _diagnostics(candidate_result)
    state_bound_pruned = (
        0
        if candidate_result is None
        else int(getattr(candidate_result.diagnostics, "state_bound_pruned", 0))
    )
    authorized = candidate_result is not None and bool(
        getattr(candidate_result.diagnostics, "state_bound_rejected", 1) == 0
    )
    semantic_match = baseline_match and candidate_match and repeat_match
    resource_clean = (after.get("vmswap_kib") or 0) == 0
    status = "PASS" if semantic_match and authorized and resource_clean else "FAIL"
    reason = None
    if errors:
        reason = "; ".join(f"{key}={value}" for key, value in sorted(errors.items()))
    elif not baseline_match:
        reason = "baseline/reference semantic mismatch"
    elif not candidate_match:
        reason = "candidate/reference semantic mismatch"
    elif not repeat_match:
        reason = "candidate is non-deterministic"
    elif not authorized:
        reason = "state-bound certificate was rejected"
    elif not resource_clean:
        reason = "swap observed"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "mode": "test-only-pruning",
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective_name,
        "dominance_policy": "disabled",
        "state_bound_authorized": authorized,
        "state_bound_certificate_digest": corridor.certificate.digest,
        "state_bound_proof_digest": corridor.proof_digest,
        "state_bound_checks": 0
        if candidate_result is None
        else int(getattr(candidate_result.diagnostics, "state_bound_checks", 0)),
        "state_bound_pruned": state_bound_pruned,
        "state_bound_rejected": 0
        if candidate_result is None
        else int(getattr(candidate_result.diagnostics, "state_bound_rejected", 0)),
        "projected_label_reduction": corridor.projected_label_reduction,
        "universe_count": len(nodes),
        "allowed_count": corridor.allowed_count,
        "excluded_count": corridor.excluded_count,
        "baseline_semantic_digest": None
        if baseline_semantic is None
        else base._canonical_digest(baseline_semantic),
        "candidate_semantic_digest": None
        if candidate_semantic is None
        else base._canonical_digest(candidate_semantic),
        "reference_match": baseline_match and candidate_match,
        "semantic_match": semantic_match,
        "reference": reference,
        "baseline_diagnostics": _diagnostics(baseline_result),
        "candidate_diagnostics": candidate_diagnostics,
        "compute_ms": elapsed_ms,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "cpu": cpu,
        "deterministic": repeat_match,
        "reason": reason,
        "planner_default_unchanged": True,
    }


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("real corridor test-only pruning timeout")


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": implementation,
        "implementation_sha256": hashlib.sha256(
            json.dumps(implementation, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "config_root_sha256": _tree_digest(fixture.config_root),
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
        },
        "route_plan_set_sha256": _sha256(fixture.route_plan_path),
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "objectives": OBJECTIVES,
        "mode": args.mode,
        "dominance_policy": "disabled",
        "bound_method": BOUND_METHOD,
        "bound_evaluator_digest": BOUND_EVALUATOR_DIGEST,
        "search_limits": SEARCH_LIMITS,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
    }
    identity["fixture_digest"] = hashlib.sha256(
        json.dumps(
            {
                "input": identity["input"],
                "risk_window": identity["risk_window"],
                "route_plan_set_sha256": identity["route_plan_set_sha256"],
                "config_root_sha256": identity["config_root_sha256"],
            },
            default=str,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    identity_digest = hashlib.sha256(
        json.dumps(identity, default=str, sort_keys=True).encode()
    ).hexdigest()[:16]
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{identity_digest}"
    return identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("test-only-pruning",), default="test-only-pruning")
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--objective", choices=("all", *OBJECTIVES), default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.worker_timeout_seconds <= 0.0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    _set_cpu_affinity(args.cpu)
    base = _load_script("benchmark_temporal_dominance_real.py", "c_temporal_pruning_base")
    corridor_module = _load_script(
        "benchmark_temporal_corridor_real.py", "c_temporal_pruning_corridor"
    )
    fixture = base._load_fixture(args)
    identity = _identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("real corridor pruning requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "dominance_policy": "disabled",
            "state_bound_mode": "test-only",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
    existing = {
        record.get("objective"): record
        for record in _read_jsonl(cases_path)
        if isinstance(record.get("objective"), str)
    }
    objectives = OBJECTIVES if args.objective == "all" else (args.objective,)
    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    records: list[dict[str, Any]] = []
    try:
        for objective in objectives:
            record = existing.get(objective)
            if record is None:
                try:
                    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
                    record = _run_objective(base, corridor_module, fixture, objective, args.cpu)
                except _WorkerTimeout as error:
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "TIMEOUT",
                        "mode": "test-only-pruning",
                        "input": fixture.input_name,
                        "segment": fixture.segment,
                        "objective": objective,
                        "dominance_policy": "disabled",
                        "state_bound_pruned": 0,
                        "semantic_match": False,
                        "deterministic": False,
                        "reason": str(error),
                    }
                except Exception as error:  # objective isolation is deliberate
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "ERROR",
                        "mode": "test-only-pruning",
                        "input": fixture.input_name,
                        "segment": fixture.segment,
                        "objective": objective,
                        "dominance_policy": "disabled",
                        "state_bound_pruned": 0,
                        "semantic_match": False,
                        "deterministic": False,
                        "reason": f"{type(error).__name__}: {error}",
                    }
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                record["experiment_id"] = identity["experiment_id"]
                _append_jsonl(cases_path, record)
            records.append(record)
            _atomic_json(
                heartbeat,
                {
                    "status": "RUNNING",
                    "updated_at": datetime.now(UTC),
                    "objective": objective,
                    "completed_objectives": len(records),
                    "expected_objectives": len(objectives),
                },
            )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    passed = bool(records) and all(
        record.get("status") == "PASS"
        and record.get("semantic_match") is True
        and record.get("deterministic") is True
        and record.get("state_bound_pruned", 0) > 0
        and record.get("resource_clean") is True
        for record in records
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "REAL_CORRIDOR_TEST_ONLY_PRUNING_PASS" if passed else "NO_PERFORMANCE_PROOF/FAIL",
        "reason": (
            "all test-only candidate replays match baseline/reference and observe certified pruning"
            if passed
            else "semantic, resource, determinism, or actual-pruning gate failed"
        ),
        "experiment_id": identity["experiment_id"],
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objectives": objectives,
        "dominance_policy": "disabled",
        "state_bound_mode": "test-only",
        "production_candidate_enabled": False,
        "observed_label_pruning": sum(
            int(record.get("state_bound_pruned", 0)) for record in records
        ),
        "objective_summaries": records,
        "semantic_match": bool(records)
        and all(record.get("semantic_match") is True for record in records),
        "deterministic": bool(records)
        and all(record.get("deterministic") is True for record in records),
        "resource_clean": bool(records)
        and all(record.get("resource_clean") is True for record in records),
        "next_action": "keep candidate disabled; plan separate broader corridor study",
    }
    _write_jsonl(output / "resource-frontier.jsonl", records)
    _write_jsonl(output / "fifo-scan.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _write_jsonl(output / "eta-interval.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
            "dominance_policy": "disabled",
            "state_bound_mode": "test-only",
        },
    )
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
