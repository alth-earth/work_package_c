"""Contract tests for the M1.9 synthetic ETA proof runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from arctic_route_planning.planners.eta_interval import EtaIntervalStatus

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_interval_proof.py"
_SPEC = importlib.util.spec_from_file_location(
    "c_benchmark_temporal_eta_interval_proof", _SCRIPT_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_proof_runner_has_complete_profile_and_adversarial_matrix() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-eta-proof.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert len(_MODULE.SCENARIOS) == 13
    assert {"contraction_unique", "scope_mismatch", "policy_checkpoint_mismatch"} <= set(
        _MODULE.SCENARIOS
    )


def test_proof_runner_unique_root_is_the_only_authorization() -> None:
    unique = _MODULE._synthetic_case("small", "fastest", "contraction_unique")
    nonunique = _MODULE._synthetic_case("small", "fastest", "continuous_nonunique")
    uncertain = _MODULE._synthetic_case("small", "fastest", "finite_no_bracket")

    assert unique["status"] == EtaIntervalStatus.ROOT_EXISTS_UNIQUE.value
    assert unique["authorization_usable"] is True
    assert nonunique["status"] == EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE.value
    assert nonunique["authorization_usable"] is False
    assert uncertain["status"] == EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF.value
    assert uncertain["authorization_usable"] is False


def test_proof_runner_identity_and_digest_mismatch_are_fail_closed() -> None:
    scope = _MODULE._synthetic_case("small", "recommended", "scope_mismatch")
    policy = _MODULE._synthetic_case("small", "recommended", "policy_checkpoint_mismatch")

    assert scope["fail_closed"] is True
    assert scope["authorization_usable"] is False
    assert policy["fail_closed"] is True
    assert policy["authorization_usable"] is False
