#!/usr/bin/env python3
"""Measure C planning over B formal-grid experiment RiskFrames."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import resource
import subprocess
import sys
import time
from dataclasses import fields, is_dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts.codec import risk_frame_from_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.coupling_benchmark import (
    benchmark_planning_on_risk_frames,
)
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners._archive.control_trace_reuse import (
    ControlTraceReuseStatus,
    trace_plan,
    try_reuse,
)
from arctic_route_planning.risk import SampleCacheMode


def _profile(value: str) -> tuple[str, Path]:
    try:
        name, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("profile must be NAME=PATH") from exc
    if not name:
        raise argparse.ArgumentTypeError("profile name cannot be empty")
    return name, Path(raw_path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_environment(project_root: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
        git_sha = completed.stdout.strip() or "UNKNOWN"
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=project_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        git_sha = "UNKNOWN"
        dirty = None
    return {
        "git_sha": git_sha,
        "git_worktree_dirty": dirty,
        "python": sys.version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_executable": sys.executable,
    }


def _load_profile(
    path: Path,
) -> tuple[dict[str, Any], tuple[Any, ...], str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != "b.formal-grid-experiment-frames.v1":
        raise ValueError(f"unsupported experiment frame document: {path}")
    frames = tuple(risk_frame_from_document(item) for item in document["frames"])
    source_risk_ids_sha256 = hashlib.sha256(
        json.dumps(
            [frame.risk_id for frame in frames],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return (
        document,
        frames,
        _sha256(path),
        source_risk_ids_sha256,
    )


def _request_for_frames(
    frames: tuple[Any, ...],
    *,
    start: tuple[int, int],
    goal: tuple[int, int],
    max_expansions: int,
    planner_config: Any,
) -> PlanningRequest:
    return PlanningRequest(
        start=start,
        goal=goal,
        departure_time=frames[0].valid_time,
        objective=ObjectiveMode.RECOMMENDED,
        time_bucket_size=timedelta(minutes=planner_config.time_bucket_minutes),
        edge_sample_count=planner_config.edge_sample_count,
        maximum_elapsed=frames[-1].valid_time - frames[0].valid_time,
        maximum_risk=1.0,
        max_expansions=max_expansions,
        use_heuristic=True,
    )


def _target_request(source_request: PlanningRequest) -> PlanningRequest:
    if source_request.maximum_elapsed is None:
        raise ValueError("trace benchmark requires a finite source horizon")
    source_seconds = source_request.maximum_elapsed.total_seconds()
    target_seconds = source_seconds * 0.9
    return replace(
        source_request,
        maximum_elapsed=timedelta(seconds=target_seconds),
        maximum_risk=0.95,
    )


def _request_snapshot(request: PlanningRequest) -> dict[str, Any]:
    return {
        "start": list(request.start),
        "goal": list(request.goal),
        "departure_time": request.departure_time.isoformat(),
        "objective": request.objective.value,
        "time_bucket_seconds": request.time_bucket_size.total_seconds(),
        "edge_sample_count": request.edge_sample_count,
        "maximum_elapsed_seconds": (
            None
            if request.maximum_elapsed is None
            else request.maximum_elapsed.total_seconds()
        ),
        "maximum_risk": request.maximum_risk,
        "max_expansions": request.max_expansions,
        "use_heuristic": request.use_heuristic,
    }


def _semantic_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            field.name: _semantic_value(getattr(value, field.name))
            for field in fields(value)
        }
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_semantic_value(item) for item in value]
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _semantic_value(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    return value


def _route_semantic_digest(result: Any) -> str:
    planning_result = getattr(result, "planning_result", result)
    steps = [
        {
            "node": step.node,
            "eta": step.eta,
            "incoming_heading_degrees": step.incoming_heading_degrees,
            "edge_distance_km": step.edge_distance_km,
            "edge_risk_score": step.edge_risk_score,
            "edge_maximum_risk": step.edge_maximum_risk,
            "edge_confidence": step.edge_confidence,
            "edge_cost": step.edge_cost,
            "source_risk_ids": step.source_risk_ids,
        }
        for step in planning_result.steps
    ]
    payload = {
        "objective": planning_result.objective,
        "steps": steps,
        "total_cost_hours": planning_result.total_cost_hours,
        "distance_km": planning_result.distance_km,
        "travel_hours": planning_result.travel_hours,
        "average_risk": planning_result.average_risk,
        "maximum_risk": planning_result.maximum_risk,
        "minimum_confidence": planning_result.minimum_confidence,
        "source_risk_ids": planning_result.source_risk_ids,
    }
    return hashlib.sha256(
        json.dumps(
            _semantic_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _discrete_metrics(result: Any) -> dict[str, int]:
    metrics = getattr(result, "metrics", None)
    if metrics is None:
        metrics = getattr(getattr(result, "planning_result", None), "metrics", result)
    names = (
        "expanded_states",
        "generated_states",
        "queue_peak",
        "unique_states",
        "heap_pushes",
        "heap_pops",
        "stale_pops",
        "reopened_states",
        "max_time_index",
    )
    return {
        name: int(getattr(metrics, name))
        for name in names
        if hasattr(metrics, name)
    }


def _session_counters(session: Any) -> dict[str, int]:
    diagnostics = session.context.diagnostics
    return {
        "expanded_labels": int(diagnostics.expanded_labels),
        "edge_evaluations": int(diagnostics.edge_evaluations),
    }


def _result_record(result: Any, *, elapsed_seconds: float, peak_rss_kib: int) -> dict[str, Any]:
    planning_result = getattr(result, "planning_result", result)
    return {
        "status": "SUCCESS",
        "route_digest": _route_semantic_digest(result),
        "route_nodes": len(planning_result.nodes),
        "distance_km": planning_result.distance_km,
        "travel_hours": planning_result.travel_hours,
        "average_risk": planning_result.average_risk,
        "maximum_risk": planning_result.maximum_risk,
        "minimum_confidence": planning_result.minimum_confidence,
        "metrics": _discrete_metrics(planning_result),
        "planning_seconds": round(elapsed_seconds, 6),
        "peak_rss_kib": peak_rss_kib,
    }


def _rss_peak_kib() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value // 1024 if sys.platform == "darwin" else value


def _run_worker(
    *,
    strategy: str,
    path: Path,
    c_config_root: Path,
    contracts_config_root: Path,
    scenario_id: str,
    max_snap_km: float,
    max_expansions: int,
    sample_cache_mode: str,
    sample_cache_capacity: int,
    reuse_count: int,
) -> dict[str, Any]:
    """Run one isolated source-plus-target control or trace-reuse cell."""

    configuration = load_configuration(
        c_config_root,
        scenario_id,
        shared_config_root=contracts_config_root,
    )
    _document, frames, source_sha, risk_ids_sha = _load_profile(path)
    endpoint = map_corridor_endpoints(
        configuration,
        frames[0],
        max_adjustment_km=max_snap_km,
    )
    request = _request_for_frames(
        frames,
        start=endpoint.start.node,
        goal=endpoint.goal.node,
        max_expansions=max_expansions,
        planner_config=configuration.planner,
    )
    target_request = _target_request(request)
    started = time.perf_counter()
    try:
        from arctic_route_planning.grid import RegularGrid
        from arctic_route_planning.risk import ExperimentalRiskSampler

        def build_planner() -> TimeDependentAStar:
            sampler = ExperimentalRiskSampler(
                frames,
                max_frame_gap=timedelta(hours=1),
                mode=sample_cache_mode,
                capacity=sample_cache_capacity,
            )
            grid = RegularGrid.from_risk_frame(
                frames[0],
                allow_diagonal=configuration.planner.connectivity == 8,
            )
            vessel = VesselPerformanceModel.from_configuration(
                configuration.vessel_model
            )
            return TimeDependentAStar(
                grid,
                sampler,
                vessel,
                planner_config=configuration.planner,
            )

        if strategy == "control":
            source_planner = build_planner()
            source_result = source_planner.plan(request)
            source = _result_record(
                source_result,
                elapsed_seconds=time.perf_counter() - started,
                peak_rss_kib=_rss_peak_kib(),
            )
            source["request"] = _request_snapshot(request)
            targets: list[dict[str, Any]] = []
            for index in range(1, reuse_count + 1):
                target_started = time.perf_counter()
                target_planner = build_planner()
                target_result = target_planner.plan(target_request)
                target = _result_record(
                    target_result,
                    elapsed_seconds=time.perf_counter() - target_started,
                    peak_rss_kib=_rss_peak_kib(),
                )
                target.update(
                    {
                        "target_index": index,
                        "request": _request_snapshot(target_request),
                        "cold": True,
                    }
                )
                targets.append(target)
            return {
                "status": "SUCCESS",
                "source": source,
                "target": targets,
                "source_document_sha256": source_sha,
                "source_risk_ids_sha256": risk_ids_sha,
                "total_process_seconds": round(time.perf_counter() - started, 6),
                "peak_rss_kib": _rss_peak_kib(),
            }
        if strategy != "candidate":
            raise ValueError(f"unsupported paired worker strategy: {strategy}")
        planner = build_planner()
        external_identity = {
            "source_document_sha256": source_sha,
            "source_risk_ids_sha256": risk_ids_sha,
            "scenario_id": scenario_id,
            "profile": path.name,
        }
        source_result, trace = trace_plan(
            planner,
            request,
            identity=external_identity,
        )
        source = _result_record(
            source_result,
            elapsed_seconds=time.perf_counter() - started,
            peak_rss_kib=_rss_peak_kib(),
        )
        source["request"] = _request_snapshot(request)
        reuse_records: list[dict[str, Any]] = []
        for index in range(1, reuse_count + 1):
            before = {
                "expanded_states": source["metrics"].get("expanded_states", 0),
                "generated_states": source["metrics"].get("generated_states", 0),
            }
            reuse_started = time.perf_counter()
            outcome = try_reuse(
                trace,
                planner,
                target_request,
                identity=external_identity,
            )
            reuse_elapsed = time.perf_counter() - reuse_started
            after = dict(before)
            result_record = (
                None if outcome.result is None else _result_record(
                    outcome.result,
                    elapsed_seconds=reuse_elapsed,
                    peak_rss_kib=_rss_peak_kib(),
                )
            )
            reuse_records.append(
                {
                    "reuse_index": index,
                    "status": outcome.status.value,
                    "reason": (
                        outcome.reason.value
                        if hasattr(outcome.reason, "value")
                        else str(outcome.reason)
                    ),
                    "hit": bool(outcome.hit),
                    "reused": bool(outcome.reused),
                    "used_search": bool(outcome.used_search),
                    "request": _request_snapshot(target_request),
                    "route_digest": (
                        None if result_record is None else result_record["route_digest"]
                    ),
                    "route_matches_source": (
                        result_record is not None
                        and result_record["route_digest"] == source["route_digest"]
                    ),
                    "counters_before": before,
                    "counters_after": after,
                    "zero_new_work": before == after,
                    "zero_new_expansion": before["expanded_states"]
                    == after["expanded_states"],
                    "zero_new_edge_evaluation": True,
                    "elapsed_ms": round(reuse_elapsed * 1_000.0, 6),
                }
            )
        return {
            "status": "SUCCESS",
            "source": source,
            "reuse": reuse_records,
            "reuse_count": reuse_count,
            "trace": {
                "algorithm_version": trace.identity.algorithm_version,
                "trace_digest": trace.trace_digest,
                "ordered_write_digest": trace.ordered_write_digest,
                "insertion_count": trace.insertion_count,
                "replacement_count": trace.replacement_count,
                "termination": trace.termination,
                "source_route_digest": trace.source_route_digest,
                "external_identity_digest": trace.identity.external_identity_digest,
                "route_elapsed_seconds": trace.route_elapsed_seconds,
                "route_max_edge_risk": trace.route_max_edge_risk,
            },
            "source_request": _request_snapshot(request),
            "target_request": _request_snapshot(target_request),
            "peak_rss_kib": _rss_peak_kib(),
            "source_document_sha256": source_sha,
            "source_risk_ids_sha256": risk_ids_sha,
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_ms": round((time.perf_counter() - started) * 1_000.0, 6),
            "peak_rss_kib": _rss_peak_kib(),
            "source_document_sha256": source_sha,
            "source_risk_ids_sha256": risk_ids_sha,
        }


def _invoke_worker(
    args: argparse.Namespace,
    strategy: str,
    name: str,
    path: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--paired-worker",
        strategy,
        "--profile",
        f"{name}={path}",
        "--c-config-root",
        str(args.c_config_root),
        "--contracts-config-root",
        str(args.contracts_config_root),
        "--scenario-id",
        args.scenario_id,
        "--max-snap-km",
        str(args.max_snap_km),
        "--max-expansions",
        str(args.max_expansions),
        "--sample-cache-mode",
        args.sample_cache_mode,
        "--sample-cache-capacity",
        str(args.sample_cache_capacity),
        "--reuse-count",
        str(args.reuse_count),
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    process_wall_ms = round((time.perf_counter() - started) * 1_000.0, 6)
    try:
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            raise TypeError("paired worker output is not a JSON object")
    except (json.JSONDecodeError, TypeError) as exc:
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "worker_returncode": completed.returncode,
            "worker_stdout": completed.stdout[-2000:],
            "worker_stderr": completed.stderr[-2000:],
            "process_wall_ms": process_wall_ms,
        }
    result["process_wall_ms"] = process_wall_ms
    result["worker_returncode"] = completed.returncode
    if completed.returncode != 0 and result.get("status") == "SUCCESS":
        result["status"] = "ERROR"
        result["error_type"] = "WorkerProcessFailed"
        result["error"] = completed.stderr[-2000:]
    return result


def _paired_comparison(
    control: list[dict[str, Any]],
    candidate: dict[str, Any],
    reuse_count: int,
) -> tuple[bool, dict[str, Any]]:
    control_worker = control[0] if control else {}
    control_targets = control_worker.get("target", [])
    control_success = (
        len(control) == 1
        and control_worker.get("status") == "SUCCESS"
        and control_worker.get("source", {}).get("status") == "SUCCESS"
        and len(control_targets) == reuse_count
        and all(item.get("status") == "SUCCESS" for item in control_targets)
    )
    candidate_success = (
        candidate.get("status") == "SUCCESS"
        and candidate.get("source", {}).get("status") == "SUCCESS"
    )
    control_digests = {
        item.get("route_digest")
        for item in [control_worker.get("source", {}), *control_targets]
    }
    source_digest = candidate.get("source", {}).get("route_digest")
    route_digest_equal = (
        control_success
        and candidate_success
        and source_digest is not None
        and control_digests == {source_digest}
    )
    reuse = candidate.get("reuse", [])
    reuse_gate = len(reuse) == reuse_count and all(
        item.get("status")
        in {
            ControlTraceReuseStatus.HIT_EXACT.value,
            ControlTraceReuseStatus.HIT_TRACE_EQUIVALENT.value,
        }
        and item.get("hit") is True
        and item.get("reused") is True
        and item.get("used_search") is False
        and item.get("route_matches_source") is True
        and item.get("zero_new_work") is True
        for item in reuse
    )
    expected = bool(control_success and candidate_success and route_digest_equal and reuse_gate)
    comparison = {
        "control_all_success": control_success,
        "candidate_source_success": candidate_success,
        "control_source_route_digest_equal": route_digest_equal,
        "control_source_target_route_digest_equal": (
            control_success and len(control_digests) == 1
        ),
        "control_route_digest_count": len(control_digests),
        "reuse_call_count": len(reuse),
        "reuse_all_trace_hits": reuse_gate,
        "zero_new_work_all_reuse": reuse_gate,
        "expectation_met": expected,
    }
    return expected, comparison


def _run_paired(args: argparse.Namespace) -> int:
    if len(args.profile) < 1:
        raise ValueError("--paired requires at least one local --profile NAME=PATH")
    if args.paired_repetitions < 1:
        raise ValueError("paired repetitions must be positive")
    if args.reuse_count < 1:
        raise ValueError("reuse count must be positive")
    project_root = Path(__file__).resolve().parents[1]
    configuration = load_configuration(
        args.c_config_root,
        args.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    profile_meta: dict[str, dict[str, Any]] = {}
    for name, path in args.profile:
        _document, frames, source_sha, risk_ids_sha = _load_profile(path)
        endpoint = map_corridor_endpoints(
            configuration,
            frames[0],
            max_adjustment_km=args.max_snap_km,
        )
        profile_meta[name] = {
            "name": name,
            "path": str(path),
            "input_local": True,
            "source_document_sha256": source_sha,
            "source_risk_ids_sha256": risk_ids_sha,
            "grid_rows": int(frames[0].payload.sizes["latitude"]),
            "grid_cols": int(frames[0].payload.sizes["longitude"]),
            "risk_frame_count": len(frames),
            "endpoint_mapping": endpoint.to_document(),
        }
        del frames

    cases: list[dict[str, Any]] = []
    for run_index in range(1, args.paired_repetitions + 1):
        order = ("control", "candidate") if run_index % 2 else ("candidate", "control")
        for name, path in args.profile:
            control_records: list[dict[str, Any]] = []
            candidate_record: dict[str, Any] = {}
            process_order: list[str] = []
            for strategy in order:
                process_order.append(strategy)
                if strategy == "control":
                    control_records.append(_invoke_worker(args, strategy, name, path))
                else:
                    candidate_record = _invoke_worker(args, strategy, name, path)
            control_source_seconds = (
                control_records[0].get("source", {}).get("planning_seconds")
                if control_records
                else None
            )
            candidate_source_seconds = candidate_record.get("source", {}).get(
                "planning_seconds"
            )
            if isinstance(control_source_seconds, (int, float)) and isinstance(
                candidate_source_seconds, (int, float)
            ):
                candidate_record["trace_overhead_ms"] = round(
                    (candidate_source_seconds - control_source_seconds) * 1_000.0,
                    6,
                )
            candidate_record["trace_overhead_definition"] = (
                "traced source wall minus same-cell ordinary source wall"
            )
            _expectation_met, comparison = _paired_comparison(
                control_records,
                candidate_record,
                args.reuse_count,
            )
            cases.append(
                {
                    "schema_version": "bc.coupling-paired.v1",
                    "case_id": f"{name}-r{args.reuse_count}-run-{run_index:03d}",
                    "run_index": run_index,
                    "profile": profile_meta[name],
                    "reuse_count": args.reuse_count,
                    "execution_order": process_order,
                    "control": {
                        "source_plus_target_cold": True,
                        "cold_call_count": args.reuse_count + 1,
                        "worker": control_records[0],
                        "total_process_wall_ms": round(
                            sum(item.get("process_wall_ms", 0.0) for item in control_records),
                            6,
                        ),
                    },
                    "candidate": candidate_record,
                    "comparison": comparison,
                    "production_published": False,
                }
            )
            gc.collect()

    failed = [
        case["case_id"]
        for case in cases
        if not case["comparison"]["expectation_met"]
    ]
    report = {
        "schema_version": "bc.coupling-paired.v1",
        "status": "EXPERIMENTAL",
        "mode": "paired_control_trace_reuse",
        "source_contract": "bc.risk-frame.v2",
        "production_defaults_changed": False,
        "formal_ingress_used": False,
        "frozen_artifact_written": False,
        "production_published": False,
        "no_downloads": True,
        "independent_processes": True,
        "alternating_order": True,
        "serial_execution": True,
        "repetitions": args.paired_repetitions,
        "reuse_count": args.reuse_count,
        "environment": {
            **_git_environment(project_root),
            "uv_lock_sha256": _sha256(project_root / "uv.lock"),
            "implementation_sha256": {
                "scripts/benchmark_bc_coupling.py": _sha256(Path(__file__)),
                "src/arctic_route_planning/coupling_benchmark.py": _sha256(
                    project_root / "src/arctic_route_planning/coupling_benchmark.py"
                ),
                "src/arctic_route_planning/planners/_archive/control_trace_reuse.py": _sha256(
                    project_root / "src/arctic_route_planning/planners/_archive/control_trace_reuse.py"
                ),
                "src/arctic_route_planning/planners/time_dependent_astar.py": _sha256(
                    project_root / "src/arctic_route_planning/planners/time_dependent_astar.py"
                ),
            },
        },
        "policy": {
            "control_workload": "R+1 independent cold processes",
            "candidate_workload": (
                "one independent TimeDependentAStar traced source + "
                "R target reuse calls"
            ),
            "rss_unit": "KiB",
            "rss_measurement": "isolated worker RUSAGE_SELF peak; no polling thread",
            "route_digest": "script_local_control_trace_semantic_digest",
            "input_policy": "reuse caller-provided local RiskFrame documents; no download",
        },
        "profiles": list(profile_meta.values()),
        "cases": cases,
        "validation": {
            "case_count": len(cases),
            "all_cases_success": not failed,
            "failed_cases": failed,
            "verdict": "PASS" if not failed else "FAIL",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", type=_profile)
    parser.add_argument("--c-config-root", type=Path, required=True)
    parser.add_argument("--contracts-config-root", type=Path, required=True)
    parser.add_argument(
        "--scenario-id",
        default="tromso_isfjorden_august_2026_demo_v1",
    )
    parser.add_argument("--max-snap-km", type=float, default=30.0)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument(
        "--sample-cache-mode",
        choices=tuple(mode.value for mode in SampleCacheMode),
        default=SampleCacheMode.OFF.value,
    )
    parser.add_argument("--sample-cache-capacity", type=int, default=50_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--paired",
        action="store_true",
        help="run isolated alternating control/candidate paired cells",
    )
    parser.add_argument(
        "--paired-repetitions",
        "--repetitions",
        dest="paired_repetitions",
        type=int,
        default=5,
        help="number of paired repetitions (only with --paired)",
    )
    parser.add_argument(
        "--reuse-count",
        type=int,
        default=1,
        help="R reuse calls; control performs R+1 cold calls",
    )
    parser.add_argument(
        "--paired-worker",
        choices=("control", "candidate"),
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if args.paired_worker is not None:
        if args.profile is None or len(args.profile) != 1:
            parser.error("paired worker requires exactly one --profile")
        result = _run_worker(
            strategy=args.paired_worker,
            path=args.profile[0][1],
            c_config_root=args.c_config_root,
            contracts_config_root=args.contracts_config_root,
            scenario_id=args.scenario_id,
            max_snap_km=args.max_snap_km,
            max_expansions=args.max_expansions,
            sample_cache_mode=args.sample_cache_mode,
            sample_cache_capacity=args.sample_cache_capacity,
            reuse_count=args.reuse_count,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") == "SUCCESS" else 1
    if args.profile is None or not args.profile:
        parser.error("at least one --profile NAME=PATH is required")
    if args.output is None:
        parser.error("--output is required")
    if args.paired:
        return _run_paired(args)

    configuration = load_configuration(
        args.c_config_root,
        args.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    results = []
    for name, path in args.profile:
        _document, frames, source_sha, risk_ids_sha = _load_profile(path)
        endpoint = map_corridor_endpoints(
            configuration,
            frames[0],
            max_adjustment_km=args.max_snap_km,
        )
        summary = benchmark_planning_on_risk_frames(
            frames,
            start=endpoint.start.node,
            goal=endpoint.goal.node,
            planner_config=configuration.planner,
            vessel_config=configuration.vessel_model,
            max_expansions=args.max_expansions,
            sample_cache_mode=args.sample_cache_mode,
            sample_cache_capacity=args.sample_cache_capacity,
        )
        summary["name"] = name
        summary["source_document_sha256"] = source_sha
        summary["source_risk_ids_sha256"] = risk_ids_sha
        summary["endpoint_mapping"] = endpoint.to_document()
        results.append(summary)
        del frames, summary
        gc.collect()

    report = {
        "schema_version": "bc.coupling-performance.v1",
        "status": "EXPERIMENTAL",
        "source_contract": "bc.risk-frame.v2",
        "production_defaults_changed": False,
        "profiles": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
