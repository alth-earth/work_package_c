from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).parents[2].joinpath("scripts/validate_temporal_semantics.py")
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "c_validate_temporal_semantics",
    _SCRIPT_PATH,
)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError("unable to load P0 validation script")
_SCRIPT = importlib.util.module_from_spec(_SCRIPT_SPEC)
sys.modules[_SCRIPT_SPEC.name] = _SCRIPT
_SCRIPT_SPEC.loader.exec_module(_SCRIPT)
run_script = _SCRIPT.main
run_validation = _SCRIPT.run_validation


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|UNKNOWN)$")
_TIMING_KEYS = frozenset({"elapsed_ms", "planner_compute_ms"})


def _without_timings(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_timings(item)
            for key, item in value.items()
            if key not in _TIMING_KEYS
        }
    if isinstance(value, list):
        return [_without_timings(item) for item in value]
    return value


def _read_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _load_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    cases = _read_cases(path / "cases.jsonl")
    return manifest, cases


def _assert_schema(manifest: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    assert manifest["schema_version"] == "c.p0-temporal-semantics.v1"
    assert manifest["status"] == "EXPERIMENTAL"
    assert manifest["production_defaults_changed"] is False
    assert manifest["formal_ingress_used"] is False
    assert manifest["frozen_artifact_written"] is False
    assert manifest["repetitions"] == 2
    assert manifest["serial_execution"] is True
    assert _GIT_SHA.fullmatch(manifest["environment"]["git_sha"])
    assert isinstance(manifest["environment"]["git_worktree_dirty"], bool)
    assert _HEX64.fullmatch(manifest["environment"]["uv_lock_sha256"])
    assert set(manifest["environment"]["implementation_sha256"]) == {
        "scripts/validate_temporal_semantics.py",
        "src/arctic_route_planning/planners/eta_refinement.py",
        "src/arctic_route_planning/planners/temporal_label_astar.py",
        "tests/reference_temporal_oracle.py",
    }
    assert all(
        _HEX64.fullmatch(value)
        for value in manifest["environment"]["implementation_sha256"].values()
    )
    assert manifest["environment"]["python_version"]
    assert manifest["environment"]["platform"]
    assert manifest["policy"]["eta_refinement"]["max_iterations"] == 12
    assert manifest["policy"]["search_limits"]["max_labels"] == 100_000
    assert manifest["strategies"]["control"]["planner"] == "TimeDependentAStar"
    assert manifest["strategies"]["candidate"]["planner"] == "TemporalLabelAStar"
    assert manifest["validation"] == {
        "all_cases_success": True,
        "case_count": 2,
        "failed_cases": [],
        "verdict": "PASS",
    }

    assert len(cases) == 2
    for index, case in enumerate(cases, start=1):
        assert case["schema_version"] == manifest["schema_version"]
        assert case["run_index"] == index
        assert case["fixture_id"] == manifest["fixture_id"]
        assert case["comparison"] == {
            "route_digest_equal": True,
            "semantic_match": True,
        }
        for strategy in ("control", "candidate"):
            result = case[strategy]
            assert result["status"] == "SUCCESS"
            assert _HEX64.fullmatch(result["route_digest"])
            assert isinstance(result["elapsed_ms"], float)
            assert isinstance(result["planner_compute_ms"], float)
            assert result["route"]["nodes"]
            assert result["metrics"]["expanded_states"] > 0


def test_p0_validation_writes_schema_and_deterministic_cases(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"

    assert run_script(["--output-dir", str(first_dir), "--repetitions", "2"]) == 0
    assert run_script(["--output-dir", str(second_dir), "--repetitions", "2"]) == 0
    first_loaded, first_cases = _load_run(first_dir)
    second_loaded, second_cases = _load_run(second_dir)

    _assert_schema(first_loaded, first_cases)
    _assert_schema(second_loaded, second_cases)
    assert _without_timings(first_loaded) == _without_timings(second_loaded)
    assert _without_timings(first_cases) == _without_timings(second_cases)


def test_p0_validation_does_not_depend_on_test_or_formal_ingress_modules() -> None:
    source = Path(__file__).parents[2].joinpath("scripts/validate_temporal_semantics.py")
    text = source.read_text(encoding="utf-8")

    assert "from tests" not in text
    assert "import tests" not in text
    assert "ReferenceTemporalOracle" not in text
    assert "RiskSourcePlanningIngress" not in text

    source_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(source_digest) == 64


def test_p0_validation_refuses_to_overwrite_existing_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    (output_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"manifest\.json"):
        run_validation(output_dir=output_dir, repetitions=1)


def _assert_p1_schema(manifest: dict[str, Any], cases: list[dict[str, Any]]) -> None:
    assert manifest["schema_version"] == "c.p1-temporal-session.v1"
    assert manifest["status"] == "EXPERIMENTAL"
    assert manifest["production_defaults_changed"] is False
    assert manifest["formal_ingress_used"] is False
    assert manifest["frozen_artifact_written"] is False
    assert manifest["repetitions"] == 2
    assert manifest["serial_execution"] is True
    assert manifest["policy"]["session_slice_expansions"] == 1
    assert manifest["strategies"]["session_candidate"]["role"] == (
        "experimental_shadow_sliced_restored"
    )
    assert manifest["validation"] == {
        "all_cases_success": True,
        "case_count": 2,
        "failed_cases": [],
        "verdict": "PASS",
    }
    assert len(cases) == 2
    for index, case in enumerate(cases, start=1):
        assert case["schema_version"] == manifest["schema_version"]
        assert case["run_index"] == index
        assert case["comparison"] == {
            "semantic_match": True,
            "control_candidate_route_digest_equal": True,
            "candidate_session_route_digest_equal": True,
            "control_session_route_digest_equal": True,
            "candidate_session_metrics_equal": True,
            "candidate_session_diagnostics_equal": True,
        }
        session_result = case["session_candidate"]
        assert session_result["status"] == "SUCCESS"
        assert session_result["session"]["terminal_state"] == "GOAL_CERTIFIED"
        assert session_result["session"]["pause_count"] > 0
        assert session_result["session"]["checkpoint_count"] == (
            session_result["session"]["pause_count"]
        )
        assert _HEX64.fullmatch(session_result["session"]["checkpoint_digest"])
        assert _HEX64.fullmatch(session_result["session"]["identity"]["digest"])
        assert session_result["session"]["cumulative_metrics"] == session_result["metrics"]
        assert _HEX64.fullmatch(session_result["route_digest"])


def test_p1_session_mode_is_explicit_and_deterministic(tmp_path: Path) -> None:
    first_dir = tmp_path / "p1-first"
    second_dir = tmp_path / "p1-second"

    assert (
        run_script(
            [
                "--output-dir",
                str(first_dir),
                "--repetitions",
                "2",
                "--session-slice-expansions",
                "1",
            ]
        )
        == 0
    )
    assert (
        run_script(
            [
                "--output-dir",
                str(second_dir),
                "--repetitions",
                "2",
                "--session-slice-expansions",
                "1",
            ]
        )
        == 0
    )
    first_manifest, first_cases = _load_run(first_dir)
    second_manifest, second_cases = _load_run(second_dir)
    _assert_p1_schema(first_manifest, first_cases)
    _assert_p1_schema(second_manifest, second_cases)
    assert _without_timings(first_manifest) == _without_timings(second_manifest)
    assert _without_timings(first_cases) == _without_timings(second_cases)


def test_p1_rejects_non_positive_session_slice(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session expansion slice must be positive"):
        run_validation(
            output_dir=tmp_path / "invalid",
            repetitions=1,
            session_slice_expansions=0,
        )


def test_p1_refuses_to_overwrite_existing_artifacts(tmp_path: Path) -> None:
    output_dir = tmp_path / "existing-p1"
    output_dir.mkdir()
    (output_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match=r"manifest\.json"):
        run_validation(
            output_dir=output_dir,
            repetitions=1,
            session_slice_expansions=1,
        )
