"""Safety checks for the frozen-input edge-envelope diagnostic runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_edge_envelope_real.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_edge_envelope_real_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_real_runner_is_explicitly_diagnostic_and_keeps_limits_frozen() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-edge-envelope-real.v1"
    assert _MODULE.SEGMENTS == {"executable_0_6h": 6.0, "rolling_0_24h": 24.0}
    assert _MODULE.SEARCH_LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    assert "qualify_topological_lower_bound" in _SCRIPT.read_text(encoding="utf-8")
    assert '"dominance_policy": "disabled"' in _SCRIPT.read_text(encoding="utf-8")
    assert '"production_candidate_enabled": False' in _SCRIPT.read_text(encoding="utf-8")


def test_real_runner_cli_exposes_only_frozen_input_and_diagnostic_options() -> None:
    actions = _MODULE._parser()._actions
    names = {option for action in actions for option in action.option_strings}
    assert "--risk-window-commit" in names
    assert "--route-plan-set" in names
    assert "--segment" in names
    assert "--output-dir" in names
    assert "--resume" in names
    assert "--worker-timeout-seconds" in names
