#!/usr/bin/env python3
"""Synthetic proof matrix for the actual Pareto state-bound bridge.

This runner is a C-internal research sidecar.  It exercises a proof-carrying
``TemporalStateBoundCertificate`` only after an actual edge has produced an
exact arrival time.  The certificate can therefore reject a newly generated
label without deleting an expanded label or importing a FIFO assumption.
The ordinary actual Pareto bridge remains the default when no certificate is
provided.  The route oracle is used only for semantic comparison and is never
passed to the candidate search.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    TemporalParetoComponent,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_bounds import (
    TemporalStateBoundCertificate,
    qualify_state_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-state-bound.v1"
OBJECTIVES = ("fastest", "low_risk", "recommended")
MODES = ("one_shot", "slice_restore", "cancelled")
SCENARIOS = (
    "certified",
    "scope_mismatch",
    "coverage_incomplete",
    "checkpoint_drift",
    "disabled",
)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}


def _load_base_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_non_fifo_temporal_pareto.py")
    spec = importlib.util.spec_from_file_location("c_m16_base_pareto_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the audited actual Pareto fixture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_base_runner()


def _jsonable(value: Any) -> Any:
    return _BASE._jsonable(value)


def _digest(value: Any) -> str:
    return _BASE._digest(value)


def _append_jsonl(path: Path, value: Any) -> None:
    _BASE._append_jsonl(path, value)


def _atomic_json(path: Path, value: Any) -> None:
    _BASE._atomic_json(path, value)


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    return _BASE._read_jsonl(path)


def _set_cpu(cpu: int) -> None:
    _BASE._set_cpu(cpu)


def _resource_snapshot() -> dict[str, Any]:
    return _BASE._resource_snapshot()


def _resource_clean(before: dict[str, Any], after: dict[str, Any], cpu: int) -> bool:
    return _BASE._resource_clean(before, after, cpu)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope_certificate(planner: Any, request: Any, scenario: str) -> Any:
    if scenario == "disabled":
        return None
    scope = planner.temporal_scope(request)
    allowed = ((0, 0), (0, 1), (0, 2), (1, 1), (1, 2))
    if scenario == "scope_mismatch":
        scope = type(scope).from_mapping({**scope.mapping, "scope_revision": "m16-drift"})
    if scenario == "coverage_incomplete":
        return qualify_state_bound(
            scope,
            allowed,
            universe_nodes=((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)),
            exclusion_proof=True,
            proof_digest="m16-incomplete-bound-v1",
            coverage_complete=False,
            evaluator_certified=True,
        )
    return TemporalStateBoundCertificate.certified(
        scope,
        allowed,
        excluded_nodes=((1, 0),),
        proof_digest=("m16-drift-bound-v1" if scenario == "checkpoint_drift" else "m16-bound-v1"),
    )


def _route_payload(result: Any) -> dict[str, Any] | None:
    return _BASE._route_payload(result)


def _worker_record(
    scenario: str,
    objective: str,
    mode: str,
    repetition: int,
    cpu: int,
) -> dict[str, Any]:
    started = __import__("time").perf_counter()
    _set_cpu(cpu)
    planner = _BASE._planner("same_exact_dominance", objective)
    request = _BASE._request(
        objective,
        cancel=(mode == "cancelled" and scenario != "checkpoint_drift"),
    )
    certificate = _scope_certificate(planner, request, scenario)
    before = _resource_snapshot()
    checkpoint_digest = None
    restore_match = None
    result = None
    oracle = None
    error = None
    mismatch_rejected = False
    try:
        if scenario == "checkpoint_drift":
            session = create_non_fifo_temporal_pareto_session(
                planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
            if session.advance(expansion_slice=1) is not None:
                raise RuntimeError("state-bound fixture did not pause before checkpoint")
            checkpoint = session.checkpoint()
            checkpoint_digest = checkpoint.digest
            drifted = TemporalStateBoundCertificate.certified(
                planner.temporal_scope(request),
                ((0, 0), (0, 1), (0, 2), (1, 1)),
                excluded_nodes=((1, 0), (1, 2)),
                proof_digest="m16-restore-drift-v1",
            )
            restore_non_fifo_temporal_pareto_session(
                planner,
                request,
                checkpoint,
                state_bound_certificate=drifted,
            )
            raise AssertionError("checkpoint state-bound drift was accepted")
        if mode == "one_shot":
            result = run_non_fifo_temporal_pareto_search(
                planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
        elif mode == "slice_restore":
            session = create_non_fifo_temporal_pareto_session(
                planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
            first = session.advance(expansion_slice=1)
            if first is not None:
                result = first
            else:
                checkpoint = session.checkpoint()
                checkpoint_digest = checkpoint.digest
                restored = restore_non_fifo_temporal_pareto_session(
                    planner,
                    request,
                    checkpoint,
                    state_bound_certificate=certificate,
                )
                result = restored.run()
                full = run_non_fifo_temporal_pareto_search(
                    _BASE._planner("same_exact_dominance", objective),
                    request,
                    pareto_pruning=True,
                    state_bound_certificate=certificate,
                )
                restore_match = (
                    result.status is full.status
                    and result.semantic_digest == full.semantic_digest
                    and result.frontier_digest == full.frontier_digest
                )
        else:
            result = run_non_fifo_temporal_pareto_search(
                planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
        if result.status is NonFifoSearchStatus.GOAL_FOUND:
            oracle = _BASE._oracle(planner, request)
    except NonFifoTemporalParetoError as exc:
        if scenario != "checkpoint_drift":
            raise
        mismatch_rejected = True
        error = f"{type(exc).__name__}:{exc}"
    except AssertionError as exc:
        error = str(exc)
    except Exception as exc:  # pragma: no cover - worker boundary evidence
        error = f"{type(exc).__name__}:{exc}"

    after = _resource_snapshot()
    diagnostics = _jsonable(result.diagnostics) if result is not None else None
    state_bound_pruned = int((diagnostics or {}).get("state_bound_pruned", 0))
    state_bound_rejected = int((diagnostics or {}).get("state_bound_rejected", 0))
    if scenario == "checkpoint_drift" and mismatch_rejected:
        status = "MISMATCH_REJECTED"
    elif result is not None:
        status = result.status.value
    else:
        status = "WORKER_ERROR"
    expected_status = (
        "MISMATCH_REJECTED"
        if scenario == "checkpoint_drift"
        else (
            NonFifoSearchStatus.CANCELLED.value
            if mode == "cancelled"
            else NonFifoSearchStatus.GOAL_FOUND.value
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario,
        "objective": objective,
        "mode": mode,
        "repetition": repetition,
        "status": status,
        "expected_status": expected_status,
        "state_bound_policy": (
            "disabled" if certificate is None else certificate.status.value.lower()
        ),
        "certificate_status": certificate.status.value if certificate is not None else "DISABLED",
        "certificate_digest": certificate.digest if certificate is not None else None,
        "checkpoint_digest": checkpoint_digest,
        "restore_match": restore_match,
        "mismatch_rejected": mismatch_rejected,
        "selected": _route_payload(result) if result is not None else None,
        "semantic_digest": result.semantic_digest if result is not None else None,
        "frontier_digest": result.frontier_digest if result is not None else None,
        "oracle": oracle,
        "diagnostics": diagnostics,
        "state_bound_checks": int((diagnostics or {}).get("state_bound_checks", 0)),
        "state_bound_pruned": state_bound_pruned,
        "state_bound_rejected": state_bound_rejected,
        "pareto_pruned": result.pareto_pruned if result is not None else 0,
        "evaluator_errors": list(result.evaluator_errors) if result is not None else [],
        "reason": result.reason if result is not None else error,
        "error": error,
        "resource_before": before,
        "resource_after": after,
        "resource_clean": _resource_clean(before, after, cpu),
        "elapsed_ms": (__import__("time").perf_counter() - started) * 1000.0,
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = (
        Path(__file__).relative_to(root),
        Path("scripts/benchmark_non_fifo_temporal_pareto.py"),
        Path("src/arctic_route_planning/planners/non_fifo_temporal_pareto.py"),
        Path("src/arctic_route_planning/planners/temporal_bounds.py"),
        Path("src/arctic_route_planning/planners/temporal_label_astar.py"),
        Path("src/arctic_route_planning/planners/temporal_session.py"),
    )
    return {
        "commit": subprocess.check_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
        ).strip(),
        "dirty": bool(
            subprocess.check_output(
                ("git", "-C", str(root), "status", "--porcelain"), text=True
            ).strip()
        ),
        "files": {str(relative): _sha256(root / relative) for relative in files},
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation": _implementation_identity(root),
        "lock_sha256": _sha256(root / "uv.lock"),
        "config_sha256": _sha256(root / "pyproject.toml"),
        "fixture_digest": _digest(
            {
                "base_schema": _BASE.SCHEMA_VERSION,
                "scenarios": SCENARIOS,
                "objectives": OBJECTIVES,
                "modes": MODES,
                "components": tuple(TemporalParetoComponent),
                "limits": LIMITS,
                "grid": {"rows": 2, "columns": 3, "allow_diagonal": False},
                "risk_frames": 3,
            }
        ),
        "objectives": OBJECTIVES,
        "modes": MODES,
        "scenarios": SCENARIOS,
        "repetitions": args.repetitions,
        "cpu": args.cpu,
        "limits": LIMITS,
        "production_candidate_enabled": False,
        "winter_enabled": False,
        "default_state_bound": "disabled",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--worker-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--scenario", choices=SCENARIOS)
    parser.add_argument("--objective", choices=OBJECTIVES)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--repetition", type=int, default=1)
    return parser


def _record_key(record: dict[str, Any]) -> tuple[str, str, str, int] | None:
    values = (
        record.get("scenario"),
        record.get("objective"),
        record.get("mode"),
        record.get("repetition"),
    )
    if values[0] not in SCENARIOS or values[1] not in OBJECTIVES or values[2] not in MODES:
        return None
    if not isinstance(values[3], int):
        return None
    return values


def _summary(
    records: list[dict[str, Any]], args: argparse.Namespace, malformed: int = 0
) -> dict[str, Any]:
    expected = len(SCENARIOS) * len(OBJECTIVES) * len(MODES) * args.repetitions
    cells: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        key = _record_key(record)
        if key is not None:
            cells[key[:3]].append(record)
    deterministic = True
    deterministic_by_cell: dict[str, bool] = {}
    for key, cell in cells.items():
        signatures = {
            (
                record.get("status"),
                record.get("semantic_digest"),
                record.get("frontier_digest"),
                record.get("state_bound_pruned"),
                record.get("state_bound_rejected"),
                record.get("mismatch_rejected"),
            )
            for record in cell
        }
        value = len(signatures) == 1 and len(cell) == args.repetitions
        deterministic_by_cell["/".join(key)] = value
        deterministic = deterministic and value
    expected_statuses = all(
        record.get("status") == record.get("expected_status") for record in records
    )
    complete = len(records) == expected and malformed == 0
    oracle_match = all(
        record.get("status") != NonFifoSearchStatus.GOAL_FOUND.value
        or (
            record.get("selected") is not None
            and record.get("oracle") is not None
            and tuple(record["selected"]["costs"]) == tuple(record["oracle"]["costs"])
            and record["selected"]["arrival_times"][-1] == record["oracle"]["arrival"]
        )
        for record in records
    )
    certified_pruning = any(
        record.get("scenario") == "certified"
        and record.get("status") == NonFifoSearchStatus.GOAL_FOUND.value
        and int(record.get("state_bound_pruned", 0)) > 0
        for record in records
    )
    rejected_zero = all(
        int(record.get("state_bound_pruned", 0)) == 0
        for record in records
        if record.get("scenario") in {"scope_mismatch", "coverage_incomplete", "disabled"}
    )
    mismatch_safe = all(
        record.get("scenario") != "checkpoint_drift" or record.get("mismatch_rejected") is True
        for record in records
    )
    resources_clean = all(record.get("resource_clean") is True for record in records)
    passed = (
        complete
        and expected_statuses
        and deterministic
        and oracle_match
        and certified_pruning
        and rejected_zero
        and mismatch_safe
        and resources_clean
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "TEMPORAL_NONFIFO_PARETO_STATE_BOUND_MATRIX_PASS"
        if passed
        else "NO_PERFORMANCE_PROOF/FAIL",
        "expected_cases": expected,
        "completed_cases": len(records),
        "malformed_records": malformed,
        "complete": complete,
        "expected_statuses": expected_statuses,
        "deterministic": deterministic,
        "deterministic_by_cell": deterministic_by_cell,
        "oracle_match": oracle_match,
        "certified_pruning_observed": certified_pruning,
        "rejected_scenarios_pruning_zero": rejected_zero,
        "checkpoint_mismatch_fail_closed": mismatch_safe,
        "resources_clean": resources_clean,
        "state_bound_default": "disabled",
        "production_candidate_enabled": False,
        "winter_enabled": False,
    }


class _RunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> _RunnerLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_args: Any) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _worker_command(
    args: argparse.Namespace, scenario: str, objective: str, mode: str, repetition: int
) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--output-dir",
        str(args.output_dir),
        "--cpu",
        str(args.cpu),
        "--scenario",
        scenario,
        "--objective",
        objective,
        "--mode",
        mode,
        "--repetition",
        str(repetition),
    ]


def _run_parent(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with _RunnerLock(output / ".lock"):
        identity = _identity(args, root)
        manifest_path = output / "manifest.json"
        cases_path = output / "cases.jsonl"
        if args.resume and manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("identity") != _jsonable(identity):
                raise RuntimeError("resume identity mismatch")
        elif manifest_path.exists():
            raise RuntimeError("experiment exists; use --resume")
        else:
            _atomic_json(
                manifest_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "RUNNING",
                    "identity": identity,
                    "started_at": datetime.now(UTC),
                    "evidence_files": (
                        "manifest.json",
                        "cases.jsonl",
                        "comparison-summary.json",
                        "heartbeat.json",
                        "ALL_DONE/STOPPED_HARD",
                    ),
                },
            )
        records, malformed = _read_jsonl(cases_path)
        completed = {_record_key(record) for record in records}
        total = len(SCENARIOS) * len(OBJECTIVES) * len(MODES) * args.repetitions
        for scenario in SCENARIOS:
            for objective in OBJECTIVES:
                for mode in MODES:
                    for repetition in range(1, args.repetitions + 1):
                        key = (scenario, objective, mode, repetition)
                        if key in completed:
                            continue
                        _atomic_json(
                            output / "heartbeat.json",
                            {
                                "schema_version": SCHEMA_VERSION,
                                "updated_at": datetime.now(UTC),
                                "completed_cases": len(records),
                                "expected_cases": total,
                                "current": key,
                            },
                        )
                        try:
                            completed_process = subprocess.run(
                                _worker_command(args, scenario, objective, mode, repetition),
                                check=False,
                                capture_output=True,
                                text=True,
                                timeout=args.worker_timeout_seconds,
                            )
                            if completed_process.returncode == 0:
                                record = json.loads(completed_process.stdout)
                            else:
                                record = {
                                    "schema_version": SCHEMA_VERSION,
                                    "scenario": scenario,
                                    "objective": objective,
                                    "mode": mode,
                                    "repetition": repetition,
                                    "status": "WORKER_ERROR",
                                    "expected_status": "unknown",
                                    "error": completed_process.stderr[-2000:],
                                    "resource_clean": False,
                                }
                        except subprocess.TimeoutExpired:
                            record = {
                                "schema_version": SCHEMA_VERSION,
                                "scenario": scenario,
                                "objective": objective,
                                "mode": mode,
                                "repetition": repetition,
                                "status": "TIMEOUT",
                                "expected_status": "unknown",
                                "error": "worker_timeout",
                                "resource_clean": False,
                            }
                        _append_jsonl(cases_path, record)
                        records.append(record)
        summary = _summary(records, args, malformed)
        _atomic_json(output / "comparison-summary.json", summary)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(
            {
                "status": summary["status"],
                "completed_at": datetime.now(UTC),
                "completed_cases": len(records),
            }
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            output / "heartbeat.json",
            {
                "schema_version": SCHEMA_VERSION,
                "updated_at": datetime.now(UTC),
                "completed_cases": len(records),
                "expected_cases": total,
                "status": summary["status"],
            },
        )
        marker = output / (
            "ALL_DONE" if summary["status"].endswith("MATRIX_PASS") else "STOPPED_HARD"
        )
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        return 0 if summary["status"].endswith("MATRIX_PASS") else 2


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if args.scenario is None or args.objective is None or args.mode is None:
            raise SystemExit("worker requires scenario, objective and mode")
        print(
            json.dumps(
                _jsonable(
                    _worker_record(
                        args.scenario, args.objective, args.mode, args.repetition, args.cpu
                    )
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
