#!/usr/bin/env python3
"""Real-input diagnostic for the graph-topological arrival envelope.

This C-internal runner reuses the frozen RiskFrame/route-plan loader and the
independent exact-arrival reference from the M6 real runner.  It installs only
the explicit topological arrival-bound certificate; FIFO dominance remains
disabled and no production artifact is written.
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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.cost.vessel import KNOT_TO_KM_PER_HOUR
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    run_non_fifo_temporal_arrival_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_corridor import derive_temporal_corridor
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-topological-bound-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
BOUND_METHOD = "graph-max-speed-lower-bound-v1"
BOUND_EVALUATOR = "certified:grid-adjacency-distance-max-speed-v1"
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_topological_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_arrival_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_bound_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
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


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
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


def _set_cpu(cpu: int) -> None:
    if cpu < 0 or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("a fixed CPU is required for real evidence")
    os.sched_setaffinity(0, {cpu})


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
            raise RuntimeError("another topology-bound runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(fixture.grid.shape[0])
        for column in range(fixture.grid.shape[1])
    )


def _certificate(point: Any, fixture: Any, objective: ObjectiveMode) -> tuple[Any, Any, Any]:
    planner = point._build_planner(fixture, objective)
    request = point._request(fixture, objective)
    request = replace(request, use_heuristic=False)
    scope = planner.temporal_scope(request)
    nodes = _nodes(fixture)
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=planner.grid.neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR,
        method=BOUND_METHOD,
        evaluator_digest=BOUND_EVALUATOR,
    )
    if not topology.usable:
        raise RuntimeError(f"topological lower-bound evidence rejected: {topology.reason}")
    corridor = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        forward_lower_hours=topology.forward_map,
        reverse_lower_hours=topology.reverse_map,
        horizon_hours=request.maximum_elapsed.total_seconds() / 3600.0,
        objective=objective.value,
        bound_evidence=topology.as_admissible_bound_evidence(),
        generated_nodes=nodes,
        include_arrival_upper_bounds=True,
    )
    if not corridor.certificate.usable or not corridor.certificate.arrival_bound_complete:
        raise RuntimeError("topological arrival certificate is incomplete")
    return planner, request, corridor


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    _set_cpu(args.cpu)
    point = _load_script("benchmark_temporal_dominance_real.py", "m7_real_point")
    fixture = point._load_fixture(_fixture_args(args))
    objective = ObjectiveMode(args.objective)
    baseline_planner = point._build_planner(fixture, objective)
    bound_planner, request, corridor = _certificate(point, fixture, objective)
    bound_planner.state_bound_certificate = corridor.certificate
    identity = TemporalSessionIdentity.from_planner(
        bound_planner,
        request,
        input_revision=0,
        risk_window_content_digest=fixture.commit["content_digest"],
        risk_window_commit_id=fixture.commit["commit_id"],
    )
    before = point._resource_snapshot()
    started = time.perf_counter()
    errors: dict[str, str] = {}
    baseline = None
    bounded = None
    reference = None
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, request)
        if baseline.status is NonFifoSearchStatus.GOAL_FOUND:
            reference = point._reference_search(baseline_planner, request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline_or_reference"] = f"{type(error).__name__}: {error}"
    try:
        bounded = run_non_fifo_temporal_arrival_bounded_search(
            bound_planner,
            request,
            corridor.certificate,
            identity=identity,
        )
    except Exception as error:  # pragma: no cover - worker boundary
        errors["bounded"] = f"{type(error).__name__}: {error}"
    after = point._resource_snapshot()
    baseline_semantic = None if baseline is None else point._route_semantic(baseline)
    bounded_semantic = None if bounded is None else point._route_semantic(bounded)
    baseline_match = (
        reference is not None
        and baseline_semantic is not None
        and point._reference_matches(baseline_semantic, reference)
    )
    bounded_match = (
        reference is not None
        and bounded_semantic is not None
        and point._reference_matches(bounded_semantic, reference)
    )
    diagnostics = None if bounded is None else _jsonable(bounded.diagnostics)
    pruned = 0 if bounded is None else int(bounded.diagnostics.state_bound_pruned)
    arrival_pruned = 0 if bounded is None else int(bounded.diagnostics.state_bound_arrival_pruned)
    rejected = 0 if bounded is None else int(bounded.diagnostics.state_bound_rejected)
    resource_clean = point._resource_clean(before, after)
    resource_evidence_complete = point._resource_evidence_complete(
        {"resources_before": before, "resources_after": after},
        cpu=args.cpu,
    )
    status = (
        "PASS"
        if (
            not errors
            and baseline is not None
            and baseline.status is NonFifoSearchStatus.GOAL_FOUND
            and bounded is not None
            and bounded.status is NonFifoSearchStatus.GOAL_FOUND
            and baseline.semantic_digest == bounded.semantic_digest
            and baseline_match
            and bounded_match
            and arrival_pruned > 0
            and rejected == 0
            and resource_clean
            and resource_evidence_complete
        )
        else "FAIL"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective.value,
        "repetition": args.repetition,
        "adapter_mode": "non_fifo_zero_heuristic_topological_arrival_bound_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope",
        "state_bound_authorized": rejected == 0 and bounded is not None,
        "state_bound_certificate_digest": corridor.certificate.digest,
        "state_bound_proof_digest": corridor.proof_digest,
        "arrival_bound_complete": corridor.certificate.arrival_bound_complete,
        "arrival_upper_bound_count": len(corridor.certificate.arrival_upper_hours),
        "state_bound_checks": 0 if bounded is None else int(bounded.diagnostics.state_bound_checks),
        "state_bound_pruned": pruned,
        "state_bound_arrival_pruned": arrival_pruned,
        "state_bound_rejected": rejected,
        "projected_label_reduction": corridor.projected_label_reduction,
        "semantic_match": baseline_match and bounded_match,
        "reference_match": baseline_match and bounded_match,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "bounded_semantic_digest": None if bounded is None else bounded.semantic_digest,
        "baseline_status": None if baseline is None else baseline.status.value,
        "bounded_status": None if bounded is None else bounded.status.value,
        "baseline_semantic": baseline_semantic,
        "bounded_semantic": bounded_semantic,
        "reference": reference,
        "diagnostics": diagnostics,
        "errors": errors,
        "reason": None if status == "PASS" else "topological-bound semantic/resource gate failed",
        "session_identity": identity.digest,
        "compute_ms": (
            None
            if bounded is None or bounded.planning_result is None
            else bounded.planning_result.metrics.compute_ms
        ),
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "production_candidate_enabled": False,
    }


def _worker_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--risk-window-commit",
        str(args.risk_window_commit),
        "--route-plan-set",
        str(args.route_plan_set),
        "--config-root",
        str(args.config_root),
        "--segment",
        args.segment,
        "--output-dir",
        str(args.output_dir),
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
    ]


def _run_worker(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
) -> dict[str, Any]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            _worker_command(args, objective, repetition),
            check=False,
            capture_output=True,
            text=True,
            timeout=args.worker_timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "TIMEOUT",
            "objective": objective.value,
            "repetition": repetition,
            "reason": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "objective": objective.value,
            "repetition": repetition,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
        }
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "objective": objective.value,
            "repetition": repetition,
            "reason": f"worker JSON decode failed: {error}",
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
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
        "objectives": [item.value for item in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "adapter_mode": "non_fifo_zero_heuristic_topological_arrival_bound_v1",
        "dominance_policy": "disabled",
        "bound_method": BOUND_METHOD,
        "bound_evaluator": BOUND_EVALUATOR,
        "search_limits": LIMITS,
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(cases: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    expected = len(OBJECTIVES) * int(identity["repetitions"])
    complete = len(cases) == expected and all(case.get("status") == "PASS" for case in cases)
    semantic = bool(cases) and all(case.get("semantic_match") is True for case in cases)
    resource = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    evidence = bool(cases) and all(case.get("resource_evidence_complete") is True for case in cases)
    deterministic = True
    for objective in OBJECTIVES:
        records = [case for case in cases if case.get("objective") == objective.value]
        digests = {case.get("bounded_semantic_digest") for case in records}
        if len(records) != int(identity["repetitions"]) or len(digests) != 1:
            deterministic = False
    pruning = sum(int(case.get("state_bound_arrival_pruned", 0)) for case in cases)
    if not complete:
        status = "INVALID/PENDING"
    elif not semantic or not resource or not evidence or not deterministic or pruning == 0:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    else:
        status = "READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REAL-REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "semantic_match": semantic,
        "resource_clean": resource,
        "resource_evidence_complete": evidence,
        "deterministic": deterministic,
        "observed_arrival_pruning": pruning,
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope",
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "cases": cases,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=("executable_0_6h",), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < 0:
        raise SystemExit("repetitions/timeout must be positive and cpu must be non-negative")
    root = Path(__file__).resolve().parents[1]
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    point = _load_script("benchmark_temporal_dominance_real.py", "m7_real_parent")
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"), text=True
    ).strip()
    if dirty:
        raise RuntimeError("real topology-bound runner requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest = output / "manifest.json"
        if manifest.exists():
            if not args.resume:
                raise RuntimeError("experiment exists; use --resume")
            if json.loads(manifest.read_text(encoding="utf-8")).get("identity") != _jsonable(
                identity
            ):
                raise RuntimeError("resume identity mismatch")
        _atomic_json(
            manifest,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "experiment_id": identity["experiment_id"],
                "identity": identity,
            },
        )
        cases_path = output / "cases.jsonl"
        existing: dict[tuple[str, int], dict[str, Any]] = {}
        if args.resume and cases_path.exists():
            for line in cases_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (record.get("objective"), record.get("repetition"))
                if key in existing:
                    raise RuntimeError("resume evidence contains duplicate case")
                existing[key] = record
        cases = list(existing.values())
        heartbeat = output / "heartbeat.json"
        expected_cases = len(OBJECTIVES) * args.repetitions
        for repetition in range(1, args.repetitions + 1):
            order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
            for objective in order:
                key = (objective.value, repetition)
                if key in existing:
                    continue
                record = _run_worker(args, objective, repetition)
                record.update(
                    {
                        "experiment_id": identity["experiment_id"],
                        "objective": objective.value,
                        "repetition": repetition,
                    }
                )
                _append_jsonl(cases_path, record)
                _append_jsonl(output / "resource-frontier.jsonl", record)
                existing[key] = record
                cases.append(record)
                _atomic_json(
                    heartbeat,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "RUNNING",
                        "updated_at": datetime.now(UTC),
                        "completed_cases": len(cases),
                        "expected_cases": expected_cases,
                    },
                )
        summary = _summary(cases, identity)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            manifest,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "experiment_id": identity["experiment_id"],
                "identity": identity,
            },
        )
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "updated_at": datetime.now(UTC),
            },
        )
        _atomic_json(output / "fifo-scan.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
        _atomic_json(output / "eta-interval.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
        marker = (
            "ALL_DONE"
            if summary["status"] == "READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REAL-REVIEW"
            else "STOPPED_HARD"
        )
        _atomic_json(
            output / marker,
            {"status": summary["status"], "experiment_id": identity["experiment_id"]},
        )
        compact = {key: value for key, value in summary.items() if key != "cases"}
        print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] == "READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REAL-REVIEW" else 2


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
