#!/usr/bin/env python3
"""Synthetic conservative ETA interval proof matrix.

This runner is separate from the historical finite point-scan runner.  It is
an auditable, deterministic gate for interval envelope semantics only; it
does not enable a planner policy, import ingress/service, or claim that a
finite fixture proves a continuous ocean model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arctic_route_planning.planners.eta_interval import (
    EtaInterval,
    EtaIntervalStatus,
    qualify_eta_interval,
    qualify_eta_partition,
)
from arctic_route_planning.planners.eta_refinement import (
    EtaRefinementError,
    EtaRefinementPolicy,
    refine_eta,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.1-temporal-eta-proof.v1"
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
    "scripts/benchmark_temporal_eta_interval_proof.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval_evaluator.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
    "src/arctic_route_planning/risk/sampler.py",
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


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return "missing"
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result


def _git_identity(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.check_output(("git", "-C", str(root), *args), text=True).strip()

    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "git_dirty": bool(run("status", "--porcelain")),
    }


def _implementation_identity(root: Path) -> dict[str, Any]:
    files = {relative: _sha256(root / relative) for relative in IMPLEMENTATION_FILES}
    return {"files": files, "sha256": _digest(files)}


def _scope(profile: str, objective: str, scenario: str) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "explicit:eta-interval-proof-v1",
            "eta_policy_digest": _digest(asdict(EtaRefinementPolicy(method="bounded"))),
            "fixture_digest": _digest((profile, objective, scenario)),
            "objective": objective,
            "profile": profile,
            "scenario": scenario,
        }
    )


def _eta_error_case(scenario: str) -> tuple[EtaIntervalStatus, str, bool]:
    """Exercise bounded ETA failure classes without fabricating a route."""

    policy = EtaRefinementPolicy(
        method="damped",
        max_iterations=6 if scenario == "eta_cycle" else 3,
        history_size=3,
        relaxation=1.0 if scenario == "eta_cycle" else 0.5,
    )
    calls = 0

    def evaluate(guess: float):
        from arctic_route_planning.planners.eta_refinement import EtaEvaluation

        nonlocal calls
        calls += 1
        if scenario == "eta_cycle":
            raw = 3.0 if guess < 2.0 else 1.0
        elif scenario == "max_iterations":
            raw = guess + 1.0
        else:  # terminal_mismatch
            raw = 1.0 if calls == 1 else 1.4
        return EtaEvaluation(samples=(), speed=None, implied_travel_hours=raw)

    try:
        refine_eta(1.0, evaluate, policy=policy)
    except EtaRefinementError as error:
        expected = {
            "eta_cycle": "cycle",
            "max_iterations": "max_iterations",
            "terminal_mismatch": "terminal_mismatch",
        }[scenario]
        if error.reason == expected:
            return EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE, error.reason, False
        return EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE, f"unexpected:{error.reason}", True
    return EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE, "missing_eta_failure", True


def _synthetic_case(profile: str, objective: str, scenario: str) -> dict[str, Any]:
    scope = _scope(profile, objective, scenario)
    domain = EtaInterval(1.0, 3.0)
    sampled_values: tuple[float, ...] = ()
    if scenario == "contraction_unique":
        image = EtaInterval(1.4, 1.6)
        sampled_values = (1.4, 1.5, 1.6)
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: image,
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.25,
            continuity_certified=True,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    elif scenario == "continuous_nonunique":
        image = EtaInterval(1.0, 3.0)
        sampled_values = (1.0, 2.0, 3.0)
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: image,
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            continuity_certified=True,
            endpoint_residuals=(-1.0, 1.0),
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
    elif scenario == "root_exclusion":
        qualification = qualify_eta_interval(
            EtaInterval(1.0, 2.0),
            lambda _domain: EtaInterval(3.0, 4.0),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.ROOT_EXCLUDED
    elif scenario == "finite_no_bracket":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=False,
            continuity_certified=True,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    elif scenario in {"hard_mask_discontinuity", "risk_frame_boundary_discontinuity"}:
        reason = (
            "hard_mask_discontinuity"
            if scenario == "hard_mask_discontinuity"
            else "risk_frame_boundary_discontinuity"
        )
        qualification = qualify_eta_partition(
            domain,
            (2.0,),
            lambda segment: EtaInterval(segment.lower_hours + 0.4, segment.lower_hours + 0.6),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.2,
            continuity_certified=True,
            boundary_continuity_certified=True,
            boundary_reasons=(reason,),
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    elif scenario == "incomplete_coverage":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=False,
            evaluator_certified=True,
            contraction_bound=0.2,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.UNCERTAIN_COVERAGE
    elif scenario == "evaluator_failure":
        def evaluate(_domain: EtaInterval) -> EtaInterval:
            raise RuntimeError("synthetic interval evaluator failure")

        qualification = qualify_eta_interval(
            domain,
            evaluate,
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
    elif scenario in {"eta_cycle", "max_iterations", "terminal_mismatch"}:
        status, reason, invalid = _eta_error_case(scenario)
        return {
            "schema_version": SCHEMA_VERSION,
            "profile": profile,
            "objective": objective,
            "scenario": scenario,
            "status": status.value,
            "expected_status": status.value,
            "usable": False,
            "authorization_usable": False,
            "fail_closed": not invalid,
            "reason": reason,
            "scope_digest": scope.digest,
            "policy_digest": scope.mapping["eta_policy_digest"],
            "certificate_digest": None,
            "sampled_values": [],
            "interval_image": None,
            "case_digest": _digest((profile, objective, scenario, status.value, reason)),
        }
    elif scenario == "scope_mismatch":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope={"edge_evaluator_digest": "explicit:other"},
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.2,
            policy_digest=scope.mapping["eta_policy_digest"],
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
        # The certificate itself is valid only for the mismatching scope; the
        # case-level authorization check below must reject it.
    elif scenario == "policy_checkpoint_mismatch":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.2,
            policy_digest="wrong-policy-digest",
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    else:  # pragma: no cover - scenario table is fixed above
        raise ValueError(f"unknown proof fixture: {scenario}")

    qualified_image = (
        qualification.image
        if hasattr(qualification, "image")
        else qualification.certificates[0].image
        if qualification.certificates
        else None
    )
    status = qualification.status
    expected_value = expected.value
    authorization = bool(
        qualification.authorization_usable
        and qualification.scope.matches(scope)
        and qualification.policy_digest == scope.mapping.get("eta_policy_digest")
    )
    # Scope/policy mismatch are deliberately expected to produce a valid
    # local certificate but no authorization in the requested scope.
    if scenario in {"scope_mismatch", "policy_checkpoint_mismatch"}:
        status_value = "UNCERTAIN_SCOPE_OR_POLICY_MISMATCH"
        fail_closed = not authorization
    else:
        status_value = status.value
        fail_closed = status.value == expected_value and (
            not status.name.startswith("UNCERTAIN") or not authorization
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "objective": objective,
        "scenario": scenario,
        "status": status_value,
        "expected_status": (
            expected_value
            if scenario not in {"scope_mismatch", "policy_checkpoint_mismatch"}
            else status_value
        ),
        "qualification_status": status.value,
        "usable": bool(qualification.usable),
        "authorization_usable": authorization,
        "fail_closed": fail_closed,
        "reason": qualification.reason,
        "scope_digest": qualification.scope.digest,
        "policy_digest": scope.mapping["eta_policy_digest"],
        "certificate_digest": qualification.digest,
        "sampled_values": sampled_values,
        "interval_image": qualified_image,
        "case_digest": _digest(
            (profile, objective, scenario, status_value, qualification.digest, sampled_values)
        ),
    }


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "profiles": profiles,
        "profile_shapes": {profile: PROFILES[profile] for profile in profiles},
        "objectives": OBJECTIVES,
        "scenarios": SCENARIOS,
        "implementation": _implementation_identity(root),
        "git": _git_identity(root),
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
        "proof_policy": {
            "finite_samples_authorize": False,
            "unique_contraction_required": True,
            "nonunique_authorizes": False,
            "uncertain_pruning": 0,
        },
        "uv_lock": {"path": str((root / "uv.lock").resolve()), "sha256": _sha256(root / "uv.lock")},
        "config_root": {
            "path": str((root / "configs").resolve()),
            "sha256": _tree_digest(root / "configs"),
        },
    }
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


class _WorkerTimeout(RuntimeError):
    pass


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("ETA proof worker timeout")


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    if args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    profiles = tuple(PROFILES) if args.all_profiles else (args.profile,)
    output = args.output_dir.resolve()
    identity = _identity(args, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("ETA proof requires a clean implementation worktree")
    manifest_path = output / "manifest.json"
    previous = None
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        if previous.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "required_evidence": (
            "manifest.json",
            "cases.jsonl",
            "eta-interval.jsonl",
            "comparison-summary.json",
            "heartbeat.json",
        ),
    }
    if previous is not None:
        manifest["resume_count"] = int(previous.get("resume_count", 0)) + 1
    _atomic_json(manifest_path, manifest)
    heartbeat = output / "heartbeat.json"
    _atomic_json(
        heartbeat,
        {"status": "RUNNING", "completed_cases": 0, "updated_at": datetime.now(UTC)},
    )
    cases_path = output / "cases.jsonl"
    interval_path = output / "eta-interval.jsonl"
    existing = {
        (item.get("profile"), item.get("objective"), item.get("scenario"))
        for item in _read_jsonl(cases_path)
    }
    old_alarm = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
    try:
        for profile in profiles:
            for objective in OBJECTIVES:
                for scenario in SCENARIOS:
                    key = (profile, objective, scenario)
                    if key in existing:
                        continue
                    case = _synthetic_case(profile, objective, scenario)
                    _append_jsonl(cases_path, case)
                    _append_jsonl(interval_path, case)
                    existing.add(key)
                    _atomic_json(
                        heartbeat,
                        {
                            "status": "RUNNING",
                            "completed_cases": len(existing),
                            "updated_at": datetime.now(UTC),
                        },
                    )
    except _WorkerTimeout as error:
        summary = {
            "schema_version": SCHEMA_VERSION,
            "status": "STOPPED_HARD",
            "reason": str(error),
            "case_count": len(_read_jsonl(cases_path)),
            "expected_case_count": len(profiles) * len(OBJECTIVES) * len(SCENARIOS),
            "proof_ready": False,
        }
        _atomic_json(output / "comparison-summary.json", summary)
        manifest.update(
            {"status": "STOPPED_HARD", "summary": summary, "completed_at": datetime.now(UTC)}
        )
        _atomic_json(manifest_path, manifest)
        _atomic_json(
            heartbeat,
            {
                "status": "STOPPED_HARD",
                "completed_cases": summary["case_count"],
                "updated_at": datetime.now(UTC),
            },
        )
        (output / "STOPPED_HARD").write_text(str(error) + "\n", encoding="utf-8")
        return 2
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_alarm)

    cases = _read_jsonl(cases_path)
    expected_count = len(profiles) * len(OBJECTIVES) * len(SCENARIOS)
    matrix_pass = (
        len(cases) == expected_count
        and all(item.get("schema_version") == SCHEMA_VERSION for item in cases)
        and all(item.get("fail_closed") for item in cases)
        and all(
            item.get("authorization_usable") is False
            for item in cases
            if item.get("scenario") != "contraction_unique"
        )
        and all(
            item.get("status") == item.get("expected_status")
            for item in cases
            if item.get("scenario") not in {"scope_mismatch", "policy_checkpoint_mismatch"}
        )
    )
    summary = {
        "schema_version": SCHEMA_VERSION,
        "status": "PROOF_MATRIX_PASS" if matrix_pass else "PROOF_MATRIX_FAIL",
        "case_count": len(cases),
        "expected_case_count": expected_count,
        "profile_count": len(profiles),
        "scenario_count": len(SCENARIOS),
        "unique_root_count": sum(
            item.get("status") == EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value for item in cases
        ),
        "nonunique_diagnostic_count": sum(
            item.get("status") == EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE.value
            for item in cases
        ),
        "uncertain_count": sum(
            str(item.get("status", "")).startswith("UNCERTAIN") for item in cases
        ),
        "authorization_count": sum(bool(item.get("authorization_usable")) for item in cases),
        "fail_closed": all(item.get("fail_closed") for item in cases),
        "proof_ready": matrix_pass,
        "cases": cases,
    }
    _atomic_json(output / "comparison-summary.json", summary)
    manifest.update(
        {"status": summary["status"], "summary": summary, "completed_at": datetime.now(UTC)}
    )
    _atomic_json(manifest_path, manifest)
    _atomic_json(
        heartbeat,
        {
            "status": summary["status"],
            "completed_cases": len(cases),
            "updated_at": datetime.now(UTC),
        },
    )
    (output / "ALL_DONE").write_text(summary["status"] + "\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if matrix_pass else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--all-profiles", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def main(argv: list[str] | None = None) -> int:
    return _run(_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
