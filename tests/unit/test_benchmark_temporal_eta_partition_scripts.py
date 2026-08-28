"""Contract checks for the partitioned ETA proof and real runners."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load(name: str):
    script = Path(__file__).parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location(f"c_{name.replace('.', '_')}", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_partition_proof_runner_has_required_profiles_and_fail_closed_cases() -> None:
    module = _load("benchmark_temporal_eta_partition_proof.py")

    assert module.SCHEMA_VERSION == "c.p0.1-temporal-evaluator-partition-proof.v1"
    assert module.PROFILES["stress"] == (13, 19, 19)
    assert "negative_boundary_fixture" in module.SCENARIOS
    stable = module._build_case("small", "fastest", "stable_partitioned")
    uncertain = module._build_case("small", "fastest", "coverage_gap")
    assert stable["status"] == "PARTITION_CERTIFIED"
    assert stable["fail_closed"] is True
    assert uncertain["status"] == "UNCERTAIN"
    assert uncertain["authorization_usable"] is False


def test_real_partition_runner_parser_and_disabled_dominance_contract() -> None:
    module = _load("benchmark_temporal_eta_partition_real.py")
    args = module._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window.json",
            "--route-plan-set",
            "/tmp/routes.json",
            "--segment",
            "executable_0_6h",
            "--config-root",
            "/tmp/config",
            "--output-dir",
            "/tmp/out",
            "--objective",
            "low_risk",
        ]
    )

    assert module.OBJECTIVES == ("fastest", "low_risk", "recommended")
    assert args.objective == "low_risk"
    assert args.worker_timeout_seconds == 900.0
    assert args.cpu == -1
    assert module.SEGMENTS["executable_0_6h"].total_seconds() == 6 * 3600
