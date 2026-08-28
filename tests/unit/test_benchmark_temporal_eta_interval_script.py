"""Focused checks for the ETA interval evidence runner."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from arctic_route_planning.planners.eta_interval import EtaIntervalStatus

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_interval.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_temporal_eta_interval", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


def test_synthetic_fixture_matrix_keeps_uncertain_cases_unusable() -> None:
    expected = {
        "contraction_unique": EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value,
        "continuous_nonunique": EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE.value,
        "root_excluded": EtaIntervalStatus.ROOT_EXCLUDED.value,
        "finite_no_bracket": EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value,
        "coverage_incomplete": EtaIntervalStatus.UNCERTAIN_COVERAGE.value,
        "evaluator_failure": EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE.value,
        "hard_mask_discontinuity": EtaIntervalStatus.UNCERTAIN_DISCONTINUITY.value,
    }

    for scenario, status in expected.items():
        case = _SCRIPT._synthetic_case("small", "fastest", scenario)
        assert case["status"] == status
        if status.startswith("UNCERTAIN"):
            assert case["usable"] is False


def test_real_point_scan_is_never_promoted_to_an_interval_certificate(monkeypatch) -> None:
    monkeypatch.setattr(
        _SCRIPT,
        "_load_real_runner",
        lambda: SimpleNamespace(
            _fifo_scan=lambda _args: {
                "status": "FIFO_UNCERTAIN_NO_INTERVAL_PROOF",
                "input": "holdout",
                "segment": "executable_0_6h",
                "edge_count": 2,
                "probe_count": 25,
                "evaluations": 50,
                "evaluation_errors": 0,
                "evaluation_failure_classes": [],
                "counterexample": None,
            }
        ),
    )
    args = argparse.Namespace(
        risk_window_commit=Path("/tmp/window.json"),
        route_plan_set=Path("/tmp/routes.json"),
        config_root=Path("/tmp/config"),
        segment="executable_0_6h",
        cpu=2,
    )

    case = _SCRIPT._real_case(args)

    assert case["status"] == "FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF"
    assert case["coverage_complete"] is False
    assert case["evaluator_certified"] is False
    assert case["dominance_usable"] is False
    assert case["certificate_digest"] is None


def test_config_tree_digest_is_content_addressed(tmp_path) -> None:
    config = tmp_path / "configs"
    config.mkdir()
    (config / "planner.yaml").write_text("horizon: 6\n", encoding="utf-8")
    first = _SCRIPT._tree_digest(config)
    (config / "planner.yaml").write_text("horizon: 24\n", encoding="utf-8")

    assert first != _SCRIPT._tree_digest(config)
