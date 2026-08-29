#!/usr/bin/env python3
"""Real-input 24h frontier for the actual Pareto topological state bound.

This is a C-internal research sidecar.  It compares the actual-edge,
zero-heuristic Pareto bridge without a state-bound certificate with the same
bridge supplied an explicit graph-topological arrival envelope.  The bound is
never installed in the production planner and this runner cannot authorize a
candidate or a Winter experiment.

The frozen real-input loader and independent exact-arrival reference are
reused from ``benchmark_temporal_dominance_real.py``.  The scalar topological
adapter is deliberately not used: this runner exercises the M16 bridge's
actual ``TemporalStateBoundCertificate`` path.  A resource-limit result is an
auditable frontier point, not a semantic or performance pass.
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
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_corridor import derive_temporal_corridor
from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-state-bound-24h.v1"
OBJECTIVES = tuple(ObjectiveMode)
MODES = ("one_shot", "slice_restore")
SEGMENTS = ("executable_0_6h", "rolling_0_24h")
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
BOUND_METHOD = "graph-max-speed-lower-bound-v1"
BOUND_EVALUATOR = "certified:grid-adjacency-distance-max-speed-v1"
IMPLEMENTATION_FILES = (
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


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load audited runner {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "total_seconds") and callable(value.total_seconds):
        return value.total_seconds()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _jsonable(getattr(value, name))
            for name in value.__dataclass_fields__
        }
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


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        raise RuntimeError("a fixed CPU is required for real evidence")
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


def _point_runner() -> Any:
    return _load_script("benchmark_temporal_dominance_real.py", "c_m17_real_point")


def _resource_snapshot() -> dict[str, Any]:
    return _point_runner()._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(_point_runner()._resource_clean(before, after))


def _resource_evidence_complete(
    before: dict[str, Any], after: dict[str, Any], cpu: int
) -> bool:
    """Require the promised 4 GiB/no-swap cgroup, not merely a host snapshot."""

    for snapshot in (before, after):
        if snapshot.get("cpu_affinity") != [cpu]:
            return False
        cgroup = snapshot.get("cgroup") or {}
        if cgroup.get("memory_max") != 4 * 1024**3:
            return False
        if cgroup.get("memory_swap_max") != 0:
            return False
        if cgroup.get("memory_events") is None:
            return False
    return True


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


def _request(point: Any, fixture: Any, objective: ObjectiveMode) -> Any:
    return replace(
        point._request(fixture, objective),
        use_heuristic=False,
        cancel_check=None,
    )


def _certificate(
    point: Any, fixture: Any, objective: ObjectiveMode
) -> tuple[Any, Any, Any, Any]:
    """Build a complete, scope-bound topological arrival certificate."""

    planner = point._build_planner(fixture, objective)
    planner.eta_policy = EtaRefinementPolicy(method="bounded")
    request = _request(point, fixture, objective)
    scope = planner.temporal_scope(request)
    nodes = _nodes(fixture)
    topology = qualify_topological_lower_bound(
        scope=scope,
        universe_nodes=nodes,
        start=request.start,
        goal=request.goal,
        neighbors=planner.grid.neighbors,
        edge_distance_km=planner.grid.distance_km,
        max_speed_km_per_hour=(
            planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR
        ),
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
    certificate = corridor.certificate
    if not certificate.usable or not certificate.arrival_bound_complete:
        raise RuntimeError("topological arrival certificate is incomplete")
    return planner, request, topology, corridor


def _route_payload(route: Any) -> dict[str, Any]:
    return {
        "nodes": [list(node) for node in route.nodes],
        "arrival_times": [_jsonable(value) for value in route.arrival_times],
        "costs": list(route.costs),
        "semantic_digest": route.semantic_digest,
        "steps": [
            {
                "start": list(step.start),
                "end": list(step.end),
                "eta": _jsonable(step.eta),
                "heading_degrees": step.heading_degrees,
                "speed_knots": step.speed_knots,
                "distance_km": step.distance_km,
                "risk_score": step.risk_score,
                "maximum_risk": step.maximum_risk,
                "confidence": step.confidence,
                "cost": _jsonable(step.cost),
                "source_risk_ids": list(step.source_risk_ids),
            }
            for step in route.steps
        ],
    }


def _close(left: Any, right: Any, tolerance: float = 1e-9) -> bool:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return abs(float(left) - float(right)) <= tolerance
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _close(a, b, tolerance) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _close(left[key], right[key], tolerance) for key in left
        )
    return left == right


def _reference_matches(candidate: dict[str, Any], reference: dict[str, Any]) -> bool:
    """Compare all actual-edge business fields with the independent oracle."""

    if candidate["nodes"] != reference["nodes"]:
        return False
    if candidate["arrival_times"] != reference["arrival_times"]:
        return False
    if not candidate["costs"] or not _close(
        candidate["costs"][0], reference["total_cost_hours"]
    ):
        return False
    reference_edges = reference["edge_values"]
    candidate_edges = candidate["steps"]
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
        _close(edge[left], expected[right])
        for edge, expected in zip(candidate_edges, reference_edges, strict=True)
        for left, right in fields
    )


def _diagnostic_value(diagnostics: Any, name: str) -> int:
    if diagnostics is None:
        return 0
    value = (
        diagnostics.get(name, 0)
        if isinstance(diagnostics, dict)
        else getattr(diagnostics, name, 0)
    )
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _search_stats(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    raw = result.raw_result
    return {
        "expanded": raw.expanded,
        "generated": raw.generated,
        "queue_peak": raw.queue_peak,
        "edge_evaluations": raw.edge_evaluations,
        "pareto_pruned": raw.pareto_pruned,
        "search_limits": raw.search_limits,
        "pareto_pruning": raw.pareto_pruning,
    }


def _run_candidate(
    planner: Any, request: Any, certificate: Any, mode: str, slice_expansions: int
) -> tuple[Any, dict[str, Any]]:
    checkpoint: dict[str, Any] = {}
    if mode == "one_shot":
        return (
            run_non_fifo_temporal_pareto_search(
                planner,
                request,
                pareto_pruning=True,
                skip_expected_rejections=True,
                state_bound_certificate=certificate,
            ),
            checkpoint,
        )
    session = create_non_fifo_temporal_pareto_session(
        planner,
        request,
        pareto_pruning=True,
        skip_expected_rejections=True,
        state_bound_certificate=certificate,
    )
    initial = session.advance(expansion_slice=slice_expansions)
    if initial is not None:
        checkpoint = {
            "reached": False,
            "state": session.state,
            "reason": "terminal-before-checkpoint",
        }
        return initial, checkpoint
    saved = session.checkpoint()
    checkpoint = {
        "reached": True,
        "digest": saved.digest,
        "session_id": session.session_id,
        "state": session.state,
    }
    restored = restore_non_fifo_temporal_pareto_session(
        planner,
        request,
        saved,
        skip_expected_rejections=True,
        state_bound_certificate=certificate,
    )
    checkpoint["restored_session_id"] = restored.session_id
    return restored.run(), checkpoint


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    _set_cpu(args.cpu)
    before = _resource_snapshot()
    point = None
    fixture = None
    baseline = None
    candidate = None
    reference = None
    corridor = None
    checkpoint: dict[str, Any] = {}
    errors: dict[str, str] = {}
    search_started = time.perf_counter()
    search_elapsed_ms: float | None = None
    try:
        point = _point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(args.objective)
        baseline_planner = point._build_planner(fixture, objective)
        baseline_planner.eta_policy = EtaRefinementPolicy(method="bounded")
        request = _request(point, fixture, objective)
        bound_planner, bound_request, _topology, corridor = _certificate(
            point, fixture, objective
        )
        if request != bound_request:
            raise RuntimeError("baseline/candidate request identity diverged")
        baseline = run_non_fifo_temporal_pareto_search(
            baseline_planner,
            request,
            pareto_pruning=True,
            skip_expected_rejections=True,
        )
        candidate, checkpoint = _run_candidate(
            bound_planner,
            bound_request,
            corridor.certificate,
            args.mode,
            args.slice_expansions,
        )
        if baseline.status is NonFifoSearchStatus.GOAL_FOUND:
            reference = point._reference_search(baseline_planner, request)
        search_elapsed_ms = (time.perf_counter() - search_started) * 1000.0
    except NonFifoTemporalParetoError as error:
        errors["identity"] = f"{type(error).__name__}: {error}"
    except Exception as error:  # pragma: no cover - child boundary evidence
        errors["worker"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()

    baseline_semantic = (
        _route_payload(baseline.selected)
        if baseline is not None and baseline.selected is not None
        else None
    )
    candidate_semantic = (
        _route_payload(candidate.selected)
        if candidate is not None and candidate.selected is not None
        else None
    )
    baseline_reference_match = (
        reference is not None
        and baseline_semantic is not None
        and _reference_matches(baseline_semantic, reference)
        if point is not None
        else False
    )
    candidate_reference_match = (
        reference is not None
        and candidate_semantic is not None
        and _reference_matches(candidate_semantic, reference)
        if point is not None
        else False
    )
    candidate_diagnostics = (
        _jsonable(candidate.diagnostics) if candidate is not None else None
    )
    baseline_diagnostics = (
        _jsonable(baseline.diagnostics) if baseline is not None else None
    )
    state_bound_checks = _diagnostic_value(candidate_diagnostics, "state_bound_checks")
    state_bound_pruned = _diagnostic_value(candidate_diagnostics, "state_bound_pruned")
    state_bound_arrival_pruned = _diagnostic_value(
        candidate_diagnostics, "state_bound_arrival_pruned"
    )
    state_bound_rejected = _diagnostic_value(
        candidate_diagnostics, "state_bound_rejected"
    )
    pareto_pruned = _diagnostic_value(candidate_diagnostics, "pareto_pruned")
    unexpected_pruning = (
        _diagnostic_value(candidate_diagnostics, "dominance_pruned") > 0
        or state_bound_rejected > 0
    )
    semantic_match = bool(
        baseline is not None
        and candidate is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and candidate.status is NonFifoSearchStatus.GOAL_FOUND
        and baseline.semantic_digest == candidate.semantic_digest
    )
    certificate_usable = bool(corridor is not None and corridor.certificate.usable)
    resource_clean = _resource_clean(before, after)
    resource_evidence_complete = _resource_evidence_complete(before, after, args.cpu)
    semantic_gate_pass = bool(
        not errors
        and certificate_usable
        and baseline is not None
        and candidate is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and candidate.status is NonFifoSearchStatus.GOAL_FOUND
        and semantic_match
        and baseline_reference_match
        and candidate_reference_match
        and state_bound_checks > 0
        and state_bound_rejected == 0
        and not unexpected_pruning
    )
    baseline_resource_limit = bool(
        baseline is not None
        and baseline.status is NonFifoSearchStatus.RESOURCE_LIMIT
    )
    candidate_resource_limit = bool(
        candidate is not None
        and candidate.status is NonFifoSearchStatus.RESOURCE_LIMIT
    )
    resource_limited = baseline_resource_limit or candidate_resource_limit
    if semantic_gate_pass:
        case_status = "PASS"
        case_reason = None
    elif resource_limited and not errors and not unexpected_pruning:
        # A frozen search limit is a valid resource-frontier observation.  It
        # must not be mistaken for a route-semantic pass or a candidate gain.
        case_status = "RESOURCE_LIMIT"
        case_reason = "frozen search limit reached"
    else:
        case_status = "FAIL"
        case_reason = "actual Pareto state-bound semantic gate failed"
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "mode": args.mode,
        "repetition": args.repetition,
        "status": case_status,
        "semantic_gate_pass": semantic_gate_pass,
        "resource_limited": resource_limited,
        "adapter_mode": "actual_edge_zero_heuristic_pareto_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "eta_method": "bounded",
        "certificate_usable": certificate_usable,
        "certificate_digest": (
            corridor.certificate.digest if corridor is not None else None
        ),
        "proof_digest": corridor.proof_digest if corridor is not None else None,
        "allowed_node_count": corridor.allowed_count if corridor is not None else 0,
        "excluded_node_count": corridor.excluded_count if corridor is not None else 0,
        "arrival_bound_complete": bool(
            corridor is not None and corridor.certificate.arrival_bound_complete
        ),
        "arrival_upper_bound_count": (
            len(corridor.certificate.arrival_upper_hours) if corridor is not None else 0
        ),
        "state_bound_checks": state_bound_checks,
        "state_bound_pruned": state_bound_pruned,
        "state_bound_arrival_pruned": state_bound_arrival_pruned,
        "state_bound_rejected": state_bound_rejected,
        "pareto_pruned": pareto_pruned,
        "unexpected_pruning": unexpected_pruning,
        "projected_label_reduction": (
            corridor.projected_label_reduction if corridor is not None else None
        ),
        "baseline_status": baseline.status.value if baseline is not None else None,
        "candidate_status": candidate.status.value if candidate is not None else None,
        "baseline_semantic_digest": (
            baseline.semantic_digest if baseline is not None else None
        ),
        "candidate_semantic_digest": (
            candidate.semantic_digest if candidate is not None else None
        ),
        "baseline_frontier_digest": (
            baseline.frontier_digest if baseline is not None else None
        ),
        "candidate_frontier_digest": (
            candidate.frontier_digest if candidate is not None else None
        ),
        "baseline_scope_digest": baseline.scope_digest if baseline is not None else None,
        "candidate_scope_digest": candidate.scope_digest if candidate is not None else None,
        "semantic_match": semantic_match,
        "baseline_reference_match": baseline_reference_match,
        "candidate_reference_match": candidate_reference_match,
        "baseline_semantic": baseline_semantic,
        "candidate_semantic": candidate_semantic,
        "reference": reference,
        "baseline_search_stats": _search_stats(baseline),
        "candidate_search_stats": _search_stats(candidate),
        "baseline_diagnostics": baseline_diagnostics,
        "candidate_diagnostics": candidate_diagnostics,
        "checkpoint": checkpoint,
        "session_id": candidate.session_id if candidate is not None else None,
        "errors": errors,
        "reason": case_reason,
        "compute_ms": search_elapsed_ms,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "resource_classification": (
            "QUALIFIED"
            if resource_evidence_complete and resource_clean
            else "INCONCLUSIVE_CGROUP_BOUNDARY"
            if not resource_evidence_complete
            else "RESOURCE_FAIL"
        ),
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    point = _point_runner()
    scope_digests = {}
    for objective in OBJECTIVES:
        planner = point._build_planner(fixture, objective)
        planner.eta_policy = EtaRefinementPolicy(method="bounded")
        scope_digests[objective.value] = planner.temporal_scope(
            _request(point, fixture, objective)
        ).digest
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": "P0.2-M18",
        "purpose": "real_24h_actual_pareto_state_bound_resource_frontier",
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
            "frame_digests": [
                point.risk_frame_content_digest(frame) for frame in fixture.frames
            ],
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
        "modes": list(MODES),
        "repetitions": args.repetitions,
        "slice_expansions": args.slice_expansions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "eta_method": "bounded",
        "adapter_mode": "actual_edge_zero_heuristic_pareto_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
        "bound_method": BOUND_METHOD,
        "bound_evaluator": BOUND_EVALUATOR,
        "scope_digests": scope_digests,
        "pareto_pruning": True,
        "skip_expected_rejections": True,
        "search_limits": LIMITS,
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _record_key(record: dict[str, Any]) -> tuple[str, int, str] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    mode = record.get("mode")
    if not isinstance(objective, str) or not isinstance(repetition, int):
        return None
    if objective not in {item.value for item in OBJECTIVES} or mode not in MODES:
        return None
    return objective, repetition, mode


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, mode: str
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
        "--mode",
        mode,
        "--slice-expansions",
        str(args.slice_expansions),
        "--worker-timeout-seconds",
        str(args.worker_timeout_seconds),
        "--cpu",
        str(args.cpu),
    ]


def _run_child(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    mode: str,
    heartbeat: Path,
) -> dict[str, Any]:
    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition, mode),
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
            "mode": mode,
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
                "mode": mode,
                "elapsed_seconds": elapsed,
            },
        )
        if elapsed > args.worker_timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            return {
                "schema_version": SCHEMA_VERSION,
                "objective": objective.value,
                "repetition": repetition,
                "mode": mode,
                "status": "TIMEOUT",
                "reason": "worker_timeout",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "resource_clean": False,
                "resource_evidence_complete": False,
            }
        time.sleep(1.0)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": mode,
            "status": "INVALID/PENDING",
            "reason": "worker exited non-zero",
            "returncode": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": mode,
            "status": "INVALID/PENDING",
            "reason": "worker did not emit one JSON object",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _summary(
    cases: list[dict[str, Any]], identity: dict[str, Any], malformed: int
) -> dict[str, Any]:
    repetitions = int(identity["repetitions"])
    expected = len(OBJECTIVES) * len(MODES) * repetitions
    complete = len(cases) == expected and malformed == 0
    valid_keys = {_record_key(case) for case in cases}
    complete = complete and None not in valid_keys and len(valid_keys) == expected
    resource_limited_cases = [
        case
        for case in cases
        if case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
    ]
    hard_failure_cases = [
        case
        for case in cases
        if case.get("status") in {"FAIL", "INVALID/PENDING"}
    ]
    semantic = bool(cases) and all(
        case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
        or (
            case.get("semantic_match") is True
            and case.get("baseline_reference_match") is True
            and case.get("candidate_reference_match") is True
        )
        for case in cases
    )
    certificate = bool(cases) and all(
        case.get("status") in {"RESOURCE_LIMIT", "TIMEOUT"}
        or (
            case.get("certificate_usable") is True
            and case.get("arrival_bound_complete") is True
            and int(case.get("state_bound_rejected", 0)) == 0
        )
        for case in cases
    )
    pruning = sum(int(case.get("state_bound_pruned", 0)) for case in cases)
    unexpected_pruning = any(case.get("unexpected_pruning") is True for case in cases)
    resource_clean = bool(cases) and all(
        case.get("resource_clean") is True for case in cases
    )
    resource_evidence = bool(cases) and all(
        case.get("resource_evidence_complete") is True for case in cases
    )
    deterministic_by_cell: dict[str, bool] = {}
    for objective in OBJECTIVES:
        for mode in MODES:
            selected = [
                case
                for case in cases
                if case.get("objective") == objective.value and case.get("mode") == mode
            ]
            signatures = {
                (
                    case.get("status"),
                    case.get("baseline_semantic_digest"),
                    case.get("candidate_semantic_digest"),
                    case.get("candidate_frontier_digest"),
                    case.get("state_bound_pruned"),
                    case.get("state_bound_rejected"),
                )
                for case in selected
            }
            deterministic_by_cell[f"{objective.value}:{mode}"] = (
                len(selected) == repetitions and len(signatures) == 1
            )
    deterministic = bool(deterministic_by_cell) and all(deterministic_by_cell.values())
    all_case_gates = bool(cases) and all(
        case.get("status") in {"PASS", "RESOURCE_LIMIT", "TIMEOUT"}
        for case in cases
    )
    if not complete:
        status = "INVALID/PENDING"
    elif (
        not semantic
        or not certificate
        or unexpected_pruning
        or hard_failure_cases
        or not all_case_gates
        or not deterministic
    ):
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif resource_limited_cases:
        status = "REAL_INPUT_24H_STATE_BOUND_RESOURCE_FAIL"
    elif not resource_clean:
        status = "REAL_INPUT_PARETO_STATE_BOUND_RESOURCE_FAIL"
    elif not resource_evidence:
        status = "REAL_INPUT_STATE_BOUND_SEMANTIC_PASS_RESOURCE_INCONCLUSIVE"
    else:
        status = "REAL_INPUT_24H_STATE_BOUND_RESOURCE_REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "semantic_match": semantic,
        "certificate_usable": certificate,
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "unexpected_pruning": unexpected_pruning,
        "observed_state_bound_pruning": pruning,
        "all_case_gates": all_case_gates,
        "resource_limited_case_count": len(resource_limited_cases),
        "hard_failure_case_count": len(hard_failure_cases),
        "all_resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence,
        "dominance_policy": "disabled",
        "state_bound_policy": "graph-topological-arrival-envelope-v1",
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
            raise RuntimeError("another M18 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    point = _point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M18 real evidence requires a clean implementation worktree")
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
                "resource-frontier.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ),
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        resource_path = output / "resource-frontier.jsonl"
        cases, malformed = _read_jsonl(cases_path) if args.resume else ([], 0)
        completed = {
            key for case in cases if (key := _record_key(case)) is not None
        }
        expected = len(OBJECTIVES) * len(MODES) * args.repetitions
        heartbeat = output / "heartbeat.json"
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    for mode in MODES:
                        key = (objective.value, repetition, mode)
                        if key in completed:
                            continue
                        record = _run_child(
                            args, objective, repetition, mode, heartbeat
                        )
                        record.setdefault("schema_version", SCHEMA_VERSION)
                        record["experiment_id"] = identity["experiment_id"]
                        if _record_key(record) != key:
                            raise RuntimeError("worker returned mismatched case identity")
                        _append_jsonl(cases_path, record)
                        _append_jsonl(resource_path, record)
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
    # A complete resource-limit frontier is a valid diagnostic outcome.  The
    # marker and summary carry the negative result; reserve non-zero for
    # incomplete/invalid evidence that cannot support a frontier claim.
    return 0 if summary["complete"] else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="rolling_0_24h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--slice-expansions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None or args.mode is None:
            raise SystemExit("worker requires --objective and --mode")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    if args.repetitions < 1 or args.slice_expansions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions/slice/timeout must be positive")
    if args.cpu < 0:
        raise SystemExit("cpu must be non-negative")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
