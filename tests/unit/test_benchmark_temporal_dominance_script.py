"""Contract and gate checks for the P0.1 synthetic runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_dominance.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_temporal_dominance", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


def _resource_snapshot(rss: int = 100) -> dict[str, object]:
    return {
        "process_swap_kib": 0,
        "host_swap_pages": {"pswpin": 10, "pswpout": 20},
        "cpu_affinity": [2],
        "max_rss_kib": rss,
        "cgroup": {
            "memory_events": {"oom": 0, "oom_kill": 0, "oom_group_kill": 0},
            "memory_swap_current": 0,
        },
    }


def _case(*, regression: float = 0.0, pruned: int = 2) -> dict[str, object]:
    baseline = {
        "status": "PASS",
        "semantic_digest": "same-route",
        "compute_ms": 100.0,
        "resources_before": _resource_snapshot(),
        "resources_after": _resource_snapshot(),
    }
    candidate = {
        "status": "PASS",
        "semantic_digest": "same-route",
        "compute_ms": 100.0 * (1.0 + regression / 100.0),
        "dominance_pruned": pruned,
        "dominance_scope_match": True,
        "reference_match": True,
        "metadata": {"fifo_status": "FIFO_CERTIFIED"},
        "resources_before": _resource_snapshot(101),
        "resources_after": _resource_snapshot(101),
    }
    return {
        "profile": "small",
        "objective": "fastest",
        "semantic_match": True,
        "regression_percent": regression,
        "resource_clean": True,
        "workers": {"baseline": baseline, "candidate": candidate},
    }


def test_summary_requires_observable_certified_pruning() -> None:
    summary = _SCRIPT._summarize([_case(pruned=0)])
    assert summary["gate_verdict"] == "NO_PERFORMANCE_PROOF"
    assert summary["gate_checks"]["observable_label_reduction"] is False


def test_summary_passes_semantic_resource_and_regression_gates() -> None:
    summary = _SCRIPT._summarize([_case(regression=-12.0), _case(regression=2.0)])
    assert summary["gate_verdict"] == "PASS"
    assert summary["median_regression_percent"] == -5.0
    assert summary["candidate_dominance_pruned"] == 4
    assert summary["gate_checks"]["semantic_identity"] is True


def test_summary_rejects_regression_above_five_percent() -> None:
    summary = _SCRIPT._summarize([_case(regression=5.1)])
    assert summary["gate_verdict"] == "FAIL"
    assert summary["gate_checks"]["median_regression_le_5pct"] is False


def test_summary_rejects_p95_regression_above_five_percent_even_when_median_passes() -> None:
    cases = [_case(regression=value) for value in (0.0, 0.0, 0.0, 0.0, 6.0)]

    summary = _SCRIPT._summarize(cases)

    assert summary["objective_summaries"]["fastest"]["median_regression_percent"] == 0.0
    assert summary["objective_summaries"]["fastest"]["p95_regression_percent"] == 6.0
    assert summary["gate_checks"]["per_objective_regression_le_5pct"] is True
    assert summary["gate_checks"]["per_objective_p95_regression_le_5pct"] is False
    assert summary["gate_verdict"] == "FAIL"


def test_experiment_identity_is_stable_for_a_profile() -> None:
    assert _SCRIPT._experiment_id("small") == _SCRIPT._experiment_id("small")
    assert _SCRIPT._experiment_id("small") != _SCRIPT._experiment_id("medium")


def test_stress_profile_is_fixed_and_identity_bound() -> None:
    profile = _SCRIPT.SYNTHETIC_PROFILES["stress"]

    assert (profile.rows, profile.cols, profile.frame_count) == (13, 19, 19)
    assert _SCRIPT._experiment_id("stress") != _SCRIPT._experiment_id("medium")


def test_m1_qualification_audit_requires_pruning_only_for_certified_case() -> None:
    audit = _SCRIPT._qualification_audit("small")

    assert audit["passed"] is True
    assert audit["case_count"] == 7
    by_name = {case["name"]: case for case in audit["cases"]}
    assert by_name["fifo_certified"]["authorized"] is True
    assert by_name["fifo_certified"]["pruned"] is True
    for name in (
        "fifo_violated",
        "fifo_uncertain",
        "suffix_not_monotone",
        "coverage_incomplete",
        "scope_mismatch",
        "unknown_evaluator",
    ):
        assert by_name[name]["authorized"] is False
        assert by_name[name]["pruned"] is False


def test_summary_uses_compute_metric_and_reports_p95() -> None:
    first = _case(regression=-10.0)
    second = _case(regression=2.0)
    first["wall_regression_percent"] = 250.0
    second["wall_regression_percent"] = 300.0

    summary = _SCRIPT._summarize([first, second])

    assert summary["regression_metric"] == "compute_ms"
    assert summary["gate_checks"]["regression_metric_compute_ms"] is True
    assert summary["p95_regression_percent"] == 2.0
    assert summary["objective_summaries"]["fastest"]["wall_p95_regression_percent"] == 300.0
    assert summary["gate_verdict"] == "PASS"
