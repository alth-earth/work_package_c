"""Fail-closed contract checks for the P0.2-M4 real adapter runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_real.py"
_SPEC = importlib.util.spec_from_file_location("c_real_non_fifo_adapter_runner", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def test_runner_has_explicit_real_adapter_schema_and_safe_defaults() -> None:
    assert _MODULE.SCHEMA_VERSION == "c.p0.2-temporal-adapter-real.v1"
    assert _MODULE.SEGMENTS == {"executable_0_6h", "rolling_0_24h"}
    assert _MODULE._parser().parse_args(
        [
            "--risk-window-commit",
            "window.json",
            "--route-plan-set",
            "routes.json",
            "--config-root",
            "configs",
            "--segment",
            "executable_0_6h",
            "--output-dir",
            "out",
        ]
    ).cpu == -1


def test_runner_source_forbids_candidate_and_heuristic_paths() -> None:
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert "run_non_fifo_temporal_search" in source
    assert '"dominance_policy": "disabled"' in source
    assert '"state_bound_policy": "absent"' in source
    assert "certified_only" not in source
    assert "use_heuristic=False" in source


def test_child_command_is_explicitly_fenced(tmp_path: Path) -> None:
    args = _MODULE._parser().parse_args(
        [
            "--risk-window-commit",
            str(tmp_path / "window.json"),
            "--route-plan-set",
            str(tmp_path / "routes.json"),
            "--config-root",
            str(tmp_path / "configs"),
            "--segment",
            "rolling_0_24h",
            "--output-dir",
            str(tmp_path / "out"),
            "--cpu",
            "3",
        ]
    )
    command = _MODULE._child_command(args, _MODULE.OBJECTIVES[0], 2)
    assert "--worker" in command
    assert "--objective" in command
    assert "--repetition" in command
    assert command[command.index("--cpu") + 1] == "3"


def test_resume_accepts_only_complete_matching_safety_records(tmp_path: Path) -> None:
    record = {
        "schema_version": _MODULE.SCHEMA_VERSION,
        "experiment_id": "experiment",
        "mode": "real-adapter",
        "objective": "fastest",
        "repetition": 1,
        "status": "RESOURCE_LIMIT",
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
    }
    (tmp_path / "resource-frontier.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    cases, malformed = _MODULE._load_resume_cases(tmp_path, "experiment")
    assert malformed == 0
    assert len(cases) == 1
    assert cases[0]["status"] == "RESOURCE_LIMIT"


def test_resume_rejects_identity_and_pruning_drift(tmp_path: Path) -> None:
    record = {
        "schema_version": _MODULE.SCHEMA_VERSION,
        "experiment_id": "other",
        "mode": "real-adapter",
        "objective": "fastest",
        "repetition": 1,
        "dominance_policy": "enabled",
        "state_bound_policy": "present",
    }
    (tmp_path / "resource-frontier.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="another experiment identity"):
        _MODULE._load_resume_cases(tmp_path, "experiment")


def test_summary_keeps_resource_failures_out_of_promotion() -> None:
    cases = [
        {
            "objective": objective.value,
            "repetition": 1,
            "status": "RESOURCE_LIMIT",
            "resource_clean": True,
            "resources_before": {"cpu_affinity": [0], "cgroup": {}},
            "resources_after": {"cpu_affinity": [0], "cgroup": {}},
            "dominance_policy": "disabled",
            "state_bound_policy": "absent",
        }
        for objective in _MODULE.OBJECTIVES
    ]
    summary = _MODULE._summary(cases, repetitions=1, cpu=0, ignored_records=0)
    assert summary["status"] == "REAL_INPUT_ADAPTER_RESOURCE_FAIL"
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False
