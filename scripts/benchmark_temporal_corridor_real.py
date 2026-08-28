#!/usr/bin/env python3
"""Research-only real-input projection for proof-carrying temporal corridors.

This runner is deliberately a projection, not a planner benchmark.  It uses
only finite-grid, maximum-speed necessary conditions to estimate how many
nodes could be excluded by a future proof-carrying state bound.  It never
injects the bound into :class:`TemporalLabelAStar`, never enables dominance,
and never treats a projected reduction as an observed label reduction.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import resource
import signal
import subprocess
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from arctic_route_planning.cost.vessel import KNOT_TO_KM_PER_HOUR
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_qualification import (
    TemporalScope,
    canonical_digest,
)

SCHEMA_VERSION = "c.p0.1-temporal-corridor-real.v1"
SEGMENTS = {
    "executable_0_6h": timedelta(hours=6),
    "rolling_0_24h": timedelta(hours=24),
}
OBJECTIVES = tuple(item.value for item in ObjectiveMode)
SEARCH_LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
BOUND_METHOD = "geodesic-max-effective-speed-v1"
BOUND_EVALUATOR_DIGEST = "certified:geodesic-max-effective-speed-v1"
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_dominance_real.py",
    "scripts/benchmark_temporal_corridor_real.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/cost/model.py",
    "src/arctic_route_planning/cost/vessel.py",
)


def _load_fixture_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_temporal_corridor_fixture_runner", path)
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
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


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


def _resource_snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    status: dict[str, int] = {}
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                fields = line.split()
                status[fields[0].rstrip(":").lower()] = int(fields[1])
    except (OSError, ValueError):
        status = {}
    return {
        "max_rss_kib": int(usage.ru_maxrss),
        "vmrss_kib": status.get("vmrss"),
        "vmswap_kib": status.get("vmswap"),
    }


def _nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    values = fixture.frames[0].payload["hard_mask"].transpose("latitude", "longitude").values
    mask = np.asarray(values, dtype=bool)
    component = fixture.grid.connected_component(fixture.start, mask)
    return tuple(sorted(component))


def _scope(
    planner: Any,
    request: Any,
    fixture: Any,
    objective: str,
    nodes: tuple[Any, ...],
) -> TemporalScope:
    base = planner.temporal_scope(request)
    return TemporalScope.from_mapping(
        {
            **base.mapping,
            "bound_evaluator_digest": BOUND_EVALUATOR_DIGEST,
            "bound_method": BOUND_METHOD,
            "corridor_universe_digest": canonical_digest(nodes),
            "corridor_segment": fixture.segment,
            "dominance_policy": "disabled",
            "search_limits": SEARCH_LIMITS,
        }
    )


def _projected_queue_profile(
    forward: dict[Any, float], allowed: tuple[Any, ...], horizon_hours: float
) -> dict[str, int]:
    """Return a clearly labelled one-node-per-state projection, not observed queue data."""

    return {
        str(hour): sum(1 for node in allowed if forward[node] <= float(hour))
        for hour in range(int(horizon_hours) + 1)
    }


def _projection_case(module: Any, fixture: Any, objective_name: str, cpu: int) -> dict[str, Any]:
    objective = ObjectiveMode(objective_name)
    planner = module._build_planner(fixture, objective)
    request = module._request(fixture, objective)
    nodes = _nodes(fixture)
    if fixture.start not in nodes or fixture.goal not in nodes:
        raise RuntimeError("departure component does not contain frozen start and goal")
    scope = _scope(planner, request, fixture, objective_name, nodes)
    max_speed_km_per_hour = planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR
    if not np.isfinite(max_speed_km_per_hour) or max_speed_km_per_hour <= 0.0:
        raise RuntimeError("vessel maximum effective speed is invalid")
    forward = {
        node: fixture.grid.distance_km(fixture.start, node) / max_speed_km_per_hour
        for node in nodes
    }
    reverse = {
        node: fixture.grid.distance_km(node, fixture.goal) / max_speed_km_per_hour for node in nodes
    }
    horizon_hours = SEGMENTS[fixture.segment].total_seconds() / 3600.0
    proof_payload = {
        "method": BOUND_METHOD,
        "max_speed_km_per_hour": max_speed_km_per_hour,
        "vessel_model_digest": canonical_digest(planner.vessel_model),
        "scope_digest": scope.digest,
        "universe": nodes,
        "forward": forward,
        "reverse": reverse,
        "horizon_hours": horizon_hours,
        "limits": SEARCH_LIMITS,
    }
    evidence = AdmissibleBoundEvidence(
        scope=scope,
        method=BOUND_METHOD,
        evaluator_digest=BOUND_EVALUATOR_DIGEST,
        proof_digest=canonical_digest(proof_payload),
        admissible=True,
        coverage_complete=True,
    )
    started = time.perf_counter()
    before = _resource_snapshot()
    derived = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon_hours,
        objective=objective_name,
        bound_evidence=evidence,
        generated_nodes=nodes,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    after = _resource_snapshot()
    allowed = tuple(derived.certificate.allowed_nodes)
    queue_profile = _projected_queue_profile(forward, allowed, horizon_hours)
    repeat = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon_hours,
        objective=objective_name,
        bound_evidence=evidence,
        generated_nodes=nodes,
    )
    deterministic = derived.digest == repeat.digest
    reduction = derived.projected_label_reduction or 0.0
    cost_model = planner._cost_model(objective)
    objective_lower_bound_digest = canonical_digest(
        {
            "cost_model": cost_model,
            "values": {
                node: cost_model.lower_bound(fixture.grid.distance_km(node, fixture.goal))
                for node in nodes
            },
        }
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS" if derived.certificate.usable and deterministic else "ERROR",
        "mode": "projection",
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective_name,
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "state_bound_pruned": 0,
        "state_bound_checks": len(nodes),
        "state_bound_rejected": 0 if derived.certificate.usable else 1,
        "certificate_status": derived.certificate.status.value,
        "certificate_usable": derived.certificate.usable,
        "certificate_digest": derived.certificate.digest,
        "proof_digest": derived.proof_digest,
        "scope_digest": scope.digest,
        "bound_method": BOUND_METHOD,
        "bound_evaluator_digest": BOUND_EVALUATOR_DIGEST,
        "max_speed_km_per_hour": max_speed_km_per_hour,
        "universe_count": len(nodes),
        "allowed_count": derived.allowed_count,
        "excluded_count": derived.excluded_count,
        "projected_label_reduction": reduction,
        "projection_gate_20pct": reduction >= 0.20,
        "projected_queue_peak": max(queue_profile.values(), default=0),
        "projected_queue_peak_by_elapsed_hour": queue_profile,
        "queue_peak_kind": "one-node-per-state-projection",
        "objective_lower_bound_digest": objective_lower_bound_digest,
        "objective_bound_mode": "not_applied_no_incumbent",
        "route_semantic_digest": None,
        "semantic_comparison": "NOT_RUN_BY_DESIGN",
        "compute_ms": elapsed_ms,
        "deterministic": deterministic,
        "resource_before": before,
        "resource_after": after,
        "resource_clean": (after.get("vmswap_kib") or 0) == 0,
        "cpu": cpu,
        "reason": derived.reason,
        "failure_class": None if derived.certificate.usable else "BOUND_REJECTED",
    }


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("real corridor projection timeout")


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
        "mode": args.mode,
        "objective": args.objective,
        "dominance_policy": "disabled",
        "bound_method": BOUND_METHOD,
        "bound_evaluator_digest": BOUND_EVALUATOR_DIGEST,
        "search_limits": SEARCH_LIMITS,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
    }
    identity["fixture_digest"] = canonical_digest(
        {
            "input": identity["input"],
            "risk_window": identity["risk_window"],
            "route_plan_set_sha256": identity["route_plan_set_sha256"],
            "config_root_sha256": identity["config_root_sha256"],
        }
    )
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{canonical_digest(identity)[:16]}"
    return identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("projection", "resource-frontier"), default="projection")
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


def _aggregate(
    identity: dict[str, Any], records: list[dict[str, Any]], fixture: Any
) -> dict[str, Any]:
    successful = [record for record in records if record.get("status") == "PASS"]
    hard_failure = [
        record
        for record in records
        if record.get("status") != "PASS" or not record.get("certificate_usable", False)
    ]
    reductions = {
        record["objective"]: record.get("projected_label_reduction", 0.0) for record in successful
    }
    required = {"low_risk", "recommended"}
    reduction_gate = (
        not hard_failure
        and required.issubset(reductions)
        and all(reductions[item] >= 0.20 for item in required)
    )
    if hard_failure:
        status = "INVALID/PENDING"
        reason = "projection certificate or deterministic evidence failed"
    elif reduction_gate:
        status = "REAL_CORRIDOR_PROJECTION_READY_FOR_TEST_ONLY_PRUNING"
        reason = "low-risk and recommended projected reductions meet 20 percent gate"
    else:
        status = "REAL_INPUT_RESOURCE_BOUND_INSUFFICIENT"
        reason = "necessary-condition projection does not meet the 20 percent gate"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "reason": reason,
        "experiment_id": identity["experiment_id"],
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objectives": [record.get("objective") for record in records],
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "projection_only": True,
        "observed_label_pruning": 0,
        "observed_queue_peak": None,
        "projected_label_reduction_by_objective": reductions,
        "projection_gate_20pct": reduction_gate,
        "objective_summaries": records,
        "deterministic": bool(records)
        and all(record.get("deterministic", False) for record in records),
        "certificate_complete": not hard_failure and len(records) == len(OBJECTIVES),
        "next_action": (
            "prepare separate test-only pruning plan"
            if reduction_gate
            else "retain dominance disabled and study stronger corridor/envelope proof"
        ),
    }


def _run(args: argparse.Namespace) -> int:
    if args.worker_timeout_seconds <= 0.0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    _set_cpu_affinity(args.cpu)
    loader = _load_fixture_runner()
    fixture = loader._load_fixture(args)
    identity = _identity(args, fixture, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("real corridor projection requires a clean implementation worktree")
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
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "dominance_policy": "disabled",
            "projection_only": True,
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
    existing = {
        record.get("objective"): record
        for record in _read_jsonl(cases_path)
        if isinstance(record.get("objective"), str)
    }
    objectives = OBJECTIVES if args.objective == "all" else (args.objective,)
    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    records: list[dict[str, Any]] = []
    try:
        for objective in objectives:
            record = existing.get(objective)
            if record is None:
                try:
                    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
                    record = _projection_case(loader, fixture, objective, args.cpu)
                except _WorkerTimeout as error:
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "TIMEOUT",
                        "mode": "projection",
                        "input": fixture.input_name,
                        "segment": fixture.segment,
                        "objective": objective,
                        "dominance_policy": "disabled",
                        "dominance_pruned": 0,
                        "reason": str(error),
                        "deterministic": False,
                        "certificate_usable": False,
                    }
                except Exception as error:  # one objective must not block the others
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "status": "ERROR",
                        "mode": "projection",
                        "input": fixture.input_name,
                        "segment": fixture.segment,
                        "objective": objective,
                        "dominance_policy": "disabled",
                        "dominance_pruned": 0,
                        "reason": f"{type(error).__name__}: {error}",
                        "deterministic": False,
                        "certificate_usable": False,
                    }
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                record["experiment_id"] = identity["experiment_id"]
                _append_jsonl(cases_path, record)
            records.append(record)
            _atomic_json(
                heartbeat,
                {
                    "status": "RUNNING",
                    "updated_at": datetime.now(UTC),
                    "objective": objective,
                    "completed_objectives": len(records),
                    "expected_objectives": len(objectives),
                },
            )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
    summary = _aggregate(identity, records, fixture)
    _write_jsonl(output / "resource-frontier.jsonl", records)
    _write_jsonl(output / "fifo-scan.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _write_jsonl(output / "eta-interval.jsonl", [{"status": "NOT_RUN_BY_DESIGN"}])
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
            "projection_only": True,
        },
    )
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] != "INVALID/PENDING" else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
