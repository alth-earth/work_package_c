#!/usr/bin/env python3
"""Research-only synthetic proof-carrying state-bound evidence.

The runner constructs a corridor certificate from an explicit finite graph
domain, then compares a disabled exact-arrival search with a certificate-
authorized search.  The route oracle is used only for semantic comparison;
the candidate never receives an oracle route or cost as input.  Rejected and
scope-mismatched certificates are also exercised and must prune zero labels.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.temporal_bounds import qualify_state_bound
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.1-temporal-state-bound.v1"
PROFILES = {
    "small": (5, 7, 7),
    "medium": (9, 13, 13),
    "stress": (13, 19, 19),
}
OBJECTIVES = ("fastest", "low_risk", "recommended")
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_state_bounds.py",
    "scripts/benchmark_temporal_dominance.py",
    "src/arctic_route_planning/planners/temporal_bounds.py",
    "src/arctic_route_planning/planners/temporal_label_astar.py",
    "src/arctic_route_planning/planners/temporal_session.py",
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
        json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
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
        handle.write(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _load_dominance_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance.py")
    spec = importlib.util.spec_from_file_location("c_synthetic_dominance_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load synthetic temporal benchmark helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_nodes(shape: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    return tuple(
        (row, column)
        for row in range(shape[0])
        for column in range(shape[1])
    )


def _corridor_nodes(shape: tuple[int, int]) -> tuple[tuple[int, int], ...]:
    rows, columns = shape
    return tuple(
        sorted(
            {(0, column) for column in range(columns)}
            | {(row, columns - 1) for row in range(rows)}
        )
    )


def _scope_digest(planner: Any, request: Any) -> TemporalScope:
    return planner.temporal_scope(request)


def _run_case(module: Any, profile: str, objective: str, certificate_kind: str) -> dict[str, Any]:
    objective_mode = module.ObjectiveMode(objective)
    baseline_planner, request, _ = module._build_components(
        profile,
        objective_mode,
        with_dominance=False,
    )
    baseline = baseline_planner.plan(request)
    baseline_route = module._route_payload(baseline)
    candidate_planner, candidate_request, _ = module._build_components(
        profile,
        objective_mode,
        with_dominance=False,
    )
    scope = _scope_digest(candidate_planner, candidate_request)
    shape = (module.SYNTHETIC_PROFILES[profile].rows, module.SYNTHETIC_PROFILES[profile].cols)
    if certificate_kind == "certified":
        certificate = qualify_state_bound(
            scope,
            _corridor_nodes(shape),
            universe_nodes=_all_nodes(shape),
            exclusion_proof=True,
            proof_digest=_digest(
                {"profile": profile, "objective": objective, "bound": "corridor-v1"}
            ),
            coverage_complete=True,
            evaluator_certified=True,
        )
    elif certificate_kind == "coverage_incomplete":
        certificate = qualify_state_bound(
            scope,
            _corridor_nodes(shape),
            universe_nodes=_all_nodes(shape),
            exclusion_proof=True,
            proof_digest="incomplete-proof",
            coverage_complete=False,
            evaluator_certified=True,
        )
    elif certificate_kind == "scope_mismatch":
        mismatched = dict(scope.mapping)
        mismatched["goal"] = (0, 0)
        certificate = qualify_state_bound(
            TemporalScope.from_mapping(mismatched),
            _corridor_nodes(shape),
            universe_nodes=_all_nodes(shape),
            exclusion_proof=True,
            proof_digest="scope-mismatch-proof",
            coverage_complete=True,
            evaluator_certified=True,
        )
    else:  # pragma: no cover - guarded by the case table
        raise ValueError(f"unknown state-bound case: {certificate_kind}")
    candidate_planner.state_bound_certificate = certificate
    candidate = candidate_planner.plan(candidate_request)
    candidate_route = module._route_payload(candidate)
    diagnostics = _jsonable(candidate.diagnostics)
    semantic_match = baseline_route == candidate_route
    expected_pruning = certificate_kind == "certified"
    actual_pruning = int(candidate.diagnostics.state_bound_pruned)
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "objective": objective,
        "certificate_kind": certificate_kind,
        "certificate_status": certificate.status.value,
        "certificate_usable": certificate.usable,
        "certificate_digest": certificate.digest,
        "scope_digest": scope.digest,
        "authorized": int(candidate.diagnostics.state_bound_rejected) == 0,
        "semantic_match": semantic_match,
        "baseline_semantic_digest": _digest(baseline_route),
        "candidate_semantic_digest": _digest(candidate_route),
        "state_bound_checks": int(candidate.diagnostics.state_bound_checks),
        "state_bound_pruned": actual_pruning,
        "state_bound_rejected": int(candidate.diagnostics.state_bound_rejected),
        "rejection_reasons": dict(candidate.diagnostics.state_bound_rejection_reasons),
        "expected_pruning": expected_pruning,
        "pruning_expectation_met": actual_pruning > 0 if expected_pruning else actual_pruning == 0,
        "diagnostics": diagnostics,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    return parser


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    identity = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "implementation": {
            relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES
        },
        "git": _git_identity(root),
    }
    identity["implementation_sha256"] = _digest(identity["implementation"])
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    if identity["git"]["git_dirty"]:
        raise RuntimeError("state-bound evidence requires a clean implementation worktree")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if recorded.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "oracle_role": "semantic_comparison_only",
        "search_limits_unchanged": True,
    }
    _atomic_json(manifest_path, manifest)
    heartbeat = output / "heartbeat.json"
    _atomic_json(heartbeat, {"updated_at": datetime.now(UTC), "status": "RUNNING"})
    module = _load_dominance_runner()
    cases: list[dict[str, Any]] = []
    kinds = ("certified", "coverage_incomplete", "scope_mismatch")
    existing = {}
    if args.resume and (output / "cases.jsonl").exists():
        for line in (output / "cases.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                key = (item.get("profile"), item.get("objective"), item.get("certificate_kind"))
                existing[key] = item
    for profile in profiles:
        for objective in OBJECTIVES:
            for kind in kinds:
                key = (profile, objective, kind)
                case = existing.get(key)
                if case is None:
                    case = _run_case(module, profile, objective, kind)
                    _append_jsonl(output / "cases.jsonl", case)
                cases.append(case)
                _atomic_json(
                    heartbeat,
                    {
                        "updated_at": datetime.now(UTC),
                        "status": "RUNNING",
                        "completed_cases": len(cases),
                    },
                )
    expected = len(profiles) * len(OBJECTIVES) * len(kinds)
    certified = [item for item in cases if item["certificate_kind"] == "certified"]
    rejected = [item for item in cases if item["certificate_kind"] != "certified"]
    passed = (
        len(cases) == expected
        and all(item["semantic_match"] for item in cases)
        and all(item["pruning_expectation_met"] for item in cases)
        and all(item["certificate_usable"] for item in certified)
        and all(not item["authorized"] for item in rejected)
        and all(item["state_bound_pruned"] == 0 for item in rejected)
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "STATE_BOUND_MATRIX_PASS" if passed else "STATE_BOUND_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected,
        "certified_case_count": len(certified),
        "rejected_case_count": len(rejected),
        "observed_certified_pruning": sum(item["state_bound_pruned"] for item in certified),
        "rejected_pruning_total": sum(item["state_bound_pruned"] for item in rejected),
        "semantic_match": all(item["semantic_match"] for item in cases),
        "cases": cases,
    }
    _atomic_json(output / "comparison-summary.json", summary)
    manifest.update(
        {"status": summary["status"], "summary": summary, "completed_at": datetime.now(UTC)}
    )
    _atomic_json(manifest_path, manifest)
    _atomic_json(heartbeat, {"updated_at": datetime.now(UTC), "status": summary["status"]})
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
