from __future__ import annotations

from arctic_route_planning.profiling import (
    SyntheticProfileConfig,
    profile_synthetic_three_objective_planning,
)


def test_synthetic_profile_preserves_three_objective_result_identity() -> None:
    config = SyntheticProfileConfig(rows=5, cols=7, frame_count=7)
    first = profile_synthetic_three_objective_planning(config)
    second = profile_synthetic_three_objective_planning(config)

    assert first["status"] == "EXPERIMENTAL"
    assert first["authoritative_route"] is False
    assert set(first["results"]) == {"fastest", "low_risk", "recommended"}
    assert {
        key: value["route_digest"] for key, value in first["results"].items()
    } == {
        key: value["route_digest"] for key, value in second["results"].items()
    }


def test_synthetic_profile_reports_component_boundaries() -> None:
    result = profile_synthetic_three_objective_planning(
        SyntheticProfileConfig(rows=5, cols=7, frame_count=7)
    )

    assert result["total_profiled_seconds"] > 0
    assert set(result["categories"]) == {
        "risk_sampling",
        "edge_traversal",
        "heuristic",
        "objective_calculation",
    }
    assert result["categories"]["risk_sampling"]["total_calls"] > 0
    assert result["categories"]["edge_traversal"]["total_calls"] > 0
    assert result["categories"]["heuristic"]["total_calls"] > 0
    assert result["categories"]["objective_calculation"]["total_calls"] > 0
    assert result["edge_geometry_cache"]["hits"] > 0
    assert result["edge_geometry_cache"]["misses"] > 0
    assert result["timings_are_overlapping"] is True
