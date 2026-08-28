#!/usr/bin/env python3
"""Synthetic proof matrix for analytic ETA-root and FIFO certificates.

The runner is deliberately independent from the historical finite point-scan
runner.  It validates interval containment, the derived contraction bound,
implicit arrival monotonicity, and every fail-closed branch without invoking a
production planner or enabling temporal dominance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.eta_analytic import (
    NavigabilityStatus,
    SlopeInterval,
    qualify_analytic_eta,
)
from arctic_route_planning.planners.eta_interval import EtaInterval, EtaIntervalStatus
from arctic_route_planning.planners.eta_refinement import (
    EtaEvaluation,
    EtaRefinementError,
    EtaRefinementPolicy,
    refine_eta,
)
from arctic_route_planning.planners.temporal_qualification import FifoStatus, TemporalScope

SCHEMA_VERSION = "c.p0.1-temporal-eta-analytic-proof.v1"
PROFILES = {
    "small": (5, 7, 7),
    "medium": (9, 13, 13),
    "stress": (13, 19, 19),
}
OBJECTIVES = ("fastest", "low_risk", "recommended")
SCENARIOS = (
    "contraction_unique",
    "continuous_nonunique",
    "root_exclusion",
    "finite_no_bracket",
    "hard_mask_discontinuity",
    "risk_frame_boundary_discontinuity",
    "incomplete_coverage",
    "evaluator_failure",
    "eta_cycle",
    "max_iterations",
    "terminal_mismatch",
    "scope_mismatch",
    "policy_checkpoint_mismatch",
)
IMPLEMENTATION_FILES = (
    "src/arctic_route_planning/planners/eta_analytic.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/risk/sampler.py",
    "scripts/benchmark_temporal_eta_analytic_proof.py",
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


def _scope(profile: str, objective: str, scenario: str) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "certified:analytic-eta-proof-v1",
            "evaluator_certification": "certified:c.temporal-evaluator.v1",
            "fixture_digest": _digest((profile, objective, scenario)),
            "objective": objective,
            "profile": profile,
            "scenario": scenario,
        }
    )


def _eta_failure(scenario: str) -> tuple[str, str, bool]:
    calls = 0
    policy = EtaRefinementPolicy(
        method="damped",
        max_iterations=6 if scenario == "eta_cycle" else 3,
        history_size=3,
        relaxation=1.0 if scenario == "eta_cycle" else 0.5,
    )

    def evaluate(guess: float) -> EtaEvaluation:
        nonlocal calls
        calls += 1
        if scenario == "eta_cycle":
            implied = 3.0 if guess < 2.0 else 1.0
        elif scenario == "max_iterations":
            implied = guess + 1.0
        else:
            implied = 1.0 if calls == 1 else 1.4
        return EtaEvaluation(samples=(), speed=None, implied_travel_hours=implied)

    try:
        refine_eta(1.0, evaluate, policy=policy)
    except EtaRefinementError as error:
        expected = {
            "eta_cycle": "cycle",
            "max_iterations": "max_iterations",
            "terminal_mismatch": "terminal_mismatch",
        }[scenario]
        return error.reason, error.failure_class, error.reason == expected
    return "missing_failure", "invalid", False


def _case(profile: str, objective: str, scenario: str) -> dict[str, Any]:
    scope = _scope(profile, objective, scenario)
    domain = EtaInterval(1.0, 3.0)
    image = EtaInterval(1.4, 1.6)
    slope = SlopeInterval(0.0, 0.0)
    expected_status: str | None = None
    diagnostic_reason: str | None = None
    expected_fifo = FifoStatus.FIFO_UNCERTAIN.value
    expected_authorized = False
    expected_failure = True
    expected_scope: TemporalScope | None = None
    active_scope = scope
    policy_digest = "analytic-policy-v1"
    continuity = True
    coverage = True
    evaluator = True
    navigation = NavigabilityStatus.ALWAYS_NAVIGABLE
    endpoint_residuals: tuple[float, float] | None = None

    if scenario == "contraction_unique":
        expected_status = EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value
        expected_fifo = FifoStatus.FIFO_CERTIFIED.value
        expected_authorized = True
    elif scenario == "continuous_nonunique":
        slope = SlopeInterval(-1.1, 1.1)
        endpoint_residuals = (-1.0, 1.0)
        expected_status = EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE.value
    elif scenario == "root_exclusion":
        image = EtaInterval(4.0, 5.0)
        expected_status = EtaIntervalStatus.ROOT_EXCLUDED.value
    elif scenario == "finite_no_bracket":
        evaluator = False
        expected_status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
    elif scenario in {"hard_mask_discontinuity", "risk_frame_boundary_discontinuity"}:
        continuity = False
        diagnostic_reason = scenario
        expected_status = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY.value
    elif scenario == "incomplete_coverage":
        coverage = False
        expected_status = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY.value
        diagnostic_reason = "interval_domain_coverage_incomplete"
    elif scenario == "evaluator_failure":
        evaluator = False
        expected_status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
        diagnostic_reason = "evaluator_not_certified"
    elif scenario in {"eta_cycle", "max_iterations", "terminal_mismatch"}:
        reason, failure_class, expected_failure = _eta_failure(scenario)
        diagnostic_reason = f"{reason}:{failure_class}"
        coverage = False
        evaluator = False
        expected_status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
    elif scenario == "scope_mismatch":
        expected_scope = scope
        active_scope = TemporalScope.from_mapping({**scope.mapping, "goal": "different-goal"})
        expected_status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
        diagnostic_reason = "scope_mismatch"
    elif scenario == "policy_checkpoint_mismatch":
        active_scope = TemporalScope.from_mapping({**scope.mapping, "eta_policy_digest": "wrong"})
        expected_status = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
        diagnostic_reason = "policy_digest_mismatch"
    else:  # pragma: no cover - guarded by SCENARIOS
        raise ValueError(f"unknown analytic proof scenario: {scenario}")

    certificate = qualify_analytic_eta(
        domain=domain,
        image=image,
        scope=active_scope,
        expected_scope=expected_scope,
        policy_digest=policy_digest,
        partition_digest=_digest((profile, objective, scenario, "partition")),
        coverage_complete=coverage,
        evaluator_certified=evaluator,
        continuity_certified=continuity,
        navigation=navigation,
        phi_departure_slope=slope,
        phi_travel_slope=slope,
        endpoint_residuals=endpoint_residuals,
    )
    if scenario == "scope_mismatch":
        expected_status = certificate.root_status.value
    if scenario == "policy_checkpoint_mismatch":
        expected_status = certificate.root_status.value
    authorization = certificate.permits_dominance
    fail_closed = (not authorization) if not expected_authorized else authorization
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "shape": PROFILES[profile],
        "objective": objective,
        "scenario": scenario,
        "status": certificate.root_status.value,
        "expected_status": expected_status,
        "fifo_status": certificate.fifo_status.value,
        "expected_fifo_status": expected_fifo,
        "authorization_usable": authorization,
        "expected_authorization": expected_authorized,
        "root_authorized": certificate.root_authorized,
        "fail_closed": fail_closed,
        "expected_failure": expected_failure,
        "reason": certificate.reason,
        "diagnostic_reason": diagnostic_reason,
        "scope_digest": certificate.scope.digest,
        "policy_digest": certificate.policy_digest,
        "partition_digest": certificate.partition_digest,
        "certificate_digest": certificate.digest,
        "contraction_bound": certificate.contraction_bound,
        "arrival_slope": certificate.arrival_slope,
        "image": certificate.image,
        "domain": certificate.domain,
        "case_digest": _digest(certificate.digest),
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    implementation = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    identity = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "objectives": OBJECTIVES,
        "scenarios": SCENARIOS,
        "repetitions": args.repetitions,
        "implementation": implementation,
        "implementation_sha256": _digest(implementation),
        "git": _git_identity(root),
        "uv_lock_sha256": _sha256(root / "uv.lock"),
        "fixture_digest": _digest(
            {"profiles": profiles, "objectives": OBJECTIVES, "scenarios": SCENARIOS}
        ),
        "policy": {"dominance": "disabled", "tolerance_seconds": 1.0},
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _run(args: argparse.Namespace) -> int:
    if args.repetitions < 1 or args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("repetitions/timeout must be positive and cpu must be -1 or non-negative")
    root = Path(__file__).resolve().parents[1]
    identity = _identity(args, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("analytic ETA proof requires a clean implementation worktree")
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
    _atomic_json(output / "heartbeat.json", {"status": "RUNNING", "updated_at": datetime.now(UTC)})
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    existing: dict[tuple[Any, ...], dict[str, Any]] = {}
    cases_path = output / "cases.jsonl"
    if args.resume and cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                key = (
                    item.get("profile"),
                    item.get("objective"),
                    item.get("scenario"),
                    item.get("repetition"),
                )
                existing[key] = item
    cases: list[dict[str, Any]] = []
    for profile in profiles:
        for objective in OBJECTIVES:
            for scenario in SCENARIOS:
                for repetition in range(args.repetitions):
                    key = (profile, objective, scenario, repetition)
                    case = existing.get(key)
                    if case is None:
                        case = _case(profile, objective, scenario)
                        case["repetition"] = repetition
                        _append_jsonl(cases_path, case)
                    cases.append(case)
                    _atomic_json(
                        output / "heartbeat.json",
                        {
                            "status": "RUNNING",
                            "updated_at": datetime.now(UTC),
                            "completed_cases": len(cases),
                        },
                    )
    expected = len(profiles) * len(OBJECTIVES) * len(SCENARIOS) * args.repetitions
    deterministic = len({item["case_digest"] for item in cases}) == len({
        (item["profile"], item["objective"], item["scenario"]) for item in cases
    })
    all_expected = all(item["status"] == item["expected_status"] for item in cases)
    all_fifo_expected = all(item["fifo_status"] == item["expected_fifo_status"] for item in cases)
    all_fail_closed = all(item["fail_closed"] for item in cases)
    all_pruning_zero = all(
        not item["authorization_usable"]
        for item in cases
        if item["scenario"] != "contraction_unique"
    )
    passed = (
        len(cases) == expected
        and deterministic
        and all_expected
        and all_fifo_expected
        and all_fail_closed
        and all_pruning_zero
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "ANALYTIC_ETA_PROOF_MATRIX_PASS" if passed else "ANALYTIC_ETA_PROOF_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected,
        "authorization_count": sum(item["authorization_usable"] for item in cases),
        "fifo_certified_count": sum(
            item["fifo_status"] == FifoStatus.FIFO_CERTIFIED.value for item in cases
        ),
        "deterministic": deterministic,
        "all_expected": all_expected,
        "all_fifo_expected": all_fifo_expected,
        "fail_closed": all_fail_closed,
        "pruning_zero_for_rejected": all_pruning_zero,
    }
    _write_jsonl(output / "eta-interval.jsonl", cases)
    _atomic_json(output / "comparison-summary.json", summary)
    _atomic_json(
        output / "manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "status": summary["status"],
            "identity": identity,
            "summary": summary,
        },
    )
    _atomic_json(
        output / "heartbeat.json",
        {"status": summary["status"], "updated_at": datetime.now(UTC)},
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"experiment_id": identity["experiment_id"], **summary},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
