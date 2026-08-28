#!/usr/bin/env python3
"""Real-input test-only replay for the proof-bound non-FIFO adapter.

This runner is intentionally separate from the historical M4 adapter runner.
It builds the finite-grid maximum-speed corridor certificate, then exercises
the actual non-FIFO adapter with that certificate.  The proof is a necessary
time-horizon condition only; it never enables FIFO dominance or a production
planner.  The runner consumes the already committed 145-frame fixtures and
writes only research evidence under the caller-provided output directory.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import resource
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arctic_route_planning.cost.vessel import KNOT_TO_KM_PER_HOUR
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_adapter import (
    run_non_fifo_temporal_bounded_search,
    run_non_fifo_temporal_search,
)
from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity

SCHEMA_VERSION = "c.p0.2-temporal-adapter-bound-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_bound_real.py",
    "scripts/benchmark_non_fifo_temporal_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "uv.lock",
)
BOUND_METHOD = "geodesic-max-effective-speed-v1"
BOUND_EVALUATOR = "certified:geodesic-max-effective-speed-v1"
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}


def _load_script(filename: str, module_name: str) -> Any:
    path = Path(__file__).resolve().with_name(filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
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
    if hasattr(value, "__dataclass_fields__"):
        return {
            name: _jsonable(getattr(value, name))
            for name in value.__dataclass_fields__
        }
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
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


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _resource_snapshot() -> dict[str, Any]:
    values: dict[str, Any] = {
        "process_swap_kib": None,
        "host_swap_pages": None,
        "cpu_affinity": sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None,
        "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "cgroup": None,
    }
    with contextlib.suppress(OSError, StopIteration, ValueError):
        values["process_swap_kib"] = next(
            int(line.split()[1])
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("VmSwap:")
        )
    with contextlib.suppress(OSError, ValueError):
        values["host_swap_pages"] = {
            key: int(raw)
            for key, raw in (
                line.split()
                for line in Path("/proc/vmstat").read_text().splitlines()
            )
            if key in {"pswpin", "pswpout"}
        }
    
    try:
        relative = next(
            candidate.lstrip("/")
            for hierarchy, controllers, candidate in (
                line.split(":", 2)
                for line in Path("/proc/self/cgroup").read_text().splitlines()
            )
            if hierarchy == "0" and controllers == ""
        )
        root = Path("/sys/fs/cgroup") / relative

        def scalar(name: str) -> int | str | None:
            value = (root / name).read_text().strip()
            if value == "max":
                return value
            return int(value)

        events = {
            key: int(raw)
            for key, raw in (
                line.split() for line in (root / "memory.events").read_text().splitlines()
            )
        }
        values["cgroup"] = {
            "path": f"/{relative}",
            "memory_current": scalar("memory.current"),
            "memory_peak": scalar("memory.peak"),
            "memory_max": scalar("memory.max"),
            "memory_swap_current": scalar("memory.swap.current"),
            "memory_swap_max": scalar("memory.swap.max"),
            "memory_events": events,
        }
    except (OSError, ValueError, StopIteration):
        pass
    return values


def _resource_clean(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if (
        before.get("process_swap_kib") is not None
        and after.get("process_swap_kib") is not None
        and after["process_swap_kib"] > before["process_swap_kib"]
    ):
        return False
    for snapshot in (before, after):
        events = (snapshot.get("cgroup") or {}).get("memory_events") or {}
        if any(events.get(name, 0) > 0 for name in ("oom", "oom_kill", "oom_group_kill")):
            return False
        if ((snapshot.get("cgroup") or {}).get("memory_swap_current") or 0) > 0:
            return False
    return True


def _resource_evidence_complete(record: dict[str, Any], cpu: int) -> bool:
    """Require the snapshots needed to audit one bounded worker."""

    before = record.get("resources_before")
    after = record.get("resources_after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    required = {"process_swap_kib", "host_swap_pages", "cpu_affinity", "max_rss_kib", "cgroup"}
    if not required.issubset(before) or not required.issubset(after):
        return False
    if not isinstance(before["cpu_affinity"], list) or not before["cpu_affinity"]:
        return False
    if before["cpu_affinity"] != after["cpu_affinity"]:
        return False
    if cpu >= 0 and before["cpu_affinity"] != [cpu]:
        return False
    if not isinstance(before["max_rss_kib"], int) or before["max_rss_kib"] <= 0:
        return False
    if not isinstance(after["max_rss_kib"], int) or after["max_rss_kib"] <= 0:
        return False
    for snapshot in (before, after):
        swap_pages = snapshot["host_swap_pages"]
        if not isinstance(swap_pages, dict) or not all(
            isinstance(swap_pages.get(key), int) and swap_pages[key] >= 0
            for key in ("pswpin", "pswpout")
        ):
            return False
        if snapshot["process_swap_kib"] is not None and not isinstance(
            snapshot["process_swap_kib"], int
        ):
            return False
        cgroup = snapshot["cgroup"]
        if not isinstance(cgroup, dict):
            return False
        events = cgroup.get("memory_events")
        if not isinstance(events, dict) or not all(
            isinstance(events.get(key), int)
            for key in ("oom", "oom_kill", "oom_group_kill")
        ):
            return False
    return True


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _nodes(fixture: Any) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(fixture.grid.shape[0])
        for column in range(fixture.grid.shape[1])
    )


def _certificate(point: Any, fixture: Any, objective: ObjectiveMode) -> tuple[Any, Any, Any]:
    planner = point._build_planner(fixture, objective)
    request = point._request(fixture, objective)
    from dataclasses import replace

    request = replace(request, use_heuristic=False)
    nodes = _nodes(fixture)
    scope = planner.temporal_scope(request)
    max_speed = planner.vessel_model.maximum_speed_knots * KNOT_TO_KM_PER_HOUR
    forward = {
        node: fixture.grid.distance_km(fixture.start, node) / max_speed for node in nodes
    }
    reverse = {
        node: fixture.grid.distance_km(node, fixture.goal) / max_speed for node in nodes
    }
    horizon = request.maximum_elapsed.total_seconds() / 3600.0
    evidence = AdmissibleBoundEvidence(
        scope=scope,
        method=BOUND_METHOD,
        evaluator_digest=BOUND_EVALUATOR,
        proof_digest=_digest(
            {
                "method": BOUND_METHOD,
                "max_speed_km_per_hour": max_speed,
                "scope": scope.digest,
                "universe": nodes,
                "forward": forward,
                "reverse": reverse,
                "horizon_hours": horizon,
                "limits": LIMITS,
            }
        ),
        admissible=True,
        coverage_complete=True,
    )
    corridor = derive_temporal_corridor(
        scope=scope,
        expected_scope=scope,
        universe_nodes=nodes,
        start=fixture.start,
        goal=fixture.goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon,
        objective=objective.value,
        bound_evidence=evidence,
        generated_nodes=nodes,
    )
    return planner, request, corridor


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    _set_cpu(args.cpu)
    point = _load_script("benchmark_temporal_dominance_real.py", "c_m5_bound_point")
    fixture = point._load_fixture(_fixture_args(args))
    objective = ObjectiveMode(args.objective)
    baseline_planner, request, _ = _certificate(point, fixture, objective)
    bound_planner, bound_request, corridor = _certificate(point, fixture, objective)
    if request != bound_request:
        raise RuntimeError("bound certificate request identity drift")
    bound_planner.state_bound_certificate = corridor.certificate
    identity = TemporalSessionIdentity.from_planner(
        bound_planner,
        request,
        input_revision=0,
        risk_window_content_digest=fixture.commit["content_digest"],
        risk_window_commit_id=fixture.commit["commit_id"],
    )
    before = _resource_snapshot()
    started = time.perf_counter()
    baseline = None
    bounded = None
    reference = None
    errors: dict[str, str] = {}
    try:
        baseline = run_non_fifo_temporal_search(baseline_planner, request)
    except Exception as error:  # pragma: no cover - worker boundary
        errors["baseline"] = f"{type(error).__name__}: {error}"
    if baseline is not None and baseline.status is NonFifoSearchStatus.GOAL_FOUND:
        try:
            reference = point._reference_search(baseline_planner, request)
        except Exception as error:  # pragma: no cover - evidence boundary
            errors["reference"] = f"{type(error).__name__}: {error}"
    try:
        bounded = run_non_fifo_temporal_bounded_search(
            bound_planner,
            request,
            corridor.certificate,
            identity=identity,
        )
    except Exception as error:  # pragma: no cover - worker boundary
        errors["bounded"] = f"{type(error).__name__}: {error}"
    after = _resource_snapshot()
    baseline_semantic = (
        None
        if baseline is None or baseline.planning_result is None
        else point._route_semantic(baseline)
    )
    bounded_semantic = (
        None
        if bounded is None or bounded.planning_result is None
        else point._route_semantic(bounded)
    )
    baseline_match = (
        reference is not None
        and baseline_semantic is not None
        and point._reference_matches(baseline_semantic, reference)
    )
    bounded_match = (
        reference is not None
        and bounded_semantic is not None
        and point._reference_matches(bounded_semantic, reference)
    )
    diagnostics = None if bounded is None else _jsonable(bounded.diagnostics)
    pruned = 0 if bounded is None or bounded.diagnostics is None else int(
        bounded.diagnostics.state_bound_pruned
    )
    rejected = 0 if bounded is None or bounded.diagnostics is None else int(
        bounded.diagnostics.state_bound_rejected
    )
    semantic_match = baseline_match and bounded_match and (
        baseline.semantic_digest == bounded.semantic_digest
        if baseline is not None and bounded is not None
        else False
    )
    resource_clean = _resource_clean(before, after)
    status = "PASS" if (
        not errors
        and baseline is not None
        and bounded is not None
        and baseline.status is NonFifoSearchStatus.GOAL_FOUND
        and bounded.status is NonFifoSearchStatus.GOAL_FOUND
        and semantic_match
        and pruned > 0
        and rejected == 0
        and resource_clean
    ) else "FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective.value,
        "repetition": args.repetition,
        "adapter_mode": "non_fifo_zero_heuristic_bound_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "explicit-certified-only",
        "state_bound_authorized": rejected == 0 and bounded is not None,
        "state_bound_certificate_digest": corridor.certificate.digest,
        "state_bound_proof_digest": corridor.proof_digest,
        "state_bound_checks": 0 if bounded is None else int(bounded.diagnostics.state_bound_checks),
        "state_bound_pruned": pruned,
        "state_bound_rejected": rejected,
        "projected_label_reduction": corridor.projected_label_reduction,
        "semantic_match": semantic_match,
        "reference_match": baseline_match and bounded_match,
        "baseline_semantic_digest": None
        if baseline is None
        else baseline.semantic_digest,
        "bounded_semantic_digest": None
        if bounded is None
        else bounded.semantic_digest,
        "baseline_status": None if baseline is None else baseline.status.value,
        "bounded_status": None if bounded is None else bounded.status.value,
        "baseline_semantic": baseline_semantic,
        "bounded_semantic": bounded_semantic,
        "reference": reference,
        "diagnostics": diagnostics,
        "errors": errors,
        "reason": None if status == "PASS" else "; ".join(
            f"{key}={value}" for key, value in sorted(errors.items())
        ) or "semantic/resource/certified-pruning gate failed",
        "session_identity": identity.digest,
        "compute_ms": None
        if bounded is None or bounded.planning_result is None
        else bounded.planning_result.metrics.compute_ms,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "production_candidate_enabled": False,
    }


def _worker_command(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--risk-window-commit",
        str(args.risk_window_commit),
        "--route-plan-set",
        str(args.route_plan_set),
        "--config-root",
        str(args.config_root),
        "--segment",
        args.segment,
        "--output-dir",
        str(args.output_dir),
        "--objective",
        objective.value,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
    ]


def _run_worker(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            _worker_command(args, objective, repetition),
            check=False,
            capture_output=True,
            text=True,
            timeout=args.worker_timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "TIMEOUT",
            "input": None,
            "segment": args.segment,
            "objective": objective.value,
            "repetition": repetition,
            "state_bound_pruned": 0,
            "reason": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "segment": args.segment,
            "objective": objective.value,
            "repetition": repetition,
            "state_bound_pruned": 0,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
        }
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "segment": args.segment,
            "objective": objective.value,
            "repetition": repetition,
            "state_bound_pruned": 0,
            "reason": f"worker JSON decode failed: {error}",
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
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
        "objectives": [item.value for item in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "adapter_mode": "non_fifo_zero_heuristic_bound_v1",
        "dominance_policy": "disabled",
        "bound_method": BOUND_METHOD,
        "bound_evaluator": BOUND_EVALUATOR,
        "search_limits": LIMITS,
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


class _RunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> _RunnerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.handle.close()
            raise RuntimeError("another bound real runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _summary(cases: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    expected = len(OBJECTIVES) * int(identity["repetitions"])
    complete = len(cases) == expected and all(
        case.get("status") in {"PASS", "FAIL", "TIMEOUT", "INVALID/PENDING"}
        for case in cases
    )
    semantic = bool(cases) and all(
        case.get("status") == "PASS" and case.get("semantic_match") is True
        for case in cases
    )
    resource = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    resource_evidence = bool(cases) and all(
        _resource_evidence_complete(case, int(identity["cpu"])) for case in cases
    )
    pruning = sum(int(case.get("state_bound_pruned", 0)) for case in cases)
    deterministic = True
    for objective in OBJECTIVES:
        digests = {
            case.get("bounded_semantic_digest")
            for case in cases
            if case.get("objective") == objective.value
            and isinstance(case.get("bounded_semantic_digest"), str)
        }
        if len(digests) != 1 or sum(
            1 for case in cases if case.get("objective") == objective.value
        ) != int(identity["repetitions"]):
            deterministic = False
    if not complete:
        status = "INVALID/PENDING"
    elif (
        not semantic
        or not resource
        or not resource_evidence
        or not deterministic
        or pruning == 0
    ):
        status = "NO_PERFORMANCE_PROOF/FAIL"
    else:
        status = "READY_FOR_P0.2-ADAPTER_RESOURCE_BOUND_PLAN"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "semantic_match": semantic,
        "resource_clean": resource,
        "resource_evidence_complete": resource_evidence,
        "deterministic": deterministic,
        "observed_state_bound_pruning": pruning,
        "dominance_policy": "disabled",
        "state_bound_policy": "explicit-certified-only",
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "cases": cases,
        "next_action": "keep candidate disabled; review certified resource-bound evidence",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=("executable_0_6h",), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("repetitions/timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    if args.worker:
        if args.objective is None:
            raise SystemExit("worker requires --objective")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    point = _load_script("benchmark_temporal_dominance_real.py", "c_m5_bound_parent_point")
    fixture = point._load_fixture(_fixture_args(args))
    identity = _identity(args, fixture, root)
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"),
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError("real bound runner requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.resume:
                raise RuntimeError("experiment exists; use --resume")
            if previous.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity mismatch")
        _atomic_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "experiment_id": identity["experiment_id"],
                "identity": identity,
                "evidence_files": (
                    "manifest.json",
                    "cases.jsonl",
                    "resource-frontier.jsonl",
                    "comparison-summary.json",
                    "heartbeat.json",
                    "ALL_DONE/STOPPED_HARD",
                ),
            },
        )
        cases_path = output / "cases.jsonl"
        existing = {}
        if args.resume and cases_path.exists():
            for case in cases_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(case)
                except json.JSONDecodeError:
                    continue
                key = (record.get("objective"), record.get("repetition"))
                if key in existing:
                    raise RuntimeError("resume evidence contains duplicate case")
                existing[key] = record
        cases = list(existing.values())
        heartbeat = output / "heartbeat.json"
        for repetition in range(1, args.repetitions + 1):
            order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
            for objective in order:
                key = (objective.value, repetition)
                if key in existing:
                    continue
                record = _run_worker(args, objective, repetition)
                record["experiment_id"] = identity["experiment_id"]
                record["objective"] = objective.value
                record["repetition"] = repetition
                _append_jsonl(output / "cases.jsonl", record)
                _append_jsonl(output / "resource-frontier.jsonl", record)
                existing[key] = record
                cases.append(record)
                _atomic_json(
                    heartbeat,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "RUNNING",
                        "updated_at": datetime.now(UTC),
                        "completed_cases": len(cases),
                        "expected_cases": len(OBJECTIVES) * args.repetitions,
                        "objective": objective.value,
                        "repetition": repetition,
                    },
                )
        summary = _summary(cases, identity)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "experiment_id": identity["experiment_id"],
                "identity": identity,
                "summary": summary,
            },
        )
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "updated_at": datetime.now(UTC),
            },
        )
        _atomic_json(output / "fifo-scan.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
        _atomic_json(output / "eta-interval.jsonl", {"status": "NOT_RUN_BY_DESIGN"})
        marker = output / "ALL_DONE"
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        compact_summary = {
            key: value for key, value in summary.items() if key != "cases"
        }
        print(
            json.dumps(
                compact_summary,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    return 0 if summary["status"] == "READY_FOR_P0.2-ADAPTER_RESOURCE_BOUND_PLAN" else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
