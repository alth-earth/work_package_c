#!/usr/bin/env python3
"""Real-input diagnostic for proof-carrying environmental speed bounds.

The runner reuses the audited M31 fixture loader and compares the existing
maximum-speed edge envelope with a candidate whose edge lower bounds are
derived from complete RiskFrame interval speed upper bounds.  The candidate
uses only the explicitly certified edge pairs; uncertain pairs remain live.
This is a C-internal diagnostic, with temporal dominance and production
candidate paths disabled.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.temporal_corridor import derive_temporal_corridor
from arctic_route_planning.planners.temporal_environment_envelope import (
    qualify_environmental_speed_envelope,
)
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-environment-speed-envelope-real.v1"
SEGMENTS = {"executable_0_6h": 6.0, "rolling_0_24h": 24.0}
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
SEARCH_LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_edge_envelope_real.py",
    "scripts/benchmark_temporal_environment_envelope_real.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_environment_envelope.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "uv.lock",
)


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("real environmental speed-envelope diagnostic timeout")


def _load_base() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_edge_envelope_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_environment_real_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load audited real-input fixture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
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
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
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
        "dirty": bool(run("status", "--porcelain")),
    }


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


def _nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(fixture.grid.shape[0])
        for column in range(fixture.grid.shape[1])
    )


def _corridor(
    *,
    scope: Any,
    expected_scope: Any,
    nodes: tuple[Any, ...],
    start: Any,
    goal: Any,
    neighbors: Any,
    topology: Any,
    horizon_hours: float,
    objective: str,
    edge_lower_hours: Any,
    edge_bound_complete: bool = False,
    edge_bound_partial: bool = False,
) -> Any:
    return derive_temporal_corridor(
        scope=scope,
        expected_scope=expected_scope,
        universe_nodes=nodes,
        start=start,
        goal=goal,
        neighbors=neighbors,
        forward_lower_hours=topology.forward_map,
        reverse_lower_hours=topology.reverse_map,
        horizon_hours=horizon_hours,
        objective=objective,
        bound_evidence=topology.as_admissible_bound_evidence(),
        include_arrival_upper_bounds=True,
        edge_lower_hours=edge_lower_hours,
        edge_bound_complete=edge_bound_complete,
        edge_bound_partial=edge_bound_partial,
        generated_nodes=nodes,
    )


def _build_certificates(
    base: Any,
    fixture: Any,
    objective_name: str,
) -> tuple[Any, Any, Any, Any, Any, Any]:
    objective = ObjectiveMode(objective_name)
    probe = base._build_planner(fixture, objective)
    request = base._request(fixture, objective)
    nodes = _nodes(fixture)
    scope = probe.temporal_scope(request)
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        neighbors=probe.grid.neighbors,
        edge_distance_km=probe.grid.distance_km,
        max_speed_km_per_hour=probe.vessel_model.maximum_speed_knots * 1.852,
    )
    if not topology.usable:
        raise RuntimeError(f"topological lower-bound evidence rejected: {topology.reason}")
    horizon = SEGMENTS[fixture.segment]
    baseline_corridor = _corridor(
        scope=scope,
        expected_scope=scope,
        nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        neighbors=probe.grid.neighbors,
        topology=topology,
        horizon_hours=horizon,
        objective=objective_name,
        edge_lower_hours=topology.edge_lower_map,
        edge_bound_complete=True,
    )
    edges: list[tuple[Any, Any, float, tuple[Any, ...]]] = []
    for node in nodes:
        for neighbor in probe.grid.neighbors(node):
            distance, _heading, points = probe._edge_geometry(
                node,
                neighbor,
                minimum_samples=request.edge_sample_count,
            )
            edges.append((node, neighbor, distance, tuple(points)))
    environmental = qualify_environmental_speed_envelope(
        risk_sampler=probe.risk_sampler,
        vessel_model=probe.vessel_model,
        scope=scope,
        expected_scope=scope,
        departure_lower=request.departure_time,
        horizon_hours=horizon,
        edges=edges,
        universe_nodes=nodes,
        evaluator_certified=True,
    )
    candidate_corridor = _corridor(
        scope=scope,
        expected_scope=scope,
        nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        neighbors=probe.grid.neighbors,
        topology=topology,
        horizon_hours=horizon,
        objective=objective_name,
        edge_lower_hours=environmental.edge_lower_map,
        edge_bound_complete=environmental.coverage_complete,
        edge_bound_partial=environmental.partial,
    )
    return (
        request,
        topology,
        baseline_corridor,
        environmental,
        candidate_corridor,
        nodes,
    )


def _case(base: Any, fixture: Any, objective_name: str, cpu: int) -> dict[str, Any]:
    request, topology, baseline_corridor, environmental, candidate_corridor, nodes = (
        _build_certificates(base, fixture, objective_name)
    )
    objective = ObjectiveMode(objective_name)
    baseline = base._build_planner(fixture, objective)
    baseline.state_bound_certificate = baseline_corridor.certificate
    candidate = base._build_planner(fixture, objective)
    candidate.state_bound_certificate = candidate_corridor.certificate
    before = base._resource_snapshot()
    started = time.perf_counter()
    errors: dict[str, str] = {}
    baseline_result = candidate_result = candidate_repeat = reference = None
    try:
        baseline_result = baseline.plan(request)
    except Exception as error:
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline_result is not None:
        try:
            reference = base._reference_search(baseline, request)
        except Exception as error:
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        candidate_result = candidate.plan(request)
    except Exception as error:
        errors["candidate"] = f"{type(error).__name__}: {error}"
    if candidate_result is not None:
        try:
            repeat = base._build_planner(fixture, objective)
            repeat.state_bound_certificate = candidate_corridor.certificate
            candidate_repeat = repeat.plan(request)
        except Exception as error:
            errors["candidate_repeat"] = f"{type(error).__name__}: {error}"
    after = base._resource_snapshot()
    baseline_semantic = None if baseline_result is None else base._route_semantic(baseline_result)
    candidate_semantic = (
        None if candidate_result is None else base._route_semantic(candidate_result)
    )
    repeat_semantic = None if candidate_repeat is None else base._route_semantic(candidate_repeat)
    baseline_match = bool(
        baseline_semantic is not None
        and reference is not None
        and base._reference_matches(baseline_semantic, reference)
    )
    candidate_match = bool(
        candidate_semantic is not None
        and reference is not None
        and base._reference_matches(candidate_semantic, reference)
    )
    deterministic = candidate_semantic is not None and candidate_semantic == repeat_semantic
    candidate_diag = None if candidate_result is None else candidate_result.diagnostics
    baseline_diag = None if baseline_result is None else baseline_result.diagnostics
    resource_clean = base._resource_clean(before, after)
    semantic_match = baseline_match and candidate_match and deterministic
    edge_pruned = 0 if candidate_diag is None else int(candidate_diag.state_bound_edge_pruned)
    edge_evaluations = 0 if candidate_diag is None else int(candidate_diag.edge_evaluations)
    baseline_edge_evaluations = 0 if baseline_diag is None else int(baseline_diag.edge_evaluations)
    status = "PASS" if semantic_match and resource_clean else "FAIL"
    speed_upper_values = [
        item.speed_factor_upper
        for item in environmental.edge_evidence
        if item.speed_factor_upper is not None
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "mode": "resource-frontier-environment-speed-envelope",
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective_name,
        "dominance_policy": "disabled",
        "state_bound_policy": "proof-carrying-environmental-speed-envelope",
        "state_bound_authorized": bool(candidate_corridor.certificate.usable),
        "state_bound_certificate_digest": candidate_corridor.certificate.digest,
        "state_bound_proof_digest": candidate_corridor.proof_digest,
        "topological_bound_digest": topology.digest,
        "environmental_envelope_status": environmental.status,
        "environmental_envelope_reason": environmental.reason,
        "environmental_envelope_digest": environmental.digest,
        "environmental_envelope_proof_digest": environmental.proof_digest,
        "environmental_evaluator_digest": environmental.evaluator_digest,
        "environmental_expected_edges": environmental.expected_edge_count,
        "environmental_covered_edges": environmental.covered_edge_count,
        "environmental_coverage_complete": environmental.coverage_complete,
        "environmental_edge_bound_partial": environmental.partial,
        "environmental_speed_factor_upper_min": min(speed_upper_values)
        if speed_upper_values
        else None,
        "environmental_speed_factor_upper_max": max(speed_upper_values)
        if speed_upper_values
        else None,
        "edge_bound_digest": candidate_corridor.certificate.edge_bound_digest,
        "edge_bound_count": len(candidate_corridor.edge_lower_hours),
        "state_bound_edge_checks": 0
        if candidate_diag is None
        else int(candidate_diag.state_bound_edge_checks),
        "state_bound_edge_pruned": edge_pruned,
        "edge_evaluations": edge_evaluations,
        "baseline_edge_evaluations": baseline_edge_evaluations,
        "state_bound_pruned": 0
        if candidate_diag is None
        else int(candidate_diag.state_bound_pruned),
        "baseline_semantic_digest": None
        if baseline_semantic is None
        else _digest(baseline_semantic),
        "candidate_semantic_digest": None
        if candidate_semantic is None
        else _digest(candidate_semantic),
        "reference_match": baseline_match and candidate_match,
        "semantic_match": semantic_match,
        "deterministic": deterministic,
        "reference": reference,
        "baseline_diagnostics": None if baseline_diag is None else _jsonable(baseline_diag),
        "candidate_diagnostics": None if candidate_diag is None else _jsonable(candidate_diag),
        "compute_ms": None
        if candidate_result is None
        else candidate_result.planning_result.metrics.compute_ms,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": False,
        "cpu": cpu,
        "errors": errors,
        "planner_default_unchanged": True,
        "production_candidate_enabled": False,
        "universe_count": len(nodes),
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
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
        "dominance_policy": "disabled",
        "state_bound_policy": "proof-carrying-environmental-speed-envelope",
        "search_limits": SEARCH_LIMITS,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
    }
    identity["fixture_digest"] = _digest(
        {
            "input": identity["input"],
            "risk_window": identity["risk_window"],
            "route_plan_set_sha256": identity["route_plan_set_sha256"],
            "config_root_sha256": identity["config_root_sha256"],
        }
    )
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--objective", choices=("all", *OBJECTIVES), default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.worker_timeout_seconds <= 0.0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    base = _load_base()
    _set_cpu(args.cpu)
    fixture = base._load_fixture(args)
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError(
            "real environmental speed-envelope diagnostic requires a clean implementation worktree"
        )
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
            "state_bound_policy": "proof-carrying-environmental-speed-envelope",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
    existing = {record.get("objective"): record for record in _read_jsonl(cases_path)}
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
                    record = _case(base, fixture, objective, args.cpu)
                except _WorkerTimeout as error:
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "TIMEOUT",
                        "objective": objective,
                        "reason": str(error),
                        "semantic_match": False,
                        "deterministic": False,
                    }
                except Exception as error:  # one objective must not block others
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "ERROR",
                        "objective": objective,
                        "reason": f"{type(error).__name__}: {error}",
                        "semantic_match": False,
                        "deterministic": False,
                    }
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                record.update(
                    {
                        "experiment_id": identity["experiment_id"],
                        "input": fixture.input_name,
                        "segment": fixture.segment,
                    }
                )
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
    semantic_ok = bool(records) and all(
        record.get("semantic_match") is True and record.get("deterministic") is True
        for record in records
    )
    resource_ok = bool(records) and all(record.get("resource_clean") is True for record in records)
    status = (
        "REAL_ENVIRONMENT_SPEED_ENVELOPE_SEMANTIC_PASS"
        if semantic_ok and resource_ok
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objectives": objectives,
        "dominance_policy": "disabled",
        "state_bound_policy": "proof-carrying-environmental-speed-envelope",
        "production_candidate_enabled": False,
        "semantic_match": semantic_ok,
        "deterministic": bool(records)
        and all(record.get("deterministic") is True for record in records),
        "resource_clean": resource_ok,
        "resource_evidence_complete": False,
        "observed_edge_pruning": sum(
            int(record.get("state_bound_edge_pruned") or 0) for record in records
        ),
        "environmental_covered_edges": sum(
            int(record.get("environmental_covered_edges") or 0) for record in records
        ),
        "objective_summaries": records,
        "reason": (
            "semantic and environmental-envelope diagnostics passed; "
            "resource boundary is diagnostic-only"
            if semantic_ok and resource_ok
            else "semantic, determinism, or resource diagnostic failed"
        ),
    }
    _write_jsonl(output / "resource-frontier.jsonl", records)
    _write_jsonl(output / "fifo-scan.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _write_jsonl(output / "eta-interval.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
        },
    )
    _atomic_json(heartbeat, {"status": status, "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(status + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if status == "REAL_ENVIRONMENT_SPEED_ENVELOPE_SEMANTIC_PASS" else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
