#!/usr/bin/env python3
"""Synthetic proof matrix for finite temporal corridor certificates.

The runner builds a small weighted grid and computes independent forward and
reverse shortest-path lower bounds.  ``derive_temporal_corridor`` may then
exclude only nodes whose certified necessary-condition cost exceeds the
horizon.  Rejected evidence is required to produce zero pruning.  No route,
cost, or oracle result is injected into a production planner.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.1-temporal-corridor-proof.v1"
PROFILES = {
    "small": (5, 7, 7),
    "medium": (9, 13, 13),
    "stress": (13, 19, 19),
}
OBJECTIVES = ("fastest", "low_risk", "recommended")
CERTIFICATE_KINDS = (
    "certified",
    "coverage_incomplete",
    "scope_mismatch",
    "non_admissible",
)
IMPLEMENTATION_FILES = (
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "scripts/benchmark_temporal_corridor.py",
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


def _write_jsonl(path: Path, values: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _nodes(shape: tuple[int, int, int]) -> tuple[tuple[int, int], ...]:
    rows, columns, _time_frames = shape
    return tuple((row, column) for row in range(rows) for column in range(columns))


def _neighbors(node: tuple[int, int], shape: tuple[int, int, int]) -> tuple[tuple[int, int], ...]:
    row, column = node
    rows, columns, _time_frames = shape
    result: list[tuple[int, int]] = []
    for delta_row, delta_column in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        candidate = (row + delta_row, column + delta_column)
        if 0 <= candidate[0] < rows and 0 <= candidate[1] < columns:
            result.append(candidate)
    return tuple(result)


def _edge_cost(first: tuple[int, int], second: tuple[int, int]) -> float:
    """Weighted-grid cost with a cheap top/right corridor and costly detours."""

    def off_corridor(node: tuple[int, int]) -> bool:
        return node[0] > 0 and node[1] < _CURRENT_LAST_COLUMN

    return 1.0 + (2.0 if off_corridor(first) or off_corridor(second) else 0.0)


_CURRENT_LAST_COLUMN = 0


def _dijkstra(
    source: tuple[int, int],
    shape: tuple[int, int, int],
) -> dict[tuple[int, int], float]:
    distances = {node: float("inf") for node in _nodes(shape)}
    distances[source] = 0.0
    queue: list[tuple[float, tuple[int, int]]] = [(0.0, source)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        for neighbor in _neighbors(node, shape):
            candidate = distance + _edge_cost(node, neighbor)
            if candidate < distances[neighbor]:
                distances[neighbor] = candidate
                heapq.heappush(queue, (candidate, neighbor))
    return distances


def _scope(profile: str, objective: str) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "certified:corridor-edge-v1",
            "bound_evaluator_digest": "certified:corridor-bound-v1",
            "fixture_digest": _digest((profile, objective, "weighted-grid-v1")),
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


def _case(profile: str, objective: str, certificate_kind: str) -> dict[str, Any]:
    global _CURRENT_LAST_COLUMN
    shape = PROFILES[profile]
    _CURRENT_LAST_COLUMN = shape[1] - 1
    nodes = _nodes(shape)
    start = (0, 0)
    goal = (shape[0] - 1, shape[1] - 1)
    scope = _scope(profile, objective)
    forward = _dijkstra(start, shape)
    reverse = _dijkstra(goal, shape)
    horizon = forward[goal]
    expected_allowed = tuple(
        node for node in nodes if forward[node] + reverse[node] <= horizon + 1e-9
    )
    expected_excluded = tuple(node for node in nodes if node not in expected_allowed)
    evidence_scope = scope
    admissible = True
    coverage_complete = True
    if certificate_kind == "coverage_incomplete":
        coverage_complete = False
    elif certificate_kind == "scope_mismatch":
        evidence_scope = TemporalScope.from_mapping({**scope.mapping, "goal": "wrong"})
    elif certificate_kind == "non_admissible":
        admissible = False
    evidence = AdmissibleBoundEvidence(
        scope=evidence_scope,
        method="independent-dijkstra-lower-bound-v1",
        evaluator_digest="certified:corridor-bound-v1",
        proof_digest=_digest((profile, objective, "independent-proof")),
        admissible=admissible,
        coverage_complete=coverage_complete,
    )
    derived = derive_temporal_corridor(
        scope=scope,
        universe_nodes=nodes,
        start=start,
        goal=goal,
        forward_lower_hours=forward,
        reverse_lower_hours=reverse,
        horizon_hours=horizon,
        objective=objective,
        bound_evidence=evidence,
        generated_nodes=nodes,
    )
    certified = certificate_kind == "certified"
    expected_pruning = len(expected_excluded) if certified else 0
    actual_pruning = derived.excluded_count if derived.usable else 0
    semantic_match = (
        (certified and set(derived.certificate.allowed_nodes) == set(expected_allowed))
        or (not certified and actual_pruning == 0)
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "shape": shape,
        "objective": objective,
        "certificate_kind": certificate_kind,
        "universe_count": len(nodes),
        "expected_allowed_count": len(expected_allowed),
        "expected_excluded_count": len(expected_excluded),
        "certificate_status": derived.certificate.status.value,
        "certificate_usable": derived.usable,
        "certificate_digest": derived.certificate.digest,
        "proof_digest": derived.proof_digest,
        "scope_digest": scope.digest,
        "expected_pruning": expected_pruning,
        "actual_pruning": actual_pruning,
        "pruning_expectation_met": actual_pruning == expected_pruning,
        "semantic_match": semantic_match,
        "projected_label_reduction": derived.projected_label_reduction,
        "rejected_reason": derived.reason,
        "state_bound_checks": len(nodes),
        "state_bound_pruned": actual_pruning,
        "state_bound_rejected": 0 if derived.usable else 1,
        "deterministic_digest": _digest(
            {
                "certificate": derived.certificate.digest,
                "actual_pruning": actual_pruning,
                "allowed": derived.certificate.allowed_nodes,
            }
        ),
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
        "fixture_digest": _digest({"profiles": profiles, "objectives": OBJECTIVES}),
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
        "oracle_role": "independent-lower-bound-and-semantic-audit-only",
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
        raise RuntimeError("temporal corridor proof requires a clean implementation worktree")
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
            "dominance_enabled": False,
        },
    )
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    existing: dict[tuple[str, str, str], dict[str, Any]] = {}
    cases_path = output / "cases.jsonl"
    if args.resume and cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                key = (item.get("profile"), item.get("objective"), item.get("certificate_kind"))
                existing[key] = item
    cases: list[dict[str, Any]] = []
    for profile in profiles:
        for objective in OBJECTIVES:
            for certificate_kind in CERTIFICATE_KINDS:
                key = (profile, objective, certificate_kind)
                case = existing.get(key)
                if case is None:
                    case = _case(profile, objective, certificate_kind)
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
    expected = len(profiles) * len(OBJECTIVES) * len(CERTIFICATE_KINDS)
    certified = [case for case in cases if case["certificate_kind"] == "certified"]
    rejected = [case for case in cases if case["certificate_kind"] != "certified"]
    passed = (
        len(cases) == expected
        and all(case["semantic_match"] for case in cases)
        and all(case["pruning_expectation_met"] for case in cases)
        and all(case["certificate_usable"] for case in certified)
        and all(not case["certificate_usable"] and case["actual_pruning"] == 0 for case in rejected)
        and all(case["actual_pruning"] > 0 for case in certified)
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "TEMPORAL_CORRIDOR_MATRIX_PASS" if passed else "TEMPORAL_CORRIDOR_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected,
        "certified_case_count": len(certified),
        "rejected_case_count": len(rejected),
        "observed_certified_pruning": sum(case["actual_pruning"] for case in certified),
        "rejected_pruning_total": sum(case["actual_pruning"] for case in rejected),
        "semantic_match": all(case["semantic_match"] for case in cases),
        "deterministic": len({case["deterministic_digest"] for case in cases})
        == len({(case["profile"], case["objective"], case["certificate_kind"]) for case in cases}),
        "fail_closed": all(
            not case["certificate_usable"] and case["actual_pruning"] == 0
            for case in rejected
        ),
    }
    _write_jsonl(output / "resource-frontier.jsonl", cases)
    _atomic_json(output / "comparison-summary.json", summary)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": summary["status"],
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "summary": summary,
        "completed_at": datetime.now(UTC),
    }
    _atomic_json(manifest_path, manifest)
    _atomic_json(heartbeat, {"status": summary["status"], "updated_at": datetime.now(UTC)})
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
