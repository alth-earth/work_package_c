"""Immutable committed-window contract at the formal B-to-C boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from arctic_route_planning.contracts.codec import (
    risk_frame_to_document,
    validate_canonical_risk_id,
)
from arctic_route_planning.contracts.models import RiskFrame
from arctic_route_planning.errors import ContextMismatchError, ContractError, RiskCoverageError
from arctic_route_planning.timeutils import isoformat_z

HOURLY_RISK_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class RiskWindowQuery:
    """Full identity and as-of selection key for one closed BC window."""

    start: datetime
    end: datetime
    interval: timedelta
    run_id: str
    scenario_id: str
    corridor_id: str
    generation_id: int
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    as_of: datetime

    def __post_init__(self) -> None:
        _require_utc(self.start, field="window.start")
        _require_utc(self.end, field="window.end")
        _require_utc(self.as_of, field="window.as_of")
        if self.end < self.start:
            raise ContractError("风险窗口 end 不能早于 start")
        if self.interval <= timedelta(0):
            raise ContractError("风险窗口 interval 必须为正")
        if (self.end - self.start) % self.interval != timedelta(0):
            raise ContractError("风险窗口闭区间必须被 interval 整除")
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or self.generation_id < 0
        ):
            raise ContractError("generation_id 必须是非负整数且不能是 bool")
        for name in (
            "run_id",
            "scenario_id",
            "corridor_id",
            "vessel_profile_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ContractError(f"{name} 不能为空")
        _validate_digest(self.config_digest, field="config_digest")
        _validate_digest(self.model_config_digest, field="model_config_digest")

    @property
    def count(self) -> int:
        """Number of frames required by this inclusive closed interval."""

        return int((self.end - self.start) // self.interval) + 1


@dataclass(frozen=True, slots=True)
class CommittedRiskWindow:
    """One atomically committed, content-addressed RiskFrame sequence.

    The direct ``start/end/interval/count`` fields make the commit independently
    inspectable.  All identity fields and the as-of snapshot are included in
    ``content_digest`` together with each complete canonical frame document.
    """

    schema_version: str
    commit_id: str
    content_digest: str
    start: datetime
    end: datetime
    interval: timedelta
    count: int
    run_id: str
    scenario_id: str
    corridor_id: str
    generation_id: int
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    as_of: datetime
    frames: tuple[RiskFrame, ...]

    @classmethod
    def create(
        cls,
        query: RiskWindowQuery,
        frames: tuple[RiskFrame, ...],
    ) -> CommittedRiskWindow:
        ordered = tuple(sorted(frames, key=lambda frame: frame.valid_time))
        digest = risk_window_content_digest(query, ordered)
        return cls(
            schema_version="bc.risk-window-commit.v1",
            commit_id=f"risk-window-sha256-{digest}",
            content_digest=digest,
            start=query.start,
            end=query.end,
            interval=query.interval,
            count=query.count,
            run_id=query.run_id,
            scenario_id=query.scenario_id,
            corridor_id=query.corridor_id,
            generation_id=query.generation_id,
            vessel_profile_id=query.vessel_profile_id,
            config_digest=query.config_digest,
            model_config_digest=query.model_config_digest,
            as_of=query.as_of,
            frames=ordered,
        )

    def __post_init__(self) -> None:
        if self.schema_version != "bc.risk-window-commit.v1":
            raise ContractError("窗口 schema_version 必须是 bc.risk-window-commit.v1")
        query = self.query
        if self.count != query.count:
            raise RiskCoverageError("窗口 count 与闭区间/interval 不一致")
        if len(self.frames) != self.count:
            raise RiskCoverageError("窗口 frames 数量与 count 不一致")
        expected_times = tuple(self.start + index * self.interval for index in range(self.count))
        actual_times = tuple(frame.valid_time for frame in self.frames)
        if actual_times != expected_times:
            raise RiskCoverageError("窗口必须逐点覆盖严格闭区间，不得缺帧、重复或错位")
        for frame in self.frames:
            validate_canonical_risk_id(frame)
            mismatched = [
                name
                for name in (
                    "run_id",
                    "scenario_id",
                    "corridor_id",
                    "generation_id",
                    "vessel_profile_id",
                    "config_digest",
                    "model_config_digest",
                )
                if getattr(frame, name) != getattr(self, name)
            ]
            if mismatched:
                raise ContextMismatchError(
                    "已提交窗口含不匹配 RiskFrame: " + ", ".join(mismatched)
                )
            if frame.as_of_time > self.as_of:
                raise ContextMismatchError("已提交窗口包含请求 as_of 之后的知识")
        _validate_digest(self.content_digest, field="window.content_digest")
        expected_digest = risk_window_content_digest(query, self.frames)
        if self.content_digest != expected_digest:
            raise ContractError("窗口 content_digest 与规范内容不一致")
        if self.commit_id != f"risk-window-sha256-{expected_digest}":
            raise ContractError("窗口 commit_id 必须绑定完整 SHA-256 content_digest")

    @property
    def query(self) -> RiskWindowQuery:
        return RiskWindowQuery(
            start=self.start,
            end=self.end,
            interval=self.interval,
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            corridor_id=self.corridor_id,
            generation_id=self.generation_id,
            vessel_profile_id=self.vessel_profile_id,
            config_digest=self.config_digest,
            model_config_digest=self.model_config_digest,
            as_of=self.as_of,
        )

    def assert_matches(self, query: RiskWindowQuery) -> None:
        """Require an exact query match; no implicit clipping or as-of fallback."""

        if self.query != query:
            raise ContextMismatchError("已提交窗口与完整查询身份或闭区间不一致")


def risk_window_content_digest(
    query: RiskWindowQuery,
    frames: tuple[RiskFrame, ...],
) -> str:
    """Hash window metadata and complete canonical frame documents."""

    document = {
        "schema_version": "bc.risk-window-commit.v1",
        "start": isoformat_z(query.start),
        "end": isoformat_z(query.end),
        "interval_seconds": int(query.interval.total_seconds()),
        "count": query.count,
        "run_id": query.run_id,
        "scenario_id": query.scenario_id,
        "corridor_id": query.corridor_id,
        "generation_id": query.generation_id,
        "vessel_profile_id": query.vessel_profile_id,
        "config_digest": query.config_digest,
        "model_config_digest": query.model_config_digest,
        "as_of": isoformat_z(query.as_of),
        "frames": [risk_frame_to_document(frame) for frame in frames],
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_utc(value: datetime, *, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{field} 必须携带 UTC 时区")
    if value.utcoffset() != timedelta(0):
        raise ContractError(f"{field} 必须使用 UTC")


def _validate_digest(value: str, *, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractError(f"{field} 必须是小写 SHA-256")


__all__ = [
    "HOURLY_RISK_INTERVAL",
    "CommittedRiskWindow",
    "RiskWindowQuery",
    "risk_window_content_digest",
]
