"""Fail-closed contract checks for the M13 actual-session evidence runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts" / "benchmark_non_fifo_temporal_session.py"
_SPEC = importlib.util.spec_from_file_location("c_m13_actual_session_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M13 runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _args(tmp_path: Path, *extra: str):
    return _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            str(tmp_path / "window.json"),
            "--route-plan-set",
            str(tmp_path / "routes.json"),
            "--config-root",
            str(tmp_path / "configs"),
            "--output-dir",
            str(tmp_path / "out"),
            *extra,
        ]
    )


def test_runner_schema_modes_and_default_safety_fence(tmp_path: Path) -> None:
    args = _args(tmp_path)
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-temporal-session-real.v1"
    assert _RUNNER.MODES == ("one_shot", "slice_restore", "cancelled")
    assert tuple(args.modes) == _RUNNER.MODES
    assert args.segment == "executable_0_6h"
    assert args.cpu == -1
    assert args.slice_expansions == 1


def test_runner_source_is_actual_session_and_default_off() -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "create_non_fifo_temporal_session" in source
    assert "restore_non_fifo_temporal_session" in source
    assert "run_non_fifo_temporal_search" in source
    assert '"dominance_policy": "disabled"' in source
    assert '"state_bound_policy": "absent"' in source
    assert "certified_only" not in source
    assert "TemporalDominancePolicy" not in source


def test_child_command_fences_mode_and_slice(tmp_path: Path) -> None:
    args = _args(tmp_path, "--cpu", "3", "--slice-expansions", "7")
    command = _RUNNER._child_command(args, _RUNNER.OBJECTIVES[0], 2, "slice_restore")
    assert "--worker" in command
    assert command[command.index("--mode") + 1] == "slice_restore"
    assert command[command.index("--slice-expansions") + 1] == "7"
    assert command[command.index("--output-dir") + 1] == str(tmp_path / "out")
    assert command[command.index("--cpu") + 1] == "3"


def _case(objective: str, mode: str, *, status: str = "GOAL_FOUND", digest: str = "route") -> dict:
    return {
        "schema_version": _RUNNER.SCHEMA_VERSION,
        "experiment_id": "experiment",
        "objective": objective,
        "repetition": 1,
        "mode": mode,
        "status": status,
        "session_identity": "session",
        "semantic": {"nodes": [[0, 0], [0, 1]]} if status == "GOAL_FOUND" else None,
        "semantic_digest": digest if status == "GOAL_FOUND" else None,
        "reference": {} if status == "GOAL_FOUND" else None,
        "reference_match": status == "GOAL_FOUND",
        "checkpoint": {"reached": mode != "one_shot"},
        "restored_session_id": "session" if mode != "one_shot" else None,
        "unexpected_pruning": False,
        "resource_clean": True,
        "resources_before": {"cpu_affinity": [0], "cgroup": {"memory_events": {}}},
        "resources_after": {"cpu_affinity": [0], "cgroup": {"memory_events": {}}},
        "dominance_policy": "disabled",
        "state_bound_policy": "absent",
    }


def test_pair_requires_route_equivalence_and_cancel_without_partial() -> None:
    group = {
        mode: _case("fastest", mode)
        for mode in _RUNNER.MODES
    }
    group["cancelled"].update(
        {
            "status": "CANCELLED",
            "semantic": None,
            "semantic_digest": None,
            "reference": None,
            "reference_match": None,
        }
    )
    assert _RUNNER._pair_ok(group)
    group["slice_restore"]["semantic_digest"] = "different"
    assert not _RUNNER._pair_ok(group)


def test_resume_rejects_duplicate_or_unsafe_records(tmp_path: Path) -> None:
    safe = _case("fastest", "one_shot")
    (tmp_path / "cases.jsonl").write_text(json.dumps(safe) + "\n", encoding="utf-8")
    cases, malformed = _RUNNER._load_resume_cases(tmp_path, "experiment")
    assert malformed == 0
    assert len(cases) == 1
    unsafe = dict(safe, dominance_policy="enabled")
    (tmp_path / "cases.jsonl").write_text(json.dumps(unsafe) + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="safety fence"):
        _RUNNER._load_resume_cases(tmp_path, "experiment")


def test_summary_never_promotes_partial_mode_matrix(tmp_path: Path) -> None:
    args = _args(tmp_path)
    cases = [
        _case(objective.value, mode)
        for objective in _RUNNER.OBJECTIVES
        for mode in _RUNNER.MODES
    ]
    for case in cases:
        case["resources_before"]["cgroup"] = {}
        case["resources_after"]["cgroup"] = {}
        if case["mode"] == "cancelled":
            case.update(
                {
                    "status": "CANCELLED",
                    "semantic": None,
                    "semantic_digest": None,
                    "reference": None,
                    "reference_match": None,
                }
            )
    summary = _RUNNER._summary(cases, args, 0)
    assert summary["status"] == "REAL_INPUT_SESSION_RESOURCE_FAIL"
    assert summary["all_pairs_equivalent"]
    assert summary["candidate_authorized"] is False
    assert summary["winter_authorized"] is False
