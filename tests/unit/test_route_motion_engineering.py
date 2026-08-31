from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from arctic_route_planning.motion import (
    EngineeringRouteMotionProfile,
    evaluate_continuous_raster_model_corridor,
)


def _metadata() -> dict[str, object]:
    return {
        "coordinate_frame": "local_equirectangular_east_north_m",
        "origin_x_m": 0.0,
        "origin_y_m": 0.0,
        "cell_size_m": 1_000.0,
        "rows": 5,
        "cols": 5,
        "coverage_complete": True,
        "raster_digest": "a" * 64,
    }


def _cells() -> dict[tuple[int, int], dict[str, object]]:
    return {
        (row, column): {"status": "SEA", "coverage_complete": True}
        for row in range(5)
        for column in range(5)
    }


def test_formula_profile_is_versioned_and_keeps_engineering_claim_boundary() -> None:
    profile = EngineeringRouteMotionProfile()

    assert profile.minimum_radius_m(10.0) == 2_000.0
    expected_at_maximum = max(
        2_000.0,
        15.7 * 0.5144444444444445 / math.radians(0.15),
        (15.7 * 0.5144444444444445) ** 2 / 0.02,
    )
    assert profile.minimum_radius_m(15.7) == expected_at_maximum
    assert profile.corridor_buffer_m(
        position_error_m=10.0,
        transform_error_m=2.0,
        chord_error_m=1.0,
    ) == 500.0
    assert profile.to_dict()["real_vessel_calibrated"] is False
    assert len(profile.digest) == 64
    assert EngineeringRouteMotionProfile.from_dict(profile.to_dict()) == profile
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" /
         "route-motion-vessel-profile-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        profile.to_dict()
    )
    tampered = profile.to_dict()
    tampered["maximum_yaw_rate_deg_s"] = 0.2
    with pytest.raises(ValueError, match="digest"):
        EngineeringRouteMotionProfile.from_dict(tampered)


def test_continuous_raster_model_proof_is_scoped_and_fail_closed() -> None:
    hull = [[[2_100.0, 2_100.0], [2_300.0, 2_100.0], [2_300.0, 2_300.0]]]
    cells = _cells()

    accepted = evaluate_continuous_raster_model_corridor(
        _metadata(), cells, hull, expansion_m=500.0
    )
    assert accepted["accepted"] is True
    assert accepted["continuous_containment_proved"] is True
    assert accepted["continuous_containment_scope"] == (
        "CONTINUOUS_IN_DECLARED_RASTER_MODEL"
    )
    assert accepted["navigation_grade"] is False
    assert accepted["ukc_checked"] is False

    cells[(2, 2)] = {"status": "UNKNOWN", "coverage_complete": True}
    rejected = evaluate_continuous_raster_model_corridor(
        _metadata(), cells, hull, expansion_m=500.0
    )
    assert rejected["accepted"] is False
    assert rejected["continuous_containment_proved"] is False
    assert [2, 2] in rejected["unknown_cells"]


def test_continuous_raster_model_rejects_extent_and_identity_gaps() -> None:
    outside = evaluate_continuous_raster_model_corridor(
        _metadata(),
        _cells(),
        [[[100.0, 100.0], [200.0, 200.0]]],
        expansion_m=500.0,
    )
    assert outside["reason"] == "expanded_hull_outside_raster_extent"

    missing_digest = _metadata()
    del missing_digest["raster_digest"]
    rejected = evaluate_continuous_raster_model_corridor(
        missing_digest,
        _cells(),
        [[[2_100.0, 2_100.0], [2_300.0, 2_300.0]]],
        expansion_m=500.0,
    )
    assert rejected["reason"] == "missing_raster_digest"


def test_continuous_raster_model_includes_both_cells_on_closed_boundary() -> None:
    cells = _cells()
    cells[(2, 1)] = {"status": "LAND", "coverage_complete": True}
    result = evaluate_continuous_raster_model_corridor(
        _metadata(),
        cells,
        [[[2_000.0, 2_100.0], [2_000.0, 2_200.0]]],
        expansion_m=0.0,
    )

    assert result["accepted"] is False
    assert [2, 1] in result["land_cells"]
    assert [2, 2] in result["enumerated_cells"]
