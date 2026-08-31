from __future__ import annotations

import math

import pytest

from arctic_route_planning.research.route_smoothing_manoeuvring import (
    BASE_MIN_RADIUS_M,
    KNOT_TO_MPS,
    SyntheticManoeuvringEnvelope,
    evaluate_synthetic_manoeuvring_envelope,
)


@pytest.mark.parametrize("scenario", ["conservative", "nominal", "permissive"])
def test_synthetic_scenarios_have_explicit_uncalibrated_labels(scenario: str) -> None:
    envelope = SyntheticManoeuvringEnvelope.for_scenario(scenario)
    assert envelope.base_min_radius_m == BASE_MIN_RADIUS_M
    assert envelope.calibration_status == "SYNTHETIC_UNCALIBRATED"
    assert "SYNTHETIC_UNCALIBRATED" in envelope.labels
    assert "SYNTHETIC_ONLY" in envelope.labels
    assert envelope.as_dict()["units"]["curvature"] == "1/m"


def test_conservative_15_7_knots_dynamic_floor_is_about_3262_m() -> None:
    envelope = SyntheticManoeuvringEnvelope.conservative()
    speed_m_s = 15.7 * KNOT_TO_MPS
    expected_acceleration_floor = speed_m_s**2 / 0.02
    expected_yaw_floor = speed_m_s / math.radians(0.15)
    radius = envelope.minimum_allowed_radius_m(speed_m_s)

    assert radius == pytest.approx(
        max(BASE_MIN_RADIUS_M, expected_yaw_floor, expected_acceleration_floor)
    )
    assert radius == pytest.approx(3_262.0, abs=2.0)


@pytest.mark.parametrize("speed_knots", [6.0, 10.0, 13.5, 15.7])
def test_pointwise_evaluation_converts_knots_and_emits_evidence(speed_knots: float) -> None:
    envelope = SyntheticManoeuvringEnvelope.conservative()
    evidence = envelope.evaluate([1.0 / 50_000.0], [speed_knots], speed_unit="knots")

    assert evidence.accepted is True
    assert evidence.status == "PASS"
    assert evidence.speeds_m_s == pytest.approx((speed_knots * KNOT_TO_MPS,))
    assert evidence.yaw_rates_deg_s[0] == pytest.approx(
        math.degrees(evidence.yaw_rates_rad_s[0])
    )
    assert evidence.lateral_accelerations_m_s2[0] == pytest.approx(
        evidence.speeds_m_s[0] ** 2 / 50_000.0
    )
    assert evidence.evidence["calibration_status"] == "SYNTHETIC_UNCALIBRATED"
    assert evidence.production_qualified is False


def test_pointwise_dynamic_limit_rejects_curvature_and_reports_index() -> None:
    envelope = SyntheticManoeuvringEnvelope.conservative()
    speed_m_s = 15.7 * KNOT_TO_MPS
    floor = envelope.minimum_allowed_radius_m(speed_m_s)
    evidence = envelope.evaluate([1.0 / (floor * 0.9)], [speed_m_s])

    assert evidence.accepted is False
    assert evidence.status == "FAIL_CLOSED"
    assert evidence.violating_indices == (0,)
    assert evidence.failure_reasons == ("pointwise_manoeuvring_limit_exceeded",)


@pytest.mark.parametrize(
    "curvatures, speeds, unit",
    [
        ([0.01], [10.0, 11.0], "m/s"),
        ([math.nan], [10.0], "m/s"),
        ([-0.01], [10.0], "m/s"),
        ([0.01], [-10.0], "m/s"),
        ([0.01], [10.0], "mph"),
        ([], [], "m/s"),
        ([[0.01]], [10.0], "m/s"),
    ],
)
def test_invalid_shape_units_nonfinite_and_negative_values_fail_closed(
    curvatures: object,
    speeds: object,
    unit: str,
) -> None:
    evidence = SyntheticManoeuvringEnvelope.nominal().evaluate(curvatures, speeds, speed_unit=unit)

    assert evidence.accepted is False
    assert evidence.status == "FAIL_CLOSED"
    assert evidence.production_qualified is False
    assert evidence.evidence["labels"] == [
        "SYNTHETIC_UNCALIBRATED",
        "SYNTHETIC_ONLY",
        "NO_PRODUCTION_QUALIFICATION",
    ]


def test_functional_wrapper_uses_conservative_default_and_serializes_evidence() -> None:
    evidence = evaluate_synthetic_manoeuvring_envelope([0.0], [6.0], speed_unit="knots")

    assert evidence.accepted is True
    document = evidence.to_dict()
    assert document["research_eligible"] is True
    assert document["production_qualified"] is False
    assert document["labels"][0] == "SYNTHETIC_UNCALIBRATED"
