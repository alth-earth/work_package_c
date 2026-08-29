#!/usr/bin/env python3
"""Research-only real-input qualification for the actual temporal Pareto bridge.

This runner is deliberately separate from the synthetic M14 matrix and from
the formal planner benchmark.  It loads the already frozen 145-frame inputs
through the audited real-input fixture loader, then executes the C-internal
actual-edge Pareto bridge with zero heuristic and no temporal dominance or
state-bound certificate.  The result is qualification evidence only: it
never changes the production planner and never authorizes a candidate.
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
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
MODES = ("one_shot", "slice_restore", "cancelled")
TERMINAL_STATUSES = {status.value for status in NonFifoSearchStatus}
SEGMENTS = ("executable_0_6h", "rolling_0_24h")
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/time_dependent_astar.py",
)
SEARCH_LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}


def _load_point_runner() -> Any:
    """Load the existing frozen real-input loader without importing its CLI."""

    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_real_point_runner_for_pareto", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the audited real-input fixture loader")
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
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _set_cpu_affinity(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    return _load_point_runner()._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(_load_point_runner()._resource_clean(before, after))


def _resource_evidence_complete(record: dict[str, Any], cpu: int) -> bool:
    before = record.get("resources_before") or {}
    after = record.get("resources_after") or {}
    if before.get("cpu_affinity") is None or after.get("cpu_affinity") is None:
        return False
    if cpu >= 0 and (
        before.get("cpu_affinity") != [cpu] or after.get("cpu_affinity") != [cpu]
    ):
        return False
    for snapshot in (before, after):
        cgroup = snapshot.get("cgroup")
        if not isinstance(cgroup, dict) or cgroup.get("memory_events") is None:
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


def _configure_eta_policy(planner: Any, method: str) -> None:
    """Apply only an explicit research ETA policy to the private planner."""

    if method == "bounded":
        planner.eta_policy = EtaRefinementPolicy(method="bounded")
    elif method != "default":
        raise ValueError(f"unsupported research ETA method: {method!r}")


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
    """Compare the research route against the independent point oracle."""

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


def _diagnostic_value(diagnostics: Any, name: str) -> int:
    if diagnostics is None:
        return 0
    if isinstance(diagnostics, dict):
        value = diagnostics.get(name, 0)
    else:
        value = getattr(diagnostics, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    before = _resource_snapshot()
    point = None
    fixture = None
    planner = None
    request = None
    result = None
    identity_error: str | None = None
    planner_error: dict[str, Any] | None = None
    checkpoint: dict[str, Any] = {}
    semantic = None
    frontier: list[dict[str, Any]] = []
    reference = None
    reference_match = None
    search_started = None
    search_elapsed_ms = None
    try:
        _set_cpu_affinity(args.cpu)
        before = _resource_snapshot()
        point = _load_point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(args.objective)
        planner = point._build_planner(fixture, objective)
        _configure_eta_policy(planner, args.eta_method)
        request = replace(
            point._request(fixture, objective),
            use_heuristic=False,
            cancel_check=None,
        )
        search_started = time.perf_counter()
        if args.mode == "one_shot":
            result = run_non_fifo_temporal_pareto_search(
                planner,
                request,
                pareto_pruning=True,
                skip_expected_rejections=True,
            )
        else:
            session = create_non_fifo_temporal_pareto_session(
                planner,
                request,
                pareto_pruning=True,
                skip_expected_rejections=True,
            )
            initial = session.advance(expansion_slice=args.slice_expansions)
            if initial is not None:
                result = initial
                checkpoint = {
                    "reached": False,
                    "state": session.state,
                    "reason": "terminal-before-checkpoint",
                }
            else:
                saved = session.checkpoint()
                checkpoint = {
                    "reached": True,
                    "digest": saved.digest,
                    "session_id": session.session_id,
                    "state": session.state,
                }
                if args.mode == "slice_restore":
                    restored = restore_non_fifo_temporal_pareto_session(
                        planner,
                        request,
                        saved,
                        skip_expected_rejections=True,
                    )
                    checkpoint["restored_session_id"] = restored.session_id
                    result = restored.run()
                else:
                    cancelled = replace(request, cancel_check=lambda: True)
                    restored = restore_non_fifo_temporal_pareto_session(
                        planner,
                        cancelled,
                        saved,
                        skip_expected_rejections=True,
                    )
                    checkpoint["restored_session_id"] = restored.session_id
                    result = restored.advance(expansion_slice=args.slice_expansions)
                    if result is None:
                        raise RuntimeError("cancelled session did not reach a terminal state")
        search_elapsed_ms = (time.perf_counter() - search_started) * 1000.0
    except NonFifoTemporalParetoError as error:
        identity_error = f"{type(error).__name__}:{error}"
    except Exception as error:  # pragma: no cover - child boundary evidence
        planner_error = {"type": type(error).__name__, "message": str(error)}
    else:
        if result is not None and result.status is NonFifoSearchStatus.GOAL_FOUND:
            try:
                if result.selected is None:
                    raise RuntimeError("GOAL_FOUND result has no selected route")
                semantic = _route_payload(result.selected)
                frontier = [_route_payload(route) for route in result.frontier]
                reference = point._reference_search(planner, request)
                reference_match = _reference_matches(semantic, reference)
            except Exception as error:  # reference failure is explicit evidence
                reference_match = False
                planner_error = {
                    "type": type(error).__name__,
                    "message": str(error),
                    "phase": "reference",
                }
    after = _resource_snapshot()
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    status = result.status.value if result is not None else "INVALID/PENDING"
    unexpected_pruning = any(
        _diagnostic_value(diagnostics, name) > 0
        for name in (
            "dominance_checks",
            "dominance_pruned",
            "state_bound_checks",
            "state_bound_pruned",
        )
    )
    if args.mode == "cancelled" and status != NonFifoSearchStatus.CANCELLED.value:
        checkpoint["unexpected_cancel_status"] = True
    stats = _search_stats(result)
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "mode": args.mode,
        "repetition": args.repetition,
        "status": status,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "pareto_pruning": True,
        "skip_expected_rejections": True,
        "eta_method": args.eta_method,
        "session_id": result.session_id if result is not None else checkpoint.get("session_id"),
        "scope_digest": result.scope_digest if result is not None else None,
        "semantic": semantic,
        "semantic_digest": result.semantic_digest if result is not None else None,
        "frontier": frontier if result is not None else [],
        "frontier_digest": result.frontier_digest if result is not None else None,
        "reference": reference,
        "reference_match": reference_match,
        "checkpoint": checkpoint,
        "diagnostics": diagnostics,
        "search_stats": stats,
        "pareto_pruned": stats["pareto_pruned"] if stats else 0,
        "unexpected_pruning": unexpected_pruning,
        "reason": result.reason if result is not None else identity_error,
        "evaluator_errors": list(result.evaluator_errors) if result is not None else [],
        "error_type": None,
        "error_message": result.reason if result is not None else identity_error,
        "planner_error": planner_error,
        "compute_ms": search_elapsed_ms,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(before, after),
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {"files": files, "sha256": _digest(files)}


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _experiment_identity(
    args: argparse.Namespace, fixture: Any, root: Path
) -> dict[str, Any]:
    point = _load_point_runner()
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": _implementation_identity(root),
        "uv_lock": {"path": str(root / "uv.lock"), "sha256": _sha256(root / "uv.lock")},
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
            "frame_digests": [
                _digest(
                    {
                        "risk_id": frame.risk_id,
                        "valid_time": frame.valid_time,
                        "generation_id": frame.generation_id,
                        "content": point.risk_frame_content_digest(frame),
                    }
                )
                for frame in fixture.frames
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
        "adapter_mode": "actual_edge_zero_heuristic_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "pareto_pruning": True,
        "skip_expected_rejections": True,
        "objectives": [objective.value for objective in OBJECTIVES],
        "modes": list(args.modes),
        "repetitions": args.repetitions,
        "slice_expansions": args.slice_expansions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "search_limits": SEARCH_LIMITS,
        "cpu": args.cpu,
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


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
        "--cpu",
        str(args.cpu),
        "--eta-method",
        args.eta_method,
    ]


def _run_child(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    mode: str,
    heartbeat: Path,
) -> dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition, mode),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "repetition": repetition,
            "mode": mode,
            "status": "INVALID/PENDING",
            "planner_error": {"type": type(error).__name__, "message": str(error)},
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
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _record_key(record: dict[str, Any]) -> tuple[str, int, str] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    mode = record.get("mode")
    if (
        not isinstance(objective, str)
        or not isinstance(repetition, int)
        or mode not in MODES
    ):
        return None
    return objective, repetition, mode


def _load_resume_cases(
    output: Path, experiment_id: str
) -> tuple[list[dict[str, Any]], int]:
    records, malformed = _read_jsonl(output / "cases.jsonl")
    by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        if key is None or record.get("schema_version") != SCHEMA_VERSION:
            continue
        if record.get("experiment_id") not in (None, experiment_id):
            raise RuntimeError("resume evidence belongs to another experiment identity")
        if (
            record.get("dominance_policy") != "disabled"
            or record.get("state_bound_policy") != "absent"
        ):
            raise RuntimeError("resume evidence violates the research safety fence")
        if key in by_key:
            raise RuntimeError("resume evidence contains duplicate complete records")
        by_key[key] = record
    return list(by_key.values()), malformed


def _pair_ok(group: dict[str, dict[str, Any]]) -> bool:
    one = group.get("one_shot")
    restored = group.get("slice_restore")
    cancelled = group.get("cancelled")
    if one is None or restored is None or cancelled is None:
        return False
    if restored.get("checkpoint", {}).get("reached") is not True:
        return False
    if cancelled.get("checkpoint", {}).get("reached") is not True:
        return False
    if one.get("status") != restored.get("status"):
        return False
    allowed = {
        NonFifoSearchStatus.GOAL_FOUND.value,
        NonFifoSearchStatus.EXHAUSTED.value,
        NonFifoSearchStatus.RESOURCE_LIMIT.value,
    }
    if one.get("status") not in allowed:
        return False
    if one.get("session_id") != restored.get("session_id"):
        return False
    if one.get("status") == NonFifoSearchStatus.GOAL_FOUND.value:
        if one.get("semantic_digest") != restored.get("semantic_digest"):
            return False
        if one.get("frontier_digest") != restored.get("frontier_digest"):
            return False
        if one.get("reference_match") is not True:
            return False
        if restored.get("reference_match") is not True:
            return False
    elif one.get("semantic") is not None or restored.get("semantic") is not None:
        return False
    if cancelled.get("status") != NonFifoSearchStatus.CANCELLED.value:
        return False
    return (
        cancelled.get("semantic") is None
        and cancelled.get("frontier") == []
        and cancelled.get("reference") is None
    )


def _summary(
    cases: list[dict[str, Any]],
    args: argparse.Namespace,
    ignored: int,
) -> dict[str, Any]:
    expected = len(OBJECTIVES) * len(args.modes) * args.repetitions
    groups: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for case in cases:
        key = _record_key(case)
        if key is not None:
            groups[(key[0], key[1])][key[2]] = case
    complete = (
        tuple(args.modes) == MODES
        and len(cases) == expected
        and len(groups) == len(OBJECTIVES) * args.repetitions
        and ignored == 0
    )
    pair_results = [_pair_ok(group) for group in groups.values()]
    deterministic_by_cell: dict[str, bool] = {}
    for objective in OBJECTIVES:
        for mode in args.modes:
            selected = [
                case
                for case in cases
                if case.get("objective") == objective.value and case.get("mode") == mode
            ]
            signatures = {
                (
                    case.get("status"),
                    case.get("semantic_digest"),
                    case.get("frontier_digest"),
                    case.get("pareto_pruned"),
                    case.get("unexpected_pruning"),
                )
                for case in selected
            }
            deterministic_by_cell[f"{objective.value}:{mode}"] = (
                args.repetitions >= 2
                and len(selected) == args.repetitions
                and len(signatures) == 1
            )
    deterministic = bool(deterministic_by_cell) and all(deterministic_by_cell.values())
    semantic_failure = any(
        case.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and case.get("reference_match") is not True
        for case in cases
    )
    unexpected_pruning = any(case.get("unexpected_pruning") for case in cases)
    resource_clean = all(case.get("resource_clean") is True for case in cases)
    resource_complete = all(
        _resource_evidence_complete(case, args.cpu) for case in cases
    )
    non_goal = any(
        case.get("mode") in {"one_shot", "slice_restore"}
        and case.get("status") != NonFifoSearchStatus.GOAL_FOUND.value
        for case in cases
    )
    terminal = all(
        case.get("status") in TERMINAL_STATUSES | {"TIMEOUT", "INVALID/PENDING"}
        for case in cases
    )
    if semantic_failure or unexpected_pruning:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif not complete or not terminal:
        status = "INVALID/PENDING"
    elif not all(pair_results) or not deterministic:
        status = "NO_PERFORMANCE_PROOF/FAIL"
    elif non_goal or not resource_clean or not resource_complete:
        status = "REAL_INPUT_PARETO_RESOURCE_FAIL"
    else:
        status = "READY_FOR_P0.2-REAL-PARETO-REVIEW"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "expected_case_count": expected,
        "case_count": len(cases),
        "ignored_incomplete_records": ignored,
        "complete": complete,
        "all_pairs_equivalent": bool(pair_results) and all(pair_results),
        "pair_count": len(pair_results),
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "all_reference_match": all(
            case.get("status") != NonFifoSearchStatus.GOAL_FOUND.value
            or case.get("reference_match") is True
            for case in cases
        ),
        "all_goal_searches_completed": not non_goal,
        "unexpected_pruning": unexpected_pruning,
        "pareto_pruned_total": sum(int(case.get("pareto_pruned", 0)) for case in cases),
        "all_resource_clean": resource_clean,
        "resource_evidence_complete": resource_complete,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "pareto_pruning": True,
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
            raise RuntimeError("another real Pareto runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    point = _load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _experiment_identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("real Pareto evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.resume:
                raise RuntimeError("experiment already exists; use --resume to continue")
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
        heartbeat = output / "heartbeat.json"
        cases, ignored = (
            _load_resume_cases(output, identity["experiment_id"])
            if args.resume
            else ([], 0)
        )
        completed = {key for case in cases if (key := _record_key(case)) is not None}
        expected = len(OBJECTIVES) * len(args.modes) * args.repetitions
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    for mode in args.modes:
                        key = (objective.value, repetition, mode)
                        if key in completed:
                            continue
                        record = _run_child(args, objective, repetition, mode, heartbeat)
                        record.setdefault("schema_version", SCHEMA_VERSION)
                        record["experiment_id"] = identity["experiment_id"]
                        if _record_key(record) != key:
                            raise RuntimeError("worker returned a mismatched case identity")
                        cases.append(record)
                        _append_jsonl(output / "cases.jsonl", record)
                        _append_jsonl(output / "resource-frontier.jsonl", record)
                        completed.add(key)
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
        except (KeyboardInterrupt, SystemExit) as error:
            summary = {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "reason": f"runner interrupted: {type(error).__name__}",
                "cases": cases,
            }
            _atomic_json(output / "comparison-summary.json", summary)
            manifest.update(
                {"status": "STOPPED_HARD", "summary": summary, "completed_at": datetime.now(UTC)}
            )
            _atomic_json(manifest_path, manifest)
            _atomic_json(
                heartbeat,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "STOPPED_HARD",
                    "updated_at": datetime.now(UTC),
                },
            )
            (output / "STOPPED_HARD").write_text(summary["reason"] + "\n", encoding="utf-8")
            return 2
        summary = _summary(cases, args, ignored)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {"status": summary["status"], "summary": summary, "completed_at": datetime.now(UTC)}
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
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] != "INVALID/PENDING" else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="executable_0_6h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--slice-expansions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument(
        "--eta-method",
        choices=("default", "bounded"),
        default="bounded",
        help="explicit research ETA policy; formal planner default remains unchanged",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None or args.mode is None:
            raise SystemExit("worker requires --objective and --mode")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    if (
        args.repetitions < 1
        or args.slice_expansions < 1
        or args.worker_timeout_seconds <= 0
        or args.cpu < -1
    ):
        raise SystemExit("repetitions/slice/timeout must be positive and cpu >= -1")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
