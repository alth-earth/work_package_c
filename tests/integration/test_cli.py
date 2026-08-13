from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from arctic_route_contracts import write_run_context_atomic

from arctic_route_planning import cli
from arctic_route_planning.adapters import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.development import create_development_run_context

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


def test_help_lists_runtime_entry_points(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert "synthetic-demo" in output
    assert "legacy-inspect" in output
    assert "legacy-plan" in output


def test_synthetic_demo_requires_explicit_output_directory() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(["synthetic-demo"])

    assert exit_info.value.code == 2


def test_synthetic_demo_wires_explicit_output_and_reports_development_status(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    output_dir = tmp_path / "explicit-output"

    class FakeFixture:
        def __init__(self, **_kwargs) -> None:
            self.frames = (object(),)

    monkeypatch.setattr(cli, "FixtureRiskSource", FakeFixture)
    monkeypatch.setattr(
        cli,
        "_plan_frames",
        lambda *_args, **_kwargs: (
            object(),
            {"start": {"snap_applied": False}, "destination": {"snap_applied": False}},
        ),
    )

    def fake_write(target, _batch, **kwargs):
        target.mkdir(parents=True)
        summary = {
            "published": True,
            "development_only": True,
            "source_kind": kwargs["source_kind"],
        }
        (target / "run-summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return summary

    monkeypatch.setattr(cli, "_write_outputs", fake_write)

    result = cli.main(
        [
            "--config-root",
            str(CONFIG_ROOT),
            "synthetic-demo",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert result == 0
    assert json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8")) == {
        "published": True,
        "development_only": True,
        "source_kind": "synthetic",
    }
    assert '"development_only": true' in capsys.readouterr().out


def test_legacy_inspect_refuses_missing_explicit_acknowledgements(capsys) -> None:
    result = cli.main(
        [
            "--config-root",
            str(CONFIG_ROOT),
            "legacy-inspect",
            "--archive",
            "/does/not/matter.zip",
            "--as-of",
            "2026-07-31T00:00:00Z",
        ]
    )

    assert result == 2
    assert "--allow-unverified-legacy is required" in capsys.readouterr().err


def test_legacy_plan_requires_bounded_endpoint_snap() -> None:
    parser = cli.build_parser()

    with pytest.raises(SystemExit) as exit_info:
        parser.parse_args(
            [
                "legacy-plan",
                "--archive",
                "legacy.zip",
                "--as-of",
                "2026-07-31T00:00:00Z",
                "--allow-unverified-legacy",
                "--acknowledge-valid-time",
                "--output-dir",
                "output",
            ]
        )

    assert exit_info.value.code == 2


def test_cli_returns_clean_error_instead_of_traceback(monkeypatch, capsys) -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["synthetic-demo", "--output-dir", "output"])
    monkeypatch.setattr(args, "handler", lambda _args: (_ for _ in ()).throw(ValueError("bad")))
    monkeypatch.setattr(cli, "build_parser", lambda: SimpleNamespace(parse_args=lambda _argv: args))

    assert cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == "error: bad"
    assert "Traceback" not in captured.err


def test_plan_frames_rejects_risk_window_beyond_run_context() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    context = create_development_run_context(configuration, source_kind="synthetic")
    source = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=context,
        frame_count=configuration.scenario.horizon_hours + 2,
        shape=(3, 3),
    )

    with pytest.raises(ValueError, match="RiskFrame window extends beyond"):
        cli._plan_frames(
            configuration,
            context,
            source.frames,
            generation_id=0,
            input_revision=0,
            start_time=configuration.scenario.simulation_start,
            max_snap_km=300.0,
        )


def test_cli_rejects_content_tampered_explicit_run_context(tmp_path) -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    context = create_development_run_context(configuration, source_kind="synthetic")
    path = tmp_path / "run-context.json"
    write_run_context_atomic(replace(context, corridor_digest="f" * 64), path)
    args = SimpleNamespace(run_context=path)

    with pytest.raises(ValueError, match="corridor_digest"):
        cli._run_context(args, configuration, source_kind="synthetic")
