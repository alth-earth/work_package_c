"""Contract checks for the synthetic heading-envelope proof runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_heading_heuristic.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_heading_heuristic_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_matrix_and_certificate_kinds_are_explicit() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-heading-heuristic.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert _MODULE.CERTIFICATE_KINDS == (
        "certified",
        "incomplete",
        "scope_mismatch",
        "non_admissible",
    )


def test_certified_heading_ordering_preserves_route_and_rejected_proofs_disable() -> None:
    certified = _MODULE._case("small", "recommended", "certified")
    incomplete = _MODULE._case("small", "recommended", "incomplete")
    mismatch = _MODULE._case("small", "recommended", "scope_mismatch")

    assert certified["certificate_usable"] is True
    assert certified["semantic_match"] is True
    assert certified["heading_heuristic_enabled"] is True
    assert certified["pruning_observed"] is False
    assert incomplete["certificate_usable"] is False
    assert mismatch["certificate_usable"] is True
    assert incomplete["heading_heuristic_enabled"] is False
    assert mismatch["heading_heuristic_enabled"] is False
    assert mismatch["scope_match"] is False
    assert incomplete["fail_closed"] is True
    assert mismatch["fail_closed"] is True


def test_all_profiles_and_objectives_are_deterministic() -> None:
    for profile in _MODULE.PROFILES:
        for objective in _MODULE.OBJECTIVES:
            first = _MODULE._case(profile, objective, "certified")
            second = _MODULE._case(profile, objective, "certified")
            assert first["semantic_match"] is True
            assert first["deterministic"] is True
            assert first["certificate_digest"] == second["certificate_digest"]
            assert first["candidate"] == second["candidate"]
