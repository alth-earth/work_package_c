#!/usr/bin/env python3
"""Evidence runner for the C-internal actual temporal Pareto bridge.

The runner deliberately uses a tiny deterministic RiskFrame fixture and an
independent exhaustive enumerator.  It is not a production benchmark and it
never imports or enables the formal planner's dominance policy.  Each worker
is a separate process so evaluator failures and resource limits cannot leak
state into another case.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import resource
import subprocess
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from arctic_route_planning.contracts.models import ProvenanceKind, RiskFrame, SourceReference
from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    TEMPORAL_PARETO_COMPONENTS,
    NonFifoTemporalParetoCheckpoint,
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.time_dependent_astar import PlanningRequest, _EdgeTraversal
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0.2-temporal-pareto-bridge.v1"
OBJECTIVES = ("fastest", "low_risk", "recommended")
MODES = ("one_shot", "slice_restore", "cancelled")
SCENARIOS = (
    "same_exact_dominance",
    "later_arrival",
    "business_evidence",
    "evaluator_failure",
    "resource_limit",
    "scope_drift",
    "checkpoint_tamper",
)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
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
    if isinstance(value, float) and not np.isfinite(value):
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


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


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
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    swap_kib = None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmSwap:"):
                swap_kib = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass
    cgroup: dict[str, Any] = {}
    for name in ("memory.max", "memory.swap.max", "memory.events"):
        path = Path("/sys/fs/cgroup") / name
        try:
            cgroup[name] = path.read_text(encoding="utf-8").strip()
        except OSError:
            cgroup[name] = None
    return {
        "cpu_affinity": (
            sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        ),
        "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "process_swap_kib": swap_kib,
        "cgroup": cgroup,
    }


def _resource_clean(before: dict[str, Any], after: dict[str, Any], cpu: int) -> bool:
    if cpu >= 0 and (before.get("cpu_affinity") != [cpu] or after.get("cpu_affinity") != [cpu]):
        return False
    return all(int(snapshot.get("process_swap_kib") or 0) == 0 for snapshot in (before, after))


def _make_frame(valid_time: datetime, risk: np.ndarray, risk_id: str) -> RiskFrame:
    rows, columns = risk.shape
    levels = np.minimum(5, np.floor(risk * 5).astype(np.uint8) + 1)
    import xarray as xr

    payload = xr.Dataset(
        {
            "risk_score": (("latitude", "longitude"), risk),
            "risk_level": (("latitude", "longitude"), levels),
            "hard_mask": (("latitude", "longitude"), np.zeros_like(risk, dtype=np.bool_)),
            "confidence": (("latitude", "longitude"), np.full_like(risk, 0.9)),
        },
        coords={
            "latitude": np.asarray([index * 0.05 for index in range(rows)], dtype=np.float64),
            "longitude": np.asarray([index * 0.05 for index in range(columns)], dtype=np.float64),
        },
        attrs={"crs": "EPSG:4326", "grid_id": "m14-pareto-fixture"},
    )
    source = SourceReference(
        source_id="m14-pareto-fixture",
        data_id=None,
        issue_time=None,
        valid_time=valid_time,
        version="v1",
        quality_flag="synthetic",
    )
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=risk_id,
        run_id="run-00000000-0000-4000-8000-000000000014",
        scenario_id="m14-pareto",
        corridor_id="m14-corridor",
        vessel_profile_id="m14-vessel",
        config_digest="0" * 64,
        model_config_digest="1" * 64,
        generation_id=14,
        valid_time=valid_time,
        as_of_time=T0,
        generated_at=T0,
        model_version="risk-model-v1",
        payload=payload,
        source_summary=(source,),
        provenance=ProvenanceKind.SYNTHETIC,
    )


def _edge_evaluator(planner: TemporalLabelAStar, scenario: str):
    direct = {((0, 0), (0, 1)), ((0, 1), (0, 2))}
    detour = {
        ((0, 0), (1, 0)),
        ((1, 0), (1, 1)),
        ((1, 1), (1, 2)),
        ((1, 2), (0, 2)),
    }

    def evaluate(start, end, departure_time, previous_heading, _request, cost_model):
        if scenario == "evaluator_failure":
            raise ValueError("m14 evaluator failure")
        if (start, end) in direct:
            hours, risk, confidence = 1.0, 0.1, 0.95
        elif (start, end) in detour:
            hours, risk, confidence = 0.5, 0.2, 0.85
        else:
            hours, risk, confidence = 10.0, 0.3, 0.8
        if scenario == "later_arrival":
            if (start, end) == ((0, 0), (0, 1)):
                hours = 0.1
            elif (start, end) == ((1, 0), (0, 0)):
                hours = 0.2
            elif (start, end) == ((0, 1), (0, 2)):
                hours = 5.0 if departure_time < T0 + timedelta(hours=0.2) else 0.1
            elif (start, end) == ((0, 0), (1, 0)):
                hours = 0.1
        distance = planner.grid.distance_km(start, end)
        heading = planner.grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=risk,
                confidence=confidence,
                heading_change_degrees=heading_change_degrees(previous_heading, heading),
            )
        )
        return _EdgeTraversal(
            start=start,
            end=end,
            arrival_time=departure_time + timedelta(hours=hours),
            heading_degrees=heading,
            speed_knots=10.0,
            distance_km=distance,
            risk_score=risk,
            maximum_risk=risk,
            confidence=confidence,
            cost=cost,
            source_risk_ids=(f"m14-{scenario}",),
        )

    return evaluate


def _planner(scenario: str, objective: str) -> TemporalLabelAStar:
    risk = np.zeros((2, 3), dtype=np.float32)
    frames = tuple(
        _make_frame(T0 + timedelta(hours=offset), risk, f"m14-risk-{index}")
        for index, offset in enumerate((0, 1, 8))
    )
    limits = TemporalSearchLimits(
        **({**LIMITS, "max_expansions": 1} if scenario == "resource_limit" else LIMITS)
    )
    planner = TemporalLabelAStar(
        RegularGrid(
            latitudes=(0.0, 0.05),
            longitudes=(0.0, 0.05, 0.1),
            allow_diagonal=False,
        ),
        RiskSampler(frames),
        VesselPerformanceModel(
            economic_speed_knots=10.0,
            minimum_steerage_speed_knots=2.0,
            maximum_speed_knots=12.0,
            minimum_speed_factor=0.2,
        ),
        limits=limits,
        edge_evaluator=None,
    )
    planner._injected_edge_evaluator = _edge_evaluator(planner, scenario)
    return planner


def _request(objective: str, *, cancel: bool = False) -> PlanningRequest:
    return PlanningRequest(
        start=(0, 0),
        goal=(0, 2),
        departure_time=T0,
        objective=ObjectiveMode(objective),
        use_heuristic=False,
        maximum_elapsed=timedelta(hours=6),
        cancel_check=(lambda: True) if cancel else None,
    )


def _vector(traversal: _EdgeTraversal) -> tuple[float, ...]:
    cost = traversal.cost
    return (
        cost.total_equivalent_hours,
        cost.travel_hours,
        cost.risk_exposure_hours,
        cost.distance_equivalent_hours,
        cost.turn_equivalent_hours,
        cost.deviation_equivalent_hours,
        cost.low_confidence_hours,
    )


def _oracle(planner: TemporalLabelAStar, request: PlanningRequest) -> dict[str, Any] | None:
    """Independent exhaustive state walk used only for small-fixture evidence."""

    context = planner._new_execution_context()
    cost_model = planner._cost_model(request.objective)
    initial = (
        (request.start, None),
        request.departure_time,
        (0.0,) * len(TEMPORAL_PARETO_COMPONENTS),
        (),
    )
    queue = [initial]
    goals: list[dict[str, Any]] = []
    visited = 0
    while queue:
        state, arrival, costs, steps = queue.pop(0)
        visited += 1
        if visited > 4096:
            raise RuntimeError("M14 oracle fixture did not remain finite")
        node, incoming = state
        if node == request.goal:
            goals.append(
                {
                    "arrival": arrival,
                    "costs": costs,
                    "steps": steps,
                }
            )
            continue
        for neighbour in planner.grid.neighbors(node):
            next_state = (
                neighbour,
                None
                if neighbour == request.goal
                else (neighbour[0] - node[0], neighbour[1] - node[1]),
            )
            previous_heading = planner._previous_heading(node, incoming)
            traversal = planner._evaluate_edge(
                node,
                neighbour,
                arrival,
                previous_heading,
                request,
                cost_model,
                context=context,
            )
            if traversal.arrival_time <= arrival:
                continue
            if request.maximum_elapsed is not None and (
                traversal.arrival_time - request.departure_time > request.maximum_elapsed
            ):
                continue
            step = {
                "start": node,
                "end": neighbour,
                "eta": traversal.arrival_time,
                "source_risk_ids": traversal.source_risk_ids,
            }
            queue.append(
                (
                    next_state,
                    traversal.arrival_time,
                    tuple(
                        left + right for left, right in zip(costs, _vector(traversal), strict=True)
                    ),
                    (*steps, step),
                )
            )
    if not goals:
        return None
    selected = min(goals, key=lambda item: (item["costs"], item["arrival"], repr(item["steps"])))
    return {
        "arrival": selected["arrival"],
        "costs": selected["costs"],
        "steps": selected["steps"],
    }


def _route_payload(result: Any) -> dict[str, Any] | None:
    route = result.selected
    if route is None:
        return None
    return {
        "nodes": route.nodes,
        "arrival_times": route.arrival_times,
        "costs": route.costs,
        "semantic_digest": route.semantic_digest,
        "steps": [
            {
                "start": step.start,
                "end": step.end,
                "eta": step.eta,
                "speed_knots": step.speed_knots,
                "risk_score": step.risk_score,
                "confidence": step.confidence,
                "source_risk_ids": step.source_risk_ids,
                "cost": step.cost,
            }
            for step in route.steps
        ],
    }


def _worker_record(
    scenario: str,
    objective: str,
    mode: str,
    repetition: int,
    cpu: int,
) -> dict[str, Any]:
    started = perf_counter()
    _set_cpu(cpu)
    planner = _planner(scenario, objective)
    request = _request(
        objective,
        cancel=(
            mode == "cancelled"
            and scenario not in {"scope_drift", "checkpoint_tamper"}
        ),
    )
    before = _resource_snapshot()
    checkpoint_digest = None
    restore_match = None
    mismatch_rejected = False
    error = None
    oracle = None
    result = None
    try:
        if scenario in {"scope_drift", "checkpoint_tamper"}:
            session = create_non_fifo_temporal_pareto_session(
                planner,
                request,
                pareto_pruning=True,
            )
            if session.advance(expansion_slice=1) is not None:
                raise RuntimeError("fixture did not pause before checkpoint")
            checkpoint = session.checkpoint()
            checkpoint_digest = checkpoint.digest
            if scenario == "scope_drift":
                alternate = (
                    ObjectiveMode.FASTEST
                    if ObjectiveMode(objective) is not ObjectiveMode.FASTEST
                    else ObjectiveMode.LOW_RISK
                )
                restore_non_fifo_temporal_pareto_session(
                    planner,
                    replace(request, objective=alternate),
                    checkpoint,
                )
            else:
                tampered = object.__new__(NonFifoTemporalParetoCheckpoint)
                object.__setattr__(tampered, "pareto_checkpoint", checkpoint.pareto_checkpoint)
                object.__setattr__(tampered, "scope_digest", checkpoint.scope_digest)
                object.__setattr__(tampered, "component_digest", checkpoint.component_digest)
                object.__setattr__(tampered, "schema_version", checkpoint.schema_version)
                object.__setattr__(tampered, "state_digest", "tampered")
                restore_non_fifo_temporal_pareto_session(planner, request, tampered)
            raise AssertionError("identity failure was not rejected")
        if mode == "one_shot":
            result = run_non_fifo_temporal_pareto_search(
                planner,
                request,
                pareto_pruning=True,
            )
        elif mode == "slice_restore":
            session = create_non_fifo_temporal_pareto_session(
                planner,
                request,
                pareto_pruning=True,
            )
            if session.advance(expansion_slice=1) is not None:
                raise RuntimeError("fixture did not pause before checkpoint")
            checkpoint = session.checkpoint()
            checkpoint_digest = checkpoint.digest
            restored = restore_non_fifo_temporal_pareto_session(planner, request, checkpoint)
            result = restored.run()
            full = run_non_fifo_temporal_pareto_search(planner, request, pareto_pruning=True)
            restore_match = (
                result.frontier_digest == full.frontier_digest
                and result.semantic_digest == full.semantic_digest
            )
        else:
            result = run_non_fifo_temporal_pareto_search(planner, request, pareto_pruning=True)
        if result.status is NonFifoSearchStatus.GOAL_FOUND:
            oracle = _oracle(planner, request)
        record = {
            "status": result.status.value,
            "reason": result.reason,
            "semantic_digest": result.semantic_digest,
            "frontier_digest": result.frontier_digest,
            "pareto_pruned": result.pareto_pruned,
            "selected": _route_payload(result),
            "oracle": oracle,
            "scope_digest": result.scope_digest,
            "session_id": result.session_id,
            "diagnostics": result.diagnostics,
            "evaluator_errors": result.evaluator_errors,
        }
    except NonFifoTemporalParetoError as exc:
        mismatch_rejected = True
        record = {
            "status": "MISMATCH_REJECTED",
            "reason": type(exc).__name__,
            "semantic_digest": None,
            "frontier_digest": None,
            "pareto_pruned": 0,
            "selected": None,
            "oracle": None,
            "scope_digest": None,
            "session_id": None,
            "diagnostics": None,
            "evaluator_errors": (),
        }
    except AssertionError as exc:
        error = str(exc)
        record = {
            "status": "UNEXPECTED_SUCCESS",
            "reason": error,
            "semantic_digest": None,
            "frontier_digest": None,
            "pareto_pruned": 0,
            "selected": None,
            "oracle": None,
            "scope_digest": None,
            "session_id": None,
            "diagnostics": None,
            "evaluator_errors": (),
        }
    except Exception as exc:  # pragma: no cover - worker boundary evidence
        error = f"{type(exc).__name__}:{exc}"
        record = {
            "status": "WORKER_ERROR",
            "reason": error,
            "semantic_digest": None,
            "frontier_digest": None,
            "pareto_pruned": 0,
            "selected": None,
            "oracle": None,
            "scope_digest": None,
            "session_id": None,
            "diagnostics": None,
            "evaluator_errors": (),
        }
    after = _resource_snapshot()
    record.update(
        {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario,
            "objective": objective,
            "mode": mode,
            "repetition": repetition,
            "expected_status": (
                "MISMATCH_REJECTED"
                if scenario in {"scope_drift", "checkpoint_tamper"}
                else (
                    NonFifoSearchStatus.CANCELLED.value
                    if mode == "cancelled"
                    else (
                        NonFifoSearchStatus.EVALUATOR_FAILURE.value
                        if scenario == "evaluator_failure"
                        else (
                            NonFifoSearchStatus.RESOURCE_LIMIT.value
                            if scenario == "resource_limit"
                            else NonFifoSearchStatus.GOAL_FOUND.value
                        )
                    )
                )
            ),
            "checkpoint_digest": checkpoint_digest,
            "restore_match": restore_match,
            "mismatch_rejected": mismatch_rejected,
            "resource_before": before,
            "resource_after": after,
            "resource_clean": _resource_clean(before, after, cpu),
            "elapsed_ms": (perf_counter() - started) * 1000.0,
            "error": error,
        }
    )
    return record


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = (
        Path(__file__).relative_to(root),
        Path("src/arctic_route_planning/planners/non_fifo_temporal_pareto.py"),
        Path("src/arctic_route_planning/planners/non_fifo_feasibility.py"),
    )
    return {
        "commit": subprocess.check_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ("git", "-C", str(root), "status", "--porcelain"), text=True
            ).strip()
        ),
        "files": {
            str(relative): hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in files
        },
    }


def _fixture_digest() -> str:
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "scenarios": SCENARIOS,
            "objectives": OBJECTIVES,
            "limits": LIMITS,
            "grid": {"rows": 2, "columns": 3, "allow_diagonal": False},
            "risk_frames": 3,
            "components": TEMPORAL_PARETO_COMPONENTS,
        }
    )


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation": _implementation_identity(root),
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest(),
        "fixture_digest": _fixture_digest(),
        "objectives": OBJECTIVES,
        "modes": MODES,
        "scenarios": SCENARIOS,
        "components": TEMPORAL_PARETO_COMPONENTS,
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "limits": LIMITS,
        "production_candidate_enabled": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--objective", choices=OBJECTIVES)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--repetition", type=int, default=1)
    return parser


def _summary(
    records: list[dict[str, Any]],
    args: argparse.Namespace,
    malformed: int = 0,
) -> dict[str, Any]:
    expected = len(SCENARIOS) * len(OBJECTIVES) * len(MODES) * args.repetitions
    expected_status_ok = all(
        record.get("status") == record.get("expected_status") for record in records
    )
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        cells[(record.get("scenario"), record.get("objective"), record.get("mode"))].append(record)
    deterministic = True
    deterministic_by_cell: dict[str, bool] = {}
    for key, cell in cells.items():
        signatures = {
            (
                record.get("status"),
                record.get("semantic_digest"),
                record.get("frontier_digest"),
                record.get("pareto_pruned"),
                record.get("restore_match"),
            )
            for record in cell
        }
        value = len(signatures) == 1
        deterministic_by_cell["/".join(key)] = value
        deterministic = deterministic and value
    oracle_match = True
    for record in records:
        selected = record.get("selected")
        oracle = record.get("oracle")
        if record.get("status") != NonFifoSearchStatus.GOAL_FOUND.value:
            continue
        if not selected or not oracle:
            oracle_match = False
            continue
        oracle_match = oracle_match and tuple(selected["costs"]) == tuple(oracle["costs"])
        oracle_match = oracle_match and selected["arrival_times"][-1] == oracle["arrival"]
        oracle_match = oracle_match and tuple(selected["nodes"]) == tuple(
            [step["start"] for step in oracle["steps"]] + [oracle["steps"][-1]["end"]]
        )
        oracle_match = oracle_match and all(
            tuple(step["source_risk_ids"]) == tuple(oracle_step["source_risk_ids"])
            for step, oracle_step in zip(selected["steps"], oracle["steps"], strict=True)
        )
    valid_pruning = any(
        record.get("scenario") == "same_exact_dominance"
        and record.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and record.get("pareto_pruned", 0) > 0
        for record in records
    )
    resources_clean = all(bool(record.get("resource_clean")) for record in records)
    mismatch_safe = all(
        record.get("status") != "MISMATCH_REJECTED" or record.get("mismatch_rejected")
        for record in records
    )
    complete = len(records) == expected and malformed == 0
    passed = (
        complete
        and expected_status_ok
        and deterministic
        and oracle_match
        and valid_pruning
        and resources_clean
        and mismatch_safe
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": (
            "TEMPORAL_NONFIFO_ACTUAL_PARETO_BRIDGE_MATRIX_PASS"
            if passed
            else "NO_PERFORMANCE_PROOF/FAIL"
        ),
        "expected_cases": expected,
        "completed_cases": len(records),
        "malformed_records": malformed,
        "expected_statuses": expected_status_ok,
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "oracle_match": oracle_match,
        "observed_same_exact_pruning": sum(
            int(record.get("pareto_pruned", 0))
            for record in records
            if record.get("scenario") == "same_exact_dominance"
        ),
        "valid_pruning": valid_pruning,
        "resources_clean": resources_clean,
        "mismatch_fail_closed": mismatch_safe,
        "production_candidate_enabled": False,
        "candidate_default": "disabled",
    }


def _worker_command(
    args: argparse.Namespace,
    scenario: str,
    objective: str,
    mode: str,
    repetition: int,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--output-dir",
        str(args.output_dir),
        "--cpu",
        str(args.cpu),
        "--scenario",
        scenario,
        "--objective",
        objective,
        "--mode",
        mode,
        "--repetition",
        str(repetition),
    ]


def _run_parent(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = _identity(args, root)
        manifest_path = output / "manifest.json"
        cases_path = output / "cases.jsonl"
        if args.resume and manifest_path.exists():
            existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing_manifest.get("identity") != identity:
                raise RuntimeError("resume identity mismatch")
        else:
            _atomic_json(manifest_path, {"identity": identity, "started_at": datetime.now(UTC)})
        records, malformed = _read_jsonl(cases_path)
        completed = {
            (
                record.get("scenario"),
                record.get("objective"),
                record.get("mode"),
                record.get("repetition"),
            )
            for record in records
        }
        total = len(SCENARIOS) * len(OBJECTIVES) * len(MODES) * args.repetitions
        for scenario in SCENARIOS:
            for objective in OBJECTIVES:
                for mode in MODES:
                    for repetition in range(1, args.repetitions + 1):
                        key = (scenario, objective, mode, repetition)
                        if key in completed:
                            continue
                        heartbeat = {
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(records),
                            "expected_cases": total,
                            "current": key,
                        }
                        _atomic_json(output / "heartbeat.json", heartbeat)
                        try:
                            completed_process = subprocess.run(
                                _worker_command(args, scenario, objective, mode, repetition),
                                check=False,
                                capture_output=True,
                                text=True,
                                timeout=args.worker_timeout_seconds,
                            )
                            if completed_process.returncode == 0:
                                record = json.loads(completed_process.stdout)
                            else:
                                record = {
                                    "schema_version": SCHEMA_VERSION,
                                    "scenario": scenario,
                                    "objective": objective,
                                    "mode": mode,
                                    "repetition": repetition,
                                    "status": "WORKER_ERROR",
                                    "expected_status": "unknown",
                                    "reason": completed_process.stderr[-1000:],
                                    "resource_clean": False,
                                }
                        except subprocess.TimeoutExpired:
                            record = {
                                "schema_version": SCHEMA_VERSION,
                                "scenario": scenario,
                                "objective": objective,
                                "mode": mode,
                                "repetition": repetition,
                                "status": "TIMEOUT",
                                "expected_status": "unknown",
                                "reason": "worker_timeout",
                                "resource_clean": False,
                            }
                        _append_jsonl(cases_path, record)
                        records.append(record)
        summary = _summary(records, args, malformed)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            output / "heartbeat.json",
            {
                "updated_at": datetime.now(UTC),
                "completed_cases": len(records),
                "expected_cases": total,
            },
        )
        marker = output / (
            "ALL_DONE" if summary["status"].endswith("MATRIX_PASS") else "STOPPED_HARD"
        )
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        return 0 if summary["status"].endswith("MATRIX_PASS") else 2


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if args.scenario is None or args.objective is None or args.mode is None:
            raise SystemExit("worker requires scenario, objective and mode")
        print(
            json.dumps(
                _jsonable(
                    _worker_record(
                        args.scenario,
                        args.objective,
                        args.mode,
                        args.repetition,
                        args.cpu,
                    )
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
