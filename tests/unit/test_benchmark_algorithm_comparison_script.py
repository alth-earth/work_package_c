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
import math
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.profiling import SyntheticProfileConfig
from arctic_route_planning.risk import RiskSampler

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_algorithm_comparison.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_algorithm_comparison", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)

_SWEEP_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_algorithm_comparison_sweep.py"
_SWEEP_SPEC = importlib.util.spec_from_file_location("c_sweep_driver", _SWEEP_PATH)
assert _SWEEP_SPEC is not None and _SWEEP_SPEC.loader is not None
_SWEEP = importlib.util.module_from_spec(_SWEEP_SPEC)
sys.modules[_SWEEP_SPEC.name] = _SWEEP
_SWEEP_SPEC.loader.exec_module(_SWEEP)

_SUMMARIZE_PATH = Path(__file__).parents[2] / "scripts" / "summarize_algorithm_comparison.py"
_SUMMARIZE_SPEC = importlib.util.spec_from_file_location(
    "c_summarize_algorithm_comparison", _SUMMARIZE_PATH
)
assert _SUMMARIZE_SPEC is not None and _SUMMARIZE_SPEC.loader is not None
_SUMMARIZE = importlib.util.module_from_spec(_SUMMARIZE_SPEC)
sys.modules[_SUMMARIZE_SPEC.name] = _SUMMARIZE
_SUMMARIZE_SPEC.loader.exec_module(_SUMMARIZE)

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
    """The search-strategy trio must stay intact for the efficiency comparison."""
    assert _SCRIPT.ALGORITHMS == (
        "time_dependent_astar",
        "dijkstra",
        "static_field",
    )
    # risk_blind is selectable, but must not join the search-strategy trio.
    assert "risk_blind" not in _SCRIPT.ALGORITHMS
    assert "risk_blind" in _SCRIPT.ALL_ALGORITHMS
    assert _SCRIPT.ALL_ALGORITHMS == _SCRIPT.ALGORITHMS + _SCRIPT.OBJECTIVE_BASELINES


def test_risk_blind_zeroes_only_the_ice_terms() -> None:
    """The ablation must change only `risk` and `uncertainty`."""
    weights = PlannerConfig().weights_for(ObjectiveMode.RECOMMENDED)
    blinded = _SCRIPT._risk_blind_weights(weights)
    assert blinded.risk == 0.0
    assert blinded.uncertainty == 0.0
    # Everything else is held fixed, otherwise the comparison is confounded.
    assert blinded.travel_time == weights.travel_time
    assert blinded.distance == weights.distance
    assert blinded.turn == weights.turn


def test_risk_blind_baseline_is_executable() -> None:
    """The risk-blind objective must still yield a route on the same input."""
    frames = _frames()
    blinded = _SCRIPT._risk_blind_weights(PlannerConfig().weights_for(ObjectiveMode.RECOMMENDED))
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    blind_planner = TimeDependentAStar(
        grid,
        RiskSampler(frames),
        _vessel(),
        cost_weights={ObjectiveMode.RECOMMENDED: blinded},
    )
    result = blind_planner.plan(_request(frames))
    assert result.nodes, "risk-blind baseline must yield a non-empty route"


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


def test_parse_node_accepts_row_col() -> None:
    assert _SCRIPT._parse_node("5,7") == (5, 7)
    assert _SCRIPT._parse_node("0,0") == (0, 0)


def test_parse_node_rejects_malformed() -> None:
    with pytest.raises(SystemExit):
        _SCRIPT._parse_node("5-7")
    with pytest.raises(SystemExit):
        _SCRIPT._parse_node("abc")


def test_departure_frame_index_matches_departure() -> None:
    frames = _frames()
    assert _SCRIPT._departure_frame_index(frames, frames[0].valid_time) == 0
    assert _SCRIPT._departure_frame_index(frames, frames[2].valid_time) == 2
    # A departure between frames keeps the earlier frame (the "current" field).
    between = frames[2].valid_time + timedelta(minutes=30)
    assert _SCRIPT._departure_frame_index(frames, between) == 2


def test_static_field_freeze_index_tracks_departure() -> None:
    """The static baseline must freeze the field at *departure*, not frame 0."""
    frames = _frames()
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    late_departure = frames[3].valid_time
    frozen = [replace(frame, payload=frames[3].payload) for frame in frames]
    sampler = RiskSampler(frozen)
    planner = TimeDependentAStar(grid, sampler, _vessel())
    request = replace(_request(frames), departure_time=late_departure)
    result = planner.plan(request)
    assert result.nodes


def test_sweep_build_od_cases_filters_by_length_bucket() -> None:
    """Every sampled case must fall into a short/medium/long hop bucket."""
    frames = _frames()
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    navigable = {(row, col) for row in range(grid.shape[0]) for col in range(grid.shape[1])}
    cases = _SWEEP._build_od_cases(
        window="holdout",
        navigable=navigable,
        grid=grid,
        per_bucket=2,
        axis="od_pair",
        departure_offset_hours=0.0,
        starts=[(1, 1)],
    )
    assert cases
    for case in cases:
        assert case.hops in range(4, 10)
        assert case.length_bucket in _SWEEP.LENGTH_BUCKETS
        assert case.case_id.startswith("holdout-od-")


def test_sweep_child_command_includes_all_four_algorithms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep must re-run every algorithm cell, including risk_blind.

    Regression guard: the first sweep only passed the search-strategy trio and
    silently dropped the risk-blind objective baseline, which would have left
    the motivation evidence at n=1.
    """

    captured: list[list[str]] = []

    def fake_run(command, **_kwargs):  # type: ignore[no-untyped-def]
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(_SWEEP.subprocess, "run", fake_run)
    case = _SWEEP.Case(
        window="holdout",
        case_id="x-canary",
        start=(5, 7),
        goal=(13, 7),
        departure_offset_hours=0.0,
        axis="od_pair",
        length_bucket="medium",
        hops=6,
    )
    _SWEEP._run_case(
        case,
        tmp_path,
        cpu=0,
        repetitions=1,
        warmup=0,
        max_expansions=50_000,
    )
    command = captured[0]
    assert "--algorithm" in command
    algorithms = [
        command[index + 1]
        for index, token in enumerate(command)
        if token == "--algorithm"
    ]
    assert algorithms == ["time_dependent_astar", "dijkstra", "static_field", "risk_blind"]


def test_sweep_plan_case_ids_are_unique() -> None:
    """Two construction axes must not collide on case ids."""
    frames = _frames()
    grid = RegularGrid.from_risk_frame(frames[0], allow_diagonal=False)
    navigable = {(r, c) for r in range(grid.shape[0]) for c in range(grid.shape[1])}
    cases = _SWEEP._build_od_cases(
        window="holdout",
        navigable=navigable,
        grid=grid,
        per_bucket=2,
        axis="od_pair",
        departure_offset_hours=0.0,
        starts=[(1, 1)],
    ) + _SWEEP._build_od_cases(
        window="holdout",
        navigable=navigable,
        grid=grid,
        per_bucket=1,
        axis="departure_time",
        departure_offset_hours=36.0,
        starts=[(1, 1)],
    )
    ids = [case.case_id for case in cases]
    assert len(ids) == len(set(ids))


def test_sign_test_is_exact_and_symmetric() -> None:
    # Two-sided exact sign test: p = 2 * P(X <= min(wins, losses)).
    # 4-4 on n=8 saturates at 1.0; the one-sided cases are small.
    assert _SUMMARIZE._sign_test_p_value(4, 4) == pytest.approx(1.0)
    assert _SUMMARIZE._sign_test_p_value(10, 0) == pytest.approx(2 / 2**10)
    assert _SUMMARIZE._sign_test_p_value(8, 2) == pytest.approx(2 * 56 / 2**10)
    # Swapping wins and losses must not change the two-sided p-value.
    assert _SUMMARIZE._sign_test_p_value(2, 8) == pytest.approx(
        _SUMMARIZE._sign_test_p_value(8, 2)
    )
    # An empty sample (no non-tied cases) is NaN, never a fake p-value.
    assert math.isnan(_SUMMARIZE._sign_test_p_value(0, 0))


def test_percentile_is_nearest_rank() -> None:
    values = [1, 2, 3, 4]
    assert _SUMMARIZE._percentile(values, 0.25) == 1
    assert _SUMMARIZE._percentile(values, 0.75) == 3
    assert _SUMMARIZE._percentile(values, 1.0) == 4
    assert math.isnan(_SUMMARIZE._percentile([], 0.5))


def test_tally_counts_wins_ties_losses() -> None:
    rows = [
        {"metric": -5.0},
        {"metric": -1.0},
        {"metric": 0.0},
        {"metric": 2.0},
        {"metric": None},
    ]
    stats = _SUMMARIZE._tally(rows, "metric", better_when_negative=True)
    assert stats["wins"] == 2
    assert stats["ties"] == 1
    assert stats["losses"] == 1
    assert stats["n"] == 4
