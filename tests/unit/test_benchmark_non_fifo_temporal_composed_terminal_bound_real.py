"""Static contract checks for the M26 composed real-input diagnostic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_composed_terminal_bound_real.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m26_composed_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M26 composed runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)
_SOURCE = _SCRIPT.read_text(encoding="utf-8")


def test_schema_and_frozen_limits() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-nonfifo-composed-terminal-bound-real.v1"
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window",
            "--route-plan-set",
            "/tmp/routes",
            "--config-root",
            "/tmp/config",
            "--segment",
            "rolling_0_24h",
        ]
    )
    assert args.mode == "composed-bound"
    assert args.repetitions == 1
    assert args.worker_timeout_seconds == 900.0


def test_runner_composes_both_certificates_without_production_hooks() -> None:
    assert "state_bound_certificate=state_certificate" in _SOURCE
    assert "incumbent_bound_certificate=terminal_certificate" in _SOURCE
    assert '"production_candidate_enabled": False' in _SOURCE
    assert '"winter_enabled": False' in _SOURCE
    assert '"dominance_policy": "disabled"' in _SOURCE


def test_summary_never_authorizes_candidate_or_winter() -> None:
    identity = {"experiment_id": "m26-test", "repetitions": 1, "git": {"dirty": False}}
    case = {
        "repetition": 1,
        "status": "READY_FOR_COMPOSED_BOUND_REVIEW",
        "semantic_match": True,
        "state_bound_ok": True,
        "terminal_bound_ok": True,
        "selection_only": True,
        "frontier_complete": False,
        "state_bound_pruned": 1,
        "terminal_bound_pruned": 0,
    }
    cases = [
        dict(case, objective=objective)
        for objective in ("fastest", "low_risk", "recommended")
    ]
    summary = _RUNNER._summary(cases, identity, 0)
    assert summary["status"] == "NO_ADDITIONAL_TERMINAL_PRUNING"
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False
