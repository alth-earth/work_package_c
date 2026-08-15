from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from arctic_route_planning.adapters.legacy_b import LegacyBArchiveAdapter
from arctic_route_planning.config import load_configuration
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.errors import LegacyDataError

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs"


def _configuration():
    return load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")


def test_legacy_adapter_requires_explicit_development_mode(tmp_path: Path) -> None:
    config = _configuration()
    with pytest.raises(LegacyDataError, match="development_mode=True"):
        LegacyBArchiveAdapter(
            archive_path=tmp_path / "retired-legacy-b.zip",
            scenario=config.scenario,
            vessel=config.vessel,
            run_context=create_development_run_context(
                config,
                source_kind="legacy_unverified",
                as_of_time=datetime(2026, 7, 31, tzinfo=UTC),
            ),
            generation_id=0,
            as_of_time=datetime(2026, 7, 31, tzinfo=UTC),
            development_mode=False,
            time_coordinate_semantics="valid_time",
            legacy_corridor_id="tromso_to_svalbard",
        )
