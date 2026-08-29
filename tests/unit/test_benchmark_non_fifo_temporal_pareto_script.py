"""Contract checks for the M14 actual temporal Pareto evidence runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_pareto.py"
_SPEC = importlib.util.spec_from_file_location("c_m14_actual_pareto_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M14 actual Pareto runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_identity_schema_modes_and_frozen_limits() -> None:
    args = _RUNNER._parser().parse_args(["--output-dir", "/tmp/m14-test"])
    identity = _RUNNER._identity(args, _SCRIPT.parents[1])
    assert identity["schema_version"] == "c.p0.2-temporal-pareto-bridge.v1"
    assert identity["production_candidate_enabled"] is False
    assert identity["components"] == _RUNNER.TEMPORAL_PARETO_COMPONENTS
    assert identity["limits"] == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }


def test_runner_worker_proves_pruning_and_business_semantics() -> None:
    record = _RUNNER._worker_record("same_exact_dominance", "fastest", "one_shot", 1, 0)
    assert record["status"] == "GOAL_FOUND"
    assert record["pareto_pruned"] > 0
    assert record["selected"]["nodes"] == ((0, 0), (0, 1), (0, 2))
    assert record["selected"]["steps"][-1]["source_risk_ids"] == ("m14-same_exact_dominance",)
    assert record["oracle"]["costs"] == record["selected"]["costs"]


def test_runner_worker_restore_and_fail_closed_cases() -> None:
    restored = _RUNNER._worker_record("later_arrival", "recommended", "slice_restore", 1, 0)
    assert restored["status"] == "GOAL_FOUND"
    assert restored["restore_match"] is True
    assert restored["checkpoint_digest"]

    for scenario in ("scope_drift", "checkpoint_tamper"):
        record = _RUNNER._worker_record(scenario, "fastest", "one_shot", 1, 0)
        assert record["status"] == "MISMATCH_REJECTED"
        assert record["mismatch_rejected"] is True
        assert record["selected"] is None
        assert record["frontier_digest"] is None


def test_runner_summary_fails_closed_when_evidence_is_incomplete() -> None:
    args = _RUNNER._parser().parse_args(["--output-dir", "/tmp/m14-test", "--repetitions", "1"])
    summary = _RUNNER._summary([], args)
    assert summary["status"] == "NO_PERFORMANCE_PROOF/FAIL"
    assert summary["completed_cases"] == 0
    assert summary["valid_pruning"] is False
