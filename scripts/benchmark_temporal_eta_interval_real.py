#!/usr/bin/env python3
"""Research-only interval qualification on the frozen 145-frame windows.

The finite point-scan runner remains untouched.  This companion evaluates a
conservative ETA envelope for every edge in the departure-time connected
component and every 15-minute probe.  It never enables certified dominance;
the output is qualification evidence (or an explicit uncertainty), not a
route or a promotion decision.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_interval import EtaInterval, EtaIntervalStatus
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.temporal_qualification import TemporalScope, canonical_digest

SCHEMA_VERSION = "c.p0.1-temporal-eta-proof-real.v1"
SEGMENTS = {
    "executable_0_6h": timedelta(hours=6),
    "rolling_0_24h": timedelta(hours=24),
}
BASE_PROBE_MINUTES = 15
FIFO_TOLERANCE_SECONDS = 1.0
MAX_REFINEMENT_LEVELS = 4
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_eta_interval_real.py",
    "src/arctic_route_planning/risk/sampler.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
)


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_real_point_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the frozen real-input fixture loader")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    import hashlib

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


def _write_jsonl(path: Path, values: list[Any]) -> None:
    """Atomically replace a small summary JSONL file.

    Per-edge evidence uses :func:`_append_jsonl` so every completed edge is
    durable.  The end-of-run point-scan file contains one aggregate record;
    replacing it on resume avoids duplicate summaries while retaining the
    append-only per-edge evidence.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _resume_edge_records(
    output: Path,
    *,
    expected_input: str,
    expected_segment: str,
    expected_scope_digest: str,
    expected_probe_count: int,
    expected_edge_count: int,
) -> tuple[dict[int, dict[str, Any]], Counter[str], Counter[str]]:
    """Load only complete, identity-matching edge records for a safe resume.

    A worker is never reconstructed from an in-progress record.  Each edge is
    considered resumable only when its JSON object contains the full probe
    list, matching scope/input/segment identity, and the fixed disabled
    dominance marker.  Duplicate complete records are rejected rather than
    silently selecting one of them.
    """

    records_by_edge: dict[int, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    failure_classes: Counter[str] = Counter()
    for record in _read_jsonl(output / "eta-interval.jsonl"):
        edge_index = record.get("edge_index")
        probe_records = record.get("probe_records")
        if not isinstance(edge_index, int) or not isinstance(probe_records, list):
            continue
        if (
            record.get("schema_version") != SCHEMA_VERSION
            or record.get("input") != expected_input
            or record.get("segment") != expected_segment
            or record.get("scope_digest") != expected_scope_digest
            or record.get("dominance_policy") != "disabled"
            or record.get("dominance_pruned") != 0
            or record.get("probe_count") != expected_probe_count
            or len(probe_records) != expected_probe_count
            or edge_index < 0
            or edge_index >= expected_edge_count
        ):
            raise RuntimeError("resume evidence contains an identity-mismatched edge record")
        if edge_index in records_by_edge:
            raise RuntimeError("resume evidence contains duplicate complete edge records")
        records_by_edge[edge_index] = record
        for probe_record in probe_records:
            evidence = probe_record.get("evidence")
            if not isinstance(evidence, dict):
                raise RuntimeError("resume evidence contains an incomplete probe record")
            status = evidence.get("status")
            if isinstance(status, str):
                status_counts[status] += 1
            reason = evidence.get("reason")
            if isinstance(reason, str) and reason:
                failure_classes[reason.split(":", 1)[0]] += 1
    return records_by_edge, status_counts, failure_classes


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _scope_digest(scope: TemporalScope) -> str:
    return scope.digest


def _probe_times(fixture: Any) -> tuple[datetime, ...]:
    count = int(SEGMENTS[fixture.segment].total_seconds() // (BASE_PROBE_MINUTES * 60))
    return tuple(
        fixture.departure + timedelta(minutes=BASE_PROBE_MINUTES * index)
        for index in range(count + 1)
    )


def _edge_domain(planner: Any, edge: Any, segment: str) -> EtaInterval:
    distance, _, _ = planner._edge_geometry(
        edge[0], edge[1], minimum_samples=planner.planner_config.edge_sample_count
    )
    calm = planner.vessel_model.effective_speed(1.0)
    nominal = distance / calm.speed_km_per_hour
    horizon = SEGMENTS[segment].total_seconds() / 3600.0
    lower = max(0.01, min(nominal * 0.5, horizon / 4.0))
    upper = min(horizon, max(lower, nominal * 2.0))
    return EtaInterval(lower, upper)


def _serialize_evidence(evidence: Any) -> dict[str, Any]:
    return {
        "status": evidence.status.value,
        "reason": evidence.reason,
        "digest": evidence.digest,
        "certificate_digest": evidence.certificate_digest,
        "authorization_usable": evidence.authorization_usable,
        "coverage_complete": evidence.coverage_complete,
        "evaluator_certified": evidence.evaluator_certified,
        "continuity_certified": evidence.continuity_certified,
        "contraction_bound": evidence.contraction_bound,
        "partition_boundaries": evidence.partition_boundaries,
        "edge_factor_lower": evidence.edge_factor_lower,
        "edge_factor_upper": evidence.edge_factor_upper,
        "speed_lower_knots": evidence.speed_lower_knots,
        "speed_upper_knots": evidence.speed_upper_knots,
        "edge_distance_km": evidence.edge_distance_km,
        "interval_sample_count": len(evidence.interval_samples),
        "interval_samples": [
            {
                "start": sample.start,
                "end": sample.end,
                "risk_lower": sample.risk_lower,
                "risk_upper": sample.risk_upper,
                "confidence_lower": sample.confidence_lower,
                "environment_speed_factor_lower": sample.environment_speed_factor_lower,
                "environment_speed_factor_upper": sample.environment_speed_factor_upper,
                "hard_mask_possible": sample.hard_mask_possible,
                "covered_frame_times": sample.covered_frame_times,
                "source_risk_ids": sample.source_risk_ids,
                "coverage_complete": sample.coverage_complete,
                "evaluator_digest": sample.evaluator_digest,
                "failure_reason": sample.failure_reason,
            }
            for sample in evidence.interval_samples
        ],
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": {"files": files, "sha256": canonical_digest(files)},
        "uv_lock": {"path": str((root / "uv.lock").resolve()), "sha256": _sha256(root / "uv.lock")},
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _tree_digest(fixture.config_root),
        },
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
        },
        "route_plan_set": {
            "path": str(fixture.route_plan_path),
            "sha256": _sha256(fixture.route_plan_path),
        },
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "eta_policy": asdict(EtaRefinementPolicy(method="bounded")),
        "dominance_policy": "disabled",
        "fifo_tolerance_seconds": FIFO_TOLERANCE_SECONDS,
        "probe_interval_minutes": BASE_PROBE_MINUTES,
        "max_refinement_levels": MAX_REFINEMENT_LEVELS,
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
        "cpu": args.cpu,
        "worker_timeout_seconds": args.worker_timeout_seconds,
    }


def _run_scan(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    module = _load_point_runner()
    fixture = module._load_fixture(args)
    planner = module._build_planner(fixture, ObjectiveMode.FASTEST)
    request = module._request(fixture, ObjectiveMode.FASTEST)
    edges = module._edge_ids(fixture)
    probes = _probe_times(fixture)
    control_scope = planner.temporal_scope(request, edge_ids=edges, probe_times=probes)
    sampler = planner.risk_sampler
    policy = EtaRefinementPolicy(method="bounded")
    # The formal planner scope carries its historical control ETA policy.
    # Interval evidence is a separate research sidecar and must bind the
    # explicit bounded policy rather than falsely reporting a policy mismatch.
    scope = TemporalScope.from_mapping(
        {
            **control_scope.mapping,
            "eta_policy_digest": canonical_digest(policy),
            "edge_evaluator_digest": "explicit:real-eta-interval-v1",
        }
    )
    existing_edges, existing_status_counts, existing_errors = _resume_edge_records(
        output,
        expected_input=fixture.input_name,
        expected_segment=fixture.segment,
        expected_scope_digest=scope.digest,
        expected_probe_count=len(probes),
        expected_edge_count=len(edges),
    )
    errors: Counter[str] = Counter(existing_errors)
    status_counts: Counter[str] = Counter(existing_status_counts)
    started = time.perf_counter()
    for edge_index, edge in enumerate(edges):
        if edge_index in existing_edges:
            _atomic_json(
                output / "heartbeat.json",
                {
                    "status": "RUNNING",
                    "updated_at": datetime.now(UTC),
                    "completed_edges": len(existing_edges),
                    "expected_edges": len(edges),
                    "elapsed_seconds": time.perf_counter() - started,
                    "resumed": True,
                },
            )
            continue
        points = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[2]
        distance = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[0]
        domain = _edge_domain(planner, edge, fixture.segment)
        evaluator = TemporalEtaIntervalEvaluator(
            sampler,
            planner.vessel_model,
            request,
            scope,
            edge_sample_points=points,
            edge_distance_km=distance,
            planner_config=fixture.planner_config,
            eta_policy=policy,
            evaluator_certified=False,
            continuity_certified=False,
        )
        edge_records: list[dict[str, Any]] = []
        for probe in probes:
            evidence = evaluator.evaluate(probe, domain)
            status_counts[evidence.status.value] += 1
            if evidence.reason:
                errors[evidence.reason.split(":", 1)[0]] += 1
            edge_records.append(
                {
                    "departure": probe,
                    "domain": domain,
                    "evidence": _serialize_evidence(evidence),
                }
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "input": fixture.input_name,
            "segment": fixture.segment,
            "edge_id": [list(edge[0]), list(edge[1])],
            "edge_index": edge_index,
            "probe_count": len(edge_records),
            "scope_digest": _scope_digest(scope),
            "dominance_policy": "disabled",
            "dominance_pruned": 0,
            "probe_records": edge_records,
        }
        _append_jsonl(output / "eta-interval.jsonl", record)
        _append_jsonl(
            output / "cases.jsonl",
            {
                **record,
                "probe_records": None,
                "status": (
                    "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
                    if not any(
                        item["evidence"]["status"] == EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value
                        for item in edge_records
                    )
                    else "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
                ),
            },
        )
        existing_edges[edge_index] = record
        _atomic_json(
            output / "heartbeat.json",
            {
                "status": "RUNNING",
                "updated_at": datetime.now(UTC),
                "completed_edges": len(existing_edges),
                "expected_edges": len(edges),
                "elapsed_seconds": time.perf_counter() - started,
            },
        )
    point_scan = module._fifo_scan(args)
    counterexample = point_scan.get("counterexample")
    if counterexample is not None:
        status = "REAL_INPUT_FIFO_VIOLATED"
        reason = "interval qualification includes a finite sampled counterexample"
    else:
        status = "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
        reason = "no counterexample observed; evaluator/continuity proof is incomplete"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "edge_count": len(edges),
        "probe_count": len(probes),
        "interval_evaluations": len(edges) * len(probes),
        "status_counts": dict(status_counts),
        "failure_classes": dict(errors),
        "scope_digest": scope.digest,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "counterexample": counterexample,
        "coverage_complete": False,
        "evaluator_certified": False,
        "authorization_count": 0,
        "point_scan": point_scan,
    }


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("real ETA interval qualification timeout")


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    module = _load_point_runner()
    fixture = module._load_fixture(args)
    identity = _identity(args, fixture, root)
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{canonical_digest(identity)[:16]}"
    if identity["git"]["git_dirty"]:
        raise RuntimeError("real interval evidence requires a clean implementation worktree")
    manifest_path = output / "manifest.json"
    previous = None
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "experiment_id": identity["experiment_id"],
        "identity": identity,
        "evidence_files": (
            "manifest.json",
            "cases.jsonl",
            "fifo-scan.jsonl",
            "eta-interval.jsonl",
            "comparison-summary.json",
            "heartbeat.json",
        ),
    }
    if previous is not None:
        manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
    _atomic_json(manifest_path, manifest)
    old_alarm = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
    try:
        summary = _run_scan(args, output)
    except _WorkerTimeout as error:
        summary = {
            "schema_version": SCHEMA_VERSION,
            "status": "STOPPED_HARD",
            "reason": str(error),
            "proof_ready": False,
        }
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {"status": "STOPPED_HARD", "summary": summary, "completed_at": datetime.now(UTC)}
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            output / "heartbeat.json",
            {"status": "STOPPED_HARD", "updated_at": datetime.now(UTC)},
        )
        (output / "STOPPED_HARD").write_text(str(error) + "\n", encoding="utf-8")
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)
    _atomic_json(output / "comparison-summary.json", summary)
    _write_jsonl(output / "fifo-scan.jsonl", [summary["point_scan"]])
    final_status = summary["status"]
    manifest.update({"status": final_status, "summary": summary, "completed_at": datetime.now(UTC)})
    _atomic_json(manifest_path, manifest)
    _atomic_json(
        output / "heartbeat.json",
        {"status": final_status, "updated_at": datetime.now(UTC)},
    )
    (output / "ALL_DONE").write_text(final_status + "\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
