"""Explicitly lossy v1 contract readers for development-only migration."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from arctic_route_planning.contracts import ProvenanceKind, RiskFrame, SourceReference
from arctic_route_planning.errors import LegacyDataError


def adapt_risk_frame_v1(
    document: Mapping[str, Any],
    *,
    run_id: str,
    model_config_digest: str,
    payload: Any,
    acknowledge_legacy_unverified: bool,
) -> RiskFrame:
    """Lift v1 into v2 while permanently downgrading provenance."""

    if not acknowledge_legacy_unverified:
        raise LegacyDataError("bc.risk-frame.v1 只能显式标记 legacy_unverified 后迁移")
    if document.get("schema_version") != "bc.risk-frame.v1":
        raise LegacyDataError("legacy adapter 只接受 bc.risk-frame.v1")
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=str(document["risk_id"]),
        run_id=run_id,
        scenario_id=str(document["scenario_id"]),
        corridor_id=str(document["corridor_id"]),
        vessel_profile_id=str(document["vessel_profile_id"]),
        config_digest=str(document["config_digest"]),
        model_config_digest=model_config_digest,
        generation_id=int(document["generation_id"]),
        valid_time=_time(document["valid_time"]),
        as_of_time=_time(document["as_of_time"]),
        generated_at=_time(document["generated_at"]),
        model_version=f"legacy-v1:{document['model_version']}",
        payload=payload,
        source_summary=(
            SourceReference(
                source_id="legacy_bc_v1_adapter",
                data_id=str(document["risk_id"]),
                issue_time=None,
                valid_time=_time(document["valid_time"]),
                version="bc.risk-frame.v1",
                quality_flag="legacy_unverified",
            ),
        ),
        provenance=ProvenanceKind.LEGACY_UNVERIFIED,
    )


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise LegacyDataError("legacy v1 时间字段必须是 ISO-8601 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LegacyDataError("legacy v1 时间字段必须带时区")
    return parsed.astimezone(UTC)


__all__ = ["adapt_risk_frame_v1"]
