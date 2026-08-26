from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

SCHEMA_ROOT = Path(__file__).parents[2] / "schemas"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _risk_frame_document() -> dict[str, object]:
    return {
        "schema_version": "bc.risk-frame.v2",
        "risk_id": f"risk-sha256-{'1' * 64}",
        "run_id": "run-00000000-0000-4000-8000-000000000001",
        "scenario_id": "scenario-1",
        "corridor_id": "corridor-1",
        "vessel_profile_id": "demo_bulk_carrier_v1",
        "config_digest": "a" * 64,
        "model_config_digest": "b" * 64,
        "generation_id": 0,
        "valid_time": "2026-07-31T01:00:00Z",
        "as_of_time": "2026-07-31T00:00:00Z",
        "generated_at": "2026-07-31T00:00:00Z",
        "model_version": "fixture.v1",
        "payload": {
            "coordinates": {"latitude": [70.0, 71.0], "longitude": [10.0, 11.0]},
            "variables": {
                "risk_score": [[0.1, 0.2], [None, 0.3]],
                "risk_level": [[1, 2], [5, 2]],
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


def test_risk_frame_schema_accepts_canonical_transport_shape() -> None:
    document = _risk_frame_document()
    validator = _validator("risk-frame-v2.schema.json")
    validator.validate(document)


def test_risk_frame_schema_rejects_unknown_top_level_field() -> None:
    document = _risk_frame_document()
    document["unexpected"] = True

    with pytest.raises(ValidationError):
        _validator("risk-frame-v2.schema.json").validate(document)


def test_formal_risk_frame_requires_speed_factor() -> None:
    document = deepcopy(_risk_frame_document())
    document["provenance"] = "formal"
    del document["payload"]["variables"]["environment_speed_factor"]  # type: ignore[index]

    with pytest.raises(ValidationError):
        _validator("risk-frame-v2.schema.json").validate(document)


def test_formal_risk_frame_rejects_missing_source_issue_time() -> None:
    document = deepcopy(_risk_frame_document())
    document["provenance"] = "formal"
    document["source_summary"][0]["issue_time"] = None  # type: ignore[index]

    with pytest.raises(ValidationError):
        _validator("risk-frame-v2.schema.json").validate(document)


@pytest.mark.parametrize("field", ("data_id", "valid_time", "checksum"))
def test_formal_risk_frame_rejects_other_missing_source_identity(field: str) -> None:
    document = deepcopy(_risk_frame_document())
    document["provenance"] = "formal"
    document["source_summary"][0][field] = None  # type: ignore[index]

    with pytest.raises(ValidationError):
        _validator("risk-frame-v2.schema.json").validate(document)


def test_risk_frame_schema_rejects_incomplete_payload_shape() -> None:
    document = deepcopy(_risk_frame_document())
    del document["payload"]["attributes"]  # type: ignore[index]

    with pytest.raises(ValidationError):
        _validator("risk-frame-v2.schema.json").validate(document)


def test_route_plan_schema_accepts_contract_shape() -> None:
    document = {
        "schema_version": "cd.route-plan.v2",
        "run_id": "run-1",
        "scenario_id": "scenario-1",
        "corridor_id": "corridor-1",
        "vessel_profile_id": "demo_bulk_carrier_v1",
        "config_digest": "c" * 64,
        "model_config_digest": "d" * 64,
        "planner_config_digest": "e" * 64,
        "provenance": "synthetic",
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

    _validator("route-plan-v2.schema.json").validate(document)

    del document["provenance"]
    with pytest.raises(ValidationError):
        _validator("route-plan-v2.schema.json").validate(document)


def _selection_rationale_document() -> dict[str, object]:
    return {
        "schema_version": "selection-rationale.v1",
        "run_id": "run-1",
        "scenario_id": "scenario-1",
        "corridor_id": "corridor-1",
        "vessel_profile_id": "demo_bulk_carrier_v1",
        "config_digest": "c" * 64,
        "model_config_digest": "d" * 64,
        "planner_config_digest": "e" * 64,
        "provenance": "synthetic",
        "generation_id": 0,
        "planning_request_id": "request-1",
        "input_revision": 0,
        "selected_plan_id": "recommended-1",
        "baseline_plan_id": "fastest-1",
        "selected_objective": "recommended",
        "baseline_objective": "fastest",
        "tradeoffs": {
            "delta_distance_km": -10.5,
            "delta_eta_hours": 0.75,
            "delta_avg_risk": -0.04,
            "delta_max_risk": 0.02,
            "delta_integrated_risk_hours": -2.6,
            "avg_risk_reduction_pct": 8.0,
            "max_risk_reduction_pct": -4.0,
        },
        "summary_text": "相比最快路线，推荐路线平均风险减少 8.0%",
    }


def test_selection_rationale_schema_accepts_canonical_shape() -> None:
    _validator("selection-rationale-v1.schema.json").validate(
        _selection_rationale_document()
    )


def test_selection_rationale_schema_rejects_non_fastest_baseline() -> None:
    document = deepcopy(_selection_rationale_document())
    document["baseline_objective"] = "low_risk"

    with pytest.raises(ValidationError):
        _validator("selection-rationale-v1.schema.json").validate(document)


def test_selection_rationale_schema_rejects_unknown_top_level_field() -> None:
    document = deepcopy(_selection_rationale_document())
    document["unexpected"] = True

    with pytest.raises(ValidationError):
        _validator("selection-rationale-v1.schema.json").validate(document)


@pytest.mark.parametrize(
    "field",
    (
        "selected_plan_id",
        "baseline_plan_id",
        "tradeoffs",
        "summary_text",
        "selected_objective",
    ),
)
def test_selection_rationale_schema_rejects_missing_required_fields(
    field: str,
) -> None:
    document = deepcopy(_selection_rationale_document())
    del document[field]  # type: ignore[index]

    with pytest.raises(ValidationError):
        _validator("selection-rationale-v1.schema.json").validate(document)
