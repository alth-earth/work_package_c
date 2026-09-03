"""CLI producer for an immutable formal C -> D route-motion artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import xarray as xr

from arctic_route_planning.contracts import HOURLY_RISK_INTERVAL, risk_frame_from_document
from arctic_route_planning.errors import ContractError
from arctic_route_planning.publishing import (
    four_layer_route_plan_set_from_dict,
    route_motion_candidate_set_to_dict,
    route_motion_set_to_dict,
)
from arctic_route_planning.publishing.route_motion_serialization import canonical_sha256
from arctic_route_planning.research.route_smoothing import EARTH_RADIUS_M
from arctic_route_planning.risk import RiskSampler
from arctic_route_planning.timeutils import parse_utc

from .corridor import evaluate_continuous_raster_model_corridor
from .producer import (
    build_route_motion_candidate_set_with_evidence,
    build_route_motion_set_with_evidence,
    merge_route_motion_qualification_evidence,
)
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
    count = commit.get("count")
    interval_seconds = commit.get("interval_seconds")
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or count < 2
        or isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds != int(HOURLY_RISK_INTERVAL.total_seconds())
    ):
        raise ValueError("formal motion requires a complete hourly RiskWindow")
    try:
        start_time = parse_utc(commit["start"], field="risk_window.start")
        end_time = parse_utc(commit["end"], field="risk_window.end")
    except (KeyError, AttributeError, TypeError, ContractError) as exc:
        raise ValueError("risk window commit has invalid start/end time") from exc
    expected_end = start_time + HOURLY_RISK_INTERVAL * (count - 1)
    if end_time != expected_end:
        raise ValueError("risk window commit is not an exact hourly closed interval")
    frames = []
    for entry in commit.get("frames", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("risk_id"), str):
            raise ValueError("risk window contains an invalid frame identity")
        frame_path = risk_store / "frames" / f"{entry['risk_id']}.json"
        frames.append(risk_frame_from_document(_read_object(frame_path, "RiskFrame")))
    if len(frames) != count:
        raise ValueError("risk window frame cardinality differs from commit")
    expected_times = tuple(
        start_time + HOURLY_RISK_INTERVAL * index for index in range(count)
    )
    actual_times = tuple(frame.valid_time for frame in frames)
    if actual_times != expected_times:
        raise ValueError("risk window frames contain a missing or misplaced hourly frame")
    sampler = RiskSampler(tuple(frames), max_frame_gap=HOURLY_RISK_INTERVAL)
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


class _RasterCorridorValidator:
    """Formal raster proof with a cheap gate-1--3 edge-screen path."""

    def __init__(
        self,
        metadata: dict[str, Any],
        cells: dict[tuple[int, int], str],
    ) -> None:
        self._metadata = metadata
        self._cells = cells

    def __call__(
        self,
        hulls: Any,
        _points: Any,
        _times: Any,
        expansion_m: float,
    ) -> dict[str, Any]:
        evidence = evaluate_continuous_raster_model_corridor(
            self._metadata,
            self._cells,
            hulls,
            expansion_m=expansion_m,
            compute_clearance=True,
            clearance_hull_count=getattr(hulls, "clearance_hull_count", None),
        )
        evidence["source_raster_digest"] = self._metadata["raster_digest"]
        return evidence

    def for_edge(
        self,
        hulls: Any,
        _points: Any,
        _times: Any,
        expansion_m: float,
    ) -> dict[str, Any]:
        """Run containment only; adaptive clearance is a final-route gate."""

        evidence = evaluate_continuous_raster_model_corridor(
            self._metadata,
            self._cells,
            hulls,
            expansion_m=expansion_m,
            compute_clearance=False,
            clearance_hull_count=getattr(hulls, "clearance_hull_count", None),
        )
        evidence["source_raster_digest"] = self._metadata["raster_digest"]
        return evidence


def _producer_digest() -> str:
    package = Path(__file__).resolve().parents[1]
    project_root = package.parent.parent
    paths = (
        package / "contracts" / "route_motion.py",
        package / "motion" / "geometry.py",
        package / "motion" / "any_angle.py",
        package / "motion" / "joint_smoothing.py",
        package / "motion" / "producer.py",
        package / "motion" / "corridor.py",
        package / "motion" / "anchoring.py",
        package / "motion" / "profile.py",
        package / "research" / "route_smoothing.py",
        package / "research" / "route_smoothing_multispan.py",
        package / "research" / "route_smoothing_v2.py",
        package / "risk" / "sampler.py",
        package / "publishing" / "route_motion_serialization.py",
        package.parent.parent / "schemas" / "route-motion-qualification-evidence-v1.schema.json",
    )
    payload = {path.relative_to(project_root).as_posix(): _sha256_file(path) for path in paths}
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
    producer_digest = _producer_digest()
    first = plan_set.layers[0].recommended.waypoints[0]
    raster_metadata, raster_cells = _load_raster(
        args.land_sea_mask, lon0=first.longitude, lat0=first.latitude
    )

    corridor = _RasterCorridorValidator(raster_metadata, raster_cells)

    result, evidence = build_route_motion_set_with_evidence(
        plan_set,
        risk_window_id=commit["commit_id"],
        risk_window_digest=commit["content_digest"],
        vessel_profile_digest=vessel_digest,
        producer_digest=producer_digest,
        risk_sampler=sampler,
        corridor_validator=corridor,
    )
    documents = {
        "route-motion-set.json": route_motion_set_to_dict(result),
        "route-motion-qualification-evidence.json": evidence,
        "route-motion-vessel-profile.json": profile.to_dict(),
    }
    include_candidate_set = bool(
        getattr(args, "include_candidate_set", False)
        or getattr(args, "require_all_curves", False)
    )
    if include_candidate_set:
        candidate_result, candidate_evidence = build_route_motion_candidate_set_with_evidence(
            plan_set,
            risk_window_id=commit["commit_id"],
            risk_window_digest=commit["content_digest"],
            vessel_profile_digest=vessel_digest,
            producer_digest=producer_digest,
            risk_sampler=sampler,
            corridor_validator=corridor,
            profile=profile,
        )
        documents["route-motion-candidate-set.json"] = (
            route_motion_candidate_set_to_dict(candidate_result)
        )
        documents["route-motion-qualification-evidence.json"] = (
            merge_route_motion_qualification_evidence(evidence, candidate_evidence)
        )
    if getattr(args, "require_all_curves", False):
        _assert_all_curve_records(documents)
    return documents


def _assert_all_curve_records(documents: dict[str, Any]) -> None:
    """Reject a strict publication unless all r17 records are real curves.

    The ordinary producer keeps its historical fail-closed raw fallback.  The
    r17 release command opts into this additional atomic guard: four
    recommended layer records and three full-voyage objective records must be
    present and every one must have passed the formal producer gates.
    """

    motion_document = documents.get("route-motion-set.json")
    candidate_document = documents.get("route-motion-candidate-set.json")
    recommended = (
        motion_document.get("records")
        if isinstance(motion_document, dict)
        else None
    )
    candidates = (
        candidate_document.get("records")
        if isinstance(candidate_document, dict)
        else None
    )
    failures: list[str] = []
    if not isinstance(recommended, list) or len(recommended) != 4:
        failures.append("recommended_records!=4")
    if not isinstance(candidates, list) or len(candidates) != 3:
        failures.append("candidate_records!=3")
    for label, records in (("recommended", recommended), ("candidate", candidates)):
        if not isinstance(records, list):
            continue
        for index, entry in enumerate(records):
            record = entry.get("record") if label == "candidate" else entry
            if not isinstance(record, dict):
                failures.append(f"{label}[{index}].record_missing")
                continue
            if record.get("mode") != "CURVE":
                failures.append(
                    f"{label}[{index}]={record.get('plan_id', 'unknown')}:"
                    f"{record.get('mode', 'missing')}"
                )
    if failures:
        raise _StrictPublicationFailure(documents, failures)


class _StrictPublicationFailure(ValueError):
    """A strict r17 refusal carrying the complete producer evidence in memory."""

    def __init__(self, documents: dict[str, Any], failures: list[str]) -> None:
        self.documents = documents
        self.failures = tuple(failures)
        super().__init__(
            "strict all-CURVE publication refused: " + "; ".join(failures)
        )


def _failure_evidence_documents(
    output_dir: Path,
    documents: dict[str, Any],
    failure: _StrictPublicationFailure,
) -> dict[str, Any]:
    """Return a non-consumable, digest-bound record of a refused publication."""

    motion_document = documents.get("route-motion-set.json")
    candidate_document = documents.get("route-motion-candidate-set.json")
    records: list[dict[str, Any]] = []
    for artifact_kind, document, candidate in (
        ("motion_set", motion_document, False),
        ("motion_candidate_set", candidate_document, True),
    ):
        if not isinstance(document, dict) or not isinstance(document.get("records"), list):
            continue
        for entry in document["records"]:
            record = entry.get("record") if candidate and isinstance(entry, dict) else entry
            if not isinstance(record, dict):
                continue
            records.append(
                {
                    "artifact_kind": artifact_kind,
                    "objective_mode": entry.get("objective_mode") if candidate else None,
                    "planning_layer": record.get("planning_layer"),
                    "plan_id": record.get("plan_id"),
                    "mode": record.get("mode"),
                    "fallback_reason": record.get("fallback_reason"),
                    "raw_route_digest": record.get("raw_route_digest"),
                    "curve_digest": record.get("curve_digest"),
                    "motion_digest": record.get("motion_digest"),
                    "qualification": record.get("qualification"),
                }
            )
    qualification = documents.get("route-motion-qualification-evidence.json")
    if not isinstance(qualification, dict):
        raise ValueError("strict failure is missing qualification evidence")
    body = {
        "schema_version": "c.route-motion-publication-failure.v1",
        "publication_status": "FAILED",
        "requested_output_dir": str(output_dir.resolve()),
        "failure_reasons": list(failure.failures),
        "required_record_counts": {"recommended": 4, "candidate": 3},
        "record_summaries": records,
        "qualification_evidence": qualification,
    }
    return {
        "route-motion-publication-failure.json": {
            **body,
            "failure_evidence_id": "route-motion-publication-failure-sha256-"
            + canonical_sha256(body),
        },
        "route-motion-qualification-evidence.json": qualification,
    }


def _failure_evidence_dir(
    output_dir: Path, documents: dict[str, Any]
) -> Path:
    """Place immutable refusal evidence beside the formal output target."""

    qualification = documents.get("route-motion-qualification-evidence.json")
    producer_digest = (
        qualification.get("producer_digest")
        if isinstance(qualification, dict)
        else None
    )
    suffix = (
        f"-{producer_digest[:16]}"
        if isinstance(producer_digest, str) and len(producer_digest) >= 16
        else ""
    )
    return output_dir.with_name(output_dir.name + "-failure-evidence" + suffix)


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
    parser.add_argument(
        "--include-candidate-set",
        action="store_true",
        help=(
            "also publish cd.route-motion-candidate-set.v1 for the three "
            "full-voyage objectives"
        ),
    )
    parser.add_argument(
        "--require-all-curves",
        action="store_true",
        help=(
            "strict r17 publication guard: include the three full-voyage "
            "candidates and refuse publication unless all seven records are CURVE"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        documents = produce(args)
    except _StrictPublicationFailure as failure:
        failure_dir = _failure_evidence_dir(args.output_dir, failure.documents)
        _publish(
            failure_dir,
            _failure_evidence_documents(args.output_dir, failure.documents, failure),
        )
        print(
            f"{failure}; failure evidence: {failure_dir.resolve()}",
            file=sys.stderr,
        )
        return 2
    _publish(args.output_dir, documents)
    motion_set = documents["route-motion-set.json"]
    print(motion_set["motion_set_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
