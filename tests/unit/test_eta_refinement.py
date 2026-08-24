from __future__ import annotations

import math

import pytest

from arctic_route_planning.planners.eta_refinement import (
    EtaEvaluation,
    EtaRefinementError,
    EtaRefinementPolicy,
    refine_eta,
)


def _evaluation(hours: float, marker: object = "sample") -> EtaEvaluation:
    return EtaEvaluation(samples=(marker,), speed="speed", implied_travel_hours=hours)


def test_constant_operator_converges_and_performs_terminal_resample() -> None:
    calls: list[float] = []

    def evaluate(guess: float) -> EtaEvaluation:
        calls.append(guess)
        return _evaluation(2.0, marker=guess)

    result = refine_eta(2.0, evaluate)

    assert result.travel_hours == pytest.approx(2.0)
    assert result.evaluation.samples == (2.0,)
    assert result.iterations == 1
    assert result.terminal_resamples == 1
    assert calls == [2.0, 2.0]


def test_damped_operator_can_converge_after_more_than_two_iterations() -> None:
    result = refine_eta(
        1.0,
        lambda guess: _evaluation(3.0),
        policy=EtaRefinementPolicy(max_iterations=30),
    )

    assert result.iterations > 2
    assert result.travel_hours == pytest.approx(3.0, abs=1.0 / 3600.0)
    assert result.max_residual_seconds == pytest.approx(7200.0)


def test_recent_non_adjacent_two_cycle_fails_closed() -> None:
    def evaluate(guess: float) -> EtaEvaluation:
        return _evaluation(10.0 if math.isclose(guess, 2.0) else 2.0)

    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(
            2.0,
            evaluate,
            policy=EtaRefinementPolicy(relaxation=1.0, max_iterations=10),
        )

    assert raised.value.reason == "cycle"
    assert raised.value.diagnostics["period"] == 2


def test_non_convergent_operator_hits_iteration_bound() -> None:
    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(
            1.0,
            lambda guess: _evaluation(guess + 1.0),
            policy=EtaRefinementPolicy(max_iterations=3),
        )

    assert raised.value.reason == "max_iterations"
    assert raised.value.diagnostics["iterations"] == 3


def test_terminal_mismatch_is_reported() -> None:
    calls = 0

    def evaluate(guess: float) -> EtaEvaluation:
        nonlocal calls
        calls += 1
        return _evaluation(1.0001 if calls == 1 else 1.2)

    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(1.0, evaluate)

    assert raised.value.reason == "terminal_mismatch"
    assert raised.value.diagnostics["terminal_residual_seconds"] > 1.0
    assert calls == 2


@pytest.mark.parametrize("bad_value", [0.0, float("nan"), float("inf"), "not-a-number"])
def test_invalid_operator_values_fail_closed(bad_value: object) -> None:
    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(1.0, lambda guess: _evaluation(bad_value))

    assert raised.value.reason == "invalid_operator"


def test_callback_exception_is_invalid_operator() -> None:
    def evaluate(guess: float) -> EtaEvaluation:
        raise ValueError("hard mask rejected")

    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(1.0, evaluate)

    assert raised.value.reason == "invalid_operator"
    assert raised.value.diagnostics["operator_exception"] == "ValueError"


def test_terminal_evaluation_is_returned_not_the_pre_terminal_sample() -> None:
    calls = 0

    def evaluate(guess: float) -> EtaEvaluation:
        nonlocal calls
        calls += 1
        if calls == 1:
            return EtaEvaluation(samples=("pre-terminal",), speed="old", implied_travel_hours=1.0)
        return EtaEvaluation(samples=("terminal",), speed="new", implied_travel_hours=1.0002)

    result = refine_eta(1.0, evaluate)

    assert result.evaluation.samples == ("terminal",)
    assert result.evaluation.speed == "new"
    assert result.travel_hours == pytest.approx(1.0)
    assert result.terminal_resamples == 1
