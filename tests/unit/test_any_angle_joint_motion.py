from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.motion.any_angle import (
    AnyAngleDecision,
    build_any_angle_candidates,
    great_circle_interpolate,
)
from arctic_route_planning.motion.joint_smoothing import build_joint_bspline


def _times(count: int) -> tuple[datetime, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return tuple(start + timedelta(hours=index) for index in range(count))


def test_any_angle_uses_great_circle_and_attempts_direct_first() -> None:
    points = ((0.0, 80.0), (0.2, 80.0), (0.2, 80.4), (0.4, 80.4))
    midpoint = great_circle_interpolate(points[0], points[-1], 0.5)
    assert midpoint[1] > (points[0][1] + points[-1][1]) / 2.0

    seen: list[tuple[int, int]] = []

    def validator(edge):
        seen.append((edge.start_index, edge.end_index))
        if (edge.start_index, edge.end_index) == (0, 3):
            return AnyAngleDecision(False, "direct_hard")
        return AnyAngleDecision(True)

    routes = build_any_angle_candidates(
        points,
        waypoint_times=_times(len(points)),
        sample_spacing_m=10_000.0,
        edge_validator=validator,
        maximum_candidates=4,
    )
    assert seen[0] == (0, 3)
    assert any(route.shortcut_count > 0 for route in routes)
    assert all(route.waypoint_indices[0] == 0 for route in routes)
    assert all(route.waypoint_indices[-1] == len(points) - 1 for route in routes)


def test_any_angle_retains_a_verified_channel_tail_representative() -> None:
    points = ((0.0, 70.0), (0.2, 70.0), (0.4, 70.2), (0.6, 70.2))

    def validator(edge):
        if (edge.start_index, edge.end_index) == (0, 3):
            return AnyAngleDecision(False, "direct_blocked")
        if edge.end_index == edge.start_index + 1 or (
            edge.start_index,
            edge.end_index,
        ) == (0, 2):
            return AnyAngleDecision(True)
        return AnyAngleDecision(False, "edge_blocked")

    routes = build_any_angle_candidates(
        points,
        edge_validator=validator,
        sample_spacing_m=10_000.0,
        maximum_candidates=3,
    )

    assert any(route.waypoint_indices == (0, 2, 3) for route in routes)


def test_any_angle_budget_keeps_a_geometry_complete_raw_fallback() -> None:
    points = ((0.0, 70.0), (0.2, 70.0), (0.2, 70.2), (0.4, 70.2))
    routes = build_any_angle_candidates(
        points,
        waypoint_times=_times(len(points)),
        sample_spacing_m=10_000.0,
        edge_validator=lambda _edge: AnyAngleDecision(False, "shortcut_rejected"),
        maximum_edge_evaluations=1,
    )
    raw = routes[-1]
    assert raw.waypoint_indices == tuple(range(len(points)))
    assert raw.accepted
    assert all(len(edge.points) >= 2 for edge in raw.edges)


def test_any_angle_shortcut_and_joint_smoothing_reduce_length_and_raise_radius() -> None:
    points = (
        (0.0, 70.0),
        (0.3, 70.0),
        (0.6, 70.4),
        (0.9, 70.8),
        (1.2, 71.0),
        (1.5, 71.4),
        (1.8, 71.8),
    )
    permitted_shortcuts = {(0, 2), (2, 4), (4, 6)}

    def validator(edge):
        if (edge.start_index, edge.end_index) == (0, 6):
            return AnyAngleDecision(False, "direct_blocked")
        if edge.end_index == edge.start_index + 1 or (
            edge.start_index,
            edge.end_index,
        ) in permitted_shortcuts:
            return AnyAngleDecision(True)
        return AnyAngleDecision(False, "fixture_blocked")

    routes = build_any_angle_candidates(
        points,
        edge_validator=validator,
        sample_spacing_m=1_000.0,
    )
    shortcut = next(route for route in routes if route.shortcut_count >= 3)
    raw = routes[-1]
    shortcut_joint = build_joint_bspline(shortcut, sample_spacing_m=1_000.0)
    raw_joint = build_joint_bspline(raw, sample_spacing_m=1_000.0)

    assert shortcut.direct_attempted
    assert shortcut.length_m < raw.length_m
    assert shortcut_joint.applied
    assert raw_joint.applied
    assert shortcut_joint.route_length_m < raw_joint.route_length_m
    assert shortcut_joint.minimum_radius_m > raw_joint.minimum_radius_m
    assert len(shortcut_joint.joint_windows) >= 1
    assert all(point not in shortcut_joint.points for point in points[1::2])


def test_joint_smoothing_rounds_multiple_corners_and_records_shared_trim() -> None:
    points = ((0.0, 70.0), (0.5, 70.0), (1.0, 70.5), (1.5, 71.0), (2.0, 71.5))
    route = build_any_angle_candidates(
        points,
        sample_spacing_m=1_000.0,
        edge_validator=lambda edge: AnyAngleDecision(
            edge.end_index == edge.start_index + 1,
            "non_adjacent_rejected",
        ),
    )[-1]
    result = build_joint_bspline(route, sample_spacing_m=1_000.0)

    assert result.applied
    assert result.c2_pass
    assert result.no_reverse_curvature_pass
    assert result.no_self_intersection_pass
    assert result.full_route_g2_pass
    assert result.joint_windows
    assert all(window.overlap_constraints_pass for window in result.joint_windows)
    assert result.minimum_radius_m > 0.0
    # The substantive turn is replaced by the joint curve.  The later raw
    # points are collinear with the outgoing span and may legitimately occur
    # as ordinary line-span samples; they remain ETA/arc-length anchors.
    assert points[1] not in result.points


def test_joint_smoothing_rejects_theoretical_local_trim_boundary() -> None:
    points = ((0.0, 70.0), (0.5, 70.0), (1.0, 70.5))
    route = build_any_angle_candidates(points, sample_spacing_m=1_000.0)[-1]
    with pytest.raises(ValueError, match=r"strictly below 0\.5"):
        build_joint_bspline(route, max_trim_fraction=0.5)
