"""Focused checks for the M21 real-input frontier-equivalence runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_pareto_frontier_real.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m21_real_frontier_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M21 real frontier runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _certificate(
    *, scope: str = "scope-v1", identity: str = "input-v1", usable: bool = True
) -> dict[str, object]:
    return {
        "digest": f"cert-{identity}",
        "usable": usable,
        "complete": usable,
        "scope_digest": scope,
        "comparison_identity_digest": identity,
        "frontier_digest": f"frontier-{identity}",
    }


def _case(
    *, policy: str, frontier: list[dict[str, object]], usable: bool = True
) -> dict[str, object]:
    return {
        "objective": "fastest",
        "policy": policy,
        "repetition": 1,
        "status": "GOAL_FOUND",
        "frontier": frontier,
        "frontier_digest": "serialized-frontier",
        "scope_digest": "scope-v1",
        "frontier_certificate": _certificate(usable=usable),
        "reference_match": True,
        "resource_clean": True,
        "resource_evidence_complete": True,
        "unexpected_pruning": False,
        "semantic_digest": "semantic-v1",
        "pareto_pruned": 1 if policy == "pareto" else 0,
    }


def test_real_runner_schema_and_frozen_research_fence() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-temporal-pareto-frontier-real.v1"
    assert _RUNNER.POLICIES == ("baseline", "pareto")
    assert _RUNNER.SEGMENTS == ("executable_0_6h",)
    assert _RUNNER.SEARCH_LIMITS == {
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
            "/tmp/m21",
        ]
    )
    assert args.segment == "executable_0_6h"
    assert args.eta_method == "bounded"
    assert args.repetitions == 2


def test_frontier_comparison_is_exact_but_order_independent() -> None:
    left = {"node": [7, 6], "arrival": "2026-01-01T01:00:00Z", "costs": [1.0, 2.0]}
    right = {"node": [7, 7], "arrival": "2026-01-01T02:00:00Z", "costs": [2.0, 1.0]}
    candidate = _case(policy="pareto", frontier=[right, left])
    baseline = _case(policy="baseline", frontier=[left, right])
    comparison = _RUNNER._frontier_comparison(candidate, baseline)
    assert comparison["status"] == "MATCH"
    assert comparison["exact_frontier_match"] is True
    assert comparison["candidate_label_count"] == 2
    assert comparison["missing_label_digests"] == []
    assert comparison["unexpected_label_digests"] == []
    assert comparison["semantic_frontier_match"] is True
    assert comparison["accepted_frontier_match"] is True


def test_frontier_comparison_allows_only_digest_metadata_drift() -> None:
    frontier = [{"node": [7, 6], "arrival": "2026-01-01T01:00:00Z", "costs": [1.0, 2.0]}]
    candidate = _case(policy="pareto", frontier=[dict(frontier[0])])
    baseline = _case(policy="baseline", frontier=[dict(frontier[0])])
    candidate["frontier"][0] = {**frontier[0], "semantic_digest": "candidate-only-digest"}
    baseline["frontier"][0] = {**frontier[0], "semantic_digest": "baseline-only-digest"}
    comparison = _RUNNER._frontier_comparison(candidate, baseline)
    assert comparison["status"] == "SEMANTIC_MATCH"
    assert comparison["exact_frontier_match"] is False
    assert comparison["semantic_frontier_match"] is True
    assert comparison["accepted_frontier_match"] is True

    baseline["frontier"][0]["costs"] = [1.0, 2.1]
    mismatch = _RUNNER._frontier_comparison(candidate, baseline)
    assert mismatch["status"] == "FRONTIER_MISMATCH"
    assert mismatch["semantic_frontier_match"] is False
    assert mismatch["accepted_frontier_match"] is False


def test_frontier_comparison_fails_closed_for_incomplete_or_drifted_evidence() -> None:
    frontier = [{"node": [7, 6], "arrival": "2026-01-01T01:00:00Z", "costs": [1.0, 2.0]}]
    incomplete = _RUNNER._frontier_comparison(
        _case(policy="pareto", frontier=frontier, usable=False),
        _case(policy="baseline", frontier=frontier),
    )
    assert incomplete["status"] == "INCOMPLETE"
    assert incomplete["exact_frontier_match"] is False

    drifted = _case(policy="baseline", frontier=frontier)
    drifted["scope_digest"] = "scope-drift"
    identity_mismatch = _RUNNER._frontier_comparison(
        _case(policy="pareto", frontier=frontier), drifted
    )
    assert identity_mismatch["status"] == "IDENTITY_MISMATCH"
    assert identity_mismatch["exact_frontier_match"] is False


def test_summary_does_not_promote_single_repetition_to_deterministic_pass() -> None:
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window",
            "--route-plan-set",
            "/tmp/routes",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/m21",
            "--repetitions",
            "1",
        ]
    )
    frontier = [{"node": [7, 6], "arrival": "2026-01-01T01:00:00Z", "costs": [1.0, 2.0]}]
    cases = []
    comparisons = []
    for objective in ("fastest", "low_risk", "recommended"):
        baseline = _case(policy="baseline", frontier=frontier)
        candidate = _case(policy="pareto", frontier=frontier)
        baseline["objective"] = objective
        candidate["objective"] = objective
        cases.extend((baseline, candidate))
        comparisons.append(_RUNNER._frontier_comparison(candidate, baseline))
    summary = _RUNNER._summary(cases, comparisons, args, 0)
    assert summary["deterministic"] is False
    assert summary["frontier_pairs_match"] is True
    assert summary["strict_frontier_pairs_match"] is True
    assert summary["status"] == "REAL_INPUT_FRONTIER_EQUIVALENCE_INCONCLUSIVE"
    assert summary["candidate_authorized"] is False
