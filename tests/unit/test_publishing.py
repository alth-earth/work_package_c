from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arctic_route_planning.contracts import ProvenanceKind
from arctic_route_planning.domain import ObjectiveMode, PlanKind
from arctic_route_planning.publishing import (
    CDLatestStore,
    PublicationRejected,
    PublicationToken,
    RouteMetrics,
    RoutePlan,
    TradeoffDeltas,
    Waypoint,
    build_selection_rationale,
    route_plan_from_dict,
    route_plan_from_geojson,
    route_plan_to_dict,
    route_plan_to_geojson,
    selection_rationale_from_dict,
    selection_rationale_to_dict,
    serialization,
    token_for_plan,
    write_route_plan_geojson,
    write_route_plan_json,
    write_selection_rationale_json,
)

CONFIG_DIGEST = "a" * 64
MODEL_CONFIG_DIGEST = "b" * 64
PLANNER_CONFIG_DIGEST = "c" * 64


def make_plan(
    *,
    request_id: str = "request-1",
    input_revision: int = 1,
    objective_mode: str = "recommended",
    plan_id: str = "plan-1",
    generated_minute: int = 1,
    config_digest: str = CONFIG_DIGEST,
) -> RoutePlan:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return RoutePlan(
        schema_version="cd.route-plan.v2",
        run_id="run-publishing-tests",
        scenario_id="demo",
        corridor_id="corridor",
        vessel_profile_id="demo-bulker-v1",
        config_digest=config_digest,
        model_config_digest=MODEL_CONFIG_DIGEST,
        planner_config_digest=PLANNER_CONFIG_DIGEST,
        provenance=ProvenanceKind.SYNTHETIC,
        generation_id=3,
        planning_request_id=request_id,
        input_revision=input_revision,
        plan_id=plan_id,
        plan_version="planner-v1",
        generated_at=start + timedelta(minutes=generated_minute),
        as_of_time=start,
        start_time=start,
        objective_mode=ObjectiveMode(objective_mode),
        plan_kind=PlanKind.INITIAL,
        waypoints=(
            Waypoint(10.0, 70.0, start, 9.0),
            Waypoint(11.0, 71.0, start + timedelta(hours=2), 8.5),
        ),
        metrics=RouteMetrics(
            distance_km=120.0,
            eta_hours=2.0,
            avg_risk=0.2,
            max_risk=0.4,
            integrated_risk_hours=0.4,
            minimum_confidence=0.8,
            hard_constraint_violations=0,
            turn_count=0,
            expanded_nodes=8,
            compute_ms=15.0,
            objective_cost=2.8,
        ),
        replan_reasons=(),
        source_risk_ids=("risk-20260101T0000Z-v1",),
        planner_version="time-dependent-a-star.v1",
    )


def test_route_plan_json_and_geojson_round_trip(tmp_path) -> None:
    plan = make_plan()

    assert route_plan_from_dict(route_plan_to_dict(plan)) == plan
    assert route_plan_to_dict(plan)["provenance"] == "synthetic"
    geojson = route_plan_to_geojson(plan)
    assert geojson["type"] == "FeatureCollection"
    assert geojson["features"][0]["geometry"]["type"] == "LineString"
    assert route_plan_from_geojson(geojson) == plan

    json_path = write_route_plan_json(tmp_path / "nested" / "plan.json", plan)
    geojson_path = write_route_plan_geojson(tmp_path / "nested" / "plan.geojson", plan)
    assert json.loads(json_path.read_text(encoding="utf-8"))["plan_id"] == "plan-1"
    assert json.loads(geojson_path.read_text(encoding="utf-8"))["type"] == "FeatureCollection"
    assert not list((tmp_path / "nested").glob("*.tmp"))

    malformed = route_plan_to_dict(plan)
    malformed["destination_reached"] = "false"
    with pytest.raises(ValueError, match="boolean"):
        route_plan_from_dict(malformed)


def test_route_plan_from_dict_rejects_missing_schema_version() -> None:
    plan = make_plan()
    malformed = route_plan_to_dict(plan)
    del malformed["schema_version"]
    with pytest.raises(ValueError, match="schema_version"):
        route_plan_from_dict(malformed)


def test_route_plan_json_matches_shared_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[2] / "schemas" / "route-plan-v2.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    jsonschema.validate(route_plan_to_dict(make_plan()), schema)


def test_atomic_write_failure_preserves_previous_file(monkeypatch, tmp_path) -> None:
    destination = tmp_path / "latest.json"
    destination.write_text('{"plan_id":"old"}\n', encoding="utf-8")

    def fail_after_partial_write(_value, handle, **_kwargs) -> None:
        handle.write('{"plan_id":')
        raise RuntimeError("simulated serialization failure")

    monkeypatch.setattr(serialization.json, "dump", fail_after_partial_write)

    with pytest.raises(RuntimeError, match="simulated"):
        serialization.atomic_write_json(destination, {"plan_id": "new"})
    assert destination.read_text(encoding="utf-8") == '{"plan_id":"old"}\n'
    assert not list(tmp_path.glob(".latest.json.*.tmp"))


def test_route_plan_rejects_invalid_contract_values() -> None:
    plan = make_plan()
    with pytest.raises(ValueError, match="严格递增"):
        replace(
            plan,
            waypoints=(plan.waypoints[0], replace(plan.waypoints[1], eta=plan.start_time)),
        )
    with pytest.raises(ValueError, match="硬约束"):
        replace(plan, metrics=replace(plan.metrics, hard_constraint_violations=1))
    with pytest.raises(ValueError, match="provenance"):
        replace(plan, provenance="claimed_formal")


def test_cd_store_keeps_current_previous_and_candidates() -> None:
    store = CDLatestStore()
    selected = make_plan()
    candidate = make_plan(objective_mode="fastest", plan_id="fastest-1")
    store.activate(token_for_plan(selected))

    first = store.publish(selected, [candidate])
    assert first.current == selected
    assert first.previous is None
    assert first.candidates == (candidate,)

    newer = replace(
        selected,
        plan_id="plan-2",
        generated_at=selected.generated_at + timedelta(minutes=1),
    )
    second = store.publish(newer)
    assert second.current == newer
    assert second.previous == selected
    assert store.latest(run_id=selected.run_id, scenario_id="demo", generation_id=3) == newer
    assert store.latest(run_id=selected.run_id, scenario_id="demo", generation_id=4) is None


def test_cd_store_rejects_mixed_candidate_provenance() -> None:
    store = CDLatestStore()
    selected = make_plan()
    mixed = replace(
        make_plan(objective_mode="fastest", plan_id="fastest-legacy"),
        provenance=ProvenanceKind.LEGACY_UNVERIFIED,
    )
    store.activate(token_for_plan(selected))

    with pytest.raises(PublicationRejected, match="provenance"):
        store.publish(selected, [mixed])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scenario_id", "another-scenario"),
        ("generation_id", 4),
        ("config_digest", "b" * 64),
        ("input_revision", 2),
        ("planning_request_id", "request-2"),
    ],
)
def test_cd_store_rejects_every_stale_context_dimension(field: str, value: object) -> None:
    store = CDLatestStore()
    plan = make_plan()
    active = token_for_plan(plan)
    store.activate(active)
    stale_token = replace(active, **{field: value})
    stale_plan = replace(plan, **{field: value})

    with pytest.raises(PublicationRejected):
        store.publish(stale_plan, token=stale_token)


def test_new_same_generation_request_supersedes_old_result() -> None:
    store = CDLatestStore()
    old_plan = make_plan(request_id="old", input_revision=5)
    newer_token = PublicationToken(
        "run-publishing-tests",
        "demo",
        3,
        CONFIG_DIGEST,
        MODEL_CONFIG_DIGEST,
        PLANNER_CONFIG_DIGEST,
        5,
        "new",
    )
    store.activate(token_for_plan(old_plan))
    store.activate(newer_token)

    with pytest.raises(PublicationRejected, match="stale"):
        store.publish(old_plan)


def test_generation_switch_hides_previous_generation_immediately() -> None:
    store = CDLatestStore()
    plan = make_plan()
    store.activate(token_for_plan(plan))
    store.publish(plan)
    store.activate(
        PublicationToken(
            "run-publishing-tests",
            "demo",
            4,
            CONFIG_DIGEST,
            MODEL_CONFIG_DIGEST,
            PLANNER_CONFIG_DIGEST,
            0,
            "after-seek",
        )
    )

    assert store.latest(run_id=plan.run_id, scenario_id="demo", generation_id=3) is None
    snapshot = store.snapshot(scenario_id="demo", generation_id=4)
    assert snapshot.current is None
    assert snapshot.previous is None


def test_config_switch_hides_incompatible_current_route() -> None:
    store = CDLatestStore()
    plan = make_plan()
    store.activate(token_for_plan(plan))
    store.publish(plan)

    store.activate(
        PublicationToken(
            "run-publishing-tests",
            "demo",
            3,
            "d" * 64,
            MODEL_CONFIG_DIGEST,
            PLANNER_CONFIG_DIGEST,
            1,
            "new-config",
        )
    )

    assert store.latest(run_id=plan.run_id, scenario_id="demo", generation_id=3) is None


def test_cancelled_token_cannot_be_reactivated() -> None:
    store = CDLatestStore()
    token = token_for_plan(make_plan())
    store.activate(token)
    assert store.cancel(token)

    with pytest.raises(PublicationRejected, match="reactivate"):
        store.activate(token)


def _make_recommended_plan(*, eta_hours: float, avg_risk: float, distance_km: float) -> RoutePlan:
    baseline = make_plan(
        objective_mode="recommended",
        plan_id="plan-recommended",
        generated_minute=1,
    )
    new_metrics = RouteMetrics(
        distance_km=distance_km,
        eta_hours=eta_hours,
        avg_risk=avg_risk,
        max_risk=max(avg_risk + 0.05, 0.5),
        integrated_risk_hours=avg_risk * eta_hours,
        minimum_confidence=0.8,
        hard_constraint_violations=0,
        turn_count=0,
        expanded_nodes=8,
        compute_ms=15.0,
        objective_cost=eta_hours,
    )
    start = baseline.start_time
    new_waypoints = (
        Waypoint(10.0, 70.0, start, baseline.waypoints[0].recommended_speed_mps),
        Waypoint(
            11.0,
            71.0,
            start + timedelta(hours=eta_hours),
            baseline.waypoints[1].recommended_speed_mps,
        ),
    )
    return replace(baseline, metrics=new_metrics, waypoints=new_waypoints)


def _make_pair(eta_baseline: float, eta_recommended: float, risk_recommended: float):
    baseline = replace(
        _make_recommended_plan(eta_hours=eta_baseline, avg_risk=0.20, distance_km=120.0),
        objective_mode=ObjectiveMode.FASTEST,
        plan_id="plan-fastest",
    )
    selected = _make_recommended_plan(
        eta_hours=eta_recommended,
        avg_risk=risk_recommended,
        distance_km=baseline.metrics.distance_km - 10.0,
    )
    return selected, baseline


def test_build_selection_rationale_derives_quantified_tradeoffs() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.16)

    rationale = build_selection_rationale(selected, baseline)

    sm, bm = selected.metrics, baseline.metrics
    assert rationale.selected_plan_id == selected.plan_id
    assert rationale.baseline_plan_id == baseline.plan_id
    assert rationale.selected_objective is ObjectiveMode.RECOMMENDED
    assert rationale.baseline_objective is ObjectiveMode.FASTEST
    assert rationale.tradeoffs.delta_distance_km == pytest.approx(sm.distance_km - bm.distance_km)
    assert rationale.tradeoffs.delta_eta_hours == pytest.approx(sm.eta_hours - bm.eta_hours)
    assert rationale.tradeoffs.delta_avg_risk == pytest.approx(sm.avg_risk - bm.avg_risk)
    expected_reduction = (bm.avg_risk - sm.avg_risk) / bm.avg_risk * 100.0
    assert rationale.tradeoffs.avg_risk_reduction_pct == pytest.approx(expected_reduction)
    assert rationale.summary_text
    assert "平均风险" in rationale.summary_text or "推荐路线" in rationale.summary_text
    assert "时间" in rationale.summary_text or "距离" in rationale.summary_text


def test_build_selection_rationale_rejects_non_fastest_baseline() -> None:
    selected = _make_recommended_plan(eta_hours=3.0, avg_risk=0.2, distance_km=110.0)
    wrong_baseline = replace(
        _make_recommended_plan(eta_hours=2.0, avg_risk=0.2, distance_km=120.0),
        objective_mode=ObjectiveMode.LOW_RISK,
        plan_id="plan-low",
    )

    with pytest.raises(ValueError, match="baseline"):
        build_selection_rationale(selected, wrong_baseline)


def test_build_selection_rationale_rejects_mismatched_run_identity() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.2)
    other_baseline = replace(baseline, run_id="other-run")

    with pytest.raises(ValueError, match="身份不一致"):
        build_selection_rationale(selected, other_baseline)


def test_selection_rationale_round_trip_through_atomic_write(tmp_path) -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.18)
    rationale = build_selection_rationale(selected, baseline)

    document = selection_rationale_to_dict(rationale)
    assert document["schema_version"] == "selection-rationale.v1"
    restored = selection_rationale_from_dict(document)
    assert restored == rationale

    written = write_selection_rationale_json(tmp_path / "nested" / "rationale.json", rationale)
    assert written == tmp_path / "nested" / "rationale.json"
    on_disk = json.loads(written.read_text(encoding="utf-8"))
    assert on_disk["selected_plan_id"] == selected.plan_id
    assert on_disk["baseline_objective"] == "fastest"
    assert not list((tmp_path / "nested").glob("*.tmp"))


def test_selection_rationale_from_dict_rejects_missing_tradeoffs() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.2)
    rationale = build_selection_rationale(selected, baseline)
    document = selection_rationale_to_dict(rationale)
    del document["tradeoffs"]
    with pytest.raises(ValueError, match="tradeoffs"):
        selection_rationale_from_dict(document)


def test_selection_rationale_from_dict_rejects_missing_schema_version() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.2)
    rationale = build_selection_rationale(selected, baseline)
    document = selection_rationale_to_dict(rationale)
    del document["schema_version"]
    with pytest.raises(ValueError, match="schema_version"):
        selection_rationale_from_dict(document)


def test_selection_rationale_from_dict_rejects_non_finite_tradeoffs() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.2)
    rationale = build_selection_rationale(selected, baseline)
    document = selection_rationale_to_dict(rationale)
    document["tradeoffs"]["delta_avg_risk"] = float("inf")
    with pytest.raises(ValueError):
        selection_rationale_from_dict(document)


def test_tradeoff_deltas_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError):
        TradeoffDeltas(
            delta_distance_km=float("nan"),
            delta_eta_hours=0.0,
            delta_avg_risk=0.0,
            delta_max_risk=0.0,
            delta_integrated_risk_hours=0.0,
            avg_risk_reduction_pct=0.0,
            max_risk_reduction_pct=0.0,
        )


def test_selection_rationale_rejects_empty_summary_text() -> None:
    selected, baseline = _make_pair(eta_baseline=2.0, eta_recommended=3.0, risk_recommended=0.2)
    rationale = build_selection_rationale(selected, baseline)
    with pytest.raises(ValueError):
        replace(rationale, summary_text="")
