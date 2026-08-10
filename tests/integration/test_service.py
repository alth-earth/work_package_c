from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_route_planning.config import configuration_digest, load_configuration
from arctic_route_planning.domain import ObjectiveMode, PlanKind, ReplanReason
from arctic_route_planning.errors import ContextMismatchError
from arctic_route_planning.planners import (
    PlanningRequest,
    PlanningResult,
    RouteStep,
    SearchMetrics,
)
from arctic_route_planning.replanning import (
    ReplanningPolicy,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.service import PlanningService, ServicePlanningRequest

CONFIG_ROOT = Path(__file__).parents[2] / "configs"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


class FakeCandidatePlanner:
    def __init__(self, planner_config) -> None:
        self.planner_config = planner_config
        self.requests: list[PlanningRequest] = []

    def plan_candidates(self, request, objectives):
        self.requests.append(request)
        return {mode: self._result(mode, request) for mode in objectives}

    @staticmethod
    def _result(mode: ObjectiveMode, request: PlanningRequest) -> PlanningResult:
        risk = {
            ObjectiveMode.FASTEST: 0.45,
            ObjectiveMode.LOW_RISK: 0.15,
            ObjectiveMode.RECOMMENDED: 0.25,
        }[mode]
        cost = {
            ObjectiveMode.FASTEST: 8.0,
            ObjectiveMode.LOW_RISK: 11.0,
            ObjectiveMode.RECOMMENDED: 9.0,
        }[mode]
        return PlanningResult(
            objective=mode,
            steps=(
                RouteStep(
                    node=request.start,
                    longitude=18.0,
                    latitude=70.0,
                    eta=request.departure_time,
                    incoming_heading_degrees=None,
                    recommended_speed_knots=None,
                    edge_distance_km=0.0,
                    edge_risk_score=risk,
                    edge_maximum_risk=risk,
                    edge_confidence=0.8,
                    edge_cost=None,
                    source_risk_ids=("risk-1",),
                ),
                RouteStep(
                    node=request.goal,
                    longitude=19.0,
                    latitude=71.0,
                    eta=request.departure_time + timedelta(hours=2),
                    incoming_heading_degrees=10.0,
                    recommended_speed_knots=10.0,
                    edge_distance_km=100.0,
                    edge_risk_score=risk,
                    edge_maximum_risk=risk,
                    edge_confidence=0.8,
                    edge_cost=None,
                    source_risk_ids=("risk-1",),
                ),
            ),
            total_cost_hours=cost,
            distance_km=100.0,
            travel_hours=2.0,
            average_risk=risk,
            maximum_risk=risk,
            minimum_confidence=0.8,
            source_risk_ids=("risk-1",),
            metrics=SearchMetrics(12, 20, 1, 2, 0, 0, 5, 3.0),
        )


def request_for(configuration, *, input_revision: int = 1) -> ServicePlanningRequest:
    return ServicePlanningRequest(
        scenario=configuration.scenario,
        vessel=configuration.vessel,
        config_digest=configuration.config_digest,
        generation_id=2,
        input_revision=input_revision,
        as_of_time=NOW,
        start_time=NOW,
        start=(0, 0),
        goal=(1, 1),
    )


def test_service_executes_three_modes_and_publishes_recommended() -> None:
    configuration = load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")
    planner = FakeCandidatePlanner(configuration.planner)
    ids = iter(("fast-id", "risk-id", "recommended-id"))
    service = PlanningService(
        planner,
        planner_config=configuration.planner,
        clock=lambda: NOW + timedelta(minutes=1),
        plan_id_factory=lambda _mode: next(ids),
    )

    batch = service.execute(request_for(configuration))

    assert batch.published
    assert set(batch.plans) == set(ObjectiveMode)
    assert batch.selected.objective_mode is ObjectiveMode.RECOMMENDED
    assert batch.snapshot.current == batch.selected
    assert {plan.objective_mode for plan in batch.snapshot.candidates} == {
        ObjectiveMode.FASTEST,
        ObjectiveMode.LOW_RISK,
    }
    assert len({plan.planning_request_id for plan in batch.plans.values()}) == 1
    assert batch.selected.metrics.expanded_nodes == 12
    assert batch.selected.waypoints[0].recommended_speed_mps > 0
    assert planner.requests[0].cancel_check is not None


def test_service_event_replan_is_published_and_records_reason() -> None:
    configuration = load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")
    evaluator = ReplanTriggerEvaluator(ReplanningPolicy(min_interval=timedelta(0)))
    evaluator.mark_replanned(
        ReplanObservation(
            observed_at=NOW,
            risk_valid_time=NOW,
            data_revision=1,
            risk_revision="risk-1",
            route_avg_risk=0.25,
            route_max_risk=0.25,
        )
    )
    service = PlanningService(
        FakeCandidatePlanner(configuration.planner),
        planner_config=configuration.planner,
        trigger_evaluator=evaluator,
        switch_gate=RouteSwitchGate(ReplanningPolicy(min_interval=timedelta(0))),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    initial = service.execute(request_for(configuration))

    outcome = service.replan_if_needed(
        request_for(configuration, input_revision=2),
        ReplanObservation(
            observed_at=NOW + timedelta(hours=1),
            risk_valid_time=NOW,
            data_revision=1,
            risk_revision="risk-1",
            route_avg_risk=0.25,
            route_max_risk=0.25,
            event_revision="closure-1",
        ),
    )

    assert outcome.decision.triggered
    assert outcome.batch is not None and outcome.batch.published
    assert outcome.batch.selected.plan_kind is PlanKind.REPLANNED
    assert outcome.batch.selected.replan_reasons == (ReplanReason.EVENT,)
    assert outcome.batch.snapshot.previous == initial.selected


def test_service_rejects_risk_context_mislabelling() -> None:
    configuration = load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")
    planner = FakeCandidatePlanner(configuration.planner)
    planner.risk_identity = SimpleNamespace(
        scenario_id="another-scenario",
        corridor_id=configuration.scenario.corridor_id,
        vessel_profile_id=configuration.vessel.vessel_profile_id,
        config_digest=configuration.config_digest,
        generation_id=2,
    )
    service = PlanningService(planner, planner_config=configuration.planner)

    with pytest.raises(ContextMismatchError, match="scenario_id"):
        service.execute(request_for(configuration))


def test_scenario_default_vessel_can_be_explicitly_overridden() -> None:
    configuration = load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")
    alternate = replace(
        configuration.vessel,
        vessel_profile_id="alternate_demo_bulk_carrier_v1",
        display_name="Alternate demo bulk carrier",
    )
    digest = configuration_digest(
        configuration.scenario,
        alternate,
        configuration.planner,
        configuration.replanning,
    )
    planner = FakeCandidatePlanner(configuration.planner)
    service = PlanningService(planner, planner_config=configuration.planner)

    batch = service.execute(
        replace(request_for(configuration), vessel=alternate, config_digest=digest)
    )

    assert batch.selected.vessel_profile_id == alternate.vessel_profile_id
    assert batch.selected.config_digest == digest


def test_rejected_route_switch_does_not_advance_committed_trigger_baseline() -> None:
    configuration = load_configuration(CONFIG_ROOT, "demo_tromso_to_svalbard_v1")
    evaluator = ReplanTriggerEvaluator(ReplanningPolicy(min_interval=timedelta(0)))
    baseline = ReplanObservation(
        observed_at=NOW,
        risk_valid_time=NOW,
        data_revision=1,
        risk_revision="risk-1",
        route_avg_risk=0.25,
        route_max_risk=0.25,
    )
    evaluator.mark_replanned(baseline)
    service = PlanningService(
        FakeCandidatePlanner(configuration.planner),
        planner_config=configuration.planner,
        trigger_evaluator=evaluator,
        switch_gate=RouteSwitchGate(ReplanningPolicy(min_interval=timedelta(0))),
        clock=lambda: NOW + timedelta(minutes=1),
    )
    service.execute(request_for(configuration))
    changed_data = replace(
        baseline,
        observed_at=NOW + timedelta(hours=1),
        data_revision=2,
        risk_revision="risk-2",
    )

    outcome = service.replan_if_needed(
        request_for(configuration, input_revision=2),
        changed_data,
    )

    assert outcome.batch is not None and not outcome.batch.published
    assert evaluator.evaluate(changed_data).triggered
