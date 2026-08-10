"""Application service joining the planner, replanning controls, and CD publication."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from arctic_route_planning.contracts import RouteMetrics, RoutePlan, Waypoint
from arctic_route_planning.domain import (
    ObjectiveMode,
    PlanKind,
    PlannerConfig,
    ReplanReason,
    ScenarioDefinition,
    VesselProfile,
)
from arctic_route_planning.errors import ContextMismatchError
from arctic_route_planning.grid import Node
from arctic_route_planning.planners import PlanningRequest, PlanningResult
from arctic_route_planning.publishing import CDStoreSnapshot
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanDecision,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
    SwitchDecision,
)

KNOT_TO_METRES_PER_SECOND = 0.5144444444444445
DEFAULT_PLANNER_VERSION = "time-dependent-a-star.v1"


class CandidatePlanner(Protocol):
    def plan_candidates(
        self,
        request: PlanningRequest,
        objectives: tuple[ObjectiveMode, ...],
    ) -> Mapping[ObjectiveMode, PlanningResult]: ...


@dataclass(frozen=True, slots=True)
class ServicePlanningRequest:
    scenario: ScenarioDefinition
    vessel: VesselProfile
    config_digest: str
    generation_id: int
    input_revision: int
    as_of_time: datetime
    start_time: datetime
    start: Node
    goal: Node
    plan_kind: PlanKind = PlanKind.INITIAL
    replan_reasons: tuple[ReplanReason, ...] = ()
    maximum_risk: float | None = None
    maximum_elapsed: timedelta | None = None

    def __post_init__(self) -> None:
        if len(self.config_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.config_digest
        ):
            raise ValueError("config_digest must be a lowercase SHA-256 digest")
        if self.generation_id < 0 or self.input_revision < 0:
            raise ValueError("generation_id and input_revision must be non-negative")
        for name in ("as_of_time", "start_time"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
            object.__setattr__(self, name, value.astimezone(UTC))
        if self.as_of_time > self.start_time:
            raise ValueError("as_of_time cannot be later than start_time")
        if self.start == self.goal:
            raise ValueError("start and goal nodes must differ")


@dataclass(frozen=True, slots=True)
class PlanningBatch:
    plans: Mapping[ObjectiveMode, RoutePlan]
    selected: RoutePlan
    snapshot: CDStoreSnapshot
    published: bool
    switch_decision: SwitchDecision | None = None


@dataclass(frozen=True, slots=True)
class ReplanningOutcome:
    decision: ReplanDecision
    batch: PlanningBatch | None


class PlanningService:
    """Execute all three objective policies and atomically publish recommended output."""

    def __init__(
        self,
        planner: CandidatePlanner,
        *,
        planner_config: PlannerConfig | None = None,
        coordinator: PlanningCoordinator | None = None,
        switch_gate: RouteSwitchGate | None = None,
        trigger_evaluator: ReplanTriggerEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
        plan_id_factory: Callable[[ObjectiveMode], str] | None = None,
        planner_version: str = DEFAULT_PLANNER_VERSION,
    ) -> None:
        self.planner = planner
        self.planner_config = planner_config or getattr(planner, "planner_config", PlannerConfig())
        self.coordinator = coordinator or PlanningCoordinator()
        self.switch_gate = switch_gate or RouteSwitchGate()
        self.trigger_evaluator = trigger_evaluator or ReplanTriggerEvaluator()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._plan_id_factory = plan_id_factory or (
            lambda objective: f"{objective.value}-{uuid4().hex}"
        )
        if not planner_version.strip():
            raise ValueError("planner_version must be non-empty")
        self.planner_version = planner_version

    def execute(
        self,
        request: ServicePlanningRequest,
        *,
        apply_switch_gate: bool = False,
    ) -> PlanningBatch:
        """Run fastest, low-risk, and recommended searches as one fenced request."""

        self._validate_risk_context(request)

        handle = self.coordinator.begin(
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
            config_digest=request.config_digest,
            input_revision=request.input_revision,
        )
        core_request = PlanningRequest(
            start=request.start,
            goal=request.goal,
            departure_time=request.start_time,
            objective=ObjectiveMode.RECOMMENDED,
            time_bucket_size=timedelta(minutes=self.planner_config.time_bucket_minutes),
            edge_sample_count=self.planner_config.edge_sample_count,
            maximum_elapsed=request.maximum_elapsed
            or timedelta(hours=self.planner_config.max_search_hours),
            maximum_risk=request.maximum_risk,
            cancel_check=lambda: handle.cancelled,
        )
        objectives = tuple(ObjectiveMode)
        results = self.planner.plan_candidates(core_request, objectives)
        self.coordinator.require_current(handle)
        missing = set(objectives) - set(results)
        if missing:
            labels = ", ".join(sorted(mode.value for mode in missing))
            raise RuntimeError(f"planner did not return all required objectives: {labels}")
        mismatched = tuple(
            objective for objective in objectives if results[objective].objective is not objective
        )
        if mismatched:
            labels = ", ".join(mode.value for mode in mismatched)
            raise RuntimeError(f"planner returned mismatched objective results: {labels}")

        generated_at = self._clock()
        if generated_at.tzinfo is None or generated_at.utcoffset() != timedelta(0):
            raise ValueError("service clock must return a timezone-aware UTC datetime")
        plans = {
            objective: self._to_route_plan(
                request,
                handle.token.planning_request_id,
                results[objective],
                generated_at.astimezone(UTC),
            )
            for objective in objectives
        }
        selected = plans[ObjectiveMode.RECOMMENDED]
        candidates = tuple(
            plans[objective]
            for objective in objectives
            if objective is not ObjectiveMode.RECOMMENDED
        )

        current = self.coordinator.store.snapshot(
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
        ).current
        switch_decision: SwitchDecision | None = None
        if apply_switch_gate and current is not None:
            switch_decision = self.switch_gate.evaluate(
                current,
                selected,
                reasons=request.replan_reasons,
            )
            if not switch_decision.accepted:
                self.coordinator.require_current(handle)
                return PlanningBatch(
                    plans=MappingProxyType(plans),
                    selected=selected,
                    snapshot=self.coordinator.store.snapshot(
                        scenario_id=request.scenario.scenario_id,
                        generation_id=request.generation_id,
                    ),
                    published=False,
                    switch_decision=switch_decision,
                )

        snapshot = self.coordinator.publish(handle, selected, candidates)
        return PlanningBatch(
            plans=MappingProxyType(plans),
            selected=selected,
            snapshot=snapshot,
            published=True,
            switch_decision=switch_decision,
        )

    def _validate_risk_context(self, request: ServicePlanningRequest) -> None:
        """Prevent a correctly solved route from being mislabeled at publication."""

        identity = getattr(self.planner, "risk_identity", None)
        if identity is None:
            return
        expected = {
            "scenario_id": request.scenario.scenario_id,
            "corridor_id": request.scenario.corridor_id,
            "vessel_profile_id": request.vessel.vessel_profile_id,
            "config_digest": request.config_digest,
            "generation_id": request.generation_id,
        }
        mismatched = [name for name, value in expected.items() if getattr(identity, name) != value]
        if mismatched:
            raise ContextMismatchError(
                "planning request does not match the RiskFrame context: " + ", ".join(mismatched)
            )

    def replan_if_needed(
        self,
        request: ServicePlanningRequest,
        observation: ReplanObservation,
    ) -> ReplanningOutcome:
        decision = self.trigger_evaluator.evaluate(observation)
        if not decision.triggered:
            return ReplanningOutcome(decision=decision, batch=None)
        replanning_request = replace(
            request,
            plan_kind=PlanKind.REPLANNED,
            replan_reasons=decision.reasons,
        )
        batch = self.execute(replanning_request, apply_switch_gate=True)
        if batch.published:
            self.trigger_evaluator.mark_replanned(observation)
        return ReplanningOutcome(decision=decision, batch=batch)

    def _to_route_plan(
        self,
        request: ServicePlanningRequest,
        planning_request_id: str,
        result: PlanningResult,
        generated_at: datetime,
    ) -> RoutePlan:
        speed_knots = next(
            (
                step.recommended_speed_knots
                for step in result.steps
                if step.recommended_speed_knots is not None
            ),
            request.vessel.cruise_speed_knots,
        )
        waypoints = tuple(
            Waypoint(
                longitude=step.longitude,
                latitude=step.latitude,
                eta=step.eta,
                recommended_speed_mps=(step.recommended_speed_knots or speed_knots)
                * KNOT_TO_METRES_PER_SECOND,
            )
            for step in result.steps
        )
        headings = tuple(
            step.incoming_heading_degrees
            for step in result.steps
            if step.incoming_heading_degrees is not None
        )
        turn_count = sum(abs(current - previous) > 1e-9 for previous, current in pairwise(headings))
        metrics = RouteMetrics(
            distance_km=result.distance_km,
            eta_hours=result.travel_hours,
            avg_risk=result.average_risk,
            max_risk=result.maximum_risk,
            integrated_risk_hours=result.average_risk * result.travel_hours,
            minimum_confidence=result.minimum_confidence,
            hard_constraint_violations=0,
            turn_count=turn_count,
            expanded_nodes=result.metrics.expanded_states,
            compute_ms=result.metrics.compute_ms,
            objective_cost=result.total_cost_hours,
        )
        return RoutePlan(
            schema_version="cd.route-plan.v1",
            scenario_id=request.scenario.scenario_id,
            corridor_id=request.scenario.corridor_id,
            vessel_profile_id=request.vessel.vessel_profile_id,
            config_digest=request.config_digest,
            generation_id=request.generation_id,
            plan_id=self._plan_id_factory(result.objective),
            plan_version=f"{self.planner_version}:{request.input_revision}",
            planning_request_id=planning_request_id,
            input_revision=request.input_revision,
            generated_at=generated_at,
            as_of_time=request.as_of_time,
            start_time=request.start_time,
            objective_mode=result.objective,
            plan_kind=request.plan_kind,
            waypoints=waypoints,
            metrics=metrics,
            replan_reasons=request.replan_reasons,
            source_risk_ids=result.source_risk_ids,
            planner_version=self.planner_version,
            destination_reached=True,
        )


__all__ = [
    "CandidatePlanner",
    "PlanningBatch",
    "PlanningService",
    "ReplanningOutcome",
    "ServicePlanningRequest",
]
