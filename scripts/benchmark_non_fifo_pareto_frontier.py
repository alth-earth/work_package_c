#!/usr/bin/env python3
"""Research-only finite non-FIFO Pareto-frontier evidence runner.

The runner is intentionally independent from the production planner.  It
exercises the finite ``search_non_fifo_pareto`` sidecar against adversarial
fixtures and an exhaustive reference implementation, while preserving the
same fail-closed status and frozen resource-bound semantics used by the C
research work.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import resource
import signal
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from arctic_route_planning.planners.non_fifo_feasibility import (
    NonFifoBusinessEvidence,
    NonFifoParetoTransition,
    NonFifoSearchStatus,
    certify_non_fifo_pareto_frontier,
    create_non_fifo_pareto_session,
)

SCHEMA_VERSION = "c.p0.2-nonfifo-pareto-frontier.v1"
OBJECTIVES = ("fastest", "low_risk", "recommended")
POLICIES = ("baseline", "pareto")
FIXTURES = (
    "later_arrival_shortcut",
    "same_bucket_exact_eta",
    "strict_same_exact_dominance",
    "equal_cost_same_exact",
    "periodic_cycle",
    "hard_mask",
    "evaluator_failure",
    "cancelled",
    "maximum_horizon",
    "edge_limit",
    "non_increasing_arrival",
    "objective_dimension_mismatch",
)
TERMINAL_STATUSES = {status.value for status in NonFifoSearchStatus}
DEFAULT_LIMITS = {
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
    start: str = "start"
    goal: str = "goal"
    objective_count: int = 3
    expected_status: str = NonFifoSearchStatus.GOAL_FOUND.value
    expected_pruning: int | None = 0
    maximum_elapsed: timedelta | None = None
    limits: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))


@dataclass(frozen=True, slots=True)
class _OracleLabel:
    node: str
    arrival_time: datetime
    costs: tuple[float, ...]
    path: tuple[str, ...]
    transitions: tuple[NonFifoParetoTransition, ...]

    @property
    def exact_key(self) -> tuple[str, datetime]:
        return self.node, self.arrival_time


def _business(edge: str) -> NonFifoBusinessEvidence:
    return NonFifoBusinessEvidence(
        speed_knots=8.0 + len(edge),
        risk_score=0.1 * (len(edge) % 4),
        maximum_risk=0.2,
        confidence=0.9,
        source_ids=(f"fixture:{edge}",),
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
    if name == "later_arrival_shortcut":
        graph = {"start": ("early", "late"), "early": ("goal",), "late": ("goal",), "goal": ()}

        def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            if (start, end) == ("start", "early"):
                return _transition(T0 + timedelta(hours=1), (1.0, 1.0, 1.0), "start-early")
            if (start, end) == ("start", "late"):
                return _transition(T0 + timedelta(hours=2), (2.0, 2.0, 2.0), "start-late")
            if (start, end) == ("early", "goal"):
                return _transition(arrival + timedelta(hours=5), (5.0, 5.0, 5.0), "early-goal")
            return _transition(arrival + timedelta(hours=0.1), (0.1, 0.1, 0.1), "late-goal")

        return _Fixture(name, graph, evaluate)

    if name == "same_bucket_exact_eta":
        graph = {"start": ("early", "late"), "early": ("join",), "late": ("join",), "join": ()}

        def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            if (start, end) == ("start", "early"):
                return _transition(T0 + timedelta(minutes=15), (0.0, 1.0, 0.0), "start-early")
            if (start, end) == ("start", "late"):
                return _transition(T0 + timedelta(minutes=45), (1.0, 0.0, 1.0), "start-late")
            if (start, end) == ("early", "join"):
                return _transition(T0 + timedelta(hours=1.1), (1.0, 0.0, 1.0), "early-join")
            return _transition(T0 + timedelta(hours=1.9), (0.0, 1.0, 0.0), "late-join")

        return _Fixture(name, graph, evaluate, goal="join")

    if name in {"strict_same_exact_dominance", "equal_cost_same_exact"}:
        graph = {"start": ("left", "right"), "left": ("join",), "right": ("join",), "join": ()}
        costs = (1.0, 1.0, 1.0) if name == "equal_cost_same_exact" else None

        def evaluate(start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            if start == "start":
                return _transition(arrival + timedelta(minutes=30), (0.0, 0.0, 0.0), f"start-{end}")
            edge_costs = costs or ((1.0, 1.0, 1.0) if start == "left" else (2.0, 2.0, 2.0))
            return _transition(T0 + timedelta(hours=1), edge_costs, f"{start}-{end}")

        return _Fixture(
            name,
            graph,
            evaluate,
            goal="join",
            expected_pruning=0 if costs else None,
        )

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
            expected_pruning=0,
            limits={**DEFAULT_LIMITS, "max_labels": 8},
        )

    if name == "hard_mask":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return NonFifoParetoTransition(
                arrival + timedelta(hours=1),
                (1.0, 1.0, 1.0),
                business=NonFifoBusinessEvidence(hard_mask=True),
            )

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EVALUATOR_FAILURE.value,
        )

    if name == "evaluator_failure":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, _arrival: datetime) -> NonFifoParetoTransition:
            raise RuntimeError("fixture evaluator failure")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EVALUATOR_FAILURE.value,
        )

    if name == "cancelled":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=1), (1.0, 1.0, 1.0), "cancelled")

        return _Fixture(name, graph, evaluate, expected_status=NonFifoSearchStatus.CANCELLED.value)

    if name == "maximum_horizon":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=3), (1.0, 1.0, 1.0), "horizon")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EXHAUSTED.value,
            maximum_elapsed=timedelta(hours=1),
        )

    if name == "edge_limit":
        graph = {"start": ("a", "b"), "a": (), "b": (), "goal": ()}

        def evaluate(_start: str, end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival + timedelta(hours=1), (1.0, 1.0, 1.0), f"edge-{end}")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.RESOURCE_LIMIT.value,
            limits={**DEFAULT_LIMITS, "max_edge_evaluations": 1},
        )

    if name == "non_increasing_arrival":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> NonFifoParetoTransition:
            return _transition(arrival, (1.0, 1.0, 1.0), "non-increasing")

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EVALUATOR_FAILURE.value,
        )

    if name == "objective_dimension_mismatch":
        graph = {"start": ("goal",), "goal": ()}

        def evaluate(_start: str, _end: str, arrival: datetime) -> Any:
            return NonFifoParetoTransition(arrival + timedelta(hours=1), (1.0, 1.0), payload={})

        return _Fixture(
            name,
            graph,
            evaluate,
            expected_status=NonFifoSearchStatus.EVALUATOR_FAILURE.value,
        )

    raise ValueError(f"unknown fixture: {name}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, NonFifoBusinessEvidence):
        return {
            "speed_knots": value.speed_knots,
            "risk_score": value.risk_score,
            "maximum_risk": value.maximum_risk,
            "confidence": value.confidence,
            "source_ids": value.source_ids,
            "hard_mask": value.hard_mask,
        }
    if isinstance(value, NonFifoParetoTransition):
        return {
            "arrival_time": _jsonable(value.arrival_time),
            "costs": value.costs,
            "payload": _jsonable(value.payload),
            "business": _jsonable(value.business),
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _digest(value: Any) -> str:
    encoded = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _label_payload(label: Any) -> dict[str, Any]:
    return {
        "node": _jsonable(label.node),
        "arrival_time": _jsonable(label.arrival_time),
        "costs": _jsonable(label.costs),
        "path": _jsonable(label.path),
        "transitions": _jsonable(label.transitions),
    }


def _frontier(labels: Iterable[_OracleLabel]) -> tuple[_OracleLabel, ...]:
    values = tuple(labels)
    return tuple(
        label
        for label in values
        if not any(other is not label and _dominates(other, label) for other in values)
    )


def _dominates(left: _OracleLabel, right: _OracleLabel) -> bool:
    if left.exact_key != right.exact_key or len(left.costs) != len(right.costs):
        return False
    return all(a <= b for a, b in zip(left.costs, right.costs, strict=True)) and any(
        a < b for a, b in zip(left.costs, right.costs, strict=True)
    )


def _oracle(fixture: _Fixture) -> tuple[dict[str, Any], ...]:
    """Exhaustively enumerate the finite successful fixture independently."""

    queue = [
        _OracleLabel(
            fixture.start,
            T0,
            (0.0,) * fixture.objective_count,
            (fixture.start,),
            (),
        )
    ]
    goals: list[_OracleLabel] = []
    while queue:
        label = queue.pop(0)
        if label.node == fixture.goal:
            goals.append(label)
            continue
        for neighbour in sorted(fixture.graph.get(label.node, ()), key=str):
            transition = fixture.evaluate(label.node, neighbour, label.arrival_time)
            if transition.business is not None and transition.business.hard_mask:
                continue
            if transition.arrival_time <= label.arrival_time:
                continue
            if fixture.maximum_elapsed is not None and (
                transition.arrival_time - T0 > fixture.maximum_elapsed
            ):
                continue
            queue.append(
                _OracleLabel(
                    neighbour,
                    transition.arrival_time,
                    tuple(a + b for a, b in zip(label.costs, transition.costs, strict=True)),
                    (*label.path, neighbour),
                    (*label.transitions, transition),
                )
            )
            if len(queue) > 10_000:
                raise RuntimeError("oracle fixture is not finite")
    selected = (
        min(goals, key=lambda item: (item.costs, item.arrival_time, item.path))
        if goals
        else None
    )
    frontier = sorted(
        _frontier(goals), key=lambda item: (item.costs, item.arrival_time, item.path)
    )
    return ({
        "selected": _label_payload(selected) if selected is not None else None,
        "frontier": [_label_payload(label) for label in frontier],
        "frontier_digest": _digest([_label_payload(label) for label in frontier]),
    },)


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


def _worker_record(
    fixture_name: str,
    objective: str,
    policy: str,
    repetition: int,
    cpu: int,
) -> dict[str, Any]:
    fixture = _fixture(fixture_name)
    if cpu >= 0:
        if not hasattr(os, "sched_setaffinity"):
            raise RuntimeError("fixed CPU evidence is unavailable")
        os.sched_setaffinity(0, {cpu})
    started = perf_counter()
    cancel_check = (lambda: True) if fixture.name == "cancelled" else None
    session = create_non_fifo_pareto_session(
        start=fixture.start,
        goal=fixture.goal,
        departure_time=T0,
        neighbors=lambda node: fixture.graph.get(node, ()),
        evaluate_edge=fixture.evaluate,
        objective_count=fixture.objective_count,
        pareto_pruning=policy == "pareto",
        cancel_check=cancel_check,
        maximum_elapsed=fixture.maximum_elapsed,
        **fixture.limits,
    )
    result = session.run()
    certificate = certify_non_fifo_pareto_frontier(
        result,
        identity=session.identity,
        scope_digest=f"fixture:{fixture_name}:{objective}",
    )
    elapsed_ms = (perf_counter() - started) * 1000.0
    oracle = (
        _oracle(fixture)
        if fixture.expected_status == NonFifoSearchStatus.GOAL_FOUND.value
        else ({},)
    )
    oracle_value = oracle[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "fixture": fixture_name,
        "objective": objective,
        "policy": policy,
        "repetition": repetition,
        "expected_status": fixture.expected_status,
        "status": result.status.value,
        "label": _label_payload(result.label) if result.label is not None else None,
        "semantic_digest": result.semantic_digest,
        "frontier": [_label_payload(label) for label in result.goal_frontier],
        "frontier_digest": result.frontier_digest,
        "frontier_certificate": {
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
        },
        "oracle": oracle_value,
        "pareto_pruned": result.pareto_pruned,
        "expanded": result.expanded,
        "generated": result.generated,
        "queue_peak": result.queue_peak,
        "edge_evaluations": result.edge_evaluations,
        "evaluator_errors": result.evaluator_errors,
        "reason": result.reason,
        "resource": _resource_snapshot(),
        "elapsed_ms": elapsed_ms,
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = (
        Path(__file__).relative_to(root),
        Path("src/arctic_route_planning/planners/non_fifo_feasibility.py"),
    )
    values: dict[str, str] = {}
    for relative in files:
        values[str(relative)] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    commit = subprocess.check_output(
        ("git", "-C", str(root), "rev-parse", "HEAD"), text=True
    ).strip()
    return {"commit": commit, "files": values}


def _fixture_identity() -> str:
    """Digest fixture topology and expected fail-closed outcomes."""

    descriptors = []
    for name in FIXTURES:
        fixture = _fixture(name)
        descriptors.append(
            {
                "name": fixture.name,
                "graph": fixture.graph,
                "start": fixture.start,
                "goal": fixture.goal,
                "objective_count": fixture.objective_count,
                "expected_status": fixture.expected_status,
                "expected_pruning": fixture.expected_pruning,
                "maximum_elapsed": fixture.maximum_elapsed,
                "limits": fixture.limits,
            }
        )
    return _digest(descriptors)


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
    parser.add_argument("--fixture", choices=FIXTURES, help=argparse.SUPPRESS)
    parser.add_argument("--objective", choices=OBJECTIVES, help=argparse.SUPPRESS)
    parser.add_argument("--policy", choices=POLICIES, help=argparse.SUPPRESS)
    parser.add_argument("--repetition", type=int, help=argparse.SUPPRESS)
    return parser


def _case_key(record: Mapping[str, Any]) -> tuple[str, str, str, int]:
    return (
        str(record["fixture"]),
        str(record["objective"]),
        str(record["policy"]),
        int(record["repetition"]),
    )


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    file_digests = _implementation_identity(root)
    lock_path = root / "uv.lock"
    project_path = root / "pyproject.toml"
    return {
        "schema_version": SCHEMA_VERSION,
        "implementation": file_digests,
        "lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest()
        if lock_path.exists()
        else None,
        "config_sha256": hashlib.sha256(project_path.read_bytes()).hexdigest()
        if project_path.exists()
        else None,
        "fixture_digest": _fixture_identity(),
        "policy_digest": _digest(
            {
                "pareto_pruning": "same_exact_new_label_strict_componentwise_only",
                "different_exact_arrival": "retain",
                "equal_cost": "retain",
                "expanded_label": "retain",
                "failure": "no_partial_route",
            }
        ),
        "objectives": list(OBJECTIVES),
        "policies": list(POLICIES),
        "fixtures": list(FIXTURES),
        "repetitions": args.repetitions,
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "search_limits": DEFAULT_LIMITS,
        "production_candidate_enabled": False,
    }


def _read_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
    return values


def _summary(cases: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    expected = len(FIXTURES) * len(OBJECTIVES) * len(POLICIES) * args.repetitions
    complete = len(cases) == expected and all(
        record.get("status") in TERMINAL_STATUSES for record in cases
    )
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for case in cases:
        groups.setdefault((case["fixture"], case["objective"], case["policy"]), []).append(case)
    deterministic = True
    semantic = True
    fail_closed = True
    policy_bound = True
    expected_statuses = True
    worker_errors = False
    resource_evidence_complete = True
    resource_clean = True
    observed_pruning = 0
    certificate_complete = True
    for (fixture_name, _objective, policy), values in groups.items():
        fingerprints = {
            _digest(
                {
                    "status": value.get("status"),
                    "reason": value.get("reason"),
                    "semantic_digest": value.get("semantic_digest"),
                    "label": value.get("label"),
                    "frontier": value.get("frontier"),
                    "frontier_digest": value.get("frontier_digest"),
                    "pareto_pruned": value.get("pareto_pruned"),
                    "expanded": value.get("expanded"),
                    "generated": value.get("generated"),
                    "queue_peak": value.get("queue_peak"),
                    "edge_evaluations": value.get("edge_evaluations"),
                }
            )
            for value in values
        }
        deterministic &= len(fingerprints) == 1
        for value in values:
            worker_errors |= bool(value.get("worker_error"))
            resource = value.get("resource")
            required_resource_keys = {"cpu_affinity", "max_rss_kib", "process_swap_kib"}
            if (
                not isinstance(resource, Mapping)
                or not required_resource_keys <= resource.keys()
            ):
                resource_evidence_complete = False
                resource_clean = False
            else:
                resource_clean &= int(resource.get("process_swap_kib") or 0) == 0
            expected_statuses &= value.get("status") == value.get("expected_status")
            certificate = value.get("frontier_certificate")
            if not isinstance(certificate, Mapping):
                certificate_complete = False
            elif value.get("status") == NonFifoSearchStatus.GOAL_FOUND.value:
                certificate_complete &= certificate.get("usable") is True
                certificate_complete &= certificate.get("complete") is True
                certificate_complete &= int(certificate.get("frontier_count") or 0) == len(
                    value.get("frontier") or []
                )
            else:
                certificate_complete &= certificate.get("usable") is False
            if value.get("status") == NonFifoSearchStatus.GOAL_FOUND.value:
                oracle = value.get("oracle") or {}
                semantic &= value.get("frontier") == oracle.get("frontier")
                semantic &= value.get("label") == oracle.get("selected")
            else:
                fail_closed &= value.get("label") is None and value.get("semantic_digest") is None
            if fixture_name == "strict_same_exact_dominance" and policy == "pareto":
                observed_pruning += int(value.get("pareto_pruned") or 0)
    for fixture_name in FIXTURES:
        for objective in OBJECTIVES:
            baseline = next(
                (
                    value
                    for value in groups.get((fixture_name, objective, "baseline"), ())
                    if value["status"] == "GOAL_FOUND"
                ),
                None,
            )
            candidate = next(
                (
                    value
                    for value in groups.get((fixture_name, objective, "pareto"), ())
                    if value["status"] == "GOAL_FOUND"
                ),
                None,
            )
            if baseline is not None and candidate is not None:
                policy_bound &= baseline["frontier"] == candidate["frontier"]
                policy_bound &= (
                    baseline["frontier_certificate"].get("policy_digest")
                    != candidate["frontier_certificate"].get("policy_digest")
                )
    status = (
        "TEMPORAL_NONFIFO_PARETO_FRONTIER_MATRIX_PASS"
        if (
            complete
            and deterministic
            and semantic
            and fail_closed
            and policy_bound
            and expected_statuses
            and certificate_complete
            and not worker_errors
            and resource_evidence_complete
            and resource_clean
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
        "semantic_match": semantic,
        "fail_closed": fail_closed,
        "policy_digest_bound": policy_bound,
        "expected_statuses": expected_statuses,
        "frontier_certificate_complete": certificate_complete,
        "worker_errors": worker_errors,
        "resource_evidence_complete": resource_evidence_complete,
        "resource_clean": resource_clean,
        "observed_strict_same_exact_pruning": observed_pruning,
        "production_candidate_enabled": False,
        "dominance_policy": "disabled",
        "pareto_policy": "explicit_research_only",
        "limits": DEFAULT_LIMITS,
    }


def _run_parent(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0:
        raise SystemExit("repetitions and worker timeout must be positive")
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock_path = output / ".runner.lock"
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = _identity(args, root)
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("identity") != identity:
                raise RuntimeError("another experiment identity already owns this output")
        else:
            _atomic_json(
                manifest_path,
                {"schema_version": SCHEMA_VERSION, "identity": identity, "status": "RUNNING"},
            )
        cases_path = output / "cases.jsonl"
        existing = _read_cases(cases_path) if args.resume else []
        completed = {_case_key(record) for record in existing}
        expected_keys = [
            (fixture, objective, policy, repetition)
            for fixture in FIXTURES
            for objective in OBJECTIVES
            for policy in POLICIES
            for repetition in range(1, args.repetitions + 1)
        ]
        total = len(expected_keys)
        _atomic_json(
            output / "heartbeat.json",
            {
                "status": "RUNNING",
                "completed_cases": len(existing),
                "expected_cases": total,
            },
        )
        env = os.environ.copy()
        source = str(root / "src")
        prior_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = source + (os.pathsep + prior_pythonpath if prior_pythonpath else "")
        stopped_hard = False

        def stop_handler(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt

        previous_sigint = signal.signal(signal.SIGINT, stop_handler)
        previous_sigterm = signal.signal(signal.SIGTERM, stop_handler)
        try:
            for key in expected_keys:
                if key in completed:
                    continue
                fixture, objective, policy, repetition = key
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--fixture",
                    fixture,
                    "--objective",
                    objective,
                    "--policy",
                    policy,
                    "--repetition",
                    str(repetition),
                    "--cpu",
                    str(args.cpu),
                    "--output-dir",
                    str(output),
                ]
                try:
                    completed_process = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=args.worker_timeout_seconds,
                        check=False,
                        env=env,
                    )
                    if completed_process.returncode != 0:
                        raise RuntimeError(completed_process.stderr.strip() or "worker failed")
                    record = json.loads(completed_process.stdout)
                except subprocess.TimeoutExpired:
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "fixture": fixture,
                        "objective": objective,
                        "policy": policy,
                        "repetition": repetition,
                        "expected_status": _fixture(fixture).expected_status,
                        "status": "RESOURCE_LIMIT",
                        "label": None,
                        "semantic_digest": None,
                        "frontier": [],
                        "frontier_digest": None,
                        "oracle": {},
                        "pareto_pruned": 0,
                        "reason": "worker_timeout",
                        "worker_error": True,
                    }
                except Exception as error:
                    record = {
                        "schema_version": SCHEMA_VERSION,
                        "fixture": fixture,
                        "objective": objective,
                        "policy": policy,
                        "repetition": repetition,
                        "expected_status": _fixture(fixture).expected_status,
                        "status": "EVALUATOR_FAILURE",
                        "label": None,
                        "semantic_digest": None,
                        "frontier": [],
                        "frontier_digest": None,
                        "oracle": {},
                        "pareto_pruned": 0,
                        "reason": f"worker_error:{type(error).__name__}:{error}",
                        "worker_error": True,
                    }
                _append_jsonl(cases_path, record)
                existing.append(record)
                completed.add(key)
                _atomic_json(
                    output / "heartbeat.json",
                    {
                        "status": "RUNNING",
                        "completed_cases": len(existing),
                        "expected_cases": total,
                    },
                )
        except KeyboardInterrupt:
            stopped_hard = True
            _atomic_json(
                output / "heartbeat.json",
                {
                    "status": "STOPPED_HARD",
                    "completed_cases": len(existing),
                    "expected_cases": total,
                },
            )
            (output / "STOPPED_HARD").write_text(
                "interrupted\n", encoding="utf-8"
            )
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)
        if stopped_hard:
            return 2
        summary = _summary(existing, args)
        _atomic_json(output / "comparison-summary.json", summary)
        _atomic_json(
            output / "heartbeat.json",
            {
                "status": "COMPLETED",
                "completed_cases": len(existing),
                "expected_cases": total,
            },
        )
        _atomic_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "identity": identity,
                "status": summary["status"],
            },
        )
        marker = output / "ALL_DONE"
        marker.write_text(summary["status"] + "\n", encoding="utf-8")
        return 0 if summary["status"] == "TEMPORAL_NONFIFO_PARETO_FRONTIER_MATRIX_PASS" else 2


def main() -> int:
    args = _parser().parse_args()
    if args.worker:
        if not all((args.fixture, args.objective, args.policy, args.repetition is not None)):
            raise SystemExit("worker requires fixture, objective, policy and repetition")
        print(
            json.dumps(
                _worker_record(
                    args.fixture,
                    args.objective,
                    args.policy,
                    args.repetition,
                    args.cpu,
                ),
                default=_jsonable,
            )
        )
        return 0
    return _run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
