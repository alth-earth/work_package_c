"""Command-line entry points for strict synthetic and legacy development runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from arctic_route_contracts import (
    RunContext,
    load_run_context,
)
from arctic_route_contracts import (
    default_config_root as default_shared_config_root,
)

from arctic_route_planning.adapters import FixtureRiskSource, LegacyBArchiveAdapter
from arctic_route_planning.config import PlanningConfiguration, load_configuration
from arctic_route_planning.context_validation import validate_run_context_binding
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.domain import CalibrationStatus
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.errors import PlanningError
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import TimeDependentAStar
from arctic_route_planning.publishing import (
    atomic_write_json,
    selection_rationale_to_dict,
    write_route_plan_geojson,
    write_route_plan_json,
    write_selection_rationale_json,
)
from arctic_route_planning.risk import RiskSampler
from arctic_route_planning.service import PlanningBatch, PlanningService, ServicePlanningRequest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arctic-route-plan",
        description="Work Package C: prediction-risk-driven Arctic route planning",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        default=_default_config_root(),
        help="configuration directory (default: project configs/)",
    )
    parser.add_argument(
        "--shared-config-root",
        type=Path,
        default=default_shared_config_root(),
        help="shared arctic_route_contracts config directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    synthetic = subparsers.add_parser(
        "synthetic-demo",
        help="run all three objectives against deterministic synthetic RiskFrames",
    )
    _add_scenario_arguments(synthetic)
    synthetic.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="explicit directory for JSON, GeoJSON, and summary outputs",
    )
    synthetic.add_argument("--rows", type=int, default=5, help="synthetic grid rows")
    synthetic.add_argument("--columns", type=int, default=5, help="synthetic grid columns")
    synthetic.add_argument(
        "--max-snap-km",
        type=float,
        default=300.0,
        help="maximum reported scenario-endpoint to grid-node adjustment",
    )
    synthetic.set_defaults(handler=_run_synthetic_demo)

    inspect = subparsers.add_parser(
        "legacy-inspect",
        help="inspect the unverified nested legacy B archive through its isolated adapter",
    )
    _add_legacy_arguments(inspect, include_planning=False)
    inspect.set_defaults(handler=_inspect_legacy)

    legacy_plan = subparsers.add_parser(
        "legacy-plan",
        help="development-only planning against explicitly acknowledged legacy B data",
    )
    _add_legacy_arguments(legacy_plan, include_planning=True)
    legacy_plan.set_defaults(handler=_run_legacy_plan)
    return parser


def _add_scenario_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--scenario",
        default="tromso_isfjorden_july_2026_retrospective_v1",
        help="scenario config ID",
    )
    parser.add_argument(
        "--simulation-start",
        type=_parse_utc,
        help="required explicit UTC anchor for a frozen-forecast template",
    )
    parser.add_argument(
        "--candidate-route-distance-nm",
        type=float,
        help="select a route-specific frozen horizon from the shared HorizonPolicy",
    )
    parser.add_argument(
        "--run-context",
        type=Path,
        help="immutable RunContext JSON from A; omitted only for development sources",
    )
    parser.add_argument("--generation-id", type=int, default=0)
    parser.add_argument("--input-revision", type=int, default=0)


def _add_legacy_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_planning: bool,
) -> None:
    _add_scenario_arguments(parser)
    parser.add_argument("--archive", type=Path, required=True, help="outer legacy delivery ZIP")
    parser.add_argument("--as-of", type=_parse_utc, required=True, help="explicit UTC cutoff")
    parser.add_argument("--variant", choices=("7days", "60days"), default="7days")
    parser.add_argument(
        "--allow-unverified-legacy",
        action="store_true",
        help="acknowledge that legacy provenance and issue_time are unverified",
    )
    parser.add_argument(
        "--acknowledge-valid-time",
        action="store_true",
        help="explicitly interpret the legacy time coordinate as valid_time",
    )
    if include_planning:
        parser.add_argument("--output-dir", type=Path, required=True)
        parser.add_argument(
            "--max-snap-km",
            type=float,
            required=True,
            help="explicit upper bound for legacy endpoint-to-grid adjustment",
        )
        parser.add_argument(
            "--departure-time",
            type=_parse_utc,
            help="UTC departure time (default: first available RiskFrame)",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (PlanningError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _run_synthetic_demo(args: argparse.Namespace) -> int:
    configuration = _configuration(args)
    run_context = _run_context(args, configuration, source_kind="synthetic")
    if args.rows < 3 or args.columns < 3:
        raise ValueError("synthetic grid rows and columns must both be at least 3")
    duration = configuration.scenario.simulation_end - configuration.scenario.simulation_start
    frame_count = int(duration.total_seconds() // 3600) + 1
    source = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=args.generation_id,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=frame_count,
        shape=(args.rows, args.columns),
    )
    batch, endpoint_report = _plan_frames(
        configuration,
        run_context,
        source.frames,
        generation_id=args.generation_id,
        input_revision=args.input_revision,
        start_time=configuration.scenario.simulation_start,
        max_snap_km=args.max_snap_km,
        endpoint_report_path=args.output_dir / "endpoint-mapping.json",
    )
    summary = _write_outputs(
        args.output_dir,
        batch,
        source_kind="synthetic",
        development_warnings=(
            "Synthetic analytic RiskFrames: not a forecast and not safe-navigation data.",
            "Demo vessel parameters are unvalidated and do not represent a real ship.",
        ),
        endpoint_report=endpoint_report,
        configuration=configuration,
        run_context=run_context,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _inspect_legacy(args: argparse.Namespace) -> int:
    configuration = _configuration(args)
    _require_legacy_acknowledgements(args)
    run_context = _run_context(args, configuration, source_kind="legacy_unverified")
    adapter = _legacy_adapter(args, configuration, run_context)
    frames = adapter.load()
    first = frames[0]
    summary = {
        "source_kind": "legacy_unverified",
        "development_only": True,
        "run_id": run_context.run_id,
        "scenario_id": configuration.scenario.scenario_id,
        "corridor_id": configuration.scenario.corridor_id,
        "frame_count": len(frames),
        "first_valid_time": _format_time(first.valid_time),
        "last_valid_time": _format_time(frames[-1].valid_time),
        "shape": list(first.grid.shape),
        "inner_member": adapter.inner_member_name,
        "coordinate_snap_applied_by_adapter": bool(
            first.payload.attrs.get("coordinate_snap_applied", False)
        ),
        "confidence_max": float(np.nanmax(first.payload["confidence"].values)),
        "warnings": [
            "Legacy issue_time is unknown; this source is forbidden in formal/replay mode.",
            "route_cost_grid is ignored; C recomputes costs and effective vessel speed.",
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run_legacy_plan(args: argparse.Namespace) -> int:
    configuration = _configuration(args)
    _require_legacy_acknowledgements(args)
    run_context = _run_context(args, configuration, source_kind="legacy_unverified")
    adapter = _legacy_adapter(args, configuration, run_context)
    frames = adapter.load()
    departure = args.departure_time or frames[0].valid_time
    batch, endpoint_report = _plan_frames(
        configuration,
        run_context,
        frames,
        generation_id=args.generation_id,
        input_revision=args.input_revision,
        start_time=departure,
        max_snap_km=args.max_snap_km,
        endpoint_report_path=args.output_dir / "endpoint-mapping.json",
    )
    summary = _write_outputs(
        args.output_dir,
        batch,
        source_kind="legacy_unverified",
        development_warnings=(
            "Legacy issue_time is unknown; output is development-only and not replay-valid.",
            "Legacy sea_mask is used only as a land hard-mask; other hard constraints are absent.",
            "Any endpoint snap is adapter-level, distance-limited, and recorded below.",
        ),
        endpoint_report=endpoint_report,
        configuration=configuration,
        run_context=run_context,
    )
    summary["legacy_inner_member"] = adapter.inner_member_name
    summary["legacy_coordinate_snap_applied_by_adapter"] = bool(
        frames[0].payload.attrs.get("coordinate_snap_applied", False)
    )
    atomic_write_json(args.output_dir / "run-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _legacy_adapter(
    args: argparse.Namespace,
    configuration: PlanningConfiguration,
    run_context: RunContext,
) -> LegacyBArchiveAdapter:
    _require_legacy_acknowledgements(args)
    return LegacyBArchiveAdapter(
        archive_path=args.archive,
        scenario=configuration.scenario,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=args.generation_id,
        as_of_time=args.as_of,
        development_mode=True,
        time_coordinate_semantics="valid_time",
        dataset_variant=args.variant,
        legacy_corridor_id={
            "tromso_to_isfjorden_outer": "tromso_to_svalbard",
        }.get(configuration.corridor.corridor_id),
    )


def _require_legacy_acknowledgements(args: argparse.Namespace) -> None:
    if not args.allow_unverified_legacy:
        raise ValueError("--allow-unverified-legacy is required for legacy data")
    if not args.acknowledge_valid_time:
        raise ValueError("--acknowledge-valid-time is required for legacy data")


def _plan_frames(
    configuration: PlanningConfiguration,
    run_context: RunContext,
    frames: Sequence[Any],
    *,
    generation_id: int,
    input_revision: int,
    start_time: datetime,
    max_snap_km: float,
    endpoint_report_path: Path | None = None,
) -> tuple[PlanningBatch, dict[str, Any]]:
    if not frames:
        raise ValueError("at least one RiskFrame is required")
    sampler = RiskSampler(
        frames,
        max_frame_gap=timedelta(minutes=configuration.planner.max_risk_frame_gap_minutes),
    )
    if (
        sampler.start_time < run_context.simulation_start
        or sampler.end_time > run_context.simulation_end
    ):
        raise ValueError(
            "supplied RiskFrame window extends beyond the RunContext simulation window"
        )
    if start_time < sampler.start_time or start_time > sampler.end_time:
        raise ValueError("departure time is outside the supplied RiskFrame window")
    ordered_frames = sampler.frames
    grid = RegularGrid.from_risk_frame(
        ordered_frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    endpoint_mapping = map_corridor_endpoints(
        configuration,
        ordered_frames[0],
        max_adjustment_km=max_snap_km,
    )
    endpoint_report = endpoint_mapping.to_document()
    if endpoint_report_path is not None:
        atomic_write_json(
            endpoint_report_path,
            {"schema_version": "endpoint-mapping.v1", **endpoint_report},
        )
        print(f"endpoint mapping written to {endpoint_report_path}", file=sys.stderr)
    vessel_model = VesselPerformanceModel.from_configuration(configuration.vessel_model)
    planner = TimeDependentAStar(
        grid,
        sampler,
        vessel_model,
        planner_config=configuration.planner,
    )
    service = PlanningService(planner, planner_config=configuration.planner)
    batch = service.execute(
        ServicePlanningRequest(
            run_context=run_context,
            scenario=configuration.scenario,
            corridor=configuration.corridor,
            vessel=configuration.vessel,
            vessel_model=configuration.vessel_model,
            model_config_digest=frames[0].model_config_digest,
            planner_config_digest=configuration.planner_config_digest,
            risk_provenance=sampler.identity.provenance,
            generation_id=generation_id,
            input_revision=input_revision,
            as_of_time=max(frame.as_of_time for frame in ordered_frames),
            start_time=start_time,
            start=endpoint_mapping.start.node,
            goal=endpoint_mapping.goal.node,
            maximum_elapsed=sampler.end_time - start_time,
        )
    )
    return batch, endpoint_report

def _write_outputs(
    output_dir: Path,
    batch: PlanningBatch,
    *,
    source_kind: str,
    development_warnings: tuple[str, ...],
    endpoint_report: dict[str, Any],
    configuration: PlanningConfiguration,
    run_context: RunContext,
) -> dict[str, Any]:
    if source_kind != batch.selected.provenance.value:
        raise ValueError("source_kind does not match the selected RoutePlan provenance")
    if any(plan.provenance is not batch.selected.provenance for plan in batch.plans.values()):
        raise ValueError("planning batch contains mixed RoutePlan provenance")
    output_dir.mkdir(parents=True, exist_ok=True)
    for objective, plan in batch.plans.items():
        write_route_plan_json(output_dir / f"{objective.value}.json", plan)
        write_route_plan_geojson(output_dir / f"{objective.value}.geojson", plan)
    write_route_plan_json(output_dir / "latest.json", batch.selected)
    write_route_plan_geojson(output_dir / "latest.geojson", batch.selected)
    selection_rationale_summary: dict[str, Any] | None = None
    rationale_files: dict[str, str] = {}
    if batch.selection_rationale is not None:
        write_selection_rationale_json(
            output_dir / "selection-rationale.json", batch.selection_rationale
        )
        rationale_files["selection_rationale"] = "selection-rationale.json"
        rationale_doc = selection_rationale_to_dict(batch.selection_rationale)
        selection_rationale_summary = {
            "selected_objective": rationale_doc["selected_objective"],
            "baseline_objective": rationale_doc["baseline_objective"],
            "selected_plan_id": rationale_doc["selected_plan_id"],
            "baseline_plan_id": rationale_doc["baseline_plan_id"],
            "tradeoffs": rationale_doc["tradeoffs"],
            "summary_text": rationale_doc["summary_text"],
        }
    summary = {
        "schema_version": "planning-run-summary.v1",
        "source_kind": source_kind,
        "development_only": (
            source_kind != "formal"
            or configuration.vessel.calibration_status
            is CalibrationStatus.PUBLIC_REFERENCE_UNVALIDATED
        ),
        "scenario_id": configuration.scenario.scenario_id,
        "vessel_profile_id": configuration.vessel.vessel_profile_id,
        "vessel_calibration_status": configuration.vessel.calibration_status.value,
        "run_id": run_context.run_id,
        "config_digest": run_context.config_digest,
        "model_config_digest": batch.selected.model_config_digest,
        "planner_config_digest": configuration.planner_config_digest,
        "published": batch.published,
        "selected_plan_id": batch.selected.plan_id,
        "selected_objective": batch.selected.objective_mode.value,
        "selection_rationale": selection_rationale_summary,
        "endpoint_mapping": endpoint_report,
        "speed_responsibility": "B supplies environmental factors; C computes final vessel speed",
        "warnings": list(development_warnings),
        "plans": {
            objective.value: {
                "plan_id": plan.plan_id,
                "distance_km": plan.metrics.distance_km,
                "eta_hours": plan.metrics.eta_hours,
                "avg_risk": plan.metrics.avg_risk,
                "max_risk": plan.metrics.max_risk,
                "compute_ms": plan.metrics.compute_ms,
                "expanded_nodes": plan.metrics.expanded_nodes,
            }
            for objective, plan in batch.plans.items()
        },
        "files": {
            "latest_json": "latest.json",
            "latest_geojson": "latest.geojson",
            "endpoint_mapping": "endpoint-mapping.json",
            **rationale_files,
        },
    }
    atomic_write_json(output_dir / "run-summary.json", summary)
    return summary


def _configuration(args: argparse.Namespace) -> PlanningConfiguration:
    return load_configuration(
        args.config_root,
        args.scenario,
        shared_config_root=args.shared_config_root,
        simulation_start=args.simulation_start,
        candidate_route_distance_nm=args.candidate_route_distance_nm,
    )


def _run_context(
    args: argparse.Namespace,
    configuration: PlanningConfiguration,
    *,
    source_kind: str,
) -> RunContext:
    if args.run_context is not None:
        context = load_run_context(args.run_context)
        validate_run_context_binding(
            context,
            scenario=configuration.scenario,
            corridor=configuration.corridor,
            vessel=configuration.vessel,
        )
        return context

    archive = getattr(args, "archive", None)
    context = create_development_run_context(
        configuration,
        source_kind=source_kind,
        source_checksum=_sha256_file(Path(archive)) if archive is not None else "",
        as_of_time=getattr(args, "as_of", None),
    )
    validate_run_context_binding(
        context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
    )
    return context


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("datetime must be timezone-aware UTC")
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _default_config_root() -> Path:
    project_root = Path(__file__).resolve().parents[2]
    candidate = project_root / "configs"
    return candidate if candidate.is_dir() else Path.cwd() / "configs"


if __name__ == "__main__":
    raise SystemExit(main())
