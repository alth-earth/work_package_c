from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from arctic_route_contracts import CalibrationStatus
from arctic_route_contracts import ContractError as SharedContractError

from arctic_route_planning.config import load_configuration
from arctic_route_planning.errors import ContractError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


@pytest.mark.parametrize(
    ("scenario_id", "corridor_id", "destination"),
    [
        (
            "murmansk_dikson_july_2026_retrospective_v1",
            "offshore_murmansk_to_offshore_dikson",
            (80.40, 73.55),
        ),
        (
            "tromso_isfjorden_july_2026_retrospective_v1",
            "tromso_to_isfjorden_outer",
            (13.00, 78.15),
        ),
    ],
)
def test_shared_configuration_and_c_digests_are_stable(
    scenario_id: str,
    corridor_id: str,
    destination: tuple[float, float],
) -> None:
    first = load_configuration(CONFIG_ROOT, scenario_id)
    second = load_configuration(CONFIG_ROOT, scenario_id)

    assert first.scenario.corridor_id == corridor_id
    actual_destination = (
        first.corridor.destination.longitude,
        first.corridor.destination.latitude,
    )
    assert actual_destination == destination
    assert first.vessel.vessel_profile_id == "nordic_odyssey_reference_v1"
    assert first.vessel.calibration_status is CalibrationStatus.PUBLIC_REFERENCE_UNVALIDATED
    assert first.planner_config_digest == second.planner_config_digest
    assert first.planner.max_search_hours == 216
    assert not first.vessel_model.bathymetry_hard_constraint_enabled
    assert set(first.scenario.required_data_types) == {
        "land_sea_mask",
        "ocean_current",
        "sea_ice_concentration",
        "sea_ice_drift",
        "sea_ice_edge",
        "sea_ice_thickness",
        "sea_ice_type",
        "temperature",
        "visibility",
        "water_level",
        "wave",
        "wind_field",
    }
    assert first.scenario.optional_data_types == (
        "bathymetry",
        "long_term_restricted_area",
    )


def test_longyearbyen_is_reference_not_destination() -> None:
    configuration = load_configuration(
        CONFIG_ROOT,
        "tromso_isfjorden_july_2026_retrospective_v1",
    )

    reference = configuration.corridor.reference_points[0]
    assert reference.reference_id == "longyearbyen_ais_reference"
    assert reference.excluded_from_route_optimization
    assert reference.location != configuration.corridor.destination


def test_frozen_template_requires_explicit_start_and_materializes() -> None:
    scenario_id = "tromso_isfjorden_frozen_forecast_template_v1"
    with pytest.raises(ContractError, match="simulation_start"):
        load_configuration(CONFIG_ROOT, scenario_id)

    configuration = load_configuration(
        CONFIG_ROOT,
        scenario_id,
        simulation_start=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert not configuration.scenario.is_template
    assert configuration.scenario.horizon_hours == 96

    route_specific = load_configuration(
        CONFIG_ROOT,
        scenario_id,
        simulation_start=datetime(2026, 8, 12, tzinfo=UTC),
        candidate_route_distance_nm=1000,
    )
    assert route_specific.scenario.horizon_hours == 144
    assert route_specific.scenario.scenario_id.endswith("_h144_v1")


def test_frozen_candidate_beyond_shared_cap_fails_before_planning() -> None:
    with pytest.raises(SharedContractError, match="forecast_coverage_insufficient"):
        load_configuration(
            CONFIG_ROOT,
            "murmansk_dikson_frozen_forecast_template_v1",
            simulation_start=datetime(2026, 8, 12, tzinfo=UTC),
            candidate_route_distance_nm=3000,
        )


def test_config_id_cannot_escape_shared_root() -> None:
    with pytest.raises(SharedContractError, match="unsafe"):
        load_configuration(CONFIG_ROOT, "../secrets")
