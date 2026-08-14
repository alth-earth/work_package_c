"""Atomic latest-value publication for complete CD v3 layer sets."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from arctic_route_planning.contracts.layered import FourLayerRoutePlanSet

from .layered_serialization import (
    four_layer_route_plan_set_semantic_digest,
    route_plan_v3_semantic_digest,
)
from .models import PublicationToken
from .store import PublicationRejected


@dataclass(frozen=True, slots=True)
class LayeredStoreSnapshot:
    token: PublicationToken | None
    current: FourLayerRoutePlanSet | None
    previous: FourLayerRoutePlanSet | None
    cancelled: bool


class LayeredRoutePlanLatestStore:
    """Fence and atomically swap one complete four-layer route set."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._active: PublicationToken | None = None
        self._current: FourLayerRoutePlanSet | None = None
        self._previous: FourLayerRoutePlanSet | None = None
        self._cancelled = False

    def activate(self, token: PublicationToken) -> None:
        with self._lock:
            active = self._active
            if token == active:
                if self._cancelled:
                    raise PublicationRejected("cannot reactivate a cancelled v3 request")
                return
            if active is not None and token.run_id == active.run_id:
                if token.generation_id < active.generation_id:
                    raise PublicationRejected("cannot activate an older v3 generation")
                if (
                    token.generation_id == active.generation_id
                    and token.input_revision < active.input_revision
                ):
                    raise PublicationRejected("cannot activate an older v3 input revision")
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

    def cancel(self, token: PublicationToken) -> bool:
        with self._lock:
            if token != self._active:
                return False
            self._cancelled = True
            return True

    def publish(
        self,
        plan_set: FourLayerRoutePlanSet,
        *,
        token: PublicationToken,
    ) -> LayeredStoreSnapshot:
        with self._lock:
            self._require_current_locked(token)
            for bundle in plan_set.layers:
                for plan in bundle.plans.values():
                    expected_plan_id = (
                        "route-v3-sha256-" f"{route_plan_v3_semantic_digest(plan)}"
                    )
                    if plan.plan_id != expected_plan_id:
                        raise PublicationRejected(
                            "four-layer route contains a non-canonical plan_id"
                        )
            expected_set_id = (
                "layer-set-sha256-"
                f"{four_layer_route_plan_set_semantic_digest(plan_set)}"
            )
            if plan_set.layer_set_id != expected_set_id:
                raise PublicationRejected("four-layer route set has a non-canonical layer_set_id")
            for field in (
                "run_id",
                "scenario_id",
                "generation_id",
                "config_digest",
                "model_config_digest",
                "planner_config_digest",
                "input_revision",
                "planning_request_id",
            ):
                if getattr(plan_set, field) != getattr(token, field):
                    raise PublicationRejected(
                        f"four-layer route set does not match publication token: {field}"
                    )
            if self._current is not None and self._current.layer_set_id != plan_set.layer_set_id:
                self._previous = self._current
            self._current = plan_set
            return self._snapshot_locked()

    def latest(
        self,
        *,
        run_id: str,
        scenario_id: str,
        generation_id: int,
    ) -> FourLayerRoutePlanSet | None:
        with self._lock:
            current = self._current
            if current is None:
                return None
            if (
                current.run_id != run_id
                or current.scenario_id != scenario_id
                or current.generation_id != generation_id
            ):
                return None
            return current

    def snapshot(
        self,
        *,
        run_id: str | None = None,
        scenario_id: str | None = None,
        generation_id: int | None = None,
    ) -> LayeredStoreSnapshot:
        with self._lock:
            active = self._active
            if run_id is not None and (active is None or active.run_id != run_id):
                return LayeredStoreSnapshot(None, None, None, False)
            if scenario_id is not None and (
                active is None or active.scenario_id != scenario_id
            ):
                return LayeredStoreSnapshot(None, None, None, False)
            if generation_id is not None and (
                active is None or active.generation_id != generation_id
            ):
                return LayeredStoreSnapshot(None, None, None, False)
            return self._snapshot_locked()

    def _require_current_locked(self, token: PublicationToken) -> None:
        if self._active is None:
            raise PublicationRejected("no four-layer planning request is active")
        if self._active != token:
            raise PublicationRejected("four-layer planning result is stale")
        if self._cancelled:
            raise PublicationRejected("four-layer planning request was cancelled")

    def _snapshot_locked(self) -> LayeredStoreSnapshot:
        return LayeredStoreSnapshot(
            token=self._active,
            current=self._current,
            previous=self._previous,
            cancelled=self._cancelled,
        )


__all__ = ["LayeredRoutePlanLatestStore", "LayeredStoreSnapshot"]
