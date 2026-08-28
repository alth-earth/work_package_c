#!/usr/bin/env python3
"""Research-only real-input audit for the composed temporal bound.

Each objective is evaluated in three isolated phases: arrival-bound-only
baseline, arrival-bound plus certified heuristic candidate, and an independent
zero-heuristic exact-arrival Dijkstra with the same conservative arrival
envelope.  The runner never enables FIFO dominance or a production candidate.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import heapq
import importlib.util
import json
import os
import shutil
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
    run_non_fifo_temporal_composed_bound_heuristic_search,
)
from arctic_route_planning.planners.temporal_corridor import derive_temporal_corridor
from arctic_route_planning.planners.temporal_heuristic_bounds import (
    qualify_temporal_heuristic,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-composed-bound-real-24h.v1"
SEGMENT = "rolling_0_24h"
PHASES = ("baseline", "candidate", "reference")
OBJECTIVES = tuple(ObjectiveMode)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
BOUND_METHOD = "graph-max-speed-lower-bound-v1"
BOUND_EVALUATOR = "certified:grid-adjacency-distance-max-speed-v1"
HEURISTIC_METHOD = "graph-topological-objective-lower-bound-v1"
HEURISTIC_EVALUATOR = "certified:cost-model-graph-lower-bound-v1"
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_composed_bound_heuristic_real_24h.py",
    "scripts/benchmark_non_fifo_temporal_composed_bound_heuristic.py",
    "scripts/benchmark_non_fifo_temporal_certified_heuristic_real_24h.py",
    "scripts/benchmark_non_fifo_temporal_topological_bound_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_heuristic_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "uv.lock",
)


def _load_point() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_m10_real_point", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the audited real fixture runner")
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
        return {
            name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__
        }
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
            raise RuntimeError("another composed-bound runner owns this output") from error
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
        segment=SEGMENT,
    )


def _nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(fixture.grid.shape[0])
        for column in range(fixture.grid.shape[1])
    )


def _bound_bundle(point: Any, fixture: Any, objective: ObjectiveMode, *, heuristic: bool):
    planner = point._build_planner(fixture, objective)
    request = replace(point._request(fixture, objective), use_heuristic=heuristic)
    scope = planner.temporal_scope(request)
    nodes = _nodes(fixture)
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=planner.grid.neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=(planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR),
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
    state_certificate = corridor.certificate
    if not state_certificate.usable or not state_certificate.arrival_bound_complete:
        raise RuntimeError("real composed arrival certificate is incomplete")
    heuristic_certificate = None
    if heuristic:
        heuristic_certificate = qualify_temporal_heuristic(
            scope=scope,
            topology=topology,
            cost_model=planner._cost_model(objective),
            objective=objective.value,
            expected_scope=scope,
        )
        if not heuristic_certificate.usable:
            raise RuntimeError(
                f"real heuristic certificate rejected: {heuristic_certificate.reason}"
            )
    return planner, request, state_certificate, topology, heuristic_certificate


def _reference_search_bounded(
    point: Any,
    planner: Any,
    request: Any,
    certificate: Any,
) -> dict[str, Any]:
    """Independent exact-arrival Dijkstra with only the certified envelope."""

    started = time.perf_counter()
    start_state = (request.start, None, request.departure_time)
    labels: dict[tuple[Any, Any, datetime], float] = {start_state: 0.0}
    predecessors: dict[tuple[Any, Any, datetime], tuple[Any, Any]] = {}
    queue: list[tuple[float, int, tuple[Any, Any, datetime]]] = [(0.0, 0, start_state)]
    serial = 0
    expanded = 0
    edge_evaluations = 0
    queue_peak = 1
    state_bound_pruned = 0
    goal_state = None
    cost_model = planner._cost_model(request.objective)
    while queue:
        queued_cost, _, state = heapq.heappop(queue)
        if queued_cost != labels.get(state):
            continue
        expanded += 1
        if expanded > point.DEFAULT_LIMITS.max_expansions:
            raise point.TemporalSearchLimitExceeded("reference exceeded expansions=50000")
        node, heading_code, arrival = state
        if node == request.goal:
            goal_state = state
            break
        previous_heading = planner._previous_heading(node, heading_code)
        for neighbour in planner.grid.neighbors(node):
            edge_evaluations += 1
            if edge_evaluations > point.DEFAULT_LIMITS.max_edge_evaluations:
                raise point.TemporalSearchLimitExceeded(
                    "reference exceeded edge_evaluations=400000"
                )
            try:
                traversal = planner._evaluate_edge(
                    node,
                    neighbour,
                    arrival,
                    previous_heading,
                    request,
                    cost_model,
                )
            except Exception:
                continue
            if traversal.arrival_time <= arrival:
                continue
            if request.maximum_elapsed is not None and (
                traversal.arrival_time - request.departure_time > request.maximum_elapsed
            ):
                continue
            if not certificate.allows_state(
                neighbour,
                traversal.arrival_time,
                request.departure_time,
            ):
                state_bound_pruned += 1
                continue
            next_state = (
                neighbour,
                (neighbour[0] - node[0], neighbour[1] - node[1]),
                traversal.arrival_time,
            )
            next_cost = queued_cost + traversal.cost.total_equivalent_hours
            if next_cost >= labels.get(next_state, float("inf")):
                continue
            labels[next_state] = next_cost
            predecessors[next_state] = (state, traversal)
            if len(labels) > point.DEFAULT_LIMITS.max_labels:
                raise point.TemporalSearchLimitExceeded("reference exceeded labels=100000")
            serial += 1
            heapq.heappush(queue, (next_cost, serial, next_state))
            queue_peak = max(queue_peak, len(queue))
            if queue_peak > point.DEFAULT_LIMITS.max_queue:
                raise point.TemporalSearchLimitExceeded("reference exceeded queue=50000")
    if goal_state is None:
        raise point.NoRouteError("bounded reference found no route")
    states = [goal_state]
    traversals = []
    current = goal_state
    while current in predecessors:
        previous, traversal = predecessors[current]
        states.append(previous)
        traversals.append(traversal)
        current = previous
    states.reverse()
    traversals.reverse()
    return {
        "nodes": [list(state[0]) for state in states],
        "arrival_times": [
            state[2].astimezone(UTC).isoformat(timespec="microseconds") for state in states
        ],
        "total_cost_hours": labels[goal_state],
        "edge_values": [
            {
                "arrival_time": traversal.arrival_time.astimezone(UTC).isoformat(
                    timespec="microseconds"
                ),
                "heading_degrees": traversal.heading_degrees,
                "speed_knots": traversal.speed_knots,
                "distance_km": traversal.distance_km,
                "risk_score": traversal.risk_score,
                "maximum_risk": traversal.maximum_risk,
                "confidence": traversal.confidence,
                "cost": _jsonable(traversal.cost),
                "source_risk_ids": list(traversal.source_risk_ids),
            }
            for traversal in traversals
        ],
        "expanded": expanded,
        "edge_evaluations": edge_evaluations,
        "queue_peak": queue_peak,
        "state_bound_pruned": state_bound_pruned,
        "state_bound_arrival_pruned": state_bound_pruned,
        "compute_ms": (time.perf_counter() - started) * 1000.0,
    }


def _phase_worker(args: argparse.Namespace) -> dict[str, Any]:
    if args.cpu < 0 or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("a fixed CPU is required for real evidence")
    os.sched_setaffinity(0, {args.cpu})
    point = _load_point()
    fixture = point._load_fixture(_fixture_args(args))
    objective = ObjectiveMode(args.objective)
    before = point._resource_snapshot()
    started = time.perf_counter()
    errors: dict[str, str] = {}
    result = None
    reference = None
    state_certificate = None
    topology = None
    heuristic_certificate = None
    identity = None
    try:
        if args.phase == "baseline":
            planner, request, state_certificate, topology, _ = _bound_bundle(
                point, fixture, objective, heuristic=False
            )
            planner.state_bound_certificate = state_certificate
            request = replace(request, use_heuristic=False)
            identity = TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
            result = run_non_fifo_temporal_arrival_bounded_search(
                planner, request, state_certificate, identity=identity
            )
        elif args.phase == "candidate":
            planner, request, state_certificate, topology, heuristic_certificate = _bound_bundle(
                point, fixture, objective, heuristic=True
            )
            planner.state_bound_certificate = state_certificate
            planner.heuristic_certificate = heuristic_certificate
            identity = TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
            result = run_non_fifo_temporal_composed_bound_heuristic_search(
                planner,
                request,
                state_certificate,
                heuristic_certificate,
                identity=identity,
            )
        elif args.phase == "reference":
            planner, request, state_certificate, topology, _ = _bound_bundle(
                point, fixture, objective, heuristic=False
            )
            request = replace(request, use_heuristic=False)
            reference = _reference_search_bounded(point, planner, request, state_certificate)
            identity = TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
        else:  # pragma: no cover - parser constrains phases
            raise ValueError(f"unknown phase: {args.phase}")
    except Exception as error:  # pragma: no cover - isolated worker boundary
        errors[args.phase] = f"{type(error).__name__}: {error}"
    after = point._resource_snapshot()
    if args.phase == "reference":
        phase_status = "GOAL_FOUND" if reference is not None else (
            "RESOURCE_LIMIT"
            if any("exceeded" in value for value in errors.values())
            else "ERROR"
        )
        semantic = reference
        diagnostics = reference
    else:
        phase_status = "ERROR" if result is None else result.status.value
        semantic = (
            None
            if result is None or result.planning_result is None
            else point._route_semantic(result)
        )
        diagnostics = None if result is None else _jsonable(result.diagnostics)
    resource_clean = point._resource_clean(before, after)
    resource_evidence_complete = point._resource_evidence_complete(
        {"resources_before": before, "resources_after": after},
        cpu=args.cpu,
    )
    result_diagnostics = None if result is None else result.diagnostics
    reference_state_bound_pruned = (
        0 if reference is None else int(reference.get("state_bound_pruned", 0))
    )
    reference_state_bound_arrival_pruned = (
        0 if reference is None else int(reference.get("state_bound_arrival_pruned", 0))
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": phase_status,
        "phase": args.phase,
        "input": fixture.input_name,
        "segment": SEGMENT,
        "objective": objective.value,
        "repetition": args.repetition,
        "adapter_mode": "non_fifo_composed_arrival_bound_heuristic_24h_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope",
        "state_bound_certificate_digest": (
            None if state_certificate is None else state_certificate.digest
        ),
        "state_bound_proof_digest": (
            None if state_certificate is None else state_certificate.proof_digest
        ),
        "state_bound_scope_digest": (
            None if state_certificate is None else state_certificate.scope.digest
        ),
        "arrival_bound_complete": (
            False if state_certificate is None else state_certificate.arrival_bound_complete
        ),
        "heuristic_policy": (
            None
            if result_diagnostics is None
            else result_diagnostics.heuristic_policy
        ),
        "heuristic_certificate_digest": (
            None if heuristic_certificate is None else heuristic_certificate.digest
        ),
        "heuristic_proof_digest": (
            None if heuristic_certificate is None else heuristic_certificate.proof_digest
        ),
        "heuristic_scope_digest": (
            None if heuristic_certificate is None else heuristic_certificate.scope.digest
        ),
        "heuristic_scope_match": (
            False
            if result_diagnostics is None
            else result_diagnostics.heuristic_scope_match
        ),
        "heuristic_rejected": (
            0 if result_diagnostics is None else result_diagnostics.heuristic_rejected
        ),
        "state_bound_checks": (
            0 if result_diagnostics is None else result_diagnostics.state_bound_checks
        ),
        "state_bound_pruned": (
            reference_state_bound_pruned
            if args.phase == "reference"
            else 0
            if result_diagnostics is None
            else result_diagnostics.state_bound_pruned
        ),
        "state_bound_arrival_pruned": (
            reference_state_bound_arrival_pruned
            if args.phase == "reference"
            else 0
            if result_diagnostics is None
            else result_diagnostics.state_bound_arrival_pruned
        ),
        "state_bound_rejected": (
            0
            if result_diagnostics is None
            else result_diagnostics.state_bound_rejected
        ),
        "dominance_pruned": (
            0 if result_diagnostics is None else result_diagnostics.dominance_pruned
        ),
        "topology_digest": None if topology is None else topology.digest,
        "semantic": semantic,
        "semantic_digest": (
            None if result is None else result.semantic_digest
        ),
        "reference": reference,
        "diagnostics": diagnostics,
        "session_identity": None if identity is None else identity.digest,
        "errors": errors,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "cpu": args.cpu,
        "production_candidate_enabled": False,
    }


def _phase_command(args: argparse.Namespace, objective: ObjectiveMode, repetition: int, phase: str):
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
        SEGMENT,
        "--output-dir",
        str(args.output_dir),
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--phase",
        phase,
        "--cpu",
        str(args.cpu),
    ]


def _systemd_command(command: list[str]) -> list[str]:
    if shutil.which("systemd-run") is None:
        return command
    probe = subprocess.run(
        ["systemd-run", "--scope", "--quiet", "true"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        return command
    return [
        "systemd-run",
        "--scope",
        "--quiet",
        "--property=MemoryMax=4G",
        "--property=MemorySwapMax=0",
        "--property=OOMPolicy=stop",
        *command,
    ]


def _run_phase(args: argparse.Namespace, objective: ObjectiveMode, repetition: int, phase: str):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            _systemd_command(_phase_command(args, objective, repetition, phase)),
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
            "phase": phase,
            "objective": objective.value,
            "repetition": repetition,
            "reason": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "phase": phase,
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
            "phase": phase,
            "objective": objective.value,
            "repetition": repetition,
            "reason": f"worker JSON decode failed: {error}",
        }
    if not isinstance(record, dict):
        raise RuntimeError("phase worker emitted a non-object JSON record")
    return record


def _combine_case(
    phases: dict[str, dict[str, Any]],
    *,
    objective: ObjectiveMode,
    repetition: int,
    experiment_id: str,
    point: Any,
) -> dict[str, Any]:
    baseline = phases["baseline"]
    candidate = phases["candidate"]
    reference_phase = phases["reference"]
    reference = reference_phase.get("reference")
    baseline_semantic = baseline.get("semantic")
    candidate_semantic = candidate.get("semantic")
    baseline_match = (
        baseline_semantic is not None
        and reference is not None
        and point._reference_matches(baseline_semantic, reference)
    )
    candidate_match = (
        candidate_semantic is not None
        and reference is not None
        and point._reference_matches(candidate_semantic, reference)
    )
    semantic_match = (
        baseline.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and candidate.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and baseline.get("semantic_digest") is not None
        and baseline.get("semantic_digest") == candidate.get("semantic_digest")
    )
    resource_clean = all(phase.get("resource_clean") is True for phase in phases.values())
    resource_evidence_complete = all(
        phase.get("resource_evidence_complete") is True for phase in phases.values()
    )
    candidate_valid = (
        candidate.get("heuristic_scope_match") is True
        and int(candidate.get("heuristic_rejected", 0)) == 0
        and int(candidate.get("state_bound_rejected", 0)) == 0
        and int(candidate.get("dominance_pruned", 0)) == 0
        and candidate.get("arrival_bound_complete") is True
    )
    phase_errors = {
        name: phase.get("errors") or phase.get("reason") or phase.get("status")
        for name, phase in phases.items()
        if phase.get("errors") or phase.get("status") not in {"GOAL_FOUND", "PASS"}
    }
    all_success = (
        all(phase.get("status") in {"GOAL_FOUND", "PASS"} for phase in phases.values())
        and semantic_match
        and baseline_match
        and candidate_match
        and candidate_valid
        and resource_clean
        and resource_evidence_complete
    )
    has_resource_failure = any(
        phase.get("status") in {"TIMEOUT", "RESOURCE_LIMIT"}
        for phase in phases.values()
    )
    status = (
        "PASS"
        if all_success
        else "REAL_INPUT_24H_RESOURCE_FAIL"
        if has_resource_failure
        else "NO_PERFORMANCE_PROOF/FAIL"
        if all(phase.get("status") not in {"INVALID/PENDING"} for phase in phases.values())
        else "INVALID/PENDING"
    )
    baseline_diag = baseline.get("diagnostics") or {}
    candidate_diag = candidate.get("diagnostics") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": experiment_id,
        "input": baseline.get("input") or candidate.get("input"),
        "segment": SEGMENT,
        "objective": objective.value,
        "repetition": repetition,
        "adapter_mode": "non_fifo_composed_arrival_bound_heuristic_24h_v1",
        "phase_statuses": {name: phase.get("status") for name, phase in phases.items()},
        "baseline_status": baseline.get("status"),
        "candidate_status": candidate.get("status"),
        "reference_status": reference_phase.get("status"),
        "baseline_semantic_digest": baseline.get("semantic_digest"),
        "candidate_semantic_digest": candidate.get("semantic_digest"),
        "semantic_match": semantic_match,
        "reference_match": baseline_match and candidate_match,
        "baseline_reference_match": baseline_match,
        "candidate_reference_match": candidate_match,
        "baseline_semantic": baseline_semantic,
        "candidate_semantic": candidate_semantic,
        "reference": reference,
        "baseline_diagnostics": baseline_diag,
        "candidate_diagnostics": candidate_diag,
        "baseline_expanded_labels": int(baseline_diag.get("expanded_labels", 0)),
        "candidate_expanded_labels": int(candidate_diag.get("expanded_labels", 0)),
        "baseline_queue_peak": int(baseline_diag.get("queue_peak", 0)),
        "candidate_queue_peak": int(candidate_diag.get("queue_peak", 0)),
        "state_bound_pruned": int(candidate.get("state_bound_pruned", 0)),
        "candidate_state_bound_arrival_pruned": int(
            candidate.get("state_bound_arrival_pruned", 0)
        ),
        "reference_state_bound_pruned": int(reference_phase.get("state_bound_pruned", 0)),
        "heuristic_policy": candidate.get("heuristic_policy"),
        "heuristic_certificate_digest": candidate.get("heuristic_certificate_digest"),
        "heuristic_proof_digest": candidate.get("heuristic_proof_digest"),
        "heuristic_scope_match": candidate.get("heuristic_scope_match", False),
        "heuristic_rejected": int(candidate.get("heuristic_rejected", 0)),
        "state_bound_certificate_digest": candidate.get("state_bound_certificate_digest"),
        "state_bound_proof_digest": candidate.get("state_bound_proof_digest"),
        "topology_digest": candidate.get("topology_digest"),
        "phase_records": phases,
        "errors": phase_errors,
        "wall_seconds": sum(float(phase.get("wall_seconds", 0.0)) for phase in phases.values()),
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "resources_by_phase": {
            name: {
                "before": phase.get("resources_before"),
                "after": phase.get("resources_after"),
            }
            for name, phase in phases.items()
        },
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "reason": None if status == "PASS" else "composed 24h phase gate failed",
    }


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
            "segment": SEGMENT,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "adapter_mode": "non_fifo_composed_arrival_bound_heuristic_24h_v1",
        "bound_method": BOUND_METHOD,
        "bound_evaluator": BOUND_EVALUATOR,
        "heuristic_method": HEURISTIC_METHOD,
        "heuristic_evaluator": HEURISTIC_EVALUATOR,
        "dominance_policy": "disabled",
        "search_limits": LIMITS,
        "resource_boundary": {"memory_max": 4 * 1024**3, "memory_swap_max": 0},
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(cases: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    expected = len(identity["objectives"]) * int(identity["repetitions"])
    complete = len(cases) == expected
    all_pass = complete and all(case.get("status") == "PASS" for case in cases)
    semantic = bool(cases) and all(case.get("semantic_match") is True for case in cases)
    reference = bool(cases) and all(case.get("reference_match") is True for case in cases)
    resources = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    evidence = bool(cases) and all(
        case.get("resource_evidence_complete") is True for case in cases
    )
    deterministic = True
    for objective in identity["objectives"]:
        records = [case for case in cases if case.get("objective") == objective]
        digests = {case.get("candidate_semantic_digest") for case in records}
        if len(records) != int(identity["repetitions"]) or len(digests) != 1 or None in digests:
            deterministic = False
    observed_pruning = sum(int(case.get("state_bound_pruned", 0)) for case in cases)
    resource_failure = any(
        case.get("status") == "REAL_INPUT_24H_RESOURCE_FAIL"
        or "RESOURCE_LIMIT" in (case.get("phase_statuses") or {}).values()
        or "TIMEOUT" in (case.get("phase_statuses") or {}).values()
        for case in cases
    )
    status = (
        "READY_FOR_P0.2-COMPOSED-BOUND-REAL-REVIEW"
        if all_pass
        and semantic
        and reference
        and resources
        and evidence
        and deterministic
        and observed_pruning > 0
        else "REAL_INPUT_24H_RESOURCE_FAIL"
        if complete and resource_failure
        else "NO_PERFORMANCE_PROOF/FAIL"
        if complete
        else "INVALID/PENDING"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "semantic_match": semantic,
        "reference_match": reference,
        "resource_clean": resources,
        "resource_evidence_complete": evidence,
        "deterministic": deterministic,
        "observed_state_bound_pruning": observed_pruning,
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
    parser.add_argument("--segment", choices=(SEGMENT,), default=SEGMENT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--phase", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < 0:
        raise SystemExit("repetitions/timeout must be positive and cpu must be non-negative")
    if args.segment != SEGMENT:
        raise SystemExit(f"M10 only supports {SEGMENT}")
    if args.worker:
        if args.objective is None or args.phase is None:
            raise SystemExit("worker requires --objective and --phase")
        print(json.dumps(_jsonable(_phase_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    root = Path(__file__).resolve().parents[1]
    point = _load_point()
    fixture = point._load_fixture(_fixture_args(args))
    selected = (ObjectiveMode(args.objective),) if args.objective else OBJECTIVES
    identity = _identity(args, fixture, root)
    identity["objectives"] = [objective.value for objective in selected]
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"), text=True
    ).strip()
    if dirty:
        raise RuntimeError("M10 real runner requires a clean implementation worktree")
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
        expected = len(selected) * args.repetitions
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
        for repetition in range(1, args.repetitions + 1):
            order = selected if repetition % 2 else tuple(reversed(selected))
            for objective in order:
                key = (objective.value, repetition)
                if key in existing:
                    continue
                phases = {
                    phase: _run_phase(args, objective, repetition, phase) for phase in PHASES
                }
                record = _combine_case(
                    phases,
                    objective=objective,
                    repetition=repetition,
                    experiment_id=identity["experiment_id"],
                    point=point,
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
                        "expected_cases": expected,
                    },
                )
        _atomic_json(output / "fifo-scan.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
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
                "completed_cases": len(cases),
                "expected_cases": expected,
            },
        )
        marker = "ALL_DONE" if summary["status"].startswith("READY_FOR") else "STOPPED_HARD"
        _atomic_json(
            output / marker,
            {"status": summary["status"], "experiment_id": identity["experiment_id"]},
        )
        compact = {key: value for key, value in summary.items() if key != "cases"}
        print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"].startswith("READY_FOR") else 2


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
