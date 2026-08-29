"""Checks for the M34 actual Pareto transition-bound proof runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_transition_bound.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m34_transition_bound_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load the M34 transition-bound runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_keeps_dominance_and_candidate_disabled() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-temporal-pareto-transition-bound.v1"
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }


def test_certified_transition_bound_prunes_before_evaluator() -> None:
    record = _RUNNER._case("certified_partial", "fastest", "one_shot", 1)
    assert record["status"] == "GOAL_FOUND"
    assert record["semantic_match"] is True
    assert record["oracle_match"] is True
    assert record["state_bound_edge_checks"] > 0
    assert record["state_bound_edge_pruned"] == 1
    assert record["fail_closed"] is True


def test_uncertain_and_disabled_cases_keep_edges_live() -> None:
    for scenario in ("scope_mismatch", "coverage_incomplete", "disabled"):
        record = _RUNNER._case(scenario, "recommended", "one_shot", 1)
        assert record["semantic_match"] is True
        assert record["state_bound_edge_pruned"] == 0
        assert record["fail_closed"] is True


def test_checkpoint_digest_drift_is_rejected() -> None:
    record = _RUNNER._case("checkpoint_drift", "low_risk", "one_shot", 1)
    assert record["status"] == "MISMATCH_REJECTED"
    assert record["restore_error"]
    assert record["fail_closed"] is True


def test_checkpoint_drift_cancel_mode_still_exercises_cancellation() -> None:
    record = _RUNNER._case("checkpoint_drift", "recommended", "cancelled", 1)
    assert record["status"] == "CANCELLED"
    assert record["semantic_match"] is True
    assert record["state_bound_edge_pruned"] == 0
    assert record["fail_closed"] is True
