"""Static checks for the M29 incumbent-seed diagnostic wrapper."""

from __future__ import annotations

from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_until_goal_real.py"
)


def test_m29_runner_selects_until_goal_with_distinct_schema() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "c.p0.2-nonfifo-pareto-until-goal-real.v1" in source
    assert '"until_goal"' in source
    assert "benchmark_non_fifo_temporal_pareto_heuristic_real.py" in source


def test_m29_runner_is_research_only() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "ordering diagnostic" in source
    assert "historical queue order" in source
