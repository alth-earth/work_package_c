from __future__ import annotations

import math

import pytest

from arctic_route_planning.research.route_smoothing_multispan import (
    CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR,
    CONTROL_POINT_COUNT,
    DEGREE,
    SPAN_COUNT,
    build_local_corner_curve,
    cox_de_boor_basis,
    evaluate_clamped_cubic_bspline,
    evaluate_clamped_cubic_bspline_derivatives,
)


def test_fixed_cubic_shape_and_partition_of_unity() -> None:
    assert DEGREE == 3
    assert SPAN_COUNT == 4
    assert CONTROL_POINT_COUNT == 7
    assert CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR == (
        0.0,
        0.0,
        0.0,
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
        1.0,
        1.0,
        1.0,
    )
    for parameter in (0.0, 0.1, 0.25, 0.49, 0.5, 0.75, 0.99, 1.0):
        weights = tuple(
            cox_de_boor_basis(index, DEGREE, parameter, CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR)
            for index in range(CONTROL_POINT_COUNT)
        )
        assert sum(weights) == pytest.approx(1.0)
        assert all(weight >= -1.0e-12 for weight in weights)


def test_evaluation_and_analytic_derivatives_match_a_finite_difference() -> None:
    controls = (
        (0.0, 0.0),
        (1.0, 0.0),
        (2.0, 0.0),
        (2.5, 0.5),
        (2.5, 1.5),
        (2.5, 2.5),
        (2.5, 3.5),
    )
    parameter = 0.37
    first, second = evaluate_clamped_cubic_bspline_derivatives(controls, parameter)
    step = 1.0e-5
    before = evaluate_clamped_cubic_bspline(controls, parameter - step)
    current = evaluate_clamped_cubic_bspline(controls, parameter)
    after = evaluate_clamped_cubic_bspline(controls, parameter + step)
    numerical_first = ((after[0] - before[0]) / (2.0 * step), (after[1] - before[1]) / (2.0 * step))
    numerical_second = (
        (after[0] - 2.0 * current[0] + before[0]) / step**2,
        (after[1] - 2.0 * current[1] + before[1]) / step**2,
    )
    assert first == pytest.approx(numerical_first, rel=1.0e-6, abs=1.0e-7)
    assert second == pytest.approx(numerical_second, rel=1.0e-5, abs=1.0e-6)


def test_local_corner_exposes_samples_curvature_metadata_and_local_g2_evidence() -> None:
    curve = build_local_corner_curve((0.0, 0.0), (10_000.0, 0.0), (10_000.0, 10_000.0), 2_000.0)

    assert len(curve.control_points) == 7
    assert curve.knot_vector == CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR
    assert len(curve.parameters) == len(curve.samples) == 65
    assert len(curve.curvatures_m_inv) == len(curve.radii_m)
    assert curve.samples[0] == pytest.approx(curve.control_points[0])
    assert curve.samples[-1] == pytest.approx(curve.control_points[-1])
    assert curve.minimum_radius_m == pytest.approx(min(curve.radii_m))
    assert curve.evidence.endpoint_g2_pass is True
    assert curve.evidence.internal_knot_c2_pass is True
    assert curve.evidence.full_route_g2_claimed is False
    assert curve.metadata()["control_point_count"] == 7


def test_first_three_and_last_three_controls_are_equally_spaced_and_collinear() -> None:
    curve = build_local_corner_curve((0.0, 0.0), (12_000.0, 0.0), (12_000.0, 9_000.0), 2_000.0)
    controls = curve.control_points
    incoming_spacing = (controls[1][0] - controls[0][0], controls[1][1] - controls[0][1])
    outgoing_spacing = (controls[5][0] - controls[4][0], controls[5][1] - controls[4][1])
    assert controls[2] == pytest.approx(
        (controls[1][0] + incoming_spacing[0], controls[1][1] + incoming_spacing[1])
    )
    assert controls[6] == pytest.approx(
        (controls[5][0] + outgoing_spacing[0], controls[5][1] + outgoing_spacing[1])
    )
    assert math.hypot(*incoming_spacing) == pytest.approx(math.hypot(*outgoing_spacing))
    assert curve.evidence.endpoint_curvature_abs_m_inv == pytest.approx((0.0, 0.0), abs=1.0e-12)


def test_endpoints_are_g2_compatible_with_adjacent_straight_segments() -> None:
    curve = build_local_corner_curve((0.0, 0.0), (10_000.0, 0.0), (10_000.0, 10_000.0), 2_000.0)
    start_first, start_second = curve.derivatives(0.0)
    end_first, end_second = curve.derivatives(1.0)
    assert start_first[1] == pytest.approx(0.0)
    assert start_first[0] > 0.0
    assert end_first[0] == pytest.approx(0.0)
    assert end_first[1] > 0.0
    assert abs(start_second[1]) == pytest.approx(0.0, abs=1.0e-9)
    assert abs(end_second[0]) == pytest.approx(0.0, abs=1.0e-9)
    assert curve.evidence.endpoint_g2_pass is True


@pytest.mark.parametrize(
    "bad_args",
    [
        ((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), 100.0),
        ((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), 100.0),
        ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), 600.0),
        ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), math.nan),
    ],
)
def test_constructor_rejects_degenerate_or_invalid_corners(bad_args: tuple[object, ...]) -> None:
    with pytest.raises(ValueError):
        build_local_corner_curve(*bad_args)


def test_radius_style_input_derives_trim_deterministically() -> None:
    direct = build_local_corner_curve((0.0, 0.0), (10_000.0, 0.0), (10_000.0, 10_000.0), 2_000.0)
    from_radius = build_local_corner_curve(
        (0.0, 0.0), (10_000.0, 0.0), (10_000.0, 10_000.0), radius_m=2_000.0
    )
    assert from_radius.trim_m == pytest.approx(2_000.0)
    for derived, expected in zip(from_radius.control_points, direct.control_points, strict=True):
        assert derived == pytest.approx(expected)
