"""Application-layer orchestration for atomic four-layer route planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType

from arctic_route_planning.contracts import ProvenanceKind, RouteMetrics, Waypoint
from arctic_route_planning.contracts.layered import (
    FourLayerRoutePlanSet,
    LayerRouteBundle,
    PlanLayer,
    RoutePlanV3,
)
from arctic_route_planning.domain import ObjectiveMode, PlanKind
from arctic_route_planning.errors import ContextMismatchError, PlanningCancelledError
from arctic_route_planning.planners import PlanningRequest, PlanningResult
from arctic_route_planning.publishing import (
    PublicationRejected,
    four_layer_route_plan_set_semantic_digest,
    route_plan_v3_semantic_digest,
)
from arctic_route_planning.publishing.layered_store import (
    LayeredRoutePlanLatestStore,
    LayeredStoreSnapshot,
)
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanDecision,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
    SwitchDecision,
)
from arctic_route_planning.service import (
    DEFAULT_PLANNER_VERSION,
    KNOT_TO_METRES_PER_SECOND,
    CandidatePlanner,
    ServicePlanningRequest,
)


class LayerNotMaterializableError(ValueError):
    """Raised when the reference route has no non-start waypoint by a cutoff."""


@dataclass(frozen=True, slots=True)
class FourLayerPlanningOutcome:
    plan_set: FourLayerRoutePlanSet
    snapshot: LayeredStoreSnapshot
    published: bool
    switch_decision: SwitchDecision | None = None


@dataclass(frozen=True, slots=True)
class FourLayerReplanningOutcome:
    decision: ReplanDecision
    outcome: FourLayerPlanningOutcome | None


class FourLayerPlanningService:
    """Run four layers under one request fence and publish only a complete set."""

    def __init__(
        self,
        planner: CandidatePlanner,
        *,
        planner_config: object,
        coordinator: PlanningCoordinator,
        store: LayeredRoutePlanLatestStore,
        switch_gate: RouteSwitchGate,
        trigger_evaluator: ReplanTriggerEvaluator,
        clock: Callable[[], datetime] | None = None,
        planner_version: str = DEFAULT_PLANNER_VERSION,
    ) -> None:
        self.planner = planner
        self.planner_config = planner_config
        self.coordinator = coordinator
        self.store = store
        self.switch_gate = switch_gate
        self.trigger_evaluator = trigger_evaluator
        self._clock = clock or (lambda: datetime.now(UTC))
        self.planner_version = planner_version

    def execute(
        self,
        request: ServicePlanningRequest,
        *,
        apply_switch_gate: bool = False,
    ) -> FourLayerPlanningOutcome:
        self._validate_risk_context(request)
        if request.maximum_elapsed is None:
            raise RuntimeError("maximum_elapsed was not resolved")
        if request.maximum_elapsed > timedelta(hours=self.planner_config.max_search_hours):
            raise ValueError("requested horizon exceeds planner safety ceiling")

        handle = self.coordinator.begin(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
            config_digest=request.run_context.config_digest,
            model_config_digest=request.model_config_digest,
            planner_config_digest=request.planner_config_digest,
            input_revision=request.input_revision,
        )
        self.store.activate(handle.token)
        generated_at = self._validated_clock()

        full_results = self._plan_layer(
            request,
            goal=request.goal,
            handle=handle,
            maximum_elapsed=request.maximum_elapsed,
        )
        full_recommended = full_results[ObjectiveMode.RECOMMENDED]
        full_end = full_recommended.steps[-1].eta
        placeholder_set_id = f"layer-set-sha256-{'0' * 64}"
        full_bundle = self._bundle(
            request,
            results=full_results,
            layer=PlanLayer.FULL_VOYAGE,
            focus_start=request.start_time,
            focus_end=full_end,
            reference_plan_id=None,
            layer_set_id=placeholder_set_id,
            planning_request_id=handle.token.planning_request_id,
            generated_at=generated_at,
        )
        reference_plan_id = full_bundle.recommended.plan_id

        layer_specs = (
            (
                PlanLayer.MAIN_CORRIDOR,
                timedelta(hours=72),
                min(request.start_time + timedelta(hours=24), full_end),
                min(request.start_time + timedelta(hours=72), full_end),
            ),
            (
                PlanLayer.ROLLING,
                timedelta(hours=24),
                request.start_time,
                min(request.start_time + timedelta(hours=24), full_end),
            ),
            (
                PlanLayer.EXECUTABLE,
                timedelta(hours=6),
                request.start_time,
                min(request.start_time + timedelta(hours=6), full_end),
            ),
        )
        bundles = [full_bundle]
        for layer, cutoff, focus_start, focus_end in layer_specs:
            anchor = _anchor_at_or_before(full_recommended, request.start_time + cutoff)
            if anchor == request.goal:
                # The layer goal is the destination.  Each objective must be
                # allowed its own arrival time up to the configured layer
                # ceiling; capping by the recommended plan's ETA would make
                # slower (e.g. low-risk) objectives unplannable even though
                # causal risk coverage is available.
                layer_elapsed = min(request.maximum_elapsed, cutoff)
            else:
                layer_elapsed = min(
                    request.maximum_elapsed,
                    cutoff,
                    full_end - request.start_time,
                )
            results = self._plan_layer(
                request,
                goal=anchor,
                handle=handle,
                maximum_elapsed=layer_elapsed,
            )
            bundles.append(
                self._bundle(
                    request,
                    results=results,
                    layer=layer,
                    focus_start=focus_start,
                    focus_end=focus_end,
                    reference_plan_id=reference_plan_id,
                    layer_set_id=placeholder_set_id,
                    planning_request_id=handle.token.planning_request_id,
                    generated_at=generated_at,
                )
            )

        self.coordinator.require_current(handle)
        provisional = FourLayerRoutePlanSet(
            schema_version="cd.four-layer-route-plan-set.v3",
            layer_set_id=placeholder_set_id,
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            corridor_id=request.corridor.corridor_id,
            vessel_profile_id=request.vessel.vessel_profile_id,
            config_digest=request.run_context.config_digest,
            model_config_digest=request.model_config_digest,
            planner_config_digest=request.planner_config_digest,
            provenance=request.risk_provenance,
            generation_id=request.generation_id,
            planning_request_id=handle.token.planning_request_id,
            input_revision=request.input_revision,
            generated_at=generated_at,
            as_of_time=request.as_of_time,
            start_time=request.start_time,
            plan_kind=request.plan_kind,
            replan_reasons=request.replan_reasons,
            layers=tuple(bundles),
        )
        layer_set_id = (
            "layer-set-sha256-"
            f"{four_layer_route_plan_set_semantic_digest(provisional)}"
        )
        finalized_bundles = tuple(
            LayerRouteBundle(
                bundle.planning_layer,
                {
                    objective: replace(plan, layer_set_id=layer_set_id)
                    for objective, plan in bundle.plans.items()
                },
            )
            for bundle in provisional.layers
        )
        plan_set = replace(
            provisional,
            layer_set_id=layer_set_id,
            layers=finalized_bundles,
        )

        current = self.store.snapshot(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
        ).current
        switch_decision = None
        if apply_switch_gate and current is not None:
            switch_decision = self.switch_gate.evaluate(
                current.recommended,
                plan_set.recommended,
                reasons=request.replan_reasons,
            )
            if not switch_decision.accepted:
                self.coordinator.require_current(handle)
                return FourLayerPlanningOutcome(
                    plan_set=plan_set,
                    snapshot=self.store.snapshot(
                        run_id=request.run_context.run_id,
                        scenario_id=request.scenario.scenario_id,
                        generation_id=request.generation_id,
                    ),
                    published=False,
                    switch_decision=switch_decision,
                )
        self.coordinator.require_current(handle)
        try:
            snapshot = self.store.publish(plan_set, token=handle.token)
        except PublicationRejected as exc:
            raise PlanningCancelledError(str(exc)) from exc
        return FourLayerPlanningOutcome(
            plan_set=plan_set,
            snapshot=snapshot,
            published=True,
            switch_decision=switch_decision,
        )

    def replan_if_needed(
        self,
        request: ServicePlanningRequest,
        observation: ReplanObservation,
    ) -> FourLayerReplanningOutcome:
        decision = self.trigger_evaluator.evaluate(observation)
        if not decision.triggered:
            return FourLayerReplanningOutcome(decision=decision, outcome=None)
        outcome = self.execute(
            replace(
                request,
                plan_kind=PlanKind.REPLANNED,
                replan_reasons=decision.reasons,
            ),
            apply_switch_gate=True,
        )
        if outcome.published:
            self.trigger_evaluator.mark_replanned(observation)
        return FourLayerReplanningOutcome(decision=decision, outcome=outcome)

    def _plan_layer(
        self,
        request: ServicePlanningRequest,
        *,
        goal: tuple[int, int],
        handle: object,
        maximum_elapsed: timedelta,
    ) -> Mapping[ObjectiveMode, PlanningResult]:
        core_request = PlanningRequest(
            start=request.start,
            goal=goal,
            departure_time=request.start_time,
            objective=ObjectiveMode.RECOMMENDED,
            time_bucket_size=timedelta(minutes=self.planner_config.time_bucket_minutes),
            edge_sample_count=self.planner_config.edge_sample_count,
            maximum_elapsed=maximum_elapsed,
            maximum_risk=request.maximum_risk,
            cancel_check=lambda: handle.cancelled,
        )
        results = self.planner.plan_candidates(core_request, tuple(ObjectiveMode))
        self.coordinator.require_current(handle)
        if set(results) != set(ObjectiveMode):
            raise RuntimeError("each v3 layer must return exactly three objectives")
        if any(result.objective is not objective for objective, result in results.items()):
            raise RuntimeError("v3 planner returned a mismatched objective")
        for result in results.values():
            if not result.steps:
                raise RuntimeError("v3 planner returned an empty route")
            if result.steps[0].node != request.start:
                raise RuntimeError("v3 planner route does not start at the requested node")
            if result.steps[-1].node != goal:
                raise RuntimeError("v3 planner route did not reach the layer goal")
            if result.steps[-1].eta > request.start_time + maximum_elapsed:
                raise RuntimeError("v3 planner route exceeded the layer time ceiling")
        return MappingProxyType(dict(results))

    def _bundle(
        self,
        request: ServicePlanningRequest,
        *,
        results: Mapping[ObjectiveMode, PlanningResult],
        layer: PlanLayer,
        focus_start: datetime,
        focus_end: datetime,
        reference_plan_id: str | None,
        layer_set_id: str,
        planning_request_id: str,
        generated_at: datetime,
    ) -> LayerRouteBundle:
        plans = {
            objective: _to_v3_plan(
                request,
                result,
                layer=layer,
                layer_set_id=layer_set_id,
                focus_start=focus_start,
                focus_end=focus_end,
                reference_plan_id=reference_plan_id,
                planning_request_id=planning_request_id,
                generated_at=generated_at,
                planner_version=self.planner_version,
            )
            for objective, result in results.items()
        }
        return LayerRouteBundle(layer, plans)

    def _validate_risk_context(self, request: ServicePlanningRequest) -> None:
        identity = getattr(self.planner, "risk_identity", None)
        if identity is None:
            if request.risk_provenance is ProvenanceKind.FORMAL:
                raise ContextMismatchError(
                    "formal risk provenance requires a planner with risk_identity"
                )
            return
        expected = {
            "run_id": request.run_context.run_id,
            "scenario_id": request.scenario.scenario_id,
            "corridor_id": request.corridor.corridor_id,
            "vessel_profile_id": request.vessel.vessel_profile_id,
            "config_digest": request.run_context.config_digest,
            "model_config_digest": request.model_config_digest,
            "provenance": request.risk_provenance,
            "generation_id": request.generation_id,
        }
        mismatched = [
            field for field, expected_value in expected.items()
            if getattr(identity, field, object()) != expected_value
        ]
        if mismatched:
            raise ContextMismatchError(
                "v3 request does not match RiskFrame context: " + ", ".join(mismatched)
            )

    def _validated_clock(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("service clock must return timezone-aware UTC")
        return value.astimezone(UTC)


def _anchor_at_or_before(result: PlanningResult, cutoff: datetime) -> tuple[int, int]:
    if not result.steps:
        raise LayerNotMaterializableError("layer_not_materializable: empty reference route")
    start_node = result.steps[0].node
    non_start = tuple(step for step in result.steps[1:] if step.node != start_node)
    if not non_start:
        raise LayerNotMaterializableError("layer_not_materializable: no non-start waypoint")
    if result.steps[-1].eta <= cutoff:
        return result.steps[-1].node
    eligible = tuple(step for step in non_start if step.eta <= cutoff)
    if not eligible:
        raise LayerNotMaterializableError(
            "layer_not_materializable: no non-start waypoint at or before cutoff"
        )
    return eligible[-1].node


def _to_v3_plan(
    request: ServicePlanningRequest,
    result: PlanningResult,
    *,
    layer: PlanLayer,
    layer_set_id: str,
    focus_start: datetime,
    focus_end: datetime,
    reference_plan_id: str | None,
    planning_request_id: str,
    generated_at: datetime,
    planner_version: str,
) -> RoutePlanV3:
    speed_knots = next(
        (
            step.recommended_speed_knots
            for step in result.steps
            if step.recommended_speed_knots is not None
        ),
        request.vessel_model.economic_speed_knots,
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
    metrics = RouteMetrics(
        distance_km=result.distance_km,
        eta_hours=result.travel_hours,
        avg_risk=result.average_risk,
        max_risk=result.maximum_risk,
        integrated_risk_hours=result.average_risk * result.travel_hours,
        minimum_confidence=result.minimum_confidence,
        hard_constraint_violations=0,
        turn_count=sum(
            abs(current - previous) > 1e-9
            for previous, current in pairwise(headings)
        ),
        expanded_nodes=result.metrics.expanded_states,
        compute_ms=result.metrics.compute_ms,
        objective_cost=result.total_cost_hours,
    )
    provisional = RoutePlanV3(
        schema_version="cd.route-plan.v3",
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        corridor_id=request.corridor.corridor_id,
        vessel_profile_id=request.vessel.vessel_profile_id,
        config_digest=request.run_context.config_digest,
        model_config_digest=request.model_config_digest,
        planner_config_digest=request.planner_config_digest,
        provenance=request.risk_provenance,
        generation_id=request.generation_id,
        plan_id=f"route-v3-sha256-{'0' * 64}",
        plan_version=f"{planner_version}:v3:{request.input_revision}",
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
        planner_version=planner_version,
        destination_reached=result.steps[-1].node == request.goal,
        planning_layer=layer,
        layer_set_id=layer_set_id,
        focus_start_time=focus_start,
        focus_end_time=focus_end,
        reference_plan_id=reference_plan_id,
        layer_goal_reached=True,
    )
    return replace(
        provisional,
        plan_id=f"route-v3-sha256-{route_plan_v3_semantic_digest(provisional)}",
    )


__all__ = [
    "FourLayerPlanningOutcome",
    "FourLayerPlanningService",
    "FourLayerReplanningOutcome",
    "LayerNotMaterializableError",
]
