"""Cancellation and publication coordination for concurrent planning requests."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from threading import Event, RLock
from uuid import uuid4

from arctic_route_planning.errors import PlanningCancelled
from arctic_route_planning.publishing import (
    CDLatestStore,
    CDStoreSnapshot,
    PublicationRejected,
    PublicationToken,
    RoutePlan,
)

# ``PlanningCancelled`` is canonically defined in ``arctic_route_planning.errors``
# and re-exported here for the historical ``replanning`` import path.


@dataclass(slots=True)
class PlanningHandle:
    token: PublicationToken
    _cancelled: Event

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise PlanningCancelled("planning request was cancelled or superseded")


class PlanningCoordinator:
    """Creates monotonic request fences and performs final atomic review."""

    def __init__(
        self,
        store: CDLatestStore | None = None,
        *,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store or CDLatestStore()
        self._request_id_factory = request_id_factory or (lambda: uuid4().hex)
        self._lock = RLock()
        self._active: PlanningHandle | None = None

    def begin(
        self,
        *,
        run_id: str,
        scenario_id: str,
        generation_id: int,
        config_digest: str,
        model_config_digest: str,
        planner_config_digest: str,
        input_revision: int,
    ) -> PlanningHandle:
        token = PublicationToken(
            run_id=run_id,
            scenario_id=scenario_id,
            generation_id=generation_id,
            config_digest=config_digest,
            model_config_digest=model_config_digest,
            planner_config_digest=planner_config_digest,
            input_revision=input_revision,
            planning_request_id=self._request_id_factory(),
        )
        with self._lock:
            self.store.activate(token)
            if self._active is not None:
                self._active._cancelled.set()
            handle = PlanningHandle(token=token, _cancelled=Event())
            self._active = handle
            return handle

    def cancel(self, handle: PlanningHandle) -> bool:
        with self._lock:
            handle._cancelled.set()
            return self.store.cancel(handle.token)

    def is_current(self, handle: PlanningHandle) -> bool:
        with self._lock:
            return (
                handle is self._active
                and not handle.cancelled
                and self.store.is_current(handle.token)
            )

    def require_current(self, handle: PlanningHandle) -> None:
        handle.raise_if_cancelled()
        with self._lock:
            if handle is not self._active:
                raise PlanningCancelled("planning request was superseded")
            try:
                self.store.require_current(handle.token)
            except PublicationRejected as exc:
                raise PlanningCancelled(str(exc)) from exc

    def publish(
        self,
        handle: PlanningHandle,
        plan: RoutePlan,
        candidates: Sequence[RoutePlan] = (),
    ) -> CDStoreSnapshot:
        # The coordinator lock serializes this final check/store publication with
        # begin/cancel. CDLatestStore repeats the exact-token check under its own
        # state-swap lock as defence in depth.
        with self._lock:
            if handle is not self._active or handle.cancelled:
                raise PlanningCancelled("planning request was cancelled or superseded")
            try:
                return self.store.publish(plan, candidates, token=handle.token)
            except PublicationRejected as exc:
                raise PlanningCancelled(str(exc)) from exc
