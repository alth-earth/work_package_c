"""Static contract checks for the M28 goal-gated real diagnostic runner."""

from __future__ import annotations

from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_goal_gated_real.py"
)


def test_m28_runner_uses_a_distinct_schema_and_after_goal_mode() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "c.p0.2-nonfifo-pareto-goal-gated-real.v1" in source
    assert '"after_goal"' in source
    assert "benchmark_non_fifo_temporal_pareto_heuristic_real.py" in source


def test_m28_runner_is_explicitly_research_only() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "does not enable temporal dominance" in source
    assert "production planner" in source
