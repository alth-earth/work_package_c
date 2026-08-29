#!/usr/bin/env python3
"""Synthetic oracle matrix for the heading-expanded objective lower bound.

The runner is deliberately independent of the production route evaluator. It
searches a finite regular-grid heading graph twice: Dijkstra is the correctness
oracle and the candidate uses only the certified heading lower bound to order
the same states. No labels are pruned by this sidecar. Rejected certificates
must fall back to the oracle ordering and remain visibly rejected.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime
from heapq import heappop, heappush
from pathlib import Path
from typing import Any

from arctic_route_planning.cost import CostModel
from arctic_route_planning.domain.models import CostWeights
from arctic_route_planning.grid import Node, RegularGrid, heading_change_degrees
from arctic_route_planning.planners.temporal_heading_heuristic import (
    TemporalHeadingHeuristicCertificate,
    qualify_heading_heuristic,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-heading-heuristic.v1"
PROFILES = {"small": (5, 7, 7), "medium": (9, 13, 13), "stress": (13, 19, 19)}
OBJECTIVES = ("fastest", "low_risk", "recommended")
CERTIFICATE_KINDS = ("certified", "incomplete", "scope_mismatch", "non_admissible")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


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
        json.dumps(
            _jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
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
        handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": bool(run("status", "--porcelain")),
    }


def _nodes(grid: RegularGrid) -> tuple[Node, ...]:
    return tuple((row, column) for row in range(grid.shape[0]) for column in range(grid.shape[1]))


def _model(objective: str) -> CostModel:
    weights = {
        "fastest": CostWeights(1.0, 0.20, 0.05, 0.05, 0.20),
        "low_risk": CostWeights(0.45, 2.50, 0.05, 0.08, 0.80),
        "recommended": CostWeights(0.85, 1.30, 0.08, 0.10, 0.45),
    }[objective]
    return CostModel(
        weights=weights, maximum_speed_km_per_hour=22.224, full_turn_penalty_hours=0.25
    )


def _scope(profile: str, objective: str, grid: RegularGrid) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "certified:synthetic-heading-edge-v1",
            "fixture_digest": _digest((profile, objective, grid.shape, "heading-v1")),
            "objective": objective,
            "profile": profile,
            "search_limits": {"max_expansions": 50_000, "max_labels": 100_000, "max_queue": 50_000},
        }
    )


def _edge_cost(
    grid: RegularGrid, state: tuple[Node, tuple[int, int] | None], neighbour: Node, model: CostModel
) -> float:
    node, incoming = state
    distance = grid.distance_km(node, neighbour)
    travel = distance / model.maximum_speed_km_per_hour
    previous_heading = None
    if incoming is not None:
        previous = (node[0] - incoming[0], node[1] - incoming[1])
        if grid.contains(previous):
            previous_heading = grid.heading_degrees(previous, node)
    turn = heading_change_degrees(previous_heading, grid.heading_degrees(node, neighbour))
    base = (model.weights.travel_time + model.weights.distance) * travel
    turn_cost = model.weights.turn * turn / 180.0 * model.full_turn_penalty_hours
    # Add a deterministic non-negative environmental term. This keeps the
    # exact edge cost above the certificate's lower bound without changing the
    # admissible proof.
    return base + turn_cost + 0.02 + 0.001 * ((node[0] + neighbour[1]) % 3)


def _search(
    grid: RegularGrid,
    start: Node,
    goal: Node,
    model: CostModel,
    certificate: TemporalHeadingHeuristicCertificate | None,
) -> dict[str, Any]:
    start_state = (start, None)
    queue: list[tuple[float, float, int, tuple[Node, tuple[int, int] | None], tuple[Node, ...]]] = [
        (0.0, 0.0, 0, start_state, (start,))
    ]
    labels: dict[tuple[Node, tuple[int, int] | None], float] = {start_state: 0.0}
    serial = 1
    expanded = 0
    while queue:
        _priority, cost, _serial, state, path = heappop(queue)
        if cost != labels.get(state):
            continue
        expanded += 1
        if state[0] == goal:
            return {
                "nodes": [list(node) for node in path],
                "cost": cost,
                "expanded": expanded,
                "queue_peak": len(queue) + 1,
            }
        for neighbour in grid.neighbors(state[0]):
            next_state = (neighbour, (neighbour[0] - state[0][0], neighbour[1] - state[0][1]))
            next_cost = cost + _edge_cost(grid, state, neighbour, model)
            if next_cost >= labels.get(next_state, float("inf")):
                continue
            labels[next_state] = next_cost
            heuristic = (
                0.0 if certificate is None else (certificate.lower_bound(*next_state) or 0.0)
            )
            heappush(
                queue, (next_cost + heuristic, next_cost, serial, next_state, (*path, neighbour))
            )
            serial += 1
    raise RuntimeError("finite heading oracle found no route")


def _case(profile: str, objective: str, kind: str) -> dict[str, Any]:
    rows, columns, _time_frames = PROFILES[profile]
    grid = RegularGrid(
        latitudes=tuple(index * 0.05 for index in range(rows)),
        longitudes=tuple(index * 0.05 for index in range(columns)),
        allow_diagonal=True,
    )
    nodes = _nodes(grid)
    start, goal = (0, 0), (rows - 1, columns - 1)
    model = _model(objective)
    scope = _scope(profile, objective, grid)
    certificate = qualify_heading_heuristic(
        scope=scope,
        grid=grid,
        nodes=nodes,
        goal=goal,
        cost_model=model,
        objective=objective,
        expected_scope=scope,
    )
    active = certificate
    expected = scope
    if kind == "incomplete":
        active = replace(certificate, objective_lower_hours=certificate.objective_lower_hours[:-1])
    elif kind == "scope_mismatch":
        active = replace(
            certificate,
            scope=TemporalScope.from_mapping({**scope.mapping, "scope_revision": "mismatch"}),
        )
    elif kind == "non_admissible":
        active = replace(certificate, admissible=False, reason="non_admissible_fixture")
    baseline = _search(grid, start, goal, model, None)
    effective = active if active.usable and active.scope.matches(expected) else None
    candidate = _search(grid, start, goal, model, effective)
    expected_enabled = kind == "certified"
    route_match = baseline["nodes"] == candidate["nodes"] and baseline["cost"] == candidate["cost"]
    ordering_improved = candidate["expanded"] <= baseline["expanded"]
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "shape": PROFILES[profile],
        "objective": objective,
        "certificate_kind": kind,
        "certificate_usable": active.usable,
        "certificate_reason": active.reason,
        "certificate_digest": active.digest,
        "baseline": baseline,
        "candidate": candidate,
        "route_match": route_match,
        "semantic_match": route_match,
        "deterministic": candidate == _search(grid, start, goal, model, effective),
        "ordering_improved_or_equal": ordering_improved,
        "heading_heuristic_enabled": effective is not None,
        "rejection_expected": not expected_enabled,
        "pruning_observed": False,
        "fail_closed": (effective is not None) is expected_enabled,
        "scope_match": active.scope.matches(expected),
        "time_origin": T0,
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    implementation_files = (
        "scripts/benchmark_temporal_heading_heuristic.py",
        "src/arctic_route_planning/planners/temporal_heading_heuristic.py",
        "src/arctic_route_planning/planners/temporal_label_astar.py",
        "src/arctic_route_planning/planners/temporal_session.py",
    )
    implementation = {relative: _sha256(root / relative) for relative in implementation_files}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "certificate_kinds": CERTIFICATE_KINDS,
        "implementation": implementation,
        "git": _git_identity(root),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "fixture_digest": _digest({"profiles": profiles, "objectives": OBJECTIVES}),
        "dominance_policy": "disabled",
        "pruning": False,
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
    if identity["git"]["dirty"]:
        raise RuntimeError("heading heuristic proof requires a clean implementation worktree")
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
        },
    )
    _atomic_json(output / "heartbeat.json", {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    cases_path = output / "cases.jsonl"
    existing = {
        tuple(item.get(key) for key in ("profile", "objective", "certificate_kind"))
        for item in _read_jsonl(cases_path)
    }
    records: list[dict[str, Any]] = _read_jsonl(cases_path)
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    try:
        for profile in profiles:
            for objective in OBJECTIVES:
                for kind in CERTIFICATE_KINDS:
                    key = (profile, objective, kind)
                    if key in existing:
                        continue
                    record = _case(profile, objective, kind)
                    records.append(record)
                    _append_jsonl(cases_path, record)
                    _atomic_json(
                        output / "heartbeat.json",
                        {
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed": len(records),
                        },
                    )
    except KeyboardInterrupt:
        _atomic_json(
            manifest_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "STOPPED_HARD",
                "identity": identity,
                "experiment_id": identity["experiment_id"],
            },
        )
        (output / "STOPPED_HARD").write_text("\n", encoding="utf-8")
        raise
    passed = all(
        item["semantic_match"] and item["deterministic"] and item["fail_closed"] for item in records
    )
    certified = [item for item in records if item["certificate_kind"] == "certified"]
    summary = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": identity["experiment_id"],
        "status": "TEMPORAL_HEADING_HEURISTIC_MATRIX_PASS" if passed else "FAIL",
        "cases": len(records),
        "certified_cases": len(certified),
        "certified_semantic_match": all(item["semantic_match"] for item in certified),
        "certified_ordering_non_worse": all(
            item["ordering_improved_or_equal"] for item in certified
        ),
        "fail_closed": all(item["fail_closed"] for item in records),
        "pruning": False,
        "dominance_policy": "disabled",
        "production_candidate_enabled": False,
    }
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "status": "ALL_DONE",
            "identity": identity,
            "experiment_id": identity["experiment_id"],
            "summary": summary,
        },
    )
    (output / "ALL_DONE").write_text("\n", encoding="utf-8")
    return 0 if passed else 1


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


if __name__ == "__main__":
    raise SystemExit(_run(_parser().parse_args()))
