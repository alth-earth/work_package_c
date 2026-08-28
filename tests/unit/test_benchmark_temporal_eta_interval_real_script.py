"""Contract checks for the real-input ETA interval qualification runner."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_eta_interval_real.py"
_SPEC = importlib.util.spec_from_file_location("c_real_eta_interval_runner", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_real_runner_schema_and_frozen_segments_are_default_off() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.1-temporal-eta-proof-real.v1"
    assert set(_MODULE.SEGMENTS) == {"executable_0_6h", "rolling_0_24h"}
    assert _MODULE.BASE_PROBE_MINUTES == 15
    assert _MODULE.FIFO_TOLERANCE_SECONDS == 1.0


def test_real_runner_never_declares_dominance_authorization() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "dominance_policy\": \"disabled\"" in source
    assert "certified_only" not in source
    assert '"dominance_pruned": 0' in source


def test_real_runner_resume_uses_only_complete_identity_bound_edges(tmp_path: Path) -> None:
    record = {
        "schema_version": _MODULE.SCHEMA_VERSION,
        "input": "holdout",
        "segment": "executable_0_6h",
        "edge_index": 0,
        "probe_count": 2,
        "scope_digest": "scope-digest",
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "probe_records": [
            {"evidence": {"status": "UNCERTAIN_COVERAGE", "reason": "gap"}},
            {"evidence": {"status": "UNCERTAIN_DISCONTINUITY", "reason": "mask"}},
        ],
    }
    (tmp_path / "eta-interval.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    complete, statuses, failures = _MODULE._resume_edge_records(
        tmp_path,
        expected_input="holdout",
        expected_segment="executable_0_6h",
        expected_scope_digest="scope-digest",
        expected_probe_count=2,
        expected_edge_count=3,
    )

    assert set(complete) == {0}
    assert statuses == {"UNCERTAIN_COVERAGE": 1, "UNCERTAIN_DISCONTINUITY": 1}
    assert failures == {"gap": 1, "mask": 1}


def test_real_runner_resume_rejects_identity_mismatch(tmp_path: Path) -> None:
    record = {
        "schema_version": _MODULE.SCHEMA_VERSION,
        "input": "other-input",
        "segment": "executable_0_6h",
        "edge_index": 0,
        "probe_count": 1,
        "scope_digest": "scope-digest",
        "dominance_policy": "disabled",
        "dominance_pruned": 0,
        "probe_records": [{"evidence": {"status": "UNCERTAIN_COVERAGE"}}],
    }
    (tmp_path / "eta-interval.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="identity-mismatched"):
        _MODULE._resume_edge_records(
            tmp_path,
            expected_input="holdout",
            expected_segment="executable_0_6h",
            expected_scope_digest="scope-digest",
            expected_probe_count=1,
            expected_edge_count=3,
        )
