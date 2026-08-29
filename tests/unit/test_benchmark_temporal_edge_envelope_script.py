"""Contract checks for the synthetic edge-envelope proof runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_edge_envelope.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_edge_envelope_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_matrix_and_frozen_limits_are_explicit() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-edge-envelope.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert _MODULE.CERTIFICATE_KINDS == (
        "certified",
        "coverage_incomplete",
        "scope_mismatch",
    )
    assert _MODULE.HORIZON_HOURS == 2.0


def test_certified_edge_envelope_prunes_and_rejected_evidence_does_not() -> None:
    certified = _MODULE._case("small", "fastest", "certified")
    incomplete = _MODULE._case("small", "fastest", "coverage_incomplete")
    mismatch = _MODULE._case("small", "fastest", "scope_mismatch")

    assert certified["certificate_usable"] is True
    assert certified["semantic_match"] is True
    assert certified["actual_edge_pruning"] > 0
    assert incomplete["certificate_usable"] is False
    assert mismatch["certificate_usable"] is False
    assert incomplete["actual_edge_pruning"] == 0
    assert mismatch["actual_edge_pruning"] == 0
    assert incomplete["pruning_expectation_met"] is True
    assert mismatch["pruning_expectation_met"] is True


def test_all_profiles_and_objectives_are_deterministic() -> None:
    for profile in _MODULE.PROFILES:
        for objective in _MODULE.OBJECTIVES:
            first = _MODULE._case(profile, objective, "certified")
            second = _MODULE._case(profile, objective, "certified")
            assert first["semantic_match"] is True
            assert first["actual_edge_pruning"] > 0
            assert first["deterministic_digest"] == second["deterministic_digest"]
