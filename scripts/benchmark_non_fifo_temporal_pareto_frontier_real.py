#!/usr/bin/env python3
"""Research-only real 6h equivalence check for non-FIFO Pareto pruning.

Each policy/objective/repetition is executed in a separate worker process.  A
worker runs the actual temporal Pareto sidecar with either pruning disabled or
with the strict same-exact-arrival new-label rule.  The parent compares the
complete serialized goal frontiers exactly; it never enables production
dominance, injects a reference route, or changes the formal planner.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-frontier-real.v1"
OBJECTIVES = tuple(ObjectiveMode)
POLICIES = ("baseline", "pareto")
SEGMENTS = ("executable_0_6h",)
TERMINAL_STATUSES = {status.value for status in NonFifoSearchStatus}
SEARCH_LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_pareto_frontier_real.py",
    "scripts/benchmark_non_fifo_temporal_pareto_real.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_feasibility.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/time_dependent_astar.py",
)


def _load_base_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_non_fifo_temporal_pareto_real.py")
    spec = importlib.util.spec_from_file_location("c_m21_real_pareto_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the audited real Pareto runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if hasattr(value, "total_seconds") and callable(value.total_seconds):
        return value.total_seconds()
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float) and not (value == value and abs(value) != float("inf")):
        raise ValueError("real Pareto evidence contains a non-finite float")
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value), ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
    )


def _route_payload(route: Any, base: Any) -> dict[str, Any]:
    return base._route_payload(route)


def _certificate_payload(certificate: Any) -> dict[str, Any]:
    return {
        "digest": certificate.digest,
        "usable": certificate.usable,
        "complete": certificate.complete,
        "status": certificate.status.value,
        "scope_digest": certificate.scope_digest,
        "session_identity_digest": certificate.session_identity_digest,
        "comparison_identity_digest": certificate.comparison_identity_digest,
        "policy_digest": certificate.policy_digest,
        "frontier_digest": certificate.frontier_digest,
        "frontier_count": certificate.frontier_count,
        "goal_label_count": certificate.goal_label_count,
        "rejection_reason": certificate.rejection_reason,
    }


def _frontier_digest(frontier: list[dict[str, Any]]) -> str:
    return _digest(
        {
            "schema_version": "c.p0.2-nonfifo-frontier-real-labels.v1",
            "labels": tuple(
                sorted(
                    json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for item in frontier
                )
            ),
        }
    )


def _semantic_frontier_token(item: Any) -> str:
    """Canonicalize one route without its self-reported digest.

    ``semantic_digest`` is evidence metadata derived from the route payload;
    it is not an additional business field.  Keeping a second comparison
    which omits only that field lets the diagnostic gate distinguish harmless
    digest/serialization drift from an actual route-frontier change.  No
    numeric tolerance, field omission, or route substitution is allowed.
    """

    if isinstance(item, Mapping):
        item = {key: value for key, value in item.items() if key != "semantic_digest"}
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    before = {}
    after = {}
    base = None
    fixture = None
    result = None
    session = None
    certificate = None
    reference = None
    semantic = None
    frontier: list[dict[str, Any]] = []
    reference_match: bool | None = None
    planner_error: dict[str, Any] | None = None
    identity_error: str | None = None
    search_started = None
    try:
        _set_cpu(args.cpu)
        base = _load_base_runner()
        before = base._resource_snapshot()
        point = base._load_point_runner()
        fixture = point._load_fixture(_fixture_args(args))
        objective = ObjectiveMode(args.objective)
        planner = point._build_planner(fixture, objective)
        base._configure_eta_policy(planner, args.eta_method)
        request = base.replace(
            point._request(fixture, objective),
            use_heuristic=False,
            cancel_check=None,
        )
        search_started = time.perf_counter()
        session = create_non_fifo_temporal_pareto_session(
            planner,
            request,
            pareto_pruning=args.policy == "pareto",
            skip_expected_rejections=True,
        )
        result = session.run()
        certificate = session.frontier_certificate
        if result.status is NonFifoSearchStatus.GOAL_FOUND:
            if result.selected is None:
                raise RuntimeError("GOAL_FOUND result has no selected route")
            semantic = _route_payload(result.selected, base)
            frontier = [_route_payload(route, base) for route in result.frontier]
            reference = point._reference_search(planner, request)
            reference_match = base._reference_matches(semantic, reference)
    except NonFifoTemporalParetoError as error:
        identity_error = f"{type(error).__name__}:{error}"
    except Exception as error:  # pragma: no cover - worker boundary evidence
        planner_error = {"type": type(error).__name__, "message": str(error)}
    else:
        certificate = session.frontier_certificate if session is not None else None
    after = base._resource_snapshot() if base is not None else {}
    status = result.status.value if result is not None else "INVALID/PENDING"
    raw = result.raw_result if result is not None else None
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    stats = (
        {
            "expanded": raw.expanded,
            "generated": raw.generated,
            "queue_peak": raw.queue_peak,
            "edge_evaluations": raw.edge_evaluations,
            "pareto_pruned": raw.pareto_pruned,
            "search_limits": raw.search_limits,
            "pareto_pruning": raw.pareto_pruning,
        }
        if raw is not None
        else None
    )
    cert_payload = _certificate_payload(certificate) if certificate is not None else None
    unexpected_pruning = False
    if isinstance(diagnostics, Mapping):
        unexpected_pruning = any(
            int(diagnostics.get(name, 0) or 0) > 0
            for name in (
                "dominance_checks",
                "dominance_pruned",
                "state_bound_checks",
                "state_bound_pruned",
            )
        )
    resource_clean = bool(base is not None and base._resource_clean(before, after))
    resource_complete = bool(
        base is not None
        and result is not None
        and base._resource_evidence_complete(
            {"resources_before": before, "resources_after": after}, args.cpu
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "input": getattr(fixture, "input_name", None),
        "segment": args.segment,
        "objective": args.objective,
        "policy": args.policy,
        "repetition": args.repetition,
        "status": status,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "pareto_pruning": args.policy == "pareto",
        "eta_method": args.eta_method,
        "session_id": result.session_id if result is not None else None,
        "scope_digest": result.scope_digest if result is not None else None,
        "semantic": semantic,
        "semantic_digest": result.semantic_digest if result is not None else None,
        "frontier": frontier,
        "frontier_digest": _frontier_digest(frontier) if frontier else None,
        "frontier_certificate": cert_payload,
        "reference": reference,
        "reference_match": reference_match,
        "diagnostics": diagnostics,
        "search_stats": stats,
        "pareto_pruned": stats["pareto_pruned"] if stats else 0,
        "unexpected_pruning": unexpected_pruning,
        "reason": result.reason if result is not None else identity_error,
        "evaluator_errors": list(result.evaluator_errors) if result is not None else [],
        "planner_error": planner_error,
        "compute_ms": (time.perf_counter() - search_started) * 1000.0 if search_started else None,
        "wall_seconds": time.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_complete,
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {"files": files, "sha256": _digest(files)}


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _experiment_identity(args: argparse.Namespace, fixture: Any, root: Path) -> dict[str, Any]:
    point = _load_base_runner()._load_point_runner()
    return {
        "schema_version": SCHEMA_VERSION,
        "git": _git_identity(root),
        "implementation": _implementation_identity(root),
        "uv_lock": {"path": str(root / "uv.lock"), "sha256": _sha256(root / "uv.lock")},
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
            "frame_digests": [
                _digest(
                    {
                        "risk_id": frame.risk_id,
                        "valid_time": frame.valid_time,
                        "generation_id": frame.generation_id,
                        "content": point.risk_frame_content_digest(frame),
                    }
                )
                for frame in fixture.frames
            ],
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
        "adapter_mode": "actual_edge_zero_heuristic_pareto_v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "eta_method": args.eta_method,
        "policies": list(POLICIES),
        "objectives": [objective.value for objective in OBJECTIVES],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "search_limits": SEARCH_LIMITS,
        "cpu": args.cpu,
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


def _child_command(
    args: argparse.Namespace, objective: ObjectiveMode, repetition: int, policy: str
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--risk-window-commit",
        str(Path(args.risk_window_commit).resolve()),
        "--route-plan-set",
        str(Path(args.route_plan_set).resolve()),
        "--config-root",
        str(Path(args.config_root).resolve()),
        "--segment",
        args.segment,
        "--output-dir",
        str(Path(args.output_dir).resolve()),
        "--objective",
        objective.value,
        "--policy",
        policy,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
        "--eta-method",
        args.eta_method,
    ]


def _run_child(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    policy: str,
    heartbeat: Path,
) -> dict[str, Any]:
    started = time.time()
    try:
        process = subprocess.Popen(
            _child_command(args, objective, repetition, policy),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "policy": policy,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "planner_error": {"type": type(error).__name__, "message": str(error)},
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    while process.poll() is None:
        elapsed = time.time() - started
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "updated_at": datetime.now(UTC),
                "pid": process.pid,
                "objective": objective.value,
                "policy": policy,
                "repetition": repetition,
                "elapsed_seconds": elapsed,
            },
        )
        if elapsed > args.worker_timeout_seconds:
            process.kill()
            stdout, stderr = process.communicate()
            return {
                "schema_version": SCHEMA_VERSION,
                "objective": objective.value,
                "policy": policy,
                "repetition": repetition,
                "status": "TIMEOUT",
                "reason": "worker_timeout",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "resource_clean": False,
                "resource_evidence_complete": False,
            }
        time.sleep(1.0)
    stdout, stderr = process.communicate()
    if process.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "policy": policy,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "reason": "worker exited non-zero",
            "returncode": process.returncode,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return {
            "schema_version": SCHEMA_VERSION,
            "objective": objective.value,
            "policy": policy,
            "repetition": repetition,
            "status": "INVALID/PENDING",
            "reason": "worker did not emit one JSON object",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "resource_clean": False,
            "resource_evidence_complete": False,
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _record_key(record: Mapping[str, Any]) -> tuple[str, str, int] | None:
    objective = record.get("objective")
    policy = record.get("policy")
    repetition = record.get("repetition")
    if not isinstance(objective, str) or policy not in POLICIES or not isinstance(repetition, int):
        return None
    return objective, policy, repetition


def _read_cases(path: Path) -> tuple[list[dict[str, Any]], int]:
    if not path.exists():
        return [], 0
    values: list[dict[str, Any]] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict):
            values.append(value)
        else:
            malformed += 1
    return values, malformed


def _frontier_comparison(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    candidate_certificate = candidate.get("frontier_certificate")
    baseline_certificate = baseline.get("frontier_certificate")
    comparison = {
        "schema_version": "c.p0.2-nonfifo-frontier-real-comparison.v1",
        "objective": candidate.get("objective"),
        "repetition": candidate.get("repetition"),
        "candidate_policy": candidate.get("policy"),
        "reference_policy": baseline.get("policy"),
        "candidate_certificate_digest": (
            candidate_certificate.get("digest")
            if isinstance(candidate_certificate, Mapping)
            else None
        ),
        "reference_certificate_digest": (
            baseline_certificate.get("digest")
            if isinstance(baseline_certificate, Mapping)
            else None
        ),
        "candidate_frontier_digest": candidate.get("frontier_digest"),
        "reference_frontier_digest": baseline.get("frontier_digest"),
        "candidate_scope_digest": candidate.get("scope_digest"),
        "reference_scope_digest": baseline.get("scope_digest"),
        "candidate_comparison_identity_digest": (
            candidate_certificate.get("comparison_identity_digest")
            if isinstance(candidate_certificate, Mapping)
            else None
        ),
        "reference_comparison_identity_digest": (
            baseline_certificate.get("comparison_identity_digest")
            if isinstance(baseline_certificate, Mapping)
            else None
        ),
    }
    identity_ok = (
        comparison["candidate_scope_digest"] is not None
        and comparison["candidate_scope_digest"] == comparison["reference_scope_digest"]
        and comparison["candidate_comparison_identity_digest"] is not None
        and comparison["candidate_comparison_identity_digest"]
        == comparison["reference_comparison_identity_digest"]
    )
    complete = bool(
        isinstance(candidate_certificate, Mapping)
        and isinstance(baseline_certificate, Mapping)
        and candidate_certificate.get("usable") is True
        and baseline_certificate.get("usable") is True
        and candidate_certificate.get("complete") is True
        and baseline_certificate.get("complete") is True
    )
    candidate_frontier = candidate.get("frontier") or []
    baseline_frontier = baseline.get("frontier") or []
    candidate_tokens = sorted(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in candidate_frontier
    )
    baseline_tokens = sorted(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for item in baseline_frontier
    )
    candidate_semantic_tokens = sorted(
        _semantic_frontier_token(item) for item in candidate_frontier
    )
    baseline_semantic_tokens = sorted(
        _semantic_frontier_token(item) for item in baseline_frontier
    )
    exact_match = bool(identity_ok and complete and candidate_tokens == baseline_tokens)
    semantic_match = bool(
        identity_ok and complete and candidate_semantic_tokens == baseline_semantic_tokens
    )
    comparison["candidate_label_count"] = len(candidate_tokens)
    comparison["reference_label_count"] = len(baseline_tokens)
    comparison["missing_label_digests"] = [
        _digest(token) for token in sorted(set(baseline_tokens) - set(candidate_tokens))
    ]
    comparison["unexpected_label_digests"] = [
        _digest(token) for token in sorted(set(candidate_tokens) - set(baseline_tokens))
    ]
    comparison["missing_semantic_label_digests"] = [
        _digest(token)
        for token in sorted(set(baseline_semantic_tokens) - set(candidate_semantic_tokens))
    ]
    comparison["unexpected_semantic_label_digests"] = [
        _digest(token)
        for token in sorted(set(candidate_semantic_tokens) - set(baseline_semantic_tokens))
    ]
    comparison["identity_match"] = identity_ok
    comparison["certificates_complete"] = complete
    comparison["exact_frontier_match"] = exact_match
    comparison["semantic_frontier_match"] = semantic_match
    comparison["accepted_frontier_match"] = semantic_match
    comparison["status"] = (
        "MATCH"
        if exact_match
        else "SEMANTIC_MATCH"
        if semantic_match
        else "IDENTITY_MISMATCH"
        if not identity_ok
        else "INCOMPLETE"
        if not complete
        else "FRONTIER_MISMATCH"
    )
    comparison["reason"] = None if exact_match else comparison["status"].lower()
    comparison["digest"] = _digest(comparison)
    return comparison


def _summary(
    cases: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
    args: argparse.Namespace,
    malformed: int,
) -> dict[str, Any]:
    expected = len(OBJECTIVES) * len(POLICIES) * args.repetitions
    keys = [_record_key(case) for case in cases]
    complete = (
        len(cases) == expected
        and malformed == 0
        and None not in keys
        and len(set(keys)) == len(keys)
    )
    by_cell: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        if isinstance(case.get("objective"), str) and case.get("policy") in POLICIES:
            by_cell[(case["objective"], case["policy"])].append(case)
    deterministic_by_cell = {}
    for objective in OBJECTIVES:
        for policy in POLICIES:
            values = by_cell.get((objective.value, policy), [])
            signatures = {
                (
                    value.get("status"),
                    value.get("semantic_digest"),
                    value.get("frontier_digest"),
                    value.get("pareto_pruned"),
                    value.get("frontier_certificate", {}).get("frontier_digest")
                    if isinstance(value.get("frontier_certificate"), Mapping)
                    else None,
                )
                for value in values
            }
            deterministic_by_cell[f"{objective.value}:{policy}"] = (
                args.repetitions >= 2 and len(values) == args.repetitions and len(signatures) == 1
            )
    deterministic = bool(deterministic_by_cell) and all(deterministic_by_cell.values())
    reference_ok = all(
        case.get("status") != NonFifoSearchStatus.GOAL_FOUND.value
        or case.get("reference_match") is True
        for case in cases
    )
    no_unexpected_pruning = not any(case.get("unexpected_pruning") for case in cases)
    resources_clean = all(case.get("resource_clean") is True for case in cases)
    resources_complete = all(case.get("resource_evidence_complete") is True for case in cases)
    all_terminal = all(
        case.get("status") in TERMINAL_STATUSES | {"TIMEOUT", "INVALID/PENDING"} for case in cases
    )
    pair_complete = len(comparisons) == len(OBJECTIVES) * args.repetitions
    strict_pair_match = pair_complete and all(item.get("status") == "MATCH" for item in comparisons)
    pair_match = pair_complete and all(
        item.get("status") in {"MATCH", "SEMANTIC_MATCH"} for item in comparisons
    )
    status = (
        "NO_FRONTIER_PROOF/FAIL"
        if not reference_ok
        or not no_unexpected_pruning
        or any(item.get("status") == "FRONTIER_MISMATCH" for item in comparisons)
        else "INVALID/PENDING"
        if not complete or not all_terminal
        else "REAL_INPUT_FRONTIER_EQUIVALENCE_INCONCLUSIVE"
        if not pair_match or not deterministic or not resources_clean or not resources_complete
        else "READY_FOR_P0.2-REAL-FRONTIER-IMPLEMENTATION-REVIEW"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "expected_case_count": expected,
        "case_count": len(cases),
        "malformed_records": malformed,
        "complete": complete,
        "all_terminal": all_terminal,
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "point_reference_match": reference_ok,
        "no_unexpected_pruning": no_unexpected_pruning,
        "frontier_pair_count": len(comparisons),
        "frontier_pairs_match": pair_match,
        "strict_frontier_pairs_match": strict_pair_match,
        "semantic_frontier_pairs_match": pair_match,
        "frontier_comparisons": comparisons,
        "resource_clean": resources_clean,
        "resource_evidence_complete": resources_complete,
        "pareto_pruned_total": sum(int(case.get("pareto_pruned", 0) or 0) for case in cases),
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "candidate_authorized": False,
        "winter_authorized": False,
        "cases": cases,
    }


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
            raise RuntimeError("another M21 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=SEGMENTS, default="executable_0_6h")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--eta-method", choices=("default", "bounded"), default="bounded")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--objective", choices=tuple(item.value for item in OBJECTIVES), help=argparse.SUPPRESS
    )
    parser.add_argument("--policy", choices=POLICIES, help=argparse.SUPPRESS)
    parser.add_argument("--repetition", type=int, help=argparse.SUPPRESS)
    return parser


def _run_parent(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("repetitions/timeout must be positive and cpu >= -1")
    root = Path(__file__).resolve().parents[1]
    point = _load_base_runner()._load_point_runner()
    fixture = point._load_fixture(_fixture_args(args))
    identity = _experiment_identity(args, fixture, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("M21 real evidence requires a clean implementation worktree")
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest_path = output / "manifest.json"
        previous = None
        if manifest_path.exists():
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not args.resume:
                raise RuntimeError("experiment already exists; use --resume")
            if previous.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity does not match")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "experiment_id": identity["experiment_id"],
            "identity": identity,
            "evidence_files": (
                "manifest.json",
                "cases.jsonl",
                "frontier-comparison.jsonl",
                "resource-frontier.jsonl",
                "comparison-summary.json",
                "heartbeat.json",
                "ALL_DONE/STOPPED_HARD",
            ),
        }
        if previous is not None:
            manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
        _atomic_json(manifest_path, manifest)
        cases_path = output / "cases.jsonl"
        cases, malformed = _read_cases(cases_path) if args.resume else ([], 0)
        completed = {_record_key(case) for case in cases}
        completed.discard(None)
        heartbeat = output / "heartbeat.json"
        expected = len(OBJECTIVES) * len(POLICIES) * args.repetitions
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "completed_cases": len(cases),
                "expected_cases": expected,
            },
        )
        try:
            for repetition in range(1, args.repetitions + 1):
                order = OBJECTIVES if repetition % 2 else tuple(reversed(OBJECTIVES))
                for objective in order:
                    for policy in POLICIES:
                        key = (objective.value, policy, repetition)
                        if key in completed:
                            continue
                        record = _run_child(args, objective, repetition, policy, heartbeat)
                        record.setdefault("schema_version", SCHEMA_VERSION)
                        record["experiment_id"] = identity["experiment_id"]
                        if _record_key(record) != key:
                            raise RuntimeError("worker returned mismatched case identity")
                        cases.append(record)
                        _append_jsonl(cases_path, record)
                        _append_jsonl(output / "resource-frontier.jsonl", record)
                        completed.add(key)
                        _atomic_json(
                            heartbeat,
                            {
                                "schema_version": SCHEMA_VERSION,
                                "status": "RUNNING",
                                "updated_at": datetime.now(UTC),
                                "completed_cases": len(cases),
                                "expected_cases": expected,
                            },
                        )
        except (KeyboardInterrupt, SystemExit) as error:
            summary = {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "reason": f"runner interrupted: {type(error).__name__}",
                "cases": cases,
            }
            _atomic_json(output / "comparison-summary.json", summary)
            manifest.update(
                {"status": "STOPPED_HARD", "summary": summary, "completed_at": datetime.now(UTC)}
            )
            _atomic_json(manifest_path, manifest)
            _atomic_json(
                heartbeat,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "STOPPED_HARD",
                    "updated_at": datetime.now(UTC),
                },
            )
            (output / "STOPPED_HARD").write_text(summary["reason"] + "\n", encoding="utf-8")
            return 2
        by_pair = {
            (case.get("objective"), case.get("repetition")): {
                case.get("policy"): case for case in cases
            }
            for case in cases
            if case.get("policy") in POLICIES
        }
        comparisons: list[dict[str, Any]] = []
        for repetition in range(1, args.repetitions + 1):
            for objective in OBJECTIVES:
                pair = by_pair.get((objective.value, repetition), {})
                baseline = pair.get("baseline")
                candidate = pair.get("pareto")
                if baseline is not None and candidate is not None:
                    comparison = _frontier_comparison(candidate, baseline)
                    comparison["experiment_id"] = identity["experiment_id"]
                    comparisons.append(comparison)
                    _append_jsonl(output / "frontier-comparison.jsonl", comparison)
        summary = _summary(cases, comparisons, args, malformed)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {"status": summary["status"], "summary": summary, "completed_at": datetime.now(UTC)}
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            heartbeat,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "updated_at": datetime.now(UTC),
                "completed_cases": len(cases),
                "expected_cases": expected,
            },
        )
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if summary["status"] == "READY_FOR_P0.2-REAL-FRONTIER-IMPLEMENTATION-REVIEW" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker:
        if args.objective is None or args.policy is None or args.repetition is None:
            raise SystemExit("worker requires objective/policy/repetition")
        print(json.dumps(_jsonable(_worker(args)), ensure_ascii=False, sort_keys=True))
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
