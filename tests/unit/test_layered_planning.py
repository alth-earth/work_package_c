from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import PlanLayer, ProvenanceKind
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.domain import ObjectiveMode, PlanKind, ReplanReason
from arctic_route_planning.errors import PlanningCancelledError
from arctic_route_planning.layered import (
    FourLayerPlanningService,
    LayerNotMaterializableError,
)
from arctic_route_planning.planners import PlanningResult, RouteStep, SearchMetrics
from arctic_route_planning.publishing import (
    SELECTION_RATIONALE_SCHEMA_VERSION,
    LayeredRoutePlanLatestStore,
    PublicationRejected,
    SelectionRationale,
    four_layer_route_plan_set_from_dict,
    four_layer_route_plan_set_from_geojson,
    four_layer_route_plan_set_to_dict,
    four_layer_route_plan_set_to_geojson,
    route_plan_v3_from_dict,
    route_plan_v3_from_geojson,
    route_plan_v3_to_dict,
    route_plan_v3_to_geojson,
)
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanningPolicy,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.service import ServicePlanningRequest

CONFIG_ROOT = Path(__file__).parents[2] / "configs"
SCHEMA_ROOT = Path(__file__).parents[2] / "schemas"


class LayerFixturePlanner:
    def __init__(self) -> None:
        self.goals: list[tuple[int, int]] = []
        self.maximum_elapsed: list[timedelta] = []

    def plan_candidates(self, request, objectives):
        self.goals.append(request.goal)
        self.maximum_elapsed.append(request.maximum_elapsed)
        goal_column = request.goal[1]
        hours = (
            (0, 3, 12, 30, 60, 90)
            if goal_column == 5
            else (0, 3, 12, 30, 60)[: goal_column + 1]
        )
        return {
            objective: _result(request.departure_time, hours, objective)
            for objective in objectives
        }


class NoSixHourAnchorPlanner(LayerFixturePlanner):
    def plan_candidates(self, request, objectives):
        if request.goal == (0, 5):
            self.goals.append(request.goal)
            self.maximum_elapsed.append(request.maximum_elapsed)
            return {
                objective: _result(
                    request.departure_time,
                    (0, 12, 30, 60, 80, 90),
                    objective,
                )
                for objective in objectives
            }
        return super().plan_candidates(request, objectives)


class BlockingFirstLayerPlanner(LayerFixturePlanner):
    def __init__(self, entered: Event, release: Event) -> None:
        super().__init__()
        self._entered = entered
        self._release = release
        self._first_call = True

    def plan_candidates(self, request, objectives):
        if self._first_call:
            self._first_call = False
            self._entered.set()
            assert self._release.wait(timeout=5)
        return super().plan_candidates(request, objectives)


class ShortVoyagePlanner(LayerFixturePlanner):
    """Recommended full-voyage ETA (60h) below the 72h main-corridor ceiling."""

    def plan_candidates(self, request, objectives):
        self.goals.append(request.goal)
        self.maximum_elapsed.append(request.maximum_elapsed)
        goal_column = request.goal[1]
        hours = (
            (0, 3, 12, 30, 50, 60)
            if goal_column == 5
            else (0, 3, 12, 30, 50)[: goal_column + 1]
        )
        return {
            objective: _result(request.departure_time, hours, objective)
            for objective in objectives
        }


def _case(*, planner=None, request_id: str = "request-v3"):
    configuration = load_configuration(
        CONFIG_ROOT,
        "tromso_isfjorden_july_2026_retrospective_v1",
    )
    run_context = create_development_run_context(configuration, source_kind="synthetic")
    active_planner = planner or LayerFixturePlanner()
    policy = ReplanningPolicy.from_config(configuration.replanning)
    store = LayeredRoutePlanLatestStore()
    service = FourLayerPlanningService(
        active_planner,
        planner_config=configuration.planner,
        coordinator=PlanningCoordinator(request_id_factory=lambda: request_id),
        store=store,
        switch_gate=RouteSwitchGate(policy),
        trigger_evaluator=ReplanTriggerEvaluator(policy),
        clock=lambda: configuration.scenario.simulation_start,
    )
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest="1" * 64,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.SYNTHETIC,
        generation_id=1,
        input_revision=0,
        as_of_time=configuration.scenario.simulation_start,
        start_time=configuration.scenario.simulation_start,
        start=(0, 0),
        goal=(0, 5),
        maximum_elapsed=timedelta(hours=96),
    )
    return configuration, active_planner, store, service, request


def test_four_layer_service_builds_twelve_routes_and_round_trips() -> None:
    configuration, planner, store, service, request = _case()

    outcome = service.execute(request)

    assert outcome.published
    assert planner.goals == [(0, 5), (0, 4), (0, 2), (0, 1)]
    assert planner.maximum_elapsed == [
        timedelta(hours=96),
        timedelta(hours=72),
        timedelta(hours=24),
        timedelta(hours=6),
    ]
    assert len(outcome.plan_set.layers) == 4
    assert sum(len(bundle.plans) for bundle in outcome.plan_set.layers) == 12
    assert tuple(bundle.planning_layer for bundle in outcome.plan_set.layers) == tuple(
        PlanLayer
    )
    assert store.latest(
        run_id=request.run_context.run_id,
        scenario_id=configuration.scenario.scenario_id,
        generation_id=1,
    ) == outcome.plan_set
    assert all(
        plan.destination_reached
        for plan in outcome.plan_set.layers[0].plans.values()
    )
    assert all(
        not plan.destination_reached
        for bundle in outcome.plan_set.layers[1:]
        for plan in bundle.plans.values()
    )

    plan = outcome.plan_set.recommended
    assert route_plan_v3_from_dict(route_plan_v3_to_dict(plan)) == plan
    assert route_plan_v3_from_geojson(route_plan_v3_to_geojson(plan)) == plan
    assert (
        four_layer_route_plan_set_from_dict(
            four_layer_route_plan_set_to_dict(outcome.plan_set)
        )
        == outcome.plan_set
    )
    assert (
        four_layer_route_plan_set_from_geojson(
            four_layer_route_plan_set_to_geojson(outcome.plan_set)
        )
        == outcome.plan_set
    )
    _validate_schema("route-plan-v3.schema.json", route_plan_v3_to_dict(plan))
    _validate_schema("route-plan-v3.geojson.schema.json", route_plan_v3_to_geojson(plan))
    _validate_schema(
        "four-layer-route-plan-set-v3.schema.json",
        four_layer_route_plan_set_to_dict(outcome.plan_set),
    )
    _validate_schema(
        "four-layer-route-plan-set-v3.geojson.schema.json",
        four_layer_route_plan_set_to_geojson(outcome.plan_set),
    )


def test_four_layer_service_attaches_selection_rationale_using_full_voyage_layer() -> None:
    _configuration, _planner, _store, service, request = _case()

    outcome = service.execute(request)

    assert isinstance(outcome.selection_rationale, SelectionRationale)
    rationale = outcome.selection_rationale
    assert rationale.schema_version == SELECTION_RATIONALE_SCHEMA_VERSION
    full_bundle = outcome.plan_set.bundle_for(PlanLayer.FULL_VOYAGE)
    assert (
        rationale.selected_plan_id
        == full_bundle.recommended.plan_id
    )
    assert (
        rationale.baseline_plan_id
        == full_bundle.plans[ObjectiveMode.FASTEST].plan_id
    )
    assert rationale.selected_objective is ObjectiveMode.RECOMMENDED
    assert rationale.baseline_objective is ObjectiveMode.FASTEST
    full_recommended_metrics = full_bundle.recommended.metrics
    full_fastest_metrics = full_bundle.plans[ObjectiveMode.FASTEST].metrics
    assert rationale.tradeoffs.delta_eta_hours == pytest.approx(
        full_recommended_metrics.eta_hours - full_fastest_metrics.eta_hours
    )
    assert rationale.tradeoffs.delta_avg_risk == pytest.approx(
        full_recommended_metrics.avg_risk - full_fastest_metrics.avg_risk
    )
    assert rationale.summary_text


def test_destination_anchor_layer_allows_objectives_beyond_recommended_eta() -> None:
    """When the main-corridor anchor is the destination and the recommended
    ETA is below 72h, the layer ceiling must be the configured 72h cap, not
    the recommended plan's ETA."""

    planner = ShortVoyagePlanner()
    _, _, _store, service, request = _case(planner=planner)

    outcome = service.execute(request)

    assert outcome.published
    assert planner.maximum_elapsed == [
        timedelta(hours=96),
        timedelta(hours=72),
        timedelta(hours=24),
        timedelta(hours=6),
    ]
    main_corridor = outcome.plan_set.layers[1]
    assert all(
        plan.destination_reached and plan.layer_goal_reached
        for plan in main_corridor.plans.values()
    )
    assert all(
        not plan.destination_reached
        for bundle in outcome.plan_set.layers[2:]
        for plan in bundle.plans.values()
    )


def test_layer_failure_leaves_no_partial_set_and_preserves_previous_set() -> None:
    _, _, store, service, request = _case()
    initial = service.execute(request)
    service.planner = NoSixHourAnchorPlanner()

    with pytest.raises(LayerNotMaterializableError, match="layer_not_materializable"):
        service.execute(replace(request, input_revision=1))

    assert store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) == initial.plan_set


def test_layered_store_rejects_late_and_noncanonical_publications() -> None:
    _, _, store, service, request = _case()
    outcome = service.execute(request)
    old_token = outcome.snapshot.token
    assert old_token is not None
    lower = outcome.plan_set.layers[-1]
    fastest = lower.plans[ObjectiveMode.FASTEST]
    tampered = replace(fastest, plan_id=f"route-v3-sha256-{'f' * 64}")
    bad_lower = replace(
        lower,
        plans={**lower.plans, ObjectiveMode.FASTEST: tampered},
    )
    bad_set = replace(
        outcome.plan_set,
        layers=(*outcome.plan_set.layers[:-1], bad_lower),
    )
    with pytest.raises(PublicationRejected, match="non-canonical plan_id"):
        store.publish(bad_set, token=old_token)

    newer = replace(
        old_token,
        planning_request_id="request-v3-newer",
        input_revision=1,
    )
    store.activate(newer)
    with pytest.raises(PublicationRejected, match="stale"):
        store.publish(outcome.plan_set, token=old_token)


def test_newer_revision_cancels_inflight_four_layer_set_before_publication() -> None:
    entered = Event()
    release = Event()
    planner = BlockingFirstLayerPlanner(entered, release)
    _, _, store, service, request = _case(planner=planner)
    older_errors: list[BaseException] = []

    def execute_older() -> None:
        try:
            service.execute(request)
        except BaseException as exc:  # test thread must surface its failure
            older_errors.append(exc)

    older = Thread(target=execute_older)
    older.start()
    assert entered.wait(timeout=5)

    newer = service.execute(replace(request, input_revision=1))
    release.set()
    older.join(timeout=5)

    assert newer.published
    assert len(older_errors) == 1
    assert isinstance(older_errors[0], PlanningCancelledError)
    assert store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) == newer.plan_set


def test_four_layer_time_replan_atomically_replaces_the_set() -> None:
    _, _, _, service, request = _case()
    initial = service.execute(request)
    start = request.start_time
    service.trigger_evaluator.mark_replanned(
        ReplanObservation(
            observed_at=start,
            risk_valid_time=start,
            data_revision=0,
            risk_revision="risk-window-initial",
            route_avg_risk=0.2,
            route_max_risk=0.3,
        )
    )
    replan_time = start + timedelta(hours=6)
    replan_request = replace(
        request,
        input_revision=1,
        start_time=replan_time,
        maximum_elapsed=timedelta(hours=90),
    )
    observation = ReplanObservation(
        observed_at=replan_time,
        risk_valid_time=replan_time,
        data_revision=1,
        risk_revision="risk-window-suffix",
        route_avg_risk=0.2,
        route_max_risk=0.3,
        manual_requested=True,
    )

    result = service.replan_if_needed(replan_request, observation)

    assert result.decision.triggered
    assert set(result.decision.reasons) == {
        ReplanReason.TIME,
        ReplanReason.DATA,
        ReplanReason.MANUAL,
    }
    assert result.outcome is not None and result.outcome.published
    assert result.outcome.plan_set.input_revision == 1
    assert result.outcome.plan_set.plan_kind is PlanKind.REPLANNED
    assert result.outcome.snapshot.previous == initial.plan_set


def test_v3_codec_rejects_unknown_fields_and_bool_revision() -> None:
    _, _, _, service, request = _case()
    plan_set = service.execute(request).plan_set
    document = four_layer_route_plan_set_to_dict(plan_set)
    document["unexpected"] = True
    with pytest.raises(ValueError, match="fields mismatch"):
        four_layer_route_plan_set_from_dict(document)

    plan_document = route_plan_v3_to_dict(plan_set.recommended)
    plan_document["generation_id"] = True
    with pytest.raises(ValueError, match="integer and not bool"):
        route_plan_v3_from_dict(plan_document)


def _result(start_time, hours, objective: ObjectiveMode) -> PlanningResult:
    steps = tuple(
        RouteStep(
            node=(0, index),
            longitude=float(index),
            latitude=70.0,
            eta=start_time + timedelta(hours=hour),
            incoming_heading_degrees=None if index == 0 else 90.0,
            recommended_speed_knots=None if index == 0 else 10.0,
            edge_distance_km=0.0 if index == 0 else 10.0,
            edge_risk_score=0.0 if index == 0 else 0.2,
            edge_maximum_risk=0.0 if index == 0 else 0.3,
            edge_confidence=1.0,
            edge_cost=None,
            source_risk_ids=(f"risk-{index}",),
        )
        for index, hour in enumerate(hours)
    )
    return PlanningResult(
        objective=objective,
        steps=steps,
        total_cost_hours=float(hours[-1]) + list(ObjectiveMode).index(objective),
        distance_km=10.0 * (len(steps) - 1),
        travel_hours=float(hours[-1]),
        average_risk=0.2,
        maximum_risk=0.3,
        minimum_confidence=1.0,
        source_risk_ids=tuple(f"risk-{index}" for index in range(len(steps))),
        metrics=SearchMetrics(
            expanded_states=10,
            generated_states=12,
            rejected_hard_edges=0,
            rejected_risk_edges=0,
            rejected_speed_edges=0,
            rejected_coverage_edges=0,
            queue_peak=4,
            compute_ms=1.0,
        ),
    )


def _validate_schema(name: str, document: dict[str, object]) -> None:
    names = (
        "route-plan-v3.schema.json",
        "route-plan-v3.geojson.schema.json",
        "four-layer-route-plan-set-v3.schema.json",
        "four-layer-route-plan-set-v3.geojson.schema.json",
    )
    resources = []
    schemas = {}
    for filename in names:
        schema = json.loads((SCHEMA_ROOT / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schemas[filename] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    validator = Draft202012Validator(
        schemas[name],
        registry=Registry().with_resources(resources),
        format_checker=FormatChecker(),
    )
    validator.validate(document)
