#!/usr/bin/env python3
"""Research-only 24-hour audit for the certified temporal heuristic.

This runner applies the M8 ordering-only heuristic to the already frozen
``rolling_0_24h`` real-input segment.  It deliberately delegates the actual
fixture/evaluator/search worker to the audited M8 runner, while giving the
long-horizon experiment its own schema and identity.  No dominance,
state-bound, approximate, or production path is enabled.
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
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import ObjectiveMode

SCHEMA_VERSION = "c.p0.2-temporal-certified-heuristic-real-24h.v2"
SEGMENT = "rolling_0_24h"
OBJECTIVES = tuple(ObjectiveMode)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_non_fifo_temporal_certified_heuristic_real_24h.py",
    "scripts/benchmark_non_fifo_temporal_certified_heuristic_real.py",
    "scripts/benchmark_non_fifo_temporal_certified_heuristic.py",
    "scripts/benchmark_temporal_dominance_real.py",
    "src/arctic_route_planning/planners/non_fifo_temporal_adapter.py",
    "src/arctic_route_planning/planners/temporal_heuristic_bounds.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
    "src/arctic_route_planning/planners/temporal_topology_bounds.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "uv.lock",
)


def _load_m8_runner() -> Any:
    path = (
        Path(__file__)
        .resolve()
        .with_name("benchmark_non_fifo_temporal_certified_heuristic_real.py")
    )
    spec = importlib.util.spec_from_file_location("c_m9_m8_real_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the audited M8 real runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _jsonable(value: Any) -> Any:
    return _load_m8_runner()._jsonable(value)


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


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    _load_m8_runner()._atomic_json(path, value)


def _append_jsonl(path: Path, value: Any) -> None:
    _load_m8_runner()._append_jsonl(path, value)


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
            raise RuntimeError("another M9 runner owns this output") from error
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _fixture_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        mode="resource-frontier",
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=SEGMENT,
    )


def _worker_phase(args: argparse.Namespace) -> dict[str, Any]:
    """Run one baseline/candidate/reference phase in its own process.

    The M8 combined worker is appropriate for the short 6-hour diagnostic, but
    a long-horizon zero-heuristic baseline can consume the whole deadline
    before the candidate is exercised.  Phase isolation makes that distinction
    explicit without changing the search implementation.
    """

    m8 = _load_m8_runner()
    point = m8._load_script("benchmark_temporal_dominance_real.py", f"c_m9_point_{args.phase}")
    fixture = point._load_fixture(_fixture_args(args))
    objective = ObjectiveMode(args.objective)
    m8._set_cpu(args.cpu)
    before = point._resource_snapshot()
    started = m8.perf_counter()
    baseline = None
    candidate = None
    reference = None
    topology = None
    certificate = None
    errors: dict[str, str] = {}
    session_identity = None
    try:
        if args.phase == "baseline":
            planner = point._build_planner(fixture, objective)
            request = replace(point._request(fixture, objective), use_heuristic=False)
            session_identity = m8.TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
            baseline = m8.run_non_fifo_temporal_search(planner, request, identity=session_identity)
        elif args.phase == "candidate":
            planner, request, evidence = m8._certificate(point, fixture, objective)
            topology, certificate = evidence
            planner.heuristic_certificate = certificate
            session_identity = m8.TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
            candidate = m8.run_non_fifo_temporal_certified_heuristic_search(
                planner, request, certificate, identity=session_identity
            )
        elif args.phase == "reference":
            planner = point._build_planner(fixture, objective)
            request = replace(point._request(fixture, objective), use_heuristic=False)
            session_identity = m8.TemporalSessionIdentity.from_planner(
                planner,
                request,
                risk_window_content_digest=fixture.commit["content_digest"],
                risk_window_commit_id=fixture.commit["commit_id"],
            )
            reference = point._reference_search(planner, request)
        else:  # pragma: no cover - argparse and worker command constrain this
            raise ValueError(f"unknown phase: {args.phase}")
    except Exception as error:  # pragma: no cover - isolated evaluator boundary
        errors[args.phase] = f"{type(error).__name__}: {error}"
    after = point._resource_snapshot()
    result = baseline if baseline is not None else candidate
    if args.phase == "reference":
        phase_status = "PASS" if reference is not None else "ERROR"
    elif result is None:
        phase_status = "ERROR"
    else:
        phase_status = result.status.value
    semantic = (
        None if result is None or result.planning_result is None else point._route_semantic(result)
    )
    diagnostics = None if result is None else _jsonable(result.diagnostics)
    resource_clean = point._resource_clean(before, after)
    resource_evidence_complete = point._resource_evidence_complete(
        {"resources_before": before, "resources_after": after}, cpu=args.cpu
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": phase_status,
        "phase": args.phase,
        "input": fixture.input_name,
        "segment": SEGMENT,
        "objective": objective.value,
        "repetition": args.repetition,
        "adapter_mode": "non_fifo_certified_graph_heuristic_24h_v2",
        "baseline_status": None if baseline is None else baseline.status.value,
        "candidate_status": None if candidate is None else candidate.status.value,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "candidate_semantic_digest": None if candidate is None else candidate.semantic_digest,
        "semantic": semantic,
        "reference": reference,
        "diagnostics": diagnostics,
        "heuristic_policy": None if candidate is None else candidate.diagnostics.heuristic_policy,
        "heuristic_certificate_digest": None if certificate is None else certificate.digest,
        "heuristic_proof_digest": None if certificate is None else certificate.proof_digest,
        "heuristic_scope_match": False
        if candidate is None
        else candidate.diagnostics.heuristic_scope_match,
        "heuristic_rejected": 0 if candidate is None else candidate.diagnostics.heuristic_rejected,
        "topology_digest": None if topology is None else topology.digest,
        "session_identity": None if session_identity is None else session_identity.digest,
        "errors": errors,
        "wall_seconds": m8.perf_counter() - started,
        "resources_before": before,
        "resources_after": after,
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "cpu": args.cpu,
        "production_candidate_enabled": False,
    }


def _worker_command(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    phase: str,
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
        SEGMENT,
        "--output-dir",
        str(args.output_dir),
        "--objective",
        objective.value,
        "--phase",
        phase,
        "--repetition",
        str(repetition),
        "--cpu",
        str(args.cpu),
    ]


def _run_worker(
    args: argparse.Namespace,
    objective: ObjectiveMode,
    repetition: int,
    phase: str,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            _worker_command(args, objective, repetition, phase),
            check=False,
            capture_output=True,
            text=True,
            timeout=args.worker_timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "TIMEOUT",
            "objective": objective.value,
            "repetition": repetition,
            "segment": SEGMENT,
            "phase": phase,
            "reason": str(error),
        }
    if completed.returncode != 0:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "objective": objective.value,
            "repetition": repetition,
            "segment": SEGMENT,
            "phase": phase,
            "reason": completed.stderr[-4000:] or completed.stdout[-4000:],
        }
    try:
        record = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "INVALID/PENDING",
            "objective": objective.value,
            "repetition": repetition,
            "segment": SEGMENT,
            "phase": phase,
            "reason": f"worker JSON decode failed: {error}",
        }
    if not isinstance(record, dict):
        raise RuntimeError("worker emitted a non-object JSON record")
    return record


def _combine_case(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    reference: dict[str, Any],
    *,
    objective: ObjectiveMode,
    repetition: int,
    experiment_id: str,
) -> dict[str, Any]:
    """Join independent phase evidence without hiding a missing phase."""

    m8 = _load_m8_runner()
    point = m8._load_script("benchmark_temporal_dominance_real.py", "c_m9_point_comparator")
    baseline_semantic = baseline.get("semantic")
    candidate_semantic = candidate.get("semantic")
    reference_value = reference.get("reference")
    baseline_match = (
        baseline_semantic is not None
        and reference_value is not None
        and point._reference_matches(baseline_semantic, reference_value)
    )
    candidate_match = (
        candidate_semantic is not None
        and reference_value is not None
        and point._reference_matches(candidate_semantic, reference_value)
    )
    semantic_match = (
        baseline.get("status") == "GOAL_FOUND"
        and candidate.get("status") == "GOAL_FOUND"
        and baseline.get("candidate_semantic_digest") is None
        and baseline.get("baseline_semantic_digest") == candidate.get("candidate_semantic_digest")
        and baseline.get("baseline_semantic_digest") is not None
    )
    phase_records = {
        "baseline": baseline,
        "candidate": candidate,
        "reference": reference,
    }
    resource_clean = all(phase.get("resource_clean") is True for phase in phase_records.values())
    resource_evidence_complete = all(
        phase.get("resource_evidence_complete") is True for phase in phase_records.values()
    )
    errors: dict[str, Any] = {}
    for phase_name, phase in phase_records.items():
        if phase.get("errors"):
            errors[phase_name] = phase["errors"]
        elif phase.get("status") not in {"PASS", "GOAL_FOUND"}:
            errors[phase_name] = phase.get("reason", phase.get("status"))
    status = (
        "PASS"
        if (
            all(phase.get("status") in {"PASS", "GOAL_FOUND"} for phase in phase_records.values())
            and semantic_match
            and baseline_match
            and candidate_match
            and candidate.get("heuristic_scope_match") is True
            and candidate.get("heuristic_rejected") == 0
            and (candidate.get("diagnostics") or {}).get("dominance_pruned", 0) == 0
            and (candidate.get("diagnostics") or {}).get("state_bound_pruned", 0) == 0
            and resource_clean
            and resource_evidence_complete
        )
        else "REAL_INPUT_24H_RESOURCE_FAIL"
        if any(
            phase.get("status") in {"TIMEOUT", "RESOURCE_LIMIT"} for phase in phase_records.values()
        )
        else "FAIL"
    )
    baseline_diagnostics = baseline.get("diagnostics") or {}
    candidate_diagnostics = candidate.get("diagnostics") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": experiment_id,
        "input": baseline.get("input") or candidate.get("input"),
        "segment": SEGMENT,
        "objective": objective.value,
        "repetition": repetition,
        "adapter_mode": "non_fifo_certified_graph_heuristic_24h_v2",
        "phase_statuses": {name: phase.get("status") for name, phase in phase_records.items()},
        "baseline_status": baseline.get("status"),
        "candidate_status": candidate.get("status"),
        "reference_status": reference.get("status"),
        "baseline_semantic_digest": baseline.get("baseline_semantic_digest"),
        "candidate_semantic_digest": candidate.get("candidate_semantic_digest"),
        "semantic_match": semantic_match,
        "reference_match": baseline_match and candidate_match,
        "baseline_reference_match": baseline_match,
        "candidate_reference_match": candidate_match,
        "baseline_semantic": baseline_semantic,
        "candidate_semantic": candidate_semantic,
        "reference": reference_value,
        "baseline_diagnostics": baseline_diagnostics,
        "candidate_diagnostics": candidate_diagnostics,
        "baseline_expanded_labels": int(baseline_diagnostics.get("expanded_labels", 0)),
        "candidate_expanded_labels": int(candidate_diagnostics.get("expanded_labels", 0)),
        "baseline_queue_peak": int(baseline_diagnostics.get("queue_peak", 0)),
        "candidate_queue_peak": int(candidate_diagnostics.get("queue_peak", 0)),
        "heuristic_policy": candidate.get("heuristic_policy"),
        "heuristic_certificate_digest": candidate.get("heuristic_certificate_digest"),
        "heuristic_proof_digest": candidate.get("heuristic_proof_digest"),
        "heuristic_scope_match": candidate.get("heuristic_scope_match", False),
        "heuristic_rejected": candidate.get("heuristic_rejected", 0),
        "topology_digest": candidate.get("topology_digest"),
        "session_identities": {
            name: phase.get("session_identity") for name, phase in phase_records.items()
        },
        "phase_records": phase_records,
        "errors": errors,
        "wall_seconds": sum(
            float(phase.get("wall_seconds", 0.0)) for phase in phase_records.values()
        ),
        "resource_clean": resource_clean,
        "resource_evidence_complete": resource_evidence_complete,
        "resources_by_phase": {
            name: {
                "before": phase.get("resources_before"),
                "after": phase.get("resources_after"),
            }
            for name, phase in phase_records.items()
        },
        "cpu": candidate.get("cpu", baseline.get("cpu")),
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "reason": None if status == "PASS" else "independent 24h phase gate failed",
    }


def _identity(
    args: argparse.Namespace, fixture: Any, root: Path, selected: tuple[ObjectiveMode, ...]
) -> dict[str, Any]:
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
            "segment": SEGMENT,
            "start": fixture.start,
            "goal": fixture.goal,
            "departure": fixture.departure,
            "frame_count": len(fixture.frames),
        },
        "objectives": [item.value for item in selected],
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "adapter_mode": "non_fifo_certified_graph_heuristic_24h_v2",
        "heuristic_method": "graph-topological-objective-lower-bound-v1",
        "heuristic_evaluator": "certified:cost-model-graph-lower-bound-v1",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "search_limits": LIMITS,
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _summary(
    cases: list[dict[str, Any]],
    identity: dict[str, Any],
) -> dict[str, Any]:
    selected = set(identity["objectives"])
    expected = len(selected) * int(identity["repetitions"])
    complete = len(cases) == expected
    all_pass = complete and all(case.get("status") == "PASS" for case in cases)
    semantic = bool(cases) and all(case.get("semantic_match") is True for case in cases)
    reference = bool(cases) and all(case.get("reference_match") is True for case in cases)
    resource = bool(cases) and all(case.get("resource_clean") is True for case in cases)
    evidence = bool(cases) and all(case.get("resource_evidence_complete") is True for case in cases)
    deterministic = True
    for objective in selected:
        records = [case for case in cases if case.get("objective") == objective]
        digests = {
            case.get("candidate_semantic_digest")
            for case in records
            if case.get("candidate_status") in {"GOAL_FOUND", "PASS"}
        }
        if len(records) != int(identity["repetitions"]) or len(digests) != 1 or None in digests:
            deterministic = False
    deltas = [
        {
            "objective": case.get("objective"),
            "repetition": case.get("repetition"),
            "expanded_delta": int(case.get("baseline_expanded_labels", 0))
            - int(case.get("candidate_expanded_labels", 0)),
            "queue_delta": int(case.get("baseline_queue_peak", 0))
            - int(case.get("candidate_queue_peak", 0)),
        }
        for case in cases
    ]
    has_resource_failure = any(
        case.get("status") in {"TIMEOUT", "RESOURCE_LIMIT"}
        or case.get("candidate_status") == "RESOURCE_LIMIT"
        or case.get("baseline_status") == "RESOURCE_LIMIT"
        for case in cases
    )
    status = (
        "READY_FOR_P0.2-CERTIFIED-HEURISTIC-24H-REVIEW"
        if all_pass and semantic and reference and resource and evidence and deterministic
        else "REAL_INPUT_24H_RESOURCE_FAIL"
        if complete and has_resource_failure
        else "NO_PERFORMANCE_PROOF/FAIL"
        if complete
        else "INVALID/PENDING"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "experiment_id": identity["experiment_id"],
        "expected_case_count": expected,
        "case_count": len(cases),
        "semantic_match": semantic,
        "reference_match": reference,
        "resource_clean": resource,
        "resource_evidence_complete": evidence,
        "deterministic": deterministic,
        "expansion_queue_deltas": deltas,
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
        "known_fifo_status": "REAL_INPUT_FIFO_VIOLATED",
        "production_candidate_enabled": False,
        "cases": cases,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-window-commit", type=Path, required=True)
    parser.add_argument("--route-plan-set", type=Path, required=True)
    parser.add_argument("--config-root", type=Path, required=True)
    parser.add_argument("--segment", choices=(SEGMENT,), default=SEGMENT)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--worker-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--objective", choices=tuple(item.value for item in OBJECTIVES))
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument(
        "--phase",
        choices=("baseline", "candidate", "reference"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < 0:
        raise SystemExit("repetitions/timeout must be positive and cpu must be non-negative")
    if args.segment != SEGMENT:
        raise SystemExit(f"M9 only supports {SEGMENT}")
    root = Path(__file__).resolve().parents[1]
    if args.worker:
        if args.objective is None or args.phase is None:
            raise SystemExit("worker requires --objective and --phase")
        print(json.dumps(_jsonable(_worker_phase(args)), ensure_ascii=False, sort_keys=True))
        return 0
    m8 = _load_m8_runner()
    point = m8._load_script("benchmark_temporal_dominance_real.py", "c_m9_point_runner")
    fixture = point._load_fixture(_fixture_args(args))
    selected = (ObjectiveMode(args.objective),) if args.objective else OBJECTIVES
    identity = _identity(args, fixture, root, selected)
    dirty = subprocess.check_output(
        ("git", "-C", str(root), "status", "--porcelain"), text=True
    ).strip()
    if dirty:
        raise RuntimeError("M9 real runner requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".runner.lock"):
        manifest = output / "manifest.json"
        if manifest.exists():
            if not args.resume:
                raise RuntimeError("experiment exists; use --resume")
            if json.loads(manifest.read_text(encoding="utf-8")).get("identity") != _jsonable(
                identity
            ):
                raise RuntimeError("resume identity mismatch")
        _atomic_json(
            manifest,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "RUNNING",
                "experiment_id": identity["experiment_id"],
                "identity": identity,
            },
        )
        cases_path = output / "cases.jsonl"
        existing: dict[tuple[str, int], dict[str, Any]] = {}
        if args.resume and cases_path.exists():
            for line in cases_path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = (record.get("objective"), record.get("repetition"))
                if key in existing:
                    raise RuntimeError("resume evidence contains duplicate case")
                existing[key] = record
        cases = list(existing.values())
        heartbeat = output / "heartbeat.json"
        expected_cases = len(selected) * args.repetitions
        for repetition in range(1, args.repetitions + 1):
            order = selected if repetition % 2 else tuple(reversed(selected))
            for objective in order:
                key = (objective.value, repetition)
                if key in existing:
                    continue
                phases = {
                    phase: _run_worker(args, objective, repetition, phase)
                    for phase in ("baseline", "candidate", "reference")
                }
                record = _combine_case(
                    phases["baseline"],
                    phases["candidate"],
                    phases["reference"],
                    objective=objective,
                    repetition=repetition,
                    experiment_id=identity["experiment_id"],
                )
                record.update(
                    {
                        "experiment_id": identity["experiment_id"],
                        "objective": objective.value,
                        "repetition": repetition,
                        "segment": SEGMENT,
                    }
                )
                _append_jsonl(cases_path, record)
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
                        "expected_cases": expected_cases,
                    },
                )
        summary = _summary(cases, identity)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            manifest,
            {
                "schema_version": SCHEMA_VERSION,
                "status": summary["status"],
                "experiment_id": identity["experiment_id"],
                "identity": identity,
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
        marker = "ALL_DONE" if summary["status"].startswith("READY_FOR") else "STOPPED_HARD"
        _atomic_json(
            output / marker,
            {"status": summary["status"], "experiment_id": identity["experiment_id"]},
        )
        compact = {key: value for key, value in summary.items() if key != "cases"}
        print(json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["status"].startswith("READY_FOR") else 2


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
