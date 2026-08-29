#!/usr/bin/env python3
"""Research-only evidence runner for resumable finite Pareto sessions."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import resource
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoBusinessEvidence,
    NonFifoParetoSessionIdentityMismatch,
    NonFifoParetoSessionRestoreError,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    create_non_fifo_pareto_session,
    restore_non_fifo_pareto_session,
    search_non_fifo_pareto,
)

SCHEMA_VERSION = "c.p0.2-nonfifo-pareto-session.v1"
OBJECTIVES = ("fastest", "low_risk", "recommended")
MODES = ("one_shot", "slice_only", "slice_restore")
SCENARIOS = (
    "frontier",
    "later_arrival",
    "same_exact_dominance",
    "periodic_cycle",
    "evaluator_failure",
    "resource_limit",
    "cancelled",
    "callback_drift",
    "policy_drift",
    "checkpoint_tamper",
)
LIMITS = {
    "max_expansions": 50_000,
    "max_labels": 100_000,
    "max_queue": 50_000,
    "max_edge_evaluations": 400_000,
}
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class _Fixture:
    name: str
    graph: Mapping[str, tuple[str, ...]]
    evaluate: Callable[[str, str, datetime], NonFifoParetoTransition]
    goal: str = "goal"
    expected_status: str = NonFifoSearchStatus.GOAL_FOUND.value
    maximum_elapsed: timedelta | None = None
    limits: Mapping[str, int] = field(default_factory=lambda: dict(LIMITS))


def _business(edge: str) -> NonFifoBusinessEvidence:
    return NonFifoBusinessEvidence(
        speed_knots=8.0 + len(edge),
        risk_score=0.1 * (len(edge) % 4),
        maximum_risk=0.2,
        confidence=0.9,
        source_ids=(f"session-fixture:{edge}",),
    )


def _transition(
    arrival_time: datetime,
    costs: tuple[float, float, float],
    edge: str,
) -> NonFifoParetoTransition:
    return NonFifoParetoTransition(
        arrival_time,
        costs,
        payload={"edge": edge},
        business=_business(edge),
    )


def _fixture(name: str) -> _Fixture:
    if name == "frontier":
        graph = {"start": ("left", "right"), "left": ("goal",), "right": ("goal",), "goal": ()}

        def evaluate(start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            if start == "start":
                return _transition(arrival + timedelta(hours=1), (0.0, 0.0, 0.0), "start")
            costs = (1.0, 4.0, 2.0) if start == "left" else (4.0, 1.0, 3.0)
            return _transition(T0 + timedelta(hours=2), costs, start)

        return _Fixture(name, graph, evaluate)

    if name == "later_arrival":
        graph = {"start": ("early", "late"), "early": ("goal",), "late": ("goal",), "goal": ()}

        def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            if start == "start" and end == "early":
                return _transition(T0 + timedelta(hours=1), (1.0, 1.0, 1.0), "early")
            if start == "start" and end == "late":
                return _transition(T0 + timedelta(hours=2), (2.0, 2.0, 2.0), "late")
            if start == "early":
                return _transition(T0 + timedelta(hours=6), (5.0, 5.0, 5.0), "early-goal")
            return _transition(T0 + timedelta(hours=2, minutes=6), (0.1, 0.1, 0.1), "late-goal")

        return _Fixture(name, graph, evaluate)

    if name == "same_exact_dominance":
        graph = {"start": ("left", "right"), "left": ("goal",), "right": ("goal",), "goal": ()}

        def evaluate(start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            if start == "start":
                return _transition(arrival + timedelta(minutes=30), (0.0, 0.0, 0.0), start)
            costs = (1.0, 1.0, 1.0) if start == "left" else (2.0, 2.0, 2.0)
            return _transition(T0 + timedelta(hours=1), costs, start)

        return _Fixture(name, graph, evaluate)

    if name == "periodic_cycle":
        graph = {"start": ("cycle",), "cycle": ("cycle", "goal"), "goal": ()}

        def evaluate(_start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(
                arrival + timedelta(hours=1),
                (0.0, 0.0, 0.0) if end == "cycle" else (10.0, 10.0, 10.0),
                f"cycle-{end}",
            )

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.RESOURCE_LIMIT.value,
            limits={**LIMITS, "max_labels": 8},
        )

    if name == "evaluator_failure":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, _arrival: datetime) -> NonFifoParetoTransition:
            raise RuntimeError("session evaluator failure")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EVALUATOR_FAILURE.value,
        )

    if name == "resource_limit":
        graph = {"start": ("a", "b"), "a": (), "b": (), "goal": ()}

        def evaluate(_start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=1), (1.0, 1.0, 1.0), end)

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.RESOURCE_LIMIT.value,
            limits={**LIMITS, "max_edge_evaluations": 1},
        )

    if name == "cancelled":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=1), (1.0, 1.0, 1.0), "cancelled")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.CANCELLED.value,
        )

    if name in {"callback_drift", "policy_drift", "checkpoint_tamper"}:
        graph = {"start": ("middle",), "middle": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=1), (1.0, 1.0, 1.0), "identity")

        return _Fixture(name, graph, evaluate)

    raise ValueError(f"unknown session fixture: {name}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, NonFifoParetoTransition):
        return {
            "arrival_time": _jsonable(value.arrival_time),
            "costs": value.costs,
            "payload": _jsonable(value.payload),
            "business": _jsonable(value.business),
        }
    if isinstance(value, NonFifoBusinessEvidence):
        return {
            "speed_knots": value.speed_knots,
            "risk_score": value.risk_score,
            "maximum_risk": value.maximum_risk,
            "confidence": value.confidence,
            "source_ids": value.source_ids,
            "hard_mask": value.hard_mask,
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _label_payload(label: Any) -> dict[str, Any]:
    return {
        "node": _jsonable(label.node),
        "arrival_time": _jsonable(label.arrival_time),
        "costs": label.costs,
        "path": _jsonable(label.path),
        "transitions": _jsonable(label.transitions),
    }


def _dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["arrival_time"] != right["arrival_time"]:
        return False
    costs_left = left["costs"]
    costs_right = right["costs"]
    return all(a <= b for a, b in zip(costs_left, costs_right, strict=True)) and any(
        a < b for a, b in zip(costs_left, costs_right, strict=True)
    )


def _oracle(fixture: _Fixture) -> dict[str, Any]:
    """Exhaustively enumerate the small fixture independently of the session."""

    labels = [("start", T0, (0.0, 0.0, 0.0), ("start",), ())]
    goals: list[dict[str, Any]] = []
    while labels:
        node, arrival, costs, path, transitions = labels.pop(0)
        if node == fixture.goal:
            goals.append(
                {
                    "node": node,
                    "arrival_time": _jsonable(arrival),
                    "costs": costs,
                    "path": path,
                    "transitions": transitions,
                }
            )
            continue
        for neighbour in sorted(fixture.graph.get(node, ()), key=str):
            transition = fixture.evaluate(node, neighbour, arrival)
            if transition.arrival_time <= arrival:
                continue
            if fixture.maximum_elapsed is not None and (
                transition.arrival_time - T0 > fixture.maximum_elapsed
            ):
                continue
            labels.append(
                (
                    neighbour,
                    transition.arrival_time,
                    tuple(a + b for a, b in zip(costs, transition.costs, strict=True)),
                    (*path, neighbour),
                    (*transitions, transition),
                )
            )
            if len(labels) > 10_000:
                raise RuntimeError("oracle fixture is not finite")
    frontier = tuple(
        sorted(
            (
                goal
                for goal in goals
                if not any(other is not goal and _dominates(other, goal) for other in goals)
            ),
            key=lambda item: (item["costs"], item["arrival_time"], item["path"]),
        )
    )
    selected = (
        min(goals, key=lambda item: (item["costs"], item["arrival_time"], item["path"]))
        if goals
        else None
    )
    return {
        "selected": selected,
        "frontier": list(frontier),
        "frontier_digest": _digest(frontier),
    }


def _resource_snapshot() -> dict[str, Any]:
    swap_kib = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmSwap:"):
                swap_kib = int(line.split()[1])
                break
    except (OSError, ValueError):
        pass
    return {
        "cpu_affinity": (
            sorted(os.sched_getaffinity(0))
            if hasattr(os, "sched_getaffinity")
            else None
        ),
        "max_rss_kib": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
        "process_swap_kib": swap_kib,
    }


def _base_kwargs(fixture: _Fixture, neighbours: Callable[[str], Iterable[str]]) -> dict[str, Any]:
    return {
        "start": "start",
        "goal": fixture.goal,
        "departure_time": T0,
        "neighbors": neighbours,
        "evaluate_edge": fixture.evaluate,
        "objective_count": 3,
        "pareto_pruning": True,
        "fixture_digest": f"m12:{fixture.name}",
        "maximum_elapsed": fixture.maximum_elapsed,
        **fixture.limits,
    }


def _run_session_mode(fixture: _Fixture, mode: str) -> dict[str, Any]:
    graph = fixture.graph

    def neighbours(node: str) -> tuple[str, ...]:
        return graph.get(node, ())

    kwargs = _base_kwargs(fixture, neighbours)
    started = perf_counter()
    checkpoint_digest = None
    pause_count = 0
    restore_match = None
    mismatch_rejected = False
    error = None
    if fixture.name == "cancelled":
        kwargs["cancel_check"] = lambda: True
    try:
        if fixture.name in {"callback_drift", "policy_drift", "checkpoint_tamper"}:
            session = create_non_fifo_pareto_session(**kwargs)
            checkpoint = session.checkpoint()
            checkpoint_digest = checkpoint.digest
            if fixture.name == "callback_drift":
                def changed_evaluate(
                    _start: str, _end: str, arrival: datetime
                ) -> NonFifoParetoTransition:
                    return _transition(arrival + timedelta(hours=1), (2.0, 2.0, 2.0), "drift")

                restore_non_fifo_pareto_session(
                    checkpoint,
                    neighbors=neighbours,
                    evaluate_edge=changed_evaluate,
                )
            elif fixture.name == "policy_drift":
                drifted = replace(checkpoint.identity, pareto_pruning=False)
                restore_non_fifo_pareto_session(
                    checkpoint,
                    neighbors=neighbours,
                    evaluate_edge=fixture.evaluate,
                    identity=drifted,
                )
            else:
                replace(checkpoint, expanded=checkpoint.expanded + 1)
            raise AssertionError("identity mismatch was not rejected")
        if mode == "one_shot":
            result = search_non_fifo_pareto(**kwargs)
        else:
            session = create_non_fifo_pareto_session(**kwargs)
            result = None
            while result is None:
                result = session.advance(expansion_slice=1)
                if result is None:
                    pause_count += 1
            if mode == "slice_restore" and pause_count > 0:
                session = create_non_fifo_pareto_session(**kwargs)
                assert session.advance(expansion_slice=1) is None
                pause_count = 1
                checkpoint = session.checkpoint()
                checkpoint_digest = checkpoint.digest
                restored = restore_non_fifo_pareto_session(
                    checkpoint,
                    neighbors=neighbours,
                    evaluate_edge=fixture.evaluate,
                )
                restored_result = restored.run()
                restore_match = (
                    restored_result.semantic_digest == result.semantic_digest
                    and restored_result.frontier_digest == result.frontier_digest
                    and restored_result.goal_frontier == result.goal_frontier
                    and restored_result.expanded == result.expanded
                    and restored_result.generated == result.generated
                    and restored_result.pareto_pruned == result.pareto_pruned
                )
                result = restored_result
        oracle = (
            _oracle(fixture)
            if fixture.expected_status == NonFifoSearchStatus.GOAL_FOUND.value
            else {}
        )
        record = {
            "status": result.status.value,
            "label": _label_payload(result.label) if result.label is not None else None,
            "frontier": [_label_payload(label) for label in result.goal_frontier],
            "frontier_digest": result.frontier_digest,
            "semantic_digest": result.semantic_digest,
            "oracle": oracle,
            "pareto_pruned": result.pareto_pruned,
            "expanded": result.expanded,
            "generated": result.generated,
            "queue_peak": result.queue_peak,
            "edge_evaluations": result.edge_evaluations,
            "reason": result.reason,
            "evaluator_errors": result.evaluator_errors,
        }
    except (NonFifoParetoSessionIdentityMismatch, NonFifoParetoSessionRestoreError) as exc:
        mismatch_rejected = True
        record = {
            "status": "MISMATCH_REJECTED",
            "label": None,
            "frontier": [],
            "frontier_digest": None,
            "semantic_digest": None,
            "oracle": {},
            "pareto_pruned": 0,
            "expanded": 0,
            "generated": 0,
            "queue_peak": 0,
            "edge_evaluations": 0,
            "reason": type(exc).__name__,
            "evaluator_errors": (),
        }
    except AssertionError as exc:
        error = str(exc)
        record = {
            "status": "UNEXPECTED_SUCCESS",
            "label": None,
            "frontier": [],
            "frontier_digest": None,
            "semantic_digest": None,
            "oracle": {},
            "pareto_pruned": 0,
            "expanded": 0,
            "generated": 0,
            "queue_peak": 0,
            "edge_evaluations": 0,
            "reason": error,
            "evaluator_errors": (),
        }
    record.update(
        {
            "checkpoint_digest": checkpoint_digest,
            "pause_count": pause_count,
            "restore_match": restore_match,
            "mismatch_rejected": mismatch_rejected,
            "error": error,
            "elapsed_ms": (perf_counter() - started) * 1000.0,
            "resource": _resource_snapshot(),
        }
    )
    return record


def _worker_record(
    scenario: str,
    objective: str,
    mode: str,
    repetition: int,
    cpu: int,
) -> dict[str, Any]:
    if cpu >= 0:
        if not hasattr(os, "sched_setaffinity"):
            raise RuntimeError("fixed CPU evidence is unavailable")
        os.sched_setaffinity(0, {cpu})
    fixture = _fixture(scenario)
    result = _run_session_mode(fixture, mode)
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario,
            "objective": objective,
            "mode": mode,
            "repetition": repetition,
            "expected_status": fixture.expected_status,
        }
    )
    return result


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = (
        Path(__file__).relative_to(root),
        Path("src/arctic_route_planning/planners/non_fifo_feasibility.py"),
    )
    return {
        "commit": subprocess.check_output(
            ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
        ).strip(),
        "files": {
            str(relative): hashlib.sha256((root / relative).read_bytes()).hexdigest()
            for relative in files
        },
    }


def _fixture_digest() -> str:
    return _digest(
        {
            name: {
                "graph": _fixture(name).graph,
                "expected_status": _fixture(name).expected_status,
                "limits": _fixture(name).limits,
            }
            for name in SCENARIOS
        }
    )


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation": _implementation_identity(root),
        "lock_sha256": hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256((root / "pyproject.toml").read_bytes()).hexdigest(),
        "fixture_digest": _fixture_digest(),
        "objectives": list(OBJECTIVES),
        "modes": list(MODES),
        "scenarios": list(SCENARIOS),
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "limits": LIMITS,
        "production_candidate_enabled": False,
    }


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(_jsonable(value), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--worker-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--cpu", type=int, default=-1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--scenario", choices=SCENARIOS, help=argparse.SUPPRESS)
    parser.add_argument("--objective", choices=OBJECTIVES, help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=MODES, help=argparse.SUPPRESS)
    parser.add_argument("--repetition", type=int, help=argparse.SUPPRESS)
    return parser


def _case_key(record: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(record["scenario"]),
        str(record["objective"]),
        str(record["mode"]),
        int(record["repetition"]),
    )


def _summary(cases: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    expected = len(SCENARIOS) * len(OBJECTIVES) * len(MODES) * args.repetitions
    complete = len(cases) == expected
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for case in cases:
        groups.setdefault((case["scenario"], case["objective"], case["mode"]), []).append(case)
    deterministic = True
    resource_complete = True
    resource_clean = True
    expected_statuses = True
    fail_closed = True
    semantic = True
    restore_equivalent = True
    mismatch_safe = True
    checkpoint_deterministic = True
    observed_pruning = 0
    for (scenario, _objective, _mode), values in groups.items():
        fingerprints = {
            _digest(
                {
                    "status": value.get("status"),
                    "label": value.get("label"),
                    "frontier": value.get("frontier"),
                    "semantic_digest": value.get("semantic_digest"),
                    "frontier_digest": value.get("frontier_digest"),
                    "checkpoint_digest": value.get("checkpoint_digest"),
                    "restore_match": value.get("restore_match"),
                    "mismatch_rejected": value.get("mismatch_rejected"),
                    "pareto_pruned": value.get("pareto_pruned"),
                    "expanded": value.get("expanded"),
                    "generated": value.get("generated"),
                    "queue_peak": value.get("queue_peak"),
                    "edge_evaluations": value.get("edge_evaluations"),
                    "reason": value.get("reason"),
                }
            )
            for value in values
        }
        deterministic &= len(fingerprints) == 1
        for value in values:
            resource = value.get("resource")
            required = {"cpu_affinity", "max_rss_kib", "process_swap_kib"}
            if not isinstance(resource, Mapping) or not required <= resource.keys():
                resource_complete = False
                resource_clean = False
            else:
                resource_clean &= int(resource.get("process_swap_kib") or 0) == 0
            fixture = _fixture(scenario)
            if scenario not in {"callback_drift", "policy_drift", "checkpoint_tamper"}:
                expected_statuses &= value.get("status") == fixture.expected_status
            else:
                mismatch_safe &= value.get("status") == "MISMATCH_REJECTED"
                mismatch_safe &= bool(value.get("mismatch_rejected"))
            if value.get("status") == NonFifoSearchStatus.GOAL_FOUND.value:
                oracle = value.get("oracle") or {}
                semantic &= value.get("label") == oracle.get("selected")
                semantic &= value.get("frontier") == oracle.get("frontier")
            else:
                fail_closed &= value.get("label") is None
                fail_closed &= value.get("frontier") == []
                fail_closed &= value.get("semantic_digest") is None
            if scenario == "same_exact_dominance":
                observed_pruning += int(value.get("pareto_pruned") or 0)
            if value.get("checkpoint_digest") is not None:
                checkpoint_deterministic &= bool(value.get("checkpoint_digest"))
    for scenario in ("frontier", "later_arrival", "same_exact_dominance"):
        for objective in OBJECTIVES:
            one = groups.get((scenario, objective, "one_shot"), [])
            for mode in ("slice_only", "slice_restore"):
                target = groups.get((scenario, objective, mode), [])
                if len(one) != args.repetitions or len(target) != args.repetitions:
                    restore_equivalent = False
                    continue
                one_by_rep = {int(value["repetition"]): value for value in one}
                target_by_rep = {int(value["repetition"]): value for value in target}
                for repetition in range(1, args.repetitions + 1):
                    left = one_by_rep.get(repetition, {})
                    right = target_by_rep.get(repetition, {})
                    restore_equivalent &= left.get("status") == right.get("status")
                    restore_equivalent &= (
                        left.get("semantic_digest") == right.get("semantic_digest")
                    )
                    restore_equivalent &= left.get("frontier") == right.get("frontier")
                    if mode == "slice_restore":
                        restore_equivalent &= right.get("restore_match") is True
    status = (
        "TEMPORAL_NONFIFO_PARETO_SESSION_MATRIX_PASS"
        if (
            complete
            and deterministic
            and resource_complete
            and resource_clean
            and expected_statuses
            and fail_closed
            and semantic
            and restore_equivalent
            and mismatch_safe
            and checkpoint_deterministic
            and observed_pruning > 0
        )
        else "NO_PERFORMANCE_PROOF/FAIL"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "case_count": len(cases),
        "expected_case_count": expected,
        "deterministic": deterministic,
        "resource_evidence_complete": resource_complete,
        "resource_clean": resource_clean,
        "expected_statuses": expected_statuses,
        "fail_closed": fail_closed,
        "semantic_match": semantic,
        "one_shot_slice_equivalent": restore_equivalent,
        "mismatch_fail_closed": mismatch_safe,
        "checkpoint_deterministic": checkpoint_deterministic,
        "observed_same_exact_pruning": observed_pruning,
        "production_candidate_enabled": False,
        "limits": LIMITS,
    }


def _run_parent(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and timeout must be positive")
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = _identity(args, root)
    manifest_path = output / "manifest.json"
    lock_path = output / ".runner.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("identity") != identity:
                raise RuntimeError("another experiment identity owns this output")
        else:
            _atomic_json(
                manifest_path,
                {"schema_version": SCHEMA_VERSION, "identity": identity, "status": "RUNNING"},
            )
        cases_path = output / "cases.jsonl"
        existing = []
        if args.resume:
            existing = [
                json.loads(line)
                for line in cases_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ] if cases_path.exists() else []
        completed = {_case_key(case) for case in existing}
        expected_keys = [
            (scenario, objective, mode, repetition)
            for scenario in SCENARIOS
            for objective in OBJECTIVES
            for mode in MODES
            for repetition in range(1, args.repetitions + 1)
        ]
        total = len(expected_keys)
        _atomic_json(
            output / "heartbeat.json",
            {"status": "RUNNING", "completed_cases": len(existing), "expected_cases": total},
        )
        environment = os.environ.copy()
        source = str(root / "src")
        prior_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = source + (
            os.pathsep + prior_pythonpath if prior_pythonpath else ""
        )
        for key in expected_keys:
            if key in completed:
                continue
            scenario, objective, mode, repetition = key
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--scenario",
                scenario,
                "--objective",
                objective,
                "--mode",
                mode,
                "--repetition",
                str(repetition),
                "--cpu",
                str(args.cpu),
                "--output-dir",
                str(output),
            ]
            try:
                process = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=args.worker_timeout_seconds,
                    check=False,
                    env=environment,
                )
                if process.returncode != 0:
                    raise RuntimeError(process.stderr.strip() or "worker failed")
                record = json.loads(process.stdout)
            except subprocess.TimeoutExpired:
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "scenario": scenario,
                    "objective": objective,
                    "mode": mode,
                    "repetition": repetition,
                    "expected_status": _fixture(scenario).expected_status,
                    "status": "TIMEOUT",
                    "label": None,
                    "frontier": [],
                    "semantic_digest": None,
                    "frontier_digest": None,
                    "reason": "worker_timeout",
                    "worker_error": True,
                    "resource": None,
                }
            except Exception as exc:
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "scenario": scenario,
                    "objective": objective,
                    "mode": mode,
                    "repetition": repetition,
                    "expected_status": _fixture(scenario).expected_status,
                    "status": "WORKER_ERROR",
                    "label": None,
                    "frontier": [],
                    "semantic_digest": None,
                    "frontier_digest": None,
                    "reason": f"{type(exc).__name__}:{exc}",
                    "worker_error": True,
                    "resource": None,
                }
            _append_jsonl(cases_path, record)
            existing.append(record)
            completed.add(key)
            _atomic_json(
                output / "heartbeat.json",
                {"status": "RUNNING", "completed_cases": len(existing), "expected_cases": total},
            )
        summary = _summary(existing, args)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            output / "heartbeat.json",
            {"status": "COMPLETED", "completed_cases": len(existing), "expected_cases": total},
        )
        _atomic_json(
            manifest_path,
            {"schema_version": SCHEMA_VERSION, "identity": identity, "status": summary["status"]},
        )
        (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
        return 0 if summary["status"] == "TEMPORAL_NONFIFO_PARETO_SESSION_MATRIX_PASS" else 2


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if not all((args.scenario, args.objective, args.mode, args.repetition is not None)):
            raise SystemExit("worker requires scenario, objective, mode and repetition")
        print(
            json.dumps(
                _worker_record(args.scenario, args.objective, args.mode, args.repetition, args.cpu),
                default=_jsonable,
            )
        )
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
