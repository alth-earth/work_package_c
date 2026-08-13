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
from arctic_route_planning.contracts.windows import HOURLY_RISK_INTERVAL, RiskWindowQuery
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.errors import ContextMismatchError, ContractError, RiskCoverageError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


def _configuration():
    return load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")


def _fixture(config, **kwargs):
    return FixtureRiskSource(
        scenario=config.scenario,
        corridor=config.corridor,
        vessel=config.vessel,
        run_context=create_development_run_context(config, source_kind="synthetic"),
        **kwargs,
    )


def test_fixture_source_is_deterministic_and_contract_valid() -> None:
    config = _configuration()
    first = _fixture(config, frame_count=3, shape=(5, 7))
    second = _fixture(config, frame_count=3, shape=(5, 7))

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
    fixture = _fixture(config, frame_count=2, shape=(4, 4))
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
        run_id=original.run_id,
        scenario_id=original.scenario_id,
        corridor_id=original.corridor_id,
        generation_id=original.generation_id,
        vessel_profile_id=original.vessel_profile_id,
        config_digest=original.config_digest,
        model_config_digest=original.model_config_digest,
        as_of=original.as_of_time,
    )
    new_view = source.get_window(
        original.valid_time,
        original.valid_time,
        run_id=original.run_id,
        scenario_id=original.scenario_id,
        corridor_id=original.corridor_id,
        generation_id=original.generation_id,
        vessel_profile_id=original.vessel_profile_id,
        config_digest=original.config_digest,
        model_config_digest=original.model_config_digest,
        as_of=revised.as_of_time,
    )

    assert old_view == (original,)
    assert new_view == (revised,)


def test_in_memory_source_publish_is_content_idempotent_and_rejects_collision() -> None:
    config = _configuration()
    original = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]
    equivalent = replace(original)
    collision = replace(original, model_version="different-model")
    source = InMemoryRiskSource()

    source.publish(original)
    source.publish(equivalent)
    with pytest.raises(ContractError, match="不同内容"):
        source.publish(collision)


def test_get_window_rejects_corridor_mismatch_before_planning() -> None:
    config = _configuration()
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]
    source = InMemoryRiskSource()
    source.publish(frame)

    with pytest.raises(ContextMismatchError, match="corridor_id"):
        source.get_window(
            frame.valid_time,
            frame.valid_time,
            run_id=frame.run_id,
            scenario_id=frame.scenario_id,
            corridor_id="wrong-corridor",
            generation_id=frame.generation_id,
            vessel_profile_id=frame.vessel_profile_id,
            config_digest=frame.config_digest,
            model_config_digest=frame.model_config_digest,
            as_of=frame.as_of_time,
        )


def test_in_memory_source_reset_discards_other_generations() -> None:
    config = _configuration()
    old = _fixture(config, generation_id=0, frame_count=2, shape=(4, 4)).frames[0]
    new = _fixture(config, generation_id=1, frame_count=2, shape=(4, 4)).frames[0]
    source = InMemoryRiskSource()
    source.publish(old)
    source.publish(new)

    source.reset_to_generation(1)

    assert (
        source.latest_before(
            old.valid_time,
            run_id=old.run_id,
            scenario_id=old.scenario_id,
            corridor_id=old.corridor_id,
            generation_id=0,
            vessel_profile_id=old.vessel_profile_id,
            config_digest=old.config_digest,
            model_config_digest=old.model_config_digest,
            as_of=old.as_of_time,
        )
        is None
    )
    assert (
        source.latest_before(
            new.valid_time,
            run_id=new.run_id,
            scenario_id=new.scenario_id,
            corridor_id=new.corridor_id,
            generation_id=1,
            vessel_profile_id=new.vessel_profile_id,
            config_digest=new.config_digest,
            model_config_digest=new.model_config_digest,
            as_of=new.as_of_time,
        )
        == new
    )


def test_risk_frame_rejects_non_2d_payload() -> None:
    config = _configuration()
    fixture = _fixture(config, frame_count=2, shape=(4, 4))
    frame = fixture.frames[0]
    broken = frame.payload.expand_dims(member=[0])

    with pytest.raises(ContractError, match=r"二维网格|未声明坐标"):
        replace(frame, payload=broken)


def test_risk_frame_rejects_invalid_environment_speed_factor() -> None:
    config = _configuration()
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]
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
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]
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


@pytest.mark.parametrize("field", ("data_id", "issue_time", "valid_time", "checksum"))
def test_formal_frame_requires_complete_source_identity(field: str) -> None:
    config = _configuration()
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]
    incomplete_source = replace(frame.source_summary[0], **{field: None})

    with pytest.raises(ContractError, match=field):
        replace(
            frame,
            source_summary=(incomplete_source,),
            provenance="formal",
        )


def test_source_reference_rejects_blank_data_id() -> None:
    config = _configuration()
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]

    with pytest.raises(ContractError, match="data_id"):
        replace(frame.source_summary[0], data_id="   ")


def test_formal_frame_requires_declared_environment_effect() -> None:
    config = _configuration()
    frame = _fixture(config, frame_count=2, shape=(4, 4)).frames[0]

    with pytest.raises(ContractError, match="environment_speed_factor"):
        replace(
            frame,
            payload=frame.payload.drop_vars("environment_speed_factor"),
            provenance=ProvenanceKind.FORMAL,
        )


def _query(frames):
    first = frames[0]
    return RiskWindowQuery(
        start=first.valid_time,
        end=frames[-1].valid_time,
        interval=HOURLY_RISK_INTERVAL,
        run_id=first.run_id,
        scenario_id=first.scenario_id,
        corridor_id=first.corridor_id,
        generation_id=first.generation_id,
        vessel_profile_id=first.vessel_profile_id,
        config_digest=first.config_digest,
        model_config_digest=first.model_config_digest,
        as_of=first.as_of_time,
    )


def test_in_memory_source_only_returns_explicit_exact_commit() -> None:
    config = _configuration()
    frames = _fixture(config, frame_count=3, shape=(4, 4)).frames
    source = InMemoryRiskSource()
    for frame in frames:
        source.publish(frame)
    query = _query(frames)

    with pytest.raises(ContextMismatchError, match="已提交"):
        source.get_committed_window(query)
    committed = source.commit_window(query)

    assert source.get_committed_window(query) is committed
    assert committed.start == frames[0].valid_time
    assert committed.end == frames[-1].valid_time
    assert committed.interval == timedelta(hours=1)
    assert committed.count == 3
    assert committed.commit_id == f"risk-window-sha256-{committed.content_digest}"


def test_commit_rejects_missing_hour_in_closed_window() -> None:
    config = _configuration()
    frames = _fixture(config, frame_count=3, shape=(4, 4)).frames
    source = InMemoryRiskSource()
    source.publish(frames[0])
    source.publish(frames[2])

    with pytest.raises(RiskCoverageError, match=r"frames 数量|逐点覆盖"):
        source.commit_window(_query(frames))
