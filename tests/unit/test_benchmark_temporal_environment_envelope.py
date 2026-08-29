"""Contract checks for the synthetic environmental-envelope runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_environment_envelope.py"
_SPEC = importlib.util.spec_from_file_location("c_temporal_environment_envelope_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_runner_schema_and_fail_closed_kinds_are_explicit() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-environment-speed-envelope.v1"
    assert _MODULE.PROFILES == {
        "small": (5, 7, 7),
        "medium": (9, 13, 13),
        "stress": (13, 19, 19),
    }
    assert _MODULE.CERTIFICATE_KINDS == (
        "certified",
        "partial",
        "hard_mask",
        "missing_speed",
        "scope_mismatch",
    )


def test_certified_envelope_prunes_and_uncertain_edges_remain_live() -> None:
    certified = _MODULE._case("small", "fastest", "certified")
    for kind in ("partial", "hard_mask", "missing_speed", "scope_mismatch"):
        rejected = _MODULE._case("small", "fastest", kind)
        assert rejected["semantic_match"] is True
        assert rejected["actual_edge_pruning"] == 0
        assert rejected["pruning_expectation_met"] is True

    assert certified["certificate_usable"] is True
    assert certified["envelope_status"] == "CERTIFIED"
    assert certified["semantic_match"] is True
    assert certified["actual_edge_pruning"] > 0


def test_all_profiles_and_objectives_are_deterministic() -> None:
    for profile in _MODULE.PROFILES:
        for objective in _MODULE.OBJECTIVES:
            first = _MODULE._case(profile, objective, "certified")
            second = _MODULE._case(profile, objective, "certified")
            assert first["semantic_match"] is True
            assert first["actual_edge_pruning"] > 0
            assert first["deterministic_digest"] == second["deterministic_digest"]
