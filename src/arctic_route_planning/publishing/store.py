"""Thread-safe latest-value C -> D route store."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from threading import RLock

from arctic_route_planning.errors import StalePlanningResultError

from .models import PublicationToken, RoutePlan, token_for_plan


class PublicationRejected(StalePlanningResultError):
    """Raised when stale or incompatible planning output attempts publication."""


@dataclass(frozen=True, slots=True)
class CDStoreSnapshot:
    token: PublicationToken | None
    current: RoutePlan | None
    previous: RoutePlan | None
    candidates: tuple[RoutePlan, ...]
    cancelled: bool


class CDLatestStore:
    """In-memory latest-value cache with atomic publication fencing.

    Planning code must call :meth:`activate` before doing work. Publication succeeds
    only while the exact token remains active. This closes both seek-generation races
    and the subtler same-generation race where an earlier request finishes last.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._active: PublicationToken | None = None
        self._cancelled = False
        self._current: RoutePlan | None = None
        self._previous: RoutePlan | None = None
        self._candidates: tuple[RoutePlan, ...] = ()

    def activate(self, token: PublicationToken) -> None:
        with self._lock:
            active = self._active
            if token == active:
                if self._cancelled:
                    raise PublicationRejected("cannot reactivate a cancelled request token")
                return
            if active is not None and token.run_id == active.run_id:
                if token.generation_id < active.generation_id:
                    raise PublicationRejected("cannot activate an older generation")
                if (
                    token.generation_id == active.generation_id
                    and token.input_revision < active.input_revision
                ):
                    raise PublicationRejected("cannot activate an older input revision")
            context_changed = active is not None and (
                token.run_id != active.run_id
                or token.scenario_id != active.scenario_id
                or token.generation_id != active.generation_id
                or token.config_digest != active.config_digest
                or token.model_config_digest != active.model_config_digest
                or token.planner_config_digest != active.planner_config_digest
            )
            self._active = token
            self._cancelled = False
            if context_changed:
                self._current = None
                self._previous = None
                self._candidates = ()

    def cancel(self, token: PublicationToken) -> bool:
        with self._lock:
            if token != self._active:
                return False
            self._cancelled = True
            return True

    def is_current(self, token: PublicationToken) -> bool:
        with self._lock:
            return token == self._active and not self._cancelled

    def require_current(self, token: PublicationToken) -> None:
        with self._lock:
            self._require_current_locked(token)

    def _require_current_locked(self, token: PublicationToken) -> None:
        if self._active is None:
            raise PublicationRejected("no planning request is active")
        if token != self._active:
            raise PublicationRejected("planning result is stale or belongs to another context")
        if self._cancelled:
            raise PublicationRejected("planning request was cancelled")

    @staticmethod
    def _validate_plan(plan: RoutePlan, token: PublicationToken, *, role: str) -> None:
        if token_for_plan(plan) != token:
            raise PublicationRejected(f"{role} does not match the active publication token")

    def publish(
        self,
        plan: RoutePlan,
        candidates: Sequence[RoutePlan] = (),
        *,
        token: PublicationToken | None = None,
    ) -> CDStoreSnapshot:
        publication_token = token or token_for_plan(plan)
        with self._lock:
            # This check and the state swap share one lock: a newer activate/cancel
            # cannot slip between final review and publication.
            self._require_current_locked(publication_token)
            self._validate_plan(plan, publication_token, role="selected plan")
            immutable_candidates = tuple(candidates)
            for candidate in immutable_candidates:
                self._validate_plan(candidate, publication_token, role="candidate")
                if candidate.corridor_id != plan.corridor_id:
                    raise PublicationRejected("candidate corridor does not match selected plan")
                if candidate.vessel_profile_id != plan.vessel_profile_id:
                    raise PublicationRejected("candidate vessel does not match selected plan")
                if candidate.provenance is not plan.provenance:
                    raise PublicationRejected("candidate provenance does not match selected plan")
                if (
                    candidate.as_of_time != plan.as_of_time
                    or candidate.start_time != plan.start_time
                ):
                    raise PublicationRejected(
                        "candidate planning time does not match selected plan"
                    )
            if self._current is not None and self._current.plan_id != plan.plan_id:
                self._previous = self._current
            self._current = plan
            self._candidates = immutable_candidates
            return self._snapshot_locked()

    def latest(self, *, run_id: str, scenario_id: str, generation_id: int) -> RoutePlan | None:
        with self._lock:
            if self._current is None:
                return None
            if (
                self._current.run_id != run_id
                or self._current.scenario_id != scenario_id
                or self._current.generation_id != generation_id
            ):
                return None
            return self._current

    def snapshot(
        self,
        *,
        run_id: str | None = None,
        scenario_id: str | None = None,
        generation_id: int | None = None,
    ) -> CDStoreSnapshot:
        with self._lock:
            if run_id is not None and (
                self._active is None or self._active.run_id != run_id
            ):
                return CDStoreSnapshot(None, None, None, (), False)
            if scenario_id is not None and (
                self._active is None or self._active.scenario_id != scenario_id
            ):
                return CDStoreSnapshot(None, None, None, (), False)
            if generation_id is not None and (
                self._active is None or self._active.generation_id != generation_id
            ):
                return CDStoreSnapshot(None, None, None, (), False)
            return self._snapshot_locked()

    def _snapshot_locked(self) -> CDStoreSnapshot:
        return CDStoreSnapshot(
            token=self._active,
            current=self._current,
            previous=self._previous,
            candidates=self._candidates,
            cancelled=self._cancelled,
        )
