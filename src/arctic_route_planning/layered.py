"""Application-layer orchestration for atomic four-layer route planning."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import MappingProxyType

from arctic_route_planning.contracts import ProvenanceKind, RouteMetrics, RoutePlan, Waypoint
from arctic_route_planning.contracts.layered import (
    EXECUTABLE_HOURS,
    FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION,
    MAIN_CORRIDOR_HOURS,
    MAIN_CORRIDOR_START_OFFSET_HOURS,
    ROLLING_HOURS,
    ROUTE_PLAN_V3_SCHEMA_VERSION,
    FourLayerRoutePlanSet,
    LayerRouteBundle,
    PlanLayer,
    RoutePlanV3,
)
from arctic_route_planning.domain import ObjectiveMode, PlanKind, PlannerConfig
from arctic_route_planning.errors import ContextMismatchError, ContractError, PlanningCancelledError
from arctic_route_planning.planners import PlanningRequest, PlanningResult
from arctic_route_planning.publishing import (
    ROUTE_PLAN_SCHEMA_VERSION,
    PublicationRejected,
    SelectionRationale,
    build_selection_rationale,
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
    progress_interval_from_env,
)


class LayerNotMaterializableError(ValueError):
    """Raised when the reference route has no non-start waypoint by a cutoff."""


@dataclass(frozen=True, slots=True)
class FourLayerPlanningOutcome:
    plan_set: FourLayerRoutePlanSet
    snapshot: LayeredStoreSnapshot
    published: bool
    switch_decision: SwitchDecision | None = None
    selection_rationale: SelectionRationale | None = None


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
        planner_config: PlannerConfig,
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
            raise ContractError("maximum_elapsed was not resolved")
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
                timedelta(hours=MAIN_CORRIDOR_HOURS),
                min(
                    request.start_time + timedelta(hours=MAIN_CORRIDOR_START_OFFSET_HOURS),
                    full_end,
                ),
                min(request.start_time + timedelta(hours=MAIN_CORRIDOR_HOURS), full_end),
            ),
            (
                PlanLayer.ROLLING,
                timedelta(hours=ROLLING_HOURS),
                request.start_time,
                min(request.start_time + timedelta(hours=ROLLING_HOURS), full_end),
            ),
            (
                PlanLayer.EXECUTABLE,
                timedelta(hours=EXECUTABLE_HOURS),
                request.start_time,
                min(request.start_time + timedelta(hours=EXECUTABLE_HOURS), full_end),
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
            schema_version=FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION,
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

        selection_rationale = self._build_rationale(plan_set)

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
                    selection_rationale=selection_rationale,
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
            selection_rationale=selection_rationale,
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
            progress_interval_seconds=progress_interval_from_env(),
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

    @staticmethod
    def _build_rationale(
        plan_set: FourLayerRoutePlanSet,
    ) -> SelectionRationale | None:
        """Derive the selection rationale from the full-voyage layer."""

        try:
            bundle = plan_set.bundle_for(PlanLayer.FULL_VOYAGE)
        except StopIteration:
            return None
        selected_v3 = bundle.recommended
        baseline_v3 = bundle.plans.get(ObjectiveMode.FASTEST)
        if baseline_v3 is None or selected_v3.plan_id == baseline_v3.plan_id:
            return None
        selected_v2 = _v3_to_v2_plan(selected_v3)
        baseline_v2 = _v3_to_v2_plan(baseline_v3)
        return build_selection_rationale(selected_v2, baseline_v2)


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
        schema_version=ROUTE_PLAN_V3_SCHEMA_VERSION,
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


_V3_ONLY_FIELDS = frozenset(
    (
        "planning_layer",
        "layer_set_id",
        "focus_start_time",
        "focus_end_time",
        "reference_plan_id",
        "layer_goal_reached",
    )
)


def _v3_to_v2_plan(plan_v3: RoutePlanV3) -> RoutePlan:
    """Project a v3 route plan back to a v2 route plan for rationale derivation.

    Only the shared identity and metrics fields are needed by
    ``build_selection_rationale``; the extra v3 layer fields are dropped.
    """

    return RoutePlan(
        schema_version=ROUTE_PLAN_SCHEMA_VERSION,
        run_id=plan_v3.run_id,
        scenario_id=plan_v3.scenario_id,
        corridor_id=plan_v3.corridor_id,
        vessel_profile_id=plan_v3.vessel_profile_id,
        config_digest=plan_v3.config_digest,
        model_config_digest=plan_v3.model_config_digest,
        planner_config_digest=plan_v3.planner_config_digest,
        provenance=plan_v3.provenance,
        generation_id=plan_v3.generation_id,
        plan_id=plan_v3.plan_id,
        plan_version=plan_v3.plan_version,
        planning_request_id=plan_v3.planning_request_id,
        input_revision=plan_v3.input_revision,
        generated_at=plan_v3.generated_at,
        as_of_time=plan_v3.as_of_time,
        start_time=plan_v3.start_time,
        objective_mode=plan_v3.objective_mode,
        plan_kind=plan_v3.plan_kind,
        waypoints=plan_v3.waypoints,
        metrics=plan_v3.metrics,
        replan_reasons=plan_v3.replan_reasons,
        source_risk_ids=plan_v3.source_risk_ids,
        planner_version=plan_v3.planner_version,
        destination_reached=plan_v3.destination_reached,
    )


__all__ = [
    "FourLayerPlanningOutcome",
    "FourLayerPlanningService",
    "FourLayerReplanningOutcome",
    "LayerNotMaterializableError",
]
