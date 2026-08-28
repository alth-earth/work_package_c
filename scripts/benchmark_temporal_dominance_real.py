#!/usr/bin/env python3
"""Research-only P0.1-M1.5 qualification on frozen real RiskFrame windows.

This runner deliberately keeps the real-input experiment separate from the
synthetic M1 runner.  FIFO probing is diagnostic: sampled monotonicity never
authorizes dominance for the continuous exact-arrival search.  The resource
frontier therefore runs with ``TemporalDominancePolicy.disabled()`` only.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import math
import os
import resource
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from arctic_route_planning.config import (
    configuration_digest,
    load_planner_config,
    load_replanning_config,
    load_vessel_model_config,
)
from arctic_route_planning.contracts import (
    CommittedRiskWindow,
    RiskWindowQuery,
    risk_frame_content_digest,
    risk_frame_from_document,
)
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import GeoPoint, ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.temporal_qualification import FifoStatus, qualify_fifo
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0.1-temporal-real-qualification.v1"
OBJECTIVES = tuple(ObjectiveMode)
SEGMENTS = {
    "executable_0_6h": timedelta(hours=6),
    "rolling_0_24h": timedelta(hours=24),
}
DEFAULT_LIMITS = TemporalSearchLimits(
    max_expansions=50_000,
    max_labels=100_000,
    max_queue=50_000,
    max_edge_evaluations=400_000,
)
FIFO_TOLERANCE_SECONDS = 1.0
BASE_PROBE_MINUTES = 15
NEAR_BOUNDARY_SECONDS = 60.0
MAX_REFINEMENT_LEVELS = 4
IMPLEMENTATION_FILES = (
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/_archive/temporal_session.py",
)


@dataclass(frozen=True, slots=True)
class RealFixture:
    commit_path: Path
    route_plan_path: Path
    commit: dict[str, Any]
    frames: tuple[Any, ...]
    grid: RegularGrid
    config_root: Path
    planner_config: Any
    vessel_config: Any
    replanning_config: Any
    start: tuple[int, int]
    goal: tuple[int, int]
    departure: datetime
    segment: str
    input_name: str


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


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
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


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _resource_snapshot() -> dict[str, Any]:
    try:
        host_swap: dict[str, int] = {}
        for line in Path("/proc/vmstat").read_text().splitlines():
            name, raw = line.split()
            if name in {"pswpin", "pswpout"}:
                host_swap[name] = int(raw)
        host_swap_value: dict[str, int] | None = host_swap if len(host_swap) == 2 else None
    except (FileNotFoundError, OSError, ValueError):
        host_swap_value = None
    process_swap = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                process_swap = int(line.split()[1])
                break
    except (FileNotFoundError, OSError, ValueError):
        pass
    cgroup = None
    try:
        relative = next(
            candidate.lstrip("/")
            for hierarchy, controllers, candidate in (
                line.split(":", 2) for line in Path("/proc/self/cgroup").read_text().splitlines()
            )
            if hierarchy == "0" and controllers == ""
        )
        root = Path("/sys/fs/cgroup") / relative

        def scalar(name: str) -> int | str | None:
            try:
                value = (root / name).read_text().strip()
            except OSError:
                return None
            if value == "max":
                return value
            try:
                return int(value)
            except ValueError:
                return None

        try:
            events = {
                key: int(value)
                for key, value in (
                    line.split() for line in (root / "memory.events").read_text().splitlines()
                )
            }
        except (OSError, ValueError):
            events = None
        cgroup = {
            "path": f"/{relative}",
            "memory_current": scalar("memory.current"),
            "memory_peak": scalar("memory.peak"),
            "memory_max": scalar("memory.max"),
            "memory_swap_current": scalar("memory.swap.current"),
            "memory_swap_max": scalar("memory.swap.max"),
            "memory_events": events,
        }
    except (FileNotFoundError, OSError, ValueError, StopIteration):
        pass
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "process_swap_kib": process_swap,
        "host_swap_pages": host_swap_value,
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "max_rss_kib": int(usage.ru_maxrss),
        "cgroup": cgroup,
    }


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if (
        before.get("process_swap_kib") is not None
        and after.get("process_swap_kib") is not None
        and after["process_swap_kib"] > before["process_swap_kib"]
    ):
        return False
    if (
        before.get("host_swap_pages")
        and after.get("host_swap_pages")
        and before["host_swap_pages"] != after["host_swap_pages"]
    ):
        return False
    for snapshot in (before, after):
        cgroup = snapshot.get("cgroup") or {}
        events = cgroup.get("memory_events") or {}
        if any(events.get(key, 0) > 0 for key in ("oom", "oom_kill", "oom_group_kill")):
            return False
        current_swap = cgroup.get("memory_swap_current")
        if isinstance(current_swap, int) and current_swap > 0:
            return False
    return True


def _resource_evidence_complete(record: dict[str, Any], *, cpu: int) -> bool:
    """Require the resource boundary promised by the unattended driver.

    A result collected outside the driver's cgroup is still useful as a
    diagnostic, but it must not be mistaken for a qualified resource point.
    The worker records the cgroup and affinity snapshots so this check remains
    auditable from the persisted JSONL record.
    """

    before = record.get("resources_before") or {}
    after = record.get("resources_after") or {}
    if before.get("cpu_affinity") is None or after.get("cpu_affinity") is None:
        return False
    if cpu >= 0:
        expected = [cpu]
        if before.get("cpu_affinity") != expected or after.get("cpu_affinity") != expected:
            return False
    for snapshot in (before, after):
        cgroup = snapshot.get("cgroup") or {}
        if cgroup.get("memory_max") != 4 * 1024**3:
            return False
        if cgroup.get("memory_swap_max") != 0:
            return False
        if cgroup.get("memory_events") is None:
            return False
    return True


def _set_cpu_affinity(cpu: int | None) -> None:
    """Pin every worker type, including the diagnostic FIFO worker."""

    if cpu is None or cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Read complete JSONL records, ignoring an interrupted trailing line."""

    if not path.exists():
        return [], 0
    records: list[dict[str, Any]] = []
    ignored = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            ignored += 1
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            ignored += 1
    return records, ignored


def _resource_record_key(record: dict[str, Any]) -> tuple[str, int] | None:
    objective = record.get("objective")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or not isinstance(repetition, int):
        return None
    return objective, repetition


def _completed_resource_records(output: Path) -> tuple[list[dict[str, Any]], int]:
    """Load one complete record per cell from resumable JSONL evidence."""

    records_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    ignored = 0
    for path in (output / "resource-frontier.jsonl", output / "cases.jsonl"):
        records, malformed = _read_jsonl(path)
        ignored += malformed
        for record in records:
            key = _resource_record_key(record)
            if key is None:
                continue
            # resource-frontier.jsonl is authoritative when both files contain
            # the same cell; cases.jsonl is the recovery fallback.
            records_by_key.setdefault(key, record)
    return list(records_by_key.values()), ignored


def _existing_fifo_record(output: Path) -> tuple[dict[str, Any] | None, int]:
    """Return the last complete FIFO record, tolerating a torn JSONL line."""

    candidates: list[dict[str, Any]] = []
    ignored = 0
    json_path = output / "fifo-scan.json"
    if json_path.exists():
        try:
            value = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            ignored += 1
        else:
            if isinstance(value, dict):
                candidates.append(value)
            else:
                ignored += 1
    jsonl_records, malformed = _read_jsonl(output / "fifo-scan.jsonl")
    ignored += malformed
    candidates.extend(jsonl_records)
    return (candidates[-1] if candidates else None), ignored


def _append_if_missing(path: Path, record: dict[str, Any], key: tuple[str, int] | None) -> None:
    """Append a recovery record only when its JSONL cell is absent."""

    existing, _ = _read_jsonl(path)
    if key is None:
        if not any(item.get("mode") == record.get("mode") for item in existing):
            _append_jsonl(path, record)
        return
    if not any(_resource_record_key(item) == key for item in existing):
        _append_jsonl(path, record)


def _nearest_rank_p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def _resource_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_objective: dict[str, list[dict[str, Any]]] = {
        objective.value: [] for objective in OBJECTIVES
    }
    for case in cases:
        objective = case.get("objective")
        if objective in by_objective:
            by_objective[objective].append(case)
    metrics: dict[str, Any] = {}
    for objective, objective_cases in by_objective.items():
        compute = [
            float(case["compute_ms"])
            for case in objective_cases
            if case.get("compute_ms") is not None
        ]
        wall = [
            float(case["wall_seconds"])
            for case in objective_cases
            if case.get("wall_seconds") is not None
        ]
        rss = [
            int(case["resources_after"]["max_rss_kib"])
            for case in objective_cases
            if (case.get("resources_after") or {}).get("max_rss_kib") is not None
        ]
        semantic_digests = [case.get("semantic_digest") for case in objective_cases]
        queue_profile: dict[str, int] = {}
        incumbent_pruned = 0
        state_bound_pruned = 0
        eta_failure_reasons: dict[str, int] = {}
        for case in objective_cases:
            diagnostics = case.get("diagnostics") or {}
            incumbent_pruned += int(diagnostics.get("incumbent_pruned", 0))
            state_bound_pruned += int(diagnostics.get("state_bound_pruned", 0))
            for raw_bucket, raw_peak in diagnostics.get(
                "queue_peak_by_elapsed_hour", ()
            ) or ():
                bucket = str(raw_bucket)
                queue_profile[bucket] = max(queue_profile.get(bucket, 0), int(raw_peak))
            for raw_reason, raw_count in diagnostics.get("eta_failure_reasons", ()) or ():
                reason = str(raw_reason)
                eta_failure_reasons[reason] = (
                    eta_failure_reasons.get(reason, 0) + int(raw_count)
                )
        metrics[objective] = {
            "case_count": len(objective_cases),
            "compute_ms": {
                "median": statistics.median(compute) if compute else None,
                "p95": _nearest_rank_p95(compute),
            },
            "wall_seconds": {
                "median": statistics.median(wall) if wall else None,
                "p95": _nearest_rank_p95(wall),
            },
            "rss_kib": {
                "median": statistics.median(rss) if rss else None,
                "peak": max(rss) if rss else None,
            },
            "deterministic": bool(semantic_digests)
            and len(set(semantic_digests)) == 1,
            "semantic_digests": semantic_digests,
            "queue_peak_by_elapsed_hour": queue_profile,
            "incumbent_pruned_total": incumbent_pruned,
            "state_bound_pruned_total": state_bound_pruned,
            "eta_failure_reasons": eta_failure_reasons,
        }
    return metrics


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_fixture(args: argparse.Namespace) -> RealFixture:
    commit_path = Path(args.risk_window_commit).resolve()
    route_path = Path(args.route_plan_set).resolve()
    config_root = Path(args.config_root).resolve()
    commit = json.loads(commit_path.read_text(encoding="utf-8"))
    if commit.get("schema_version") != "bc.risk-window-commit.v1":
        raise ValueError("risk window is not bc.risk-window-commit.v1")
    if (
        commit.get("count") != 145
        or len(commit.get("frames", [])) != 145
        or commit.get("interval_seconds") != 3600
    ):
        raise ValueError("M1.5 requires the complete 145-frame hourly window")
    if commit.get("commit_id") != f"risk-window-sha256-{commit.get('content_digest')}":
        raise ValueError("risk window commit/content identity is inconsistent")
    frames_dir = commit_path.parent.parent / "frames"
    frames = []
    for reference in commit["frames"]:
        risk_id = reference["risk_id"]
        frame_path = frames_dir / f"{risk_id}.json"
        frame_doc = json.loads(frame_path.read_text(encoding="utf-8"))
        frame = risk_frame_from_document(frame_doc)
        digest = risk_frame_content_digest(frame)
        if digest != reference.get("content_digest") or frame.risk_id != risk_id:
            raise ValueError(f"RiskFrame identity mismatch for {risk_id}")
        frames.append(frame)
    if not frames:
        raise ValueError("risk window has no frames")
    valid_times = tuple(frame.valid_time for frame in frames)
    expected_start = datetime.fromisoformat(commit["start"].replace("Z", "+00:00")).astimezone(UTC)
    for index, value in enumerate(valid_times):
        if value != expected_start + timedelta(hours=index):
            raise ValueError("RiskFrame valid_time sequence is not hourly and contiguous")
    query = RiskWindowQuery(
        start=expected_start,
        end=datetime.fromisoformat(commit["end"].replace("Z", "+00:00")).astimezone(UTC),
        interval=timedelta(seconds=int(commit["interval_seconds"])),
        run_id=commit["run_id"],
        scenario_id=commit["scenario_id"],
        corridor_id=commit["corridor_id"],
        generation_id=commit["generation_id"],
        vessel_profile_id=commit["vessel_profile_id"],
        config_digest=commit["config_digest"],
        model_config_digest=commit["model_config_digest"],
        as_of=datetime.fromisoformat(commit["as_of"].replace("Z", "+00:00")).astimezone(UTC),
    )
    committed = CommittedRiskWindow.create(query, tuple(frames))
    if committed.content_digest != commit["content_digest"]:
        raise ValueError("committed window content digest does not match canonical frames")
    scenario_name = str(commit.get("scenario_id", ""))
    if "holdout" in scenario_name:
        input_name = "holdout"
        expected_goals = {"executable_0_6h": (7, 6), "rolling_0_24h": (14, 5)}
    elif "development" in scenario_name:
        input_name = "development"
        expected_goals = {"executable_0_6h": (7, 7), "rolling_0_24h": (14, 6)}
    else:
        raise ValueError("cannot classify frozen input as holdout or development")
    route_doc = json.loads(route_path.read_text(encoding="utf-8"))
    if route_doc.get("schema_version") != "cd.four-layer-route-plan-set.v3":
        raise ValueError("route plan set is not cd.four-layer-route-plan-set.v3")
    layer = next(
        (
            item
            for item in route_doc.get("layers", [])
            if item.get("planning_layer") == args.segment
        ),
        None,
    )
    if layer is None:
        raise ValueError(f"route plan set has no {args.segment} layer")
    for objective in OBJECTIVES:
        plan = layer.get("plans", {}).get(objective.value)
        if not isinstance(plan, dict) or not plan.get("waypoints"):
            raise ValueError(f"{args.segment}/{objective.value} has no waypoints")
        if "latitude" not in plan["waypoints"][-1] or "longitude" not in plan["waypoints"][-1]:
            raise ValueError(f"{args.segment}/{objective.value} has an invalid final waypoint")
    first_frame = frames[0]
    planner_config = load_planner_config(config_root)
    vessel_config = load_vessel_model_config(config_root, first_frame.vessel_profile_id)
    replanning_config = load_replanning_config(config_root)
    grid = RegularGrid.from_risk_frame(first_frame, allow_diagonal=planner_config.connectivity == 8)
    start = (5, 7)
    if not grid.contains(start):
        raise ValueError("frozen start is outside the RiskFrame grid")
    goal = expected_goals[args.segment]
    if not grid.contains(goal):
        raise ValueError("frozen segment goal is outside the RiskFrame grid")
    for objective in OBJECTIVES:
        plan = layer["plans"][objective.value]
        point = grid.nearest_node(
            GeoPoint(
                latitude=float(plan["waypoints"][-1]["latitude"]),
                longitude=float(plan["waypoints"][-1]["longitude"]),
            )
        )
        if point != goal:
            raise ValueError(f"{args.segment}/{objective.value} goal does not match frozen mapping")
    if route_doc.get("start_time"):
        route_start = datetime.fromisoformat(
            route_doc["start_time"].replace("Z", "+00:00")
        ).astimezone(UTC)
        if route_start != expected_start:
            raise ValueError("route plan start_time does not match committed window")
    if commit.get("vessel_profile_id") != vessel_config.vessel_profile_id:
        raise ValueError("vessel profile identity mismatch")
    if commit.get("model_config_digest") != first_frame.model_config_digest:
        raise ValueError("frame model_config_digest mismatch")
    expected_planner_digest = configuration_digest(vessel_config, planner_config, replanning_config)
    if layer["plans"][OBJECTIVES[0].value].get("planner_config_digest") != expected_planner_digest:
        raise ValueError("planner/config digest does not match frozen route plan")
    return RealFixture(
        commit_path=commit_path,
        route_plan_path=route_path,
        commit=commit,
        frames=tuple(frames),
        grid=grid,
        config_root=config_root,
        planner_config=planner_config,
        vessel_config=vessel_config,
        replanning_config=replanning_config,
        start=start,
        goal=goal,
        departure=expected_start,
        segment=args.segment,
        input_name=input_name,
    )


def _build_planner(fixture: RealFixture, objective: ObjectiveMode) -> TemporalLabelAStar:
    sampler = RiskSampler(
        fixture.frames,
        max_frame_gap=timedelta(minutes=fixture.planner_config.max_risk_frame_gap_minutes),
    )
    return TemporalLabelAStar(
        fixture.grid,
        sampler,
        VesselPerformanceModel.from_configuration(fixture.vessel_config),
        planner_config=fixture.planner_config,
        limits=DEFAULT_LIMITS,
    )


def _request(fixture: RealFixture, objective: ObjectiveMode) -> PlanningRequest:
    return PlanningRequest(
        start=fixture.start,
        goal=fixture.goal,
        departure_time=fixture.departure,
        objective=objective,
        maximum_elapsed=SEGMENTS[fixture.segment],
        maximum_risk=1.0,
        max_expansions=DEFAULT_LIMITS.max_expansions,
        time_bucket_size=timedelta(minutes=fixture.planner_config.time_bucket_minutes),
        edge_sample_count=fixture.planner_config.edge_sample_count,
        use_heuristic=True,
    )


def _route_semantic(result: Any) -> dict[str, Any]:
    planning = result.planning_result
    return {
        "objective": planning.objective.value,
        "nodes": [list(node) for node in planning.nodes],
        "total_cost_hours": planning.total_cost_hours,
        "distance_km": planning.distance_km,
        "travel_hours": planning.travel_hours,
        "average_risk": planning.average_risk,
        "maximum_risk": planning.maximum_risk,
        "minimum_confidence": planning.minimum_confidence,
        "source_risk_ids": list(planning.source_risk_ids),
        "steps": [
            {
                "node": list(step.node),
                "eta": step.eta.astimezone(UTC).isoformat(timespec="microseconds"),
                "incoming_heading_degrees": step.incoming_heading_degrees,
                "recommended_speed_knots": step.recommended_speed_knots,
                "edge_distance_km": step.edge_distance_km,
                "edge_risk_score": step.edge_risk_score,
                "edge_maximum_risk": step.edge_maximum_risk,
                "edge_confidence": step.edge_confidence,
                "edge_cost": _jsonable(step.edge_cost),
                "source_risk_ids": list(step.source_risk_ids),
            }
            for step in planning.steps
        ],
    }


def _close(a: Any, b: Any, tolerance: float = 1e-9) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=tolerance)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_close(x, y, tolerance) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[key], b[key], tolerance) for key in a)
    return a == b


def _reference_search(planner: TemporalLabelAStar, request: PlanningRequest) -> dict[str, Any]:
    """Independent zero-heuristic exact-arrival search using the frozen evaluator."""

    started = time.perf_counter()
    start_state = (request.start, None, request.departure_time)
    labels: dict[tuple[Any, Any, datetime], float] = {start_state: 0.0}
    predecessors: dict[tuple[Any, Any, datetime], tuple[tuple[Any, Any, datetime], Any]] = {}
    queue: list[tuple[float, int, tuple[Any, Any, datetime]]] = [(0.0, 0, start_state)]
    serial = 0
    expanded = 0
    edge_evaluations = 0
    queue_peak = 1
    goal_state = None
    cost_model = planner._cost_model(request.objective)
    while queue:
        queued_cost, _, state = heapq.heappop(queue)
        if queued_cost != labels.get(state):
            continue
        expanded += 1
        if expanded > DEFAULT_LIMITS.max_expansions:
            raise RuntimeError("reference exceeded expansions=50000")
        node, heading_code, arrival = state
        if node == request.goal:
            goal_state = state
            break
        previous_heading = planner._previous_heading(node, heading_code)
        for neighbour in planner.grid.neighbors(node):
            edge_evaluations += 1
            if edge_evaluations > DEFAULT_LIMITS.max_edge_evaluations:
                raise RuntimeError("reference exceeded edge_evaluations=400000")
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
            if request.maximum_elapsed is not None and (
                traversal.arrival_time - request.departure_time > request.maximum_elapsed
            ):
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
            if len(labels) > DEFAULT_LIMITS.max_labels:
                raise RuntimeError("reference exceeded labels=100000")
            serial += 1
            heapq.heappush(queue, (next_cost, serial, next_state))
            queue_peak = max(queue_peak, len(queue))
            if queue_peak > DEFAULT_LIMITS.max_queue:
                raise RuntimeError("reference exceeded queue=50000")
    if goal_state is None:
        raise RuntimeError("reference found no route")
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
        "compute_ms": (time.perf_counter() - started) * 1000.0,
    }


def _reference_matches(candidate: dict[str, Any], reference: dict[str, Any]) -> bool:
    if candidate["nodes"] != reference["nodes"]:
        return False
    if [step["eta"] for step in candidate["steps"]] != reference["arrival_times"]:
        return False
    if not _close(candidate["total_cost_hours"], reference["total_cost_hours"]):
        return False
    candidate_edges = candidate["steps"][1:]
    if len(candidate_edges) != len(reference["edge_values"]):
        return False
    fields = (
        ("eta", "arrival_time"),
        ("incoming_heading_degrees", "heading_degrees"),
        ("recommended_speed_knots", "speed_knots"),
        ("edge_distance_km", "distance_km"),
        ("edge_risk_score", "risk_score"),
        ("edge_maximum_risk", "maximum_risk"),
        ("edge_confidence", "confidence"),
        ("edge_cost", "cost"),
        ("source_risk_ids", "source_risk_ids"),
    )
    return all(
        _close(edge[left], ref[right])
        for edge, ref in zip(candidate_edges, reference["edge_values"], strict=True)
        for left, right in fields
    )


def _worker_resource(args: argparse.Namespace) -> dict[str, Any]:
    _set_cpu_affinity(args.cpu)
    fixture = _load_fixture(args)
    objective = ObjectiveMode(args.objective)
    planner = _build_planner(fixture, objective)
    request = _request(fixture, objective)
    before = _resource_snapshot()
    started = time.perf_counter()
    try:
        result = planner.plan(request)
        semantic = _route_semantic(result)
        planner_error = None
    except Exception as error:
        result = None
        semantic = None
        planner_error = {"type": type(error).__name__, "message": str(error)}
    wall_seconds = time.perf_counter() - started
    after = _resource_snapshot()
    reference = None
    reference_error = None
    if result is not None:
        try:
            reference = _reference_search(planner, request)
        except Exception as error:
            reference_error = {"type": type(error).__name__, "message": str(error)}
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if result is not None else "ERROR",
        "mode": "resource-frontier",
        "profile": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective.value,
        "repetition": args.repetition,
        "dominance_policy": "disabled",
        "dominance_pruned": int(getattr(result.diagnostics, "dominance_pruned", 0))
        if result is not None
        else 0,
        "semantic": semantic,
        "semantic_digest": _canonical_digest(semantic) if semantic is not None else None,
        "compute_ms": result.planning_result.metrics.compute_ms if result is not None else None,
        "wall_seconds": wall_seconds,
        "diagnostics": diagnostics,
        "reference": reference,
        "reference_match": (
            semantic is not None
            and reference is not None
            and _reference_matches(semantic, reference)
        ),
        "planner_error": planner_error,
        "reference_error": reference_error,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _resource_clean(before, after),
    }


def _edge_ids(fixture: RealFixture) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    # Qualification starts from the departure-time navigable component.  A
    # later frame becoming open must not expand the domain being claimed for
    # the departure scan; dynamic hard-mask changes are reported by the edge
    # evaluator as uncertain coverage instead.
    values = fixture.frames[0].payload["hard_mask"].transpose("latitude", "longitude").values
    departure_mask = np.asarray(values, dtype=bool)
    component = fixture.grid.connected_component(fixture.start, departure_mask)
    return tuple(
        (node, neighbour)
        for node in sorted(component)
        for neighbour in fixture.grid.neighbors(node)
        if neighbour in component
    )


def _probe_times(fixture: RealFixture) -> tuple[datetime, ...]:
    horizon = SEGMENTS[fixture.segment]
    count = int(horizon.total_seconds() // (BASE_PROBE_MINUTES * 60))
    return tuple(
        fixture.departure + timedelta(minutes=BASE_PROBE_MINUTES * index)
        for index in range(count + 1)
    )


def _fifo_scan(args: argparse.Namespace) -> dict[str, Any]:
    _set_cpu_affinity(args.cpu)
    fixture = _load_fixture(args)
    planner = _build_planner(fixture, ObjectiveMode.FASTEST)
    request = _request(fixture, ObjectiveMode.FASTEST)
    edges = _edge_ids(fixture)
    probes = _probe_times(fixture)
    if not edges:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "FIFO_UNCERTAIN_EVALUATOR_FAILURE",
            "mode": "fifo-scan",
            "fifo_status": FifoStatus.FIFO_UNCERTAIN.value,
            "reason": "empty navigable component",
            "edge_count": 0,
            "probe_count": len(probes),
            "dominance_policy": "disabled",
            "dominance_pruned": 0,
        }
    scope = planner.temporal_scope(request, edge_ids=edges, probe_times=probes)
    cache: dict[tuple[Any, datetime], datetime | None] = {}
    errors: list[dict[str, Any]] = []

    def evaluate(edge: Any, departure: datetime) -> datetime | None:
        key = (tuple(edge), departure)
        if key in cache:
            return cache[key]
        try:
            traversal = planner._evaluate_edge(
                edge[0], edge[1], departure, None, request, planner._cost_model(request.objective)
            )
            value = traversal.arrival_time
        except Exception as error:
            value = None
            failure_class = getattr(error, "failure_class", None)
            errors.append(
                {
                    "edge": [list(edge[0]), list(edge[1])],
                    "departure": departure.astimezone(UTC).isoformat(timespec="microseconds"),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "failure_class": failure_class,
                    "diagnostics": dict(getattr(error, "diagnostics", {})),
                }
            )
        cache[key] = value
        return value

    fifo = qualify_fifo(
        edges,
        probes,
        evaluate,
        tolerance_seconds=FIFO_TOLERANCE_SECONDS,
        scope=scope,
    )
    first_counterexample = fifo.counterexample
    adaptive_insertions = 0
    adaptive_counterexample = None
    for edge in edges:
        times = list(probes)
        for _ in range(MAX_REFINEMENT_LEVELS):
            times.sort()
            insertions: list[datetime] = []
            for earlier, later in pairwise(times):
                earlier_arrival = evaluate(edge, earlier)
                later_arrival = evaluate(edge, later)
                if earlier_arrival is None or later_arrival is None:
                    continue
                slack = (later_arrival - earlier_arrival).total_seconds()
                if slack <= NEAR_BOUNDARY_SECONDS:
                    midpoint = earlier + (later - earlier) / 2
                    if midpoint not in times:
                        insertions.append(midpoint)
                if adaptive_counterexample is None and later_arrival < earlier_arrival - timedelta(
                    seconds=FIFO_TOLERANCE_SECONDS
                ):
                    adaptive_counterexample = {
                        "edge_id": [list(edge[0]), list(edge[1])],
                        "earlier_departure": earlier.astimezone(UTC).isoformat(
                            timespec="microseconds"
                        ),
                        "earlier_arrival": earlier_arrival.astimezone(UTC).isoformat(
                            timespec="microseconds"
                        ),
                        "later_departure": later.astimezone(UTC).isoformat(timespec="microseconds"),
                        "later_arrival": later_arrival.astimezone(UTC).isoformat(
                            timespec="microseconds"
                        ),
                        "slack_seconds": slack,
                    }
            if not insertions:
                break
            times.extend(insertions)
            adaptive_insertions += len(insertions)
    if first_counterexample is not None:
        counterexample = _jsonable(first_counterexample)
    else:
        counterexample = adaptive_counterexample
    status, reason = _diagnostic_fifo_status(
        fifo.status,
        counterexample=counterexample,
        evaluation_errors=bool(errors),
    )
    failure_classes = sorted(
        {
            str(item["failure_class"])
            for item in errors
            if item.get("failure_class") is not None
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "mode": "fifo-scan",
        "fifo_status": fifo.status.value,
        "reason": reason,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "edge_count": len(edges),
        "probe_count": len(probes),
        "evaluations": len(cache),
        "evaluation_errors": len(errors),
        "evaluation_failure_classes": failure_classes,
        "evaluation_error_samples": errors[:20],
        "adaptive_insertions": adaptive_insertions,
        "counterexample": counterexample,
        "fifo_certificate_digest": fifo.digest,
        "scope_digest": scope.digest,
        "coverage_complete": False,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
    }


def _diagnostic_fifo_status(
    fifo_status: FifoStatus,
    *,
    counterexample: Any,
    evaluation_errors: bool,
) -> tuple[str, str]:
    """Map finite probe observations to a deliberately non-authorizing status."""

    if counterexample is not None:
        return FifoStatus.FIFO_VIOLATED.value, "sampled counterexample observed"
    if evaluation_errors:
        return "FIFO_UNCERTAIN_EVALUATOR_FAILURE", "evaluator/coverage incomplete"
    if fifo_status is FifoStatus.FIFO_UNCERTAIN:
        return "FIFO_UNCERTAIN_EVALUATOR_FAILURE", "FIFO classifier could not evaluate the domain"
    return (
        "FIFO_UNCERTAIN_NO_INTERVAL_PROOF",
        "finite probes cannot certify continuous exact arrivals",
    )


def _implementation_identity() -> dict[str, Any]:
    root = _repo_root()
    files = {
        relative: _sha256(root / relative)
        for relative in ("scripts/benchmark_temporal_dominance_real.py", *IMPLEMENTATION_FILES)
    }
    return {"files": files, "sha256": _canonical_digest(files)}


def _experiment_identity(args: argparse.Namespace, fixture: RealFixture) -> dict[str, Any]:
    root = _repo_root()
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": _implementation_identity(),
        "risk_window": {
            "path": str(fixture.commit_path),
            "file_sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_identities": [
                {
                    "risk_id": frame.risk_id,
                    "valid_time": frame.valid_time,
                    "generation_id": frame.generation_id,
                    "content_digest": risk_frame_content_digest(frame),
                }
                for frame in fixture.frames
            ],
        },
        "route_plan_set": {
            "path": str(fixture.route_plan_path),
            "sha256": _sha256(fixture.route_plan_path),
        },
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
        "lock_sha256": _sha256(root / "uv.lock"),
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": list(fixture.start),
            "goal": list(fixture.goal),
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "mode": args.mode,
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "fifo_tolerance_seconds": FIFO_TOLERANCE_SECONDS,
        "probe_interval_minutes": BASE_PROBE_MINUTES,
        "search_limits": asdict(DEFAULT_LIMITS),
        "dominance_policy": "disabled",
    }


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode | None, repetition: int
) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--mode",
        args.mode,
        "--worker-mode",
        "resource" if args.mode == "resource-frontier" else "fifo",
        "--risk-window-commit",
        str(Path(args.risk_window_commit).resolve()),
        "--route-plan-set",
        str(Path(args.route_plan_set).resolve()),
        "--config-root",
        str(Path(args.config_root).resolve()),
        "--segment",
        args.segment,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
    ]
    if objective is not None:
        command.extend(("--objective", objective.value))
    return command


def _run_child(
    args: argparse.Namespace, objective: ObjectiveMode | None, repetition: int, heartbeat: Path
) -> dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        return {"schema_version": SCHEMA_VERSION, "status": "ERROR", "error": str(error)}
    while process.poll() is None:
        elapsed = time.time() - started
        _atomic_json(
            heartbeat,
            {
                "updated_at": datetime.now(UTC),
                "pid": process.pid,
                "elapsed_seconds": elapsed,
                "objective": objective.value if objective else None,
                "repetition": repetition,
            },
        )
        if elapsed > args.worker_timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            return {
                "schema_version": SCHEMA_VERSION,
                "status": "TIMEOUT",
                "objective": objective.value if objective else None,
                "repetition": repetition,
                "stderr": stderr[-4000:],
                "stdout": stdout[-4000:],
            }
        time.sleep(2.0)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "objective": objective.value if objective else None,
            "repetition": repetition,
            "returncode": process.returncode,
            "stderr": stderr[-4000:],
            "stdout": stdout[-4000:],
        }
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "objective": objective.value if objective else None,
            "repetition": repetition,
            "error": "worker did not emit one JSON document",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }


def _annotate_record(record: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    annotated = dict(record)
    annotated.setdefault("experiment_id", identity["experiment_id"])
    return annotated


def _record_identity_matches(record: dict[str, Any], identity: dict[str, Any]) -> bool:
    recorded = record.get("experiment_id")
    return recorded is None or recorded == identity["experiment_id"]


def _resource_summary(
    cases: list[dict[str, Any]],
    *,
    repetitions: int,
    cpu: int,
    ignored_records: int,
) -> dict[str, Any]:
    expected_count = len(OBJECTIVES) * repetitions
    valid = [
        item
        for item in cases
        if item.get("status") == "PASS"
        and item.get("mode") == "resource-frontier"
        and item.get("dominance_policy") == "disabled"
        and item.get("dominance_pruned", 0) == 0
        and item.get("reference_match") is True
        and item.get("resource_clean") is True
        and _resource_evidence_complete(item, cpu=cpu)
    ]
    metrics = _resource_metrics(cases)
    deterministic = all(
        details["case_count"] == repetitions and details["deterministic"]
        for details in metrics.values()
    )
    complete = len(cases) == expected_count and len(valid) == expected_count
    status = "RESOURCE_FRONTIER_PASS" if complete and deterministic else "RESOURCE_FRONTIER_PARTIAL"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "resource-frontier",
        "status": status,
        "expected_case_count": expected_count,
        "case_count": len(cases),
        "valid_case_count": len(valid),
        "all_reference_match": complete
        and all(item.get("reference_match") is True for item in cases),
        "all_resource_clean": complete
        and all(item.get("resource_clean") is True for item in cases),
        "resource_evidence_complete": complete
        and all(_resource_evidence_complete(item, cpu=cpu) for item in cases),
        "deterministic": deterministic,
        "dominance_pruned_total": sum(int(item.get("dominance_pruned", 0)) for item in cases),
        "ignored_incomplete_records": ignored_records,
        "metrics": metrics,
        "cases": cases,
    }


def _run(args: argparse.Namespace) -> int:
    fixture = _load_fixture(args)
    output = Path(args.output_dir).resolve()
    identity = _experiment_identity(args, fixture)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("evidence mode requires a clean implementation worktree")
    identity["experiment_id"] = (
        f"c-p01-m15-real-{fixture.input_name}-{fixture.segment}-{_canonical_digest(identity)[:16]}"
    )
    manifest_path = output / "manifest.json"
    output.mkdir(parents=True, exist_ok=True)
    recorded: dict[str, Any] | None = None
    if manifest_path.exists():
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        if recorded.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match prepared experiment")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "identity": identity,
        "experiment_id": identity["experiment_id"],
    }
    if recorded is not None:
        manifest["resumed_from"] = recorded.get("status")
        manifest["resume_count"] = int(recorded.get("resume_count", 0)) + 1
    _atomic_json(manifest_path, manifest)
    heartbeat = output / "heartbeat.json"
    cases: list[dict[str, Any]] = []
    ignored_records = 0
    if args.mode == "fifo-scan":
        record, ignored_records = _existing_fifo_record(output) if args.resume else (None, 0)
        if record is not None and not _record_identity_matches(record, identity):
            raise RuntimeError("resume FIFO record belongs to another experiment identity")
        if record is None:
            record = _run_child(args, None, 1, heartbeat)
            record = _annotate_record(record, identity)
            _append_jsonl(output / "fifo-scan.jsonl", record)
            _append_jsonl(output / "cases.jsonl", record)
        else:
            record = _annotate_record(record, identity)
            _append_if_missing(output / "fifo-scan.jsonl", record, None)
            _append_if_missing(output / "cases.jsonl", record, None)
        _atomic_json(output / "fifo-scan.json", record)
        summary = record
        status = record.get("status", "ERROR")
    else:
        if args.resume:
            cases, ignored_records = _completed_resource_records(output)
            if any(not _record_identity_matches(item, identity) for item in cases):
                raise RuntimeError("resume resource record belongs to another experiment identity")
            cases = [_annotate_record(item, identity) for item in cases]
            for item in cases:
                _append_if_missing(
                    output / "resource-frontier.jsonl",
                    item,
                    _resource_record_key(item),
                )
        completed_keys = {
            key for item in cases if (key := _resource_record_key(item)) is not None
        }
        for repetition in range(1, args.repetitions + 1):
            order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
            for objective in order:
                key = (objective.value, repetition)
                if key in completed_keys:
                    continue
                record = _run_child(args, objective, repetition, heartbeat)
                record = _annotate_record(record, identity)
                cases.append(record)
                _append_jsonl(output / "resource-frontier.jsonl", record)
                _append_jsonl(output / "cases.jsonl", record)
                completed_keys.add(key)
            _atomic_json(
                heartbeat,
                {
                    "updated_at": datetime.now(UTC),
                    "status": "RUNNING",
                    "completed_cases": len(cases),
                    "expected_cases": len(OBJECTIVES) * args.repetitions,
                },
            )
        summary = _resource_summary(
            cases,
            repetitions=args.repetitions,
            cpu=args.cpu,
            ignored_records=ignored_records,
        )
        status = summary["status"]
    _atomic_json(output / "comparison-summary.json", summary)
    manifest.update({"status": status, "summary": summary, "completed_at": datetime.now(UTC)})
    _atomic_json(manifest_path, manifest)
    _atomic_json(heartbeat, {"updated_at": datetime.now(UTC), "status": status})
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if status
        in {
            "FIFO_UNCERTAIN_NO_INTERVAL_PROOF",
            "FIFO_UNCERTAIN_EVALUATOR_FAILURE",
            "FIFO_VIOLATED",
            "RESOURCE_FRONTIER_PASS",
        }
        else 2
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fifo-scan", "resource-frontier"), required=True)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-mode", choices=("resource", "fifo"), help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.worker_mode == "resource":
            if args.objective is None:
                raise SystemExit("resource worker requires --objective")
            print(json.dumps(_jsonable(_worker_resource(args)), ensure_ascii=False, sort_keys=True))
            return 0
        if args.worker_mode == "fifo":
            print(json.dumps(_jsonable(_fifo_scan(args)), ensure_ascii=False, sort_keys=True))
            return 0
        raise SystemExit("worker requires --worker-mode")
    if args.output_dir is None:
        raise SystemExit("--output-dir is required")
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions must be positive and timeout must be positive")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
