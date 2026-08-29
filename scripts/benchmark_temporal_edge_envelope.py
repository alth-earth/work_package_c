#!/usr/bin/env python3
"""Synthetic oracle matrix for proof-carrying edge-envelope pre-gating.

The graph is finite and intentionally contains two exact-arrival labels for
one node.  The slower label is safely rejected before its final edge
evaluator, while the faster label remains and reaches the goal.  Rejected or
scope-mismatched certificates must never prune.  This runner is a C-internal
diagnostic and does not enable temporal dominance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from heapq import heappop, heappush
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-edge-envelope.v1"
PROFILES = {"small": (5, 7, 7), "medium": (9, 13, 13), "stress": (13, 19, 19)}
OBJECTIVES = ("fastest", "low_risk", "recommended")
CERTIFICATE_KINDS = ("certified", "coverage_incomplete", "scope_mismatch")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HORIZON_HOURS = 2.0
NODES = ((0, 0), (0, 1), (1, 1), (1, 2))
EDGES = {
    (0, 0): (((1, 1), 1.5), ((0, 1), 0.5)),
    (0, 1): (((1, 1), 0.5),),
    (1, 1): (((1, 2), 1.0),),
    (1, 2): (),
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_edge_envelope.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
)


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    return value


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
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
        handle.write(json.dumps(_jsonable(value), sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), sort_keys=True))
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


def _scope(profile: str, objective: str) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "certified:synthetic-edge-v1",
            "bound_evaluator_digest": "certified:synthetic-bound-v1",
            "fixture_digest": _digest((profile, objective, "edge-envelope-v1")),
            "objective": objective,
            "profile": profile,
            "search_limits": {
                "max_expansions": 50_000,
                "max_labels": 100_000,
                "max_queue": 50_000,
                "max_edge_evaluations": 400_000,
            },
        }
    )


def _bounds() -> tuple[dict[Any, float], dict[Any, float], dict[tuple[Any, Any], float]]:
    forward = {NODES[0]: 0.0, NODES[1]: 0.5, NODES[2]: 1.0, NODES[3]: 2.0}
    reverse = {NODES[0]: 2.0, NODES[1]: 1.5, NODES[2]: 1.0, NODES[3]: 0.0}
    edges = {
        (start, end): hours for start, neighbours in EDGES.items() for end, hours in neighbours
    }
    return forward, reverse, edges


def _search(certificate: Any | None) -> dict[str, Any]:
    queue: list[tuple[float, int, tuple[int, int], datetime, tuple[tuple[int, int], ...]]] = [
        (0.0, 0, NODES[0], T0, (NODES[0],))
    ]
    serial = 0
    labels: dict[tuple[tuple[int, int], datetime], float] = {(NODES[0], T0): 0.0}
    edge_evaluations = 0
    edge_pruned = 0
    while queue:
        cost, _order, node, arrival, path = heappop(queue)
        if cost != labels.get((node, arrival)):
            continue
        if node == NODES[-1]:
            return {
                "nodes": [list(item) for item in path],
                "arrival_times": [
                    (T0 + timedelta(hours=0)).isoformat(),
                    (T0 + timedelta(hours=0.5)).isoformat(),
                    (T0 + timedelta(hours=1)).isoformat(),
                    arrival.isoformat(),
                ],
                "total_cost_hours": cost,
                "edge_evaluations": edge_evaluations,
                "edge_pruned": edge_pruned,
            }
        for neighbour, hours in EDGES[node]:
            if certificate is not None and not certificate.allows_transition(
                node, neighbour, arrival, T0
            ):
                edge_pruned += 1
                continue
            edge_evaluations += 1
            next_arrival = arrival + timedelta(hours=hours)
            if next_arrival - T0 > timedelta(hours=HORIZON_HOURS):
                continue
            next_cost = cost + hours
            key = (neighbour, next_arrival)
            if next_cost >= labels.get(key, float("inf")):
                continue
            labels[key] = next_cost
            serial += 1
            heappush(queue, (next_cost, serial, neighbour, next_arrival, (*path, neighbour)))
    raise RuntimeError("finite oracle found no route")


def _oracle() -> dict[str, Any]:
    # Exhaustive simple-path enumeration is independent of the heap search.
    routes: list[dict[str, Any]] = []

    def visit(node: Any, arrival: datetime, path: tuple[Any, ...], cost: float) -> None:
        if node == NODES[-1]:
            routes.append(
                {
                    "nodes": [list(item) for item in path],
                    "arrival": arrival.isoformat(),
                    "total_cost_hours": cost,
                }
            )
            return
        for neighbour, hours in EDGES[node]:
            if neighbour in path:
                continue
            next_arrival = arrival + timedelta(hours=hours)
            if next_arrival - T0 <= timedelta(hours=HORIZON_HOURS):
                visit(neighbour, next_arrival, (*path, neighbour), cost + hours)

    visit(NODES[0], T0, (NODES[0],), 0.0)
    return min(routes, key=lambda item: (item["total_cost_hours"], item["nodes"]))


def _case(profile: str, objective: str, kind: str) -> dict[str, Any]:
    scope = _scope(profile, objective)
    forward, reverse, edge_map = _bounds()
    evidence_scope = scope
    edge_input: dict[tuple[Any, Any], float] = edge_map
    expected_scope = scope
    if kind == "coverage_incomplete":
        edge_input = dict(edge_map)
        edge_input.pop((NODES[2], NODES[3]))
    elif kind == "scope_mismatch":
        expected_scope = TemporalScope.from_mapping({**scope.mapping, "profile": "mismatch"})
    evidence = AdmissibleBoundEvidence(
        scope=evidence_scope,
        method="independent-synthetic-edge-bound-v1",
        evaluator_digest="certified:synthetic-bound-v1",
        proof_digest=_digest((profile, objective, kind, edge_input)),
        admissible=True,
        coverage_complete=True,
    )
    derived = derive_temporal_corridor(
        scope=scope,
        expected_scope=expected_scope,
        universe_nodes=NODES,
        start=NODES[0],
        goal=NODES[-1],
        neighbors=lambda node: tuple(neighbour for neighbour, _hours in EDGES[node]),
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=HORIZON_HOURS,
        objective=objective,
        bound_evidence=evidence,
        include_arrival_upper_bounds=True,
        edge_lower_hours=edge_input,
        edge_bound_complete=True,
    )
    baseline = _search(None)
    candidate = _search(derived.certificate if derived.certificate.usable else None)
    oracle = _oracle()
    certified = kind == "certified"
    route_match = (
        candidate["nodes"] == oracle["nodes"]
        and baseline["nodes"] == oracle["nodes"]
        and candidate["total_cost_hours"] == oracle["total_cost_hours"]
    )
    pruning_ok = candidate["edge_pruned"] > 0 if certified else candidate["edge_pruned"] == 0
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "shape": PROFILES[profile],
        "objective": objective,
        "certificate_kind": kind,
        "certificate_usable": derived.certificate.usable,
        "certificate_digest": derived.certificate.digest,
        "edge_bound_digest": derived.certificate.edge_bound_digest,
        "certificate_reason": derived.reason,
        "route_match": route_match,
        "semantic_match": route_match,
        "baseline": baseline,
        "candidate": candidate,
        "oracle": oracle,
        "actual_edge_pruning": candidate["edge_pruned"],
        "pruning_expectation_met": pruning_ok,
        "deterministic_digest": _digest((derived.certificate.digest, candidate)),
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "certificate_kinds": CERTIFICATE_KINDS,
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
        "git": _git_identity(root),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "fixture_digest": _digest({"profiles": profiles, "objectives": OBJECTIVES, "edges": EDGES}),
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
        "dominance_policy": "disabled",
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    identity = _identity(args, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("edge-envelope proof requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if recorded.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "dominance_policy": "disabled",
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    existing: dict[tuple[str, str, str], dict[str, Any]] = {}
    cases_path = output / "cases.jsonl"
    if args.resume:
        for item in _read_jsonl(cases_path):
            key = (
                str(item.get("profile")),
                str(item.get("objective")),
                str(item.get("certificate_kind")),
            )
            existing[key] = item
    cases: list[dict[str, Any]] = []
    for profile in profiles:
        for objective in OBJECTIVES:
            for kind in CERTIFICATE_KINDS:
                key = (profile, objective, kind)
                case = existing.get(key) or _case(profile, objective, kind)
                if key not in existing:
                    _append_jsonl(cases_path, case)
                cases.append(case)
                _atomic_json(
                    heartbeat,
                    {
                        "status": "RUNNING",
                        "updated_at": datetime.now(UTC),
                        "completed_cases": len(cases),
                    },
                )
    certified = [case for case in cases if case["certificate_kind"] == "certified"]
    rejected = [case for case in cases if case["certificate_kind"] != "certified"]
    passed = (
        len(cases) == len(profiles) * len(OBJECTIVES) * len(CERTIFICATE_KINDS)
        and all(case["semantic_match"] and case["pruning_expectation_met"] for case in cases)
        and all(
            case["certificate_usable"] and case["actual_edge_pruning"] > 0 for case in certified
        )
        and all(
            not case["certificate_usable"] and case["actual_edge_pruning"] == 0 for case in rejected
        )
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "TEMPORAL_EDGE_ENVELOPE_MATRIX_PASS"
        if passed
        else "TEMPORAL_EDGE_ENVELOPE_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": len(profiles) * len(OBJECTIVES) * len(CERTIFICATE_KINDS),
        "certified_case_count": len(certified),
        "rejected_case_count": len(rejected),
        "observed_edge_pruning": sum(case["actual_edge_pruning"] for case in certified),
        "rejected_edge_pruning": sum(case["actual_edge_pruning"] for case in rejected),
        "semantic_match": all(case["semantic_match"] for case in cases),
        "fail_closed": all(
            not case["certificate_usable"] and case["actual_edge_pruning"] == 0 for case in rejected
        ),
        "dominance_policy": "disabled",
        "production_candidate_enabled": False,
    }
    _write_jsonl(output / "resource-frontier.jsonl", cases)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
        },
    )
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
