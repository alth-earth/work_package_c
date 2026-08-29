"""Boundary checks for the frozen-input environmental envelope runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_environment_envelope_real.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_environment_real_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_real_runner_is_diagnostic_and_keeps_resource_limits_frozen() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-environment-speed-envelope-real.v1"
    assert _MODULE.SEGMENTS == {"executable_0_6h": 6.0, "rolling_0_24h": 24.0}
    assert _MODULE.SEARCH_LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    text = _SCRIPT.read_text(encoding="utf-8")
    assert '"dominance_policy": "disabled"' in text
    assert '"production_candidate_enabled": False' in text
    assert "qualify_environmental_speed_envelope" in text


def test_real_runner_cli_exposes_frozen_fixture_and_timeout_options() -> None:
    names = {option for action in _MODULE._parser()._actions for option in action.option_strings}
    assert {
        "--risk-window-commit",
        "--route-plan-set",
        "--segment",
        "--config-root",
        "--output-dir",
        "--objective",
        "--resume",
        "--worker-timeout-seconds",
        "--cpu",
    } <= names
