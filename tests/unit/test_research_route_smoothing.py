"""Synthetic checks for the C research-only route smoothing sidecar geometry."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.research.route_smoothing import (
    CandidateDecision,
    RouteSmoothingPolicy,
    build_route_smoothing,
    build_route_smoothing_sidecar,
)
from arctic_route_planning.research.route_smoothing_baseline import (
    build_route_geometry_baseline,
)
from arctic_route_planning.research.route_smoothing_runner import build_research_sidecar


def _right_angle_route() -> list[dict[str, float]]:
    return [
        {"lon": 0.0, "lat": 0.0},
        {"lon": 0.2, "lat": 0.0},
        {"lon": 0.2, "lat": 0.2},
    ]


def _zigzag_route() -> list[dict[str, float]]:
    return [
        {"lon": 0.0, "lat": 0.0},
        {"lon": 0.2, "lat": 0.0},
        {"lon": 0.2, "lat": 0.2},
        {"lon": 0.4, "lat": 0.2},
    ]


def test_adaptive_geometry_chooses_largest_safe_radius_and_preserves_endpoints() -> None:
    source = _right_angle_route()
    result = build_route_smoothing(source)

    assert result.status == "ACCEPTED"
    assert result.geometry_only is True
    assert result.raw_points == ((0.0, 0.0), (0.2, 0.0), (0.2, 0.2))
    assert result.points[0] == (0.0, 0.0)
    assert result.points[-1] == pytest.approx((0.2, 0.2))
    assert len(result.points) > len(source)
    assert len(result.segments) == 1
    segment = result.segments[0]
    assert segment.radius_m == max(
        candidate.radius_m for candidate in result.segments
    )
    assert segment.minimum_radius_m >= segment.radius_m * 0.9
    assert result.curve_digest == build_route_smoothing(source).curve_digest
    assert source == _right_angle_route()


def test_validator_can_reject_large_radii_and_records_evidence() -> None:
    tested: list[float] = []

    def validator(samples: tuple[tuple[float, float], ...], radius_m: float) -> CandidateDecision:
        tested.append(radius_m)
        assert samples
        assert all(len(point) == 2 for point in samples)
        if radius_m > 5_000.0:
            return CandidateDecision(False, reason="synthetic_radius_limit")
        return CandidateDecision(True, evidence={"synthetic": "pass", "radius_m": radius_m})

    result = build_route_smoothing(_right_angle_route(), validator=validator)

    assert result.status == "ACCEPTED"
    assert result.geometry_only is False
    assert tested == sorted(tested, reverse=True)
    assert result.segments[0].radius_m <= 5_000.0
    assert result.segments[0].validator_evidence == {
        "synthetic": "pass",
        "radius_m": result.segments[0].radius_m,
    }


def test_adjacent_corners_reduce_the_later_radius_instead_of_overlapping() -> None:
    policy = RouteSmoothingPolicy(maximum_overlap_fraction=0.8)
    result = build_route_smoothing(_zigzag_route(), policy=policy)

    assert result.status == "ACCEPTED"
    assert [segment.corner_index for segment in result.segments] == [1, 2]
    first, second = result.segments
    shared_length_m = 6_371_008.8 * math.radians(0.2)
    assert first.trim_m + second.trim_m <= shared_length_m * policy.maximum_overlap_fraction
    assert second.radius_m < first.radius_m
    assert not any(
        item.get("reason") == "adjacent_curve_overlap" for item in result.rejected_corners
    )


def test_invalid_and_unsafe_inputs_fail_closed_without_fabricating_geometry() -> None:
    invalid = build_route_smoothing(None)
    assert invalid.status == "FALLBACK"
    assert invalid.points == ()
    assert invalid.raw_points == ()
    assert invalid.fallback_reason == "invalid_points"

    rejected = build_route_smoothing(
        _right_angle_route(),
        validator=lambda _samples, _radius: CandidateDecision(
            False, reason="synthetic_hard_mask"
        ),
    )
    assert rejected.status == "FALLBACK"
    assert rejected.points == tuple((item["lon"], item["lat"]) for item in _right_angle_route())
    assert rejected.geometry_only is False
    assert rejected.fallback_reason == "all_curves_rejected"
    assert rejected.rejected_corners[0]["reason"] == "synthetic_hard_mask"


def test_no_corner_falls_back_to_the_original_route() -> None:
    result = build_route_smoothing(
        [
            {"lon": 0.0, "lat": 0.0},
            {"lon": 0.2, "lat": 0.0},
            {"lon": 0.4, "lat": 0.0},
        ]
    )

    assert result.status == "FALLBACK"
    assert result.fallback_reason == "no_eligible_corner"
    assert result.points == result.raw_points


def test_validator_errors_fail_closed_instead_of_publishing_a_curve() -> None:
    def broken_validator(_samples, _radius):
        raise RuntimeError("missing safety evidence")

    result = build_route_smoothing(_right_angle_route(), validator=broken_validator)

    assert result.status == "FALLBACK"
    assert result.fallback_reason == "validator_error"
    assert result.geometry_only is False


def test_sidecar_binds_authoritative_route_and_assigns_monotonic_eta_samples() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    route = {
        "plan_id": "synthetic-route-1",
        "waypoints": [
            {"longitude": 0.0, "latitude": 0.0, "eta": start},
            {"longitude": 0.2, "latitude": 0.0, "eta": start + timedelta(hours=1)},
            {"longitude": 0.2, "latitude": 0.2, "eta": start + timedelta(hours=2)},
        ],
    }

    sidecar = build_route_smoothing_sidecar(
        route,
        experiment_id="c.route-smoothing.synthetic.v1",
        input_identity={"source": "unit-test"},
    )

    assert sidecar["status"] == "ACCEPTED"
    assert sidecar["research_only"] is True
    assert sidecar["authoritative_route"]["route_id"] == "synthetic-route-1"
    assert sidecar["authoritative_route"]["waypoint_count"] == 3
    assert sidecar["parameterization"]["anchor_count"] == 3
    assert sidecar["motion_samples"][0]["eta"] == "2026-01-01T00:00:00Z"
    assert sidecar["motion_samples"][-1]["eta"] == "2026-01-01T02:00:00Z"
    eta = [item["eta"] for item in sidecar["motion_samples"]]
    assert eta == sorted(eta)
    assert sidecar["validation"]["production_qualified"] is False
    assert sidecar["sidecar_digest"] == build_route_smoothing_sidecar(
        route,
        experiment_id="c.route-smoothing.synthetic.v1",
        input_identity={"source": "unit-test"},
    )["sidecar_digest"]


def test_sidecar_does_not_expose_geometry_as_safe_when_validator_rejects_it() -> None:
    route = {
        "route_id": "unsafe-synthetic",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": "2026-01-01T00:00:00Z"},
            {"lon": 0.2, "lat": 0.0, "eta": "2026-01-01T01:00:00Z"},
            {"lon": 0.2, "lat": 0.2, "eta": "2026-01-01T02:00:00Z"},
        ],
    }

    sidecar = build_route_smoothing_sidecar(
        route,
        experiment_id="c.route-smoothing.rejected.v1",
        validator=lambda _samples, _radius: CandidateDecision(
            False, reason="unknown_risk_coverage"
        ),
    )

    assert sidecar["status"] == "FALLBACK"
    assert sidecar["applied"] is False
    assert sidecar["fallback_reason"] == "all_curves_rejected"
    assert sidecar["motion_samples"] == []
    assert sidecar["validation"]["coverage_complete"] is False


def test_fallback_sidecars_are_digest_bound() -> None:
    sidecar = build_route_smoothing_sidecar(
        {"route_id": "invalid-route", "waypoints": "not-a-list"},
        experiment_id="c.route-smoothing.invalid.v1",
    )

    assert sidecar["status"] == "FALLBACK"
    declared_digest = sidecar.pop("sidecar_digest")
    encoded = json.dumps(
        sidecar,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert declared_digest == hashlib.sha256(encoded).hexdigest()
    assert declared_digest == build_route_smoothing_sidecar(
        {"route_id": "invalid-route", "waypoints": "not-a-list"},
        experiment_id="c.route-smoothing.invalid.v1",
    )["sidecar_digest"]


def test_research_runner_records_three_radius_scenarios() -> None:
    route = {
        "plan_id": "synthetic-envelope-route",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": "2026-01-01T00:00:00Z"},
            {"lon": 0.2, "lat": 0.0, "eta": "2026-01-01T01:00:00Z"},
            {"lon": 0.2, "lat": 0.2, "eta": "2026-01-01T02:00:00Z"},
        ],
    }

    sidecar = build_research_sidecar(
        route,
        experiment_id="c.route-smoothing.envelope.synthetic.v1",
    )

    scenarios = sidecar["radius_sensitivity"]["scenarios"]
    assert [item["minimum_radius_m"] for item in scenarios] == [1_000.0, 2_000.0, 4_000.0]
    assert all(item["status"] == "ACCEPTED" for item in scenarios)
    assert all(item["selected_radius_m"] for item in scenarios)


def test_r02_baseline_reports_heading_discontinuity_without_smoothing() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    route = {
        "plan_id": "baseline-route",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": start},
            {"lon": 0.2, "lat": 0.0, "eta": start + timedelta(hours=1)},
            {"lon": 0.2, "lat": 0.2, "eta": start + timedelta(hours=2)},
        ],
    }

    baseline = build_route_geometry_baseline(route)

    assert baseline["status"] == "PASS"
    assert baseline["corner_count"] == 1
    assert baseline["eligible_corner_count"] == 1
    assert baseline["corners"][0]["turn_angle_deg"] == pytest.approx(90.0)
    assert baseline["corners"][0]["classification"] == "CORNER_PRESENT"
    assert baseline["angle_bins"] == {"45-90deg": 1}
    assert baseline["baseline_digest"]
