"""Static and summary checks for the P0.2-M5 synthetic runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "c_benchmark_non_fifo_temporal_bound",
    _ROOT / "scripts" / "benchmark_non_fifo_temporal_bound.py",
)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load non-FIFO bound runner")
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


def test_runner_is_explicit_and_fail_closed() -> None:
    source = (_ROOT / "scripts" / "benchmark_non_fifo_temporal_bound.py").read_text(
        encoding="utf-8"
    )
    assert "run_non_fifo_temporal_bounded_search" in source
    assert '"state_bound_pruned"' in source
    assert '"scope_mismatch"' in source
    assert '"production_candidate_enabled": False' in source


def test_summary_requires_certified_pruning_and_rejected_zero_pruning() -> None:
    identity = {
        "experiment_id": "synthetic-bound-test",
        "profiles": ["small"],
        "objectives": ["fastest"],
        "modes": ["certified", "scope_mismatch"],
    }
    records = [
        {
            "profile": "small",
            "objective": "fastest",
            "mode": "certified",
            "status": "PASS",
            "deterministic_probe": True,
            "semantic_match": True,
            "state_bound_pruned": 2,
            "baseline_semantic_digest": "same",
            "bounded_semantic_digest": "same",
        },
        {
            "profile": "small",
            "objective": "fastest",
            "mode": "scope_mismatch",
            "status": "REJECTED_FAIL_CLOSED",
            "deterministic_probe": True,
            "semantic_match": True,
            "state_bound_pruned": 0,
            "baseline_semantic_digest": "same",
            "bounded_semantic_digest": None,
        },
    ]
    summary = _SCRIPT._summary(records, identity)
    assert summary["status"] == "TEMPORAL_ADAPTER_STATE_BOUND_MATRIX_PASS"
    assert summary["observed_certified_pruning"] == 2
    assert summary["rejected_pruning_total"] == 0
    assert summary["fail_closed"] is True


def test_summary_fails_when_certified_case_has_no_pruning() -> None:
    identity = {
        "experiment_id": "synthetic-bound-test",
        "profiles": ["small"],
        "objectives": ["fastest"],
        "modes": ["certified"],
    }
    records = [
        {
            "profile": "small",
            "objective": "fastest",
            "mode": "certified",
            "status": "PASS",
            "deterministic_probe": True,
            "semantic_match": True,
            "state_bound_pruned": 0,
            "baseline_semantic_digest": "same",
            "bounded_semantic_digest": "same",
        }
    ]
    summary = _SCRIPT._summary(records, identity)
    assert summary["status"] == "NO_PERFORMANCE_PROOF/FAIL"
