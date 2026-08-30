"""Tests for the bounded geometry-only route-smoothing artifact runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arctic_route_planning.research.route_smoothing_experiment import (
    run_geometry_experiment,
)


def test_geometry_experiment_records_inherited_identities_without_safety_claims(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    route = {
        "route_id": "route-experiment-test",
        "revision": 3,
        "effective_adoption_time": "2026-01-01T00:00:00Z",
        "layer": "full_voyage",
        "objective": "recommended",
        "waypoints": [
            {"lon": 0.0, "lat": 0.0, "eta": start.isoformat().replace("+00:00", "Z")},
            {
                "lon": 0.2,
                "lat": 0.0,
                "eta": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            },
            {
                "lon": 0.2,
                "lat": 0.2,
                "eta": (start + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
            },
        ],
    }
    document = {
        "routes": [route],
        "route_candidates": {
            "selected_candidate_id": route["route_id"],
            "candidates": [
                {
                    "candidate_id": route["route_id"],
                    "provenance": {
                        "scenario_id": "synthetic-scenario",
                        "corridor_id": "synthetic-corridor",
                        "vessel_profile_id": "nordic_odyssey_reference_v1",
                        "model_config_digest": "m" * 64,
                        "config_digest": "c" * 64,
                    },
                }
            ],
        },
        "research_validation": {
            "risk_schema": "bc.risk-frame.v2",
            "risk_frame_count": 2,
            "risk_window_id": "risk-window",
            "dataset_bundle_id": "dataset",
        },
        "risk": {
            "source": {
                "schema_version": "bc.risk-frame.v2",
                "scenario_id": "synthetic-scenario",
                "run_id": "run",
                "risk_window_id": "risk-window",
                "risk_window_digest": "r" * 64,
            },
            "grid": {"rows": 3, "cols": 3},
        },
    }
    input_path = tmp_path / "bundle.json"
    input_path.write_text(json.dumps(document), encoding="utf-8")
    output_dir = tmp_path / "experiment"

    summary = run_geometry_experiment(
        input_path,
        output_dir,
        experiment_id="c.route-smoothing.experiment.synthetic.v1",
    )

    assert summary["status"] == "PASS"
    assert summary["evidence_level"] == "GEOMETRY_ONLY"
    assert summary["corner_count"] == 1
    assert summary["risk_evidence"] == "NOT_EVALUATED"
    assert summary["production_qualified"] is False
    for name in (
        "manifest.json",
        "cases.jsonl",
        "summary.json",
        "route-smoothing-sidecar.json",
        "baseline.json",
        "ALL_DONE",
    ):
        assert (output_dir / name).exists()

    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["route_identity"]["revision"] == 3
    assert manifest["vessel_profile_identity"]["vessel_profile_id"] == (
        "nordic_odyssey_reference_v1"
    )
    assert manifest["qualification_status"] == "NOT_EVALUATED"
    assert manifest["resource_evidence_complete"] is False
