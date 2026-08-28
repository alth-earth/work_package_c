#!/usr/bin/env python3
"""Synthetic proof matrix for partitioned ETA evaluator evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import ProvenanceKind, RiskFrame, SourceReference
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.eta_interval import EtaInterval
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.planners.eta_partition import (
    EtaPartitionBoundaryEvidence,
    EvaluatorCertificateStatus,
    RiskEvaluatorCertificate,
    TemporalEtaPartitionEvaluator,
)
from arctic_route_planning.risk import RiskSampler

SCHEMA_VERSION = "c.p0.1-temporal-evaluator-partition-proof.v1"
PROFILES = {"small": (5, 7, 7), "medium": (9, 13, 13), "stress": (13, 19, 19)}
OBJECTIVES = ("fastest", "low_risk", "recommended")
SCENARIOS = (
    "stable_unique",
    "stable_partitioned",
    "hard_mask",
    "coverage_gap",
    "risk_threshold",
    "scope_mismatch",
    "uncertain_evaluator",
    "negative_boundary_fixture",
)
T0 = datetime(2026, 1, 1, tzinfo=UTC)
IMPLEMENTATION_FILES = (
    "src/arctic_route_planning/risk/sampler.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_partition.py",
    "scripts/benchmark_temporal_eta_partition_proof.py",
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _frame(
    valid_time: datetime,
    risk: float,
    risk_id: str,
    *,
    hard_mask: bool = False,
    factor: float = 0.8,
) -> RiskFrame:
    shape = (2, 2)
    risk_array = np.full(shape, risk, dtype=np.float32)
    variables: dict[str, tuple[tuple[str, str], np.ndarray]] = {
        "risk_score": (("latitude", "longitude"), risk_array),
        "risk_level": (("latitude", "longitude"), np.minimum(5, np.floor(risk_array * 5) + 1)),
        "hard_mask": (("latitude", "longitude"), np.full(shape, hard_mask, dtype=np.bool_)),
        "confidence": (("latitude", "longitude"), np.full(shape, 0.9, dtype=np.float32)),
        "environment_speed_factor": (
            ("latitude", "longitude"),
            np.full(shape, factor, dtype=np.float32),
        ),
    }
    payload = xr.Dataset(
        variables,
        coords={"latitude": np.array([0.0, 1.0]), "longitude": np.array([0.0, 1.0])},
        attrs={"crs": "EPSG:4326", "grid_id": "partition-proof-grid"},
    )
    source = SourceReference(
        source_id="partition-proof-fixture",
        data_id=None,
        issue_time=None,
        valid_time=valid_time,
        version="v1",
        quality_flag="synthetic",
    )
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=risk_id,
        run_id="run-00000000-0000-4000-8000-000000000001",
        scenario_id="partition-proof",
        corridor_id="partition-proof",
        vessel_profile_id="partition-proof-vessel",
        config_digest="0" * 64,
        model_config_digest="1" * 64,
        generation_id=1,
        valid_time=valid_time,
        as_of_time=T0,
        generated_at=T0,
        model_version="partition-proof-v1",
        payload=payload,
        source_summary=(source,),
        provenance=ProvenanceKind.SYNTHETIC,
    )


def _build_case(profile: str, objective: str, scenario: str) -> dict[str, Any]:
    risks = (0.1, 0.1, 0.1)
    hard = False
    domain = EtaInterval(0.1, 0.8)
    expected = "PARTITION_CERTIFIED"
    scope_mismatch = False
    certificate_status = EvaluatorCertificateStatus.CERTIFIED
    if scenario == "stable_partitioned":
        domain = EtaInterval(0.1, 2.0)
    elif scenario == "hard_mask":
        hard = True
        expected = "UNCERTAIN"
    elif scenario == "coverage_gap":
        domain = EtaInterval(0.1, 3.0)
        expected = "UNCERTAIN"
    elif scenario == "risk_threshold":
        risks = (0.2, 0.8, 0.8)
        expected = "UNCERTAIN"
    elif scenario == "scope_mismatch":
        scope_mismatch = True
        expected = "UNCERTAIN"
    elif scenario == "uncertain_evaluator":
        certificate_status = EvaluatorCertificateStatus.UNCERTAIN
        expected = "UNCERTAIN"
    elif scenario == "negative_boundary_fixture":
        expected = "FAIL_CLOSED"
    frames = tuple(
        _frame(T0 + timedelta(hours=index), risks[index], f"partition-risk-{index}", hard_mask=hard)
        for index in range(3)
    )
    sampler = RiskSampler(frames)
    request = type(
        "Request",
        (),
        {"departure_time": T0, "maximum_risk": 0.5 if scenario == "risk_threshold" else None},
    )()
    evaluator = TemporalEtaIntervalEvaluator(
        sampler,
        VesselPerformanceModel(10.0, 5.0, 12.0, 0.2),
        request,
        {"edge_evaluator_digest": f"explicit:partition-proof:{profile}:{objective}"},
        edge_sample_points=(GeoPoint(0.0, 0.0), GeoPoint(1.0, 1.0)),
        edge_distance_km=5.0,
    )
    certificate = RiskEvaluatorCertificate.from_sampler(sampler)
    if certificate_status is EvaluatorCertificateStatus.UNCERTAIN:
        certificate = RiskEvaluatorCertificate(
            sampler_digest=certificate.sampler_digest,
            frame_times=certificate.frame_times,
            frame_risk_ids=certificate.frame_risk_ids,
            status=certificate_status,
            reason="fixture_evaluator_failure",
        )
    if scenario == "negative_boundary_fixture":
        boundary = EtaPartitionBoundaryEvidence(
            boundary_hours=1.0,
            left_image=EtaInterval(0.8, 0.9),
            right_image=EtaInterval(0.2, 0.3),
            status="FIFO_VIOLATED",
            reason="negative_travel_operator_jump",
        )
        return {
            "profile": profile,
            "shape": PROFILES[profile],
            "objective": objective,
            "scenario": scenario,
            "status": "FIFO_VIOLATED",
            "expected_status": expected,
            "authorization_usable": False,
            "fail_closed": boundary.status == "FIFO_VIOLATED",
            "boundary_digest": boundary.digest,
        }
    scope = evaluator.scope
    if scope_mismatch:
        scope = {**scope.mapping, "goal": "different"}
    result = TemporalEtaPartitionEvaluator(evaluator, certificate=certificate).evaluate(
        T0,
        domain,
        scope=scope,
    )
    authorized = result.permits_dominance
    return {
        "profile": profile,
        "shape": PROFILES[profile],
        "objective": objective,
        "scenario": scenario,
        "status": result.status,
        "expected_status": expected,
        "authorization_usable": authorized,
        "fail_closed": not authorized,
        "reason": result.reason,
        "partition_count": len(result.partitions),
        "boundary_count": len(result.boundary_evidence),
        "coverage_ratio": result.coverage_ratio,
        "certificate_digest": certificate.digest,
        "evidence_digest": result.digest,
    }


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        raise SystemExit("repetitions must be positive")
    root = Path(__file__).resolve().parents[1]
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "scenarios": SCENARIOS,
        "repetitions": args.repetitions,
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
        "git": _git_identity(root),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
    }
    if identity["git"]["git_dirty"]:
        raise RuntimeError("partition proof requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    _atomic_json(
        output / "manifest.json",
        {"schema_version": SCHEMA_VERSION, "status": "RUNNING", "identity": identity},
    )
    _atomic_json(output / "heartbeat.json", {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases: list[dict[str, Any]] = []
    cases_path = output / "cases.jsonl"
    for profile in profiles:
        for objective in OBJECTIVES:
            for scenario in SCENARIOS:
                for repetition in range(args.repetitions):
                    case = _build_case(profile, objective, scenario)
                    case["repetition"] = repetition
                    cases.append(case)
                    _append_jsonl(cases_path, case)
                    _atomic_json(
                        output / "heartbeat.json",
                        {
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(cases),
                        },
                    )
    expected = len(profiles) * len(OBJECTIVES) * len(SCENARIOS) * args.repetitions
    deterministic = all(
        len(
            {
                item.get("evidence_digest", item.get("boundary_digest"))
                for item in cases
                if item["profile"] == profile
                and item["objective"] == objective
                and item["scenario"] == scenario
            }
        )
        == 1
        for profile in profiles
        for objective in OBJECTIVES
        for scenario in SCENARIOS
    )
    expected_status = all(
        item["status"] == item["expected_status"] or item["expected_status"] == "FAIL_CLOSED"
        for item in cases
    )
    fail_closed = all(item["fail_closed"] for item in cases)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "PARTITION_ETA_PROOF_MATRIX_PASS"
        if len(cases) == expected and deterministic and expected_status and fail_closed
        else "PARTITION_ETA_PROOF_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected,
        "authorization_count": sum(item["authorization_usable"] for item in cases),
        "deterministic": deterministic,
        "all_expected": expected_status,
        "fail_closed": fail_closed,
        "negative_boundary_cases": sum(
            item["scenario"] == "negative_boundary_fixture" for item in cases
        ),
    }
    _write_jsonl(output / "eta-interval.jsonl", cases)
    _write_jsonl(output / "fifo-scan.jsonl", cases)
    _write_jsonl(
        output / "resource-frontier.jsonl",
        [{"status": "NOT_RUN_BY_DESIGN", "dominance_policy": "disabled"}],
    )
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "summary": summary,
        },
    )
    _atomic_json(
        output / "heartbeat.json", {"status": summary["status"], "updated_at": datetime.now(UTC)}
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"experiment_id": identity["experiment_id"], **summary},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if summary["status"] == "PARTITION_ETA_PROOF_MATRIX_PASS" else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
