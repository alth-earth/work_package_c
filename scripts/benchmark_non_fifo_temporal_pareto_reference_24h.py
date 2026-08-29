#!/usr/bin/env python3
"""Independent certificate-aware exact-arrival reference for the real 24h path.

This is a C-internal research sidecar.  It runs the M18 actual-edge Pareto
candidate and a separate scalar, zero-heuristic Dijkstra implementation that
uses the same proof-carrying arrival envelope only as a necessary-condition
state bound.  The reference is correctness evidence, never a performance
baseline, and cannot enable a production candidate or Winter experiment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import heapq
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.cost import UnnavigableSpeedError
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_refinement import EtaRefinementError
from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoSearchStatus,
)
from arctic_route_planning.planners.temporal_label_astar import _RejectedEdge
from arctic_route_planning.risk import RiskCoverageError, RiskSamplingError

SCHEMA_VERSION = "c.p0.2-temporal-pareto-reference-24h.v1"
OBJECTIVES = tuple(ObjectiveMode)
SEGMENTS = ("rolling_0_24h",)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_reference_24h.py",
    "scripts/benchmark_non_fifo_temporal_pareto_state_bound_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "uv.lock",
)


def _load_m18() -> Any:
    path = (
        Path(__file__).resolve().with_name("benchmark_non_fifo_temporal_pareto_state_bound_real.py")
    )
    spec = importlib.util.spec_from_file_location("c_m18_reference_bridge", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load M18 actual Pareto bridge")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        raise ValueError("evidence contains a non-finite float")
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


def _reference_route(
    goal_state: tuple[Any, Any, datetime],
    predecessors: dict[tuple[Any, Any, datetime], tuple[tuple[Any, Any, datetime], Any]],
    labels: dict[tuple[Any, Any, datetime], float],
) -> dict[str, Any]:
    states = [goal_state]
    traversals: list[Any] = []
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
    }


def _close_values(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    """Compare JSON evidence without hiding a business-field mismatch."""

    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _close_values(a, b, tolerance) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _close_values(left[key], right[key], tolerance) for key in left
        )
    return left == right


def _reference_matches(candidate: dict[str, Any], reference: dict[str, Any]) -> bool:
    """Compare route and every actual-edge business field independently."""

    if candidate.get("nodes") != reference.get("nodes"):
        return False
    if candidate.get("arrival_times") != reference.get("arrival_times"):
        return False
    costs = candidate.get("costs") or []
    if not costs or not _close_values(costs[0], reference.get("total_cost_hours")):
        return False
    candidate_edges = candidate.get("steps") or []
    reference_edges = reference.get("edge_values") or []
    if len(candidate_edges) != len(reference_edges):
        return False
    fields = (
        ("eta", "arrival_time"),
        ("heading_degrees", "heading_degrees"),
        ("speed_knots", "speed_knots"),
        ("distance_km", "distance_km"),
        ("risk_score", "risk_score"),
        ("maximum_risk", "maximum_risk"),
        ("confidence", "confidence"),
        ("cost", "cost"),
        ("source_risk_ids", "source_risk_ids"),
    )
    return all(
        _close_values(edge[left], expected[right])
        for edge, expected in zip(candidate_edges, reference_edges, strict=True)
        for left, right in fields
    )


@dataclass(frozen=True, slots=True)
class ReferenceOutcome:
    status: str
    route: dict[str, Any] | None
    stats: dict[str, Any]
    rejection_reasons: dict[str, int]
    error: str | None = None


def _reference_search(
    planner: Any,
    request: Any,
    certificate: Any,
) -> ReferenceOutcome:
    """Run an independent scalar Dijkstra with exact-arrival states.

    The certificate is consulted only after exact edge evaluation and only for
    newly generated states.  No candidate frontier or route is read here.
    """

    scope = planner.temporal_scope(request)
    if not certificate.permits(scope) or not certificate.arrival_bound_complete:
        return ReferenceOutcome(
            "REFERENCE_FAILURE",
            None,
            {},
            {},
            "certificate scope or arrival envelope is not usable",
        )
    start = (request.start, None, request.departure_time)
    labels: dict[tuple[Any, Any, datetime], float] = {start: 0.0}
    predecessors: dict[tuple[Any, Any, datetime], tuple[tuple[Any, Any, datetime], Any]] = {}
    queue: list[tuple[float, int, tuple[Any, Any, datetime]]] = [(0.0, 0, start)]
    serial = 0
    expanded = 0
    generated = 0
    edge_evaluations = 0
    queue_peak = 1
    state_bound_checks = 0
    state_bound_pruned = 0
    rejection_reasons: dict[str, int] = {}
    goal_state: tuple[Any, Any, datetime] | None = None
    cost_model = planner._cost_model(ObjectiveMode(request.objective))
    expected_rejections = (
        RiskCoverageError,
        RiskSamplingError,
        UnnavigableSpeedError,
        EtaRefinementError,
        _RejectedEdge,
    )

    def stats() -> dict[str, Any]:
        return {
            "expanded": expanded,
            "generated": generated,
            "labels": len(labels),
            "queue_peak": queue_peak,
            "edge_evaluations": edge_evaluations,
            "state_bound_checks": state_bound_checks,
            "state_bound_pruned": state_bound_pruned,
            "search_limits": LIMITS,
        }

    while queue:
        queued_cost, _order, state = heapq.heappop(queue)
        if queued_cost != labels.get(state):
            continue
        expanded += 1
        if expanded > LIMITS["max_expansions"]:
            return ReferenceOutcome(
                "REFERENCE_RESOURCE_LIMIT", None, stats(), rejection_reasons, "expansions=50000"
            )
        node, heading_code, arrival = state
        if node == request.goal:
            goal_state = state
            break
        previous_heading = planner._previous_heading(node, heading_code)
        for neighbor in planner.grid.neighbors(node):
            edge_evaluations += 1
            if edge_evaluations > LIMITS["max_edge_evaluations"]:
                return ReferenceOutcome(
                    "REFERENCE_RESOURCE_LIMIT",
                    None,
                    stats(),
                    rejection_reasons,
                    "edge_evaluations=400000",
                )
            try:
                traversal = planner._evaluate_edge(
                    node,
                    neighbor,
                    arrival,
                    previous_heading,
                    request,
                    cost_model,
                )
            except expected_rejections as error:
                reason = type(error).__name__
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                continue
            except Exception as error:  # unknown evaluator is a hard reference failure
                return ReferenceOutcome(
                    "REFERENCE_FAILURE",
                    None,
                    stats(),
                    rejection_reasons,
                    f"{type(error).__name__}: {error}",
                )
            if traversal.arrival_time <= arrival:
                return ReferenceOutcome(
                    "REFERENCE_FAILURE",
                    None,
                    stats(),
                    rejection_reasons,
                    "edge arrival is not strictly later than departure",
                )
            if request.maximum_elapsed is not None and (
                traversal.arrival_time - request.departure_time > request.maximum_elapsed
            ):
                rejection_reasons["horizon"] = rejection_reasons.get("horizon", 0) + 1
                continue
            state_bound_checks += 1
            next_heading = (
                None if neighbor == request.goal else (neighbor[0] - node[0], neighbor[1] - node[1])
            )
            if not certificate.allows_state(
                neighbor, traversal.arrival_time, request.departure_time
            ):
                state_bound_pruned += 1
                continue
            next_state = (neighbor, next_heading, traversal.arrival_time)
            next_cost = queued_cost + traversal.cost.total_equivalent_hours
            if next_cost >= labels.get(next_state, float("inf")):
                continue
            labels[next_state] = next_cost
            predecessors[next_state] = (state, traversal)
            generated += 1
            if len(labels) > LIMITS["max_labels"]:
                return ReferenceOutcome(
                    "REFERENCE_RESOURCE_LIMIT", None, stats(), rejection_reasons, "labels=100000"
                )
            serial += 1
            heapq.heappush(queue, (next_cost, serial, next_state))
            queue_peak = max(queue_peak, len(queue))
            if queue_peak > LIMITS["max_queue"]:
                return ReferenceOutcome(
                    "REFERENCE_RESOURCE_LIMIT", None, stats(), rejection_reasons, "queue=50000"
                )
    if goal_state is None:
        return ReferenceOutcome("REFERENCE_FAILURE", None, stats(), rejection_reasons, "no route")
    return ReferenceOutcome(
        "GOAL_FOUND",
        _reference_route(goal_state, predecessors, labels),
        stats(),
        rejection_reasons,
    )


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    m18 = _load_m18()
    point = m18._point_runner()
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    scope_digests: dict[str, str] = {}
    for objective in OBJECTIVES:
        planner = point._build_planner(fixture, objective)
        planner.eta_policy = m18.EtaRefinementPolicy(method="bounded")
        scope_digests[objective.value] = planner.temporal_scope(
            m18._request(point, fixture, objective)
        ).digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M19",
        "purpose": "independent_certificate_aware_24h_semantic_reference",
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
            "frame_digests": [m18.risk_frame_content_digest(frame) for frame in fixture.frames],
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
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "reference_policy": "certificate-aware-zero-heuristic-dijkstra-v1",
        "adapter_mode": "m18_actual_edge_zero_heuristic_pareto_v1",
        "eta_method": "bounded",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "search_limits": LIMITS,
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "winter_enabled": False,
        "scope_digests": scope_digests,
    }


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    m18 = _load_m18()
    started = time.perf_counter()
    m18._set_cpu(args.cpu)
    before = m18._resource_snapshot()
    fixture = None
    candidate = None
    reference: ReferenceOutcome | None = None
    corridor = None
    errors: dict[str, str] = {}
    try:
        point = m18._point_runner()
        fixture = point._load_fixture(m18._fixture_args(args))
        objective = ObjectiveMode(args.objective)
        bound_planner, request, _topology, corridor = m18._certificate(point, fixture, objective)
        reference = _reference_search(bound_planner, request, corridor.certificate)
        candidate = m18.run_non_fifo_temporal_pareto_search(
            bound_planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
            state_bound_certificate=corridor.certificate,
        )
    except Exception as error:  # pragma: no cover - child boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = m18._resource_snapshot()
    candidate_semantic = (
        m18._route_payload(candidate.selected)
        if candidate is not None and candidate.selected is not None
        else None
    )
    reference_route = reference.route if reference is not None else None
    reference_match = bool(
        candidate_semantic is not None
        and reference_route is not None
        and _reference_matches(candidate_semantic, reference_route)
    )
    diagnostics = m18._jsonable(candidate.diagnostics) if candidate is not None else None
    state_bound_rejected = m18._diagnostic_value(diagnostics, "state_bound_rejected")
    dominance_pruned = m18._diagnostic_value(diagnostics, "dominance_pruned")
    # Pareto and certified state-bound pruning are the explicitly audited
    # research mechanisms in this worker.  Only an actual dominance callback
    # or a rejected/mismatched state-bound certificate is unexpected.
    unexpected_pruning = dominance_pruned > 0 or state_bound_rejected > 0
    certificate_usable = bool(corridor is not None and corridor.certificate.usable)
    candidate_goal = bool(
        candidate is not None and candidate.status is NonFifoSearchStatus.GOAL_FOUND
    )
    reference_goal = bool(reference is not None and reference.status == "GOAL_FOUND")
    if errors or not certificate_usable or unexpected_pruning:
        status = "FAIL"
        reason = "reference/certificate/fail-closed gate failed"
    elif reference is not None and reference.status != "GOAL_FOUND":
        status = reference.status
        reason = reference.error or "reference did not produce a route"
    elif not candidate_goal:
        status = (
            "CANDIDATE_RESOURCE_LIMIT"
            if candidate is not None and candidate.status is NonFifoSearchStatus.RESOURCE_LIMIT
            else "FAIL"
        )
        reason = "candidate did not produce a route"
    elif not reference_match:
        status = "FAIL"
        reason = "candidate/reference semantic mismatch"
    else:
        status = "PASS"
        reason = None
    resource_clean = m18._resource_clean(before, after)
    resource_evidence_complete = m18._resource_evidence_complete(before, after, args.cpu)
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "repetition": args.repetition,
        "status": status,
        "reason": reason,
        "reference_status": reference.status if reference is not None else None,
        "reference_error": reference.error if reference is not None else None,
        "reference_route": reference_route,
        "reference_stats": reference.stats if reference is not None else None,
        "reference_rejection_reasons": reference.rejection_reasons if reference is not None else {},
        "candidate_status": candidate.status.value if candidate is not None else None,
        "candidate_semantic": candidate_semantic,
        "candidate_semantic_digest": candidate.semantic_digest if candidate is not None else None,
        "candidate_frontier_digest": candidate.frontier_digest if candidate is not None else None,
        "candidate_scope_digest": candidate.scope_digest if candidate is not None else None,
        "candidate_diagnostics": diagnostics,
        "candidate_state_bound_pruned": m18._diagnostic_value(diagnostics, "state_bound_pruned"),
        "candidate_state_bound_checks": m18._diagnostic_value(diagnostics, "state_bound_checks"),
        "candidate_state_bound_rejected": state_bound_rejected,
        "dominance_pruned": dominance_pruned,
        "unexpected_pruning": unexpected_pruning,
        "certificate_usable": certificate_usable,
        "certificate_digest": corridor.certificate.digest if corridor is not None else None,
        "proof_digest": corridor.proof_digest if corridor is not None else None,
        "scope_digest": corridor.certificate.scope.digest if corridor is not None else None,
        "reference_match": reference_match,
        "reference_goal": reference_goal,
        "candidate_goal": candidate_goal,
        "compute_ms": (time.perf_counter() - started) * 1000.0,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _record_key(record: dict[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or objective not in {item.value for item in OBJECTIVES}:
        return None
    if not isinstance(repetition, int):
        return None
    return objective, repetition


def _resource_failure_statuses() -> set[str]:
    return {
        "REFERENCE_RESOURCE_LIMIT",
        "REFERENCE_TIMEOUT",
        "CANDIDATE_RESOURCE_LIMIT",
        "CANDIDATE_TIMEOUT",
    }


def _summary(
    cases: list[dict[str, Any]], identity: dict[str, Any], malformed: int
) -> dict[str, Any]:
    repetitions = int(identity["repetitions"])
    expected = len(OBJECTIVES) * repetitions
    valid_keys = {_record_key(case) for case in cases}
    complete = (
        len(cases) == expected
        and malformed == 0
        and None not in valid_keys
        and len(valid_keys) == expected
    )
    resource_statuses = _resource_failure_statuses()
    resource_cases = [case for case in cases if case.get("status") in resource_statuses]
    hard_cases = [
        case for case in cases if case.get("status") not in ({"PASS"} | resource_statuses)
    ]
    semantic = bool(cases) and all(
        case.get("status") in resource_statuses
        or (
            case.get("status") == "PASS"
            and case.get("reference_match") is True
            and case.get("reference_goal") is True
            and case.get("candidate_goal") is True
        )
        for case in cases
    )
    certificate = bool(cases) and all(
        case.get("status") in resource_statuses or case.get("certificate_usable") is True
        for case in cases
    )
    deterministic_by_objective: dict[str, bool] = {}
    for objective in OBJECTIVES:
        selected = [case for case in cases if case.get("objective") == objective.value]
        signatures = {
            (
                case.get("status"),
                case.get("reference_status"),
                case.get("candidate_semantic_digest"),
                case.get("candidate_frontier_digest"),
                case.get("reference_match"),
                case.get("candidate_state_bound_pruned"),
            )
            for case in selected
        }
        deterministic_by_objective[objective.value] = (
            len(selected) == repetitions and len(signatures) == 1
        )
    deterministic = bool(deterministic_by_objective) and all(deterministic_by_objective.values())
    all_case_gates = bool(cases) and all(
        case.get("status") == "PASS" or case.get("status") in resource_statuses for case in cases
    )
    resource_clean = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    resource_evidence = bool(cases) and all(
        case.get("resource_evidence_complete") is True for case in cases
    )
    if not complete:
        status = "INVALID/PENDING"
    elif not semantic or not certificate or hard_cases or not all_case_gates or not deterministic:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif resource_cases:
        status = "REAL_INPUT_24H_REFERENCE_RESOURCE_FAIL"
    elif not resource_clean:
        status = "REAL_INPUT_24H_SEMANTIC_REFERENCE_RESOURCE_FAIL"
    elif not resource_evidence:
        status = "REAL_INPUT_24H_SEMANTIC_REFERENCE_RESOURCE_INCONCLUSIVE"
    else:
        status = "REAL_INPUT_24H_SEMANTIC_REFERENCE_READY"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "semantic_reference_complete": bool(cases) and not resource_cases and semantic,
        "semantic_match": bool(cases)
        and all(case.get("reference_match") is True for case in cases),
        "certificate_usable": certificate,
        "deterministic": deterministic,
        "deterministic_by_objective": deterministic_by_objective,
        "all_case_gates": all_case_gates,
        "resource_case_count": len(resource_cases),
        "hard_failure_case_count": len(hard_cases),
        "all_resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence,
        "reference_policy": "certificate-aware-zero-heuristic-dijkstra-v1",
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
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
            raise RuntimeError("another M19 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int
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
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--cpu",
        str(args.cpu),
    ]


def _run_child(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, heartbeat: Path
) -> dict[str, Any]:
    m18 = _load_m18()
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    driver_before = m18._resource_snapshot()
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
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
                "elapsed_seconds": elapsed,
            },
        )
        if elapsed > args.worker_timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            driver_after = m18._resource_snapshot()
            return {
                "schema_version": SCHEMA_VERSION,
                "objective": objective.value,
                "repetition": repetition,
                "status": "REFERENCE_TIMEOUT",
                "reason": "worker_timeout",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "resources_before": driver_before,
                "resources_after": driver_after,
                "resource_clean": m18._resource_clean(driver_before, driver_after),
                "resource_evidence_complete": m18._resource_evidence_complete(
                    driver_before, driver_after, args.cpu
                ),
            }
        time.sleep(1.0)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        driver_after = m18._resource_snapshot()
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "reason": "worker exited non-zero",
            "returncode": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resources_before": driver_before,
            "resources_after": driver_after,
            "resource_clean": m18._resource_clean(driver_before, driver_after),
            "resource_evidence_complete": m18._resource_evidence_complete(
                driver_before, driver_after, args.cpu
            ),
        }
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        driver_after = m18._resource_snapshot()
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "reason": "worker did not emit one JSON object",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resources_before": driver_before,
            "resources_after": driver_after,
            "resource_clean": m18._resource_clean(driver_before, driver_after),
            "resource_evidence_complete": m18._resource_evidence_complete(
                driver_before, driver_after, args.cpu
            ),
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _run(args: argparse.Namespace) -> int:
    m18 = _load_m18()
    m18._set_cpu(args.cpu)
    root = Path(__file__).resolve().parents[1]
    point = m18._point_runner()
    fixture = point._load_fixture(m18._fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M19 real evidence requires a clean implementation worktree")
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
                raise RuntimeError("resume identity does not match prepared experiment")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": (
                "manifest.json",
                "cases.jsonl",
                "reference-frontier.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ),
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        reference_path = output / "reference-frontier.jsonl"
        cases, malformed = _read_jsonl(cases_path) if args.resume else ([], 0)
        completed = {key for case in cases if (key := _record_key(case)) is not None}
        expected = len(OBJECTIVES) * args.repetitions
        heartbeat = output / "heartbeat.json"
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    key = (objective.value, repetition)
                    if key in completed:
                        continue
                    record = _run_child(args, objective, repetition, heartbeat)
                    record.setdefault("schema_version", SCHEMA_VERSION)
                    record["experiment_id"] = identity["experiment_id"]
                    if _record_key(record) != key:
                        raise RuntimeError("worker returned mismatched case identity")
                    _append_jsonl(cases_path, record)
                    _append_jsonl(reference_path, record)
                    cases.append(record)
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
            },
        )
        marker = output / ("ALL_DONE" if summary["complete"] else "STOPPED_HARD")
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["complete"] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="rolling_0_24h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < 0:
        raise SystemExit("repetitions/timeout must be positive and cpu non-negative")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
