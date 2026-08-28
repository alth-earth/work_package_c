"""Static safety checks for the M5 real-input bound runner."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).parents[2]
_SOURCE = (_ROOT / "scripts" / "benchmark_non_fifo_temporal_bound_real.py").read_text(
    encoding="utf-8"
)


def test_real_runner_uses_explicit_bound_adapter_and_keeps_dominance_disabled() -> None:
    assert "run_non_fifo_temporal_bounded_search" in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE
    assert '"state_bound_policy": "explicit-certified-only"' in _SOURCE
    assert '"known_fifo_status": "REAL_INPUT_FIFO_VIOLATED"' in _SOURCE


def test_real_runner_keeps_frozen_resource_limits_and_evidence_files() -> None:
    assert '"max_expansions": 50_000' in _SOURCE
    assert '"max_labels": 100_000' in _SOURCE
    assert '"max_queue": 50_000' in _SOURCE
    assert '"max_edge_evaluations": 400_000' in _SOURCE
    assert '"resource-frontier.jsonl"' in _SOURCE
    assert '"comparison-summary.json"' in _SOURCE
    assert "_resource_evidence_complete" in _SOURCE
