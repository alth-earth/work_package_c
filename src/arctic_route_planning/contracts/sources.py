"""RiskSource protocol and an in-memory BC implementation for tests and demos."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from threading import RLock
from typing import Protocol, runtime_checkable

from arctic_route_planning.contracts.codec import canonical_risk_frame_bytes
from arctic_route_planning.contracts.models import RiskFrame
from arctic_route_planning.contracts.windows import CommittedRiskWindow, RiskWindowQuery
from arctic_route_planning.errors import ContextMismatchError, ContractError
from arctic_route_planning.timeutils import ensure_utc


@runtime_checkable
class RiskSource(Protocol):
    def publish(self, frame: RiskFrame) -> None: ...

    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        run_id: str,
        scenario_id: str,
        corridor_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        model_config_digest: str,
        as_of: datetime,
    ) -> Sequence[RiskFrame]: ...

    def latest_before(
        self,
        target: datetime,
        *,
        run_id: str,
        scenario_id: str,
        corridor_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        model_config_digest: str,
        as_of: datetime,
    ) -> RiskFrame | None: ...


@runtime_checkable
class CommittedRiskSource(Protocol):
    """Structural protocol implemented by formal B stores.

    Implementers do not inherit a C class. They return the public, immutable
    :class:`CommittedRiskWindow` for an exact full-key query. The execution
    lease must keep that exact commit and the query's active generation stable
    until context exit; generation activation must use the same fence.
    """

    def get_committed_window(self, query: RiskWindowQuery) -> CommittedRiskWindow: ...

    def lease_committed_window(
        self, query: RiskWindowQuery
    ) -> AbstractContextManager[CommittedRiskWindow]: ...


class InMemoryRiskSource:
    """Thread-safe, bounded-by-caller BC store with as-of version selection."""

    def __init__(self) -> None:
        self._frames: dict[str, RiskFrame] = {}
        self._committed_windows: dict[RiskWindowQuery, CommittedRiskWindow] = {}
        self._lock = RLock()

    def publish(self, frame: RiskFrame) -> None:
        with self._lock:
            current = self._frames.get(frame.risk_id)
            if current is not None and canonical_risk_frame_bytes(
                current
            ) != canonical_risk_frame_bytes(frame):
                raise ContractError(f"risk_id {frame.risk_id} 已对应不同内容")
            self._frames[frame.risk_id] = frame

    def commit_window(self, query: RiskWindowQuery) -> CommittedRiskWindow:
        """Atomically freeze an exact query result for contract tests and demos."""

        frames = self.get_window(
            query.start,
            query.end,
            run_id=query.run_id,
            scenario_id=query.scenario_id,
            corridor_id=query.corridor_id,
            generation_id=query.generation_id,
            vessel_profile_id=query.vessel_profile_id,
            config_digest=query.config_digest,
            model_config_digest=query.model_config_digest,
            as_of=query.as_of,
        )
        committed = CommittedRiskWindow.create(query, tuple(frames))
        with self._lock:
            current = self._committed_windows.get(query)
            if current is not None and current.content_digest != committed.content_digest:
                raise ContractError("同一 RiskWindowQuery 已提交不同内容")
            self._committed_windows[query] = committed
        return committed

    def get_committed_window(self, query: RiskWindowQuery) -> CommittedRiskWindow:
        """Return only an explicitly committed exact snapshot."""

        with self._lock:
            committed = self._committed_windows.get(query)
        if committed is None:
            raise ContextMismatchError("BC 中没有完全匹配该查询的已提交风险窗口")
        committed.assert_matches(query)
        return committed

    @contextmanager
    def lease_committed_window(
        self, query: RiskWindowQuery
    ) -> Iterator[CommittedRiskWindow]:
        """Hold the in-memory generation/commit state stable through execution."""

        with self._lock:
            committed = self._committed_windows.get(query)
            if committed is None:
                raise ContextMismatchError("BC 中没有完全匹配该查询的已提交风险窗口")
            committed.assert_matches(query)
            yield committed

    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        run_id: str,
        scenario_id: str,
        corridor_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        model_config_digest: str,
        as_of: datetime,
    ) -> tuple[RiskFrame, ...]:
        start_utc = ensure_utc(start, field="start")
        end_utc = ensure_utc(end, field="end")
        as_of_utc = ensure_utc(as_of, field="as_of")
        if end_utc < start_utc:
            raise ContractError("风险窗口 end 不能早于 start")
        for name, value in (
            ("run_id", run_id),
            ("scenario_id", scenario_id),
            ("corridor_id", corridor_id),
            ("vessel_profile_id", vessel_profile_id),
        ):
            if not value.strip():
                raise ContractError(f"{name} 不能为空")
        with self._lock:
            if any(
                frame.run_id == run_id
                and frame.scenario_id == scenario_id
                and frame.generation_id == generation_id
                and frame.vessel_profile_id == vessel_profile_id
                and frame.config_digest == config_digest
                and frame.model_config_digest == model_config_digest
                and frame.corridor_id != corridor_id
                for frame in self._frames.values()
            ):
                raise ContextMismatchError("请求 corridor_id 与该运行已发布的 RiskFrame 不一致")
            candidates = [
                frame
                for frame in self._frames.values()
                if frame.run_id == run_id
                and frame.scenario_id == scenario_id
                and frame.corridor_id == corridor_id
                and frame.generation_id == generation_id
                and frame.vessel_profile_id == vessel_profile_id
                and frame.config_digest == config_digest
                and frame.model_config_digest == model_config_digest
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
        run_id: str,
        scenario_id: str,
        corridor_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        model_config_digest: str,
        as_of: datetime,
    ) -> RiskFrame | None:
        target_utc = ensure_utc(target, field="target")
        frames = self.get_window(
            datetime.min.replace(tzinfo=target_utc.tzinfo),
            target_utc,
            run_id=run_id,
            scenario_id=scenario_id,
            corridor_id=corridor_id,
            generation_id=generation_id,
            vessel_profile_id=vessel_profile_id,
            config_digest=config_digest,
            model_config_digest=model_config_digest,
            as_of=as_of,
        )
        return frames[-1] if frames else None

    def reset_to_generation(self, generation_id: int) -> None:
        """Discard every frame outside the newly active simulation generation."""

        if (
            isinstance(generation_id, bool)
            or not isinstance(generation_id, int)
            or generation_id < 0
        ):
            raise ContractError("generation_id 必须是非负整数且不能是 bool")
        with self._lock:
            self._frames = {
                risk_id: frame
                for risk_id, frame in self._frames.items()
                if frame.generation_id == generation_id
            }
            self._committed_windows = {
                query: window
                for query, window in self._committed_windows.items()
                if query.generation_id == generation_id
            }

    def assert_context_available(
        self,
        *,
        run_id: str,
        scenario_id: str,
        corridor_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        model_config_digest: str,
    ) -> None:
        with self._lock:
            if not any(
                frame.run_id == run_id
                and frame.scenario_id == scenario_id
                and frame.corridor_id == corridor_id
                and frame.generation_id == generation_id
                and frame.vessel_profile_id == vessel_profile_id
                and frame.config_digest == config_digest
                and frame.model_config_digest == model_config_digest
                for frame in self._frames.values()
            ):
                raise ContextMismatchError(
                    "BC 中没有匹配运行、场景、航区、代次、船型和模型配置的风险帧"
                )
