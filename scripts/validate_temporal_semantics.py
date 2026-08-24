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
import platform
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.temporal_label_astar import (
    TemporalLabelAStar,
    TemporalSearchLimits,
)
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0-temporal-semantics.v1"
FIXTURE_ID = "synthetic-static-5x7x7-v1"
OBJECTIVE = ObjectiveMode.RECOMMENDED
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FRAME_COUNT = 7
ROWS = 5
COLS = 7
MAX_ELAPSED = timedelta(hours=6)


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


def _make_frames() -> tuple[RiskFrame, ...]:
    risk = np.zeros((ROWS, COLS), dtype=np.float32)
    confidence = np.full((ROWS, COLS), 0.9, dtype=np.float32)
    hard_mask = np.zeros((ROWS, COLS), dtype=np.bool_)
    speed_factor = np.ones((ROWS, COLS), dtype=np.float32)
    risk_level = np.ones((ROWS, COLS), dtype=np.uint8)
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
            attrs={"crs": "EPSG:4326", "grid_id": "p0-static-grid"},
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


def _build_components() -> tuple[RegularGrid, RiskSampler, VesselPerformanceModel, PlannerConfig]:
    frames = _make_frames()
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


def _run_one(strategy: str) -> dict[str, Any]:
    grid, sampler, vessel, planner_config = _build_components()
    request = _request()
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


def _semantic_match(control: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if control.get("status") != "SUCCESS" or candidate.get("status") != "SUCCESS":
        return False
    return bool(control.get("route_digest") == candidate.get("route_digest"))


def run_validation(*, output_dir: Path, repetitions: int = 10) -> dict[str, Any]:
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
    args = parser.parse_args(argv)
    manifest = run_validation(output_dir=args.output_dir, repetitions=args.repetitions)
    print(json.dumps(manifest["validation"], ensure_ascii=False, sort_keys=True))
    return 0 if manifest["validation"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
