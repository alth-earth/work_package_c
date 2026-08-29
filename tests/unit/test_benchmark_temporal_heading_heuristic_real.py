"""Contract checks for the frozen real-input heading diagnostic wrapper."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_heading_heuristic_real.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_heading_real_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_real_runner_keeps_frozen_limits_and_diagnostic_boundary() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-heading-heuristic-real.v1"
    assert _MODULE.SEGMENTS == {"executable_0_6h": 6.0, "rolling_0_24h": 24.0}
    assert _MODULE.SEARCH_LIMITS["max_queue"] == 50_000
    assert '"dominance_policy": "disabled"' in _SCRIPT.read_text(encoding="utf-8")
    assert '"production_candidate_enabled": False' in _SCRIPT.read_text(encoding="utf-8")


def test_real_runner_cli_exposes_fixture_and_timeout_options() -> None:
    names = {option for action in _MODULE._parser()._actions for option in action.option_strings}
    assert {
        "--risk-window-commit",
        "--route-plan-set",
        "--segment",
        "--config-root",
        "--output-dir",
        "--resume",
        "--worker-timeout-seconds",
        "--cpu",
    } <= names
