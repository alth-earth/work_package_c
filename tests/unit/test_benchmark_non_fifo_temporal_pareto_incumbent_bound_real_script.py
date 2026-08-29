"""Focused checks for the M24 real incumbent-bound qualification runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "benchmark_non_fifo_temporal_pareto_incumbent_bound_real.py"
)
_SPEC = importlib.util.spec_from_file_location("c_m24_real_incumbent_runner", _SCRIPT)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load M24 real incumbent-bound runner")
_RUNNER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _RUNNER
_SPEC.loader.exec_module(_RUNNER)


def _case(
    objective: str,
    *,
    status: str = "REAL_INPUT_INCUMBENT_BOUND_UNCERTAIN",
    candidate_started: bool = False,
    pruning: int = 0,
) -> dict[str, object]:
    return {
        "mode": "qualification",
        "objective": objective,
        "repetition": 1,
        "status": status,
        "candidate_started": candidate_started,
        "incumbent_bound_pruned": pruning,
    }


def _identity() -> dict[str, object]:
    return {
        "experiment_id": "m24-test",
        "mode": "qualification",
        "repetitions": 1,
        "git": {"dirty": False},
    }


def test_schema_parser_and_frozen_limits() -> None:
    assert _RUNNER.SCHEMA_VERSION == "c.p0.2-nonfifo-pareto-incumbent-bound-real.v1"
    assert _RUNNER.LIMITS == {
        "max_expansions": 50_000,
        "max_labels": 100_000,
        "max_queue": 50_000,
        "max_edge_evaluations": 400_000,
    }
    args = _RUNNER._parser().parse_args(
        [
            "--risk-window-commit",
            "/tmp/window",
            "--route-plan-set",
            "/tmp/routes",
            "--config-root",
            "/tmp/config",
            "--segment",
            "rolling_0_24h",
            "--output-dir",
            "/tmp/m24",
        ]
    )
    assert args.mode == "qualification"
    assert args.repetitions == 1
    assert args.worker_timeout_seconds == 900.0


def test_summary_accepts_only_fail_closed_uncertain_qualification() -> None:
    cases = [_case(objective) for objective in ("fastest", "low_risk", "recommended")]
    summary = _RUNNER._summary(cases, _identity(), 0)

    assert summary["status"] == "REAL_INPUT_INCUMBENT_BOUND_UNCERTAIN"
    assert summary["complete"] is True
    assert summary["fail_closed"] is True
    assert summary["candidate_started"] is False
    assert summary["incumbent_bound_pruned_total"] == 0
    assert summary["candidate_authorized"] is False


@pytest.mark.parametrize(
    "changed",
    [
        {"candidate_started": True},
        {"incumbent_bound_pruned": 1},
        {"status": "INVALID/FAIL"},
    ],
)
def test_summary_rejects_fail_open_or_invalid_cases(changed: dict[str, object]) -> None:
    cases = [_case(objective) for objective in ("fastest", "low_risk", "recommended")]
    cases[0].update(changed)
    summary = _RUNNER._summary(cases, _identity(), 0)
    assert summary["status"] == "INVALID/FAIL"
    assert summary["candidate_authorized"] is False


def test_load_certificate_normalizes_json_state_and_times(tmp_path: Path) -> None:
    payload = {
        "schema_version": "c.p0.2-nonfifo-pareto-incumbent-bound.v1",
        "status": "CERTIFIED",
        "scope_digest": "scope-m24",
        "goal": ["goal", None],
        "objective_count": 2,
        "state_lower_bounds": [
            [
                [[1, 2], "2026-08-29T00:00:00Z"],
                ["2026-08-29T01:00:00Z", [0.5, 0.25]],
            ]
        ],
        "coverage_complete": True,
        "evaluator_certified": True,
        "proof_digest": "proof-m24",
    }
    path = tmp_path / "fastest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    certificate = _RUNNER._load_certificate(path)

    assert certificate.usable is True
    assert certificate.scope_digest == "scope-m24"
    assert certificate.goal == ("goal", None)
    assert certificate.lower_bound((1, 2), certificate.state_lower_bounds[0][0][1]) == (
        certificate.state_lower_bounds[0][1][0],
        (0.5, 0.25),
    )
