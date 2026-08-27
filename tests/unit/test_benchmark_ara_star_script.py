"""Evidence and gate tests for the research-only ARA* runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_ara_star.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_ara_star", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


def _resource_snapshot() -> dict[str, object]:
    return {
        "process_swap_kib": 0,
        "host_swap_pages": {"pswpin": 10, "pswpout": 20},
        "cpu_affinity": [2],
        "cgroup": {
            "memory_max": 4 * 1024 * 1024 * 1024,
            "memory_swap_current": 0,
            "memory_swap_max": 0,
            "memory_events": {"oom": 0, "oom_kill": 0},
        },
    }


def _worker(
    *,
    mode: str,
    control_ms: float,
    first_ms: float,
    first_cost: float = 1.05,
    final_cost: float = 1.0,
    rss: int = 105,
) -> dict[str, object]:
    routes = {
        objective.value: {
            "semantic_digest": f"digest-{objective.value}",
            "semantic": {"total_cost_hours": final_cost},
            "compute_ms": control_ms,
        }
        for objective in _SCRIPT.OBJECTIVES
    }
    stages = {
        objective.value: [
            {
                "epsilon": 2.5,
                "total_cost_hours": final_cost,
                "first_solution_cost_hours": first_cost,
                "first_solution_elapsed_ms": first_ms,
            },
            {
                "epsilon": 1.0,
                "total_cost_hours": final_cost,
                "first_solution_cost_hours": first_cost,
                "first_solution_elapsed_ms": first_ms,
            },
        ]
        for objective in _SCRIPT.OBJECTIVES
    }
    return {
        "mode": mode,
        "routes": routes,
        "stage_diagnostics": stages if mode == "ara" else {},
        "peak_rss_kib": rss,
        "resources_before": _resource_snapshot(),
        "resources_after": _resource_snapshot(),
        "wall_seconds": 1.0,
    }


def test_pair_order_alternates() -> None:
    assert _SCRIPT._pair_order(1) == ("baseline", "ara")
    assert _SCRIPT._pair_order(2) == ("ara", "baseline")


def test_ara_m0_summary_requires_all_objective_gates() -> None:
    baseline = [_worker(mode="baseline", control_ms=100.0, first_ms=0.0) for _ in range(3)]
    ara = [_worker(mode="ara", control_ms=0.0, first_ms=70.0) for _ in range(3)]

    summary = _SCRIPT._summarize(baseline, ara, strict_resources=True)
    assert summary["gate_verdict"] == "PASS"
    assert all(
        summary["gate_checks"][f"{objective}.{name}"] is True
        for objective in (mode.value for mode in _SCRIPT.OBJECTIVES)
        for name in (
            "epsilon_one_route_identity",
            "stage_cost_monotonic",
            "epsilon_2_5_first_solution_gap_le_10pct",
            "first_solution_median_at_least_20pct_faster",
        )
    )
    assert summary["control_limitation"] == "INHERITED_CONTROL_LIMITATION"


def test_ara_m0_summary_rejects_cost_gap_and_rss() -> None:
    baseline = [_worker(mode="baseline", control_ms=100.0, first_ms=0.0) for _ in range(3)]
    ara = [
        _worker(mode="ara", control_ms=0.0, first_ms=90.0, first_cost=1.2, rss=130)
        for _ in range(3)
    ]

    summary = _SCRIPT._summarize(baseline, ara, strict_resources=True)
    assert summary["gate_verdict"] == "FAIL"
    assert summary["gate_checks"]["recommended.epsilon_2_5_first_solution_gap_le_10pct"] is False
    assert summary["gate_checks"]["rss_ratio_le_1_10"] is False
