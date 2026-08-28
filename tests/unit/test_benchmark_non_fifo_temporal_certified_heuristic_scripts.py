"""Static contract checks for the M8 synthetic and real runners."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_synthetic_runner_keeps_heuristic_explicit_and_fail_closed() -> None:
    source = (ROOT / "scripts/benchmark_non_fifo_temporal_certified_heuristic.py").read_text()
    assert "c.p0.2-temporal-certified-heuristic.v1" in source
    assert "run_non_fifo_temporal_certified_heuristic_search" in source
    assert '"dominance_policy": "disabled"' in source
    assert '"state_bound_policy": "absent"' in source
    assert '"REJECTED_FAIL_CLOSED"' in source
    assert '"candidate_enabled": False' in source


def test_real_runner_binds_committed_input_and_keeps_candidate_disabled() -> None:
    source = (ROOT / "scripts/benchmark_non_fifo_temporal_certified_heuristic_real.py").read_text()
    assert "c.p0.2-temporal-certified-heuristic-real.v1" in source
    assert 'risk_window_content_digest=fixture.commit["content_digest"]' in source
    assert 'risk_window_commit_id=fixture.commit["commit_id"]' in source
    assert "run_non_fifo_temporal_certified_heuristic_search" in source
    assert '"production_candidate_enabled": False' in source
