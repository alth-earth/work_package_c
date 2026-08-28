"""Static safety checks for the M7 real-input runner."""

from __future__ import annotations

from pathlib import Path

_SOURCE_PATH = (
    Path(__file__)
    .parents[2]
    .joinpath("scripts", "benchmark_non_fifo_temporal_topological_bound_real.py")
)
_SOURCE = _SOURCE_PATH.read_text(encoding="utf-8")


def test_real_runner_reuses_frozen_input_and_topological_adapter() -> None:
    assert "_load_fixture" in _SOURCE
    assert "qualify_topological_lower_bound" in _SOURCE
    assert "run_non_fifo_temporal_arrival_bounded_search" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"known_fifo_status": "REAL_INPUT_FIFO_VIOLATED"' in _SOURCE


def test_real_runner_binds_limits_and_cgroup_evidence() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert "resource_evidence_complete" in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
