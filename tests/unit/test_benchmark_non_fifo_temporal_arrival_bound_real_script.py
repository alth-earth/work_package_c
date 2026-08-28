"""Static safety checks for the M6 real-input arrival-bound runner."""

from __future__ import annotations

from pathlib import Path

_SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_arrival_bound_real.py"
).read_text(encoding="utf-8")


def test_real_runner_uses_arrival_adapter_and_keeps_default_off() -> None:
    assert "run_non_fifo_temporal_arrival_bounded_search" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"state_bound_policy": "explicit-arrival-envelope-only"' in _SOURCE
    assert '"known_fifo_status": "REAL_INPUT_FIFO_VIOLATED"' in _SOURCE


def test_real_runner_binds_frozen_limits_and_resource_evidence() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
    assert '"resource_evidence_complete"' in _SOURCE
