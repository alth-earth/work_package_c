"""Static contract checks for the P0.2-M6 arrival-bound runner."""

from __future__ import annotations

from pathlib import Path

_SOURCE = (
    Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_arrival_bound.py"
).read_text(encoding="utf-8")


def test_runner_uses_explicit_arrival_adapter_and_keeps_dominance_disabled() -> None:
    assert "run_non_fifo_temporal_arrival_bounded_search" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"state_bound_mode": "explicit-arrival-envelope-only"' in _SOURCE
    assert '"state_bound_arrival_pruned"' in _SOURCE


def test_runner_has_fail_closed_incomplete_and_scope_modes() -> None:
    assert 'MODES = ("certified", "scope_mismatch", "incomplete")' in _SOURCE
    assert "arrival_bound_complete" in _SOURCE
    assert '"state_bound_arrival_pruned": 0' in _SOURCE


def test_runner_keeps_frozen_search_limits_and_artifacts() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
    assert '"comparison-summary.json"' in _SOURCE
