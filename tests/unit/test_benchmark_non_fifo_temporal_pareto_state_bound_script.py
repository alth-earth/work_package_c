"""Contract checks for the M16 actual Pareto state-bound proof runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_pareto_state_bound.py"
_SPEC = importlib.util.spec_from_file_location("c_m16_pareto_state_bound_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M16 state-bound runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_identity_keeps_bound_disabled_by_default() -> None:
    args = _RUNNER._parser().parse_args(["--output-dir", "/tmp/m16-test"])
    identity = _RUNNER._identity(args, _SCRIPT.parents[1])
    assert identity["schema_version"] == "c.p0.2-temporal-pareto-state-bound.v1"
    assert identity["default_state_bound"] == "disabled"
    assert identity["production_candidate_enabled"] is False
    assert identity["winter_enabled"] is False
    assert identity["limits"] == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }


def test_runner_observes_certified_pruning_and_oracle_semantics() -> None:
    record = _RUNNER._worker_record("certified", "fastest", "one_shot", 1, 0)
    assert record["status"] == "GOAL_FOUND"
    assert record["state_bound_pruned"] > 0
    assert record["state_bound_rejected"] == 0
    assert record["selected"]["nodes"] == ((0, 0), (0, 1), (0, 2))
    assert record["selected"]["costs"] == record["oracle"]["costs"]


def test_runner_rejects_scope_and_checkpoint_drift_without_pruning() -> None:
    scope_mismatch = _RUNNER._worker_record("scope_mismatch", "fastest", "one_shot", 1, 0)
    assert scope_mismatch["status"] == "GOAL_FOUND"
    assert scope_mismatch["state_bound_pruned"] == 0
    assert scope_mismatch["state_bound_rejected"] > 0

    checkpoint_drift = _RUNNER._worker_record(
        "checkpoint_drift", "recommended", "one_shot", 1, 0
    )
    assert checkpoint_drift["status"] == "MISMATCH_REJECTED"
    assert checkpoint_drift["mismatch_rejected"] is True
    assert checkpoint_drift["selected"] is None


def test_runner_summary_requires_actual_certified_pruning() -> None:
    args = _RUNNER._parser().parse_args(["--output-dir", "/tmp/m16-test", "--repetitions", "1"])
    summary = _RUNNER._summary([], args)
    assert summary["status"] == "NO_PERFORMANCE_PROOF/FAIL"
    assert summary["certified_pruning_observed"] is False
