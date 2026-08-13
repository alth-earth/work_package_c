from __future__ import annotations

from pathlib import Path

import pytest

from arctic_route_planning.adapters import adapt_risk_frame_v1
from arctic_route_planning.adapters.fixture import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.errors import LegacyDataError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


def test_v1_risk_adapter_requires_explicit_legacy_downgrade() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    frame = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=create_development_run_context(configuration, source_kind="synthetic"),
        frame_count=2,
        shape=(3, 3),
    ).frames[0]
    document = {
        "schema_version": "bc.risk-frame.v1",
        "risk_id": "old-risk",
        "scenario_id": frame.scenario_id,
        "corridor_id": frame.corridor_id,
        "vessel_profile_id": frame.vessel_profile_id,
        "config_digest": frame.config_digest,
        "generation_id": frame.generation_id,
        "valid_time": "2026-01-01T00:00:00Z",
        "as_of_time": "2026-01-01T00:00:00Z",
        "generated_at": "2026-01-01T00:00:00Z",
        "model_version": "old-model",
    }

    with pytest.raises(LegacyDataError, match="legacy_unverified"):
        adapt_risk_frame_v1(
            document,
            run_id="run-legacy",
            model_config_digest="1" * 64,
            payload=frame.payload,
            acknowledge_legacy_unverified=False,
        )

    migrated = adapt_risk_frame_v1(
        document,
        run_id="run-legacy",
        model_config_digest="1" * 64,
        payload=frame.payload,
        acknowledge_legacy_unverified=True,
    )
    assert migrated.schema_version == "bc.risk-frame.v2"
    assert migrated.provenance.value == "legacy_unverified"
