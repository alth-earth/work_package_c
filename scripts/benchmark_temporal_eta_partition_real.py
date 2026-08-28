#!/usr/bin/env python3
"""Research-only partitioned ETA qualification on frozen real RiskFrames.

The runner reuses the frozen fixture loader but evaluates each edge through
the sampler-derived partition certificate.  It never enables dominance or
changes the production planner.
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
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.eta_interval import EtaInterval
from arctic_route_planning.planners.eta_interval_evaluator import TemporalEtaIntervalEvaluator
from arctic_route_planning.planners.eta_partition import TemporalEtaPartitionEvaluator
from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy
from arctic_route_planning.planners.temporal_qualification import TemporalScope, canonical_digest

SCHEMA_VERSION = "c.p0.1-temporal-evaluator-partition-real.v1"
SEGMENTS = {"executable_0_6h": timedelta(hours=6), "rolling_0_24h": timedelta(hours=24)}
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
BASE_PROBE_MINUTES = 15
FIFO_TOLERANCE_SECONDS = 1.0
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_eta_partition_real.py",
    "src/arctic_route_planning/risk/sampler.py",
    "src/arctic_route_planning/planners/eta_analytic.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_partition.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
)


def _load_fixture_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_partition_fixture_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen real-input fixture runner")
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
    planner: Any, request: Any, edges: tuple[Any, ...], probes: tuple[datetime, ...]
) -> TemporalScope:
    policy = EtaRefinementPolicy(method="bounded")
    base = planner.temporal_scope(request)
    return TemporalScope.from_mapping(
        {
            **base.mapping,
            "eta_policy_digest": canonical_digest(policy),
            "edge_evaluator_digest": "explicit:real-partition-eta-v1",
            "edge_set_digest": canonical_digest(edges),
            "probe_set_digest": canonical_digest(probes),
            "fifo_tolerance_seconds": FIFO_TOLERANCE_SECONDS,
            "interval_probe_minutes": BASE_PROBE_MINUTES,
            "partition_rule": "risk-frame-arrival-events-v1",
        }
    )


def _serialize_partition(result: Any) -> dict[str, Any]:
    return {
        "status": result.status,
        "reason": result.reason,
        "digest": result.digest,
        "scope_digest": result.scope.digest,
        "boundaries": result.boundaries,
        "coverage_ratio": result.coverage_ratio,
        "certified_partition_count": result.certified_partition_count,
        "blocked_partition_count": result.blocked_partition_count,
        "permits_dominance": result.permits_dominance,
        "evaluator_certificate": {
            "status": result.evaluator_certificate.status.value,
            "proof_digest": result.evaluator_certificate.proof_digest,
            "identity": result.evaluator_certificate.identity,
        },
        "partitions": [
            {
                "status": item.status.value,
                "reason": item.reason,
                "image": item.image,
                "digest": item.digest,
                "fifo_status": item.fifo_status,
                "analytic_certificate": None
                if item.analytic_certificate is None
                else {
                    "root_status": item.analytic_certificate.root_status.value,
                    "fifo_status": item.analytic_certificate.fifo_status.value,
                    "root_authorized": item.analytic_certificate.root_authorized,
                    "contraction_bound": item.analytic_certificate.contraction_bound,
                },
            }
            for item in result.partitions
        ],
        "boundary_evidence": [
            {
                "boundary_hours": item.boundary_hours,
                "status": item.status,
                "reason": item.reason,
                "left_image": item.left_image,
                "right_image": item.right_image,
                "digest": item.digest,
            }
            for item in result.boundary_evidence
        ],
    }


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("partitioned real-input qualification timeout")


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": implementation,
        "implementation_sha256": canonical_digest(implementation),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "config_root_sha256": _tree_digest(fixture.config_root),
        "risk_window": {
            "path": str(fixture.commit_path),
            "sha256": _sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
        },
        "route_plan_set_sha256": _sha256(fixture.route_plan_path),
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
        "mode": args.mode,
        "objective": args.objective,
        "cpu": args.cpu,
        "worker_timeout_seconds": args.worker_timeout_seconds,
    }
    identity["fixture_digest"] = canonical_digest(
        identity["input"]
        | {
            "risk_window": identity["risk_window"],
            "route_plan_set_sha256": identity["route_plan_set_sha256"],
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
    scope = _scope(planner, request, edges, probes)
    policy = EtaRefinementPolicy(method="bounded")
    records: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    boundary_counts: Counter[str] = Counter()
    failure_classes: Counter[str] = Counter()
    deterministic = True
    certified = 0
    violated = 0
    for edge_index, edge in enumerate(edges):
        key = (objective_name, edge_index)
        edge_scope = TemporalScope.from_mapping({**scope.mapping, "edge_id": edge})
        record = existing.get(key)
        if record is not None:
            if (
                record.get("schema_version") != SCHEMA_VERSION
                or record.get("scope_digest") != edge_scope.digest
                or record.get("probe_count") != len(probes)
                or record.get("dominance_policy") != "disabled"
            ):
                raise RuntimeError("resume evidence contains an identity-mismatched edge record")
            records.append(record)
            for probe in record.get("probe_records", []):
                evidence = probe.get("evidence", {})
                status_counts[str(evidence.get("status"))] += 1
                certified += int(evidence.get("certified_partition_count", 0) > 0)
                violated += int(evidence.get("status") == "FIFO_VIOLATED")
            deterministic = deterministic and bool(record.get("deterministic", False))
            continue
        points = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[2]
        distance = planner._edge_geometry(
            edge[0], edge[1], minimum_samples=request.edge_sample_count
        )[0]
        evaluator = TemporalEtaIntervalEvaluator(
            planner.risk_sampler,
            planner.vessel_model,
            request,
            edge_scope,
            edge_sample_points=points,
            edge_distance_km=distance,
            planner_config=fixture.planner_config,
            eta_policy=policy,
            evaluator_digest="explicit:real-partition-eta-v1",
        )
        partition_evaluator = TemporalEtaPartitionEvaluator(
            evaluator,
            tolerance_seconds=FIFO_TOLERANCE_SECONDS,
        )
        domain = _edge_domain(planner, edge, fixture.segment)
        probe_records: list[dict[str, Any]] = []
        edge_deterministic = True
        for probe in probes:
            result = partition_evaluator.evaluate(probe, domain, scope=edge_scope)
            repeat = partition_evaluator.evaluate(probe, domain, scope=edge_scope)
            edge_deterministic = edge_deterministic and result.digest == repeat.digest
            evidence = _serialize_partition(result)
            status_counts[result.status] += 1
            boundary_counts.update(item.status for item in result.boundary_evidence)
            if result.reason:
                failure_classes[result.reason.split(":", 1)[0]] += 1
            certified += int(result.certified_partition_count > 0)
            violated += int(result.status == "FIFO_VIOLATED")
            probe_records.append({"departure": probe, "domain": domain, "evidence": evidence})
        record = {
            "schema_version": SCHEMA_VERSION,
            "input": fixture.input_name,
            "segment": fixture.segment,
            "objective": objective_name,
            "edge_id": [list(edge[0]), list(edge[1])],
            "edge_index": edge_index,
            "probe_count": len(probe_records),
            "scope_digest": edge_scope.digest,
            "dominance_policy": "disabled",
            "dominance_pruned": 0,
            "deterministic": edge_deterministic,
            "probe_records": probe_records,
        }
        records.append(record)
        _append_jsonl(output / "cases.jsonl", record)
        _atomic_json(
            heartbeat,
            {
                "status": "RUNNING",
                "updated_at": datetime.now(UTC),
                "objective": objective_name,
                "completed_edges": edge_index + 1,
                "expected_edges": len(edges),
            },
        )
    evidence_count = len(records) * len(probes)
    all_evidence = [
        probe.get("evidence", {}) for record in records for probe in record.get("probe_records", [])
    ]
    if violated:
        status = "REAL_INPUT_FIFO_VIOLATED"
        reason = "partition boundary contains a certified negative travel-operator jump"
    elif all_evidence and all(item.get("status") == "PARTITION_CERTIFIED" for item in all_evidence):
        status = "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
        reason = "all sampled partition domains have proof-carrying evidence"
    else:
        status = "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_PARTITION_PROOF"
        reason = "one or more real partitions lack complete evaluator, boundary, or root proof"
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective_name,
        "edge_count": len(edges),
        "probe_count": len(probes),
        "interval_evaluations": evidence_count,
        "status_counts": dict(status_counts),
        "boundary_status_counts": dict(boundary_counts),
        "failure_classes": dict(failure_classes),
        "scope_digest": scope.digest,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "certified_partition_probe_count": certified,
        "fifo_violated_count": violated,
        "deterministic": deterministic,
        "all_partition_evidence": bool(all_evidence),
    }
    return summary, records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("fifo-scan", "interval-qualification", "both"), default="both"
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
    if args.worker_timeout_seconds <= 0.0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    _set_cpu_affinity(args.cpu)
    loader = _load_fixture_runner()
    fixture = loader._load_fixture(args)
    identity = _identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("partitioned real qualification requires a clean worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "dominance_policy": "disabled",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    existing = {
        (record.get("objective"), record.get("edge_index")): record
        for record in _read_jsonl(output / "cases.jsonl")
        if isinstance(record.get("objective"), str) and isinstance(record.get("edge_index"), int)
    }
    objectives = OBJECTIVES if args.objective == "all" else (args.objective,)
    signal_old = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
    summaries: list[dict[str, Any]] = []
    try:
        for objective in objectives:
            summary, _ = _scan_objective(loader, fixture, objective, output, existing, heartbeat)
            summaries.append(summary)
    except _WorkerTimeout as error:
        final = {
            "schema_version": SCHEMA_VERSION,
            "status": "STOPPED_HARD",
            "reason": str(error),
            "completed_objectives": len(summaries),
        }
        _atomic_json(output / "comparison-summary.json", final)
        _atomic_json(
            output / "manifest.json",
            {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "identity": identity,
                "summary": final,
            },
        )
        _atomic_json(heartbeat, {"status": "STOPPED_HARD", "updated_at": datetime.now(UTC)})
        (output / "STOPPED_HARD").write_text(str(error) + "\n", encoding="utf-8")
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal_old)
    if any(item["status"] == "REAL_INPUT_FIFO_VIOLATED" for item in summaries):
        status = "REAL_INPUT_FIFO_VIOLATED"
    elif summaries and all(
        item["status"] == "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN" for item in summaries
    ):
        status = "READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN"
    else:
        status = "REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_PARTITION_PROOF"
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
        "edge_count": sum(item["edge_count"] for item in summaries),
        "interval_evaluations": sum(item["interval_evaluations"] for item in summaries),
        "certified_partition_probe_count": sum(
            item["certified_partition_probe_count"] for item in summaries
        ),
        "fifo_violated_count": sum(item["fifo_violated_count"] for item in summaries),
        "deterministic": all(item["deterministic"] for item in summaries),
    }
    _write_jsonl(output / "fifo-scan.jsonl", summaries)
    _write_jsonl(
        output / "eta-interval.jsonl",
        [
            record
            for objective in objectives
            for record in _read_jsonl(output / "cases.jsonl")
            if record.get("objective") == objective
        ],
    )
    _write_jsonl(
        output / "resource-frontier.jsonl",
        [
            {
                "status": "NOT_RUN_BY_DESIGN",
                "reason": "partition runner does not enable exact-arrival dominance",
            }
        ],
    )
    _atomic_json(output / "comparison-summary.json", aggregate)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "identity": identity,
            "summary": aggregate,
        },
    )
    _atomic_json(heartbeat, {"status": status, "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(status + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"experiment_id": identity["experiment_id"], **aggregate},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
