"""Evidence-chain tests for the P3.2 SMO-A* benchmark runner."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_smo_astar.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_smo_astar", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)


def _resource_snapshot() -> dict[str, object]:
    return {
        "process_swap_kib": 0,
        "host_swap_pages": {"pswpin": 10, "pswpout": 20},
        "cpu_affinity": [2],
        "cgroup": {
            "memory_current": 100,
            "memory_peak": 200,
            "memory_max": 4 * 1024 * 1024 * 1024,
            "memory_swap_current": 0,
            "memory_swap_max": 0,
            "memory_events": {"oom": 0, "oom_kill": 0},
        },
    }


def _run(
    wall_seconds: float,
    *,
    peak_rss_kib: int,
    hits: int = 0,
    misses: int = 0,
) -> dict[str, object]:
    return {
        "wall_seconds": wall_seconds,
        "peak_rss_kib": peak_rss_kib,
        "traversal_cache": {"hits": hits, "misses": misses},
        "resources_before": _resource_snapshot(),
        "resources_after": _resource_snapshot(),
    }


def test_pair_order_alternates_control_and_candidate() -> None:
    assert _SCRIPT._pair_order(1) == ("baseline", "shared")
    assert _SCRIPT._pair_order(2) == ("shared", "baseline")


def test_attempt_number_counts_pairs_not_worker_files(tmp_path: Path) -> None:
    key = "timed_pair-001"
    (tmp_path / f"{key}-attempt-01-baseline.json").write_text("{}")
    (tmp_path / f"{key}-attempt-01-shared.json").write_text("{}")

    assert _SCRIPT._next_attempt(tmp_path, key) == 2


def test_resume_rejects_any_identity_drift() -> None:
    recorded = {"identity_sha256": "old", "repetitions": 5}
    with pytest.raises(RuntimeError, match="identity does not match"):
        _SCRIPT._validate_resume_identity(
            recorded,
            {"identity_sha256": "new", "repetitions": 5},
        )


def test_interrupted_half_pair_is_recorded_and_excluded(tmp_path: Path) -> None:
    workers = tmp_path / "workers"
    workers.mkdir()
    orphan = workers / "timed_pair-001-attempt-01-baseline.json"
    orphan.write_text("{}", encoding="utf-8")
    cases_path = tmp_path / "cases.jsonl"

    _SCRIPT._exclude_unreferenced_workers(tmp_path, cases_path)
    _SCRIPT._exclude_unreferenced_workers(tmp_path, cases_path)

    cases = [json.loads(line) for line in cases_path.read_text().splitlines()]
    assert len(cases) == 1
    assert cases[0]["status"] == "ORPHANED_EXCLUDED"
    assert cases[0]["worker_files"] == {"orphan": "workers/timed_pair-001-attempt-01-baseline.json"}


def test_m0_gate_accepts_semantic_and_resource_safe_no_regression() -> None:
    baseline = [_run(10.0, peak_rss_kib=100) for _ in range(3)]
    shared = [_run(10.4, peak_rss_kib=105, hits=60, misses=40) for _ in range(3)]

    summary = _SCRIPT._summarize_runs(
        baseline,
        shared,
        gate_profile="m0",
        repetitions=3,
        strict_resources=True,
    )

    assert summary["gate_verdict"] == "PASS"
    assert summary["gate_checks"]["median_wall_regression_le_5pct"] is True


def test_m1_gate_requires_speed_hit_rate_and_memory_together() -> None:
    baseline = [_run(100.0, peak_rss_kib=100) for _ in range(5)]
    shared = [_run(80.0, peak_rss_kib=105, hits=60, misses=40) for _ in range(5)]

    passed = _SCRIPT._summarize_runs(
        baseline,
        shared,
        gate_profile="m1",
        repetitions=5,
        strict_resources=True,
    )
    assert passed["gate_verdict"] == "PASS"

    shared[0]["traversal_cache"] = {"hits": 0, "misses": 1000}
    for run in shared:
        run["peak_rss_kib"] = 130
    failed = _SCRIPT._summarize_runs(
        baseline,
        shared,
        gate_profile="m1",
        repetitions=5,
        strict_resources=True,
    )
    assert failed["gate_verdict"] == "FAIL"
    assert failed["gate_checks"]["cache_hit_rate_ge_50pct"] is False
    assert failed["gate_checks"]["rss_ratio_le_1_10"] is False
