#!/usr/bin/env python3
"""Synthetic proof matrix for environmental speed lower-time envelopes.

The finite graph intentionally contains a slow branch whose final transition
can be rejected before its edge evaluator.  The matrix also exercises partial
coverage, hard-mask uncertainty, missing speed factors, and scope mismatch.
It is a C-internal diagnostic: exact-arrival labels and temporal dominance are
unchanged, and the independent exhaustive oracle remains the correctness
source.
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

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.planners.temporal_corridor import (
    AdmissibleBoundEvidence,
    derive_temporal_corridor,
)
from arctic_route_planning.planners.temporal_environment_envelope import (
    qualify_environmental_speed_envelope,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.2-temporal-environment-speed-envelope.v1"
PROFILES = {"small": (5, 7, 7), "medium": (9, 13, 13), "stress": (13, 19, 19)}
OBJECTIVES = ("fastest", "low_risk", "recommended")
CERTIFICATE_KINDS = ("certified", "partial", "hard_mask", "missing_speed", "scope_mismatch")
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HORIZON_HOURS = 2.0
NODES = ((0, 0), (0, 1), (1, 0), (1, 1))
EDGES = {
    (0, 0): (((0, 1), 0.5), ((1, 0), 1.5)),
    (0, 1): (((1, 1), 1.2),),
    (1, 0): (((1, 1), 1.5),),
    (1, 1): (),
}
EDGE_DISTANCE = {
    ((0, 0), (0, 1)): 3.0,
    ((0, 0), (1, 0)): 3.0,
    ((0, 1), (1, 1)): 3.0,
    ((1, 0), (1, 1)): 10.0,
}
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_environment_envelope.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_corridor.py",
    "src/arctic_route_planning/planners/temporal_environment_envelope.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
)


class _SyntheticInterval:
    def __init__(
        self,
        start: datetime,
        end: datetime,
        environment_speed_factor_lower: float | None,
        environment_speed_factor_upper: float | None,
        effective_environment_speed_factor_lower: float | None,
        effective_environment_speed_factor_upper: float | None,
        *,
        hard_mask_possible: bool = False,
        navigability_status: str = "ALWAYS_NAVIGABLE",
        coverage_complete: bool = True,
        failure_reason: str | None = None,
        evaluator_digest: str = "certified:synthetic-interval-v1",
        covered_frame_times: tuple[datetime, ...] = (T0,),
        source_risk_ids: tuple[str, ...] = ("synthetic-source",),
        risk_lower: float = 0.0,
        risk_upper: float = 0.0,
        confidence_lower: float = 1.0,
        confidence_upper: float = 1.0,
    ) -> None:
        self.start = start
        self.end = end
        self.environment_speed_factor_lower = environment_speed_factor_lower
        self.environment_speed_factor_upper = environment_speed_factor_upper
        self.effective_environment_speed_factor_lower = effective_environment_speed_factor_lower
        self.effective_environment_speed_factor_upper = effective_environment_speed_factor_upper
        self.hard_mask_possible = hard_mask_possible
        self.navigability_status = navigability_status
        self.coverage_complete = coverage_complete
        self.failure_reason = failure_reason
        self.evaluator_digest = evaluator_digest
        self.covered_frame_times = covered_frame_times
        self.source_risk_ids = source_risk_ids
        self.risk_lower = risk_lower
        self.risk_upper = risk_upper
        self.confidence_lower = confidence_lower
        self.confidence_upper = confidence_upper

    @property
    def usable(self) -> bool:
        return self.coverage_complete and self.failure_reason is None


class _SyntheticSampler:
    interval_evaluator_digest = "certified:synthetic-interval-v1"

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def _sample_interval(self, start, end, longitude, latitude):
        failed_edge = longitude >= 0.75
        if self.kind == "hard_mask" and failed_edge:
            return _SyntheticInterval(
                start,
                end,
                0.5,
                0.5,
                0.5,
                0.5,
                hard_mask_possible=True,
                navigability_status="TRANSITION_OR_UNKNOWN",
            )
        if self.kind == "missing_speed" and failed_edge:
            return _SyntheticInterval(
                start,
                end,
                None,
                None,
                None,
                None,
                failure_reason="environment_speed_factor_missing",
            )
        return _SyntheticInterval(start, end, 0.5, 0.5, 0.5, 0.5)


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
            "edge_evaluator_digest": "explicit:synthetic-edge-v1",
            "bound_evaluator_digest": "certified:synthetic-bound-v1",
            "fixture_digest": _digest((profile, objective, "environment-speed-v1")),
            "objective": objective,
            "profile": profile,
        }
    )


def _model() -> VesselPerformanceModel:
    return VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )


def _edge_input() -> tuple[tuple[Any, Any, float, tuple[GeoPoint, ...]], ...]:
    return tuple(
        (
            start,
            end,
            EDGE_DISTANCE[(start, end)],
            (
                GeoPoint(float(start[1]), float(start[0])),
                GeoPoint(float(end[1]), float(end[0])),
            ),
        )
        for start, neighbours in EDGES.items()
        for end, _hours in neighbours
    )


def _search(certificate: Any | None) -> dict[str, Any]:
    queue: list[tuple[float, int, Any, datetime, tuple[Any, ...]]] = [
        (0.0, 0, NODES[0], T0, (NODES[0],))
    ]
    labels = {(NODES[0], T0): 0.0}
    serial = 0
    edge_evaluations = 0
    edge_pruned = 0
    while queue:
        cost, _order, node, arrival, path = heappop(queue)
        if cost != labels.get((node, arrival)):
            continue
        if node == NODES[-1]:
            return {
                "nodes": [list(item) for item in path],
                "arrival": arrival.isoformat(),
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
    raise RuntimeError("synthetic graph has no route")


def _oracle() -> dict[str, Any]:
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
    expected_scope = scope
    sampler_kind = "clean"
    if kind == "partial" or kind == "hard_mask":
        sampler_kind = "hard_mask"
    elif kind == "missing_speed":
        sampler_kind = "missing_speed"
    elif kind == "scope_mismatch":
        expected_scope = TemporalScope.from_mapping({**scope.mapping, "profile": "drift"})
    sampler = _SyntheticSampler(sampler_kind)
    environmental = qualify_environmental_speed_envelope(
        risk_sampler=sampler,
        vessel_model=_model(),
        scope=scope,
        expected_scope=expected_scope,
        departure_lower=T0,
        horizon_hours=HORIZON_HOURS,
        edges=_edge_input(),
        universe_nodes=NODES,
        evaluator_certified=True,
    )
    # The node horizon bounds are deliberately independent and weak.  The
    # environmental edge map is the only new evidence under test.
    evidence = AdmissibleBoundEvidence(
        scope=scope,
        method="independent-synthetic-bound-v1",
        evaluator_digest="certified:synthetic-bound-v1",
        proof_digest=_digest((profile, objective, kind, "node-bound")),
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
        forward_lower_hours={node: 0.0 for node in NODES},
        reverse_lower_hours={node: 0.0 for node in NODES},
        horizon_hours=HORIZON_HOURS,
        objective=objective,
        bound_evidence=evidence,
        include_arrival_upper_bounds=True,
        edge_lower_hours=environmental.edge_lower_map,
        edge_bound_complete=environmental.coverage_complete,
        edge_bound_partial=environmental.partial,
    )
    baseline = _search(None)
    certificate = derived.certificate if derived.certificate.usable else None
    candidate = _search(certificate)
    oracle = _oracle()
    route_match = (
        baseline["nodes"] == oracle["nodes"]
        and candidate["nodes"] == oracle["nodes"]
        and candidate["total_cost_hours"] == oracle["total_cost_hours"]
    )
    expect_pruning = kind == "certified"
    pruning_ok = candidate["edge_pruned"] > 0 if expect_pruning else candidate["edge_pruned"] == 0
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "shape": PROFILES[profile],
        "objective": objective,
        "certificate_kind": kind,
        "envelope_status": environmental.status,
        "envelope_reason": environmental.reason,
        "envelope_covered_edges": environmental.covered_edge_count,
        "envelope_expected_edges": environmental.expected_edge_count,
        "certificate_usable": certificate is not None,
        "certificate_digest": None if certificate is None else certificate.digest,
        "edge_bound_digest": None if certificate is None else certificate.edge_bound_digest,
        "baseline": baseline,
        "candidate": candidate,
        "oracle": oracle,
        "semantic_match": route_match,
        "actual_edge_pruning": candidate["edge_pruned"],
        "pruning_expectation_met": pruning_ok,
        "fail_closed": (not expect_pruning and candidate["edge_pruned"] == 0),
        "deterministic_digest": _digest((certificate.digest if certificate else None, candidate)),
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
        "dominance_policy": "disabled",
        "search_limits": {
            "max_expansions": 50_000,
            "max_labels": 100_000,
            "max_queue": 50_000,
            "max_edge_evaluations": 400_000,
        },
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
        raise RuntimeError("environment envelope proof requires a clean implementation worktree")
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
    cases_path = output / "cases.jsonl"
    existing = {
        (
            str(item.get("profile")),
            str(item.get("objective")),
            str(item.get("certificate_kind")),
        ): item
        for item in _read_jsonl(cases_path)
    }
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
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
        and all(case["actual_edge_pruning"] == 0 for case in rejected)
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "TEMPORAL_ENVIRONMENT_SPEED_ENVELOPE_MATRIX_PASS"
        if passed
        else "TEMPORAL_ENVIRONMENT_SPEED_ENVELOPE_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": len(profiles) * len(OBJECTIVES) * len(CERTIFICATE_KINDS),
        "certified_case_count": len(certified),
        "observed_edge_pruning": sum(case["actual_edge_pruning"] for case in certified),
        "rejected_edge_pruning": sum(case["actual_edge_pruning"] for case in rejected),
        "semantic_match": all(case["semantic_match"] for case in cases),
        "fail_closed": all(case["actual_edge_pruning"] == 0 for case in rejected),
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
