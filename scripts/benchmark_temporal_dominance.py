#!/usr/bin/env python3
"""Research-only M0 runner for certified temporal-label dominance.

The runner never calls a production ingress or writes a production store.  It
uses the two labelled synthetic profiles already used by C's component
benchmarks and starts a fresh worker for every ``(strategy, objective,
repetition)``.  A candidate is eligible for a PASS only when its exact route
semantics match the baseline, its certificate scope matches, resources remain
clean, and at least one certified label reduction is observed.
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
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import arctic_route_planning.profiling as synthetic_profiling
from arctic_route_planning.contracts.codec import risk_frame_content_digest
from arctic_route_planning.cost import EdgeCostInput, VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid, heading_change_degrees
from arctic_route_planning.planners import PlanningRequest
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners.temporal_qualification import (
    FifoStatus,
    TemporalDominanceCertificate,
    TemporalDominancePolicy,
    qualify_fifo,
)
from arctic_route_planning.planners.time_dependent_astar import _EdgeTraversal
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0.1-temporal-dominance.v1"
OBJECTIVES = tuple(ObjectiveMode)
SYNTHETIC_PROFILES = {
    "small": SyntheticProfileConfig(rows=5, cols=7, frame_count=7),
    "medium": SyntheticProfileConfig(rows=9, cols=13, frame_count=13),
}
_T0 = datetime(2026, 2, 15, tzinfo=UTC)
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_EXPANSIONS = 50_000
_DEFAULT_MAX_LABELS = 100_000
_DEFAULT_MAX_QUEUE = 50_000
_DEFAULT_MAX_EDGE_EVALUATIONS = 400_000
_REGRESSION_CEILING_PERCENT = 5.0
_RSS_RATIO_CEILING = 1.10
_EPSILON = 1e-12
_REGRESSION_METRIC = "compute_ms"
_REGRESSION_METRIC_UNIT = "milliseconds"
_IMPLEMENTATION_FILES = (
    Path(__file__).resolve(),
    Path(__file__).resolve().parents[1]
    / "src/arctic_route_planning/planners/temporal_qualification.py",
    Path(__file__).resolve().parents[1]
    / "src/arctic_route_planning/planners/temporal_label_astar.py",
    Path(__file__).resolve().parents[1]
    / "src/arctic_route_planning/planners/_archive/temporal_session.py",
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _implementation_identity() -> dict[str, Any]:
    """Return the immutable source identity bound to every M0 artifact."""

    repository_root = Path(__file__).resolve().parents[1]
    files: dict[str, str] = {}
    for path in _IMPLEMENTATION_FILES:
        relative = path.relative_to(repository_root).as_posix()
        files[relative] = _sha256_file(path)
    return {
        "files": files,
        "sha256": _canonical_digest(files),
    }


def _fixture_identity(profile_name: str) -> dict[str, Any]:
    """Digest the generated RiskFrame fixture, not just its dimensions."""

    profile = SYNTHETIC_PROFILES[profile_name]
    frames = synthetic_profiling._make_frames(profile)  # type: ignore[attr-defined]
    frame_identity = tuple(
        {
            "risk_id": frame.risk_id,
            "valid_time": frame.valid_time,
            "content_digest": risk_frame_content_digest(frame),
        }
        for frame in frames
    )
    return {
        "profile_config": asdict(profile),
        "frames": frame_identity,
        "sha256": _canonical_digest(frame_identity),
    }


def _config_identity(
    profile_name: str,
    *,
    warmup_runs: int = 1,
    repetitions: int = 10,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return {
        "profile": profile_name,
        "profile_config": asdict(SYNTHETIC_PROFILES[profile_name]),
        "objectives": tuple(mode.value for mode in OBJECTIVES),
        "warmup_runs": warmup_runs,
        "repetitions": repetitions,
        "worker_timeout_seconds": timeout_seconds,
        "regression_metric": _REGRESSION_METRIC,
        "regression_ceiling_percent": _REGRESSION_CEILING_PERCENT,
        "rss_ratio_ceiling": _RSS_RATIO_CEILING,
        "search_limits": {
            "max_expansions": _DEFAULT_MAX_EXPANSIONS,
            "max_labels": _DEFAULT_MAX_LABELS,
            "max_queue": _DEFAULT_MAX_QUEUE,
            "max_edge_evaluations": _DEFAULT_MAX_EDGE_EVALUATIONS,
        },
    }


def _host_swap_pages() -> dict[str, int] | None:
    try:
        values = {}
        for line in Path("/proc/vmstat").read_text().splitlines():
            name, raw = line.split()
            if name in {"pswpin", "pswpout"}:
                values[name] = int(raw)
        return values if set(values) == {"pswpin", "pswpout"} else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _process_swap_kib() -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                return int(line.split()[1])
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def _read_scalar(path: Path) -> int | str | None:
    try:
        value = path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    if value == "max":
        return value
    try:
        return int(value)
    except ValueError:
        return None


def _read_key_values(path: Path) -> dict[str, int] | None:
    try:
        return {
            key: int(value)
            for key, value in (line.split() for line in path.read_text().splitlines())
        }
    except (FileNotFoundError, OSError, ValueError):
        return None


def _cgroup_snapshot() -> dict[str, Any] | None:
    try:
        relative = None
        for line in Path("/proc/self/cgroup").read_text().splitlines():
            hierarchy, controllers, candidate = line.split(":", 2)
            if hierarchy == "0" and controllers == "":
                relative = candidate.lstrip("/")
                break
        if relative is None:
            return None
        root = Path("/sys/fs/cgroup") / relative
        return {
            "path": f"/{relative}",
            "memory_current": _read_scalar(root / "memory.current"),
            "memory_peak": _read_scalar(root / "memory.peak"),
            "memory_max": _read_scalar(root / "memory.max"),
            "memory_swap_current": _read_scalar(root / "memory.swap.current"),
            "memory_swap_max": _read_scalar(root / "memory.swap.max"),
            "memory_events": _read_key_values(root / "memory.events"),
        }
    except (FileNotFoundError, OSError, ValueError):
        return None


def _resource_snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "process_swap_kib": _process_swap_kib(),
        "host_swap_pages": _host_swap_pages(),
        "cpu_affinity": (
            sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
        ),
        "max_rss_kib": int(usage.ru_maxrss),
        "cgroup": _cgroup_snapshot(),
    }


def _atomic_write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(_jsonable(document), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _append_jsonl(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(document), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _edge_duration_hours(
    edge: tuple[tuple[int, int], tuple[int, int]],
    departure_time: datetime,
    *,
    profile: SyntheticProfileConfig,
) -> float:
    """Return a small monotone time-dependent synthetic edge duration."""

    start, end = edge
    row_delta = end[0] - start[0]
    column_delta = end[1] - start[1]
    allowed = (
        (start == (0, 0) and end in {(0, 1), (1, 0)})
        or (start == (1, 0) and end == (0, 0))
        or (start[0] == 0 and row_delta == 0 and column_delta == 1)
        or (
            start[1] == profile.cols - 1
            and row_delta == 1
            and column_delta == 0
        )
    )
    # The profile has one deterministic corridor plus a short two-edge cycle
    # at its origin.  The cycle creates repeated exact arrivals for the
    # dominance candidate without the combinatorial path explosion of a fully
    # connected synthetic grid.  All other neighbours exceed the horizon.
    if not allowed:
        return float(profile.frame_count + 1)
    if (row_delta, column_delta) == (0, 1):
        base = 0.50
    elif (row_delta, column_delta) == (1, 0):
        base = 0.70
    else:
        base = 0.40
    phase_hours = (departure_time - _T0).total_seconds() / 3600.0
    # |d duration / d departure| <= 0.02, so arrival is monotone over the
    # complete finite probe domain while different path orderings can produce
    # distinct exact arrivals at the same node.
    return base + 0.01 * (1.0 + math.sin(phase_hours))


def _edge_ids(grid: RegularGrid) -> tuple[tuple[tuple[int, int], tuple[int, int]], ...]:
    return tuple(
        (node, neighbour)
        for row in range(grid.shape[0])
        for column in range(grid.shape[1])
        for node in ((row, column),)
        for neighbour in grid.neighbors(node)
    )


def _reference_solution(
    grid: RegularGrid,
    request: PlanningRequest,
    profile: SyntheticProfileConfig,
    cost_model: Any,
) -> dict[str, Any]:
    """Independent zero-heuristic exact-arrival Dijkstra for the fixture.

    This function intentionally repeats only the synthetic transition math;
    it does not call a production planner, its edge evaluator, or any
    candidate helper.  The result is a small semantic oracle used by the
    runner's hard gate.
    """

    state_type = tuple[tuple[int, int], tuple[int, int] | None, datetime]
    start: state_type = (request.start, None, request.departure_time)
    labels: dict[state_type, float] = {start: 0.0}
    predecessors: dict[state_type, state_type] = {}
    queue: list[tuple[float, int, int, int, int, datetime, state_type]] = [
        (0.0, request.start[0], request.start[1], 0, 0, request.departure_time, start)
    ]
    serial = 0
    goal_state: state_type | None = None
    while queue:
        cost, _, _, _, _, _, state = heapq.heappop(queue)
        if abs(cost - labels.get(state, float("inf"))) > _EPSILON:
            continue
        node, incoming_code, arrival = state
        if node == request.goal:
            goal_state = state
            break
        previous_heading = None
        if incoming_code is not None:
            previous = node[0] - incoming_code[0], node[1] - incoming_code[1]
            previous_heading = grid.heading_degrees(previous, node)
        for neighbour in grid.neighbors(node):
            edge = (node, neighbour)
            travel_hours = _edge_duration_hours(edge, arrival, profile=profile)
            next_arrival = arrival + timedelta(hours=travel_hours)
            if request.maximum_elapsed is not None and (
                next_arrival - request.departure_time > request.maximum_elapsed
            ):
                continue
            distance = grid.distance_km(node, neighbour)
            heading = grid.heading_degrees(node, neighbour)
            edge_cost = cost_model.evaluate(
                EdgeCostInput(
                    distance_km=distance,
                    travel_hours=travel_hours,
                    risk_score=0.0,
                    confidence=0.9,
                    heading_change_degrees=heading_change_degrees(previous_heading, heading),
                )
            )
            next_state: state_type = (
                neighbour,
                (neighbour[0] - node[0], neighbour[1] - node[1]),
                next_arrival,
            )
            next_cost = cost + edge_cost.total_equivalent_hours
            if next_cost >= labels.get(next_state, float("inf")) - _EPSILON:
                continue
            labels[next_state] = next_cost
            predecessors[next_state] = state
            serial += 1
            heapq.heappush(
                queue,
                (
                    next_cost,
                    neighbour[0],
                    neighbour[1],
                    next_state[1][0] if next_state[1] is not None else 0,
                    next_state[1][1] if next_state[1] is not None else 0,
                    next_arrival,
                    next_state,
                ),
            )
    if goal_state is None:
        raise RuntimeError("reference oracle found no route")
    states = [goal_state]
    current = goal_state
    while current in predecessors:
        current = predecessors[current]
        states.append(current)
    states.reverse()
    heading_degrees = [
        None
        if index == 0
        else grid.heading_degrees(states[index - 1][0], state[0])
        for index, state in enumerate(states)
    ]
    return {
        "nodes": [list(state[0]) for state in states],
        "headings": heading_degrees,
        "arrival_times": [
            state[2].astimezone(UTC).isoformat(timespec="microseconds") for state in states
        ],
        "total_cost_hours": labels[goal_state],
    }


def _build_components(
    profile_name: str,
    objective: ObjectiveMode,
    *,
    with_dominance: bool,
) -> tuple[TemporalLabelAStar, PlanningRequest, dict[str, Any]]:
    profile = SYNTHETIC_PROFILES[profile_name]
    frames = synthetic_profiling._make_frames(profile)  # type: ignore[attr-defined]
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    limits = TemporalSearchLimits(
        max_expansions=_DEFAULT_MAX_EXPANSIONS,
        max_labels=_DEFAULT_MAX_LABELS,
        max_queue=_DEFAULT_MAX_QUEUE,
        max_edge_evaluations=_DEFAULT_MAX_EDGE_EVALUATIONS,
    )
    planner: TemporalLabelAStar

    def evaluate(start, end, departure_time, previous_heading, request, cost_model):
        edge = (start, end)
        hours = _edge_duration_hours(edge, departure_time, profile=profile)
        distance = grid.distance_km(start, end)
        heading = grid.heading_degrees(start, end)
        cost = cost_model.evaluate(
            EdgeCostInput(
                distance_km=distance,
                travel_hours=hours,
                risk_score=0.0,
                confidence=0.9,
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
            risk_score=0.0,
            maximum_risk=0.0,
            confidence=0.9,
            cost=cost,
            source_risk_ids=("synthetic-temporal-dominance",),
        )

    planner = TemporalLabelAStar(
        grid,
        sampler,
        vessel,
        limits=limits,
        edge_evaluator=evaluate,
    )
    request = PlanningRequest(
        start=(0, 0),
        goal=(profile.rows - 1, profile.cols - 1),
        departure_time=_T0,
        objective=objective,
        maximum_elapsed=timedelta(hours=profile.frame_count - 1),
        use_heuristic=False,
    )
    metadata: dict[str, Any] = {
        "profile": profile_name,
        "profile_config": asdict(profile),
        "objective": objective.value,
    }
    if with_dominance:
        edges = _edge_ids(grid)
        probes = tuple(_T0 + timedelta(minutes=15 * index) for index in range(profile.frame_count))
        scope = planner.temporal_scope(request)
        fifo = qualify_fifo(
            edges,
            probes,
            lambda edge, departure: departure
            + timedelta(hours=_edge_duration_hours(edge, departure, profile=profile)),
            scope=scope,
        )
        dominance_certificate = TemporalDominanceCertificate.from_fifo(
            fifo,
            suffix_monotone=True,
            coverage_complete=True,
        )
        planner.dominance_policy = TemporalDominancePolicy.certified_only(
            dominance_certificate
        )
        metadata.update(
            {
                "fifo_status": fifo.status.value,
                "fifo_usable": fifo.usable,
                "fifo_certificate_digest": fifo.digest,
                "fifo_edge_count": len(fifo.edge_ids),
                "fifo_probe_count": len(fifo.probe_times),
                "fifo_minimum_slack_seconds": fifo.minimum_slack_seconds,
                "dominance_certificate_digest": dominance_certificate.digest,
                "dominance_certificate_usable": dominance_certificate.usable,
                "suffix_monotone": dominance_certificate.suffix_monotone,
                "coverage_complete": dominance_certificate.coverage_complete,
                "scope_digest": scope.digest,
            }
        )
    else:
        metadata.update(
            {
                "fifo_status": FifoStatus.FIFO_UNCERTAIN.value,
                "fifo_usable": False,
                "fifo_certificate_digest": None,
                "dominance_certificate_digest": None,
                "dominance_certificate_usable": False,
                "suffix_monotone": False,
                "coverage_complete": False,
                "scope_digest": None,
            }
        )
    return planner, request, metadata


def _route_payload(result: Any) -> dict[str, Any]:
    planning_result = result.planning_result
    return {
        "objective": planning_result.objective.value,
        "nodes": [list(node) for node in planning_result.nodes],
        "total_cost_hours": planning_result.total_cost_hours,
        "distance_km": planning_result.distance_km,
        "travel_hours": planning_result.travel_hours,
        "average_risk": planning_result.average_risk,
        "maximum_risk": planning_result.maximum_risk,
        "minimum_confidence": planning_result.minimum_confidence,
        "source_risk_ids": list(planning_result.source_risk_ids),
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
            for step in planning_result.steps
        ],
    }


def _worker(profile_name: str, objective_name: str, mode: str) -> dict[str, Any]:
    objective = ObjectiveMode(objective_name)
    resources_before = _resource_snapshot()
    planner, request, metadata = _build_components(
        profile_name,
        objective,
        with_dominance=mode == "certified_dominance",
    )
    started = perf_counter()
    try:
        result = planner.plan(request)
    except Exception as error:
        wall_seconds = perf_counter() - started
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "mode": mode,
            "profile": profile_name,
            "objective": objective.value,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "wall_seconds": wall_seconds,
            "metadata": metadata,
            "resources_before": resources_before,
            "resources_after": _resource_snapshot(),
        }
    wall_seconds = perf_counter() - started
    route = _route_payload(result)
    reference = _reference_solution(
        planner.grid,
        request,
        SYNTHETIC_PROFILES[profile_name],
        planner._cost_model(objective),
    )
    candidate_arrivals = [step["eta"] for step in route["steps"]]
    candidate_headings = [
        step["incoming_heading_degrees"] for step in route["steps"]
    ]
    reference_match = (
        route["nodes"] == reference["nodes"]
        and candidate_headings == reference["headings"]
        and candidate_arrivals == reference["arrival_times"]
        and math.isclose(
            float(route["total_cost_hours"]),
            float(reference["total_cost_hours"]),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )
    resources_after = _resource_snapshot()
    diagnostics = _jsonable(result.diagnostics)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "mode": mode,
        "profile": profile_name,
        "objective": objective.value,
        "implementation_sha256": _implementation_identity()["sha256"],
        "semantic_digest": _canonical_digest(route),
        "semantic": route,
        "compute_ms": result.planning_result.metrics.compute_ms,
        "wall_seconds": wall_seconds,
        "expanded_labels": result.diagnostics.expanded_labels,
        "generated_labels": result.diagnostics.generated_labels,
        "label_peak": result.diagnostics.label_peak,
        "queue_peak": result.diagnostics.queue_peak,
        "edge_evaluations": result.diagnostics.edge_evaluations,
        "dominance_pruned": result.diagnostics.dominance_pruned,
        "dominance_checks": result.diagnostics.dominance_checks,
        "dominance_scope_match": result.diagnostics.dominance_scope_match,
        "reference_match": reference_match,
        "reference_semantic_digest": _canonical_digest(reference),
        "diagnostics": diagnostics,
        "metadata": metadata,
        "resources_before": resources_before,
        "resources_after": resources_after,
    }


def _worker_command(profile: str, objective: ObjectiveMode, mode: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--synthetic-profile",
        profile,
        "--objective",
        objective.value,
        "--mode",
        mode,
    ]


def _run_worker(
    profile: str,
    objective: ObjectiveMode,
    mode: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            _worker_command(profile, objective, mode),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "TIMEOUT",
            "mode": mode,
            "profile": profile,
            "objective": objective.value,
            "error_message": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "mode": mode,
            "profile": profile,
            "objective": objective.value,
            "error_message": completed.stderr[-4000:] or completed.stdout[-4000:],
            "returncode": completed.returncode,
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ERROR",
            "mode": mode,
            "profile": profile,
            "objective": objective.value,
            "error_type": type(error).__name__,
            "error_message": "worker did not emit one JSON document",
        }
    return payload


def _swap_clean(worker: dict[str, Any]) -> bool:
    before = worker.get("resources_before", {})
    after = worker.get("resources_after", {})
    process_before = before.get("process_swap_kib")
    process_after = after.get("process_swap_kib")
    if process_before is not None and process_after is not None and process_after > process_before:
        return False
    host_before = before.get("host_swap_pages")
    host_after = after.get("host_swap_pages")
    if host_before and host_after:
        return all(
            host_after.get(key, 0) == host_before.get(key, 0)
            for key in ("pswpin", "pswpout")
        )
    return True


def _resource_clean(worker: dict[str, Any]) -> bool:
    if not _swap_clean(worker):
        return False
    for snapshot_name in ("resources_before", "resources_after"):
        cgroup = worker.get(snapshot_name, {}).get("cgroup")
        if not cgroup:
            continue
        memory_events = cgroup.get("memory_events") or {}
        if any(memory_events.get(key, 0) > 0 for key in ("oom", "oom_kill", "oom_group_kill")):
            return False
        swap_current = cgroup.get("memory_swap_current")
        if isinstance(swap_current, (int, float)) and swap_current > 0:
            return False
    return True


def _resource_evidence_complete(worker: dict[str, Any]) -> bool:
    """Require the per-worker resource contract before evaluating a gate."""

    required = {
        "process_swap_kib",
        "host_swap_pages",
        "cpu_affinity",
        "max_rss_kib",
        "cgroup",
    }
    snapshots = [worker.get(name) for name in ("resources_before", "resources_after")]
    if any(not isinstance(snapshot, dict) for snapshot in snapshots):
        return False
    for snapshot in snapshots:
        if not required.issubset(snapshot):
            return False
        if not isinstance(snapshot["cpu_affinity"], list) or not snapshot["cpu_affinity"]:
            return False
        if not isinstance(snapshot["max_rss_kib"], (int, float)) or snapshot["max_rss_kib"] <= 0:
            return False
        host_swap = snapshot["host_swap_pages"]
        if not isinstance(host_swap, dict) or not all(
            isinstance(host_swap.get(key), int) and host_swap[key] >= 0
            for key in ("pswpin", "pswpout")
        ):
            return False
        process_swap = snapshot["process_swap_kib"]
        if not isinstance(process_swap, int) or process_swap < 0:
            return False
        cgroup = snapshot["cgroup"]
        if not isinstance(cgroup, dict):
            return False
        memory_events = cgroup.get("memory_events")
        if not isinstance(memory_events, dict) or not all(
            isinstance(memory_events.get(key), int)
            for key in ("oom", "oom_kill", "oom_group_kill")
        ):
            return False
    return True


def _resource_affinity_consistent(worker: dict[str, Any]) -> bool:
    before = worker.get("resources_before", {})
    after = worker.get("resources_after", {})
    return before.get("cpu_affinity") == after.get("cpu_affinity")


def _pair_case(
    profile: str,
    objective: ObjectiveMode,
    repetition: int,
    order: tuple[str, str],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    workers: dict[str, dict[str, Any]] = {}
    for mode in order:
        workers[mode] = _run_worker(
            profile,
            objective,
            "baseline" if mode == "baseline" else "certified_dominance",
            timeout_seconds=timeout_seconds,
        )
    baseline = workers["baseline"]
    candidate = workers["candidate"]
    semantic_match = (
        baseline.get("status") == "PASS"
        and candidate.get("status") == "PASS"
        and baseline.get("semantic_digest") == candidate.get("semantic_digest")
    )
    control_compute_ms = baseline.get("compute_ms")
    candidate_compute_ms = candidate.get("compute_ms")
    compute_regression = None
    if (
        isinstance(control_compute_ms, (int, float))
        and isinstance(candidate_compute_ms, (int, float))
        and control_compute_ms > 0
    ):
        compute_regression = (
            (candidate_compute_ms - control_compute_ms) / control_compute_ms * 100.0
        )
    control_wall_seconds = baseline.get("wall_seconds")
    candidate_wall_seconds = candidate.get("wall_seconds")
    wall_regression = None
    if (
        isinstance(control_wall_seconds, (int, float))
        and isinstance(candidate_wall_seconds, (int, float))
        and control_wall_seconds > 0
    ):
        wall_regression = (
            (candidate_wall_seconds - control_wall_seconds) / control_wall_seconds * 100.0
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "objective": objective.value,
        "repetition": repetition,
        "order": list(order),
        "workers": workers,
        "semantic_match": semantic_match,
        # ``regression_percent`` is the governed metric for M0.  Process
        # launch wall time is retained separately because it is dominated by
        # interpreter start-up on the small fixture.
        "regression_metric": _REGRESSION_METRIC,
        "regression_percent": compute_regression,
        "compute_regression_percent": compute_regression,
        "wall_regression_percent": wall_regression,
        "resource_clean": _resource_clean(baseline) and _resource_clean(candidate),
    }


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _mean_ci_95(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    mean = float(statistics.mean(values))
    if len(values) < 2:
        half_width = 0.0
    else:
        half_width = 1.96 * float(statistics.stdev(values)) / math.sqrt(len(values))
    return {
        "mean_percent": mean,
        "half_width_percent": half_width,
        "lower_percent": mean - half_width,
        "upper_percent": mean + half_width,
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    """Return a deterministic nearest-rank percentile."""

    if not values:
        return None
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return float(ordered[rank - 1])


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    regressions = [
        float(case["regression_percent"])
        for case in cases
        if isinstance(case.get("regression_percent"), (int, float))
    ]
    semantic_match = all(case.get("semantic_match") is True for case in cases) if cases else False
    resource_clean = all(case.get("resource_clean") is True for case in cases) if cases else False
    metric_consistent = all(
        case.get("regression_metric", _REGRESSION_METRIC) == _REGRESSION_METRIC
        for case in cases
    ) if cases else False
    candidate_workers = [
        case.get("workers", {}).get("candidate", {})
        for case in cases
    ]
    candidate_pass = (
        all(worker.get("status") == "PASS" for worker in candidate_workers)
        if cases
        else False
    )
    fifo_certified = all(
        worker.get("metadata", {}).get("fifo_status") == FifoStatus.FIFO_CERTIFIED.value
        for worker in candidate_workers
    ) if cases else False
    scope_match = (
        all(worker.get("dominance_scope_match") is True for worker in candidate_workers)
        if cases
        else False
    )
    reference_match = (
        all(worker.get("reference_match") is True for worker in candidate_workers)
        if cases
        else False
    )
    worker_status_clean = all(
        worker.get("status") == "PASS"
        for case in cases
        for worker in case.get("workers", {}).values()
    ) if cases else False
    all_workers = [
        worker
        for case in cases
        for worker in case.get("workers", {}).values()
    ]
    resource_evidence_complete = bool(all_workers) and all(
        _resource_evidence_complete(worker) for worker in all_workers
    )
    resource_affinity_consistent = bool(all_workers) and all(
        _resource_affinity_consistent(worker) for worker in all_workers
    )
    semantic_digests: dict[tuple[str, str, str], set[str]] = {}
    for case in cases:
        for mode, worker in case.get("workers", {}).items():
            digest = worker.get("semantic_digest")
            if isinstance(digest, str):
                key = (str(case.get("profile")), str(case.get("objective")), str(mode))
                semantic_digests.setdefault(key, set()).add(digest)
    deterministic = bool(semantic_digests) and all(
        len(digests) == 1 for digests in semantic_digests.values()
    )
    pruned = sum(int(worker.get("dominance_pruned", 0)) for worker in candidate_workers)
    candidate_rss = [
        float(worker.get("resources_after", {}).get("max_rss_kib"))
        for worker in candidate_workers
        if isinstance(worker.get("resources_after", {}).get("max_rss_kib"), (int, float))
    ]
    baseline_workers = [case.get("workers", {}).get("baseline", {}) for case in cases]
    baseline_rss = [
        float(worker.get("resources_after", {}).get("max_rss_kib"))
        for worker in baseline_workers
        if isinstance(worker.get("resources_after", {}).get("max_rss_kib"), (int, float))
    ]
    rss_ratio = None
    if candidate_rss and baseline_rss and statistics.median(baseline_rss) > 0:
        rss_ratio = statistics.median(candidate_rss) / statistics.median(baseline_rss)
    objective_summaries: dict[str, dict[str, Any]] = {}
    for objective in sorted({str(case.get("objective")) for case in cases}):
        subset = [case for case in cases if str(case.get("objective")) == objective]
        samples = [
            float(case["regression_percent"])
            for case in subset
            if isinstance(case.get("regression_percent"), (int, float))
        ]
        wall_samples = [
            float(case["wall_regression_percent"])
            for case in subset
            if isinstance(case.get("wall_regression_percent"), (int, float))
        ]
        objective_summaries[objective] = {
            "case_count": len(subset),
            "median_regression_percent": _median(samples),
            "p95_regression_percent": _percentile(samples, 0.95),
            "regression_mean_ci_95_percent": _mean_ci_95(samples),
            "wall_median_regression_percent": _median(wall_samples),
            "wall_p95_regression_percent": _percentile(wall_samples, 0.95),
            "candidate_dominance_pruned": sum(
                int(case.get("workers", {}).get("candidate", {}).get("dominance_pruned", 0))
                for case in subset
            ),
            "semantic_identity": all(case.get("semantic_match") is True for case in subset),
            "reference_oracle_match": all(
                case.get("workers", {}).get("candidate", {}).get("reference_match") is True
                for case in subset
            ),
        }
    objective_regression_ok = bool(objective_summaries) and all(
        item["median_regression_percent"] is not None
        and item["median_regression_percent"] <= _REGRESSION_CEILING_PERCENT
        for item in objective_summaries.values()
    )
    gate_checks = {
        "paired_cases_present": bool(cases),
        "candidate_workers_pass": candidate_pass,
        "worker_status_clean": worker_status_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "resource_affinity_consistent": resource_affinity_consistent,
        "semantic_identity": semantic_match,
        "deterministic_semantics": deterministic,
        "fifo_certified": fifo_certified,
        "certificate_usable": all(
            worker.get("metadata", {}).get("dominance_certificate_usable", True)
            is True
            for worker in candidate_workers
        ) if cases else False,
        "dominance_scope_match": scope_match,
        "reference_oracle_match": reference_match,
        "per_objective_regression_le_5pct": objective_regression_ok,
        "regression_metric_compute_ms": metric_consistent,
        "median_regression_le_5pct": (
            bool(regressions)
            and float(statistics.median(regressions)) <= _REGRESSION_CEILING_PERCENT
        ),
        "rss_ratio_le_1_10": rss_ratio is not None and rss_ratio <= _RSS_RATIO_CEILING,
        "resource_clean": resource_clean,
        "observable_label_reduction": pruned > 0,
    }
    hard_gate_checks = {
        key: value
        for key, value in gate_checks.items()
        if key != "observable_label_reduction"
    }
    if not cases or not all(hard_gate_checks.values()):
        verdict = "FAIL"
    elif pruned == 0:
        verdict = "NO_PERFORMANCE_PROOF"
    else:
        verdict = "PASS"
    return {
        "schema_version": SCHEMA_VERSION,
        "gate_verdict": verdict,
        "gate_checks": gate_checks,
        "case_count": len(cases),
        "median_regression_percent": _median(regressions),
        "p95_regression_percent": _percentile(regressions, 0.95),
        "mean_regression_percent": statistics.mean(regressions) if regressions else None,
        "regression_mean_ci_95_percent": _mean_ci_95(regressions),
        "regression_samples": regressions,
        "regression_metric": _REGRESSION_METRIC,
        "regression_metric_unit": _REGRESSION_METRIC_UNIT,
        "wall_regression_samples": [
            float(case["wall_regression_percent"])
            for case in cases
            if isinstance(case.get("wall_regression_percent"), (int, float))
        ],
        "candidate_dominance_pruned": pruned,
        "rss_median_ratio": rss_ratio,
        "profiles": sorted({str(case.get("profile")) for case in cases}),
        "objectives": sorted({str(case.get("objective")) for case in cases}),
        "objective_summaries": objective_summaries,
    }


def _experiment_id(
    profile: str,
    *,
    warmup_runs: int = 1,
    repetitions: int = 10,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    fixture_digest = _fixture_identity(profile)["sha256"][:16]
    implementation_digest = _implementation_identity()["sha256"][:16]
    config_digest = _canonical_digest(
        _config_identity(
            profile,
            warmup_runs=warmup_runs,
            repetitions=repetitions,
            timeout_seconds=timeout_seconds,
        )
    )[:16]
    return (
        f"c-p0.1-temporal-dominance-{profile}-"
        f"{fixture_digest}-{implementation_digest}-{config_digest}"
    )


def _run(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    implementation = _implementation_identity()
    fixture = _fixture_identity(args.synthetic_profile)
    config = _config_identity(
        args.synthetic_profile,
        warmup_runs=args.warmup_runs,
        repetitions=args.repetitions,
        timeout_seconds=args.worker_timeout_seconds,
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": _experiment_id(
            args.synthetic_profile,
            warmup_runs=args.warmup_runs,
            repetitions=args.repetitions,
            timeout_seconds=args.worker_timeout_seconds,
        ),
        "status": "RUNNING",
        "fixture_provenance": "synthetic",
        "authoritative_route": False,
        "profile": args.synthetic_profile,
        "profile_config": asdict(SYNTHETIC_PROFILES[args.synthetic_profile]),
        "implementation_sha256": implementation["sha256"],
        "implementation_files": implementation["files"],
        "fixture_digest": fixture["sha256"],
        "fixture_identity": fixture,
        "config_digest": _canonical_digest(config),
        "config": config,
        "warmup_runs": args.warmup_runs,
        "repetitions": args.repetitions,
        "objectives": [mode.value for mode in OBJECTIVES],
        "worker_isolation": "one-process-per-strategy-objective-repetition",
        "regression_metric": _REGRESSION_METRIC,
        "regression_metric_unit": _REGRESSION_METRIC_UNIT,
    }
    _atomic_write_json(output_dir / "manifest.json", manifest)
    cases: list[dict[str, Any]] = []
    for _ in range(args.warmup_runs):
        for objective in OBJECTIVES:
            order = ("baseline", "candidate")
            _pair_case(
                args.synthetic_profile,
                objective,
                0,
                order,
                timeout_seconds=args.worker_timeout_seconds,
            )
    for repetition in range(1, args.repetitions + 1):
        order = (
            ("baseline", "candidate")
            if repetition % 2
            else ("candidate", "baseline")
        )
        for objective in OBJECTIVES:
            case = _pair_case(
                args.synthetic_profile,
                objective,
                repetition,
                order,
                timeout_seconds=args.worker_timeout_seconds,
            )
            cases.append(case)
            _append_jsonl(output_dir / "cases.jsonl", case)
    summary = _summarize(cases)
    _atomic_write_json(output_dir / "comparison-summary.json", summary)
    manifest.update({"status": summary["gate_verdict"], "summary": summary})
    _atomic_write_json(output_dir / "manifest.json", manifest)
    print(
        json.dumps(
            _jsonable(manifest),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["gate_verdict"] == "PASS" else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic-profile", choices=tuple(SYNTHETIC_PROFILES))
    parser.add_argument("--output-dir")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=_DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--objective", choices=tuple(mode.value for mode in OBJECTIVES))
    parser.add_argument("--mode", choices=("baseline", "certified_dominance"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.worker:
        if args.synthetic_profile is None or args.objective is None or args.mode is None:
            raise SystemExit("worker requires --synthetic-profile, --objective and --mode")
        print(
            json.dumps(
                _worker(args.synthetic_profile, args.objective, args.mode),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.synthetic_profile is None or args.output_dir is None:
        raise SystemExit("--synthetic-profile and --output-dir are required")
    if args.repetitions < 1 or args.warmup_runs < 0 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions must be positive, warmups non-negative, timeout positive")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
