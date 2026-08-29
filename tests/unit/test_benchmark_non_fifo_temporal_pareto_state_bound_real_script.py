"""Static safety checks for the M18 real actual-Pareto runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_state_bound_real.py"
)
_SOURCE = _SCRIPT.read_text(encoding="utf-8")
_SPEC = importlib.util.spec_from_file_location("c_m17_real_runner_test", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M17 real runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def test_runner_uses_actual_pareto_and_explicit_topological_certificate() -> None:
    assert "run_non_fifo_temporal_pareto_search" in _SOURCE
    assert "create_non_fifo_temporal_pareto_session" in _SOURCE
    assert "restore_non_fifo_temporal_pareto_session" in _SOURCE
    assert "qualify_topological_lower_bound" in _SOURCE
    assert "derive_temporal_corridor" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"state_bound_policy": "graph-topological-arrival-envelope-v1"' in _SOURCE
    assert '"c.p0.2-temporal-pareto-state-bound-24h.v1"' in _SOURCE
    assert '"rolling_0_24h"' in _SOURCE


def test_runner_keeps_frozen_limits_and_zero_heuristic_fence() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert "use_heuristic=False" in _SOURCE
    assert '"pareto_pruning": True' in _SOURCE
    assert '"production_candidate_enabled": False' in _SOURCE
    assert '"winter_enabled": False' in _SOURCE


def test_runner_persists_identity_resume_and_resource_evidence() -> None:
    assert "experiment_id" in _SOURCE
    assert "resume identity does not match prepared experiment" in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
    assert '"resource_evidence_complete"' in _SOURCE
    assert '"INCONCLUSIVE_CGROUP_BOUNDARY"' in _SOURCE
    assert '"ALL_DONE" if summary["complete"] else "STOPPED_HARD"' in _SOURCE
    assert '"REAL_INPUT_24H_STATE_BOUND_RESOURCE_FAIL"' in _SOURCE
    assert '"REAL_INPUT_24H_STATE_BOUND_RESOURCE_REVIEW"' in _SOURCE


def test_frozen_resource_limit_is_recorded_without_being_a_semantic_pass() -> None:
    assert 'case_status = "RESOURCE_LIMIT"' in _SOURCE
    assert 'case_reason = "frozen search limit reached"' in _SOURCE
    assert 'case.get("status") in {"PASS", "RESOURCE_LIMIT", "TIMEOUT"}' in _SOURCE


def test_runner_does_not_call_production_planner_or_enable_candidate() -> None:
    assert "planner.plan(" not in _SOURCE
    assert "certified_only" not in _SOURCE
    assert '"candidate_authorized": False' in _SOURCE


def test_reference_match_covers_actual_pareto_business_fields() -> None:
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
    candidate["steps"][0]["source_risk_ids"] = ["different-risk"]
    assert not _RUNNER._reference_matches(candidate, reference)
