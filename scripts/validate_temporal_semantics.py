#!/usr/bin/env python3
"""Run the small synthetic P0 control/candidate temporal-semantics check.

This entry point is deliberately separate from the formal ingress and from the
test-only reference oracle.  It exercises the production control planner and
the experimental exact-arrival-time candidate on an immutable, static
``5 x 7 x 7`` synthetic RiskFrame fixture.  Results are written only beneath
the caller-provided output directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import resource
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_planning import profiling as synthetic_profiling
from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners._archive.control_trace_reuse import (
    ControlTraceReuseStatus,
    trace_plan,
)
from arctic_route_planning.planners._archive.control_trace_reuse import (
    try_reuse as try_control_trace_reuse,
)
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.planners._archive.temporal_reuse import (
    TemporalReuseStatus,
    certify_session,
    reuse_or_plan,
    route_semantic_digest,
    try_reuse,
)
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0-temporal-semantics.v1"
P1_SCHEMA_VERSION = "c.p1-temporal-session.v1"
P2_SCHEMA_VERSION = "c.p2-temporal-goal-reuse.v1"
P21_SCHEMA_VERSION = "c.p2.1-control-trace-reuse.v1"
FIXTURE_ID = "synthetic-static-5x7x7-v1"
P2_FIXTURE_ID = "synthetic-static-5x7x7-risk04-v1"
OBJECTIVE = ObjectiveMode.RECOMMENDED
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FRAME_COUNT = 7
ROWS = 5
COLS = 7
MAX_ELAPSED = timedelta(hours=6)
TRACE_SLICE_EXPANSIONS = 128
TRACE_REUSE_COUNTS = (1, 4)
_TRACE_PROFILES = (
    {
        "name": "synthetic-static-5x7x7",
        "kind": "small",
        "rows": 5,
        "columns": 7,
        "frame_count": 7,
    },
    {
        "name": "synthetic-profile-9x13x13",
        "kind": "existing_profile",
        "rows": 9,
        "columns": 13,
        "frame_count": 13,
    },
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha(project_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "UNKNOWN"
    value = completed.stdout.strip()
    return value or "UNKNOWN"


def _git_worktree_dirty(project_root: Path) -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return bool(completed.stdout.strip())


def _implementation_sha256(project_root: Path) -> dict[str, str]:
    relative_paths = (
        "scripts/validate_temporal_semantics.py",
        "src/arctic_route_planning/planners/eta_refinement.py",
        "src/arctic_route_planning/planners/temporal_label_astar.py",
        "tests/reference_temporal_oracle.py",
    )
    return {relative: _sha256(project_root / relative) for relative in relative_paths}


def _implementation_sha256_p1(project_root: Path) -> dict[str, str]:
    paths = set(_implementation_sha256(project_root))
    paths.add("src/arctic_route_planning/planners/temporal_session.py")
    paths.add("src/arctic_route_planning/planners/_archive/temporal_session.py")
    return {relative: _sha256(project_root / relative) for relative in sorted(paths)}


def _implementation_sha256_p2(project_root: Path) -> dict[str, str]:
    paths = set(_implementation_sha256_p1(project_root))
    paths.add("src/arctic_route_planning/planners/_archive/temporal_reuse.py")
    return {relative: _sha256(project_root / relative) for relative in sorted(paths)}


def _implementation_sha256_p21(project_root: Path) -> dict[str, str]:
    return {
        "scripts/validate_temporal_semantics.py": _sha256(
            project_root / "scripts/validate_temporal_semantics.py"
        ),
        "src/arctic_route_planning/planners/_archive/control_trace_reuse.py": _sha256(
            project_root / "src/arctic_route_planning/planners/_archive/control_trace_reuse.py"
        ),
        "src/arctic_route_planning/planners/time_dependent_astar.py": _sha256(
            project_root / "src/arctic_route_planning/planners/time_dependent_astar.py"
        ),
        "src/arctic_route_planning/profiling.py": _sha256(
            project_root / "src/arctic_route_planning/profiling.py"
        ),
    }


def _make_frames(
    *, risk_value: float = 0.0, grid_id: str = "p0-static-grid"
) -> tuple[RiskFrame, ...]:
    risk = np.full((ROWS, COLS), risk_value, dtype=np.float32)
    confidence = np.full((ROWS, COLS), 0.9, dtype=np.float32)
    hard_mask = np.zeros((ROWS, COLS), dtype=np.bool_)
    speed_factor = np.ones((ROWS, COLS), dtype=np.float32)
    risk_level = np.full(
        (ROWS, COLS), min(5, int(np.floor(risk_value * 5)) + 1), dtype=np.uint8
    )
    latitudes = np.asarray(tuple(index * 0.05 for index in range(ROWS)), dtype=np.float64)
    longitudes = np.asarray(tuple(index * 0.05 for index in range(COLS)), dtype=np.float64)

    frames: list[RiskFrame] = []
    for index in range(FRAME_COUNT):
        valid_time = T0 + timedelta(hours=index)
        payload = xr.Dataset(
            {
                "risk_score": (("latitude", "longitude"), risk.copy()),
                "risk_level": (("latitude", "longitude"), risk_level.copy()),
                "hard_mask": (("latitude", "longitude"), hard_mask.copy()),
                "confidence": (("latitude", "longitude"), confidence.copy()),
                "environment_speed_factor": (
                    ("latitude", "longitude"),
                    speed_factor.copy(),
                ),
            },
            coords={"latitude": latitudes, "longitude": longitudes},
            attrs={"crs": "EPSG:4326", "grid_id": grid_id},
        )
        source = SourceReference(
            source_id="p0-static-synthetic",
            data_id=None,
            issue_time=None,
            valid_time=valid_time,
            version="v1",
            quality_flag="synthetic",
        )
        frames.append(
            RiskFrame(
                schema_version="bc.risk-frame.v2",
                risk_id=f"p0-risk-{index}",
                run_id="run-00000000-0000-4000-8000-000000000001",
                scenario_id="p0-static-scenario",
                corridor_id="p0-static-corridor",
                vessel_profile_id="p0-static-vessel",
                config_digest="0" * 64,
                model_config_digest="1" * 64,
                generation_id=0,
                valid_time=valid_time,
                as_of_time=T0,
                generated_at=T0,
                model_version="p0-static-risk.v1",
                payload=payload,
                source_summary=(source,),
                provenance=ProvenanceKind.SYNTHETIC,
            )
        )
    return tuple(frames)


def _build_components(
    *, risk_value: float = 0.0, grid_id: str = "p0-static-grid"
) -> tuple[RegularGrid, RiskSampler, VesselPerformanceModel, PlannerConfig]:
    frames = _make_frames(risk_value=risk_value, grid_id=grid_id)
    planner_config = PlannerConfig(connectivity=4, edge_sample_count=3)
    sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return grid, sampler, vessel, planner_config


def _request() -> PlanningRequest:
    return PlanningRequest(
        start=(ROWS // 2, 0),
        goal=(ROWS // 2, COLS - 1),
        departure_time=T0,
        objective=OBJECTIVE,
        time_bucket_size=timedelta(minutes=60),
        edge_sample_count=3,
        maximum_elapsed=MAX_ELAPSED,
        max_expansions=250_000,
        use_heuristic=True,
    )


def _p2_request(
    *,
    maximum_elapsed: timedelta | None = timedelta(hours=8),
    maximum_risk: float | None = 1.0,
) -> PlanningRequest:
    """Build the P2 source/target request envelope without changing P0/P1."""

    return replace(
        _request(),
        maximum_elapsed=maximum_elapsed,
        maximum_risk=maximum_risk,
        use_heuristic=False,
    )


def _candidate_policy() -> EtaRefinementPolicy:
    return EtaRefinementPolicy(
        max_iterations=12,
        absolute_tolerance_seconds=1.0,
        relative_tolerance=1e-6,
        relaxation=0.5,
        history_size=4,
    )


def _candidate_limits() -> TemporalSearchLimits:
    return TemporalSearchLimits(
        max_expansions=50_000,
        max_labels=100_000,
        max_queue=50_000,
        max_edge_evaluations=400_000,
    )


def _round(value: float) -> float:
    return round(float(value), 12)


def _route_snapshot(result: Any) -> dict[str, Any]:
    """Serialize semantic route fields while excluding wall-clock metrics."""

    planning_result = getattr(result, "planning_result", result)
    steps = [
        {
            "node": [int(step.node[0]), int(step.node[1])],
            "eta": step.eta.isoformat(),
            "recommended_speed_knots": (
                None
                if step.recommended_speed_knots is None
                else _round(step.recommended_speed_knots)
            ),
            "edge_distance_km": _round(step.edge_distance_km),
            "edge_risk_score": _round(step.edge_risk_score),
            "edge_maximum_risk": _round(step.edge_maximum_risk),
            "edge_confidence": _round(step.edge_confidence),
            "source_risk_ids": list(step.source_risk_ids),
        }
        for step in planning_result.steps
    ]
    return {
        "objective": planning_result.objective.value,
        "nodes": [item["node"] for item in steps],
        "steps": steps,
        "total_cost_hours": _round(planning_result.total_cost_hours),
        "distance_km": _round(planning_result.distance_km),
        "travel_hours": _round(planning_result.travel_hours),
        "average_risk": _round(planning_result.average_risk),
        "maximum_risk": _round(planning_result.maximum_risk),
        "minimum_confidence": _round(planning_result.minimum_confidence),
        "source_risk_ids": list(planning_result.source_risk_ids),
    }


def _route_digest(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _discrete_metrics(result: Any) -> dict[str, Any]:
    planning_result = getattr(result, "planning_result", result)
    metrics = planning_result.metrics
    return {
        "expanded_states": int(metrics.expanded_states),
        "generated_states": int(metrics.generated_states),
        "rejected_hard_edges": int(metrics.rejected_hard_edges),
        "rejected_risk_edges": int(metrics.rejected_risk_edges),
        "rejected_speed_edges": int(metrics.rejected_speed_edges),
        "rejected_coverage_edges": int(metrics.rejected_coverage_edges),
        "queue_peak": int(metrics.queue_peak),
        "unique_states": int(metrics.unique_states),
        "heap_pushes": int(metrics.heap_pushes),
        "heap_pops": int(metrics.heap_pops),
        "stale_pops": int(metrics.stale_pops),
        "reopened_states": int(metrics.reopened_states),
        "max_time_index": int(metrics.max_time_index),
    }


def _diagnostics(result: Any) -> dict[str, Any]:
    value = getattr(result, "diagnostics", None)
    if value is None:
        return {}
    result_dict = asdict(value)
    reasons = result_dict.get("rejection_reasons")
    if reasons is not None:
        result_dict["rejection_reasons"] = [list(item) for item in reasons]
    return result_dict


def _run_one(
    strategy: str,
    request: PlanningRequest | None = None,
    *,
    risk_value: float = 0.0,
) -> dict[str, Any]:
    grid, sampler, vessel, planner_config = _build_components(risk_value=risk_value)
    request = _request() if request is None else request
    started = time.perf_counter()
    try:
        if strategy == "control":
            planner = TimeDependentAStar(
                grid,
                sampler,
                vessel,
                planner_config=planner_config,
            )
            result = planner.plan(request)
        elif strategy == "candidate":
            planner = TemporalLabelAStar(
                grid,
                sampler,
                vessel,
                planner_config=planner_config,
                limits=_candidate_limits(),
                eta_policy=_candidate_policy(),
            )
            result = planner.plan(request)
        else:  # pragma: no cover - internal caller invariant
            raise ValueError(f"unsupported strategy: {strategy}")
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1_000.0
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "elapsed_ms": _round(elapsed_ms),
        }

    snapshot = _route_snapshot(result)
    metrics = _discrete_metrics(result)
    return {
        "status": "SUCCESS",
        "route_digest": _route_digest(snapshot),
        "route": snapshot,
        "metrics": metrics,
        "diagnostics": _diagnostics(result),
        "planner_compute_ms": _round(result.planning_result.metrics.compute_ms)
        if hasattr(result, "planning_result")
        else _round(result.metrics.compute_ms),
        "elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
    }


_NON_SEMANTIC_KEYS = frozenset(
    {
        "elapsed_ms",
        "planner_compute_ms",
        "compute_ms",
        "started",
        "started_at",
        "finished_at",
        "wall_time_ms",
    }
)


def _canonical_value(value: Any, *, key: str | None = None) -> Any:
    """Convert an internal session/checkpoint object to stable JSON data.

    Checkpoints are intentionally private Python objects, not a published
    schema.  This serializer is only a diagnostic digest helper and excludes
    wall-clock bookkeeping so repeated semantic runs remain comparable.
    """

    if key in _NON_SEMANTIC_KEYS:
        return None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, (timedelta, Path)):
        return str(value)
    if hasattr(value, "value") and isinstance(value.value, (str, int, bool)):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _canonical_value(getattr(value, item.name), key=item.name)
            for item in fields(value)
            if item.name not in _NON_SEMANTIC_KEYS
        }
    if isinstance(value, Mapping):
        return {
            str(item_key): _canonical_value(item_value, key=str(item_key))
            for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))
            if str(item_key) not in _NON_SEMANTIC_KEYS
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        values = [_canonical_value(item) for item in value]
        if isinstance(value, (frozenset, set)):
            return sorted(values, key=lambda item: json.dumps(item, sort_keys=True))
        return values
    if hasattr(value, "__dict__"):
        return _canonical_value(vars(value))
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _session_state(session: Any) -> str:
    state = getattr(session, "state", None)
    value = getattr(state, "value", state)
    return str(value) if value is not None else "UNKNOWN"


def _session_identity(session: Any) -> dict[str, Any]:
    identity = getattr(session, "identity", None)
    if identity is None:
        return {"available": False}
    snapshot = _canonical_value(identity)
    result = {
        "available": True,
        "snapshot": snapshot,
        "digest": _stable_digest(snapshot),
    }
    for name in ("session_id", "identity_digest", "digest"):
        value = getattr(identity, name, None)
        if value is not None:
            result[name] = str(value)
    return result


def _run_session_candidate(expansion_slice: int) -> dict[str, Any]:
    """Run the candidate through repeated pause/checkpoint/restore cycles."""

    grid, sampler, vessel, planner_config = _build_components()
    request = _request()
    planner = TemporalLabelAStar(
        grid,
        sampler,
        vessel,
        planner_config=planner_config,
        limits=_candidate_limits(),
        eta_policy=_candidate_policy(),
    )
    started = time.perf_counter()
    pause_count = 0
    slices: list[int] = []
    checkpoints: list[dict[str, Any]] = []
    try:
        session = planner.create_session(request)
        identity = _session_identity(session)
        result = None
        while result is None:
            result = planner.advance_session(session, expansion_slice=expansion_slice)
            slices.append(expansion_slice)
            if result is not None:
                break
            pause_count += 1
            checkpoint = planner.checkpoint_session(session)
            checkpoint_value = _canonical_value(checkpoint)
            checkpoints.append(
                {
                    "pause_index": pause_count,
                    "state": _session_state(session),
                    "digest": _stable_digest(checkpoint_value),
                }
            )
            session = planner.restore_session(checkpoint, request=request)
            if pause_count > 100_000:
                raise RuntimeError("session validation exceeded pause safety bound")
        snapshot = _route_snapshot(result)
        return {
            "status": "SUCCESS",
            "route_digest": _route_digest(snapshot),
            "route": snapshot,
            "metrics": _discrete_metrics(result),
            "diagnostics": _diagnostics(result),
            "session": {
                "terminal_state": _session_state(session),
                "pause_count": pause_count,
                "expansion_slice": expansion_slice,
                "expansion_slices": slices,
                "identity": identity,
                "checkpoint_count": len(checkpoints),
                "checkpoints": checkpoints,
                "checkpoint_digest": _stable_digest(checkpoints),
                "cumulative_metrics": _discrete_metrics(result),
            },
            "planner_compute_ms": _round(result.planning_result.metrics.compute_ms),
            "elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
        }
    except Exception as exc:
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "session": {
                "pause_count": pause_count,
                "expansion_slice": expansion_slice,
                "expansion_slices": slices,
                "checkpoints": checkpoints,
            },
            "elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
        }


def _semantic_match(control: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if control.get("status") != "SUCCESS" or candidate.get("status") != "SUCCESS":
        return False
    return bool(control.get("route_digest") == candidate.get("route_digest"))


def _run_p1_validation(
    *,
    output_dir: Path,
    repetitions: int,
    expansion_slice: int,
) -> dict[str, Any]:
    if expansion_slice < 1:
        raise ValueError("session expansion slice must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    existing = [
        path.name
        for path in (manifest_path, cases_path)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing P1 validation artifacts: "
            + ", ".join(existing)
        )
    project_root = Path(__file__).resolve().parents[1]
    lock_path = project_root / "uv.lock"
    git_sha = _git_sha(project_root)
    worktree_dirty = _git_worktree_dirty(project_root)
    cases: list[dict[str, Any]] = []

    for index in range(1, repetitions + 1):
        control = _run_one("control")
        candidate = _run_one("candidate")
        session_candidate = _run_session_candidate(expansion_slice)
        route_digests = [
            control.get("route_digest"),
            candidate.get("route_digest"),
            session_candidate.get("route_digest"),
        ]
        all_success = all(
            result.get("status") == "SUCCESS"
            for result in (control, candidate, session_candidate)
        )
        candidate_session_metrics_equal = (
            candidate.get("metrics") == session_candidate.get("metrics")
        )
        candidate_session_diagnostics_equal = (
            candidate.get("diagnostics") == session_candidate.get("diagnostics")
        )
        semantic_match = (
            all_success
            and len(set(route_digests)) == 1
            and candidate_session_metrics_equal
            and candidate_session_diagnostics_equal
        )
        cases.append(
            {
                "schema_version": P1_SCHEMA_VERSION,
                "case_id": f"{FIXTURE_ID}-p1-run-{index:03d}",
                "run_index": index,
                "fixture_id": FIXTURE_ID,
                "control": control,
                "candidate": candidate,
                "session_candidate": session_candidate,
                "comparison": {
                    "semantic_match": semantic_match,
                    "control_candidate_route_digest_equal": (
                        control.get("route_digest") == candidate.get("route_digest")
                    ),
                    "candidate_session_route_digest_equal": (
                        candidate.get("route_digest")
                        == session_candidate.get("route_digest")
                    ),
                    "control_session_route_digest_equal": (
                        control.get("route_digest")
                        == session_candidate.get("route_digest")
                    ),
                    "candidate_session_metrics_equal": (
                        candidate_session_metrics_equal
                    ),
                    "candidate_session_diagnostics_equal": (
                        candidate_session_diagnostics_equal
                    ),
                },
            }
        )

    failed = [
        case["case_id"]
        for case in cases
        if not case["comparison"]["semantic_match"]
    ]
    manifest = {
        "schema_version": P1_SCHEMA_VERSION,
        "status": "EXPERIMENTAL",
        "experiment_id": (
            f"c-p1-temporal-session-v1-{git_sha[:8]}"
            + ("-dirty" if worktree_dirty else "")
        ),
        "fixture_id": FIXTURE_ID,
        "production_defaults_changed": False,
        "formal_ingress_used": False,
        "frozen_artifact_written": False,
        "repetitions": repetitions,
        "serial_execution": True,
        "environment": {
            "git_sha": git_sha,
            "git_worktree_dirty": worktree_dirty,
            "uv_lock_sha256": _sha256(lock_path),
            "implementation_sha256": _implementation_sha256_p1(project_root),
            "python": sys.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_executable": sys.executable,
        },
        "fixture": {
            "rows": ROWS,
            "columns": COLS,
            "frame_count": FRAME_COUNT,
            "start": [ROWS // 2, 0],
            "goal": [ROWS // 2, COLS - 1],
            "departure_time": T0.isoformat(),
            "maximum_elapsed_hours": MAX_ELAPSED.total_seconds() / 3600.0,
            "risk_semantics": "constant_zero",
            "environment_speed_factor": 1.0,
            "provenance": ProvenanceKind.SYNTHETIC.value,
        },
        "policy": {
            "objective": OBJECTIVE.value,
            "connectivity": 4,
            "edge_sample_count": 3,
            "time_bucket_minutes": 60,
            "eta_refinement": asdict(_candidate_policy()),
            "search_limits": asdict(_candidate_limits()),
            "session_slice_expansions": expansion_slice,
            "semantic_digest_excludes": [
                "elapsed_ms",
                "planner_compute_ms",
                "compute_ms",
            ],
        },
        "strategies": {
            "control": {
                "planner": "TimeDependentAStar",
                "state_semantics": "node,time_bucket,heading",
                "role": "formal_control",
            },
            "candidate": {
                "planner": "TemporalLabelAStar",
                "state_semantics": "node,heading,exact_arrival_time",
                "role": "experimental_shadow_one_shot",
            },
            "session_candidate": {
                "planner": "TemporalLabelAStar",
                "state_semantics": "node,heading,exact_arrival_time",
                "role": "experimental_shadow_sliced_restored",
            },
        },
        "discrete_result_fields": [
            "route_digest",
            "route",
            "metrics",
            "diagnostics",
            "session.identity",
            "session.checkpoints",
            "comparison.semantic_match",
        ],
        "validation": {
            "case_count": len(cases),
            "all_cases_success": not failed,
            "failed_cases": failed,
            "verdict": "PASS" if not failed else "FAIL",
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with cases_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    return manifest


def _p2_request_snapshot(request: PlanningRequest) -> dict[str, Any]:
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


def _p2_session_counters(session: Any) -> dict[str, int]:
    diagnostics = session.context.diagnostics
    return {
        "expanded_labels": int(diagnostics.expanded_labels),
        "edge_evaluations": int(diagnostics.edge_evaluations),
    }


def _p2_result_record(result: Any) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "route_digest": route_semantic_digest(result),
        "route": _route_snapshot(result),
        "metrics": _discrete_metrics(result),
        "diagnostics": _diagnostics(result),
    }


def _p2_certificate_record(certified: Any) -> dict[str, Any]:
    certificate = certified.certificate
    return {
        "status": certificate.status.value,
        "open_termination": certificate.open_termination.value,
        "upper_bound": _round(certificate.upper_bound),
        "lower_bound": (
            None
            if certificate.lower_bound is None
            else _round(certificate.lower_bound)
        ),
        "epsilon": _round(certificate.epsilon),
        "state_digest": certificate.state_digest,
        "route_digest": certificate.route_digest,
        "route_elapsed_seconds": _round(certificate.route_elapsed_seconds),
        "route_maximum_risk": _round(certificate.route_maximum_risk),
        "source_constraints": {
            "maximum_elapsed_seconds": (
                None
                if certificate.source_constraints.maximum_elapsed_seconds is None
                else _round(certificate.source_constraints.maximum_elapsed_seconds)
            ),
            "maximum_risk": certificate.source_constraints.maximum_risk,
        },
    }


def _p2_outcome_record(
    outcome: Any,
    *,
    before: dict[str, int],
    after: dict[str, int],
    source_route_digest: str,
) -> dict[str, Any]:
    result = _p2_result_record(outcome.result)
    zero_new_work = (
        after["expanded_labels"] == before["expanded_labels"]
        and after["edge_evaluations"] == before["edge_evaluations"]
    )
    return {
        "status": outcome.status.value,
        "reason": outcome.fallback_reason,
        "hit": bool(outcome.hit),
        "reused": bool(outcome.reused),
        "used_search": bool(outcome.used_search),
        "zero_new_expansion": after["expanded_labels"] == before["expanded_labels"],
        "zero_new_edge_evaluation": (
            after["edge_evaluations"] == before["edge_evaluations"]
        ),
        "zero_new_work": zero_new_work,
        "counters_before": before,
        "counters_after": after,
        "result": result,
        "result_route_matches_source": (
            result is not None and result["route_digest"] == source_route_digest
        ),
    }


def _p2_variants(source_request: PlanningRequest) -> tuple[dict[str, Any], ...]:
    """Return the frozen P2 hit/miss matrix for one source session."""

    return (
        {
            "name": "exact",
            "request": source_request,
            "expected_status": TemporalReuseStatus.HIT_EXACT,
            "expected_reason": "EXACT_IDENTITY",
        },
        {
            "name": "tighter_horizon",
            "request": replace(
                source_request, maximum_elapsed=timedelta(hours=2)
            ),
            "expected_status": TemporalReuseStatus.HIT_MONOTONIC,
            "expected_reason": "MONOTONIC_TIGHTENING",
        },
        {
            "name": "tighter_risk",
            "request": replace(source_request, maximum_risk=0.45),
            "expected_status": TemporalReuseStatus.HIT_MONOTONIC,
            "expected_reason": "MONOTONIC_TIGHTENING",
        },
        {
            "name": "tighter_both",
            "request": replace(
                source_request,
                maximum_elapsed=timedelta(hours=2),
                maximum_risk=0.45,
            ),
            "expected_status": TemporalReuseStatus.HIT_MONOTONIC,
            "expected_reason": "MONOTONIC_TIGHTENING",
        },
        {
            "name": "looser_horizon",
            "request": replace(
                source_request, maximum_elapsed=timedelta(hours=12)
            ),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "CONSTRAINT_WIDENING",
        },
        {
            "name": "looser_risk",
            "request": replace(source_request, maximum_risk=1.0),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "CONSTRAINT_WIDENING",
        },
        {
            "name": "looser_both",
            "request": replace(
                source_request,
                maximum_elapsed=timedelta(hours=12),
                maximum_risk=1.0,
            ),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "CONSTRAINT_WIDENING",
        },
        {
            "name": "incompatible_objective",
            "request": replace(source_request, objective=ObjectiveMode.FASTEST),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "IDENTITY_MISMATCH",
        },
        {
            "name": "route_violates_horizon",
            "request": replace(
                source_request, maximum_elapsed=timedelta(minutes=1)
            ),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "ROUTE_VIOLATES_TARGET",
        },
        {
            "name": "route_violates_risk",
            "request": replace(source_request, maximum_risk=0.3),
            "expected_status": TemporalReuseStatus.MISS_INCOMPATIBLE,
            "expected_reason": "ROUTE_VIOLATES_TARGET",
        },
    )


def _run_p2_case(index: int) -> dict[str, Any]:
    source_request = _p2_request(
        maximum_elapsed=timedelta(hours=8), maximum_risk=0.5
    )
    grid, sampler, vessel, planner_config = _build_components(
        risk_value=0.4, grid_id="p2-risk04-grid"
    )
    planner = TemporalLabelAStar(
        grid,
        sampler,
        vessel,
        planner_config=planner_config,
        limits=_candidate_limits(),
        eta_policy=_candidate_policy(),
    )
    session = planner.create_session(source_request)
    source_result = None
    while source_result is None:
        source_result = planner.advance_session(session)
    certified = certify_session(session)
    source_record = _p2_result_record(source_result)
    assert source_record is not None
    source_route_digest = certified.route_digest
    source_snapshot_digest = _route_digest(_route_snapshot(source_result))

    control = _run_one("control", source_request, risk_value=0.4)
    candidate = _run_one("candidate", source_request, risk_value=0.4)
    variants: list[dict[str, Any]] = []
    for variant in _p2_variants(source_request):
        before = _p2_session_counters(session)
        outcome = try_reuse(certified, planner, variant["request"])
        after = _p2_session_counters(session)
        record = _p2_outcome_record(
            outcome,
            before=before,
            after=after,
            source_route_digest=source_route_digest,
        )
        record.update(
            {
                "name": variant["name"],
                "request": _p2_request_snapshot(variant["request"]),
                "expected_status": variant["expected_status"].value,
                "expected_reason": variant["expected_reason"],
                "expectation_met": (
                    record["status"] == variant["expected_status"].value
                    and record["reason"] == variant["expected_reason"]
                    and (
                        record["hit"]
                        or (
                            record["used_search"] is False
                            and record["result"] is None
                        )
                    )
                    and (
                        record["zero_new_work"]
                        if record["hit"]
                        else True
                    )
                ),
            }
        )
        variants.append(record)

    fallback_planner = TimeDependentAStar(
        grid, sampler, vessel, planner_config=planner_config
    )
    fallback_before = _p2_session_counters(session)
    fallback = reuse_or_plan(
        None,
        planner,
        source_request,
        fallback_planner=fallback_planner,
    )
    fallback_after = _p2_session_counters(session)
    fallback_record = _p2_outcome_record(
        fallback,
        before=fallback_before,
        after=fallback_after,
        source_route_digest=source_route_digest,
    )
    fallback_record.update(
        {
            "expected_status": TemporalReuseStatus.FALLBACK_CONTROL.value,
            "expected_reason": "NO_CERTIFICATE",
            "expectation_met": (
                fallback_record["status"] == TemporalReuseStatus.FALLBACK_CONTROL.value
                and fallback_record["reason"] == "NO_CERTIFICATE"
                and fallback_record["used_search"]
                and fallback_record["result"] is not None
            ),
        }
    )

    comparison = {
        "control_candidate_route_digest_equal": (
            control.get("route_digest") == candidate.get("route_digest")
        ),
        "candidate_source_route_digest_equal": (
            candidate.get("route_digest") == source_snapshot_digest
        ),
        "fallback_control_route_digest_equal": (
            fallback_record["result_route_matches_source"]
        ),
        "all_reuse_variants_expectations_met": all(
            variant["expectation_met"] for variant in variants
        ),
        "fallback_expectation_met": fallback_record["expectation_met"],
    }
    comparison["semantic_match"] = bool(
        control.get("status") == "SUCCESS"
        and candidate.get("status") == "SUCCESS"
        and source_record["route_digest"] == source_route_digest
        and comparison["control_candidate_route_digest_equal"]
        and comparison["candidate_source_route_digest_equal"]
        and comparison["all_reuse_variants_expectations_met"]
        and comparison["fallback_expectation_met"]
    )
    return {
        "schema_version": P2_SCHEMA_VERSION,
        "case_id": f"{P2_FIXTURE_ID}-run-{index:03d}",
        "run_index": index,
        "fixture_id": P2_FIXTURE_ID,
        "source_request": _p2_request_snapshot(source_request),
        "control": control,
        "candidate": candidate,
        "source_session": {
            "terminal_state": _session_state(session),
            "result": source_record,
            "certificate": _p2_certificate_record(certified),
            "counters": _p2_session_counters(session),
        },
        "reuse_variants": variants,
        "fallback_control": fallback_record,
        "comparison": comparison,
    }


def _run_p2_validation(*, output_dir: Path, repetitions: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    existing = [
        path.name
        for path in (manifest_path, cases_path)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing P2 validation artifacts: "
            + ", ".join(existing)
        )

    project_root = Path(__file__).resolve().parents[1]
    lock_path = project_root / "uv.lock"
    git_sha = _git_sha(project_root)
    worktree_dirty = _git_worktree_dirty(project_root)
    cases: list[dict[str, Any]] = []
    for index in range(1, repetitions + 1):
        try:
            cases.append(_run_p2_case(index))
        except Exception as exc:  # keep the sidecar useful on a failed run
            cases.append(
                {
                    "schema_version": P2_SCHEMA_VERSION,
                    "case_id": f"{P2_FIXTURE_ID}-run-{index:03d}",
                    "run_index": index,
                    "fixture_id": P2_FIXTURE_ID,
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "comparison": {"semantic_match": False},
                }
            )

    failed = [
        case["case_id"]
        for case in cases
        if not case.get("comparison", {}).get("semantic_match", False)
    ]
    manifest = {
        "schema_version": P2_SCHEMA_VERSION,
        "status": "EXPERIMENTAL",
        "experiment_id": (
            f"c-p2-temporal-goal-reuse-v1-{git_sha[:8]}"
            + ("-dirty" if worktree_dirty else "")
        ),
        "fixture_id": P2_FIXTURE_ID,
        "production_defaults_changed": False,
        "formal_ingress_used": False,
        "frozen_artifact_written": False,
        "repetitions": repetitions,
        "serial_execution": True,
        "environment": {
            "git_sha": git_sha,
            "git_worktree_dirty": worktree_dirty,
            "uv_lock_sha256": _sha256(lock_path),
            "implementation_sha256": _implementation_sha256_p2(project_root),
            "python": sys.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_executable": sys.executable,
        },
        "fixture": {
            "rows": ROWS,
            "columns": COLS,
            "frame_count": FRAME_COUNT,
            "start": [ROWS // 2, 0],
            "goal": [ROWS // 2, COLS - 1],
            "departure_time": T0.isoformat(),
            "source_maximum_elapsed_hours": 8.0,
            "source_maximum_risk": 0.5,
            "risk_value": 0.4,
            "risk_semantics": "constant_0.4",
            "environment_speed_factor": 1.0,
            "provenance": ProvenanceKind.SYNTHETIC.value,
        },
        "policy": {
            "objective": OBJECTIVE.value,
            "connectivity": 4,
            "edge_sample_count": 3,
            "time_bucket_minutes": 60,
            "eta_refinement": asdict(_candidate_policy()),
            "search_limits": asdict(_candidate_limits()),
            "certificate_epsilon": 1e-12,
            "hit_requires_zero_new_expansion_and_edge_evaluation": True,
            "semantic_digest_excludes": [
                "elapsed_ms",
                "planner_compute_ms",
                "compute_ms",
            ],
        },
        "matrix": [
            {
                "name": variant["name"],
                "expected_status": variant["expected_status"].value,
                "expected_reason": variant["expected_reason"],
            }
            for variant in _p2_variants(
                _p2_request(
                    maximum_elapsed=timedelta(hours=8), maximum_risk=0.5
                )
            )
        ],
        "strategies": {
            "control": {
                "planner": "TimeDependentAStar",
                "role": "formal_control",
            },
            "candidate": {
                "planner": "TemporalLabelAStar",
                "role": "experimental_temporal_session_source",
            },
            "certified_reuse": {
                "api": "certify_session + try_reuse",
                "role": "experimental_no_search_reuse",
            },
            "fallback_control": {
                "api": "reuse_or_plan(fallback_planner=TimeDependentAStar)",
                "role": "explicit_scratch_control",
            },
        },
        "discrete_result_fields": [
            "source_session.certificate",
            "reuse_variants.status",
            "reuse_variants.reason",
            "reuse_variants.zero_new_expansion",
            "reuse_variants.zero_new_edge_evaluation",
            "comparison.semantic_match",
        ],
        "validation": {
            "case_count": len(cases),
            "all_cases_success": not failed,
            "failed_cases": failed,
            "verdict": "PASS" if not failed else "FAIL",
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with cases_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    return manifest


def _trace_profile_frames(profile: Mapping[str, Any]) -> tuple[RiskFrame, ...]:
    """Return one of the frozen synthetic M0 inputs without downloading data.

    The larger profile deliberately calls the package's existing synthetic
    profile generator.  Keeping that generator as the source avoids silently
    introducing a second 9 x 13 x 13 fixture whose semantics only look like
    the documented profile.
    """

    if profile["kind"] == "small":
        return _make_frames(grid_id="p2.1-small-grid")
    if profile["kind"] == "existing_profile":
        return synthetic_profiling._make_frames(  # type: ignore[attr-defined]
            SyntheticProfileConfig(
                rows=int(profile["rows"]),
                cols=int(profile["columns"]),
                frame_count=int(profile["frame_count"]),
            )
        )
    raise ValueError(f"unsupported P2.1 synthetic profile kind: {profile['kind']}")


def _trace_components(
    profile: Mapping[str, Any],
) -> tuple[
    tuple[RiskFrame, ...],
    RegularGrid,
    RiskSampler,
    VesselPerformanceModel,
    PlannerConfig,
    PlanningRequest,
]:
    frames = _trace_profile_frames(profile)
    planner_config = PlannerConfig(connectivity=4, edge_sample_count=3)
    sampler = RiskSampler(frames, max_frame_gap=timedelta(hours=1))
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    request = replace(
        _request(),
        departure_time=frames[0].valid_time,
        start=(int(profile["rows"]) // 2, 0),
        goal=(int(profile["rows"]) // 2, int(profile["columns"]) - 1),
        maximum_elapsed=timedelta(hours=len(frames) - 1),
        maximum_risk=1.0,
        time_bucket_size=timedelta(minutes=planner_config.time_bucket_minutes),
        edge_sample_count=planner_config.edge_sample_count,
    )
    return frames, grid, sampler, vessel, planner_config, request


def _trace_target_request(source_request: PlanningRequest) -> PlanningRequest:
    if source_request.maximum_elapsed is None:
        raise ValueError("P2.1 trace mode requires a finite source horizon")
    return replace(
        source_request,
        maximum_elapsed=source_request.maximum_elapsed * 0.9,
        maximum_risk=0.95,
    )


def _trace_external_identity(profile: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "fixture_id": str(profile["name"]),
        "scenario_id": "c-p2.1-synthetic",
        "profile": dict(profile),
    }


def _rss_peak_kib() -> int | None:
    """Return process peak RSS in KiB where the host exposes it."""

    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (AttributeError, OSError, ValueError):
        return None
    # Linux reports KiB; macOS reports bytes.  The runner is normally Linux,
    # but recording an explicit unit is safer than silently mixing the two.
    if sys.platform == "darwin":
        return value // 1024
    return value


def _trace_result_record(
    result: Any,
    *,
    trace_route_digest: str | None = None,
) -> dict[str, Any]:
    snapshot = _route_snapshot(result)
    planning_result = getattr(result, "planning_result", result)
    record = {
        "status": "SUCCESS",
        "route_digest": _route_digest(snapshot),
        "route_snapshot_digest": _route_digest(snapshot),
        "route": snapshot,
        "metrics": _discrete_metrics(result),
        "diagnostics": _diagnostics(result),
        "planner_compute_ms": _round(planning_result.metrics.compute_ms),
    }
    if trace_route_digest is not None:
        record["trace_route_digest"] = trace_route_digest
    return record


def _trace_error_record(exc: Exception, started: float) -> dict[str, Any]:
    return {
        "status": "ERROR",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
    }


def _run_trace_control(
    profile: Mapping[str, Any],
    request: PlanningRequest,
    target_request: PlanningRequest,
    reuse_count: int,
) -> dict[str, Any]:
    """Run one source and R deliberately cold target control searches."""

    started = time.perf_counter()
    try:
        _frames, grid, sampler, vessel, planner_config, _ = _trace_components(profile)
        source_planner = TimeDependentAStar(
            grid,
            sampler,
            vessel,
            planner_config=planner_config,
        )
        source_started = time.perf_counter()
        source_result = source_planner.plan(request)
        source = _trace_result_record(source_result)
        source["request"] = _p2_request_snapshot(request)
        source["elapsed_ms"] = _round((time.perf_counter() - source_started) * 1_000.0)
        targets: list[dict[str, Any]] = []
        for target_index in range(1, reuse_count + 1):
            target_frames, target_grid, target_sampler, target_vessel, target_config, _ = (
                _trace_components(profile)
            )
            target_planner = TimeDependentAStar(
                target_grid,
                target_sampler,
                target_vessel,
                planner_config=target_config,
            )
            target_started = time.perf_counter()
            target_result = target_planner.plan(target_request)
            target = _trace_result_record(target_result)
            target.update(
                {
                    "target_index": target_index,
                    "request": _p2_request_snapshot(target_request),
                    "elapsed_ms": _round(
                        (time.perf_counter() - target_started) * 1_000.0
                    ),
                    "cold": True,
                }
            )
            targets.append(target)
            del target_frames
        return {
            "status": "SUCCESS",
            "source": source,
            "target": targets,
            "source_plus_target_count": reuse_count + 1,
            "total_elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
            "peak_rss_kib": _rss_peak_kib(),
        }
    except Exception as exc:  # retain a structured failed cell
        record = _trace_error_record(exc, started)
        record["target"] = []
        return record


def _trace_event(session: Any, checkpoint: Any, pause_index: int) -> dict[str, Any]:
    checkpoint_value = _canonical_value(checkpoint)
    return {
        "pause_index": pause_index,
        "state": _session_state(session),
        "checkpoint_digest": _stable_digest(checkpoint_value),
        "counters": _p2_session_counters(session),
    }


def _run_trace_candidate(
    profile: Mapping[str, Any],
    request: PlanningRequest,
    target_request: PlanningRequest,
    reuse_count: int,
) -> dict[str, Any]:
    """Trace one control source and replay its proof against R target queries."""

    started = time.perf_counter()
    reuse_records: list[dict[str, Any]] = []
    try:
        _frames, grid, sampler, vessel, planner_config, _ = _trace_components(profile)
        planner = TimeDependentAStar(
            grid,
            sampler,
            vessel,
            planner_config=planner_config,
        )
        source_started = time.perf_counter()
        external_identity = _trace_external_identity(profile)
        source_result, trace = trace_plan(
            planner,
            request,
            identity=external_identity,
        )
        source_record = _trace_result_record(
            source_result,
            trace_route_digest=trace.source_route_digest,
        )
        source_record["elapsed_ms"] = _round(
            (time.perf_counter() - source_started) * 1_000.0
        )
        source_record["request"] = _p2_request_snapshot(request)
        reuse_started = time.perf_counter()
        for reuse_index in range(1, reuse_count + 1):
            before = {
                "expanded_states": source_record["metrics"]["expanded_states"],
                "generated_states": source_record["metrics"]["generated_states"],
            }
            outcome = try_control_trace_reuse(
                trace,
                planner,
                target_request,
                identity=external_identity,
            )
            after = dict(before)
            result_record = (
                None
                if outcome.result is None
                else _trace_result_record(
                    outcome.result,
                    trace_route_digest=trace.source_route_digest,
                )
            )
            zero_new_work = (
                before["expanded_states"] == after["expanded_states"]
                and before["generated_states"] == after["generated_states"]
            )
            reuse_records.append(
                {
                    "reuse_index": reuse_index,
                    "status": outcome.status.value,
                    "reason": (
                        outcome.reason.value
                        if hasattr(outcome.reason, "value")
                        else str(outcome.reason)
                    ),
                    "hit": bool(outcome.hit),
                    "reused": bool(outcome.reused),
                    "used_search": bool(outcome.used_search),
                    "result": result_record,
                    "route_digest": (
                        None
                        if result_record is None
                        else result_record["route_digest"]
                    ),
                    "route_matches_source": (
                        result_record is not None
                        and result_record["route_digest"] == source_record["route_digest"]
                    ),
                    "counters_before": before,
                    "counters_after": after,
                    "zero_new_expansion": (
                        before["expanded_states"] == after["expanded_states"]
                    ),
                    "zero_new_edge_evaluation": True,
                    "zero_new_work": zero_new_work,
                    "request": _p2_request_snapshot(target_request),
                }
            )
        reuse_elapsed_ms = (time.perf_counter() - reuse_started) * 1_000.0
        total_elapsed_ms = (time.perf_counter() - started) * 1_000.0
        return {
            "status": "SUCCESS",
            "source": source_record,
            "trace": {
                "algorithm_version": trace.identity.algorithm_version,
                "trace_digest": trace.trace_digest,
                "ordered_write_digest": trace.ordered_write_digest,
                "insertion_count": trace.insertion_count,
                "replacement_count": trace.replacement_count,
                "termination": trace.termination,
                "source_route_digest": trace.source_route_digest,
                "external_identity_digest": trace.identity.external_identity_digest,
                "route_elapsed_seconds": _round(trace.route_elapsed_seconds),
                "route_max_edge_risk": _round(trace.route_max_edge_risk),
            },
            "reuse_count": reuse_count,
            "reuse": reuse_records,
            "reuse_elapsed_ms": _round(reuse_elapsed_ms),
            "total_elapsed_ms": _round(total_elapsed_ms),
            "peak_rss_kib": _rss_peak_kib(),
        }
    except Exception as exc:  # keep one failed profile/R cell auditable
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "reuse_count": reuse_count,
            "trace": {
                "algorithm_version": "control-trace-v1",
            },
            "reuse": reuse_records,
            "total_elapsed_ms": _round((time.perf_counter() - started) * 1_000.0),
            "peak_rss_kib": _rss_peak_kib(),
        }


def _trace_case_expectation(
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
    source = candidate.get("source", {})
    reuse = candidate.get("reuse", [])
    source_digest = source.get("route_digest")
    control_digests = {
        item.get("route_digest")
        for item in [control_worker.get("source", {}), *control_targets]
    }
    route_match = (
        control_success
        and candidate.get("status") == "SUCCESS"
        and source_digest is not None
        and control_digests == {source_digest}
    )
    reuse_hits = len(reuse) == reuse_count and all(
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
    trace_digest = candidate.get("trace", {}).get("trace_digest")
    trace_valid = isinstance(trace_digest, str) and len(trace_digest) == 64
    comparison = {
        "control_call_count": len(control),
        "control_source_plus_target_count": 1 + len(control_targets),
        "control_all_success": control_success,
        "candidate_source_success": (
            candidate.get("status") == "SUCCESS"
            and source.get("status") == "SUCCESS"
        ),
        "control_source_route_digest_equal": route_match,
        "reuse_call_count": len(reuse),
        "reuse_all_trace_hits": reuse_hits,
        "trace_digest_present": trace_valid,
        "zero_new_work_all_reuse": reuse_hits,
    }
    return bool(route_match and reuse_hits and trace_valid), comparison


def _summary_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if percentile == 0.5:
        midpoint = len(ordered) // 2
        if len(ordered) % 2:
            return _round(ordered[midpoint])
        return _round((ordered[midpoint - 1] + ordered[midpoint]) / 2.0)
    # Nearest-rank percentile.  This deliberately makes p95 conservative for
    # the ten-run M0 sample (the slowest observation is selected).
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return _round(ordered[index])


def _p21_summary(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    groups = sorted(
        {
            (case["profile"]["name"], int(case["reuse_count"]))
            for case in cases
        }
    )
    for profile_name, reuse_count in groups:
        group = [
            case
            for case in cases
            if case["profile"]["name"] == profile_name
            and int(case["reuse_count"]) == reuse_count
        ]
        control_times = [
            float(case["control"]["source_plus_target"]["total_elapsed_ms"])
            for case in group
            if case["comparison"]["expectation_met"]
        ]
        candidate_times = [
            float(case["candidate"]["total_elapsed_ms"])
            for case in group
            if case["comparison"]["expectation_met"]
        ]
        trace_overheads = [
            float(case["candidate"]["trace_overhead_ms"])
            for case in group
            if isinstance(case["candidate"].get("trace_overhead_ms"), (int, float))
        ]
        trace_overhead_percents = [
            float(case["candidate"]["trace_overhead_percent"])
            for case in group
            if isinstance(
                case["candidate"].get("trace_overhead_percent"), (int, float)
            )
        ]
        control_median = _summary_percentile(control_times, 0.5)
        candidate_median = _summary_percentile(candidate_times, 0.5)
        improvement = (
            None
            if control_median in (None, 0) or candidate_median is None
            else _round((control_median - candidate_median) / control_median * 100.0)
        )
        p95_control = _summary_percentile(control_times, 0.95)
        p95_candidate = _summary_percentile(candidate_times, 0.95)
        trace_overhead_percent_median = _summary_percentile(
            trace_overhead_percents, 0.5
        )
        performance_gate = (
            "NOT_EVALUATED_INSUFFICIENT_REPETITIONS"
            if len(group) < 10
            else (
                "PASS"
                if improvement is not None
                and improvement >= 20.0
                and trace_overhead_percent_median is not None
                and trace_overhead_percent_median <= 5.0
                else "FAIL"
            )
        )
        summary.append(
            {
                "profile": profile_name,
                "reuse_count": reuse_count,
                "sample_count": len(group),
                "semantic_pass_count": len(control_times),
                "control_total_wall_median_ms": control_median,
                "candidate_total_wall_median_ms": candidate_median,
                "control_total_wall_p95_ms": p95_control,
                "candidate_total_wall_p95_ms": p95_candidate,
                "median_improvement_percent": improvement,
                "trace_overhead_median_ms": _summary_percentile(
                    trace_overheads, 0.5
                ),
                "trace_overhead_percent_median": trace_overhead_percent_median,
                "rss_comparison": "NOT_MEASURED",
                "performance_gate": performance_gate,
                "gate_verdict": (
                    "PASS"
                    if all(case["comparison"]["expectation_met"] for case in group)
                    and performance_gate != "FAIL"
                    else "FAIL"
                ),
            }
        )
    return summary


def _run_p21_validation(*, output_dir: Path, repetitions: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    existing = [
        path.name
        for path in (manifest_path, cases_path)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing P2.1 validation artifacts: "
            + ", ".join(existing)
        )

    project_root = Path(__file__).resolve().parents[1]
    lock_path = project_root / "uv.lock"
    git_sha = _git_sha(project_root)
    worktree_dirty = _git_worktree_dirty(project_root)
    cases: list[dict[str, Any]] = []
    for index in range(1, repetitions + 1):
        for profile in _TRACE_PROFILES:
            # Capture the request once so every R cell uses the same input
            # identity.  The actual control/candidate components are rebuilt
            # cold inside their respective calls.
            _frames, _grid, _sampler, _vessel, _config, request = _trace_components(
                profile
            )
            target_request = _trace_target_request(request)
            for reuse_count in TRACE_REUSE_COUNTS:
                control = [
                    _run_trace_control(
                        profile,
                        request,
                        target_request,
                        reuse_count,
                    )
                ]
                candidate = _run_trace_candidate(
                    profile,
                    request,
                    target_request,
                    reuse_count,
                )
                control_source_elapsed = control[0].get("source", {}).get(
                    "elapsed_ms"
                )
                candidate_source_elapsed = candidate.get("source", {}).get(
                    "elapsed_ms"
                )
                if (
                    isinstance(control_source_elapsed, (int, float))
                    and isinstance(candidate_source_elapsed, (int, float))
                ):
                    candidate["trace_overhead_ms"] = _round(
                        candidate_source_elapsed - control_source_elapsed
                    )
                    candidate["trace_overhead_percent"] = _round(
                        candidate["trace_overhead_ms"]
                        / max(float(control_source_elapsed), 1e-12)
                        * 100.0
                    )
                candidate["trace_overhead_definition"] = (
                    "traced source wall minus same-cell ordinary source wall"
                )
                expectation_met, comparison = _trace_case_expectation(
                    control,
                    candidate,
                    reuse_count,
                )
                cases.append(
                    {
                        "schema_version": P21_SCHEMA_VERSION,
                        "case_id": (
                            f"{profile['name']}-r{reuse_count}-run-{index:03d}"
                        ),
                        "run_index": index,
                        "profile": dict(profile),
                        "reuse_count": reuse_count,
                        "source_request": _p2_request_snapshot(request),
                        "target_request": _p2_request_snapshot(target_request),
                        "control": {
                            "cold_call_count": reuse_count + 1,
                            "source_plus_target": control[0],
                            "total_elapsed_ms": _round(
                                control[0].get("total_elapsed_ms", 0.0)
                            ),
                        },
                        "candidate": candidate,
                        "comparison": {
                            **comparison,
                            "expectation_met": expectation_met,
                        },
                    }
                )
            del _frames

    failed = [
        case["case_id"]
        for case in cases
        if not case["comparison"]["expectation_met"]
    ]
    manifest = {
        "schema_version": P21_SCHEMA_VERSION,
        "status": "EXPERIMENTAL",
        "mode": "control_trace_reuse",
        "experiment_id": (
            f"c-p2.1-control-trace-reuse-v1-{git_sha[:8]}"
            + ("-dirty" if worktree_dirty else "")
        ),
        "production_defaults_changed": False,
        "formal_ingress_used": False,
        "frozen_artifact_written": False,
        "repetitions": repetitions,
        "serial_execution": True,
        "reuse_counts": list(TRACE_REUSE_COUNTS),
        "environment": {
            "git_sha": git_sha,
            "git_worktree_dirty": worktree_dirty,
            "uv_lock_sha256": _sha256(lock_path),
            "implementation_sha256": _implementation_sha256_p21(project_root),
            "python": sys.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_executable": sys.executable,
        },
        "profiles": [
            {
                **profile,
                "provenance": ProvenanceKind.SYNTHETIC.value,
                "source": (
                    "validate_temporal_semantics._make_frames"
                    if profile["kind"] == "small"
                    else "arctic_route_planning.profiling.SyntheticProfileConfig"
                ),
            }
            for profile in _TRACE_PROFILES
        ],
        "policy": {
            "control_workload": (
                "one source cold + R target cold TimeDependentAStar searches"
            ),
            "candidate_workload": (
                "one traced TimeDependentAStar source + R control-trace reuse calls"
            ),
            "target_constraints": "source maximum_elapsed/risk tightened only",
            "hit_requires_zero_new_expansion_and_edge_evaluation": True,
            "semantic_digest": "script route digest plus trace source digest",
            "rss_comparison": "NOT_MEASURED",
            "trace_overhead_definition": (
                "traced source wall minus same-cell ordinary source wall"
            ),
            "performance_gate": {
                "candidate_total_median_improvement_floor_percent": 20.0,
                "trace_source_overhead_median_ceiling_percent": 5.0,
                "minimum_repetitions": 10,
                "p95": "diagnostic_only_nearest_rank",
                "rss": "NOT_MEASURED_IN_SHARED_PROCESS",
            },
            "production_publication": "none",
        },
        "strategies": {
            "control": {
                "planner": "TimeDependentAStar",
                "role": "formal_control_cold",
            },
            "candidate_source": {
                "planner": "TimeDependentAStar",
                "role": "experimental_traced_source",
            },
            "candidate_reuse": {
                "api": "control_trace_reuse.trace_plan + try_reuse",
                "role": "experimental_zero_search_replay",
            },
        },
        "discrete_result_fields": [
            "route_digest",
            "trace.trace_digest",
            "candidate.reuse.status",
            "candidate.reuse.zero_new_work",
            "comparison.expectation_met",
        ],
        "summary_by_profile_reuse": _p21_summary(cases),
        "validation": {
            "case_count": len(cases),
            "all_cases_success": not failed,
            "failed_cases": failed,
            "verdict": "PASS" if not failed else "FAIL",
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with cases_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    return manifest


def run_validation(
    *,
    output_dir: Path,
    repetitions: int = 10,
    session_slice_expansions: int | None = None,
    p2_exact_goal_reuse: bool = False,
    control_trace_reuse: bool = False,
) -> dict[str, Any]:
    selected_modes = sum(
        mode is not None
        for mode in (
            session_slice_expansions,
            p2_exact_goal_reuse or None,
            control_trace_reuse or None,
        )
    )
    if selected_modes > 1:
        raise ValueError(
            "P1 session, P2 reuse, and P2.1 control-trace-reuse modes are mutually exclusive"
        )
    if control_trace_reuse:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        return _run_p21_validation(output_dir=output_dir, repetitions=repetitions)
    if p2_exact_goal_reuse:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        return _run_p2_validation(output_dir=output_dir, repetitions=repetitions)
    if session_slice_expansions is not None:
        if repetitions < 1:
            raise ValueError("repetitions must be positive")
        return _run_p1_validation(
            output_dir=output_dir,
            repetitions=repetitions,
            expansion_slice=session_slice_expansions,
        )
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    cases_path = output_dir / "cases.jsonl"
    existing = [
        path.name
        for path in (manifest_path, cases_path)
        if path.exists() or path.is_symlink()
    ]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing P0 validation artifacts: "
            + ", ".join(existing)
        )
    project_root = Path(__file__).resolve().parents[1]
    lock_path = project_root / "uv.lock"
    git_sha = _git_sha(project_root)
    worktree_dirty = _git_worktree_dirty(project_root)
    eta_policy = _candidate_policy()
    limits = _candidate_limits()
    cases: list[dict[str, Any]] = []

    for index in range(1, repetitions + 1):
        control = _run_one("control")
        candidate = _run_one("candidate")
        cases.append(
            {
                "schema_version": SCHEMA_VERSION,
                "case_id": f"{FIXTURE_ID}-run-{index:03d}",
                "run_index": index,
                "fixture_id": FIXTURE_ID,
                "control": control,
                "candidate": candidate,
                "comparison": {
                    "semantic_match": _semantic_match(control, candidate),
                    "route_digest_equal": control.get("route_digest")
                    == candidate.get("route_digest"),
                },
            }
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "EXPERIMENTAL",
        "experiment_id": (
            f"c-p0-temporal-semantics-v1-{git_sha[:8]}"
            + ("-dirty" if worktree_dirty else "")
        ),
        "fixture_id": FIXTURE_ID,
        "production_defaults_changed": False,
        "formal_ingress_used": False,
        "frozen_artifact_written": False,
        "repetitions": repetitions,
        "serial_execution": True,
        "environment": {
            "git_sha": git_sha,
            "git_worktree_dirty": worktree_dirty,
            "uv_lock_sha256": _sha256(lock_path),
            "implementation_sha256": _implementation_sha256(project_root),
            "python": sys.version,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "python_executable": sys.executable,
        },
        "fixture": {
            "rows": ROWS,
            "columns": COLS,
            "frame_count": FRAME_COUNT,
            "start": [ROWS // 2, 0],
            "goal": [ROWS // 2, COLS - 1],
            "departure_time": T0.isoformat(),
            "maximum_elapsed_hours": MAX_ELAPSED.total_seconds() / 3600.0,
            "risk_semantics": "constant_zero",
            "environment_speed_factor": 1.0,
            "provenance": ProvenanceKind.SYNTHETIC.value,
        },
        "policy": {
            "objective": OBJECTIVE.value,
            "connectivity": 4,
            "edge_sample_count": 3,
            "time_bucket_minutes": 60,
            "eta_refinement": asdict(eta_policy),
            "search_limits": asdict(limits),
            "semantic_digest_excludes": ["elapsed_ms", "planner_compute_ms"],
        },
        "strategies": {
            "control": {
                "planner": "TimeDependentAStar",
                "state_semantics": "node,time_bucket,heading",
                "role": "formal_control",
            },
            "candidate": {
                "planner": "TemporalLabelAStar",
                "state_semantics": "node,heading,exact_arrival_time",
                "role": "experimental_shadow",
            },
        },
        "discrete_result_fields": [
            "route_digest",
            "route",
            "metrics",
            "diagnostics",
            "comparison.semantic_match",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with cases_path.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")

    failed = [
        case["case_id"]
        for case in cases
        if not case["comparison"]["semantic_match"]
    ]
    manifest["validation"] = {
        "case_count": len(cases),
        "all_cases_success": not failed,
        "failed_cases": failed,
        "verdict": "PASS" if not failed else "FAIL",
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", "--runs", type=int, default=10)
    parser.add_argument(
        "--session-slice-expansions",
        type=int,
        default=None,
        help="enable explicit P1 pause/checkpoint/restore validation with this slice",
    )
    parser.add_argument(
        "--p2-exact-goal-reuse",
        "--p2",
        dest="p2_exact_goal_reuse",
        action="store_true",
        help="enable explicit P2 same-goal certificate/reuse validation",
    )
    parser.add_argument(
        "--control-trace-reuse",
        "--p2.1-control-trace-reuse",
        dest="control_trace_reuse",
        action="store_true",
        help=(
            "enable explicit P2.1 R+1 cold control versus one traced source + R reuse"
        ),
    )
    args = parser.parse_args(argv)
    manifest = run_validation(
        output_dir=args.output_dir,
        repetitions=args.repetitions,
        session_slice_expansions=args.session_slice_expansions,
        p2_exact_goal_reuse=args.p2_exact_goal_reuse,
        control_trace_reuse=args.control_trace_reuse,
    )
    print(json.dumps(manifest["validation"], ensure_ascii=False, sort_keys=True))
    return 0 if manifest["validation"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
