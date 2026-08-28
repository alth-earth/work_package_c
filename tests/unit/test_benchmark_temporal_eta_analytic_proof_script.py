"""Contract tests for the analytic ETA/FIFO proof runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from arctic_route_planning.planners.eta_interval import EtaIntervalStatus
from arctic_route_planning.planners.temporal_qualification import FifoStatus

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_analytic_proof.py"
_SPEC = importlib.util.spec_from_file_location("c_analytic_eta_proof_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_schema_profiles_and_adversarial_matrix_are_frozen() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-eta-analytic-proof.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert len(_MODULE.SCENARIOS) == 13
    assert "continuous_nonunique" in _MODULE.SCENARIOS
    assert "policy_checkpoint_mismatch" in _MODULE.SCENARIOS


def test_runner_authorizes_only_unique_root_with_fifo_slope() -> None:
    unique = _MODULE._case("small", "fastest", "contraction_unique")
    nonunique = _MODULE._case("small", "fastest", "continuous_nonunique")
    discontinuity = _MODULE._case("small", "fastest", "hard_mask_discontinuity")

    assert unique["status"] == EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value
    assert unique["fifo_status"] == FifoStatus.FIFO_CERTIFIED.value
    assert unique["authorization_usable"] is True
    assert nonunique["status"] == EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE.value
    assert nonunique["authorization_usable"] is False
    assert discontinuity["authorization_usable"] is False


def test_runner_scope_and_policy_mismatch_fail_closed() -> None:
    scope = _MODULE._case("small", "recommended", "scope_mismatch")
    policy = _MODULE._case("small", "recommended", "policy_checkpoint_mismatch")

    assert scope["authorization_usable"] is False
    assert scope["fail_closed"] is True
    assert policy["authorization_usable"] is False
    assert policy["fail_closed"] is True
