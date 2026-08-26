from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import numpy as np
import pytest

from arctic_route_planning.adapters import FixtureRiskSource
from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import (
    InMemoryRiskSource,
    ProvenanceKind,
    RiskWindowQuery,
    canonical_risk_id,
)
from arctic_route_planning.development import create_development_run_context
from arctic_route_planning.domain import PlanKind, ReplanReason
from arctic_route_planning.errors import (
    ContextMismatchError,
    ContractError,
    PlanningCancelledError,
)
from arctic_route_planning.ingress import RiskSourcePlanningIngress
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanObservation,
)
from arctic_route_planning.service import ServicePlanningRequest

CONFIG_ROOT = Path(__file__).parents[2] / "configs"


class RecordingCommittedSource:
    def __init__(self, source: InMemoryRiskSource) -> None:
        self.source = source
        self.queries: list[RiskWindowQuery] = []

    def get_committed_window(self, query: RiskWindowQuery):
        self.queries.append(query)
        return self.source.get_committed_window(query)

    @contextmanager
    def lease_committed_window(self, query: RiskWindowQuery):
        self.queries.append(query)
        with self.source.lease_committed_window(query) as window:
            yield window


def _formal_frame(frame, *, longitude_step: float = 0.001):
    payload = frame.payload.assign_coords(
        latitude=np.asarray([70.0, 70.001, 70.002], dtype=np.float64),
        longitude=np.asarray(
            [18.0, 18.0 + longitude_step, 18.0 + 2 * longitude_step],
            dtype=np.float64,
        ),
    )
    payload.attrs["grid_id"] = "formal-ingress-test-grid"
    draft = replace(
        frame,
        risk_id="draft",
        payload=payload,
        provenance=ProvenanceKind.FORMAL,
    )
    return replace(draft, risk_id=canonical_risk_id(draft))


def _prepared_temporal_shadow_case(
    *,
    goal: tuple[int, int] = (0, 1),
    maximum_elapsed: timedelta = timedelta(hours=2),
):
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    store = InMemoryRiskSource()
    for frame in frames:
        store.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=frames[0].generation_id,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    store.commit_window(query)
    recording = RecordingCommittedSource(store)
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=goal,
        maximum_elapsed=maximum_elapsed,
    )
    ingress = RiskSourcePlanningIngress(recording, configuration=configuration)
    return ingress.prepare(request), recording, query, request


def test_formal_ingress_queries_full_exact_commit_and_executes_existing_planner() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    store = InMemoryRiskSource()
    for frame in frames:
        store.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=frames[0].generation_id,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    store.commit_window(query)
    recording = RecordingCommittedSource(store)
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    ingress = RiskSourcePlanningIngress(
        recording,
        configuration=configuration,
    )

    prepared = ingress.prepare(request)
    batch = prepared.execute()

    assert recording.queries == [query, query]
    assert prepared.window.query == query
    assert prepared.window.start == query.start
    assert prepared.window.end == query.end
    assert batch.published
    assert batch.selected.provenance is ProvenanceKind.FORMAL
    assert batch.selected.run_id == run_context.run_id

    with pytest.raises(ContextMismatchError, match="不属于已提交 RiskFrame 网格"):
        ingress.prepare(replace(request, start=(99, 99)))
    mismatched_vessel_model = replace(
        configuration.vessel_model,
        economic_speed_knots=configuration.vessel_model.economic_speed_knots + 0.25,
    )
    with pytest.raises(ContextMismatchError, match="vessel_model"):
        ingress.prepare(replace(request, vessel_model=mismatched_vessel_model))

    mutable_payload = prepared.window.frames[0].payload
    mutable_payload["risk_score"] = (
        ("latitude", "longitude"),
        np.zeros(mutable_payload["risk_score"].shape, dtype=np.float32),
    )
    with pytest.raises(ContractError, match=r"risk_level|risk_id"):
        prepared.execute()


def test_formal_ingress_executes_one_atomic_four_layer_set_under_the_lease() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    store = InMemoryRiskSource()
    for frame in frames:
        store.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=frames[0].generation_id,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    store.commit_window(query)
    recording = RecordingCommittedSource(store)
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 2),
        maximum_elapsed=timedelta(hours=2),
    )

    outcome = RiskSourcePlanningIngress(
        recording,
        configuration=configuration,
    ).execute_four_layer(request)

    assert recording.queries == [query, query]
    assert outcome.published
    assert len(outcome.plan_set.layers) == 4
    assert sum(len(bundle.plans) for bundle in outcome.plan_set.layers) == 12
    assert all(
        plan.provenance is ProvenanceKind.FORMAL
        and plan.generation_id == request.generation_id
        and plan.input_revision == request.input_revision
        and plan.source_risk_ids
        for bundle in outcome.plan_set.layers
        for plan in bundle.plans.values()
    )


def test_temporal_shadow_isolated_from_formal_latest_and_uses_two_scratch_runs() -> None:
    prepared, recording, query, request = _prepared_temporal_shadow_case()

    assert prepared.coordinator.store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.layered_store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    shadow = prepared.execute_four_layer_temporal_shadow()

    assert shadow.production_published is False
    assert shadow.risk_window_commit_id == prepared.window.commit_id
    assert shadow.risk_window_content_digest == prepared.window.content_digest
    assert shadow.control.status == "SUCCEEDED"
    assert shadow.candidate.status == "SUCCEEDED"
    assert shadow.control.scratch_published is True
    assert shadow.candidate.scratch_published is True
    assert shadow.control.outcome is not None
    assert shadow.candidate.outcome is not None
    assert shadow.control.outcome.published is False
    assert shadow.candidate.outcome.published is False
    assert shadow.control.snapshot is not None
    assert shadow.candidate.snapshot is not None
    assert shadow.control.snapshot.token != shadow.candidate.snapshot.token
    assert len(shadow.candidate.goal_certificates) == 3
    assert shadow.trace_observations == ()
    assert any(record.reused for record in shadow.reuse_outcomes), shadow.reuse_outcomes
    # Neither scratch publication may appear in the persistent formal store.
    assert prepared.coordinator.store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.layered_store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.trigger_evaluator._last_replan_at is None
    assert prepared.session.trigger_evaluator._baseline_avg_risk is None
    assert prepared.session.trigger_evaluator._baseline_max_risk is None
    assert recording.queries == [query, query]


def test_control_trace_shadow_reuses_only_full_to_main_and_cold_controls_other_layers() -> None:
    prepared, recording, query, request = _prepared_temporal_shadow_case()

    shadow = prepared.execute_four_layer_temporal_shadow(candidate_mode="control_trace")

    assert shadow.candidate_mode == "control_trace"
    assert shadow.production_published is False
    assert shadow.control.status == "SUCCEEDED"
    assert shadow.candidate.status == "SUCCEEDED"
    assert shadow.control.outcome is not None
    assert shadow.candidate.outcome is not None
    assert shadow.control.outcome.published is False
    assert shadow.candidate.outcome.published is False
    assert len(shadow.candidate.goal_certificates) == 0
    assert len(shadow.trace_observations) == 3
    assert all(item.status == "TRACE_CAPTURED" for item in shadow.trace_observations)
    assert all(item.digest and item.identity_digest for item in shadow.trace_observations)

    reused = [record for record in shadow.reuse_outcomes if record.reused]
    assert len(reused) == 3
    assert all(record.mode == "control_trace" for record in shadow.reuse_outcomes)
    assert all(record.target_goal == request.goal for record in reused)
    assert all(record.trace_digest and record.trace_write_count for record in reused)
    cold_control = [
        record for record in shadow.reuse_outcomes if record.status == "COLD_CONTROL"
    ]
    assert len(cold_control) == 6
    assert all(record.used_search for record in cold_control)

    # The shadow's scratch publication and its replan state must not leak into
    # the formal coordinator/store or the prepared session baseline.
    assert prepared.coordinator.store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.layered_store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.trigger_evaluator._last_replan_at is None
    assert prepared.session.trigger_evaluator._baseline_avg_risk is None
    assert prepared.session.trigger_evaluator._baseline_max_risk is None
    assert recording.queries == [query, query]


def test_control_trace_candidate_keeps_one_planner_and_audits_cache_progression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared, _, _, _ = _prepared_temporal_shadow_case()
    original_factory = type(prepared)._private_planner
    created: list[object] = []

    def record_factory(owner, current):
        planner = original_factory(owner, current)
        created.append(planner)
        return planner

    monkeypatch.setattr(type(prepared), "_private_planner", record_factory)
    result = prepared.execute_four_layer_temporal_shadow_track(
        track="candidate",
        candidate_mode="control_trace",
    )

    assert result.status == "SUCCEEDED"
    assert len(created) == 1
    assert len(result.timings) == 12
    for previous, current in zip(result.timings, result.timings[1:], strict=False):
        assert current.edge_geometry_cache_before == previous.edge_geometry_cache_after
    for timing in result.timings:
        before = timing.edge_geometry_cache_before
        after = timing.edge_geometry_cache_after
        delta = timing.edge_geometry_cache_delta
        assert set(before) == set(after) == set(delta) == {"entries", "hits", "misses"}
        assert all(after[name] >= before[name] for name in before)
        assert all(delta[name] == after[name] - before[name] for name in before)
    assert all(
        timing.edge_geometry_cache_delta == {"entries": 0, "hits": 0, "misses": 0}
        for timing in result.timings[3:6]
    )


def test_control_trace_candidate_failure_is_captured_without_formal_publication(
    monkeypatch,
) -> None:
    prepared, recording, query, request = _prepared_temporal_shadow_case()

    from arctic_route_planning import ingress as ingress_module

    def fail_candidate(*_args, **_kwargs):
        raise RuntimeError("synthetic candidate failure")

    monkeypatch.setattr(
        ingress_module._TemporalShadowCandidatePlanner,
        "plan_candidates",
        fail_candidate,
    )

    shadow = prepared.execute_four_layer_temporal_shadow(candidate_mode="control_trace")

    assert shadow.candidate_mode == "control_trace"
    assert shadow.production_published is False
    assert shadow.control.status == "SUCCEEDED"
    assert shadow.candidate.status == "FAILED"
    assert shadow.candidate.outcome is None
    assert shadow.candidate.error_type == "RuntimeError"
    assert shadow.candidate.error_message == "synthetic candidate failure"
    assert prepared.coordinator.store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert prepared.session.layered_store.latest(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) is None
    assert recording.queries == [query, query]


def test_single_shadow_track_reports_twelve_rows_and_preserves_formal_state() -> None:
    prepared, recording, query, request = _prepared_temporal_shadow_case()
    formal_layered_before = prepared.session.layered_store.snapshot(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    )
    formal_generation_before = prepared.session.generation_id

    control = prepared.execute_four_layer_temporal_shadow_track(track="control")
    candidate = prepared.execute_four_layer_temporal_shadow_track(
        track="candidate",
        candidate_mode="control_trace",
    )

    for result in (control, candidate):
        assert result.status == "SUCCEEDED"
        assert result.outcome is not None
        assert result.outcome.published is False
        assert result.production_published is False
        assert result.scratch_published is True
        assert result.route_integrity is True
        assert result.plan_set_digest
        assert len(result.timings) == 12
        assert all(item.wall_ms >= 0 for item in result.timings)
        assert all(
            item.pre_ms >= 0 and item.planner_ms >= 0 and item.post_ms >= 0
            for item in result.timings
        )
        assert all(
            item.pre_ms + item.planner_ms + item.post_ms
            <= item.wall_ms + 1e-6
            for item in result.timings
        )
        assert all(item.expanded >= 0 and item.edge >= 0 for item in result.timings)
        assert all(item.identity_digest for item in result.timings)
        assert all(item.state_counts["expanded_labels"] >= 0 for item in result.timings)
        assert result.scratch_proof.production_published is False
        assert result.scratch_proof.production_store_unchanged is True
        assert result.scratch_proof.production_session_unchanged is True
        assert result.scratch_proof.scratch_store_isolated is True

    assert [item.reuse_status for item in candidate.timings[:3]] == [
        "TRACE_CAPTURED"
    ] * 3
    assert [item.reuse_status for item in candidate.timings[3:6]] == [
        "HIT_EXACT"
    ] * 3
    assert all(item.search_used is False for item in candidate.timings[3:6])
    assert all(item.expanded == 0 and item.edge == 0 for item in candidate.timings[3:6])
    assert all(item.trace_context_present for item in candidate.timings[3:])
    assert all(item.trace_reuse_used for item in candidate.timings[3:6])
    assert all(not item.trace_reuse_used for item in candidate.timings[6:])
    assert candidate.status_counts == {
        "TRACE_CAPTURED": 3,
        "HIT_EXACT": 3,
        "COLD_CONTROL": 6,
    }
    assert all(
        item.reuse_status == "COLD_CONTROL" and item.search_used
        for item in candidate.timings[6:]
    )
    assert prepared.session.generation_id == formal_generation_before
    assert prepared.session.layered_store.snapshot(
        run_id=request.run_context.run_id,
        scenario_id=request.scenario.scenario_id,
        generation_id=request.generation_id,
    ) == formal_layered_before
    assert recording.queries == [query, query, query]


def test_single_shadow_track_plan_set_digest_is_repeatable() -> None:
    prepared, _, _, _ = _prepared_temporal_shadow_case()
    first = prepared.execute_four_layer_temporal_shadow_track(track="control")
    second = prepared.execute_four_layer_temporal_shadow_track(track="control")
    assert first.status == second.status == "SUCCEEDED"
    assert first.route_integrity is second.route_integrity is True
    assert first.plan_set_digest == second.plan_set_digest
    assert [item.route_digest for item in first.timings] == [
        item.route_digest for item in second.timings
    ]


def test_shadow_track_exposes_cpu_gc_and_trace_lifecycle_diagnostics() -> None:
    prepared, _, _, _ = _prepared_temporal_shadow_case()
    control = prepared.execute_four_layer_temporal_shadow_track(track="control")
    candidate = prepared.execute_four_layer_temporal_shadow_track(
        track="candidate",
        candidate_mode="control_trace",
    )

    assert all(item.planner_cpu_ms >= 0 for item in control.timings)
    assert all(len(item.gc_count_before) == 3 for item in control.timings)
    assert all(len(item.gc_count_after) == 3 for item in control.timings)
    assert all(len(item.gc_collections_delta) == 3 for item in control.timings)
    assert candidate.trace_lifecycle[:3] == ("retained",) * 3
    assert candidate.trace_lifecycle[3:] == ("retained",) * 9


def test_shadow_diagnostic_profiles_are_isolated_from_formal_baseline() -> None:
    prepared, _, _, _ = _prepared_temporal_shadow_case()
    forced_cold = prepared.execute_four_layer_temporal_shadow_track(
        track="candidate",
        candidate_mode="control_trace",
        diagnostic_profile="force_main_cold",
    )
    normalized = prepared.execute_four_layer_temporal_shadow_track(
        track="candidate",
        candidate_mode="control_trace",
        diagnostic_profile="post_main_normalize",
    )

    assert forced_cold.status == normalized.status == "SUCCEEDED"
    assert [item.reuse_status for item in forced_cold.timings[3:6]] == [
        "COLD_CONTROL"
    ] * 3
    assert forced_cold.status_counts == {
        "TRACE_CAPTURED": 3,
        "COLD_CONTROL": 9,
    }
    assert normalized.trace_lifecycle[:6] == ("retained",) * 6
    assert normalized.trace_lifecycle[6:] == ("retired",) * 6
    assert all(not item.trace_context_present for item in normalized.timings[6:])
    assert normalized.trace_normalization_ms >= 0


def test_formal_four_layer_replan_uses_six_hour_suffix_and_new_revision() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=11,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame, longitude_step=1.0) for frame in fixture.frames)
    store = InMemoryRiskSource()
    for frame in frames:
        store.publish(frame)
    initial_query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=frames[0].generation_id,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    store.commit_window(initial_query)
    initial_request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=0,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 2),
        maximum_elapsed=timedelta(hours=10),
    )
    ingress = RiskSourcePlanningIngress(store, configuration=configuration)
    initial = ingress.execute_four_layer(initial_request)
    trigger_time = initial_request.start_time + timedelta(hours=6)
    suffix_query = replace(
        initial_query,
        start=trigger_time,
    )
    suffix = store.commit_window(suffix_query)
    replan_request = replace(
        initial_request,
        input_revision=1,
        start_time=trigger_time,
        start=(0, 1),
        maximum_elapsed=timedelta(hours=4),
    )
    previous = initial.plan_set.recommended.metrics
    observation = ReplanObservation(
        observed_at=trigger_time,
        risk_valid_time=trigger_time,
        data_revision=1,
        risk_revision=suffix.commit_id,
        route_avg_risk=previous.avg_risk,
        route_max_risk=previous.max_risk,
    )

    replanned = ingress.replan_four_layer_if_needed(replan_request, observation)

    assert replanned.decision.triggered
    assert set(replanned.decision.reasons) == {ReplanReason.TIME, ReplanReason.DATA}
    assert replanned.outcome is not None and replanned.outcome.published
    assert replanned.outcome.plan_set.input_revision == 1
    assert replanned.outcome.plan_set.start_time == trigger_time
    assert replanned.outcome.plan_set.plan_kind is PlanKind.REPLANNED
    assert replanned.outcome.snapshot.previous == initial.plan_set


def test_ingress_recomputes_digest_from_complete_execution_configuration() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    changed_planner = replace(
        configuration.planner,
        edge_sample_count=configuration.planner.edge_sample_count + 2,
    )
    falsely_labelled = replace(configuration, planner=changed_planner)

    with pytest.raises(ValueError, match="planner_config_digest"):
        RiskSourcePlanningIngress(
            InMemoryRiskSource(),
            configuration=falsely_labelled,
        )


def test_prepared_execution_rechecks_configuration_digest() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    source = InMemoryRiskSource()
    for frame in frames:
        source.publish(frame)
    source.commit_window(
        RiskWindowQuery(
            start=frames[0].valid_time,
            end=frames[-1].valid_time,
            interval=timedelta(hours=1),
            run_id=frames[0].run_id,
            scenario_id=frames[0].scenario_id,
            corridor_id=frames[0].corridor_id,
            generation_id=7,
            vessel_profile_id=frames[0].vessel_profile_id,
            config_digest=frames[0].config_digest,
            model_config_digest=frames[0].model_config_digest,
            as_of=frames[0].as_of_time,
        )
    )
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    prepared = RiskSourcePlanningIngress(
        source,
        configuration=configuration,
    ).prepare(request)
    object.__setattr__(
        configuration,
        "planner",
        replace(
            configuration.planner,
            edge_sample_count=configuration.planner.edge_sample_count + 2,
        ),
    )

    with pytest.raises(ValueError, match="planner_config_digest"):
        prepared.execute()


def test_execution_lease_blocks_generation_reset_until_planning_finishes() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    source = InMemoryRiskSource()
    for frame in frames:
        source.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=frames[0].generation_id,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    source.commit_window(query)
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    entered = Event()
    release = Event()
    reset_done = Event()
    result: list[object] = []

    class BlockingLeaseSource:
        def get_committed_window(self, requested):
            return source.get_committed_window(requested)

        @contextmanager
        def lease_committed_window(self, requested):
            with source.lease_committed_window(requested) as leased:
                entered.set()
                assert release.wait(timeout=5)
                yield leased

    ingress = RiskSourcePlanningIngress(
        BlockingLeaseSource(),
        configuration=configuration,
    )
    prepared = ingress.prepare(request)
    planning_thread = Thread(target=lambda: result.append(prepared.execute()))
    reset_thread = Thread(
        target=lambda: (source.reset_to_generation(8), reset_done.set())
    )

    planning_thread.start()
    assert entered.wait(timeout=5)
    reset_thread.start()
    assert not reset_done.wait(timeout=0.1)
    release.set()
    planning_thread.join(timeout=5)
    reset_thread.join(timeout=5)

    assert result and result[0].published
    assert reset_done.is_set()
    with pytest.raises(ContextMismatchError, match="已提交"):
        prepared.execute()


def test_ingress_reuses_one_planning_coordinator_across_prepared_requests() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    source = InMemoryRiskSource()
    for frame in frames:
        source.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=7,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    source.commit_window(query)
    base_request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    ingress = RiskSourcePlanningIngress(
        source,
        configuration=configuration,
    )

    first = ingress.prepare(base_request)
    second = ingress.prepare(replace(base_request, input_revision=2))

    assert first.coordinator is ingress.coordinator
    assert second.coordinator is ingress.coordinator
    assert not hasattr(first, "service")


def test_ingress_isolates_planning_coordinators_between_runs() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    first_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=first_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    first_frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    second_context = replace(
        first_context,
        run_id="run-00000000-0000-4000-8000-000000000222",
    )
    second_frames = []
    for frame in first_frames:
        draft = replace(frame, run_id=second_context.run_id, risk_id="draft")
        second_frames.append(replace(draft, risk_id=canonical_risk_id(draft)))
    source = InMemoryRiskSource()
    for frame in (*first_frames, *second_frames):
        source.publish(frame)
    for frames in (first_frames, tuple(second_frames)):
        source.commit_window(
            RiskWindowQuery(
                start=frames[0].valid_time,
                end=frames[-1].valid_time,
                interval=timedelta(hours=1),
                run_id=frames[0].run_id,
                scenario_id=frames[0].scenario_id,
                corridor_id=frames[0].corridor_id,
                generation_id=7,
                vessel_profile_id=frames[0].vessel_profile_id,
                config_digest=frames[0].config_digest,
                model_config_digest=frames[0].model_config_digest,
                as_of=frames[0].as_of_time,
            )
        )
    request = ServicePlanningRequest(
        run_context=first_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=first_frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=first_frames[0].as_of_time,
        start_time=first_frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    ingress = RiskSourcePlanningIngress(
        source,
        configuration=configuration,
    )

    first = ingress.prepare(request)
    second = ingress.prepare(replace(request, run_context=second_context))

    assert first.coordinator is ingress.coordinator
    assert second.coordinator is not first.coordinator


def test_generation_changed_after_prepare_rejects_old_execute_without_publication() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    source = InMemoryRiskSource()
    for frame in frames:
        source.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=7,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    source.commit_window(query)
    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    ingress = RiskSourcePlanningIngress(
        source,
        configuration=configuration,
    )
    prepared = ingress.prepare(request)

    source.reset_to_generation(8)

    with pytest.raises(ContextMismatchError, match="已提交"):
        prepared.execute()
    assert ingress.coordinator.store.snapshot().current is None


def test_shared_coordinator_supersedes_concurrent_older_revision() -> None:
    configuration = load_configuration(
        CONFIG_ROOT, "tromso_isfjorden_july_2026_retrospective_v1"
    )
    run_context = create_development_run_context(configuration, source_kind="formal")
    fixture = FixtureRiskSource(
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        run_context=run_context,
        generation_id=7,
        as_of_time=configuration.scenario.simulation_start,
        frame_count=3,
        shape=(3, 3),
    )
    frames = tuple(_formal_frame(frame) for frame in fixture.frames)
    backing = InMemoryRiskSource()
    for frame in frames:
        backing.publish(frame)
    query = RiskWindowQuery(
        start=frames[0].valid_time,
        end=frames[-1].valid_time,
        interval=timedelta(hours=1),
        run_id=frames[0].run_id,
        scenario_id=frames[0].scenario_id,
        corridor_id=frames[0].corridor_id,
        generation_id=7,
        vessel_profile_id=frames[0].vessel_profile_id,
        config_digest=frames[0].config_digest,
        model_config_digest=frames[0].model_config_digest,
        as_of=frames[0].as_of_time,
    )
    window = backing.commit_window(query)

    class ConcurrentLeaseSource:
        def get_committed_window(self, requested):
            window.assert_matches(requested)
            return window

        @contextmanager
        def lease_committed_window(self, requested):
            window.assert_matches(requested)
            yield window

    request = ServicePlanningRequest(
        run_context=run_context,
        scenario=configuration.scenario,
        corridor=configuration.corridor,
        vessel=configuration.vessel,
        vessel_model=configuration.vessel_model,
        model_config_digest=frames[0].model_config_digest,
        planner_config_digest=configuration.planner_config_digest,
        risk_provenance=ProvenanceKind.FORMAL,
        generation_id=7,
        input_revision=1,
        as_of_time=frames[0].as_of_time,
        start_time=frames[0].valid_time,
        start=(0, 0),
        goal=(0, 1),
        maximum_elapsed=timedelta(hours=2),
    )
    entered = Event()
    release = Event()

    class BlockingCoordinator(PlanningCoordinator):
        def begin(self, **kwargs):
            handle = super().begin(**kwargs)
            if kwargs["input_revision"] == 1:
                entered.set()
                assert release.wait(timeout=5)
            return handle

    ingress = RiskSourcePlanningIngress(
        ConcurrentLeaseSource(),
        configuration=configuration,
        coordinator=BlockingCoordinator(),
    )
    older = ingress.prepare(request)
    newer = ingress.prepare(replace(request, input_revision=2))
    older_errors: list[BaseException] = []

    def execute_older() -> None:
        try:
            older.execute()
        except BaseException as exc:  # test thread must surface its failure
            older_errors.append(exc)

    older_thread = Thread(target=execute_older)
    older_thread.start()
    assert entered.wait(timeout=5)

    newer_batch = newer.execute()
    release.set()
    older_thread.join(timeout=5)

    assert newer_batch.published
    assert newer_batch.selected.input_revision == 2
    assert len(older_errors) == 1
    assert isinstance(older_errors[0], PlanningCancelledError)
    assert ingress.coordinator.store.snapshot().current == newer_batch.selected
