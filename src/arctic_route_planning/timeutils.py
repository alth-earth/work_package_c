"""UTC-only time helpers for reproducible replay and planning."""

from __future__ import annotations

from datetime import UTC, datetime

from arctic_route_planning.errors import ContractError


def ensure_utc(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ContractError(f"{field} 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{field} 必须携带时区")
    return value.astimezone(UTC)


def parse_utc(value: str, *, field: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(f"{field} 不是有效 ISO-8601 时间: {value}") from exc
    return ensure_utc(parsed, field=field)


def isoformat_z(value: datetime) -> str:
    return ensure_utc(value, field="datetime").isoformat().replace("+00:00", "Z")
