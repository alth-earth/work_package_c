"""Safety and status semantics for the M19 real 24h reference runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_pareto_reference_24h.py"
)
_SOURCE = _SCRIPT.read_text(encoding="utf-8")
_SPEC = importlib.util.spec_from_file_location("c_m19_reference_runner_test", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M19 reference runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _args():
    return _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/risk.json",
            "--route-plan-set",
            "/tmp/routes.json",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/m19",
        ]
    )


def test_runner_is_independent_and_keeps_research_fences() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-temporal-pareto-reference-24h.v1"
    assert "tuple[Any, Any, datetime]" in _SOURCE
    assert "zero-heuristic" in _SOURCE
    assert "planner.plan(" not in _SOURCE
    assert "certified_only" not in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"production_candidate_enabled": False' in _SOURCE
    assert '"winter_enabled": False' in _SOURCE
    assert "candidate" in _SOURCE and "reference" in _SOURCE


def test_runner_keeps_frozen_limits_and_evidence_files() -> None:
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    for filename in (
        "manifest.json",
        "cases.jsonl",
        "reference-frontier.jsonl",
        "comparison-summary.json",
        "heartbeat.json",
        "ALL_DONE",
        "STOPPED_HARD",
    ):
        assert filename in _SOURCE
    args = _args()
    command = _RUNNER._child_command(args, _RUNNER.OBJECTIVES[0], 1)
    assert "--worker" in command
    assert command[command.index("--objective") + 1] == "fastest"
    assert args.segment == "rolling_0_24h"


def test_summary_marks_complete_reference_resource_frontier_without_pass() -> None:
    cases = []
    for objective in ("fastest", "low_risk", "recommended"):
        cases.append(
            {
                "objective": objective,
                "repetition": 1,
                "status": "REFERENCE_RESOURCE_LIMIT",
                "reference_status": "REFERENCE_RESOURCE_LIMIT",
                "candidate_semantic_digest": None,
                "candidate_frontier_digest": None,
                "reference_match": False,
                "candidate_state_bound_pruned": 10,
                "certificate_usable": True,
                "resource_clean": True,
                "resource_evidence_complete": True,
            }
        )
    summary = _RUNNER._summary(cases, {"repetitions": 1, "experiment_id": "m19-test"}, 0)
    assert summary["complete"] is True
    assert summary["status"] == "REAL_INPUT_24H_REFERENCE_RESOURCE_FAIL"
    assert summary["semantic_reference_complete"] is False
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False


def test_summary_fails_closed_for_missing_or_hard_case() -> None:
    args = {"repetitions": 1, "experiment_id": "m19-test"}
    incomplete = _RUNNER._summary([], args, 0)
    assert incomplete["status"] == "INVALID/PENDING"

    hard_cases = [
        {
            "objective": objective,
            "repetition": 1,
            "status": "FAIL",
            "certificate_usable": False,
            "resource_clean": True,
            "resource_evidence_complete": True,
        }
        for objective in ("fastest", "low_risk", "recommended")
    ]
    failed = _RUNNER._summary(hard_cases, args, 0)
    assert failed["status"] == "NO_PERFORMANCE_PROOF/FAIL"
    assert failed["hard_failure_case_count"] == 3


def test_reference_match_requires_all_edge_business_fields() -> None:
    candidate = {
        "nodes": [[0, 0], [0, 1]],
        "arrival_times": [
            "2026-01-01T00:00:00.000000+00:00",
            "2026-01-01T01:00:00.000000+00:00",
        ],
        "costs": [2.0],
        "steps": [
            {
                "eta": "2026-01-01T01:00:00.000000+00:00",
                "heading_degrees": 90.0,
                "speed_knots": 10.0,
                "distance_km": 1.0,
                "risk_score": 0.1,
                "maximum_risk": 0.1,
                "confidence": 0.9,
                "cost": {"total_equivalent_hours": 2.0},
                "source_risk_ids": ["risk-1"],
            }
        ],
    }
    reference = {
        "nodes": candidate["nodes"],
        "arrival_times": candidate["arrival_times"],
        "total_cost_hours": 2.0,
        "edge_values": [
            {
                "arrival_time": candidate["steps"][0]["eta"],
                "heading_degrees": 90.0,
                "speed_knots": 10.0,
                "distance_km": 1.0,
                "risk_score": 0.1,
                "maximum_risk": 0.1,
                "confidence": 0.9,
                "cost": {"total_equivalent_hours": 2.0},
                "source_risk_ids": ["risk-1"],
            }
        ],
    }
    assert _RUNNER._reference_matches(candidate, reference)
    candidate["steps"][0]["source_risk_ids"] = ["risk-2"]
    assert not _RUNNER._reference_matches(candidate, reference)
