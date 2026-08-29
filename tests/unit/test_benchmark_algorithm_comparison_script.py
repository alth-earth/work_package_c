"""Correctness smoke tests for the algorithm-comparison runner.

These tests guard the *fairness* of the comparison rather than promoting any
candidate: the production planner and the uninformed baseline must operate on
the same time-expanded graph, so their total cost has to agree exactly, and the
informed search must never expand more states than the uninformed one.

They are deliberately small (the ``small`` synthetic profile) so they run in
milliseconds and can be executed on every commit.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

_SCRIPT_PATH = (
    Path(__file__).parents[2] / "scripts" / "benchmark_algorithm_comparison.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "c_benchmark_algorithm_comparison", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)

_PROFILE = SyntheticProfileConfig(rows=5, cols=7, frame_count=7)


def _vessel() -> VesselPerformanceModel:
    return VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )


def _frames():
    return _SCRIPT.synthetic_profiling._make_frames(_PROFILE)


def _planner(frames) -> TimeDependentAStar:
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    return TimeDependentAStar(grid, sampler, _vessel())


def _request(frames, **overrides) -> PlanningRequest:
    base = PlanningRequest(
        start=(2, 0),
        goal=(2, 6),
        departure_time=frames[0].valid_time,
        objective=ObjectiveMode.RECOMMENDED,
        maximum_elapsed=timedelta(hours=6),
        maximum_risk=1.0,
        max_expansions=50_000,
    )
    return replace(base, **overrides) if overrides else base


@pytest.mark.parametrize("objective", list(ObjectiveMode))
def test_heuristic_does_not_change_total_cost(objective: ObjectiveMode) -> None:
    """A* and the zero-heuristic baseline must agree exactly on total cost."""
    frames = _frames()
    planner = _planner(frames)
    ours = planner.plan(_request(frames, objective=objective))
    baseline = planner.plan(_request(frames, objective=objective, use_heuristic=False))
    assert ours.total_cost_hours == pytest.approx(baseline.total_cost_hours, abs=1e-9)


@pytest.mark.parametrize("objective", list(ObjectiveMode))
def test_heuristic_never_expands_more(objective: ObjectiveMode) -> None:
    frames = _frames()
    planner = _planner(frames)
    ours = planner.plan(_request(frames, objective=objective))
    baseline = planner.plan(_request(frames, objective=objective, use_heuristic=False))
    assert ours.metrics.expanded_states <= baseline.metrics.expanded_states, (
        "informed search must not expand more states than the uninformed baseline"
    )


def test_static_field_baseline_is_executable() -> None:
    """The frozen-field baseline must still produce a route on the same input."""
    frames = _frames()
    frozen = [replace(frame, payload=frames[0].payload) for frame in frames]
    planner = _planner(frozen)
    result = planner.plan(_request(frozen))
    assert result.nodes, "static-field baseline must yield a non-empty route"


def test_runner_exposes_three_algorithms() -> None:
    assert _SCRIPT.ALGORITHMS == (
        "time_dependent_astar",
        "dijkstra",
        "static_field",
    )


def test_runner_rejects_real_input_without_route_plan_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real-input run without a route-plan-set must fail closed."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_algorithm_comparison.py",
            "--real-commit",
            "/nonexistent/commit.json",
            "--output-dir",
            "/tmp/should-not-be-created",
        ],
    )
    assert _SCRIPT.main() == 2
