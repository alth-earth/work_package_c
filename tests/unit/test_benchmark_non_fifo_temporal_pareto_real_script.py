"""Focused safety checks for the M15 real-input Pareto runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_pareto_real.py"
_SPEC = importlib.util.spec_from_file_location("c_m15_real_pareto_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M15 real Pareto runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _args():
    return _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/risk.json",
            "--route-plan-set",
            "/tmp/routes.json",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/m15",
        ]
    )


def test_runner_parser_and_child_command_keep_research_fences() -> None:
    args = _args()
    command = _RUNNER._child_command(args, _RUNNER.OBJECTIVES[0], 2, "one_shot")
    assert args.segment == "executable_0_6h"
    assert args.cpu == 0
    assert "--worker" in command
    assert "--pareto-pruning" not in command
    assert command[command.index("--mode") + 1] == "one_shot"


def test_reference_match_checks_full_business_edge_evidence() -> None:
    candidate = {
        "nodes": [[0, 0], [0, 1]],
        "arrival_times": ["2026-01-01T00:00:00.000000+00:00", "2026-01-01T01:00:00.000000+00:00"],
        "costs": [2.0],
        "steps": [
            {
                "eta": "2026-01-01T01:00:00.000000+00:00",
                "heading_degrees": 90.0,
                "speed_knots": 10.0,
                "distance_km": 1.0,
                "risk_score": 0.1,
                "maximum_risk": 0.1,
                "confidence": 0.9,
                "cost": {"total_equivalent_hours": 2.0},
                "source_risk_ids": ["risk-1"],
            }
        ],
    }
    reference = {
        "nodes": [[0, 0], [0, 1]],
        "arrival_times": candidate["arrival_times"],
        "total_cost_hours": 2.0,
        "edge_values": [
            {
                "arrival_time": candidate["steps"][0]["eta"],
                "heading_degrees": 90.0,
                "speed_knots": 10.0,
                "distance_km": 1.0,
                "risk_score": 0.1,
                "maximum_risk": 0.1,
                "confidence": 0.9,
                "cost": {"total_equivalent_hours": 2.0},
                "source_risk_ids": ["risk-1"],
            }
        ],
    }
    assert _RUNNER._reference_matches(candidate, reference)
    candidate["steps"][0]["source_risk_ids"] = ["different-risk"]
    assert not _RUNNER._reference_matches(candidate, reference)


def test_summary_is_fail_closed_for_incomplete_real_matrix() -> None:
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/risk.json",
            "--route-plan-set",
            "/tmp/routes.json",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/m15",
            "--repetitions",
            "1",
        ]
    )
    summary = _RUNNER._summary([], args, 0)
    assert summary["status"] == "INVALID/PENDING"
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False


def test_source_keeps_zero_heuristic_and_disabled_temporal_dominance() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "use_heuristic=False" in source
    assert '"dominance_policy": "disabled"' in source
    assert "pareto_pruning=True" in source
    assert "run_non_fifo_temporal_pareto_search" in source
