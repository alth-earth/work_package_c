from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from arctic_route_planning.domain import ObjectiveMode, PlanKind
from arctic_route_planning.publishing import RouteMetrics, RoutePlan, Waypoint
from arctic_route_planning.replanning import (
    PlanningCancelled,
    PlanningCoordinator,
    ReplanningPolicy,
    ReplanObservation,
    ReplanReason,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)
CONFIG_DIGEST = "a" * 64


def observation(**changes: object) -> ReplanObservation:
    values: dict[str, object] = {
        "observed_at": BASE,
        "risk_valid_time": BASE,
        "data_revision": 1,
        "risk_revision": "risk-1",
        "route_avg_risk": 0.2,
        "route_max_risk": 0.3,
    }
    values.update(changes)
    return ReplanObservation(**values)  # type: ignore[arg-type]


def plan_for(handle, **changes: object) -> RoutePlan:
    token = handle.token
    values: dict[str, object] = {
        "schema_version": "cd.route-plan.v1",
        "scenario_id": token.scenario_id,
        "corridor_id": "corridor",
        "vessel_profile_id": "demo-bulker-v1",
        "config_digest": token.config_digest,
        "generation_id": token.generation_id,
        "planning_request_id": token.planning_request_id,
        "input_revision": token.input_revision,
        "plan_id": "plan",
        "plan_version": "v1",
        "generated_at": BASE + timedelta(minutes=1),
        "as_of_time": BASE,
        "start_time": BASE,
        "objective_mode": ObjectiveMode.RECOMMENDED,
        "plan_kind": PlanKind.INITIAL,
        "waypoints": (
            Waypoint(10.0, 70.0, BASE, 9.0),
            Waypoint(11.0, 71.0, BASE + timedelta(hours=2), 9.0),
        ),
        "metrics": RouteMetrics(
            distance_km=120.0,
            eta_hours=2.0,
            avg_risk=0.2,
            max_risk=0.3,
            integrated_risk_hours=0.4,
            minimum_confidence=0.8,
            hard_constraint_violations=0,
            turn_count=0,
            expanded_nodes=8,
            compute_ms=10.0,
            objective_cost=10.0,
        ),
        "replan_reasons": (),
        "source_risk_ids": ("risk-1",),
        "planner_version": "v1",
    }
    values.update(changes)
    return RoutePlan(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"risk_valid_time": BASE + timedelta(hours=1)}, ReplanReason.TIME),
        ({"data_revision": 2}, ReplanReason.DATA),
        ({"route_max_risk": 0.7}, ReplanReason.RISK),
        ({"deviation_km": 10.0}, ReplanReason.DEVIATION),
        ({"event_revision": "closure-1"}, ReplanReason.EVENT),
        ({"manual_requested": True}, ReplanReason.MANUAL),
    ],
)
def test_each_replan_trigger_class(changes: dict[str, object], reason: ReplanReason) -> None:
    evaluator = ReplanTriggerEvaluator()
    evaluator.mark_replanned(observation())

    decision = evaluator.evaluate(observation(observed_at=BASE + timedelta(hours=1), **changes))

    assert decision.triggered
    assert reason in decision.reasons


def test_minimum_interval_suppresses_noise_but_manual_bypasses() -> None:
    evaluator = ReplanTriggerEvaluator(ReplanningPolicy(min_interval=timedelta(hours=1)))
    evaluator.mark_replanned(observation())

    noise = evaluator.evaluate(
        observation(observed_at=BASE + timedelta(minutes=10), deviation_km=20.0)
    )
    manual = evaluator.evaluate(
        observation(observed_at=BASE + timedelta(minutes=10), manual_requested=True)
    )

    assert not noise.triggered
    assert noise.suppressed_by_min_interval
    assert noise.retry_at == BASE + timedelta(hours=1)
    assert manual.triggered
    assert ReplanReason.MANUAL in manual.reasons


def test_risk_hysteresis_does_not_repeat_unchanged_high_signal() -> None:
    evaluator = ReplanTriggerEvaluator(ReplanningPolicy(min_interval=timedelta(0)))
    high = observation(route_avg_risk=0.6, route_max_risk=0.7)
    evaluator.mark_replanned(high)

    steady = evaluator.evaluate(
        observation(
            observed_at=BASE + timedelta(hours=1),
            route_avg_risk=0.61,
            route_max_risk=0.71,
        )
    )

    assert ReplanReason.RISK not in steady.reasons


def test_trigger_evaluator_rejects_stale_data_revision() -> None:
    evaluator = ReplanTriggerEvaluator()
    evaluator.mark_replanned(observation(data_revision=3))

    with pytest.raises(ValueError, match="data_revision"):
        evaluator.evaluate(observation(observed_at=BASE + timedelta(hours=1), data_revision=2))


def test_switch_gate_requires_benefit_and_limits_risk_regression() -> None:
    coordinator = PlanningCoordinator(request_id_factory=lambda: "request")
    handle = coordinator.begin(
        scenario_id="demo", generation_id=1, config_digest=CONFIG_DIGEST, input_revision=1
    )
    current = plan_for(handle)
    gate = RouteSwitchGate(ReplanningPolicy(min_switch_improvement=0.05, risk_hysteresis=0.02))

    assert not gate.evaluate(
        current,
        replace(current, plan_id="tiny", metrics=replace(current.metrics, objective_cost=9.8)),
    ).accepted
    assert gate.evaluate(
        current,
        replace(current, plan_id="better", metrics=replace(current.metrics, objective_cost=9.0)),
    ).accepted
    assert not gate.evaluate(
        current,
        replace(
            current,
            plan_id="riskier",
            metrics=replace(current.metrics, objective_cost=8.0, max_risk=0.4),
        ),
    ).accepted
    assert gate.evaluate(
        current,
        replace(
            current,
            plan_id="event",
            metrics=replace(current.metrics, objective_cost=11.0, max_risk=0.4),
        ),
        reasons=(ReplanReason.EVENT,),
    ).accepted


def test_coordinator_cancels_same_generation_older_request_before_publish() -> None:
    request_ids = iter(("old", "new"))
    coordinator = PlanningCoordinator(request_id_factory=lambda: next(request_ids))
    old = coordinator.begin(
        scenario_id="demo", generation_id=1, config_digest=CONFIG_DIGEST, input_revision=7
    )
    old_plan = plan_for(old)
    new = coordinator.begin(
        scenario_id="demo", generation_id=1, config_digest=CONFIG_DIGEST, input_revision=7
    )

    assert old.cancelled
    with pytest.raises(PlanningCancelled):
        coordinator.publish(old, old_plan)

    new_plan = plan_for(new, plan_id="new-plan")
    snapshot = coordinator.publish(new, new_plan)
    assert snapshot.current == new_plan


def test_coordinator_rejects_old_input_revision_activation() -> None:
    coordinator = PlanningCoordinator(request_id_factory=lambda: "request")
    current = coordinator.begin(
        scenario_id="demo", generation_id=2, config_digest=CONFIG_DIGEST, input_revision=5
    )

    with pytest.raises(Exception, match="older input revision"):
        coordinator.begin(
            scenario_id="demo",
            generation_id=2,
            config_digest=CONFIG_DIGEST,
            input_revision=4,
        )
    assert not current.cancelled
    assert coordinator.is_current(current)
