"""SMO-A* (Shared-Memoization Objective-A*) correctness tests.

These tests verify that the shared-edge-evaluation optimization produces
identical routes to the baseline A* while correctly caching edge traversals.
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner(frames: tuple[object, ...]) -> TimeDependentAStar:
    sampler = RiskSampler(frames)
    grid = RegularGrid.from_risk_frame(frames[0])
    vessel = VesselPerformanceModel(
        economic_speed_knots=10.0,
        minimum_steerage_speed_knots=2.0,
        maximum_speed_knots=12.0,
        minimum_speed_factor=0.2,
    )
    return TimeDependentAStar(grid, sampler, vessel)


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


def _base_request() -> PlanningRequest:
    return PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
    )


def _routes_equal(a, b) -> bool:
    return (
        a.nodes == b.nodes
        and a.total_cost_hours == pytest.approx(b.total_cost_hours)
        and a.distance_km == pytest.approx(b.distance_km)
        and a.travel_hours == pytest.approx(b.travel_hours)
        and a.average_risk == pytest.approx(b.average_risk)
    )


# --- 1. Route identity ---


class TestSmoAstarRouteIdentity:
    def test_zero_risk_grid_routes_match(self) -> None:
        zeros = tuple(np.zeros((3, 4), dtype=np.float32) for _ in range(3))
        planner = _planner(_risk_window(zeros))
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            assert _routes_equal(baseline[mode], shared[mode]), (
                f"Route mismatch for {mode}"
            )

    def test_dynamic_risk_grid_routes_match(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        future = zero.copy()
        future[1, 1:3] = 1.0
        planner = _planner(_risk_window((zero, future, future)))
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            assert _routes_equal(baseline[mode], shared[mode])

    def test_hard_masked_grid_routes_match(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        hard = np.zeros((3, 4), dtype=np.bool_)
        hard[0, 1] = True
        hard[0, 2] = True
        hard[2, 1] = True
        planner = _planner(
            _risk_window((zero, zero, zero), hard_masks=(hard, hard, hard))
        )
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            assert _routes_equal(baseline[mode], shared[mode])

    def test_all_step_details_match(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        future = zero.copy()
        future[0, 1] = 0.8
        future[2, 2] = 0.6
        planner = _planner(_risk_window((zero, future, future)))
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            b_steps = baseline[mode].steps
            s_steps = shared[mode].steps
            assert len(b_steps) == len(s_steps)
            for bs, ss in zip(b_steps, s_steps, strict=True):
                assert bs.node == ss.node
                assert bs.eta == ss.eta
                assert bs.edge_distance_km == pytest.approx(ss.edge_distance_km)
                assert bs.edge_risk_score == pytest.approx(ss.edge_risk_score)
                assert bs.edge_maximum_risk == pytest.approx(ss.edge_maximum_risk)
                assert bs.edge_confidence == pytest.approx(ss.edge_confidence)


# --- 2. Cache statistics ---


class TestSmoAstarCacheStatistics:
    def test_cache_hits_nonzero_when_shared(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        planner = _planner(_risk_window((zero, zero, zero)))
        request = _base_request()
        results = planner.plan_candidates(request, shared_edge_evaluation=True)
        total_hits = sum(r.metrics.traversal_cache_hits for r in results.values())
        assert total_hits > 0

    def test_cache_hits_zero_when_not_shared(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        planner = _planner(_risk_window((zero, zero, zero)))
        request = _base_request()
        results = planner.plan_candidates(request, shared_edge_evaluation=False)
        for _mode, result in results.items():
            assert result.metrics.traversal_cache_hits == 0
            assert result.metrics.traversal_cache_misses == 0

    def test_total_cache_ops_positive(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        planner = _planner(_risk_window((zero, zero, zero)))
        request = _base_request()
        results = planner.plan_candidates(request, shared_edge_evaluation=True)
        total_hits = sum(r.metrics.traversal_cache_hits for r in results.values())
        total_misses = sum(r.metrics.traversal_cache_misses for r in results.values())
        assert total_hits + total_misses > 0

    def test_first_objective_all_misses(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        planner = _planner(_risk_window((zero, zero, zero)))
        request = _base_request()
        results = planner.plan_candidates(request, shared_edge_evaluation=True)
        modes_ordered = list(ObjectiveMode)
        first = results[modes_ordered[0]]
        assert first.metrics.traversal_cache_hits == 0
        assert first.metrics.traversal_cache_misses > 0


# --- 3. Rejected-edge caching ---


class TestSmoAstarRejectedEdgeCaching:
    def test_rejected_hard_edges_are_cached(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        hard = np.zeros((3, 4), dtype=np.bool_)
        hard[0, 1] = True
        hard[2, 1] = True
        planner = _planner(
            _risk_window((zero, zero, zero), hard_masks=(hard, hard, hard))
        )
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            assert _routes_equal(baseline[mode], shared[mode])
        total_hits = sum(r.metrics.traversal_cache_hits for r in shared.values())
        assert total_hits > 0

    def test_rejections_are_traceback_free_cache_records(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        hard = np.zeros((3, 4), dtype=np.bool_)
        hard[0, 1] = True
        planner = _planner(
            _risk_window((zero, zero, zero), hard_masks=(hard, hard, hard))
        )
        planner.plan_candidates(_base_request(), shared_edge_evaluation=True)

        stats = planner.traversal_cache_stats
        assert stats["rejected_hits"] > 0
        assert stats["rejected_entries"] > 0
        assert stats["entries"] <= stats["peak_entries"]

    def test_rejected_risk_edges_are_cached(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        high = zero.copy()
        high[0, 1] = 0.9
        high[2, 1] = 0.9
        planner = _planner(_risk_window((zero, high, high)))
        request = PlanningRequest(
            start=(1, 0),
            goal=(1, 3),
            departure_time=T0,
            objective=ObjectiveMode.RECOMMENDED,
            maximum_risk=0.5,
        )
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            assert _routes_equal(baseline[mode], shared[mode])
        total_hits = sum(r.metrics.traversal_cache_hits for r in shared.values())
        assert total_hits > 0


# --- 4. Backward compatibility ---


class TestSmoAstarBackwardCompat:
    def test_default_is_unshared(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        planner = _planner(_risk_window((zero, zero, zero)))
        request = _base_request()
        default_results = planner.plan_candidates(request)
        explicit_results = planner.plan_candidates(
            request, shared_edge_evaluation=False
        )
        for mode in ObjectiveMode:
            assert _routes_equal(default_results[mode], explicit_results[mode])

    def test_shared_and_unshared_identical_digest_fields(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        future = zero.copy()
        future[0, 2] = 0.7
        planner = _planner(_risk_window((zero, future, future)))
        request = _base_request()
        baseline = planner.plan_candidates(request)
        shared = planner.plan_candidates(request, shared_edge_evaluation=True)
        for mode in ObjectiveMode:
            b = baseline[mode]
            s = shared[mode]
            assert b.source_risk_ids == s.source_risk_ids
            assert b.metrics.expanded_states == s.metrics.expanded_states
            assert b.metrics.generated_states == s.metrics.generated_states

    def test_final_objective_does_not_grow_shared_cache(self) -> None:
        zero = np.zeros((3, 4), dtype=np.float32)
        future = zero.copy()
        future[1, 1:3] = 1.0
        planner = _planner(_risk_window((zero, future, future)))
        planner.plan_candidates(_base_request(), shared_edge_evaluation=True)

        stats = planner.traversal_cache_stats
        assert stats["peak_entries"] > 0
        assert stats["entries"] < stats["misses"]
        assert stats["entries"] <= stats["peak_entries"]
