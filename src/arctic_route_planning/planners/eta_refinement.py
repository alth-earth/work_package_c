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
    {
        "cycle",
        "max_iterations",
        "terminal_mismatch",
        "invalid_operator",
        "no_fixed_point",
    }
)
_ALLOWED_METHODS = frozenset({"damped", "bounded"})


@dataclass(frozen=True, slots=True)
class EtaRefinementPolicy:
    """Bounds and numerical tolerances for ETA fixed-point refinement.

    ``method="damped"`` keeps the historical damped fixed-point iteration.
    ``method="bounded"`` uses an interval-contraction (bisection-like) search
    that first brackets a sign change of ``implied(t) - t``; it converges on
    oscillatory fields where damping diverges, and reports ``no_fixed_point``
    (fail-closed) when no sign change exists on the search interval instead of
    silently returning a non-fixed point.  ``max_iterations`` bounds the
    bisection steps.
    """

    max_iterations: int = 12
    absolute_tolerance_seconds: float = 1.0
    relative_tolerance: float = 1e-6
    relaxation: float = 0.5
    history_size: int = 4
    method: str = "damped"

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
        if self.method not in _ALLOWED_METHODS:
            raise ValueError(f"unsupported ETA refinement method: {self.method!r}")


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
    that ETA guess and its implied travel time.  ``policy.method`` selects the
    fixed-point algorithm:

    - ``"damped"`` (default, historical): ``guess += relaxation * (implied -
      guess)``.  Converges on smooth fields; may diverge on strongly
      oscillatory fields.
    - ``"bounded"`` (C-ALG-03B): interval contraction over the ETA domain.
      Brackets a sign change of ``implied(t) - t`` and bisects to the
      tolerance; converges on oscillatory fields.  If no sign change exists on
      the interval it fails closed with ``no_fixed_point`` instead of
      returning a non-fixed point.

    Once the *raw* residual is within tolerance, the operator is evaluated one
    more time at the raw ETA; only that terminal evaluation is returned to
    callers.  Any invalid operator output, detected recent cycle,
    non-convergence, or terminal inconsistency fails closed with
    :class:`EtaRefinementError`.
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
    if active_policy.method == "bounded":
        return _refine_bounded(initial, evaluate, active_policy)
    return _refine_damped(initial, evaluate, active_policy)


def _refine_damped(
    initial: float,
    evaluate: Callable[[float], EtaEvaluation],
    active_policy: EtaRefinementPolicy,
) -> EtaRefinementResult:
    """Damped fixed-point iteration (historical default)."""
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


def _refine_bounded(
    initial: float,
    evaluate: Callable[[float], EtaEvaluation],
    active_policy: EtaRefinementPolicy,
) -> EtaRefinementResult:
    """Interval-contraction fixed point (C-ALG-03B, robust on oscillatory fields).

    The residual function is ``g(t) = implied(t) - t``.  The search starts
    from the initial guess and widens outward until ``g`` changes sign (a
    bracketed root) or the widening hits the iteration budget.  On a bracketed
    root the interval is bisected down to tolerance.  If no sign change is
    found on the searched interval the refinement fails closed with
    ``no_fixed_point``: returning the closest sample would silently accept an
    ETA that is not self-consistent, which is exactly the correctness debt the
    bounded method exists to eliminate.
    """

    max_residual_seconds = 0.0

    def _implied(t: float) -> float:
        evaluation = _invoke_operator(
            evaluate, t, iteration=0, stage="bounded"
        )
        raw = _finite_positive_hours(evaluation.implied_travel_hours)
        if raw is None:  # pragma: no cover - defensive invariant
            raise EtaRefinementError(
                "invalid_operator", {"stage": "bounded", "message": "implied invalid"}
            )
        nonlocal max_residual_seconds
        max_residual_seconds = max(max_residual_seconds, abs(raw - t) * 3600.0)
        return raw

    def _g(t: float) -> float:
        return _implied(t) - t

    # Bracket: expand outward from the initial guess until g changes sign.
    # The bracket phase is budgeted separately so the bisection below keeps
    # enough iterations to actually converge.  Bisection halves the interval
    # each step, so it needs O(log2(width / tolerance)) evaluations: give it
    # its own budget decoupled from the damped iteration count.
    bracket_budget = max(1, active_policy.max_iterations // 3)
    bisection_budget = max(16, active_policy.max_iterations * 2)
    left = initial
    right = initial
    left_g = _g(left)
    right_g = left_g
    expanded = 0
    while (left_g < 0.0) == (right_g < 0.0) and expanded < bracket_budget:
        step = max(initial, 0.5) * (2.0 ** expanded)
        left = max(initial - step, initial / 4.0)
        right = initial + step
        left_g = _g(left)
        right_g = _g(right)
        expanded += 1

    bracketed = (left_g < 0.0) != (right_g < 0.0)
    if not bracketed:
        # No sign change on the searched interval: no fixed point is provable.
        raise EtaRefinementError(
            "no_fixed_point",
            {
                "stage": "bounded_bracket",
                "initial_guess_hours": initial,
                "left_hours": left,
                "right_hours": right,
                "left_residual_seconds": left_g * 3600.0,
                "right_residual_seconds": right_g * 3600.0,
                "iterations": expanded,
                "max_residual_seconds": max_residual_seconds,
                "message": "no ETA fixed point found on the searched interval",
            },
        )

    iterations = 0
    while iterations < bisection_budget:
        mid = (left + right) / 2.0
        implied_mid = _implied(mid)
        mid_g = implied_mid - mid
        residual_seconds = abs(mid_g) * 3600.0
        max_residual_seconds = max(max_residual_seconds, residual_seconds)
        tolerance_hours = _tolerance_hours(mid, implied_mid, active_policy)
        if residual_seconds / 3600.0 <= tolerance_hours:
            # Terminal evaluation at the accepted ETA for self-consistency.
            terminal_evaluation = _invoke_operator(
                evaluate, mid, iteration=iterations, stage="terminal"
            )
            terminal_raw = _finite_positive_hours(
                terminal_evaluation.implied_travel_hours
            )
            if terminal_raw is None:  # pragma: no cover - defensive invariant
                raise EtaRefinementError(
                    "invalid_operator",
                    {"stage": "terminal", "iteration": iterations},
                )
            terminal_residual_seconds = abs(terminal_raw - mid) * 3600.0
            max_residual_seconds = max(
                max_residual_seconds, terminal_residual_seconds
            )
            terminal_tolerance_hours = _tolerance_hours(
                mid, terminal_raw, active_policy
            )
            if (
                terminal_residual_seconds / 3600.0
                > terminal_tolerance_hours
            ):
                raise EtaRefinementError(
                    "terminal_mismatch",
                    {
                        "stage": "terminal",
                        "iteration": iterations,
                        "raw_travel_hours": mid,
                        "terminal_implied_travel_hours": terminal_raw,
                        "terminal_residual_seconds": terminal_residual_seconds,
                        "tolerance_seconds": terminal_tolerance_hours * 3600.0,
                        "max_residual_seconds": max_residual_seconds,
                        "message": "terminal evaluation is not self-consistent",
                    },
                )
            return EtaRefinementResult(
                travel_hours=mid,
                evaluation=terminal_evaluation,
                iterations=iterations,
                terminal_resamples=1,
                max_residual_seconds=max_residual_seconds,
            )
        if (left_g < 0.0) == (mid_g < 0.0):
            left, left_g = mid, mid_g
        else:
            right, right_g = mid, mid_g
        iterations += 1

    raise EtaRefinementError(
        "max_iterations",
        {
            "stage": "bounded",
            "iterations": bisection_budget,
            "left_hours": left,
            "right_hours": right,
            "max_residual_seconds": max_residual_seconds,
            "message": "bounded ETA refinement did not converge within budget",
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
