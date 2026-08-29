#!/usr/bin/env python3
"""Research-only real-input stale queue compaction diagnostic.

The audited real-input fixture and semantic/reference helpers are reused from
``benchmark_temporal_dominance_real``.  This runner compares the historical
lazy stale-pop queue with an explicit equality-proven queue compaction policy.
It never enables temporal dominance, changes the production contract, or
publishes a candidate result.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.temporal_queue_compaction import (
    TemporalQueueCompactionPolicy,
)

SCHEMA_VERSION = "c.p0.2-temporal-queue-compaction-real.v1"
MILESTONE = "P0.2-M30"


class _WorkerTimeout(RuntimeError):
    """Bound a diagnostic call without changing planner failure semantics."""


def _run_with_timeout(call: Any, timeout_seconds: float) -> Any:
    """Run one in-process diagnostic call under a wall-clock deadline."""

    if timeout_seconds <= 0 or not hasattr(signal, "setitimer"):
        return call()

    def alarm_handler(_signum: int, _frame: Any) -> None:
        raise _WorkerTimeout(f"worker timeout after {timeout_seconds:g}s")

    previous = signal.signal(signal.SIGALRM, alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _load_audited_runner() -> Any:
    path = Path(__file__).with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_m30_audited_real_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load audited real-input runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_audited_runner()
SEGMENTS = _BASE.SEGMENTS
OBJECTIVES = tuple(ObjectiveMode)
DEFAULT_LIMITS = _BASE.DEFAULT_LIMITS


def _jsonable(value: Any) -> Any:
    return _BASE._jsonable(value)


def _canonical_digest(value: Any) -> str:
    return _BASE._canonical_digest(value)


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


def _policy(args: argparse.Namespace) -> TemporalQueueCompactionPolicy:
    return TemporalQueueCompactionPolicy.live_only(
        check_interval=args.compaction_check_interval,
        min_stale_entries=args.compaction_min_stale_entries,
        min_stale_fraction=args.compaction_min_stale_fraction,
    )


def _build_identity(args: argparse.Namespace, fixture: Any) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    implementation_files = (
        "scripts/benchmark_temporal_queue_compaction_real.py",
        "scripts/benchmark_temporal_dominance_real.py",
        "src/arctic_route_planning/planners/temporal_queue_compaction.py",
        "src/arctic_route_planning/planners/temporal_label_astar.py",
        "src/arctic_route_planning/planners/temporal_session.py",
        "src/arctic_route_planning/planners/_archive/temporal_session.py",
    )
    files = {relative: _BASE._sha256(root / relative) for relative in implementation_files}
    return {
        "schema_version": SCHEMA_VERSION,
        "milestone": MILESTONE,
        "git": _BASE._git_identity(root),
        "implementation": {"files": files, "sha256": _canonical_digest(files)},
        "risk_window": {
            "path": str(fixture.commit_path),
            "file_sha256": _BASE._sha256(fixture.commit_path),
            "content_digest": fixture.commit["content_digest"],
            "commit_id": fixture.commit["commit_id"],
            "frame_count": len(fixture.frames),
            "frame_digests": [
                {
                    "risk_id": frame.risk_id,
                    "valid_time": frame.valid_time,
                    "generation_id": frame.generation_id,
                    "content_digest": _BASE.risk_frame_content_digest(frame),
                }
                for frame in fixture.frames
            ],
        },
        "route_plan_set": {
            "path": str(fixture.route_plan_path),
            "sha256": _BASE._sha256(fixture.route_plan_path),
        },
        "config_root": {
            "path": str(fixture.config_root),
            "sha256": _BASE._tree_digest(fixture.config_root),
        },
        "lock_sha256": _BASE._sha256(root / "uv.lock"),
        "input": {
            "name": fixture.input_name,
            "segment": fixture.segment,
            "start": list(fixture.start),
            "goal": list(fixture.goal),
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "objectives": [objective.value for objective in args.objectives],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "search_limits": _BASE.asdict(DEFAULT_LIMITS),
        "dominance_policy": "disabled",
        "queue_compaction": {
            "method": "live-label-equality-v1",
            "policy_digest": _policy(args).digest,
            "check_interval": args.compaction_check_interval,
            "min_stale_entries": args.compaction_min_stale_entries,
            "min_stale_fraction": args.compaction_min_stale_fraction,
        },
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _run_plan(
    fixture: Any,
    objective: ObjectiveMode,
    *,
    compact: bool,
    policy: TemporalQueueCompactionPolicy,
    args: argparse.Namespace,
) -> dict[str, Any]:
    planner = _BASE._build_planner(fixture, objective)
    if compact:
        planner.queue_compaction_policy = policy
    request = _BASE._request(fixture, objective)
    before = _BASE._resource_snapshot()
    started = time.perf_counter()
    result = None
    error: dict[str, str] | None = None
    try:
        result = _run_with_timeout(
            lambda: planner.plan(request), args.worker_timeout_seconds
        )
    except Exception as exc:  # evidence records must preserve domain failure semantics
        error = {"type": type(exc).__name__, "message": str(exc)}
    wall_seconds = time.perf_counter() - started
    after = _BASE._resource_snapshot()
    semantic = _BASE._route_semantic(result) if result is not None else None
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    return {
        "status": "PASS" if result is not None else "ERROR",
        "policy": "live_only" if compact else "disabled",
        "policy_digest": planner.queue_compaction_policy_digest,
        "semantic": semantic,
        "semantic_digest": _canonical_digest(semantic) if semantic is not None else None,
        "compute_ms": result.planning_result.metrics.compute_ms if result is not None else None,
        "wall_seconds": wall_seconds,
        "diagnostics": diagnostics,
        "planner_error": error,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": _BASE._resource_clean(before, after),
        "resource_evidence_complete": (
            _BASE._resource_evidence_complete(result_record, cpu=args.cpu)
            if (result_record := {
                "resources_before": before,
                "resources_after": after,
            })
            else False
        ),
    }


def _close_semantic(left: Any, right: Any) -> bool:
    return left is not None and right is not None and _BASE._close(left, right)


def _case(
    fixture: Any,
    objective: ObjectiveMode,
    repetition: int,
    policy: TemporalQueueCompactionPolicy,
    args: argparse.Namespace,
    experiment_id: str,
) -> dict[str, Any]:
    baseline = _run_plan(
        fixture,
        objective,
        compact=False,
        policy=policy,
        args=args,
    )
    compact = _run_plan(
        fixture,
        objective,
        compact=True,
        policy=policy,
        args=args,
    )
    reference = None
    reference_error = None
    if baseline["status"] == "PASS":
        try:
            reference_planner = _BASE._build_planner(fixture, objective)
            reference = _run_with_timeout(
                lambda: _BASE._reference_search(
                    reference_planner, _BASE._request(fixture, objective)
                ),
                args.worker_timeout_seconds,
            )
        except Exception as exc:
            reference_error = {"type": type(exc).__name__, "message": str(exc)}
    baseline_reference_match = (
        reference is not None
        and baseline["semantic"] is not None
        and _BASE._reference_matches(baseline["semantic"], reference)
    )
    compact_reference_match = (
        reference is not None
        and compact["semantic"] is not None
        and _BASE._reference_matches(compact["semantic"], reference)
    )
    semantic_match = _close_semantic(baseline["semantic"], compact["semantic"])
    planner_errors = baseline["planner_error"] or compact["planner_error"]
    reference_limit = bool(
        reference_error
        and reference_error.get("type") == "RuntimeError"
        and any(
            marker in reference_error.get("message", "")
            for marker in ("queue=", "labels=", "expansions=", "edge_evaluations=")
        )
    )
    if planner_errors or not semantic_match:
        status = "FAIL"
    elif reference_limit:
        status = "REFERENCE_RESOURCE_LIMIT"
    elif reference_error or not baseline_reference_match or not compact_reference_match:
        status = "FAIL"
    else:
        status = "PASS"
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "input": fixture.input_name,
        "segment": fixture.segment,
        "objective": objective.value,
        "repetition": repetition,
        "status": status,
        "dominance_policy": "disabled",
        "baseline": baseline,
        "compacted": compact,
        "reference": reference,
        "reference_error": reference_error,
        "baseline_reference_match": baseline_reference_match,
        "compacted_reference_match": compact_reference_match,
        "semantic_match": semantic_match,
        "queue_compactions": int(
            (compact.get("diagnostics") or {}).get("queue_compactions", 0)
        ),
        "queue_compaction_removed": int(
            (compact.get("diagnostics") or {}).get("queue_compaction_removed", 0)
        ),
        "resource_classification": (
            "RESOURCE_CLEAN_BOUNDARY_INCOMPLETE"
            if not compact.get("resource_evidence_complete")
            else "RESOURCE_EVIDENCE_COMPLETE"
        ),
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _summary(cases: list[dict[str, Any]], identity: dict[str, Any]) -> dict[str, Any]:
    expected = len(identity["objectives"]) * int(identity["repetitions"])
    valid = [case for case in cases if case.get("status") == "PASS"]
    resource_limited = [
        case for case in cases if case.get("status") == "REFERENCE_RESOURCE_LIMIT"
    ]
    complete_semantic = len(cases) == expected and all(
        case.get("semantic_match") and case.get("status") in {"PASS", "REFERENCE_RESOURCE_LIMIT"}
        for case in cases
    )
    if complete_semantic and resource_limited:
        status = "QUEUE_COMPACTION_REFERENCE_RESOURCE_LIMIT"
    elif complete_semantic:
        status = "QUEUE_COMPACTION_SEMANTIC_PASS"
    else:
        status = "QUEUE_COMPACTION_DIAGNOSTIC_FAIL"
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": identity["experiment_id"],
        "status": status,
        "expected_case_count": expected,
        "case_count": len(cases),
        "semantic_all_match": bool(cases)
        and all(case["semantic_match"] for case in cases),
        "reference_all_match": bool(cases)
        and all(
            case["baseline_reference_match"] and case["compacted_reference_match"]
            for case in cases
        ),
        "reference_resource_limit_cases": len(resource_limited),
        "semantic_pass_cases": len(valid) + len(resource_limited),
        "queue_compactions_total": sum(case["queue_compactions"] for case in cases),
        "queue_compaction_removed_total": sum(
            case["queue_compaction_removed"] for case in cases
        ),
        "resource_evidence_complete": all(
            case["resource_classification"] == "RESOURCE_EVIDENCE_COMPLETE" for case in cases
        ),
        "cases": cases,
        "candidate_authorized": False,
        "winter_authorized": False,
    }


def _run(args: argparse.Namespace) -> int:
    if args.cpu >= 0:
        _BASE._set_cpu_affinity(args.cpu)
    fixture_args = argparse.Namespace(
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )
    fixture = _BASE._load_fixture(fixture_args)
    identity = _build_identity(args, fixture)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("evidence mode requires a clean implementation worktree")
    identity["experiment_id"] = (
        f"{SCHEMA_VERSION}-{fixture.input_name}-{args.segment}-"
        f"{_canonical_digest(identity)[:16]}"
    )
    output = args.output_dir.resolve()
    manifest_path = output / "manifest.json"
    existing = None
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        if existing.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match prepared experiment")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "resumed_from": existing.get("status") if existing else None,
    }
    _atomic_json(manifest_path, manifest)
    cases = _read_jsonl(output / "cases.jsonl") if args.resume else []
    completed = {(case.get("objective"), case.get("repetition")) for case in cases}
    policy = _policy(args)
    for repetition in range(1, args.repetitions + 1):
        for objective in args.objectives:
            key = (objective.value, repetition)
            if key in completed:
                continue
            case = _case(fixture, objective, repetition, policy, args, identity["experiment_id"])
            cases.append(case)
            _append_jsonl(output / "cases.jsonl", case)
            _atomic_json(
                output / "heartbeat.json",
                {
                    "updated_at": datetime.now(UTC),
                    "status": "RUNNING",
                    "completed_cases": len(cases),
                    "expected_cases": len(args.objectives) * args.repetitions,
                    "objective": objective.value,
                    "repetition": repetition,
                },
            )
    summary = _summary(cases, identity)
    _atomic_json(output / "comparison-summary.json", summary)
    manifest.update(
        {
            "status": summary["status"],
            "summary": summary,
            "completed_at": datetime.now(UTC),
        }
    )
    _atomic_json(manifest_path, manifest)
    _atomic_json(
        output / "heartbeat.json",
        {"updated_at": datetime.now(UTC), "status": summary["status"]},
    )
    (output / "ALL_DONE").write_text("\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"] in {
        "QUEUE_COMPACTION_SEMANTIC_PASS",
        "QUEUE_COMPACTION_REFERENCE_RESOURCE_LIMIT",
    } else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=tuple(SEGMENTS), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--objectives",
        nargs="+",
        type=ObjectiveMode,
        choices=OBJECTIVES,
        default=list(OBJECTIVES),
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--compaction-check-interval", type=int, default=64)
    parser.add_argument("--compaction-min-stale-entries", type=int, default=4)
    parser.add_argument("--compaction-min-stale-fraction", type=float, default=0.2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and timeout must be positive")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
