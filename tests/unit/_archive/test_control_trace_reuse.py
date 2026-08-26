from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.errors import PlanningCancelled
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners._archive.control_trace_reuse import (
    ControlTraceObserver,
    ControlTraceReuseReason,
    ControlTraceReuseStatus,
    reuse_or_plan,
    try_reuse,
)
from arctic_route_planning.planners.time_dependent_astar import PlanningRequest, TimeDependentAStar
from arctic_route_planning.risk import RiskSampler

from .factories import T0, make_frame


def _planner() -> TimeDependentAStar:
    zero = np.zeros((3, 4), dtype=np.float32)
    frames = tuple(
        make_frame(
            T0 + timedelta(hours=offset),
            zero,
            risk_id=f"trace-{offset}",
            latitudes=(0.0, 0.05, 0.10),
            longitudes=(0.0, 0.05, 0.10, 0.15),
        )
        for offset in (0, 1, 3)
    )
    return TimeDependentAStar(
        RegularGrid.from_risk_frame(frames[0]),
        RiskSampler(frames),
        VesselPerformanceModel(10.0, 2.0, 12.0, 0.2),
    )


def _request(**kwargs) -> PlanningRequest:
    return PlanningRequest(
        start=(1, 0),
        goal=(1, 3),
        departure_time=T0,
        objective=ObjectiveMode.RECOMMENDED,
        maximum_elapsed=timedelta(hours=8),
        maximum_risk=1.0,
        **kwargs,
    )


def test_traced_control_is_deterministic_and_default_plan_is_unchanged() -> None:
    planner = _planner()
    request = _request()
    baseline = planner.plan(request)
    observed = ControlTraceObserver()
    traced, first = planner._plan_traced(request, observer=observed)
    _, second = planner._plan_traced(request)

    assert traced.nodes == baseline.nodes
    assert traced.total_cost_hours == pytest.approx(baseline.total_cost_hours)
    assert first.ordered_insertion_digest == second.ordered_insertion_digest
    assert first.certificate_digest == second.certificate_digest
    assert first.insertion_count == len(observed.events)
    assert first.insertion_count >= 1
    assert first.termination == "FIRST_GOAL_POP"
    assert any(event.transient for event in observed.events)


def test_same_query_hits_without_evaluating_an_edge_and_tightening_is_equivalent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = _planner()
    source_request = _request()
    _, trace = planner._plan_traced(source_request)

    def unexpected_edge(*args, **kwargs):
        raise AssertionError("a trace hit must not evaluate an edge")

    monkeypatch.setattr(planner, "_evaluate_edge", unexpected_edge)
    exact = try_reuse(trace, planner, source_request)
    tighter = try_reuse(
        trace,
        planner,
        replace(source_request, maximum_elapsed=timedelta(hours=5), maximum_risk=0.5),
    )

    assert exact.status is ControlTraceReuseStatus.HIT_EXACT
    assert tighter.status is ControlTraceReuseStatus.HIT_TRACE_EQUIVALENT
    assert exact.result is not None
    assert tighter.result is not None


def test_identity_change_and_constraint_widening_fail_closed() -> None:
    planner = _planner()
    source_request = _request()
    _, trace = planner._plan_traced(source_request)

    changed_objective = try_reuse(
        trace,
        planner,
        replace(source_request, objective=ObjectiveMode.FASTEST),
    )
    widened = try_reuse(
        trace,
        planner,
        replace(source_request, maximum_elapsed=None),
    )

    assert changed_objective.reason is ControlTraceReuseReason.IDENTITY_MISMATCH
    assert widened.reason is ControlTraceReuseReason.CONSTRAINT_WIDENING


def test_transient_history_envelope_blocks_reuse_even_when_final_route_fits() -> None:
    planner = _planner()
    source_request = _request()
    _, trace = planner._plan_traced(source_request)
    # This models a transient successful write retained by the compact
    # certificate maxima: the returned route still fits the target, but the
    # historical write did not.  Re-seal the immutable test carrier so the
    # failure is specifically the envelope fence, not seal corruption.
    transient_envelope = replace(
        trace,
        maximum_inserted_path_edge_risk=0.8,
        certificate_digest="",
    )
    target = replace(source_request, maximum_risk=0.5)

    outcome = try_reuse(transient_envelope, planner, target)

    assert transient_envelope.route_max_edge_risk <= 0.5
    assert outcome.status is ControlTraceReuseStatus.MISS_INCOMPATIBLE
    assert outcome.reason is ControlTraceReuseReason.TRACE_VIOLATES_TARGET


def test_invalid_seal_and_cancellation_are_not_silently_reused() -> None:
    planner = _planner()
    source_request = _request()
    _, trace = planner._plan_traced(source_request)
    with pytest.raises(ValueError):
        replace(trace, maximum_inserted_elapsed=0.1)

    cancelled = replace(source_request, cancel_check=lambda: True)
    with pytest.raises(PlanningCancelled):
        try_reuse(trace, planner, cancelled)
    with pytest.raises(PlanningCancelled):
        try_reuse(None, planner, cancelled)


def test_non_goal_termination_certificate_fails_closed() -> None:
    planner = _planner()
    source_request = _request()
    _, trace = planner._plan_traced(source_request)

    with pytest.raises(ValueError, match="first goal pop"):
        replace(trace, termination="EXHAUSTED", certificate_digest="")


def test_no_trace_has_explicit_cold_control_fallback() -> None:
    planner = _planner()
    outcome = reuse_or_plan(None, planner, _request())

    assert outcome.status is ControlTraceReuseStatus.COLD_CONTROL
    assert outcome.used_search is True
    assert outcome.result is not None
