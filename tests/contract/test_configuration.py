from __future__ import annotations

from pathlib import Path

import pytest

from arctic_route_planning.config import load_configuration
from arctic_route_planning.domain.models import CalibrationStatus
from arctic_route_planning.errors import ContractError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


@pytest.mark.parametrize(
    ("scenario_id", "corridor_id"),
    [
        (
            "demo_offshore_murmansk_to_offshore_dikson_v1",
            "offshore_murmansk_to_offshore_dikson",
        ),
        ("demo_tromso_to_svalbard_v1", "tromso_to_svalbard"),
    ],
)
def test_demo_configuration_is_valid_and_content_addressed(
    scenario_id: str, corridor_id: str
) -> None:
    first = load_configuration(CONFIG_ROOT, scenario_id)
    second = load_configuration(CONFIG_ROOT, scenario_id)

    assert first.scenario.corridor_id == corridor_id
    assert first.vessel.vessel_profile_id == "demo_bulk_carrier_v1"
    assert first.vessel.calibration_status is CalibrationStatus.DEMO_UNVALIDATED
    assert "not calibrated" in first.vessel.source_notes.lower()
    assert len(first.config_digest) == 64
    assert first.config_digest == second.config_digest
    assert not hasattr(first.planner, "risk_speed_penalty")


def test_config_id_cannot_escape_config_root() -> None:
    with pytest.raises(ContractError, match="不安全"):
        load_configuration(CONFIG_ROOT, "../secrets")
