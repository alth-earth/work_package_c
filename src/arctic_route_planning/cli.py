"""Command-line entry points for strict synthetic and legacy development runs."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from arctic_route_planning.adapters import FixtureRiskSource, LegacyBArchiveAdapter
from arctic_route_planning.config import PlanningConfiguration, load_configuration
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain import CalibrationStatus
from arctic_route_planning.errors import PlanningError
from arctic_route_planning.grid import GeoPoint, RegularGrid, SnapResult
from arctic_route_planning.planners import TimeDependentAStar
from arctic_route_planning.publishing import (
    atomic_write_json,
    write_route_plan_geojson,
    write_route_plan_json,
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
        default="demo_tromso_to_svalbard_v1",
        help="scenario config ID",
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
    if args.rows < 3 or args.columns < 3:
        raise ValueError("synthetic grid rows and columns must both be at least 3")
    duration = configuration.scenario.simulation_end - configuration.scenario.simulation_start
    frame_count = int(duration.total_seconds() // 3600) + 1
    source = FixtureRiskSource(
        scenario=configuration.scenario,
        vessel=configuration.vessel,
        config_digest=configuration.config_digest,
        generation_id=args.generation_id,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=frame_count,
        shape=(args.rows, args.columns),
    )
    batch, endpoint_report = _plan_frames(
        configuration,
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
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _inspect_legacy(args: argparse.Namespace) -> int:
    configuration = _configuration(args)
    adapter = _legacy_adapter(args, configuration)
    frames = adapter.load()
    first = frames[0]
    summary = {
        "source_kind": "legacy_unverified",
        "development_only": True,
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
    adapter = _legacy_adapter(args, configuration)
    frames = adapter.load()
    departure = args.departure_time or frames[0].valid_time
    batch, endpoint_report = _plan_frames(
        configuration,
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
) -> LegacyBArchiveAdapter:
    if not args.allow_unverified_legacy:
        raise ValueError("--allow-unverified-legacy is required for legacy data")
    if not args.acknowledge_valid_time:
        raise ValueError("--acknowledge-valid-time is required for legacy data")
    return LegacyBArchiveAdapter(
        archive_path=args.archive,
        scenario=configuration.scenario,
        vessel=configuration.vessel,
        config_digest=configuration.config_digest,
        generation_id=args.generation_id,
        as_of_time=args.as_of,
        development_mode=True,
        time_coordinate_semantics="valid_time",
        dataset_variant=args.variant,
    )


def _plan_frames(
    configuration: PlanningConfiguration,
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
    if not math.isfinite(max_snap_km) or max_snap_km < 0:
        raise ValueError("max_snap_km must be finite and non-negative")
    if start_time < frames[0].valid_time or start_time > frames[-1].valid_time:
        raise ValueError("departure time is outside the supplied RiskFrame window")
    sampler = RiskSampler(
        frames,
        max_frame_gap=timedelta(minutes=configuration.planner.max_risk_frame_gap_minutes),
    )
    grid = RegularGrid.from_risk_frame(
        frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    start_snap, destination_snap = _snap_endpoints(
        configuration,
        grid,
        np.asarray(frames[0].payload["hard_mask"].values, dtype=np.bool_),
        max_snap_km=max_snap_km,
    )
    endpoint_report = {
        "start": _snap_report(configuration.scenario.start, start_snap, max_snap_km),
        "destination": _snap_report(
            configuration.scenario.destination,
            destination_snap,
            max_snap_km,
        ),
    }
    if endpoint_report_path is not None:
        atomic_write_json(
            endpoint_report_path,
            {"schema_version": "endpoint-mapping.v1", **endpoint_report},
        )
        print(f"endpoint mapping written to {endpoint_report_path}", file=sys.stderr)
    vessel_model = VesselPerformanceModel.from_profile(configuration.vessel)
    planner = TimeDependentAStar(
        grid,
        sampler,
        vessel_model,
        planner_config=configuration.planner,
    )
    service = PlanningService(planner, planner_config=configuration.planner)
    batch = service.execute(
        ServicePlanningRequest(
            scenario=configuration.scenario,
            vessel=configuration.vessel,
            config_digest=configuration.config_digest,
            generation_id=generation_id,
            input_revision=input_revision,
            as_of_time=frames[0].as_of_time,
            start_time=start_time,
            start=start_snap.node,
            goal=destination_snap.node,
            maximum_elapsed=frames[-1].valid_time - start_time,
        )
    )
    return batch, endpoint_report


def _snap_endpoints(
    configuration: PlanningConfiguration,
    grid: RegularGrid,
    hard_mask: np.ndarray,
    *,
    max_snap_km: float,
) -> tuple[SnapResult, SnapResult]:
    start = grid.snap_to_navigable(
        GeoPoint(
            configuration.scenario.start.longitude,
            configuration.scenario.start.latitude,
        ),
        hard_mask,
        max_adjustment_km=max_snap_km,
    )
    component = grid.connected_component(start.node, hard_mask)
    try:
        destination = grid.snap_to_navigable(
            GeoPoint(
                configuration.scenario.destination.longitude,
                configuration.scenario.destination.latitude,
            ),
            hard_mask,
            max_adjustment_km=max_snap_km,
            required_component=component,
        )
    except ValueError as exc:
        raise ValueError(
            "start endpoint mapped by "
            f"{start.adjustment_km:.3f} km, but destination mapping failed: {exc}"
        ) from exc
    if start.node == destination.node:
        raise ValueError("bounded endpoint mapping resolved start and destination to one node")
    return start, destination


def _snap_report(requested: Any, resolved: SnapResult, max_snap_km: float) -> dict[str, Any]:
    return {
        "requested": [requested.longitude, requested.latitude],
        "resolved": [resolved.point.longitude, resolved.point.latitude],
        "adjustment_km": resolved.adjustment_km,
        "snap_applied": resolved.adjustment_km > 1e-9,
        "max_snap_km": max_snap_km,
    }


def _write_outputs(
    output_dir: Path,
    batch: PlanningBatch,
    *,
    source_kind: str,
    development_warnings: tuple[str, ...],
    endpoint_report: dict[str, Any],
    configuration: PlanningConfiguration,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for objective, plan in batch.plans.items():
        write_route_plan_json(output_dir / f"{objective.value}.json", plan)
        write_route_plan_geojson(output_dir / f"{objective.value}.geojson", plan)
    write_route_plan_json(output_dir / "latest.json", batch.selected)
    write_route_plan_geojson(output_dir / "latest.geojson", batch.selected)
    summary = {
        "schema_version": "planning-run-summary.v1",
        "source_kind": source_kind,
        "development_only": (
            source_kind != "formal"
            or configuration.vessel.calibration_status is CalibrationStatus.DEMO_UNVALIDATED
        ),
        "scenario_id": configuration.scenario.scenario_id,
        "vessel_profile_id": configuration.vessel.vessel_profile_id,
        "vessel_calibration_status": configuration.vessel.calibration_status.value,
        "config_digest": configuration.config_digest,
        "published": batch.published,
        "selected_plan_id": batch.selected.plan_id,
        "selected_objective": batch.selected.objective_mode.value,
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
        },
    }
    atomic_write_json(output_dir / "run-summary.json", summary)
    return summary


def _configuration(args: argparse.Namespace) -> PlanningConfiguration:
    return load_configuration(args.config_root, args.scenario)


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
