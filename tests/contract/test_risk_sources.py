from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

from arctic_route_planning.adapters.fixture import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts.models import ProvenanceKind
from arctic_route_planning.contracts.sources import InMemoryRiskSource
from arctic_route_planning.errors import ContractError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


def _configuration():
    return load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")


def test_fixture_source_is_deterministic_and_contract_valid() -> None:
    config = _configuration()
    first = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=3,
        shape=(5, 7),
    )
    second = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=3,
        shape=(5, 7),
    )

    assert [frame.risk_id for frame in first.frames] == [frame.risk_id for frame in second.frames]
    assert first.frames[0].provenance is ProvenanceKind.SYNTHETIC
    np.testing.assert_array_equal(
        first.frames[2].payload["risk_score"], second.frames[2].payload["risk_score"]
    )
    assert first.frames[0].payload["risk_score"].dims == ("latitude", "longitude")
    assert float(first.frames[0].payload["environment_speed_factor"].min()) > 0
    assert float(first.frames[0].payload["environment_speed_factor"].max()) <= 1
    assert not first.frames[0].payload["risk_score"].values.flags.writeable


def test_in_memory_source_selects_latest_as_of_revision() -> None:
    config = _configuration()
    fixture = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=2,
        shape=(4, 4),
    )
    original = fixture.frames[0]
    revised = replace(
        original,
        risk_id=f"{original.risk_id}-revision",
        as_of_time=original.as_of_time + timedelta(minutes=30),
        generated_at=original.generated_at + timedelta(minutes=30),
    )
    source = InMemoryRiskSource()
    source.publish(original)
    source.publish(revised)

    old_view = source.get_window(
        original.valid_time,
        original.valid_time,
        scenario_id=original.scenario_id,
        generation_id=original.generation_id,
        vessel_profile_id=original.vessel_profile_id,
        config_digest=original.config_digest,
        as_of=original.as_of_time,
    )
    new_view = source.get_window(
        original.valid_time,
        original.valid_time,
        scenario_id=original.scenario_id,
        generation_id=original.generation_id,
        vessel_profile_id=original.vessel_profile_id,
        config_digest=original.config_digest,
        as_of=revised.as_of_time,
    )

    assert old_view == (original,)
    assert new_view == (revised,)


def test_in_memory_source_reset_discards_other_generations() -> None:
    config = _configuration()
    old = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        generation_id=0,
        frame_count=2,
        shape=(4, 4),
    ).frames[0]
    new = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        generation_id=1,
        frame_count=2,
        shape=(4, 4),
    ).frames[0]
    source = InMemoryRiskSource()
    source.publish(old)
    source.publish(new)

    source.reset_to_generation(1)

    assert (
        source.latest_before(
            old.valid_time,
            scenario_id=old.scenario_id,
            generation_id=0,
            vessel_profile_id=old.vessel_profile_id,
            config_digest=old.config_digest,
            as_of=old.as_of_time,
        )
        is None
    )
    assert (
        source.latest_before(
            new.valid_time,
            scenario_id=new.scenario_id,
            generation_id=1,
            vessel_profile_id=new.vessel_profile_id,
            config_digest=new.config_digest,
            as_of=new.as_of_time,
        )
        == new
    )


def test_risk_frame_rejects_non_2d_payload() -> None:
    config = _configuration()
    fixture = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=2,
        shape=(4, 4),
    )
    frame = fixture.frames[0]
    broken = frame.payload.expand_dims(member=[0])

    with pytest.raises(ContractError, match="二维网格"):
        replace(frame, payload=broken)


def test_risk_frame_rejects_invalid_environment_speed_factor() -> None:
    config = _configuration()
    frame = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=2,
        shape=(4, 4),
    ).frames[0]
    broken = frame.payload.assign(
        environment_speed_factor=(
            ("latitude", "longitude"),
            np.zeros((4, 4), dtype=np.float32),
        )
    )

    with pytest.raises(ContractError, match="environment_speed_factor"):
        replace(frame, payload=broken)


def test_formal_frame_rejects_source_published_after_as_of() -> None:
    config = _configuration()
    frame = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=2,
        shape=(4, 4),
    ).frames[0]
    future_source = replace(
        frame.source_summary[0],
        issue_time=frame.as_of_time + timedelta(seconds=1),
    )

    with pytest.raises(ContractError, match="as_of_time"):
        replace(
            frame,
            source_summary=(future_source,),
            provenance=ProvenanceKind.FORMAL,
        )


def test_formal_frame_requires_declared_environment_effect() -> None:
    config = _configuration()
    frame = FixtureRiskSource(
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        frame_count=2,
        shape=(4, 4),
    ).frames[0]

    with pytest.raises(ContractError, match="environment_speed_factor"):
        replace(
            frame,
            payload=frame.payload.drop_vars("environment_speed_factor"),
            provenance=ProvenanceKind.FORMAL,
        )
