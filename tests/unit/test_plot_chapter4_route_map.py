"""Fail-closed checks for the Chapter 4 retrospective route-map exporter."""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "plot_chapter4_route_map.py"
_SPEC = importlib.util.spec_from_file_location("c_plot_chapter4_route_map", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


@pytest.fixture(scope="module")
def evidence():
    return _SCRIPT.load_evidence(_SCRIPT.DEFAULT_BUNDLE, _SCRIPT.DEFAULT_BASEMAP)


def test_source_has_expected_grid_and_replanning_chain(evidence) -> None:
    assert len(evidence.latitudes) == 31
    assert len(evidence.longitudes) == 11
    assert len(evidence.risk_frame["risk_scores"]) == 341
    assert evidence.risk_frame["valid_time"] == _SCRIPT.DECISION_TIME
    assert evidence.source_timeline_time == "2026-02-15T14:18:00Z"


def test_routes_are_source_native_and_diverge_after_same_adoption_point(evidence) -> None:
    assert _SCRIPT._xy(evidence.completed_track[-1]) == pytest.approx(evidence.adoption_position)
    assert _SCRIPT._xy(evidence.superseded_route[0]) == pytest.approx(evidence.adoption_position)
    assert _SCRIPT._xy(evidence.adopted_route[0]) == pytest.approx(evidence.adoption_position)
    assert _SCRIPT._xy(evidence.superseded_route[-1]) == pytest.approx(
        _SCRIPT._xy(evidence.adopted_route[-1])
    )
    assert tuple(map(_SCRIPT._xy, evidence.superseded_route)) != tuple(
        map(_SCRIPT._xy, evidence.adopted_route)
    )


def test_editable_csv_exports_preserve_counts_and_hard_reasons(tmp_path: Path, evidence) -> None:
    route_csv, risk_csv = _SCRIPT.write_evidence_tables(tmp_path, evidence)
    with route_csv.open(encoding="utf-8", newline="") as handle:
        route_rows = list(csv.DictReader(handle))
    with risk_csv.open(encoding="utf-8", newline="") as handle:
        risk_rows = list(csv.DictReader(handle))

    expected_route_rows = (
        len(evidence.completed_track) + len(evidence.superseded_route) + len(evidence.adopted_route)
    )
    assert len(route_rows) == expected_route_rows
    assert len(risk_rows) == 341
    assert {row["hard_reason"] for row in risk_rows} == {"NONE", "LAND", "DATA_UNAVAILABLE"}
    assert sum(row["hard_reason"] == "LAND" for row in risk_rows) == 65
    assert sum(row["hard_reason"] == "DATA_UNAVAILABLE" for row in risk_rows) == 55


def test_coordinate_dimension_mismatch_fails_closed(evidence) -> None:
    malformed = deepcopy(evidence.bundle)
    frame = next(
        item for item in malformed["risk"]["frames"] if item["valid_time"] == _SCRIPT.DECISION_TIME
    )
    frame["coordinates"]["latitude"] = frame["coordinates"]["latitude"][:-1]
    with pytest.raises(_SCRIPT.EvidenceError, match="latitude coordinates"):
        _SCRIPT.extract_evidence(malformed)


def test_missing_adoption_event_fails_closed(evidence) -> None:
    malformed = deepcopy(evidence.bundle)
    malformed["events"] = [
        event
        for event in malformed["events"]
        if not (event.get("type") == "REPLAN_ADOPTED" and event.get("rev") == "3")
    ]
    with pytest.raises(_SCRIPT.EvidenceError, match="event chain"):
        _SCRIPT.extract_evidence(malformed)


def test_bundle_digest_drift_fails_before_render(tmp_path: Path) -> None:
    changed_bundle = tmp_path / "bundle.json"
    shutil.copyfile(_SCRIPT.DEFAULT_BUNDLE, changed_bundle)
    with changed_bundle.open("a", encoding="utf-8") as handle:
        handle.write(" \n")
    with pytest.raises(_SCRIPT.EvidenceError, match="SHA256"):
        _SCRIPT.load_evidence(changed_bundle, _SCRIPT.DEFAULT_BASEMAP)


def test_metadata_is_valid_json_after_generation_contract(tmp_path: Path, evidence) -> None:
    route_csv, risk_csv = _SCRIPT.write_evidence_tables(tmp_path, evidence)
    placeholder_png = tmp_path / "fig-route-replanning-map.png"
    placeholder_svg = tmp_path / "fig-route-replanning-map.svg"
    placeholder_png.write_bytes(b"png-placeholder")
    placeholder_svg.write_text("<svg/>", encoding="utf-8")
    metadata = tmp_path / "metadata.json"
    _SCRIPT._write_metadata(
        metadata,
        _SCRIPT.DEFAULT_BUNDLE,
        _SCRIPT.DEFAULT_BASEMAP,
        evidence,
        (placeholder_png, placeholder_svg, route_csv, risk_csv),
    )
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    assert payload["evidence_kind"] == "RETROSPECTIVE_DYNAMIC_REPLAY_PRESENTATION"
    assert payload["qualification_boundary"] == "NOT_LIVE_CAUSAL_OR_NAVIGATION_GRADE"
    assert payload["grid"]["cells"] == 341
