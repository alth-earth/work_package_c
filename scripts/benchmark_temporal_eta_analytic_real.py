#!/usr/bin/env python3
"""Research-only analytic ETA/FIFO qualification on frozen real windows.

This sidecar evaluates conservative interval evidence for every directed edge
in the departure-time navigable component.  It never enables temporal
dominance and never substitutes a point-scan result for a continuous proof.
The historical real-input fixture loader is reused, while its point-scan
runner remains unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
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
from arctic_route_planning.planners.eta_interval import EtaInterval
from arctic_route_planning.planners.eta_interval_evaluator import (
    TemporalEtaIntervalEvaluator,
)
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.temporal_qualification import (
    FifoStatus,
    TemporalScope,
    canonical_digest,
)

SCHEMA_VERSION = "c.p0.1-temporal-eta-analytic-real.v1"
SEGMENTS = {
    "executable_0_6h": timedelta(hours=6),
    "rolling_0_24h": timedelta(hours=24),
}
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
BASE_PROBE_MINUTES = 15
FIFO_TOLERANCE_SECONDS = 1.0
MAX_REFINEMENT_LEVELS = 4
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_eta_analytic_real.py",
    "src/arctic_route_planning/risk/sampler.py",
    "src/arctic_route_planning/planners/eta_analytic.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
)


def _load_point_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_real_fixture_runner", path)
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
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
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
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _set_cpu_affinity(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable on this platform")
    os.sched_setaffinity(0, {cpu})


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
    nominal_speed = planner.vessel_model.effective_speed(1.0)
    nominal = distance / nominal_speed.speed_km_per_hour
    horizon = SEGMENTS[segment].total_seconds() / 3600.0
    lower = max(0.01, min(nominal * 0.5, horizon / 4.0))
    upper = min(horizon, max(lower, nominal * 2.0))
    return EtaInterval(lower, upper)


def _scope(
    planner: Any,
    request: Any,
    edges: tuple[Any, ...],
    probes: tuple[datetime, ...],
    policy: EtaRefinementPolicy,
) -> TemporalScope:
    base = planner.temporal_scope(request, edge_ids=edges, probe_times=probes)
    return TemporalScope.from_mapping(
        {
            **base.mapping,
            "eta_policy_digest": canonical_digest(policy),
            "edge_evaluator_digest": "explicit:real-analytic-eta-v1",
            "evaluator_certification": "uncertified:real-risk-window-v1",
            "fifo_tolerance_seconds": FIFO_TOLERANCE_SECONDS,
            "interval_probe_minutes": BASE_PROBE_MINUTES,
            "boundary_refinement_levels": MAX_REFINEMENT_LEVELS,
        }
    )


def _serialize_sample(sample: Any) -> dict[str, Any]:
    return {
        "start": sample.start,
        "end": sample.end,
        "risk_lower": sample.risk_lower,
        "risk_upper": sample.risk_upper,
        "risk_slope_lower": sample.risk_slope_lower,
        "risk_slope_upper": sample.risk_slope_upper,
        "confidence_lower": sample.confidence_lower,
        "confidence_upper": sample.confidence_upper,
        "environment_speed_factor_lower": sample.environment_speed_factor_lower,
        "environment_speed_factor_upper": sample.environment_speed_factor_upper,
        "environment_speed_factor_slope_lower": sample.environment_speed_factor_slope_lower,
        "environment_speed_factor_slope_upper": sample.environment_speed_factor_slope_upper,
        "hard_mask_possible": sample.hard_mask_possible,
        "navigability_status": sample.navigability_status,
        "covered_frame_times": sample.covered_frame_times,
        "source_risk_ids": sample.source_risk_ids,
        "coverage_complete": sample.coverage_complete,
        "evaluator_digest": sample.evaluator_digest,
        "failure_reason": sample.failure_reason,
    }


def _serialize_evidence(evidence: Any) -> dict[str, Any]:
    certificate = evidence.analytic_certificate
    return {
        "status": evidence.status.value,
        "reason": evidence.reason,
        "digest": evidence.digest,
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
        "fifo_status": evidence.fifo_status,
        "permits_dominance": evidence.permits_dominance,
        "interval_samples": [_serialize_sample(sample) for sample in evidence.interval_samples],
        "analytic_certificate": None
        if certificate is None
        else {
            "digest": certificate.digest,
            "root_status": certificate.root_status.value,
            "fifo_status": certificate.fifo_status.value,
            "root_authorized": certificate.root_authorized,
            "fifo_authorized": certificate.fifo_authorized,
            "navigation": certificate.navigation.value,
            "coverage_complete": certificate.coverage_complete,
            "evaluator_certified": certificate.evaluator_certified,
            "continuity_certified": certificate.continuity_certified,
            "contraction_bound": certificate.contraction_bound,
            "arrival_slope": certificate.arrival_slope,
            "reason": certificate.reason,
            "fifo_reason": certificate.fifo_reason,
            "scope_digest": certificate.scope.digest,
            "policy_digest": certificate.policy_digest,
            "partition_digest": certificate.partition_digest,
        },
    }


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": implementation,
        "implementation_sha256": canonical_digest(implementation),
        "uv_lock": {
            "path": str((root / "uv.lock").resolve()),
            "sha256": _sha256(root / "uv.lock"),
        },
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
        "objectives": OBJECTIVES,
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
        "mode": args.mode,
        "cpu": args.cpu,
        "worker_timeout_seconds": args.worker_timeout_seconds,
    }
    identity["fixture_digest"] = canonical_digest(
        {
            "risk_window": identity["risk_window"],
            "route_plan_set": identity["route_plan_set"],
            "input": identity["input"],
        }
    )
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{canonical_digest(identity)[:16]}"
    return identity


def _scan_objective(
    module: Any,
    fixture: Any,
    objective_name: str,
    output: Path,
    existing: dict[tuple[str, int], dict[str, Any]],
    heartbeat: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    objective = ObjectiveMode(objective_name)
    planner = module._build_planner(fixture, objective)
    request = module._request(fixture, objective)
    edges = module._edge_ids(fixture)
    probes = _probe_times(fixture)
    policy = EtaRefinementPolicy(method="bounded")
    scope = _scope(planner, request, edges, probes, policy)
    intervals: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    failure_classes: Counter[str] = Counter()
    authorized = 0
    fifo_certified = 0
    fifo_violated = 0
    for edge_index, edge in enumerate(edges):
        key = (objective_name, edge_index)
        if key in existing:
            record = existing[key]
            intervals.append(record)
            for probe_record in record.get("probe_records", []):
                evidence = probe_record.get("evidence", {})
                status = evidence.get("status")
                if isinstance(status, str):
                    status_counts[status] += 1
                if evidence.get("permits_dominance"):
                    authorized += 1
                if evidence.get("fifo_status") == FifoStatus.FIFO_CERTIFIED.value:
                    fifo_certified += 1
                if evidence.get("fifo_status") == FifoStatus.FIFO_VIOLATED.value:
                    fifo_violated += 1
            continue
        points = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[2]
        distance = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[0]
        domain = _edge_domain(planner, edge, fixture.segment)
        evaluator = TemporalEtaIntervalEvaluator(
            planner.risk_sampler,
            planner.vessel_model,
            request,
            scope,
            edge_sample_points=points,
            edge_distance_km=distance,
            planner_config=fixture.planner_config,
            eta_policy=policy,
            evaluator_certified=False,
            continuity_certified=False,
            evaluator_digest="explicit:real-analytic-eta-v1",
        )
        probe_records: list[dict[str, Any]] = []
        for probe in probes:
            evidence = evaluator.evaluate_analytic(probe, domain, scope=scope)
            serialized = _serialize_evidence(evidence)
            status_counts[evidence.status.value] += 1
            if evidence.reason:
                failure_classes[evidence.reason.split(":", 1)[0]] += 1
            authorized += int(evidence.permits_dominance)
            fifo_certified += int(evidence.fifo_status == FifoStatus.FIFO_CERTIFIED.value)
            fifo_violated += int(evidence.fifo_status == FifoStatus.FIFO_VIOLATED.value)
            probe_records.append(
                {
                    "departure": probe,
                    "domain": domain,
                    "evidence": serialized,
                }
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "input": fixture.input_name,
            "segment": fixture.segment,
            "objective": objective_name,
            "edge_id": [list(edge[0]), list(edge[1])],
            "edge_index": edge_index,
            "probe_count": len(probe_records),
            "scope_digest": scope.digest,
            "dominance_policy": "disabled",
            "dominance_pruned": 0,
            "probe_records": probe_records,
        }
        _append_jsonl(output / "eta-interval.jsonl", record)
        _append_jsonl(
            output / "cases.jsonl",
            {
                **record,
                "probe_records": None,
                "case_status": (
                    "REAL_INPUT_FIFO_VIOLATED"
                    if fifo_violated
                    else "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
                ),
            },
        )
        existing[key] = record
        intervals.append(record)
        _atomic_json(
            heartbeat,
            {
                "status": "RUNNING",
                "updated_at": datetime.now(UTC),
                "objective": objective_name,
                "completed_edges": len([item for item in existing if item[0] == objective_name]),
                "expected_edges": len(edges),
            },
        )
    all_probe_evidence = [
        probe.get("evidence", {})
        for record in intervals
        for probe in record.get("probe_records", [])
    ]
    if fifo_violated:
        status = "REAL_INPUT_FIFO_VIOLATED"
        reason = "analytic arrival slope contains a FIFO counterexample"
    elif all(
        item.get("fifo_status") == FifoStatus.FIFO_CERTIFIED.value
        for item in all_probe_evidence
    ):
        status = "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
        reason = "all interval probes have an independently authorized FIFO certificate"
    else:
        status = "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
        reason = "finite real-window evidence lacks a complete certified evaluator/interval proof"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective_name,
        "edge_count": len(edges),
        "probe_count": len(probes),
        "interval_evaluations": len(edges) * len(probes),
        "status_counts": dict(status_counts),
        "failure_classes": dict(failure_classes),
        "scope_digest": scope.digest,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "authorization_count": authorized,
        "fifo_certified_count": fifo_certified,
        "fifo_violated_count": fifo_violated,
        "coverage_complete": all(
            bool(item.get("evidence", {}).get("coverage_complete"))
            for item in all_probe_evidence
        ),
        "evaluator_certified": all(
            bool(item.get("evidence", {}).get("evaluator_certified"))
            for item in all_probe_evidence
        ),
        "deterministic": len(
            {
                item.get("evidence", {}).get("digest") for item in all_probe_evidence
            }
        )
        == len(all_probe_evidence),
    }
    return summary, intervals


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("analytic real-input qualification timeout")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("fifo-scan", "interval-qualification", "both"),
        default="both",
    )
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--objective", choices=("all", *OBJECTIVES), default="all")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    _set_cpu_affinity(args.cpu)
    module = _load_point_runner()
    fixture = module._load_fixture(args)
    identity = _identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("analytic real qualification requires a clean worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "experiment_id": identity["experiment_id"],
        "identity": identity,
        "dominance_policy": "disabled",
        "evidence_files": (
            "manifest.json",
            "cases.jsonl",
            "fifo-scan.jsonl",
            "eta-interval.jsonl",
            "resource-frontier.jsonl",
            "comparison-summary.json",
            "heartbeat.json",
        ),
    }
    _atomic_json(manifest_path, manifest)
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    existing: dict[tuple[str, int], dict[str, Any]] = {}
    for record in _read_jsonl(output / "eta-interval.jsonl"):
        objective = record.get("objective")
        edge_index = record.get("edge_index")
        probes = record.get("probe_records")
        if (
            isinstance(objective, str)
            and isinstance(edge_index, int)
            and isinstance(probes, list)
            and len(probes) > 0
            and record.get("scope_digest")
            and record.get("dominance_pruned") == 0
        ):
            key = (objective, edge_index)
            if key in existing:
                raise RuntimeError("resume evidence contains duplicate edge records")
            existing[key] = record
    objectives = OBJECTIVES if args.objective == "all" else (args.objective,)
    old_alarm = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
    started = time.perf_counter()
    summaries: list[dict[str, Any]] = []
    try:
        for objective in objectives:
            summary, _ = _scan_objective(
                module,
                fixture,
                objective,
                output,
                existing,
                heartbeat,
            )
            summary["elapsed_seconds"] = time.perf_counter() - started
            summaries.append(summary)
    except _WorkerTimeout as error:
        final = {
            "schema_version": SCHEMA_VERSION,
            "status": "STOPPED_HARD",
            "reason": str(error),
            "completed_objectives": len(summaries),
            "dominance_policy": "disabled",
        }
        _atomic_json(output / "comparison-summary.json", final)
        manifest.update(
            {"status": "STOPPED_HARD", "summary": final, "completed_at": datetime.now(UTC)}
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(heartbeat, {"status": "STOPPED_HARD", "updated_at": datetime.now(UTC)})
        (output / "STOPPED_HARD").write_text(str(error) + "\n", encoding="utf-8")
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)

    all_uncertain = all(
        summary["status"] == "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
        for summary in summaries
    )
    any_violated = any(summary["status"] == "REAL_INPUT_FIFO_VIOLATED" for summary in summaries)
    all_ready = all(
        summary["status"] == "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
        for summary in summaries
    )
    if any_violated:
        status = "REAL_INPUT_FIFO_VIOLATED"
    elif all_ready and summaries:
        status = "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
    elif all_uncertain and summaries:
        status = "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
    else:
        status = "INVALID/PENDING"
    aggregate = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objectives": objectives,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "proof_ready": status == "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN",
        "objective_summaries": summaries,
        "edge_count": sum(summary["edge_count"] for summary in summaries),
        "interval_evaluations": sum(summary["interval_evaluations"] for summary in summaries),
        "authorization_count": sum(summary["authorization_count"] for summary in summaries),
        "fifo_certified_count": sum(summary["fifo_certified_count"] for summary in summaries),
        "fifo_violated_count": sum(summary["fifo_violated_count"] for summary in summaries),
        "deterministic": all(summary["deterministic"] for summary in summaries),
    }
    _write_jsonl(output / "fifo-scan.jsonl", summaries)
    _write_jsonl(
        output / "resource-frontier.jsonl",
        [
            {
                "schema_version": SCHEMA_VERSION,
                "mode": "resource-frontier",
                "dominance_policy": "disabled",
                "dominance_pruned": 0,
                "status": "NOT_RUN_BY_DESIGN",
                "reason": (
                    "this sidecar only qualifies ETA/FIFO; exact-arrival resources "
                    "remain in the frozen real runner"
                ),
            }
        ],
    )
    _atomic_json(output / "comparison-summary.json", aggregate)
    manifest.update({"status": status, "summary": aggregate, "completed_at": datetime.now(UTC)})
    _atomic_json(manifest_path, manifest)
    _atomic_json(heartbeat, {"status": status, "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(status + "\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
