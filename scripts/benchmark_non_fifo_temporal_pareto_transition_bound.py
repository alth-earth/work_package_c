#!/usr/bin/env python3
"""Synthetic proof matrix for the actual Pareto transition pre-gate.

The runner exercises the explicit transition-level state-bound check added to
the C-internal actual-edge Pareto bridge.  It compares a no-bound baseline to
an edge-bound candidate and keeps the finite graph/oracle independent of the
candidate.  It is diagnostic evidence only: production planning,
``TemporalDominancePolicy`` and all public contracts remain unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.non_fifo_feasibility import NonFifoSearchStatus
from arctic_route_planning.planners.non_fifo_temporal_pareto import (
    NonFifoTemporalParetoError,
    create_non_fifo_temporal_pareto_session,
    restore_non_fifo_temporal_pareto_session,
    run_non_fifo_temporal_pareto_search,
)
from arctic_route_planning.planners.temporal_bounds import (
    TemporalStateBoundCertificate,
    qualify_state_bound,
)

SCHEMA_VERSION = "c.p0.2-temporal-pareto-transition-bound.v1"
OBJECTIVES = ("fastest", "low_risk", "recommended")
MODES = ("one_shot", "slice_restore", "cancelled")
SCENARIOS = (
    "certified_partial",
    "scope_mismatch",
    "coverage_incomplete",
    "disabled",
    "checkpoint_drift",
)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}


def _load_base() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_non_fifo_temporal_pareto.py")
    spec = importlib.util.spec_from_file_location("c_m34_pareto_transition_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load the audited finite Pareto fixture runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_BASE = _load_base()


def _jsonable(value: Any) -> Any:
    return _BASE._jsonable(value)


def _digest(value: Any) -> str:
    return _BASE._digest(value)


def _atomic_json(path: Path, value: Any) -> None:
    _BASE._atomic_json(path, value)


def _append_jsonl(path: Path, value: Any) -> None:
    _BASE._append_jsonl(path, value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _set_cpu(cpu: int) -> None:
    if cpu < 0:
        return
    if not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("fixed CPU evidence is unavailable")
    os.sched_setaffinity(0, {cpu})


def _certificate(planner: Any, request: Any, scenario: str) -> Any:
    if scenario == "disabled":
        return None
    scope = planner.temporal_scope(request)
    nodes = tuple((row, column) for row in range(2) for column in range(3))
    if scenario == "scope_mismatch":
        scope = type(scope).from_mapping({**scope.mapping, "scope_revision": "m34-drift"})
    if scenario == "coverage_incomplete":
        return qualify_state_bound(
            scope,
            nodes,
            universe_nodes=nodes,
            exclusion_proof=True,
            proof_digest="m34-incomplete-transition-bound-v1",
            coverage_complete=False,
            evaluator_certified=True,
            arrival_upper_hours=tuple((node, 6.0) for node in nodes),
            edge_lower_hours=(((1, 0), (1, 1), 100.0),),
            edge_bound_partial=True,
        )
    return TemporalStateBoundCertificate.certified(
        scope,
        nodes,
        proof_digest="m34-transition-bound-v1",
        arrival_upper_hours=tuple((node, 6.0) for node in nodes),
        # Only this directed edge is certified impossible.  Every omitted
        # pair remains live under the partial-coverage rule.
        edge_lower_hours=(((1, 0), (1, 1), 100.0),),
        edge_bound_partial=True,
    )


def _diagnostics(result: Any) -> dict[str, Any]:
    return _jsonable(result.diagnostics) if result is not None else {}


def _case(scenario: str, objective: str, mode: str, repetition: int) -> dict[str, Any]:
    request = _BASE._request(objective, cancel=mode == "cancelled")
    baseline_planner = _BASE._planner("same_exact_dominance", objective)
    candidate_planner = _BASE._planner("same_exact_dominance", objective)
    certificate = _certificate(candidate_planner, request, scenario)
    baseline = None
    candidate = None
    restore_error = None
    checkpoint_digest = None
    try:
        baseline = run_non_fifo_temporal_pareto_search(
            baseline_planner,
            request,
            pareto_pruning=True,
        )
        if scenario == "checkpoint_drift":
            session = create_non_fifo_temporal_pareto_session(
                candidate_planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
            while session.context.diagnostics.state_bound_edge_pruned == 0:
                paused = session.advance(expansion_slice=1)
                if paused is not None:
                    raise RuntimeError("transition-bound fixture did not pause before drift")
            checkpoint = session.checkpoint()
            checkpoint_digest = checkpoint.digest
            drifted = TemporalStateBoundCertificate.certified(
                candidate_planner.temporal_scope(request),
                tuple((row, column) for row in range(2) for column in range(3)),
                proof_digest="m34-transition-bound-drift-v1",
                arrival_upper_hours=tuple(
                    ((row, column), 6.0) for row in range(2) for column in range(3)
                ),
                edge_lower_hours=(((1, 0), (1, 1), 99.0),),
                edge_bound_partial=True,
            )
            try:
                restore_non_fifo_temporal_pareto_session(
                    candidate_planner,
                    request,
                    checkpoint,
                    state_bound_certificate=drifted,
                )
            except NonFifoTemporalParetoError as error:
                restore_error = f"{type(error).__name__}: {error}"
            else:
                raise AssertionError("checkpoint transition-bound drift was accepted")
        elif mode == "one_shot" or mode == "cancelled":
            candidate = run_non_fifo_temporal_pareto_search(
                candidate_planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
        else:
            session = create_non_fifo_temporal_pareto_session(
                candidate_planner,
                request,
                pareto_pruning=True,
                state_bound_certificate=certificate,
            )
            first = session.advance(expansion_slice=1)
            if first is not None:
                candidate = first
            else:
                checkpoint = session.checkpoint()
                checkpoint_digest = checkpoint.digest
                restored = restore_non_fifo_temporal_pareto_session(
                    candidate_planner,
                    request,
                    checkpoint,
                    state_bound_certificate=certificate,
                )
                candidate = restored.run()
    except Exception as error:  # pragma: no cover - evidence boundary
        return {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario,
            "objective": objective,
            "mode": mode,
            "repetition": repetition,
            "status": "ERROR",
            "error": f"{type(error).__name__}: {error}",
            "semantic_match": False,
            "fail_closed": False,
        }

    baseline_diag = _diagnostics(baseline)
    candidate_diag = _diagnostics(candidate)
    edge_pruned = int(candidate_diag.get("state_bound_edge_pruned", 0) or 0)
    edge_checks = int(candidate_diag.get("state_bound_edge_checks", 0) or 0)
    state_pruned = int(candidate_diag.get("state_bound_pruned", 0) or 0)
    mismatch_expected = scenario == "checkpoint_drift"
    if mismatch_expected:
        semantic_match = baseline is not None and restore_error is not None
        status = "MISMATCH_REJECTED" if semantic_match else "FAIL"
    else:
        semantic_match = bool(
            baseline is not None
            and candidate is not None
            and baseline.status is candidate.status
            and baseline.semantic_digest == candidate.semantic_digest
        )
        status = candidate.status.value if candidate is not None else "FAIL"
    should_prune = scenario == "certified_partial" and mode != "cancelled"
    fail_closed = edge_pruned > 0 if should_prune else edge_pruned == 0
    if mismatch_expected:
        fail_closed = restore_error is not None
    oracle = None
    if candidate is not None and candidate.status is NonFifoSearchStatus.GOAL_FOUND:
        oracle = _BASE._oracle(_BASE._planner("same_exact_dominance", objective), request)
    if oracle is None or candidate is None:
        oracle_match = True
    elif candidate.selected is None:
        oracle_match = False
    else:
        route = candidate.selected
        oracle_nodes = (request.start, *(step["end"] for step in oracle["steps"]))
        oracle_arrivals = (request.departure_time, *(step["eta"] for step in oracle["steps"]))
        oracle_match = bool(
            route.nodes == oracle_nodes
            and route.arrival_times == oracle_arrivals
            and route.costs == oracle["costs"]
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "scenario": scenario,
        "objective": objective,
        "mode": mode,
        "repetition": repetition,
        "status": status,
        "expected_status": "MISMATCH_REJECTED"
        if mismatch_expected
        else ("CANCELLED" if mode == "cancelled" else "GOAL_FOUND"),
        "certificate_status": certificate.status.value if certificate is not None else "DISABLED",
        "certificate_digest": certificate.digest if certificate is not None else None,
        "checkpoint_digest": checkpoint_digest,
        "restore_error": restore_error,
        "baseline_status": None if baseline is None else baseline.status.value,
        "candidate_status": None if candidate is None else candidate.status.value,
        "baseline_semantic_digest": None if baseline is None else baseline.semantic_digest,
        "candidate_semantic_digest": None if candidate is None else candidate.semantic_digest,
        "baseline_diagnostics": baseline_diag,
        "candidate_diagnostics": candidate_diag,
        "state_bound_edge_checks": edge_checks,
        "state_bound_edge_pruned": edge_pruned,
        "state_bound_pruned": state_pruned,
        "pareto_pruned": 0 if candidate is None else candidate.pareto_pruned,
        "semantic_match": semantic_match,
        "oracle_match": oracle_match,
        "fail_closed": fail_closed,
        "production_candidate_enabled": False,
        "dominance_policy": "disabled",
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    files = {
        relative: _sha256(root / relative)
        for relative in (
            "scripts/benchmark_non_fifo_temporal_pareto_transition_bound.py",
            "src/arctic_route_planning/planners/non_fifo_temporal_pareto.py",
            "src/arctic_route_planning/planners/temporal_bounds.py",
        )
    }
    identity = {
        "schema_version": SCHEMA_VERSION,
        "implementation": files,
        "implementation_sha256": _digest(files),
        "git": {
            "commit": subprocess.check_output(
                ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
            ).strip(),
            "branch": subprocess.check_output(
                ("git", "-C", str(root), "branch", "--show-current"), text=True
            ).strip(),
            "dirty": bool(
                subprocess.check_output(
                    ("git", "-C", str(root), "status", "--porcelain"), text=True
                ).strip()
            ),
        },
        "objectives": OBJECTIVES,
        "modes": MODES,
        "scenarios": SCENARIOS,
        "repetitions": args.repetitions,
        "limits": LIMITS,
        "dominance_policy": "disabled",
        "production_candidate_enabled": False,
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1:
        raise SystemExit("repetitions must be positive")
    root = Path(__file__).resolve().parents[1]
    identity = _identity(args, root)
    if identity["git"]["dirty"]:
        raise RuntimeError("transition-bound matrix requires a clean implementation worktree")
    _set_cpu(args.cpu)
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cases_path = output / "cases.jsonl"
    cases: list[dict[str, Any]] = []
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
        },
    )
    _atomic_json(output / "heartbeat.json", {"status": "RUNNING"})
    for repetition in range(1, args.repetitions + 1):
        for scenario in SCENARIOS:
            for objective in OBJECTIVES:
                for mode in MODES:
                    record = _case(scenario, objective, mode, repetition)
                    record["experiment_id"] = identity["experiment_id"]
                    cases.append(record)
                    _append_jsonl(cases_path, record)
                    _atomic_json(
                        output / "heartbeat.json",
                        {
                            "status": "RUNNING",
                            "completed_cases": len(cases),
                            "expected_cases": args.repetitions
                            * len(SCENARIOS)
                            * len(OBJECTIVES)
                            * len(MODES),
                        },
                    )
    expected = args.repetitions * len(SCENARIOS) * len(OBJECTIVES) * len(MODES)
    certified = [case for case in cases if case["scenario"] == "certified_partial"]
    rejected = [case for case in cases if case["scenario"] != "certified_partial"]
    passed = bool(cases) and len(cases) == expected and all(
        case["semantic_match"] and case["oracle_match"] and case["fail_closed"]
        for case in cases
    )
    passed = passed and all(
        case["state_bound_edge_pruned"] > 0
        for case in certified
        if case["mode"] != "cancelled"
    )
    passed = passed and all(case["state_bound_edge_pruned"] == 0 for case in rejected)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "TEMPORAL_PARETO_TRANSITION_BOUND_MATRIX_PASS"
        if passed
        else "TEMPORAL_PARETO_TRANSITION_BOUND_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected,
        "certified_cases": len(certified),
        "observed_transition_pruning": sum(
            case["state_bound_edge_pruned"] for case in certified
        ),
        "rejected_transition_pruning": sum(
            case["state_bound_edge_pruned"] for case in rejected
        ),
        "semantic_match": all(case["semantic_match"] for case in cases),
        "oracle_match": all(case["oracle_match"] for case in cases),
        "fail_closed": all(case["fail_closed"] for case in cases),
        "dominance_policy": "disabled",
        "production_candidate_enabled": False,
    }
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
        },
    )
    _atomic_json(output / "heartbeat.json", {"status": summary["status"]})
    (output / "resource-frontier.jsonl").write_text(
        "".join(json.dumps(_jsonable(case), sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
