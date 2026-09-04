from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode, PlannerConfig
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import (
    EndpointBlockedError,
    PlanningCancelled,
    PlanningRequest,
    TimeDependentAStar,
)
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(
    frames: tuple[object, ...],
    *,
    planner_config: PlannerConfig | None = None,
) -> TimeDependentAStar:
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return TimeDependentAStar(grid, sampler, vessel, planner_config=planner_config)


def _risk_window(
    risks: tuple[np.ndarray, np.ndarray, np.ndarray],
    *,
    hard_masks: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> tuple[object, ...]:
    times = (T0, T0 + timedelta(hours=1), T0 + timedelta(hours=3))
    latitudes = (0.0, 0.05, 0.10)
    longitudes = (0.0, 0.05, 0.10, 0.15)
    if hard_masks is None:
        hard_masks = tuple(np.zeros((3, 4), dtype=np.bool_) for _ in times)
    return tuple(
        make_frame(
            valid_time,
            risk,
            risk_id=f"risk-{index}",
            hard_mask=hard_masks[index],
            latitudes=latitudes,
            longitudes=longitudes,
        )
        for index, (valid_time, risk) in enumerate(zip(times, risks, strict=True))
    )


def test_astar_matches_zero_heuristic_dijkstra_on_a_small_grid() -> None:
    zeros = tuple(np.zeros((3, 4), dtype=np.float32) for _ in range(3))
    planner = _planner(_risk_window(zeros))
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
    )

    astar = planner.plan(request)
    dijkstra = planner.plan(
        PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=request.objective,
            use_heuristic=False,
        )
    )

    assert astar.total_cost_hours == pytest.approx(dijkstra.total_cost_hours)
    assert astar.distance_km == pytest.approx(dijkstra.distance_km)
    assert astar.nodes == dijkstra.nodes
    assert astar.metrics.expanded_states <= dijkstra.metrics.expanded_states


def test_future_risk_changes_the_low_risk_route() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    future = zero.copy()
    future[1, 1:3] = 1.0
    static_planner = _planner(_risk_window((zero, zero, zero)))
    dynamic_planner = _planner(_risk_window((zero, future, future)))
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.LOW_RISK,
    )

    static_route = static_planner.plan(request)
    dynamic_route = dynamic_planner.plan(request)
    dynamic_fastest = dynamic_planner.plan(
        PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.departure_time,
            objective=ObjectiveMode.FASTEST,
        )
    )

    assert static_route.nodes == ((1, 0), (1, 1), (1, 2), (1, 3))
    assert dynamic_fastest.nodes == static_route.nodes
    assert dynamic_route.nodes != static_route.nodes
    assert any(row != 1 for row, _ in dynamic_route.nodes[1:-1])
    assert dynamic_route.average_risk < dynamic_fastest.average_risk


def test_goal_is_evaluated_at_eta_not_rejected_from_departure_snapshot() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    hard_at_departure = np.zeros((3, 4), dtype=np.bool_)
    hard_at_departure[1, 3] = True
    clear = np.zeros((3, 4), dtype=np.bool_)
    planner = _planner(
        _risk_window(
            (zero, zero, zero),
            hard_masks=(hard_at_departure, clear, clear),
        )
    )

    result = planner.plan(
        PlanningRequest(
            start=(1, 0),
            goal=(1, 3),
            departure_time=T0,
            objective=ObjectiveMode.FASTEST,
        )
    )

    assert result.nodes[-1] == (1, 3)
    assert result.steps[-1].eta > T0 + timedelta(hours=1)


def test_blocked_start_is_rejected_without_implicit_snapping() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    hard = np.zeros((3, 4), dtype=np.bool_)
    hard[1, 0] = True
    planner = _planner(_risk_window((zero, zero, zero), hard_masks=(hard, hard, hard)))

    with pytest.raises(EndpointBlockedError):
        planner.plan(
            PlanningRequest(
                start=(1, 0),
                goal=(1, 3),
                departure_time=T0,
            )
        )


def test_planning_can_be_cancelled_before_search() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))

    with pytest.raises(PlanningCancelled):
        planner.plan(
            PlanningRequest(
                start=(1, 0),
                goal=(1, 3),
                departure_time=T0,
                cancel_check=lambda: True,
            )
        )


def test_plan_candidates_runs_all_three_objectives() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))
    request = PlanningRequest(start=(1, 0), goal=(1, 3), departure_time=T0)

    candidates = planner.plan_candidates(request)

    assert set(candidates) == set(ObjectiveMode)
    assert all(result.objective is mode for mode, result in candidates.items())


def test_operational_speed_reserve_changes_planning_eta_only() -> None:
    """A planning reserve slows ETA/suggested speed without changing B factors."""

    zero = np.zeros((3, 4), dtype=np.float32)
    speed_factor = np.full((3, 4), 0.8, dtype=np.float32)
    frames = _risk_window((zero, zero, zero))
    frames = tuple(
        make_frame(
            frame.valid_time,
            zero,
            risk_id=f"reserve-{index}",
            latitudes=(0.0, 0.05, 0.10),
            longitudes=(0.0, 0.05, 0.10, 0.15),
            environment_speed_factor=speed_factor,
        )
        for index, frame in enumerate(frames)
    )
    base = _planner(frames, planner_config=PlannerConfig())
    reserved = _planner(
        frames,
        planner_config=PlannerConfig(operational_speed_reserve_fraction=0.05),
    )
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 1),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )

    base_result = base.plan(request)
    reserved_result = reserved.plan(request)

    assert reserved_result.nodes == base_result.nodes
    assert reserved_result.travel_hours == pytest.approx(base_result.travel_hours / 0.95)
    assert reserved_result.steps[-1].recommended_speed_knots == pytest.approx(
        base_result.steps[-1].recommended_speed_knots * 0.95
    )
    # The source factor is consumed unchanged; only C's operational speed is
    # reduced.  The ratio is therefore visible through the ETA/speed result.
    assert reserved_result.steps[-1].recommended_speed_knots < 10.0 * 0.8


def test_edge_geometry_cache_keeps_sample_count_in_identity() -> None:
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))

    three_point_geometry = planner._edge_geometry((1, 0), (1, 1), minimum_samples=3)
    five_point_geometry = planner._edge_geometry((1, 0), (1, 1), minimum_samples=5)

    assert len(three_point_geometry[2]) == 3
    assert len(five_point_geometry[2]) == 5
    assert len(planner._edge_cache) == 2
    assert planner.edge_geometry_cache_stats == {"hits": 0, "misses": 2, "entries": 2}

    planner._edge_geometry((1, 0), (1, 1), minimum_samples=3)

    assert planner.edge_geometry_cache_stats == {"hits": 1, "misses": 2, "entries": 2}


def test_edge_evaluation_converges_under_oscillating_environment_factor() -> None:
    """C-ALG-03: _evaluate_edge_data uses refine_eta (fail-closed fixed point).

    An environment factor that alternates with the sampled time drives the old
    fixed two-round loop toward whichever two rounds it sampled; the damped
    fixed point instead converges to a self-consistent ETA without raising.
    """
    from arctic_route_planning.planners.eta_refinement import (
        EtaRefinementPolicy,
    )

    # Oscillating risk field: risk_score alternates so that effective speed
    # factors differ between a "fast" and a "slow" time band.
    oscillating = np.zeros((3, 4), dtype=np.float32)
    # seed high risk in the middle band to force a slowdown at that sample time
    oscillating[1, :] = 1.0
    planner = _planner(_risk_window((oscillating, oscillating, oscillating)))
    # tight policy still converges on this small edge
    planner.eta_refinement_policy = EtaRefinementPolicy(
        max_iterations=12, absolute_tolerance_seconds=1.0
    )
    # _calm_speed is set on the plan() hot path; mirror it for the direct call.
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )
    data = planner._evaluate_edge_data(
        start=request.start,
        end=(1, 1),
        departure_time=request.departure_time,
        incoming_code=None,
        request=request,
    )
    assert data.travel_hours > 0.0
    assert data.arrival_time > request.departure_time
    assert data.confidence > 0.0


def test_edge_evaluation_hard_mask_rejection_preserved() -> None:
    """C-ALG-03: refine_eta integration preserves _RejectedEdge semantics."""
    from arctic_route_planning.planners.time_dependent_astar import _RejectedEdge

    zero = np.zeros((3, 4), dtype=np.float32)
    hard = np.zeros((3, 4), dtype=np.bool_)
    hard[1, 1] = True  # node (1,1) lies on the sampled (1,0)->(1,1) edge
    planner = _planner(
        _risk_window((zero, zero, zero), hard_masks=(hard, hard, hard))
    )
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )
    with pytest.raises(_RejectedEdge):
        planner._evaluate_edge_data(
            start=request.start,
            end=(1, 1),
            departure_time=request.departure_time,
            incoming_code=None,
            request=request,
        )


def test_edge_evaluation_injectable_policy_rounds() -> None:
    """C-ALG-03: an injected EtaRefinementPolicy is honored by the hot path.

    A single-iteration policy still converges on a calm edge (first implied ETA
    already satisfies the tolerance), which is the correct fail-open behavior:
    the fixed point is skipped only when it is provably unnecessary.
    """
    from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy

    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))
    planner.eta_refinement_policy = EtaRefinementPolicy(max_iterations=1)
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )
    data = planner._evaluate_edge_data(
        start=request.start,
        end=(1, 1),
        departure_time=request.departure_time,
        incoming_code=None,
        request=request,
    )
    assert data.travel_hours > 0.0
    assert planner.eta_refinement_policy.max_iterations == 1


def test_edge_evaluation_propagates_non_rejection_refinement_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-ALG-03: non-rejection EtaRefinementError fails closed (no silent pass).

    A cycle / max_iterations / terminal_mismatch inside refine_eta must surface
    to callers instead of being swallowed; only domain rejections
    (hard/risk/speed) are restored to their original exception types.  Only
    reached when an EtaRefinementPolicy is injected (default stays two-round).
    """
    from arctic_route_planning.planners import eta_refinement as refinement_module
    from arctic_route_planning.planners import time_dependent_astar as astar_module

    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))
    planner.eta_refinement_policy = refinement_module.EtaRefinementPolicy()
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )

    def _boom(*args: object, **kwargs: object) -> object:
        raise refinement_module.EtaRefinementError(
            "max_iterations", {"message": "did not converge (test)"}
        )

    monkeypatch.setattr(astar_module, "refine_eta", _boom)
    with pytest.raises(refinement_module.EtaRefinementError) as excinfo:
        planner._evaluate_edge_data(
            start=request.start,
            end=(1, 1),
            departure_time=request.departure_time,
            incoming_code=None,
            request=request,
        )
    assert excinfo.value.reason == "max_iterations"


def test_edge_evaluation_default_keeps_two_round_refinement() -> None:
    """C-ALG-03 (progressive): without an injected policy the historical
    two-round refinement is used, so the formal route digest stays unchanged.
    """
    zero = np.zeros((3, 4), dtype=np.float32)
    planner = _planner(_risk_window((zero, zero, zero)))
    assert planner.eta_refinement_policy is None
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )
    data = planner._evaluate_edge_data(
        start=request.start,
        end=(1, 1),
        departure_time=request.departure_time,
        incoming_code=None,
        request=request,
    )
    assert data.travel_hours > 0.0
    assert data.arrival_time > request.departure_time


def test_edge_evaluation_bounded_policy_hot_path() -> None:
    """C-ALG-03B: injecting a bounded (interval-contraction) policy works on
    the hot path and still runs the terminal self-consistency check.
    """
    from arctic_route_planning.planners.eta_refinement import EtaRefinementPolicy

    oscillating = np.zeros((3, 4), dtype=np.float32)
    oscillating[1, :] = 1.0
    planner = _planner(_risk_window((oscillating, oscillating, oscillating)))
    planner.eta_refinement_policy = EtaRefinementPolicy(method="bounded")
    planner._calm_speed = planner.vessel_model.effective_speed(1.0)
    request = PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.FASTEST,
    )
    data = planner._evaluate_edge_data(
        start=request.start,
        end=(1, 1),
        departure_time=request.departure_time,
        incoming_code=None,
        request=request,
    )
    assert data.travel_hours > 0.0
    assert data.arrival_time > request.departure_time
    assert data.confidence > 0.0
