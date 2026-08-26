from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from arctic_route_planning.config import configuration_digest, load_configuration
from arctic_route_planning.contracts import ProvenanceKind
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.domain import ObjectiveMode, PlanKind, ReplanReason
from arctic_route_planning.errors import ContextMismatchError
from arctic_route_planning.planners import (
    PlanningRequest,
    PlanningResult,
    RouteStep,
    SearchMetrics,
)
from arctic_route_planning.publishing import (
    SELECTION_RATIONALE_SCHEMA_VERSION,
    SelectionRationale,
)
from arctic_route_planning.replanning import (
    ReplanningPolicy,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.service import PlanningService, ServicePlanningRequest

CONFIG_ROOT = Path(__file__).parents[2] / "configs"
NOW = datetime(2026, 7, 15, tzinfo=UTC)
B_MODEL_DIGEST = "b" * 64


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
        run_context=create_development_run_context(configuration, source_kind="synthetic"),
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=B_MODEL_DIGEST,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.SYNTHETIC,
        generation_id=2,
        input_revision=input_revision,
        as_of_time=NOW,
        start_time=NOW,
        start=(0, 0),
        goal=(1, 1),
    )


def test_service_executes_three_modes_and_publishes_recommended() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
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
    assert batch.selected.provenance is ProvenanceKind.SYNTHETIC
    assert batch.selected.waypoints[0].recommended_speed_mps > 0
    assert planner.requests[0].cancel_check is not None


def test_service_attaches_selection_rationale_with_quantified_tradeoffs() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    planner = FakeCandidatePlanner(configuration.planner)
    ids = iter(("plan-fast", "plan-risk", "plan-rec"))
    service = PlanningService(
        planner,
        planner_config=configuration.planner,
        clock=lambda: NOW + timedelta(minutes=1),
        plan_id_factory=lambda _mode: next(ids),
    )

    batch = service.execute(request_for(configuration))

    assert isinstance(batch.selection_rationale, SelectionRationale)
    rationale = batch.selection_rationale
    assert rationale.schema_version == SELECTION_RATIONALE_SCHEMA_VERSION
    assert rationale.selected_plan_id == batch.plans[ObjectiveMode.RECOMMENDED].plan_id
    assert rationale.baseline_plan_id == batch.plans[ObjectiveMode.FASTEST].plan_id
    assert rationale.selected_objective is ObjectiveMode.RECOMMENDED
    assert rationale.baseline_objective is ObjectiveMode.FASTEST
    assert rationale.summary_text
    sm, bm = (
        batch.plans[ObjectiveMode.RECOMMENDED].metrics,
        batch.plans[ObjectiveMode.FASTEST].metrics,
    )
    assert rationale.tradeoffs.delta_eta_hours == pytest.approx(sm.eta_hours - bm.eta_hours)
    assert rationale.tradeoffs.delta_avg_risk == pytest.approx(sm.avg_risk - bm.avg_risk)


def test_service_skips_rationale_when_recommended_is_identical_to_fastest() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    # Force recommended and fastest to share the same plan_id, simulating the
    # case where the recommended route is identical to the fastest route.
    shared_ids = {
        "fastest": "shared-plan-id",
        "low_risk": "low-plan-id",
        "recommended": "shared-plan-id",
    }
    service = PlanningService(
        FakeCandidatePlanner(configuration.planner),
        planner_config=configuration.planner,
        clock=lambda: NOW + timedelta(minutes=1),
        plan_id_factory=lambda mode: shared_ids[mode.value],
    )

    batch = service.execute(request_for(configuration))

    recommended = batch.plans[ObjectiveMode.RECOMMENDED]
    fastest = batch.plans[ObjectiveMode.FASTEST]
    assert recommended.plan_id == fastest.plan_id
    assert batch.selection_rationale is None


def test_service_event_replan_is_published_and_records_reason() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
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
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    planner = FakeCandidatePlanner(configuration.planner)
    planner.risk_identity = SimpleNamespace(
        run_id=request_for(configuration).run_context.run_id,
        scenario_id="another-scenario",
        corridor_id=configuration.scenario.corridor_id,
        vessel_profile_id=configuration.vessel.vessel_profile_id,
        config_digest=request_for(configuration).run_context.config_digest,
        model_config_digest=B_MODEL_DIGEST,
        provenance=ProvenanceKind.SYNTHETIC,
        generation_id=2,
    )
    service = PlanningService(planner, planner_config=configuration.planner)

    with pytest.raises(ContextMismatchError, match="scenario_id"):
        service.execute(request_for(configuration))


def test_planner_digest_is_independent_from_public_run_digest() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    changed = replace(configuration.planner, minimum_confidence=0.2)
    changed_digest = configuration_digest(changed, configuration.replanning)
    request = request_for(configuration)

    assert changed_digest != configuration.planner_config_digest
    assert request.run_context.config_digest != changed_digest


def test_route_plan_propagates_b_model_digest_and_c_owns_planner_digest() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    service = PlanningService(
        FakeCandidatePlanner(configuration.planner),
        planner_config=configuration.planner,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    selected = service.execute(request_for(configuration)).selected

    assert selected.model_config_digest == B_MODEL_DIGEST
    assert selected.planner_config_digest == configuration.planner_config_digest
    assert selected.model_config_digest != selected.planner_config_digest
    assert selected.provenance is ProvenanceKind.SYNTHETIC


def test_service_rejects_formal_provenance_without_verifiable_risk_identity() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = replace(
        request_for(configuration),
        risk_provenance=ProvenanceKind.FORMAL,
    )

    with pytest.raises(ContextMismatchError, match="formal risk provenance"):
        PlanningService(
            FakeCandidatePlanner(configuration.planner),
            planner_config=configuration.planner,
        ).execute(request)


def test_request_horizon_comes_from_scenario_and_refuses_over_216_hours() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = request_for(configuration)

    assert request.maximum_elapsed == timedelta(hours=96)
    with pytest.raises(ValueError, match="216-hour"):
        replace(request, maximum_elapsed=timedelta(hours=217))


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    (
        ("scenario_digest", "1" * 64, "scenario_digest"),
        ("corridor_digest", "2" * 64, "corridor_digest"),
        ("vessel_profile_digest", "3" * 64, "vessel_profile_digest"),
        ("config_digest", "4" * 64, "config_digest"),
    ),
)
def test_request_rejects_tampered_run_context_digests(
    field: str,
    replacement: str,
    message: str,
) -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = request_for(configuration)

    with pytest.raises(ValueError, match=message):
        replace(request, run_context=replace(request.run_context, **{field: replacement}))


def test_request_rejects_same_id_but_changed_shared_content() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = request_for(configuration)

    with pytest.raises(ValueError, match="scenario_digest"):
        replace(
            request,
            scenario=replace(configuration.scenario, display_name="changed without a version bump"),
        )
    with pytest.raises(ValueError, match="corridor_digest"):
        replace(
            request,
            corridor=replace(configuration.corridor, display_name="changed without a version bump"),
        )
    with pytest.raises(ValueError, match="vessel_profile_digest"):
        replace(
            request,
            vessel=replace(configuration.vessel, display_name="changed without a version bump"),
        )


def test_request_requires_exact_context_times_and_bounded_planning_window() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = request_for(configuration)

    with pytest.raises(ValueError, match="simulation_end"):
        replace(
            request,
            run_context=replace(
                request.run_context,
                simulation_end=request.run_context.simulation_end - timedelta(hours=1),
            ),
        )
    with pytest.raises(ValueError, match="start_time"):
        replace(request, start_time=request.run_context.simulation_end)
    with pytest.raises(ValueError, match="beyond RunContext"):
        replace(
            request,
            start_time=request.start_time + timedelta(hours=1),
            maximum_elapsed=timedelta(hours=96),
        )


def test_retrospective_allows_late_knowledge_but_frozen_forecast_does_not() -> None:
    retrospective = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    retrospective_request = replace(
        request_for(retrospective),
        as_of_time=datetime(2026, 8, 12, tzinfo=UTC),
    )
    assert retrospective_request.as_of_time > retrospective_request.start_time

    frozen = load_configuration(
        CONFIG_ROOT,
        "tromso_isfjorden_frozen_forecast_template_v1",
        simulation_start=NOW,
    )
    with pytest.raises(ValueError, match="frozen_forecast"):
        replace(request_for(frozen), as_of_time=NOW + timedelta(minutes=1))


def test_service_rejects_risk_knowledge_after_request_cutoff() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
    request = request_for(configuration)
    planner = FakeCandidatePlanner(configuration.planner)
    planner.risk_identity = SimpleNamespace(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        corridor_id=request.corridor.corridor_id,
        vessel_profile_id=request.vessel.vessel_profile_id,
        config_digest=request.run_context.config_digest,
        model_config_digest=request.model_config_digest,
        provenance=ProvenanceKind.SYNTHETIC,
        generation_id=request.generation_id,
    )
    planner.risk_as_of_times = (request.as_of_time + timedelta(seconds=1),)

    with pytest.raises(ContextMismatchError, match="knowledge cutoff"):
        PlanningService(planner, planner_config=configuration.planner).execute(request)


def test_rejected_route_switch_does_not_advance_committed_trigger_baseline() -> None:
    configuration = load_configuration(CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1")
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
