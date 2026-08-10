from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from arctic_route_planning.adapters.legacy_b import LegacyBArchiveAdapter
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts.models import ProvenanceKind
from arctic_route_planning.errors import LegacyDataError

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs"
DELIVERY_ARCHIVE = Path("/mnt/c/Users/asd233/Desktop/挑战杯/挑战/交付包.zip")


def _configuration():
    return load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")


def test_legacy_adapter_requires_explicit_development_mode() -> None:
    config = _configuration()
    with pytest.raises(LegacyDataError, match="development_mode=True"):
        LegacyBArchiveAdapter(
            archive_path=DELIVERY_ARCHIVE,
            scenario=config.scenario,
            vessel=config.vessel,
            config_digest=config.config_digest,
            generation_id=0,
            as_of_time=datetime(2026, 7, 31, tzinfo=UTC),
            development_mode=False,
            time_coordinate_semantics="valid_time",
        )


@pytest.mark.external_artifact
@pytest.mark.skipif(not DELIVERY_ARCHIVE.is_file(), reason="user-provided delivery archive absent")
def test_nested_delivery_archive_maps_only_comprehensive_risk_and_land_mask() -> None:
    config = _configuration()
    adapter = LegacyBArchiveAdapter(
        archive_path=DELIVERY_ARCHIVE,
        scenario=config.scenario,
        vessel=config.vessel,
        config_digest=config.config_digest,
        generation_id=2,
        as_of_time=datetime(2026, 7, 31, tzinfo=UTC),
        generated_at=datetime(2026, 8, 9, tzinfo=UTC),
        development_mode=True,
        time_coordinate_semantics="valid_time",
        dataset_variant="7days",
    )

    frames = adapter.load()

    assert len(frames) == 11
    assert adapter.load() is frames
    assert adapter.inner_member_name is not None
    assert "comprehensive_risk_tromso_to_svalbard_7days.nc" in adapter.inner_member_name
    assert "route_cost_grid" not in adapter.inner_member_name
    first = frames[0]
    assert first.provenance is ProvenanceKind.LEGACY_UNVERIFIED
    assert first.source_summary[0].issue_time is None
    assert first.payload.attrs["coordinate_snap_applied"] is False
    assert first.payload.attrs["speed_factor_defaulted"] is True
    assert np.all(first.payload["environment_speed_factor"].values == np.float32(1.0))
    assert float(first.payload["confidence"].max()) == pytest.approx(0.20)
    np.testing.assert_array_equal(
        first.payload["hard_mask"].values,
        np.isnan(first.payload["risk_score"].values),
    )
