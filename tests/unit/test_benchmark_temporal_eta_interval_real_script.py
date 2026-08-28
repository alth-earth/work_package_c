"""Contract checks for the real-input ETA interval qualification runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_interval_real.py"
_SPEC = importlib.util.spec_from_file_location("c_real_eta_interval_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_real_runner_schema_and_frozen_segments_are_default_off() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-eta-proof-real.v1"
    assert set(_MODULE.SEGMENTS) == {"executable_0_6h", "rolling_0_24h"}
    assert _MODULE.BASE_PROBE_MINUTES == 15
    assert _MODULE.FIFO_TOLERANCE_SECONDS == 1.0


def test_real_runner_never_declares_dominance_authorization() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "dominance_policy\": \"disabled\"" in source
    assert "certified_only" not in source
    assert '"dominance_pruned": 0' in source
