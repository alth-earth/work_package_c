"""Tests for the research-only real-input queue compaction runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_RUNNER_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "benchmark_temporal_queue_compaction_real.py"
)
_SPEC = importlib.util.spec_from_file_location("m30_queue_compaction_runner", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)
_summary = _RUNNER._summary


def _identity() -> dict[str, object]:
    return {
        "experiment_id": "m30-test",
        "objectives": ["fastest"],
        "repetitions": 1,
    }


def _case(status: str, *, semantic_match: bool = True) -> dict[str, object]:
    return {
        "status": status,
        "semantic_match": semantic_match,
        "baseline_reference_match": status == "PASS",
        "compacted_reference_match": status == "PASS",
        "queue_compactions": 0,
        "queue_compaction_removed": 0,
        "resource_classification": "RESOURCE_CLEAN_BOUNDARY_INCOMPLETE",
    }


def test_summary_preserves_reference_resource_limit_as_diagnostic_success() -> None:
    summary = _summary([_case("REFERENCE_RESOURCE_LIMIT")], _identity())

    assert summary["status"] == "QUEUE_COMPACTION_REFERENCE_RESOURCE_LIMIT"
    assert summary["semantic_all_match"] is True
    assert summary["reference_all_match"] is False
    assert summary["reference_resource_limit_cases"] == 1
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False


def test_summary_rejects_semantic_failure_even_when_reference_is_available() -> None:
    summary = _summary([_case("PASS", semantic_match=False)], _identity())

    assert summary["status"] == "QUEUE_COMPACTION_DIAGNOSTIC_FAIL"
    assert summary["semantic_all_match"] is False
