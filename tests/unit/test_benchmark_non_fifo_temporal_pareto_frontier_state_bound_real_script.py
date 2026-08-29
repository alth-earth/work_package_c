"""Focused checks for the M22 real 24h frontier runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_frontier_state_bound_real.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m22_real_frontier_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M22 real frontier runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _case(objective: str, mode: str, *, status: str = "PASS") -> dict[str, object]:
    return {
        "objective": objective,
        "mode": mode,
        "repetition": 1,
        "status": status,
        "semantic_match": status == "PASS",
        "baseline_reference_match": status == "PASS",
        "candidate_reference_match": status == "PASS",
        "certificate_usable": status == "PASS",
        "comparison": {"accepted_frontier_match": status == "PASS"},
        "unexpected_pruning": False,
        "resource_clean": True,
        "resource_evidence_complete": True,
        "baseline": {
            "semantic_digest": f"semantic-{objective}",
            "frontier_digest": f"baseline-{objective}",
        },
        "candidate": {
            "semantic_digest": f"semantic-{objective}",
            "frontier_digest": f"candidate-{objective}",
            "state_bound_pruned": 10,
        },
    }


def test_schema_and_frozen_limits() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-temporal-pareto-state-bound-frontier-real.v1"
    assert _RUNNER.SEGMENTS == ("rolling_0_24h",)
    assert _RUNNER.MODES == ("one_shot", "slice_restore")
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window",
            "--route-plan-set",
            "/tmp/routes",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/m22",
        ]
    )
    assert args.segment == "rolling_0_24h"
    assert args.repetitions == 1
    assert args.slice_expansions == 1
    assert args.worker_timeout_seconds == 1800.0


def test_summary_accepts_complete_frontier_and_requires_real_pruning() -> None:
    cases = [
        _case(objective, mode)
        for objective in ("fastest", "low_risk", "recommended")
        for mode in _RUNNER.MODES
    ]
    identity = {"experiment_id": "m22-test", "repetitions": 1}
    summary = _RUNNER._summary(cases, identity, 0)
    assert summary["status"] == "READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW"
    assert summary["complete"] is True
    assert summary["frontier_equivalence"] is True
    assert summary["deterministic"] is True
    assert summary["observed_state_bound_pruning"] == 60
    assert summary["candidate_authorized"] is False


def test_summary_fails_closed_when_frontier_evidence_is_not_accepted() -> None:
    cases = [
        _case(objective, mode)
        for objective in ("fastest", "low_risk", "recommended")
        for mode in _RUNNER.MODES
    ]
    cases[0]["comparison"] = {"accepted_frontier_match": False}
    identity = {"experiment_id": "m22-test", "repetitions": 1}
    summary = _RUNNER._summary(cases, identity, 0)
    assert summary["status"] == "NO_FRONTIER_PROOF/FAIL"
    assert summary["frontier_equivalence"] is False


def test_summary_preserves_resource_limit_as_non_success() -> None:
    cases = [
        _case(objective, mode)
        for objective in ("fastest", "low_risk", "recommended")
        for mode in _RUNNER.MODES
    ]
    cases[0] = _case("fastest", "one_shot", status="RESOURCE_LIMIT")
    identity = {"experiment_id": "m22-test", "repetitions": 1}
    summary = _RUNNER._summary(cases, identity, 0)
    assert summary["status"] == "REAL_INPUT_24H_STATE_BOUND_FRONTIER_RESOURCE_FAIL"
    assert summary["candidate_authorized"] is False
