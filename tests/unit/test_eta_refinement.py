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


def test_bounded_method_converges_on_oscillatory_operator() -> None:
    """C-ALG-03B: the bounded method converges where damping diverges.

    An operator whose implied ETA oscillates with the guess (the signature of a
    strongly time-varying Winter risk field) makes the damped fixed point
    diverge.  The bounded interval contraction brackets the sign change of
    ``implied(t) - t`` and bisects to the tolerance.
    """

    def evaluate(guess: float) -> EtaEvaluation:
        implied = 2.2 + 0.5 * math.sin(guess * 6.0)
        return _evaluation(implied)

    result = refine_eta(
        1.0, evaluate, policy=EtaRefinementPolicy(method="bounded")
    )

    assert result.travel_hours > 0.0
    assert result.max_residual_seconds > 0.0
    # the returned ETA must be self-consistent within tolerance
    implied_at_return = 2.2 + 0.5 * math.sin(result.travel_hours * 6.0)
    assert abs(implied_at_return - result.travel_hours) * 3600.0 <= 1.0


def test_bounded_method_reports_missing_bracket_as_uncertain() -> None:
    """C-ALG-03B: no finite sign change is not a global no-root proof.

    An operator with ``implied(t) == t + 0.5`` everywhere has no fixed point,
    but the finite bracket search is not itself a proof of that fact.  The
    bounded method must fail closed with ``no_bracket_found`` and preserve an
    explicitly uncertain evidence class.
    """
    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(
            1.0,
            lambda guess: _evaluation(guess + 0.5),
            policy=EtaRefinementPolicy(method="bounded"),
        )

    assert raised.value.reason == "no_bracket_found"
    assert raised.value.failure_class == "fixed_point_uncertain"
    assert raised.value.diagnostics["proof_status"] == "uncertain_no_bracket"
    assert raised.value.diagnostics["initial_guess_hours"] == 1.0


def test_bounded_method_does_not_call_a_root_outside_finite_bracket_absent_proof() -> None:
    """A root outside the searched interval must remain an uncertainty."""

    def evaluate(guess: float) -> EtaEvaluation:
        # The root is at 10h, while the default finite bracket from 1h never
        # reaches it.  No-bracket is therefore the only sound conclusion.
        return _evaluation(10.0)

    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(
            1.0,
            evaluate,
            policy=EtaRefinementPolicy(method="bounded"),
        )

    assert raised.value.reason == "no_bracket_found"
    assert raised.value.failure_class == "fixed_point_uncertain"


@pytest.mark.parametrize("bad_method", ["bogus", "", "damped2", None])
def test_unsupported_method_value_fails_closed(bad_method: object) -> None:
    with pytest.raises(ValueError):
        EtaRefinementPolicy(method=bad_method)  # type: ignore[arg-type]


def test_bounded_method_restores_domain_rejection() -> None:
    """C-ALG-03B: callback exceptions inside the bounded path remain
    invalid_operator (fail-closed) with the original exception preserved."""
    with pytest.raises(EtaRefinementError) as raised:
        refine_eta(
            1.0,
            lambda guess: (_ for _ in ()).throw(ValueError("hard mask")),
            policy=EtaRefinementPolicy(method="bounded"),
        )

    assert raised.value.reason == "invalid_operator"
    assert raised.value.diagnostics["operator_exception"] == "ValueError"
