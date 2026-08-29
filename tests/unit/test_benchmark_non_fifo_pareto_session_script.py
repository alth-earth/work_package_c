"""Contract checks for the M12 resumable Pareto-session runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_pareto_session.py"
_SPEC = importlib.util.spec_from_file_location("c_m12_pareto_session_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M12 runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_schema_modes_scenarios_and_frozen_limits() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-nonfifo-pareto-session.v1"
    assert _RUNNER.MODES == ("one_shot", "slice_only", "slice_restore")
    assert _RUNNER.OBJECTIVES == ("fastest", "low_risk", "recommended")
    assert {
        "frontier",
        "later_arrival",
        "same_exact_dominance",
        "periodic_cycle",
        "evaluator_failure",
        "resource_limit",
        "cancelled",
        "callback_drift",
        "policy_drift",
        "checkpoint_tamper",
    } <= set(_RUNNER.SCENARIOS)
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }


def test_runner_identity_binds_code_lock_config_and_fixture() -> None:
    args = _RUNNER._parser().parse_args(["--output-dir", "/tmp/m12-test"])
    identity = _RUNNER._identity(args, _SCRIPT.parents[1])
    assert identity["production_candidate_enabled"] is False
    assert identity["implementation"]["commit"]
    assert identity["lock_sha256"]
    assert identity["config_sha256"]
    assert identity["fixture_digest"]
    assert identity["worker_timeout_seconds"] == 30.0


def test_runner_records_mismatch_as_rejected_not_success() -> None:
    record = _RUNNER._worker_record("policy_drift", "fastest", "slice_restore", 1, 0)
    assert record["status"] == "MISMATCH_REJECTED"
    assert record["mismatch_rejected"] is True
    assert record["label"] is None
    assert record["semantic_digest"] is None


def test_runner_summary_requires_resume_equivalence_and_pruning() -> None:
    args = _RUNNER._parser().parse_args(
        ["--output-dir", "/tmp/m12-test", "--repetitions", "1"]
    )
    summary = _RUNNER._summary([], args)
    assert summary["status"] == "NO_PERFORMANCE_PROOF/FAIL"
    assert summary["one_shot_slice_equivalent"] is False
    assert summary["observed_same_exact_pruning"] == 0
