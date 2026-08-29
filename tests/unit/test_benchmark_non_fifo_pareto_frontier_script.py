"""Contract and safety checks for the M11 finite Pareto runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_pareto_frontier.py"
_SPEC = importlib.util.spec_from_file_location("c_m11_pareto_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M11 runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_schema_matrix_and_frozen_limits_are_explicit() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-nonfifo-pareto-frontier.v1"
    assert _RUNNER.OBJECTIVES == ("fastest", "low_risk", "recommended")
    assert len(_RUNNER.FIXTURES) >= 10
    assert _RUNNER.DEFAULT_LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }


def test_runner_has_success_and_fail_closed_adversarial_fixtures() -> None:
    successful = _RUNNER._fixture("strict_same_exact_dominance")
    assert successful.expected_status == "GOAL_FOUND"
    assert successful.expected_pruning is None

    for name in (
        "hard_mask",
        "evaluator_failure",
        "cancelled",
        "maximum_horizon",
        "edge_limit",
        "non_increasing_arrival",
        "objective_dimension_mismatch",
    ):
        fixture = _RUNNER._fixture(name)
        assert fixture.expected_status != "GOAL_FOUND"


def test_runner_oracle_and_policy_identity_are_independent() -> None:
    fixture = _RUNNER._fixture("strict_same_exact_dominance")
    oracle = _RUNNER._oracle(fixture)[0]
    assert oracle["selected"]["node"] == fixture.goal
    assert oracle["frontier"]
    identity = _RUNNER._identity(
        _RUNNER._parser().parse_args(["--output-dir", "/tmp/m11-test"]),
        _SCRIPT.parents[1],
    )
    assert identity["production_candidate_enabled"] is False
    assert identity["fixture_digest"]
    assert identity["policy_digest"]
    assert identity["lock_sha256"]


def test_runner_summary_requires_complete_resource_evidence() -> None:
    args = _RUNNER._parser().parse_args(
        ["--output-dir", "/tmp/m11-test", "--repetitions", "1"]
    )
    fixture = _RUNNER._fixture("strict_same_exact_dominance")
    record = {
        "fixture": fixture.name,
        "objective": "fastest",
        "policy": "baseline",
        "repetition": 1,
        "status": "GOAL_FOUND",
        "expected_status": "GOAL_FOUND",
        "label": None,
        "semantic_digest": None,
        "frontier": [],
        "frontier_digest": "digest",
        "oracle": {"selected": None, "frontier": []},
        "pareto_pruned": 0,
    }
    summary = _RUNNER._summary([record], args)
    assert summary["resource_evidence_complete"] is False
    assert summary["status"] == "NO_PERFORMANCE_PROOF/FAIL"


def test_runner_worker_records_a_scope_bound_complete_frontier_certificate() -> None:
    record = _RUNNER._worker_record(
        "strict_same_exact_dominance",
        "fastest",
        "pareto",
        1,
        -1,
    )
    certificate = record["frontier_certificate"]
    assert record["status"] == "GOAL_FOUND"
    assert certificate["usable"] is True
    assert certificate["complete"] is True
    assert certificate["scope_digest"] == "fixture:strict_same_exact_dominance:fastest"
    assert certificate["frontier_count"] == len(record["frontier"])
    assert certificate["goal_label_count"] >= certificate["frontier_count"]
