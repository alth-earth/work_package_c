"""CLI producer for an immutable formal C -> D route-motion artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import xarray as xr

from arctic_route_planning.contracts import risk_frame_from_document
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_from_dict,
    route_motion_set_to_dict,
)
from arctic_route_planning.research.route_smoothing import EARTH_RADIUS_M
from arctic_route_planning.risk import RiskSampler

from .corridor import evaluate_continuous_raster_model_corridor
from .producer import build_route_motion_set
from .profile import EngineeringRouteMotionProfile


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_sampler(risk_store: Path, commit_path: Path) -> tuple[RiskSampler, dict[str, Any]]:
    commit = _read_object(commit_path, "RiskWindow commit")
    if commit.get("schema_version") != "bc.risk-window-commit.v1":
        raise ValueError("risk window commit schema is not bc.risk-window-commit.v1")
    frames = []
    for entry in commit.get("frames", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("risk_id"), str):
            raise ValueError("risk window contains an invalid frame identity")
        frame_path = risk_store / "frames" / f"{entry['risk_id']}.json"
        frames.append(risk_frame_from_document(_read_object(frame_path, "RiskFrame")))
    if len(frames) != commit.get("count") or len(frames) < 2:
        raise ValueError("risk window frame cardinality differs from commit")
    sampler = RiskSampler(tuple(frames))
    if commit.get("run_id") != sampler.identity.run_id or (
        commit.get("scenario_id") != sampler.identity.scenario_id
    ):
        raise ValueError("risk window identity differs from its frames")
    return sampler, commit


def _load_raster(
    path: Path, *, lon0: float, lat0: float
) -> tuple[dict[str, Any], dict[tuple[int, int], str]]:
    with xr.open_dataset(path) as dataset:
        latitudes = dataset["latitude"].values.astype(float)
        longitudes = dataset["longitude"].values.astype(float)
        values = dataset["land_sea_mask"].isel(time=0).values
    if len(latitudes) < 2 or len(longitudes) < 2:
        raise ValueError("land/sea raster is too small")
    delta_lat = float(latitudes[1] - latitudes[0])
    delta_lon = float(longitudes[1] - longitudes[0])
    if delta_lat <= 0.0 or delta_lon <= 0.0:
        raise ValueError("land/sea raster axes must be strictly increasing")
    cos_lat0 = math.cos(math.radians(lat0))
    cell_width_m = EARTH_RADIUS_M * math.radians(delta_lon) * cos_lat0
    cell_height_m = EARTH_RADIUS_M * math.radians(delta_lat)
    metadata = {
        "coordinate_frame": "c_local_equirectangular_east_north_m",
        "origin_x_m": (
            EARTH_RADIUS_M * math.radians(float(longitudes[0]) - lon0) * cos_lat0
            - cell_width_m / 2.0
        ),
        "origin_y_m": (
            EARTH_RADIUS_M * math.radians(float(latitudes[0]) - lat0)
            - cell_height_m / 2.0
        ),
        "cell_width_m": cell_width_m,
        "cell_height_m": cell_height_m,
        "rows": len(latitudes),
        "cols": len(longitudes),
        "coverage_complete": True,
        "raster_digest": _sha256_file(path),
    }
    cells = {
        (row, column): "SEA" if float(values[row, column]) == 1.0 else "LAND"
        for row in range(len(latitudes))
        for column in range(len(longitudes))
    }
    return metadata, cells


def _producer_digest() -> str:
    package = Path(__file__).resolve().parents[1]
    paths = (
        package / "contracts" / "route_motion.py",
        package / "motion" / "geometry.py",
        package / "motion" / "producer.py",
        package / "motion" / "corridor.py",
        package / "motion" / "profile.py",
        package / "publishing" / "route_motion_serialization.py",
    )
    payload = {path.relative_to(package).as_posix(): _sha256_file(path) for path in paths}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def produce(args: argparse.Namespace) -> dict[str, Any]:
    plan_document = _read_object(args.plan_set, "FourLayerRoutePlanSet")
    plan_set = four_layer_route_plan_set_from_dict(plan_document)
    run_context = _read_object(args.run_context, "RunContext")
    for name in ("run_id", "scenario_id", "corridor_id", "vessel_profile_id"):
        if run_context.get(name) != getattr(plan_set, name):
            raise ValueError(f"RunContext differs from plan set: {name}")
    profile = EngineeringRouteMotionProfile()
    if run_context.get("vessel_profile_version") != profile.vessel_profile_version:
        raise ValueError("RunContext vessel profile version differs from engineering profile")
    vessel_digest = run_context.get("vessel_profile_digest")
    if not isinstance(vessel_digest, str):
        raise ValueError("RunContext vessel_profile_digest is missing")
    sampler, commit = _load_sampler(args.risk_store, args.risk_window_commit)
    first = plan_set.layers[0].recommended.waypoints[0]
    raster_metadata, raster_cells = _load_raster(
        args.land_sea_mask, lon0=first.longitude, lat0=first.latitude
    )

    def corridor(hulls, _points, _times, expansion_m):
        evidence = evaluate_continuous_raster_model_corridor(
            raster_metadata, raster_cells, hulls, expansion_m=expansion_m
        )
        evidence["source_raster_digest"] = raster_metadata["raster_digest"]
        return evidence

    result = build_route_motion_set(
        plan_set,
        risk_window_id=commit["commit_id"],
        risk_window_digest=commit["content_digest"],
        vessel_profile_digest=vessel_digest,
        producer_digest=_producer_digest(),
        risk_sampler=sampler,
        corridor_validator=corridor,
    )
    return {
        "route-motion-set.json": route_motion_set_to_dict(result),
        "route-motion-vessel-profile.json": profile.to_dict(),
    }


def _publish(output_dir: Path, documents: dict[str, Any]) -> None:
    target = output_dir.resolve()
    if target.exists():
        raise ValueError(f"immutable output directory already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        checksums = {}
        for name, document in documents.items():
            path = staging / name
            path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            checksums[name] = _sha256_file(path)
        (staging / "checksums.json").write_text(
            json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arctic-route-motion")
    parser.add_argument("--plan-set", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--risk-store", type=Path, required=True)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--land-sea-mask", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    documents = produce(args)
    _publish(args.output_dir, documents)
    motion_set = documents["route-motion-set.json"]
    print(motion_set["motion_set_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
