"""Bounded R0 route-smoothing baseline/geometry experiment runner.

This runner only consumes an existing route JSON and writes research
artifacts.  It never calls the formal planner, changes a route, or claims
RiskFrame/vessel qualification.  Qualified synthetic experiments use the
separate programmatic API in :mod:`route_smoothing_qualification`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .route_smoothing import RouteSmoothingPolicy, _canonical_digest
from .route_smoothing_baseline import (
    _read_route,
    build_route_geometry_baseline,
)
from .route_smoothing_runner import build_research_sidecar

EXPERIMENT_SCHEMA_VERSION = "c.research-route-smoothing-experiment.v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git_snapshot() -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[3]
    snapshot: dict[str, Any] = {"repository": str(repository)}
    for name, command in (
        ("head", ("git", "rev-parse", "HEAD")),
        ("tree", ("git", "rev-parse", "HEAD^{tree}")),
    ):
        result = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
        )
        snapshot[name] = result.stdout.strip() if result.returncode == 0 else None
    status = subprocess.run(
        ("git", "status", "--porcelain"),
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    snapshot["working_tree_clean"] = status.returncode == 0 and not status.stdout.strip()
    return snapshot


def _read_document(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read route experiment input {path}: {exc}") from exc


def _selected_candidate(document: Any, route: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    candidate_document = document.get("route_candidates")
    if not isinstance(candidate_document, dict):
        return None
    candidates = candidate_document.get("candidates")
    selected_id = candidate_document.get("selected_candidate_id")
    route_id = route.get("route_id") if isinstance(route, dict) else None
    for candidate in candidates if isinstance(candidates, list) else ():
        if not isinstance(candidate, dict):
            continue
        candidate_id = candidate.get("candidate_id")
        if candidate_id in (selected_id, route_id):
            return candidate
    return None


def _research_context(
    document: Any,
    route: Any,
    *,
    input_path: Path,
    input_sha256: str,
) -> dict[str, Any]:
    """Extract identity without pretending to have run RiskSampler."""

    route_id = route.get("route_id") if isinstance(route, dict) else None
    route_identity = {
        key: route.get(key)
        for key in (
            "route_id",
            "revision",
            "layer",
            "objective",
            "decision_time",
            "effective_adoption_time",
        )
        if isinstance(route, dict) and route.get(key) is not None
    }
    route_semantic_digest = _canonical_digest(route)
    candidate = _selected_candidate(document, route)
    provenance = candidate.get("provenance", {}) if candidate else {}
    if not isinstance(provenance, dict):
        provenance = {}
    source = document.get("risk", {}).get("source", {}) if isinstance(document, dict) else {}
    if not isinstance(source, dict):
        source = {}
    validation = document.get("research_validation", {}) if isinstance(document, dict) else {}
    if not isinstance(validation, dict):
        validation = {}
    risk_frame_identity = {
        key: value
        for key, value in {
            "risk_schema": source.get("schema_version") or validation.get("risk_schema"),
            "scenario_id": source.get("scenario_id") or validation.get("scenario_label"),
            "run_id": source.get("run_id") or validation.get("run_context_id"),
            "dataset_bundle_id": source.get("dataset_bundle_id")
            or validation.get("dataset_bundle_id"),
            "dataset_bundle_digest": source.get("dataset_bundle_digest"),
            "risk_window_id": source.get("risk_window_id") or validation.get("risk_window_id"),
            "risk_window_digest": source.get("risk_window_digest"),
            "risk_frame_count": validation.get("risk_frame_count"),
            "risk_store_root": source.get("risk_store_root"),
            "grid": document.get("risk", {}).get("grid") if isinstance(document, dict) else None,
        }.items()
        if value is not None
    }
    vessel_profile_id = provenance.get("vessel_profile_id")
    vessel_profile_identity = {
        key: provenance.get(key)
        for key in (
            "vessel_profile_id",
            "model_config_digest",
            "config_digest",
            "planner_config_digest",
        )
        if provenance.get(key) is not None
    }
    return {
        "input_identity": {
            "source_path": str(input_path),
            "source_sha256": input_sha256,
        },
        "source_path": str(input_path),
        "source_sha256": input_sha256,
        "evidence_level": "GEOMETRY_ONLY",
        "route_identity": route_identity,
        "route_semantic_digest": route_semantic_digest,
        "scenario_id": provenance.get("scenario_id") or source.get("scenario_id"),
        "corridor_id": provenance.get("corridor_id"),
        "vessel_profile_id": vessel_profile_id,
        "vessel_profile_identity": vessel_profile_identity,
        "model_config_digest": provenance.get("model_config_digest"),
        "planner_config_digest": provenance.get("planner_config_digest"),
        "risk_frame_identity": risk_frame_identity,
        "risk_evidence": {
            "status": "NOT_EVALUATED",
            "coverage_complete": False,
            "reason": "R0.2 geometry-only baseline; RiskSampler was not invoked",
            "risk_frame_identity": risk_frame_identity,
        },
        "hard_mask_evidence": {
            "status": "NOT_EVALUATED",
            "complete": False,
            "reason": "R0.2 geometry-only baseline; hard-mask envelope was not evaluated",
        },
        "coverage_evidence": {
            "status": "NOT_EVALUATED",
            "complete": False,
            "reason": "R0.2 geometry-only baseline; curve coverage was not evaluated",
        },
        "route_id": route_id,
    }


def run_geometry_experiment(
    input_path: Path,
    output_dir: Path,
    *,
    experiment_id: str,
    minimum_radius_m: float = 2_000.0,
) -> dict[str, Any]:
    """Write the fixed R0.2 artifact set and return its summary."""

    document = _read_document(input_path)
    route = _read_route(input_path)
    input_sha256 = _sha256_file(input_path)
    git_snapshot = _git_snapshot()
    context = _research_context(
        document,
        route,
        input_path=input_path,
        input_sha256=input_sha256,
    )
    baseline = build_route_geometry_baseline(
        route,
        policy=RouteSmoothingPolicy(minimum_radius_m=minimum_radius_m),
        context=context,
    )
    sidecar = build_research_sidecar(
        route,
        experiment_id=experiment_id,
        minimum_radius_m=minimum_radius_m,
        input_identity=context,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "route-smoothing-sidecar.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    case = {
        "case_id": "current_route_geometry",
        "provenance": "CURRENT_VIEWER_BUNDLE_INHERITED",
        "status": "PASS" if baseline.get("status") == "PASS" else "FALLBACK",
        "baseline_digest": baseline.get("baseline_digest"),
        "sidecar_digest": sidecar.get("sidecar_digest"),
        "waypoint_count": baseline.get("waypoint_count"),
        "corner_count": baseline.get("corner_count"),
        "eligible_corner_count": baseline.get("eligible_corner_count"),
        "selected_radius_m": [
            segment.get("radius_m") for segment in sidecar.get("geometry", {}).get("segments", [])
        ],
        "route_identity": context.get("route_identity"),
        "route_semantic_digest": context.get("route_semantic_digest"),
        "risk_frame_identity": context.get("risk_frame_identity"),
        "vessel_profile_identity": context.get("vessel_profile_identity"),
        "risk_rechecked": False,
        "hard_mask_rechecked": False,
        "coverage_complete": False,
        "resource_evidence_complete": False,
        "production_qualified": False,
    }
    (output_dir / "cases.jsonl").write_text(
        json.dumps(case, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "status": case["status"],
        "evidence_level": "GEOMETRY_ONLY",
        "input_path": str(input_path),
        "input_sha256": input_sha256,
        "route_identity": context.get("route_identity"),
        "route_semantic_digest": context.get("route_semantic_digest"),
        "risk_frame_identity": context.get("risk_frame_identity"),
        "vessel_profile_identity": context.get("vessel_profile_identity"),
        "baseline_digest": baseline.get("baseline_digest"),
        "sidecar_digest": sidecar.get("sidecar_digest"),
        "waypoint_count": baseline.get("waypoint_count"),
        "leg_count": baseline.get("leg_count"),
        "corner_count": baseline.get("corner_count"),
        "eligible_corner_count": baseline.get("eligible_corner_count"),
        "accepted_corner_count": len(sidecar.get("geometry", {}).get("segments", [])),
        "fallback_count": len(sidecar.get("geometry", {}).get("rejected_corners", [])),
        "minimum_radius_m": minimum_radius_m,
        "radius_policy": {
            "minimum_radius_scenarios_m": [1_000.0, 2_000.0, 4_000.0],
            "primary_minimum_radius_m": minimum_radius_m,
            "max_trim_fraction": 0.45,
            "radius_trials": 65,
        },
        "risk_evidence": "NOT_EVALUATED",
        "hard_mask_evidence": "NOT_EVALUATED",
        "coverage_evidence": "NOT_EVALUATED",
        "resource_evidence_complete": False,
        "production_qualified": False,
        "generated_at": _utc_now(),
        "case_digest": _canonical_digest(case),
        "git_snapshot": git_snapshot,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "run_kind": "STATIC_GEOMETRY_ONLY",
        "provenance": "CURRENT_VIEWER_BUNDLE_INHERITED",
        "status": summary["status"],
        "artifacts": {
            name: {"path": str(output_dir / name), "sha256": _sha256_file(output_dir / name)}
            for name in (
                "baseline.json",
                "route-smoothing-sidecar.json",
                "cases.jsonl",
                "summary.json",
            )
        },
        "input_identity": context,
        "route_identity": context.get("route_identity"),
        "risk_frame_identity": context.get("risk_frame_identity"),
        "vessel_profile_identity": context.get("vessel_profile_identity"),
        "evidence_level": "GEOMETRY_ONLY",
        "qualification_status": "NOT_EVALUATED",
        "git_snapshot": git_snapshot,
        "resource_evidence_complete": False,
        "real_replay": False,
        "production_qualified": False,
        "created_at": _utc_now(),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (output_dir / "ALL_DONE").write_text(
        "status=PASS\nrun_kind=STATIC_GEOMETRY_ONLY\nevidence_level=GEOMETRY_ONLY\n"
        "risk=NOT_EVALUATED\nhard_mask=NOT_EVALUATED\ncoverage=NOT_EVALUATED\n"
        "production_qualified=false\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arctic_route_planning.research.route_smoothing_experiment"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--minimum-radius-m", type=float, default=2_000.0)
    args = parser.parse_args(argv)
    summary = run_geometry_experiment(
        args.input,
        args.output_dir,
        experiment_id=args.experiment_id,
        minimum_radius_m=args.minimum_radius_m,
    )
    print(
        f"wrote {args.output_dir} status={summary['status']} "
        f"corners={summary['corner_count']} accepted={summary['accepted_corner_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
