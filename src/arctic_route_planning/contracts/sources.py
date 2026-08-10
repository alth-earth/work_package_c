"""RiskSource protocol and an in-memory BC implementation for tests and demos."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from threading import RLock
from typing import Protocol

from arctic_route_planning.contracts.models import RiskFrame
from arctic_route_planning.errors import ContextMismatchError, ContractError
from arctic_route_planning.timeutils import ensure_utc


class RiskSource(Protocol):
    def publish(self, frame: RiskFrame) -> None: ...

    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        as_of: datetime,
    ) -> Sequence[RiskFrame]: ...

    def latest_before(
        self,
        target: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        as_of: datetime,
    ) -> RiskFrame | None: ...


class InMemoryRiskSource:
    """Thread-safe, bounded-by-caller BC store with as-of version selection."""

    def __init__(self) -> None:
        self._frames: dict[str, RiskFrame] = {}
        self._lock = RLock()

    def publish(self, frame: RiskFrame) -> None:
        with self._lock:
            current = self._frames.get(frame.risk_id)
            if current is not None and current is not frame:
                raise ContractError(f"risk_id {frame.risk_id} 已对应不同内容")
            self._frames[frame.risk_id] = frame

    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        as_of: datetime,
    ) -> tuple[RiskFrame, ...]:
        start_utc = ensure_utc(start, field="start")
        end_utc = ensure_utc(end, field="end")
        as_of_utc = ensure_utc(as_of, field="as_of")
        if end_utc < start_utc:
            raise ContractError("风险窗口 end 不能早于 start")
        with self._lock:
            candidates = [
                frame
                for frame in self._frames.values()
                if frame.scenario_id == scenario_id
                and frame.generation_id == generation_id
                and frame.vessel_profile_id == vessel_profile_id
                and frame.config_digest == config_digest
                and frame.as_of_time <= as_of_utc
                and start_utc <= frame.valid_time <= end_utc
            ]
        by_time: dict[datetime, RiskFrame] = {}
        for frame in candidates:
            current = by_time.get(frame.valid_time)
            if current is None or (frame.as_of_time, frame.generated_at, frame.risk_id) > (
                current.as_of_time,
                current.generated_at,
                current.risk_id,
            ):
                by_time[frame.valid_time] = frame
        return tuple(by_time[key] for key in sorted(by_time))

    def latest_before(
        self,
        target: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        as_of: datetime,
    ) -> RiskFrame | None:
        target_utc = ensure_utc(target, field="target")
        frames = self.get_window(
            datetime.min.replace(tzinfo=target_utc.tzinfo),
            target_utc,
            scenario_id=scenario_id,
            generation_id=generation_id,
            vessel_profile_id=vessel_profile_id,
            config_digest=config_digest,
            as_of=as_of,
        )
        return frames[-1] if frames else None

    def reset_to_generation(self, generation_id: int) -> None:
        """Discard every frame outside the newly active simulation generation."""

        if generation_id < 0:
            raise ContractError("generation_id 不能为负")
        with self._lock:
            self._frames = {
                risk_id: frame
                for risk_id, frame in self._frames.items()
                if frame.generation_id == generation_id
            }

    def assert_context_available(
        self,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
    ) -> None:
        with self._lock:
            if not any(
                frame.scenario_id == scenario_id
                and frame.generation_id == generation_id
                and frame.vessel_profile_id == vessel_profile_id
                and frame.config_digest == config_digest
                for frame in self._frames.values()
            ):
                raise ContextMismatchError("BC 中没有匹配场景、代次、船型和配置的风险帧")
