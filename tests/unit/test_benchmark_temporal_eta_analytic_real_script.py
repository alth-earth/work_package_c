"""Contract checks for the real analytic ETA qualification sidecar."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_analytic_real.py"
_SPEC = importlib.util.spec_from_file_location("c_real_analytic_eta_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_is_a_disabled_dominance_sidecar() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-eta-analytic-real.v1"
    assert {
        "executable_0_6h": _MODULE.timedelta(hours=6),
        "rolling_0_24h": _MODULE.timedelta(hours=24),
    } == _MODULE.SEGMENTS
    assert _MODULE.OBJECTIVES == ("fastest", "low_risk", "recommended")
    assert _MODULE.FIFO_TOLERANCE_SECONDS == 1.0
    assert _MODULE.BASE_PROBE_MINUTES == 15


def test_parser_exposes_identity_and_timeout_controls() -> None:
    parser = _MODULE._parser()
    args = parser.parse_args(
        [
            "--risk-window-commit",
            "/tmp/window.json",
            "--route-plan-set",
            "/tmp/route.json",
            "--segment",
            "executable_0_6h",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/out",
            "--objective",
            "fastest",
            "--cpu",
            "3",
        ]
    )
    assert args.mode == "both"
    assert args.objective == "fastest"
    assert args.worker_timeout_seconds == 900.0
    assert args.cpu == 3


def test_serialization_preserves_fail_closed_marker() -> None:
    class Evidence:
        status = type("Status", (), {"value": "UNCERTAIN_NO_INTERVAL_PROOF"})()
        reason = "evaluator_not_certified"
        digest = "evidence"
        coverage_complete = True
        evaluator_certified = False
        continuity_certified = True
        contraction_bound = 0.2
        partition_boundaries = ()
        edge_factor_lower = 0.8
        edge_factor_upper = 1.0
        speed_lower_knots = 4.0
        speed_upper_knots = 5.0
        edge_distance_km = 1.0
        fifo_status = "FIFO_UNCERTAIN"
        permits_dominance = False
        interval_samples = ()
        analytic_certificate = None

    result = _MODULE._serialize_evidence(Evidence())
    assert result["evaluator_certified"] is False
    assert result["permits_dominance"] is False
    assert result["analytic_certificate"] is None
