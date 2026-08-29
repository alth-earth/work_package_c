"""Static contract checks for the M27 real-input ordering diagnostic."""

from __future__ import annotations

from pathlib import Path

SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_heuristic_real.py"
)


def test_m27_runner_has_identity_and_evidence_fences() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "c.p0.2-nonfifo-pareto-heuristic-real.v1" in source
    assert "manifest.json" in source
    assert "cases.jsonl" in source
    assert "comparison-summary.json" in source
    assert "heartbeat.json" in source
    assert "ALL_DONE" in source
    assert "identity_clean" in source
    assert "production_candidate_enabled" in source
    assert "winter_enabled" in source


def test_m27_runner_composes_same_bounds_and_only_changes_priority() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "state_bound_certificate=state_certificate" in source
    assert "incumbent_bound_certificate=terminal_certificate" in source
    assert "heuristic_certificate=heuristic" in source
    assert "run_non_fifo_temporal_pareto_search" in source
    assert "certified-total-equivalent-hours-lower-bound-v1" in source


def test_m27_runner_keeps_frozen_limits_and_default_off_policies() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"max_expansions": 50_000' in source
    assert '"max_labels": 100_000' in source
    assert '"max_queue": 50_000' in source
    assert '"max_edge_evaluations": 400_000' in source
    assert '"dominance_policy": "disabled"' in source
    assert '"candidate_authorized": False' in source
    assert '"winter_authorized": False' in source
