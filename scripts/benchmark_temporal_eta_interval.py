#!/usr/bin/env python3
"""Research-only ETA interval qualification runner.

The runner exercises the C-internal interval sidecar and keeps point-sampled
real-input diagnostics separate from proof-bearing synthetic fixtures.  A
finite point scan can report a counterexample, but it is never promoted to a
continuous FIFO or fixed-point certificate.  No production planner, ingress,
service, or public contract is imported by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arctic_route_planning.planners.eta_interval import (
    EtaInterval,
    EtaIntervalQualification,
    EtaIntervalStatus,
    qualify_eta_interval,
    qualify_eta_partition,
)
from arctic_route_planning.planners.temporal_qualification import TemporalScope

SCHEMA_VERSION = "c.p0.1-temporal-eta-interval.v1"
PROFILES = {
    "small": (5, 7, 7),
    "medium": (9, 13, 13),
    "stress": (13, 19, 19),
}
OBJECTIVES = ("fastest", "low_risk", "recommended")
IMPLEMENTATION_FILES = (
    "scripts/benchmark_temporal_eta_interval.py",
    "src/arctic_route_planning/planners/eta_interval.py",
    "src/arctic_route_planning/planners/eta_refinement.py",
    "src/arctic_route_planning/planners/temporal_qualification.py",
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


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
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


def _heartbeat(path: Path, **values: Any) -> None:
    _atomic_json(path, {"updated_at": datetime.now(UTC), **values})


class _WorkerTimeout(RuntimeError):
    """Raised when a real-input interval scan exceeds its deadline."""


def _timeout_handler(_signum: int, _frame: Any) -> None:
    raise _WorkerTimeout("ETA interval worker timeout")


def _scope(profile: str, objective: str, scenario: str) -> TemporalScope:
    return TemporalScope.from_mapping(
        {
            "edge_evaluator_digest": "explicit:eta-interval-fixture-v1",
            "fixture_digest": _digest((profile, objective, scenario)),
            "objective": objective,
            "profile": profile,
            "scenario": scenario,
        }
    )


def _certificate_record(
    *,
    profile: str,
    objective: str,
    scenario: str,
    qualification: EtaIntervalQualification | None = None,
    certificate: Any = None,
) -> dict[str, Any]:
    payload = qualification if qualification is not None else certificate
    status = payload.status.value if payload is not None else "UNCERTAIN_EVALUATOR_FAILURE"
    usable = bool(payload is not None and payload.usable)
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "objective": objective,
        "scenario": scenario,
        "status": status,
        "usable": usable,
        "scope_digest": payload.scope.digest if payload is not None else None,
        "certificate_digest": payload.digest if payload is not None else None,
        "evidence": _jsonable(payload) if payload is not None else None,
    }


def _synthetic_case(profile: str, objective: str, scenario: str) -> dict[str, Any]:
    scope = _scope(profile, objective, scenario)
    domain = EtaInterval(1.0, 3.0)
    if scenario == "contraction_unique":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.2,
            continuity_certified=True,
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    elif scenario == "continuous_nonunique":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.0, 3.0),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            continuity_certified=True,
            endpoint_residuals=(0.0, 0.0),
        )
        expected = EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
    elif scenario == "root_excluded":
        qualification = qualify_eta_interval(
            EtaInterval(1.0, 2.0),
            lambda _domain: EtaInterval(3.0, 4.0),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
        )
        expected = EtaIntervalStatus.ROOT_EXCLUDED
    elif scenario == "finite_no_bracket":
        qualification = qualify_eta_interval(
            EtaInterval(1.0, 2.0),
            lambda _domain: EtaInterval(3.0, 4.0),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=False,
        )
        expected = EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    elif scenario == "coverage_incomplete":
        qualification = qualify_eta_interval(
            domain,
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=False,
            evaluator_certified=True,
            contraction_bound=0.2,
        )
        expected = EtaIntervalStatus.UNCERTAIN_COVERAGE
    elif scenario == "evaluator_failure":
        def evaluate(_domain: EtaInterval) -> EtaInterval:
            raise RuntimeError("synthetic evaluator failure")

        qualification = qualify_eta_interval(
            domain,
            evaluate,
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
        )
        expected = EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
    elif scenario == "hard_mask_discontinuity":
        qualification = qualify_eta_partition(
            domain,
            (2.0,),
            lambda _domain: EtaInterval(1.4, 1.6),
            scope=scope,
            coverage_complete=True,
            evaluator_certified=True,
            contraction_bound=0.2,
            boundary_continuity_certified=True,
            boundary_reasons=("hard_mask_discontinuity",),
        )
        expected = EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    else:  # pragma: no cover - guarded by the scenario table
        raise ValueError(f"unknown ETA interval fixture: {scenario}")
    certificate = _certificate_record(
        profile=profile,
        objective=objective,
        scenario=scenario,
        certificate=qualification,
    )
    certificate["expected_status"] = expected.value
    certificate["case_digest"] = _digest(certificate)
    certificate["synthetic_shape"] = {
        "rows": PROFILES[profile][0],
        "columns": PROFILES[profile][1],
        "frames": PROFILES[profile][2],
    }
    return certificate


def _load_real_runner() -> Any:
    path = Path(__file__).resolve().with_name("benchmark_temporal_dominance_real.py")
    spec = importlib.util.spec_from_file_location("c_real_temporal_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load the real-input diagnostic runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _real_case(args: argparse.Namespace) -> dict[str, Any]:
    module = _load_real_runner()
    scan_args = SimpleNamespace(
        risk_window_commit=args.risk_window_commit,
        route_plan_set=args.route_plan_set,
        config_root=args.config_root,
        segment=args.segment,
        cpu=args.cpu,
    )
    scan = module._fifo_scan(scan_args)
    counterexample = scan.get("counterexample")
    if counterexample is not None:
        status = "FIFO_VIOLATED"
        reason = "sampled counterexample observed"
    elif scan.get("evaluation_errors", 0):
        status = "FIFO_UNCERTAIN_EVALUATOR_FAILURE"
        reason = "finite scan encountered evaluator/coverage failures"
    else:
        status = "FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
        reason = "finite point samples do not prove continuous interval monotonicity"
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "real",
        "status": status,
        "reason": reason,
        "input": scan.get("input"),
        "segment": scan.get("segment", args.segment),
        "edge_count": scan.get("edge_count", 0),
        "probe_count": scan.get("probe_count", 0),
        "evaluations": scan.get("evaluations", 0),
        "evaluation_errors": scan.get("evaluation_errors", 0),
        "evaluation_failure_classes": scan.get("evaluation_failure_classes", []),
        "counterexample": counterexample,
        "coverage_complete": False,
        "evaluator_certified": False,
        "dominance_usable": False,
        "point_scan": scan,
        "certificate_digest": None,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("synthetic", "real"), default="synthetic")
    parser.add_argument("--profile", choices=tuple(PROFILES), default="small")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--risk-window-commit", type=Path)
    parser.add_argument("--route-plan-set", type=Path)
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--segment", choices=("executable_0_6h", "rolling_0_24h"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--worker-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--cpu", type=int, default=-1)
    return parser


def _identity(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": args.mode,
        "profile": args.profile if args.mode == "synthetic" else None,
        "segment": args.segment,
        "implementation": _implementation_identity(root),
        "git": _git_identity(root),
        "worker_timeout_seconds": args.worker_timeout_seconds,
        "cpu": args.cpu,
    }
    if args.mode == "real":
        for name in ("risk_window_commit", "route_plan_set"):
            path = getattr(args, name)
            identity[name] = {"path": str(path.resolve()), "sha256": _sha256(path)}
        config_root = args.config_root.resolve()
        identity["config_root"] = {
            "path": str(config_root),
            "sha256": _tree_digest(config_root),
        }
    else:
        identity["profile_shape"] = PROFILES[args.profile]
    identity["experiment_id"] = f"{SCHEMA_VERSION}-{_digest(identity)[:16]}"
    return identity


def _run(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parents[1]
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    required_real_args = ("risk_window_commit", "route_plan_set", "config_root", "segment")
    if args.mode == "real" and not all(
        getattr(args, name) is not None for name in required_real_args
    ):
        raise SystemExit(
            "real mode requires --risk-window-commit, --route-plan-set, "
            "--config-root and --segment"
        )
    identity = _identity(args, root)
    if identity["git"]["git_dirty"]:
        raise RuntimeError("ETA interval evidence requires a clean implementation worktree")
    manifest_path = output / "manifest.json"
    recorded = None
    if manifest_path.exists():
        recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not args.resume:
            raise RuntimeError("experiment already exists; use --resume to continue it")
        if recorded.get("identity") != _jsonable(identity):
            raise RuntimeError("resume identity does not match the prepared experiment")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "RUNNING",
        "identity": identity,
        "experiment_id": identity["experiment_id"],
        "proof_policy": {
            "finite_samples_authorize": False,
            "requires_complete_interval_coverage": True,
            "requires_certified_evaluator": True,
            "requires_scope_match": True,
        },
    }
    if recorded is not None:
        manifest["resume_count"] = int(recorded.get("resume_count", 0)) + 1
    _atomic_json(manifest_path, manifest)
    heartbeat_path = output / "heartbeat.json"
    _heartbeat(heartbeat_path, status="RUNNING", completed_cases=0)
    cases_path = output / "cases.jsonl"
    interval_path = output / "eta-interval.jsonl"
    cases: list[dict[str, Any]] = []
    if args.mode == "synthetic":
        scenarios = (
            "contraction_unique",
            "continuous_nonunique",
            "root_excluded",
            "finite_no_bracket",
            "coverage_incomplete",
            "evaluator_failure",
            "hard_mask_discontinuity",
        )
        existing = {
            (item.get("objective"), item.get("scenario"))
            for item in _read_jsonl(cases_path)
        }
        for objective in OBJECTIVES:
            for scenario in scenarios:
                key = (objective, scenario)
                if key in existing:
                    continue
                case = _synthetic_case(args.profile, objective, scenario)
                cases.append(case)
                _append_jsonl(cases_path, case)
                _append_jsonl(interval_path, case)
                _heartbeat(heartbeat_path, status="RUNNING", completed_cases=len(cases))
        all_cases = _read_jsonl(cases_path)
        expected = len(OBJECTIVES) * len(scenarios)
        matrix_pass = len(all_cases) == expected and all(
            item.get("status") == item.get("expected_status") for item in all_cases
        )
        summary: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "mode": "synthetic",
            "status": "QUALIFICATION_MATRIX_PASS" if matrix_pass else "QUALIFICATION_MATRIX_FAIL",
            "case_count": len(all_cases),
            "expected_case_count": expected,
            "usable_certificate_count": sum(bool(item.get("usable")) for item in all_cases),
            "uncertain_count": sum(
                str(item.get("status", "")).startswith("UNCERTAIN")
                for item in all_cases
            ),
            "fail_closed": all(
                not item.get("usable")
                for item in all_cases
                if str(item.get("expected_status", "")).startswith("UNCERTAIN")
            ),
            "cases": all_cases,
        }
    else:
        previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, args.worker_timeout_seconds)
        try:
            case = _real_case(args)
        except _WorkerTimeout as error:
            summary = {
                "schema_version": SCHEMA_VERSION,
                "mode": "real",
                "status": "STOPPED_HARD",
                "reason": str(error),
                "case_count": 0,
                "proof_ready": False,
                "dominance_usable": False,
                "cases": [],
            }
            _atomic_json(output / "comparison-summary.json", summary)
            manifest.update(
                {
                    "status": "STOPPED_HARD",
                    "summary": summary,
                    "completed_at": datetime.now(UTC),
                }
            )
            _atomic_json(manifest_path, manifest)
            _heartbeat(heartbeat_path, status="STOPPED_HARD", completed_cases=0)
            (output / "STOPPED_HARD").write_text(str(error) + "\n", encoding="utf-8")
            return 2
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
        _append_jsonl(cases_path, case)
        _append_jsonl(interval_path, case)
        cases = [case]
        summary = {
            "schema_version": SCHEMA_VERSION,
            "mode": "real",
            "status": case["status"],
            "case_count": 1,
            "proof_ready": False,
            "dominance_usable": False,
            "cases": cases,
        }
    _atomic_json(output / "comparison-summary.json", summary)
    final_status = summary["status"]
    manifest.update({"status": final_status, "summary": summary, "completed_at": datetime.now(UTC)})
    _atomic_json(manifest_path, manifest)
    _heartbeat(heartbeat_path, status=final_status, completed_cases=summary["case_count"])
    (output / "ALL_DONE").write_text(final_status + "\n", encoding="utf-8")
    print(json.dumps(_jsonable(manifest), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if final_status not in {"QUALIFICATION_MATRIX_FAIL"} else 2


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.worker_timeout_seconds <= 0 or args.cpu < -1:
        raise SystemExit("timeout must be positive and cpu must be -1 or non-negative")
    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
