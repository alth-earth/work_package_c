"""Static safety checks for the M7 topological-bound runner."""

from __future__ import annotations

from pathlib import Path

_SOURCE_PATH = (
    Path(__file__)
    .parents[2]
    .joinpath("scripts", "benchmark_non_fifo_temporal_topological_bound.py")
)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")


def test_runner_uses_topological_qualification_and_explicit_adapter() -> None:
    assert "qualify_topological_lower_bound" in _SOURCE
    assert "run_non_fifo_temporal_arrival_bounded_search" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"graph-topological-arrival-envelope"' in _SOURCE


def test_runner_keeps_frozen_limits_and_rejected_modes() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert '"adjacency_failure"' in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
