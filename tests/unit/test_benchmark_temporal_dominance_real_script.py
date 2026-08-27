"""Fail-closed and identity checks for the real-input P0.1 runner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_route_planning.planners.temporal_qualification import FifoStatus

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_temporal_dominance_real.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_temporal_dominance_real", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


def _resource_snapshot(*, swap: int = 0) -> dict[str, object]:
    return {
        "process_swap_kib": swap,
        "host_swap_pages": {"pswpin": 10, "pswpout": 20},
        "cpu_affinity": [2],
        "max_rss_kib": 100,
        "cgroup": {
            "memory_events": {"oom": 0, "oom_kill": 0, "oom_group_kill": 0},
            "memory_swap_current": 0,
        },
    }


def test_sampled_monotonicity_never_authorizes_real_dominance() -> None:
    status, reason = _SCRIPT._diagnostic_fifo_status(
        FifoStatus.FIFO_CERTIFIED,
        counterexample=None,
        evaluation_errors=False,
    )

    assert status == "FIFO_UNCERTAIN_NO_INTERVAL_PROOF"
    assert "continuous" in reason


def test_real_counterexample_is_preserved_as_fifo_violation() -> None:
    status, reason = _SCRIPT._diagnostic_fifo_status(
        FifoStatus.FIFO_CERTIFIED,
        counterexample={"slack_seconds": -2.0},
        evaluation_errors=False,
    )

    assert status == FifoStatus.FIFO_VIOLATED.value
    assert reason == "sampled counterexample observed"


def test_evaluator_failure_wins_over_sampled_certified_status() -> None:
    status, _ = _SCRIPT._diagnostic_fifo_status(
        FifoStatus.FIFO_CERTIFIED,
        counterexample=None,
        evaluation_errors=True,
    )

    assert status == "FIFO_UNCERTAIN_EVALUATOR_FAILURE"


def test_resource_clean_rejects_new_swap_and_oom() -> None:
    before = _resource_snapshot()
    assert _SCRIPT._resource_clean(before, _resource_snapshot()) is True
    assert _SCRIPT._resource_clean(before, _resource_snapshot(swap=1)) is False
    after_oom = _resource_snapshot()
    after_oom["cgroup"]["memory_events"]["oom"] = 1  # type: ignore[index]
    assert _SCRIPT._resource_clean(before, after_oom) is False


def test_resource_evidence_requires_the_declared_cgroup_and_cpu() -> None:
    snapshot = _resource_snapshot()
    snapshot["cgroup"]["memory_max"] = 4 * 1024**3  # type: ignore[index]
    snapshot["cgroup"]["memory_swap_max"] = 0  # type: ignore[index]
    record = {
        "resources_before": snapshot,
        "resources_after": snapshot,
    }

    assert _SCRIPT._resource_evidence_complete(record, cpu=2) is True
    snapshot["cgroup"]["memory_swap_max"] = 1  # type: ignore[index]
    assert _SCRIPT._resource_evidence_complete(record, cpu=2) is False


def test_resource_summary_requires_complete_deterministic_cells() -> None:
    snapshot = _resource_snapshot()
    snapshot["cgroup"]["memory_max"] = 4 * 1024**3  # type: ignore[index]
    snapshot["cgroup"]["memory_swap_max"] = 0  # type: ignore[index]
    cases = []
    for objective in ("fastest", "low_risk", "recommended"):
        cases.append(
            {
                "status": "PASS",
                "mode": "resource-frontier",
                "objective": objective,
                "repetition": 1,
                "dominance_policy": "disabled",
                "dominance_pruned": 0,
                "reference_match": True,
                "resource_clean": True,
                "resources_before": snapshot,
                "resources_after": snapshot,
                "semantic_digest": objective,
                "compute_ms": 10.0,
                "wall_seconds": 0.1,
            }
        )
    summary = _SCRIPT._resource_summary(cases, repetitions=1, cpu=2, ignored_records=0)

    assert summary["status"] == "RESOURCE_FRONTIER_PASS"
    assert summary["resource_evidence_complete"] is True
    assert summary["metrics"]["fastest"]["compute_ms"]["p95"] == pytest.approx(10.0)


def test_resume_reuses_complete_cells_without_rerunning_workers(tmp_path, monkeypatch) -> None:
    snapshot = _resource_snapshot()
    snapshot["cgroup"]["memory_max"] = 4 * 1024**3  # type: ignore[index]
    snapshot["cgroup"]["memory_swap_max"] = 0  # type: ignore[index]
    calls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        _SCRIPT,
        "_load_fixture",
        lambda _args: SimpleNamespace(input_name="holdout", segment="executable_0_6h"),
    )
    monkeypatch.setattr(
        _SCRIPT,
        "_experiment_identity",
        lambda _args, _fixture: {"git": {"git_dirty": False}, "fixture": "frozen"},
    )

    def fake_child(_args, objective, repetition, _heartbeat):
        assert objective is not None
        calls.append((objective.value, repetition))
        return {
            "schema_version": _SCRIPT.SCHEMA_VERSION,
            "status": "PASS",
            "mode": "resource-frontier",
            "objective": objective.value,
            "repetition": repetition,
            "dominance_policy": "disabled",
            "dominance_pruned": 0,
            "reference_match": True,
            "resource_clean": True,
            "resources_before": snapshot,
            "resources_after": snapshot,
            "semantic_digest": objective.value,
            "compute_ms": 10.0,
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(_SCRIPT, "_run_child", fake_child)
    args = argparse.Namespace(
        mode="resource-frontier",
        output_dir=tmp_path,
        resume=False,
        repetitions=1,
        worker_timeout_seconds=600.0,
        cpu=2,
    )

    assert _SCRIPT._run(args) == 0
    assert len(calls) == 3
    args.resume = True
    assert _SCRIPT._run(args) == 0
    assert len(calls) == 3
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["resume_count"] == 1


def test_probe_schedule_is_closed_over_segment_horizon() -> None:
    class Fixture:
        departure = datetime(2026, 1, 1, tzinfo=UTC)
        segment = "executable_0_6h"

    probes = _SCRIPT._probe_times(Fixture())

    assert probes[0] == Fixture.departure
    assert probes[-1] == Fixture.departure + timedelta(hours=6)
    assert len(probes) == 25
