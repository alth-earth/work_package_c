from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_ROOT = Path(__file__).parents[2] / "schemas"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_risk_frame_schema_accepts_canonical_transport_shape() -> None:
    document = {
        "schema_version": "bc.risk-frame.v1",
        "risk_id": "risk-1",
        "scenario_id": "scenario-1",
        "corridor_id": "corridor-1",
        "vessel_profile_id": "demo_bulk_carrier_v1",
        "config_digest": "a" * 64,
        "generation_id": 0,
        "valid_time": "2026-07-31T01:00:00Z",
        "as_of_time": "2026-07-31T00:00:00Z",
        "generated_at": "2026-07-31T00:00:00Z",
        "model_version": "fixture.v1",
        "payload": {
            "coordinates": {"latitude": [70.0, 71.0], "longitude": [10.0, 11.0]},
            "variables": {
                "risk_score": [[0.1, 0.2], [None, 0.3]],
                "risk_level": [[1, 2], [1, 2]],
                "hard_mask": [[False, False], [True, False]],
                "confidence": [[0.9, 0.9], [0.0, 0.8]],
                "environment_speed_factor": [[1.0, 0.9], [0.8, 0.7]],
            },
            "attributes": {"crs": "EPSG:4326", "development_only": True},
        },
        "source_summary": [
            {
                "source_id": "fixture",
                "data_id": "fixture-1",
                "issue_time": "2026-07-31T00:00:00Z",
                "valid_time": "2026-07-31T01:00:00Z",
                "version": "v1",
                "quality_flag": "synthetic",
                "checksum": "b" * 64,
            }
        ],
        "provenance": "synthetic",
    }

    validator = _validator("risk-frame-v1.schema.json")
    validator.validate(document)

    document["provenance"] = "formal"
    del document["payload"]["variables"]["environment_speed_factor"]
    errors = list(validator.iter_errors(document))
    assert any("environment_speed_factor" in error.message for error in errors)


def test_route_plan_schema_accepts_contract_shape() -> None:
    document = {
        "schema_version": "cd.route-plan.v1",
        "scenario_id": "scenario-1",
        "corridor_id": "corridor-1",
        "vessel_profile_id": "demo_bulk_carrier_v1",
        "config_digest": "c" * 64,
        "generation_id": 0,
        "plan_id": "plan-1",
        "plan_version": "1",
        "planning_request_id": "request-1",
        "input_revision": 0,
        "generated_at": "2026-07-31T00:00:01Z",
        "as_of_time": "2026-07-31T00:00:00Z",
        "start_time": "2026-07-31T00:00:00Z",
        "objective_mode": "recommended",
        "plan_kind": "initial",
        "waypoints": [
            {
                "longitude": 10.0,
                "latitude": 70.0,
                "eta": "2026-07-31T00:00:00Z",
                "recommended_speed_mps": 6.9,
            },
            {
                "longitude": 11.0,
                "latitude": 71.0,
                "eta": "2026-07-31T02:00:00Z",
                "recommended_speed_mps": 6.5,
            },
        ],
        "metrics": {
            "distance_km": 120.0,
            "eta_hours": 2.0,
            "avg_risk": 0.2,
            "max_risk": 0.4,
            "integrated_risk_hours": 0.4,
            "minimum_confidence": 0.8,
            "hard_constraint_violations": 0,
            "turn_count": 0,
            "expanded_nodes": 10,
            "compute_ms": 5.0,
            "objective_cost": 3.0,
        },
        "replan_reasons": [],
        "source_risk_ids": ["risk-1"],
        "planner_version": "planner.v1",
        "destination_reached": True,
    }

    _validator("route-plan-v1.schema.json").validate(document)
