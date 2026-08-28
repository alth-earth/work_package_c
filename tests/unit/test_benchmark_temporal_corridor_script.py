"""Contract checks for the synthetic temporal corridor proof runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_corridor.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_corridor_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_matrix_and_profiles_are_frozen() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-corridor-proof.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert _MODULE.CERTIFICATE_KINDS == (
        "certified",
        "coverage_incomplete",
        "scope_mismatch",
        "non_admissible",
    )


def test_certified_fixture_prunes_but_rejected_evidence_does_not() -> None:
    certified = _MODULE._case("small", "fastest", "certified")
    rejected = _MODULE._case("small", "fastest", "coverage_incomplete")

    assert certified["certificate_usable"] is True
    assert certified["actual_pruning"] > 0
    assert certified["semantic_match"] is True
    assert rejected["certificate_usable"] is False
    assert rejected["actual_pruning"] == 0
    assert rejected["pruning_expectation_met"] is True


def test_scope_mismatch_is_fail_closed() -> None:
    case = _MODULE._case("medium", "recommended", "scope_mismatch")
    assert case["certificate_usable"] is False
    assert case["actual_pruning"] == 0
    assert case["state_bound_rejected"] == 1
