"""Contract checks for the M25 selected-route real-input runner."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_selected_route_bound_real.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m25_selected_route_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M25 selected-route runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _identity() -> dict[str, object]:
    return {
        "experiment_id": "m25-test",
        "repetitions": 1,
        "git": {"dirty": False},
    }


def _case(objective: str, *, status: str = "READY_FOR_SELECTED_ROUTE_BOUND_REVIEW"):
    return {
        "objective": objective,
        "repetition": 1,
        "status": status,
        "semantic_match": status.startswith("READY"),
        "selection_only": True,
        "frontier_complete": False,
        "incumbent_bound_pruned": 1,
    }


def test_runner_schema_and_frozen_limits() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-nonfifo-selected-route-bound-real.v1"
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window",
            "--route-plan-set",
            "/tmp/routes",
            "--config-root",
            "/tmp/config",
            "--segment",
            "rolling_0_24h",
            "--output-dir",
            "/tmp/m25",
        ]
    )
    assert args.mode == "selected-route"
    assert args.repetitions == 1
    assert args.worker_timeout_seconds == 900.0


def test_summary_requires_complete_semantic_selected_route_evidence() -> None:
    cases = [_case(objective) for objective in ("fastest", "low_risk", "recommended")]
    summary = _RUNNER._summary(cases, _identity(), 0)
    assert summary["status"] == "READY_FOR_SEPARATE_SELECTED_ROUTE_BOUND_PLAN"
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False
    assert summary["incumbent_bound_pruned_total"] == 3

    cases[0]["frontier_complete"] = True
    rejected = _RUNNER._summary(cases, _identity(), 0)
    assert rejected["status"] == "FAIL"


def test_summary_is_invalid_for_incomplete_or_dirty_identity() -> None:
    cases = [_case(objective) for objective in ("fastest", "low_risk", "recommended")]
    incomplete = _RUNNER._summary(cases[:2], _identity(), 0)
    assert incomplete["status"] == "INVALID/PENDING"
    dirty = _identity()
    dirty["git"] = {"dirty": True}
    assert _RUNNER._summary(cases, dirty, 0)["status"] == "INVALID/PENDING"

