from __future__ import annotations

from pathlib import Path

SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_certified_heuristic_real_24h.py"
)


def test_m9_runner_is_24h_research_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'SCHEMA_VERSION = "c.p0.2-temporal-certified-heuristic-real-24h.v2"' in source
    assert 'SEGMENT = "rolling_0_24h"' in source
    assert '"dominance_policy": "disabled"' in source
    assert '"state_bound_policy": "absent"' in source
    assert '"production_candidate_enabled": False' in source
    assert "--resume" in source
    assert '"phase": args.phase' in source
    assert '"baseline", "candidate", "reference"' in source
