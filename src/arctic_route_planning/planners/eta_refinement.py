"""Fail-closed fixed-point refinement for time-dependent edge ETAs.

The planner owns the fixed-point iteration while the evaluation callback owns
all domain checks (risk coverage, hard masks, confidence, and vessel speed).
The callback is deliberately small so the refinement policy can be tested
independently from the production grid and risk sampler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from math import isfinite

_ALLOWED_ERROR_REASONS = frozenset(
    {"cycle", "max_iterations", "terminal_mismatch", "invalid_operator"}
)


@dataclass(frozen=True, slots=True)
class EtaRefinementPolicy:
    """Bounds and numerical tolerances for ETA fixed-point refinement."""

    max_iterations: int = 12
    absolute_tolerance_seconds: float = 1.0
    relative_tolerance: float = 1e-6
    relaxation: float = 0.5
    history_size: int = 4

    def __post_init__(self) -> None:
        if isinstance(self.max_iterations, bool) or self.max_iterations < 1:
            raise ValueError("max_iterations must be a positive integer")
        if not isfinite(self.absolute_tolerance_seconds) or (
            self.absolute_tolerance_seconds <= 0.0
        ):
            raise ValueError("absolute_tolerance_seconds must be finite and positive")
        if not isfinite(self.relative_tolerance) or self.relative_tolerance < 0.0:
            raise ValueError("relative_tolerance must be finite and non-negative")
        if not isfinite(self.relaxation) or not 0.0 < self.relaxation <= 1.0:
            raise ValueError("relaxation must be finite and within (0, 1]")
        if isinstance(self.history_size, bool) or self.history_size < 3:
            raise ValueError("history_size must be at least 3")


@dataclass(frozen=True, slots=True)
class EtaEvaluation:
    """Domain result for one ETA-dependent edge evaluation.

    ``samples`` and ``speed`` intentionally remain opaque to this module. The
    callback validates their domain-specific contents before returning them.
    """

    samples: tuple[object, ...]
    speed: object
    implied_travel_hours: float


@dataclass(frozen=True, slots=True)
class EtaRefinementResult:
    """Converged ETA and the terminal-domain evaluation used to accept it."""

    travel_hours: float
    evaluation: EtaEvaluation
    iterations: int
    terminal_resamples: int
    max_residual_seconds: float


class EtaRefinementError(RuntimeError):
    """Raised when ETA refinement cannot produce a self-consistent result."""

    def __init__(self, reason: str, diagnostics: Mapping[str, object] | None = None) -> None:
        if reason not in _ALLOWED_ERROR_REASONS:
            raise ValueError(f"unsupported ETA refinement error reason: {reason!r}")
        self.reason = reason
        self.diagnostics = dict(diagnostics or {})
        detail = self.diagnostics.get("message", "ETA refinement failed")
        super().__init__(f"{reason}: {detail}")


def refine_eta(
    initial_guess_hours: float,
    evaluate: Callable[[float], EtaEvaluation],
    policy: EtaRefinementPolicy | None = None,
) -> EtaRefinementResult:
    """Refine an ETA until the raw fixed-point residual and terminal residual pass.

    ``evaluate(guess_hours)`` must return the domain evaluation sampled using
    that ETA guess and its implied travel time. The update is damped as
    ``guess += relaxation * (implied - guess)``. Once the *raw* residual is
    within tolerance, the operator is evaluated one more time at the raw ETA;
    only that terminal evaluation is returned to callers.

    Any invalid operator output, detected recent cycle, non-convergence, or
    terminal inconsistency fails closed with :class:`EtaRefinementError`.
    """

    active_policy = policy or EtaRefinementPolicy()
    initial = _finite_positive_hours(initial_guess_hours)
    if initial is None:
        raise EtaRefinementError(
            "invalid_operator",
            {
                "stage": "initial_guess",
                "initial_guess_hours": repr(initial_guess_hours),
                "message": "initial_guess_hours must be finite and positive",
            },
        )

    guess = initial
    history: list[tuple[float, float]] = []
    max_residual_seconds = 0.0

    for iteration in range(1, active_policy.max_iterations + 1):
        evaluation = _invoke_operator(evaluate, guess, iteration=iteration, stage="iterate")
        raw = _finite_positive_hours(evaluation.implied_travel_hours)
        # _invoke_operator validates this. Keeping the guard local makes the
        # arithmetic below explicit to static type checkers and future edits.
        if raw is None:  # pragma: no cover - defensive invariant
            raise EtaRefinementError(
                "invalid_operator",
                {"stage": "iterate", "iteration": iteration},
            )

        residual_seconds = abs(raw - guess) * 3600.0
        max_residual_seconds = max(max_residual_seconds, residual_seconds)
        tolerance_hours = _tolerance_hours(guess, raw, active_policy)

        if residual_seconds / 3600.0 <= tolerance_hours:
            terminal_evaluation = _invoke_operator(
                evaluate,
                raw,
                iteration=iteration,
                stage="terminal",
            )
            terminal_raw = _finite_positive_hours(terminal_evaluation.implied_travel_hours)
            if terminal_raw is None:  # pragma: no cover - defensive invariant
                raise EtaRefinementError(
                    "invalid_operator",
                    {"stage": "terminal", "iteration": iteration},
                )
            terminal_residual_seconds = abs(terminal_raw - raw) * 3600.0
            max_residual_seconds = max(max_residual_seconds, terminal_residual_seconds)
            terminal_tolerance_hours = _tolerance_hours(raw, terminal_raw, active_policy)
            if terminal_residual_seconds / 3600.0 > terminal_tolerance_hours:
                raise EtaRefinementError(
                    "terminal_mismatch",
                    {
                        "stage": "terminal",
                        "iteration": iteration,
                        "raw_travel_hours": raw,
                        "terminal_implied_travel_hours": terminal_raw,
                        "terminal_residual_seconds": terminal_residual_seconds,
                        "tolerance_seconds": terminal_tolerance_hours * 3600.0,
                        "max_residual_seconds": max_residual_seconds,
                        "message": "terminal evaluation is not self-consistent",
                    },
                )
            return EtaRefinementResult(
                # ``terminal_evaluation`` was sampled at ``raw``. Keep that
                # sampling instant as the accepted ETA; ``terminal_raw`` is
                # the independently checked operator value and may differ by
                # up to the declared tolerance.
                travel_hours=raw,
                evaluation=terminal_evaluation,
                iterations=iteration,
                terminal_resamples=1,
                max_residual_seconds=max_residual_seconds,
            )

        state = (guess, raw)
        cycle = _find_recent_cycle(history, state, active_policy)
        if cycle is not None:
            period, previous_index = cycle
            raise EtaRefinementError(
                "cycle",
                {
                    "stage": "iterate",
                    "iteration": iteration,
                    "period": period,
                    "history_index": previous_index,
                    "guess_hours": guess,
                    "implied_travel_hours": raw,
                    "residual_seconds": residual_seconds,
                    "max_residual_seconds": max_residual_seconds,
                    "history": tuple([*history, state]),
                    "message": "recent non-adjacent ETA states repeat",
                },
            )
        history.append(state)
        if len(history) > active_policy.history_size:
            del history[0]

        guess += active_policy.relaxation * (raw - guess)
        if not isfinite(guess) or guess <= 0.0:
            raise EtaRefinementError(
                "invalid_operator",
                {
                    "stage": "iterate_update",
                    "iteration": iteration,
                    "updated_guess_hours": guess,
                    "message": "damped ETA update is not finite and positive",
                },
            )

    raise EtaRefinementError(
        "max_iterations",
        {
            "stage": "iterate",
            "iterations": active_policy.max_iterations,
            "last_guess_hours": guess,
            "max_residual_seconds": max_residual_seconds,
            "history": tuple(history),
            "message": "ETA fixed-point residual did not converge",
        },
    )


def _invoke_operator(
    evaluate: Callable[[float], EtaEvaluation],
    guess_hours: float,
    *,
    iteration: int,
    stage: str,
) -> EtaEvaluation:
    try:
        evaluation = evaluate(guess_hours)
    except Exception as exc:
        raise EtaRefinementError(
            "invalid_operator",
            {
                "stage": stage,
                "iteration": iteration,
                "guess_hours": guess_hours,
                "operator_exception": type(exc).__name__,
                "operator_message": str(exc),
                "message": "ETA evaluation callback failed",
            },
        ) from exc
    if not isinstance(evaluation, EtaEvaluation):
        raise EtaRefinementError(
            "invalid_operator",
            {
                "stage": stage,
                "iteration": iteration,
                "guess_hours": guess_hours,
                "returned_type": type(evaluation).__name__,
                "message": "ETA evaluation callback must return EtaEvaluation",
            },
        )
    implied = _finite_positive_hours(evaluation.implied_travel_hours)
    if implied is None:
        raise EtaRefinementError(
            "invalid_operator",
            {
                "stage": stage,
                "iteration": iteration,
                "guess_hours": guess_hours,
                "implied_travel_hours": repr(evaluation.implied_travel_hours),
                "message": "implied_travel_hours must be finite and positive",
            },
        )
    return evaluation


def _finite_positive_hours(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(numeric) or numeric <= 0.0:
        return None
    return numeric


def _tolerance_hours(
    left_hours: float,
    right_hours: float,
    policy: EtaRefinementPolicy,
) -> float:
    scale_hours = max(1.0, abs(left_hours), abs(right_hours))
    return max(policy.absolute_tolerance_seconds / 3600.0, policy.relative_tolerance * scale_hours)


def _find_recent_cycle(
    history: list[tuple[float, float]],
    current: tuple[float, float],
    policy: EtaRefinementPolicy,
) -> tuple[int, int] | None:
    """Return ``(period, index)`` for a repeated non-adjacent state."""

    current_guess, current_raw = current
    for index, (previous_guess, previous_raw) in enumerate(history):
        period = len(history) - index
        if period < 2:
            continue
        tolerance_guess = _tolerance_hours(current_guess, previous_guess, policy)
        tolerance_raw = _tolerance_hours(current_raw, previous_raw, policy)
        if (
            abs(current_guess - previous_guess) <= tolerance_guess
            and abs(current_raw - previous_raw) <= tolerance_raw
        ):
            return period, index
    return None
