"""Canonical JSON transport codec for ``bc.risk-frame.v2``.

The codec is deliberately owned by the public BC contract instead of a B or C
implementation adapter.  Its canonical content digest excludes ``risk_id``
and includes every other transport field.  This makes it possible to derive a
stable full-SHA-256 identifier without a circular dependency::

    risk-sha256-<risk_frame_content_digest(frame)>

Source-reference order is canonicalized because it is a set of evidence, not a
priority list.  Grid coordinate and payload array order remains significant.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)
from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import isoformat_z, parse_utc

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "risk_id",
        "run_id",
        "scenario_id",
        "corridor_id",
        "vessel_profile_id",
        "config_digest",
        "model_config_digest",
        "generation_id",
        "valid_time",
        "as_of_time",
        "generated_at",
        "model_version",
        "payload",
        "source_summary",
        "provenance",
    }
)
_PAYLOAD_FIELDS = frozenset({"coordinates", "variables", "attributes"})
_COORDINATE_FIELDS = frozenset({"latitude", "longitude"})
_REQUIRED_VARIABLES = frozenset({"risk_score", "risk_level", "hard_mask", "confidence"})
_OPTIONAL_VARIABLES = frozenset({"environment_speed_factor", "hard_reason"})
_HARD_REASONS = frozenset({"NONE", "LAND", "DATA_UNAVAILABLE", "OTHER"})
_SOURCE_FIELDS = frozenset(
    {
        "source_id",
        "data_id",
        "issue_time",
        "valid_time",
        "version",
        "quality_flag",
        "checksum",
    }
)
_CANONICAL_RISK_ID = re.compile(r"^risk-sha256-[0-9a-f]{64}$")


def risk_frame_to_document(frame: RiskFrame) -> dict[str, Any]:
    """Encode a validated frame as the canonical JSON-compatible shape.

    NumPy NaN is the Python in-memory representation of unknown risk.  It is
    encoded as JSON ``null``; no other non-finite numeric value is accepted.
    """

    if not isinstance(frame, RiskFrame):
        raise ContractError("risk_frame_to_document 只接受 RiskFrame")
    document = _risk_frame_to_document_unchecked(_validated_snapshot(frame))
    _validate_document_canonical_risk_id(document)
    return document


def _risk_frame_to_document_unchecked(frame: RiskFrame) -> dict[str, Any]:
    """Encode a validated frame without recursively checking its content ID."""

    payload = frame.payload
    variables: dict[str, Any] = {
        "risk_score": _risk_rows(payload["risk_score"].values),
        "risk_level": _array_rows(payload["risk_level"].values, kind="integer"),
        "hard_mask": _array_rows(payload["hard_mask"].values, kind="boolean"),
        "confidence": _array_rows(payload["confidence"].values, kind="number"),
    }
    if "environment_speed_factor" in payload:
        variables["environment_speed_factor"] = _array_rows(
            payload["environment_speed_factor"].values,
            kind="number",
        )
    if "hard_reason" in payload:
        variables["hard_reason"] = _array_rows(
            payload["hard_reason"].values,
            kind="string",
        )
    sources = sorted(
        (_source_to_document(source) for source in frame.source_summary),
        key=_canonical_json_bytes,
    )
    return {
        "schema_version": frame.schema_version,
        "risk_id": frame.risk_id,
        "run_id": frame.run_id,
        "scenario_id": frame.scenario_id,
        "corridor_id": frame.corridor_id,
        "vessel_profile_id": frame.vessel_profile_id,
        "config_digest": frame.config_digest,
        "model_config_digest": frame.model_config_digest,
        "generation_id": frame.generation_id,
        "valid_time": isoformat_z(frame.valid_time),
        "as_of_time": isoformat_z(frame.as_of_time),
        "generated_at": isoformat_z(frame.generated_at),
        "model_version": frame.model_version,
        "payload": {
            "coordinates": {
                "latitude": _coordinate_values(payload["latitude"].values),
                "longitude": _coordinate_values(payload["longitude"].values),
            },
            "variables": variables,
            "attributes": _json_value(dict(payload.attrs), field="payload.attributes"),
        },
        "source_summary": sources,
        "provenance": frame.provenance.value,
    }


def risk_frame_from_document(document: Mapping[str, Any]) -> RiskFrame:
    """Decode and semantically validate one strict v2 transport document."""

    data = _mapping(document, field="RiskFrame")
    _require_exact_fields(data, _TOP_LEVEL_FIELDS, field="RiskFrame")
    payload = _mapping(data["payload"], field="payload")
    _require_exact_fields(payload, _PAYLOAD_FIELDS, field="payload")
    coordinates = _mapping(payload["coordinates"], field="payload.coordinates")
    _require_exact_fields(coordinates, _COORDINATE_FIELDS, field="payload.coordinates")
    variables = _mapping(payload["variables"], field="payload.variables")
    variable_names = frozenset(variables)
    missing = _REQUIRED_VARIABLES - variable_names
    extra = variable_names - _REQUIRED_VARIABLES - _OPTIONAL_VARIABLES
    if missing or extra:
        raise ContractError(
            "payload.variables 字段不匹配"
            f"; missing={sorted(missing)}; extra={sorted(extra)}"
        )
    attributes = _mapping(payload["attributes"], field="payload.attributes")
    latitude = _one_dimensional_numbers(coordinates["latitude"], field="latitude")
    longitude = _one_dimensional_numbers(coordinates["longitude"], field="longitude")
    shape = (latitude.size, longitude.size)
    data_vars: dict[str, tuple[tuple[str, str], np.ndarray]] = {
        "risk_score": (
            ("latitude", "longitude"),
            _two_dimensional_numbers(
                variables["risk_score"],
                field="risk_score",
                shape=shape,
                allow_null=True,
                dtype=np.float64,
            ),
        ),
        "risk_level": (
            ("latitude", "longitude"),
            _two_dimensional_integers(
                variables["risk_level"], field="risk_level", shape=shape
            ),
        ),
        "hard_mask": (
            ("latitude", "longitude"),
            _two_dimensional_booleans(
                variables["hard_mask"], field="hard_mask", shape=shape
            ),
        ),
        "confidence": (
            ("latitude", "longitude"),
            _two_dimensional_numbers(
                variables["confidence"],
                field="confidence",
                shape=shape,
                allow_null=False,
                dtype=np.float64,
            ),
        ),
    }
    if "environment_speed_factor" in variables:
        data_vars["environment_speed_factor"] = (
            ("latitude", "longitude"),
            _two_dimensional_numbers(
                variables["environment_speed_factor"],
                field="environment_speed_factor",
                shape=shape,
                allow_null=False,
                dtype=np.float64,
            ),
        )
    if "hard_reason" in variables:
        data_vars["hard_reason"] = (
            ("latitude", "longitude"),
            _two_dimensional_strings(
                variables["hard_reason"],
                field="hard_reason",
                shape=shape,
                allowed=_HARD_REASONS,
            ),
        )
    sources_raw = _sequence(data["source_summary"], field="source_summary")
    sources = tuple(
        _source_from_document(item, index=index)
        for index, item in enumerate(sources_raw)
    )
    generation_id = data["generation_id"]
    if isinstance(generation_id, bool) or not isinstance(generation_id, int):
        raise ContractError("generation_id 必须是整数，不能是 bool 或浮点数")
    dataset = xr.Dataset(
        data_vars,
        coords={"latitude": latitude, "longitude": longitude},
        attrs=_json_value(dict(attributes), field="payload.attributes"),
    )
    frame = RiskFrame(
        schema_version=_string(data["schema_version"], field="schema_version"),
        risk_id=_string(data["risk_id"], field="risk_id"),
        run_id=_string(data["run_id"], field="run_id"),
        scenario_id=_string(data["scenario_id"], field="scenario_id"),
        corridor_id=_string(data["corridor_id"], field="corridor_id"),
        vessel_profile_id=_string(data["vessel_profile_id"], field="vessel_profile_id"),
        config_digest=_string(data["config_digest"], field="config_digest"),
        model_config_digest=_string(
            data["model_config_digest"], field="model_config_digest"
        ),
        generation_id=generation_id,
        valid_time=_parse_z(data["valid_time"], field="valid_time"),
        as_of_time=_parse_z(data["as_of_time"], field="as_of_time"),
        generated_at=_parse_z(data["generated_at"], field="generated_at"),
        model_version=_string(data["model_version"], field="model_version"),
        payload=dataset,
        source_summary=sources,
        provenance=ProvenanceKind(_string(data["provenance"], field="provenance")),
    )
    validate_canonical_risk_id(frame)
    return frame


def canonical_risk_frame_bytes(
    frame_or_document: RiskFrame | Mapping[str, Any],
    *,
    include_risk_id: bool = True,
) -> bytes:
    """Return stable UTF-8 JSON bytes after full v2 semantic normalization."""

    if isinstance(frame_or_document, RiskFrame):
        frame = _validated_snapshot(frame_or_document)
    else:
        frame = risk_frame_from_document(frame_or_document)
    document = _risk_frame_to_document_unchecked(frame)
    if include_risk_id:
        _validate_document_canonical_risk_id(document)
    if not include_risk_id:
        del document["risk_id"]
    return _canonical_json_bytes(document)


def risk_frame_content_digest(frame_or_document: RiskFrame | Mapping[str, Any]) -> str:
    """Hash every canonical transport field except ``risk_id`` with SHA-256."""

    if isinstance(frame_or_document, RiskFrame):
        frame = _validated_snapshot(frame_or_document)
    else:
        frame = risk_frame_from_document(frame_or_document)
    return _document_content_digest(_risk_frame_to_document_unchecked(frame))


def canonical_risk_id(frame_or_document: RiskFrame | Mapping[str, Any]) -> str:
    """Derive the normative full-SHA-256 RiskFrame content identifier."""

    return f"risk-sha256-{risk_frame_content_digest(frame_or_document)}"


def is_canonical_risk_id(value: str) -> bool:
    """Return whether ``value`` uses the normative full-digest identifier shape."""

    return isinstance(value, str) and _CANONICAL_RISK_ID.fullmatch(value) is not None


def validate_canonical_risk_id(frame: RiskFrame) -> None:
    """Require formal frames to use and match their canonical content ID.

    Synthetic and explicitly legacy frames retain human-readable fixture IDs.
    A formal producer may first build a validated draft with a placeholder ID,
    derive :func:`canonical_risk_id`, and then create the final frozen frame via
    ``dataclasses.replace``.
    """

    if not isinstance(frame, RiskFrame):
        raise ContractError("validate_canonical_risk_id 只接受 RiskFrame")
    document = _risk_frame_to_document_unchecked(_validated_snapshot(frame))
    _validate_document_canonical_risk_id(document)


def _validated_snapshot(frame: RiskFrame) -> RiskFrame:
    """Re-run the Python contract and detach mutable xarray container state."""

    return replace(frame)


def _document_content_digest(document: Mapping[str, Any]) -> str:
    content = dict(document)
    content.pop("risk_id", None)
    return hashlib.sha256(_canonical_json_bytes(content)).hexdigest()


def _validate_document_canonical_risk_id(document: Mapping[str, Any]) -> None:
    """Validate an ID against the exact document snapshot being emitted."""

    if document.get("provenance") != ProvenanceKind.FORMAL.value:
        return
    expected = f"risk-sha256-{_document_content_digest(document)}"
    if document.get("risk_id") != expected:
        raise ContractError(
            "正式 RiskFrame.risk_id 必须等于 risk-sha256-<canonical-content-digest>"
        )


def _source_to_document(source: SourceReference) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "data_id": source.data_id,
        "issue_time": isoformat_z(source.issue_time) if source.issue_time is not None else None,
        "valid_time": isoformat_z(source.valid_time) if source.valid_time is not None else None,
        "version": source.version,
        "quality_flag": source.quality_flag,
        "checksum": source.checksum,
    }


def _source_from_document(value: Any, *, index: int) -> SourceReference:
    data = _mapping(value, field=f"source_summary[{index}]")
    _require_exact_fields(data, _SOURCE_FIELDS, field=f"source_summary[{index}]")
    return SourceReference(
        source_id=_string(data["source_id"], field="source.source_id"),
        data_id=_nullable_string(data["data_id"], field="source.data_id"),
        issue_time=_nullable_z(data["issue_time"], field="source.issue_time"),
        valid_time=_nullable_z(data["valid_time"], field="source.valid_time"),
        version=_string(data["version"], field="source.version"),
        quality_flag=_string(data["quality_flag"], field="source.quality_flag"),
        checksum=_nullable_string(data["checksum"], field="source.checksum"),
    )


def _risk_rows(values: Any) -> list[list[float | None]]:
    array = np.asarray(values)
    if array.ndim != 2:
        raise ContractError("risk_score 必须是二维数组")
    rows: list[list[float | None]] = []
    for row in array:
        output_row: list[float | None] = []
        for raw in row:
            value = float(raw)
            if math.isnan(value):
                output_row.append(None)
            elif math.isfinite(value):
                output_row.append(value)
            else:
                raise ContractError("risk_score 只允许有限值或 NaN")
        rows.append(output_row)
    return rows


def _array_rows(values: Any, *, kind: str) -> list[list[Any]]:
    array = np.asarray(values)
    if array.ndim != 2:
        raise ContractError("payload 变量必须是二维数组")
    rows: list[list[Any]] = []
    for row in array:
        output: list[Any] = []
        for raw in row:
            if kind == "integer":
                output.append(int(raw))
            elif kind == "boolean":
                output.append(bool(raw))
            elif kind == "string":
                if not isinstance(raw, str) or not raw:
                    raise ContractError("payload 字符串变量只能包含非空字符串")
                output.append(str(raw))
            else:
                value = float(raw)
                if not math.isfinite(value):
                    raise ContractError("除 risk_score 外的 payload 数值必须有限")
                output.append(value)
        rows.append(output)
    return rows


def _coordinate_values(values: Any) -> list[float]:
    array = np.asarray(values)
    if array.ndim != 1:
        raise ContractError("坐标必须是一维数组")
    result = [float(value) for value in array]
    if any(not math.isfinite(value) for value in result):
        raise ContractError("坐标必须是有限值")
    return result


def _one_dimensional_numbers(value: Any, *, field: str) -> np.ndarray:
    items = _sequence(value, field=field)
    if len(items) < 2:
        raise ContractError(f"{field} 至少需要两个坐标")
    result = np.asarray([_number(item, field=field) for item in items], dtype=np.float64)
    if result.ndim != 1:
        raise ContractError(f"{field} 必须是一维")
    return result


def _two_dimensional_numbers(
    value: Any,
    *,
    field: str,
    shape: tuple[int, int],
    allow_null: bool,
    dtype: Any,
) -> np.ndarray:
    rows = _sequence(value, field=field)
    parsed: list[list[float]] = []
    for row_index, raw_row in enumerate(rows):
        row = _sequence(raw_row, field=f"{field}[{row_index}]")
        parsed.append(
            [
                math.nan
                if item is None and allow_null
                else _number(item, field=f"{field}[{row_index}]")
                for item in row
            ]
        )
    result = np.asarray(parsed, dtype=dtype)
    if result.shape != shape:
        raise ContractError(f"{field} 形状必须为 {shape}，实际为 {result.shape}")
    return result


def _two_dimensional_integers(
    value: Any, *, field: str, shape: tuple[int, int]
) -> np.ndarray:
    rows = _sequence(value, field=field)
    parsed: list[list[int]] = []
    for row_index, raw_row in enumerate(rows):
        row = _sequence(raw_row, field=f"{field}[{row_index}]")
        parsed_row: list[int] = []
        for item in row:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ContractError(f"{field} 只能包含整数")
            parsed_row.append(item)
        parsed.append(parsed_row)
    result = np.asarray(parsed, dtype=np.int64)
    if result.shape != shape:
        raise ContractError(f"{field} 形状必须为 {shape}，实际为 {result.shape}")
    return result


def _two_dimensional_booleans(
    value: Any, *, field: str, shape: tuple[int, int]
) -> np.ndarray:
    rows = _sequence(value, field=field)
    parsed: list[list[bool]] = []
    for row_index, raw_row in enumerate(rows):
        row = _sequence(raw_row, field=f"{field}[{row_index}]")
        if any(not isinstance(item, bool) for item in row):
            raise ContractError(f"{field} 只能包含 bool")
        parsed.append(list(row))
    result = np.asarray(parsed, dtype=np.bool_)
    if result.shape != shape:
        raise ContractError(f"{field} 形状必须为 {shape}，实际为 {result.shape}")
    return result


def _two_dimensional_strings(
    value: Any,
    *,
    field: str,
    shape: tuple[int, int],
    allowed: frozenset[str],
) -> np.ndarray:
    rows = _sequence(value, field=field)
    parsed: list[list[str]] = []
    for row_index, raw_row in enumerate(rows):
        row = _sequence(raw_row, field=f"{field}[{row_index}]")
        parsed_row: list[str] = []
        for item in row:
            if not isinstance(item, str) or item not in allowed:
                raise ContractError(
                    f"{field} 只能包含 {sorted(allowed)} 中的字符串"
                )
            parsed_row.append(item)
        parsed.append(parsed_row)
    result = np.asarray(parsed, dtype=np.str_)
    if result.shape != shape:
        raise ContractError(f"{field} 形状必须为 {shape}，实际为 {result.shape}")
    return result


def _json_value(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        result = float(value)
        if not math.isfinite(result):
            raise ContractError(f"{field} 不得含 NaN/Infinity")
        return result
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError(f"{field} 的对象键必须是字符串")
        return {key: _json_value(item, field=f"{field}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item, field=field) for item in value]
    raise ContractError(f"{field} 含不可 JSON 序列化的值: {type(value).__name__}")


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ContractError(f"{field} 必须是字符串键对象")
    return value


def _sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{field} 必须是数组")
    return value


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, field: str
) -> None:
    actual = frozenset(value)
    if actual != expected:
        raise ContractError(
            f"{field} 字段不匹配; missing={sorted(expected - actual)}; "
            f"extra={sorted(actual - expected)}"
        )


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} 必须是非空字符串")
    return value


def _nullable_string(value: Any, *, field: str) -> str | None:
    return None if value is None else _string(value, field=field)


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{field} 必须是数值")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{field} 必须是有限数值")
    return result


def _parse_z(value: Any, *, field: str) -> datetime:
    text = _string(value, field=field)
    if not text.endswith("Z"):
        raise ContractError(f"{field} 必须使用 Z 结尾的 UTC 时间")
    return parse_utc(text, field=field)


def _nullable_z(value: Any, *, field: str) -> datetime | None:
    return None if value is None else _parse_z(value, field=field)


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("RiskFrame 含不可规范序列化内容") from exc


__all__ = [
    "canonical_risk_frame_bytes",
    "canonical_risk_id",
    "is_canonical_risk_id",
    "risk_frame_content_digest",
    "risk_frame_from_document",
    "risk_frame_to_document",
    "validate_canonical_risk_id",
]
