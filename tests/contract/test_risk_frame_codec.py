from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from jsonschema import Draft202012Validator, FormatChecker

from arctic_route_planning.adapters import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import (
    ProvenanceKind,
    canonical_risk_frame_bytes,
    canonical_risk_id,
    risk_frame_content_digest,
    risk_frame_from_document,
    risk_frame_to_document,
)
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.errors import ContractError

PROJECT_ROOT = Path(__file__).parents[2]
CONFIG_ROOT = PROJECT_ROOT / "configs"


def _frame():
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    return FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=create_development_run_context(configuration, source_kind="synthetic"),
        frame_count=2,
        shape=(3, 4),
    ).frames[0]


def _frame_with_unknown_risk():
    frame = _frame()
    payload = frame.payload.copy(deep=True)
    risk = np.asarray(payload["risk_score"].values).copy()
    level = np.asarray(payload["risk_level"].values).copy()
    hard = np.asarray(payload["hard_mask"].values).copy()
    confidence = np.asarray(payload["confidence"].values).copy()
    risk[0, 0] = np.nan
    level[0, 0] = 5
    hard[0, 0] = True
    confidence[0, 0] = 0.0
    payload["risk_score"] = (("latitude", "longitude"), risk)
    payload["risk_level"] = (("latitude", "longitude"), level)
    payload["hard_mask"] = (("latitude", "longitude"), hard)
    payload["confidence"] = (("latitude", "longitude"), confidence)
    return replace(frame, payload=payload)


def test_codec_round_trip_maps_nan_to_json_null_and_matches_schema() -> None:
    frame = _frame_with_unknown_risk()

    document = risk_frame_to_document(frame)
    restored = risk_frame_from_document(document)

    assert document["payload"]["variables"]["risk_score"][0][0] is None
    assert np.isnan(restored.payload["risk_score"].values[0, 0])
    assert int(restored.payload["risk_level"].values[0, 0]) == 5
    np.testing.assert_allclose(
        restored.payload["confidence"], frame.payload["confidence"], equal_nan=True
    )
    schema = json.loads(
        (PROJECT_ROOT / "schemas/risk-frame-v2.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(document)
    assert canonical_risk_frame_bytes(restored) == canonical_risk_frame_bytes(frame)


def test_content_digest_excludes_only_risk_id() -> None:
    frame = _frame()
    renamed = replace(frame, risk_id="another-readable-synthetic-id")
    changed_model = replace(frame, model_version="fixture-risk.v2")

    assert risk_frame_content_digest(frame) == risk_frame_content_digest(renamed)
    assert risk_frame_content_digest(frame) != risk_frame_content_digest(changed_model)


def test_formal_codec_requires_full_canonical_risk_id() -> None:
    draft = replace(_frame(), risk_id="draft", provenance=ProvenanceKind.FORMAL)
    with pytest.raises(ContractError, match="risk_id"):
        risk_frame_to_document(draft)
    formal = replace(draft, risk_id=canonical_risk_id(draft))
    document = risk_frame_to_document(formal)

    assert risk_frame_from_document(document).risk_id == formal.risk_id
    tampered = deepcopy(document)
    tampered["risk_id"] = f"risk-sha256-{'0' * 64}"
    with pytest.raises(ContractError, match="risk_id"):
        risk_frame_from_document(tampered)


def test_codec_rejects_extra_payload_variable_and_non_z_time() -> None:
    document = risk_frame_to_document(_frame())
    document["payload"]["variables"]["route_cost"] = document["payload"]["variables"][
        "risk_score"
    ]
    with pytest.raises(ContractError, match="extra"):
        risk_frame_from_document(document)

    document = risk_frame_to_document(_frame())
    document["valid_time"] = document["valid_time"].replace("Z", "+00:00")
    with pytest.raises(ContractError, match="Z"):
        risk_frame_from_document(document)


def test_python_contract_rejects_lossy_auxiliary_coordinate() -> None:
    frame = _frame()
    payload = frame.payload.assign_coords(row_index=("latitude", [0, 1, 2]))

    with pytest.raises(ContractError, match="未声明坐标"):
        replace(frame, payload=payload)


@pytest.mark.parametrize("generation_id", (True, 0.5))
def test_codec_rejects_non_transport_integer_generation(generation_id: object) -> None:
    document = risk_frame_to_document(_frame())
    document["generation_id"] = generation_id

    with pytest.raises(ContractError, match="generation_id"):
        risk_frame_from_document(document)


def test_risk_level_is_frozen_from_finite_score_and_unknown_is_level_five() -> None:
    frame = _frame()
    payload = frame.payload.copy(deep=True)
    levels = np.asarray(payload["risk_level"].values).copy()
    levels[0, 0] = 5 if levels[0, 0] != 5 else 1
    payload["risk_level"] = (("latitude", "longitude"), levels)
    with pytest.raises(ContractError, match="floor"):
        replace(frame, payload=payload)

    unknown = _frame_with_unknown_risk()
    payload = unknown.payload.copy(deep=True)
    levels = np.asarray(payload["risk_level"].values).copy()
    levels[0, 0] = 1
    payload["risk_level"] = (("latitude", "longitude"), levels)
    with pytest.raises(ContractError, match="未知风险必须为 5"):
        replace(unknown, payload=payload)
