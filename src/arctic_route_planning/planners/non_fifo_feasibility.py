"""Finite, test-only non-FIFO label-correcting feasibility reference.

This module is deliberately not imported by the production planner or any
contract/ingress module.  It answers only a research question: can a finite
non-FIFO transition system be explored with exact-arrival labels while making
termination, cancellation, evaluator failure, and resource limits explicit?
No label with a different exact arrival is discarded as time-dominated.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from heapq import heappop, heappush
from math import isfinite
from typing import Any


class NonFifoSearchStatus(StrEnum):
    GOAL_FOUND = "GOAL_FOUND"
    EXHAUSTED = "EXHAUSTED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    CANCELLED = "CANCELLED"
    EVALUATOR_FAILURE = "EVALUATOR_FAILURE"


class NonFifoEvaluationError(RuntimeError):
    """A transition cannot be evaluated in the finite reference domain."""


@dataclass(frozen=True, slots=True)
class NonFifoBusinessEvidence:
    """Optional route-field evidence carried by one research transition.

    The production route schema is deliberately not imported here.  These
    fields let a finite fixture prove that a non-FIFO search preserves the
    business observations supplied by its edge evaluator instead of comparing
    only node names and scalar costs.
    """

    speed_knots: float | None = None
    risk_score: float | None = None
    maximum_risk: float | None = None
    confidence: float | None = None
    source_ids: tuple[str, ...] = ()
    hard_mask: bool = False

    def __post_init__(self) -> None:
        for name in ("speed_knots", "risk_score", "maximum_risk", "confidence"):
            value = getattr(self, name)
            if value is not None and (not isfinite(value) or value < 0.0):
                raise ValueError(f"{name} must be finite and non-negative when present")
        if self.confidence is not None and self.confidence > 1.0:
            raise ValueError("confidence must be at most one when present")
        source_ids = tuple(self.source_ids)
        if any(not isinstance(source_id, str) or not source_id for source_id in source_ids):
            raise ValueError("source_ids must contain non-empty strings")
        object.__setattr__(self, "source_ids", source_ids)
        if not isinstance(self.hard_mask, bool):
            raise ValueError("hard_mask must be a boolean")


@dataclass(frozen=True, slots=True)
class NonFifoTransition:
    """One exact-arrival edge result supplied by a test fixture."""

    arrival_time: datetime
    cost: float
    payload: Mapping[str, Any] | None = None
    business: NonFifoBusinessEvidence | None = None

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("non-FIFO transition arrival must be timezone-aware")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))
        if not isfinite(self.cost) or self.cost < 0.0:
            raise ValueError("non-FIFO transition cost must be finite and non-negative")
        object.__setattr__(self, "payload", dict(self.payload or {}))
        if self.business is not None and not isinstance(self.business, NonFifoBusinessEvidence):
            raise ValueError("business must be NonFifoBusinessEvidence when present")


@dataclass(frozen=True, slots=True)
class NonFifoParetoTransition:
    """A finite non-FIFO transition with a vector-valued route objective.

    This type is intentionally local to the research sidecar.  The vector is
    not exported to the production route contract: it only lets the finite
    feasibility search check that a label is discarded when (and only when) a
    newly generated label is component-wise dominated at the *same exact
    arrival state*.
    """

    arrival_time: datetime
    costs: tuple[float, ...]
    payload: Mapping[str, Any] | None = None
    business: NonFifoBusinessEvidence | None = None

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("non-FIFO Pareto transition arrival must be timezone-aware")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))
        costs = tuple(self.costs)
        if not costs or any(not isfinite(value) or value < 0.0 for value in costs):
            raise ValueError("non-FIFO Pareto transition costs must be finite and non-negative")
        object.__setattr__(self, "costs", costs)
        object.__setattr__(self, "payload", dict(self.payload or {}))
        if self.business is not None and not isinstance(self.business, NonFifoBusinessEvidence):
            raise ValueError("business must be NonFifoBusinessEvidence when present")


@dataclass(frozen=True, slots=True)
class NonFifoLabel:
    node: Any
    arrival_time: datetime
    cost: float
    path: tuple[Any, ...]
    transitions: tuple[NonFifoTransition, ...] = ()

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("non-FIFO label arrival must be timezone-aware")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))
        if not isfinite(self.cost) or self.cost < 0.0:
            raise ValueError("non-FIFO label cost must be finite and non-negative")
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "transitions", tuple(self.transitions))

    @property
    def exact_key(self) -> tuple[Any, datetime]:
        return self.node, self.arrival_time

    @property
    def business_evidence(self) -> tuple[NonFifoBusinessEvidence, ...]:
        """Return the edge evidence retained by this route label."""

        return tuple(
            transition.business
            for transition in self.transitions
            if transition.business is not None
        )


@dataclass(frozen=True, slots=True)
class NonFifoParetoLabel:
    """One exact-arrival label in the finite Pareto feasibility search."""

    node: Any
    arrival_time: datetime
    costs: tuple[float, ...]
    path: tuple[Any, ...]
    transitions: tuple[NonFifoParetoTransition, ...] = ()

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("non-FIFO Pareto label arrival must be timezone-aware")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))
        costs = tuple(self.costs)
        if not costs or any(not isfinite(value) or value < 0.0 for value in costs):
            raise ValueError("non-FIFO Pareto label costs must be finite and non-negative")
        object.__setattr__(self, "costs", costs)
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "transitions", tuple(self.transitions))

    @property
    def exact_key(self) -> tuple[Any, datetime]:
        return self.node, self.arrival_time

    @property
    def business_evidence(self) -> tuple[NonFifoBusinessEvidence, ...]:
        """Return the edge evidence retained by this route label."""

        return tuple(
            transition.business
            for transition in self.transitions
            if transition.business is not None
        )

    def dominates(self, other: NonFifoParetoLabel) -> bool:
        """Return safe same-exact-state Pareto dominance.

        Arrival time is part of the state.  Therefore a label at an earlier or
        later exact instant never dominates another label merely because its
        costs are lower; the future non-FIFO transition operator can differ.
        """

        if self.exact_key != other.exact_key or len(self.costs) != len(other.costs):
            return False
        return all(
            left <= right for left, right in zip(self.costs, other.costs, strict=True)
        ) and any(left < right for left, right in zip(self.costs, other.costs, strict=True))


@dataclass(frozen=True, slots=True)
class NonFifoSearchResult:
    status: NonFifoSearchStatus
    label: NonFifoLabel | None
    labels: tuple[NonFifoLabel, ...]
    expanded: int
    generated: int
    queue_peak: int
    edge_evaluations: int = 0
    evaluator_errors: tuple[str, ...] = ()
    reason: str | None = None
    pareto_pruned: int = 0

    @property
    def semantic_digest(self) -> str | None:
        if self.label is None:
            return None
        payload = {
            "path": self.label.path,
            "arrival_time": self.label.arrival_time,
            "cost": self.label.cost,
            "transitions": self.label.transitions,
        }
        return hashlib.sha256(
            json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class NonFifoParetoSearchResult:
    """Result of the bounded, test-only vector-label search."""

    status: NonFifoSearchStatus
    label: NonFifoParetoLabel | None
    labels: tuple[NonFifoParetoLabel, ...]
    expanded: int
    generated: int
    queue_peak: int
    edge_evaluations: int = 0
    pareto_pruned: int = 0
    evaluator_errors: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def goal_labels(self) -> tuple[NonFifoParetoLabel, ...]:
        """All retained goal labels, ordered by objective vector."""

        if self.label is None:
            return ()
        return tuple(label for label in self.labels if label.node == self.label.node)

    @property
    def goal_frontier(self) -> tuple[NonFifoParetoLabel, ...]:
        """The safe frontier; different exact arrivals remain incomparable."""

        goals = self.goal_labels
        return tuple(
            candidate
            for candidate in goals
            if not any(other is not candidate and other.dominates(candidate) for other in goals)
        )

    @property
    def semantic_digest(self) -> str | None:
        if self.label is None:
            return None
        payload = {
            "path": self.label.path,
            "arrival_time": self.label.arrival_time,
            "costs": self.label.costs,
            "transitions": self.label.transitions,
        }
        return hashlib.sha256(
            json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def search_non_fifo(
    *,
    start: Any,
    goal: Any,
    departure_time: datetime,
    neighbors: Callable[[Any], Iterable[Any]],
    evaluate_edge: Callable[[Any, Any, datetime], NonFifoTransition],
    max_expansions: int = 50_000,
    max_labels: int = 100_000,
    max_queue: int = 50_000,
    max_edge_evaluations: int = 400_000,
    cancel_check: Callable[[], bool] | None = None,
    maximum_elapsed: timedelta | None = None,
) -> NonFifoSearchResult:
    """Explore a finite non-FIFO graph without cross-arrival time pruning.

    Exact-arrival labels at one node are retained independently.  The only
    replacement is same-node/same-arrival cost improvement, which cannot
    change the future transition input.  The search drains the queue before
    accepting a goal so that an early goal is not mistaken for an optimum in a
    non-FIFO system.  Any evaluator error, cancellation, or bound exhaustion
    is returned as an explicit non-success status.
    """

    if departure_time.tzinfo is None or departure_time.utcoffset() is None:
        raise ValueError("departure_time must be timezone-aware")
    _validate_non_fifo_limits(
        objective_count=1,
        max_expansions=max_expansions,
        max_labels=max_labels,
        max_queue=max_queue,
        max_edge_evaluations=max_edge_evaluations,
    )
    departure = departure_time.astimezone(UTC)
    initial = NonFifoLabel(start, departure, 0.0, (start,))
    queue: list[tuple[float, datetime, int, NonFifoLabel]] = [(0.0, departure, 0, initial)]
    by_key: dict[tuple[Any, datetime], NonFifoLabel] = {initial.exact_key: initial}
    serial = 0
    expanded = 0
    generated = 0
    queue_peak = 1
    edge_evaluations = 0
    errors: list[str] = []
    best_goal: NonFifoLabel | None = None
    bounded = False

    while queue:
        if cancel_check is not None and cancel_check():
            return _result(
                NonFifoSearchStatus.CANCELLED,
                best_goal,
                by_key,
                expanded,
                generated,
                queue_peak,
                edge_evaluations,
                errors,
                "cancelled",
            )
        _, _, _, label = heappop(queue)
        if by_key.get(label.exact_key) != label:
            continue
        expanded += 1
        if expanded > max_expansions:
            bounded = True
            break
        if label.node == goal:
            if best_goal is None or label.cost < best_goal.cost:
                best_goal = label
            # Continue draining: non-FIFO means another arrival may unlock a
            # cheaper suffix even after a goal label has been observed.
            continue
        for neighbor in neighbors(label.node):
            if cancel_check is not None and cancel_check():
                return _result(
                    NonFifoSearchStatus.CANCELLED,
                    best_goal,
                    by_key,
                    expanded,
                    generated,
                    queue_peak,
                    edge_evaluations,
                    errors,
                    "cancelled",
                )
            edge_evaluations += 1
            if edge_evaluations > max_edge_evaluations:
                bounded = True
                break
            try:
                transition = evaluate_edge(label.node, neighbor, label.arrival_time)
                if not isinstance(transition, NonFifoTransition):
                    raise TypeError("non-FIFO evaluator must return NonFifoTransition")
                if transition.business is not None and transition.business.hard_mask:
                    raise NonFifoEvaluationError("hard_mask")
            except Exception as error:
                errors.append(f"{type(error).__name__}:{error}")
                continue
            if transition.arrival_time < departure:
                errors.append("ValueError:arrival_before_departure")
                continue
            if maximum_elapsed is not None and (
                transition.arrival_time - departure > maximum_elapsed
            ):
                continue
            next_label = NonFifoLabel(
                neighbor,
                transition.arrival_time,
                label.cost + transition.cost,
                (*label.path, neighbor),
                (*label.transitions, transition),
            )
            previous = by_key.get(next_label.exact_key)
            if previous is not None and previous.cost <= next_label.cost:
                # Same exact arrival only: a later or earlier arrival is not
                # considered dominated in a non-FIFO system.
                continue
            if previous is None and len(by_key) >= max_labels:
                bounded = True
                break
            by_key[next_label.exact_key] = next_label
            serial += 1
            heappush(queue, (next_label.cost, next_label.arrival_time, serial, next_label))
            generated += 1
            queue_peak = max(queue_peak, len(queue))
            if len(queue) > max_queue:
                bounded = True
                break
        if bounded:
            break

    if bounded:
        status = NonFifoSearchStatus.RESOURCE_LIMIT
        reason = "search_limit_exceeded"
    elif errors:
        status = NonFifoSearchStatus.EVALUATOR_FAILURE
        reason = "evaluator_failure"
    elif best_goal is not None:
        status = NonFifoSearchStatus.GOAL_FOUND
        reason = None
    else:
        status = NonFifoSearchStatus.EXHAUSTED
        reason = "no_route"
    return _result(
        status,
        best_goal,
        by_key,
        expanded,
        generated,
        queue_peak,
        edge_evaluations,
        errors,
        reason,
    )


def search_non_fifo_pareto(
    *,
    start: Any,
    goal: Any,
    departure_time: datetime,
    neighbors: Callable[[Any], Iterable[Any]],
    evaluate_edge: Callable[[Any, Any, datetime], NonFifoTransition | NonFifoParetoTransition],
    objective_count: int = 1,
    pareto_pruning: bool = False,
    max_expansions: int = 50_000,
    max_labels: int = 100_000,
    max_queue: int = 50_000,
    max_edge_evaluations: int = 400_000,
    cancel_check: Callable[[], bool] | None = None,
    maximum_elapsed: timedelta | None = None,
) -> NonFifoParetoSearchResult:
    """Explore a finite non-FIFO graph with exact-arrival Pareto labels.

    This is a C-internal research sidecar, not a production planner.  A
    label's exact ``(node, arrival_time)`` is part of its state, so Pareto
    pruning is limited to a newly generated label at the same exact state.
    Existing labels are never deleted, including labels that have already
    been expanded.  This conservative rule makes the safety boundary visible
    and avoids importing FIFO assumptions into a non-FIFO transition system.

    ``pareto_pruning`` is deliberately explicit.  When disabled, every
    finite label is retained until a frozen resource bound is reached.  When
    enabled, only a newly generated component-wise dominated label is
    discarded; an older label is never removed in response to a later label.
    Any evaluator error, cancellation, or resource limit is a non-success
    result and never returns a partial route.
    """

    _validate_non_fifo_limits(
        objective_count=objective_count,
        max_expansions=max_expansions,
        max_labels=max_labels,
        max_queue=max_queue,
        max_edge_evaluations=max_edge_evaluations,
    )
    if departure_time.tzinfo is None or departure_time.utcoffset() is None:
        raise ValueError("departure_time must be timezone-aware")
    if maximum_elapsed is not None and maximum_elapsed <= timedelta(0):
        raise ValueError("maximum_elapsed must be positive")

    departure = departure_time.astimezone(UTC)
    initial = NonFifoParetoLabel(start, departure, (0.0,) * objective_count, (start,))
    queue: list[tuple[tuple[float, ...], datetime, int, NonFifoParetoLabel]] = [
        (initial.costs, departure, 0, initial)
    ]
    labels_by_key: dict[tuple[Any, datetime], list[NonFifoParetoLabel]] = {
        initial.exact_key: [initial]
    }
    serial = 0
    expanded = 0
    generated = 0
    queue_peak = 1
    edge_evaluations = 0
    total_labels = 1
    pareto_pruned = 0
    errors: list[str] = []
    goals: list[NonFifoParetoLabel] = []
    bounded = False

    while queue:
        if _cancelled(cancel_check):
            return _pareto_result(
                NonFifoSearchStatus.CANCELLED,
                goals,
                labels_by_key,
                expanded,
                generated,
                queue_peak,
                edge_evaluations,
                pareto_pruned,
                errors,
                "cancelled",
            )
        _, _, _, label = heappop(queue)
        if not _contains_label(labels_by_key[label.exact_key], label):
            continue
        expanded += 1
        if expanded > max_expansions:
            bounded = True
            break
        if label.node == goal:
            goals.append(label)
            # Do not expand a goal, but drain other labels.  In a non-FIFO
            # system a later-arriving label can still have a cheaper vector.
            continue
        try:
            neighbours = tuple(neighbors(label.node))
        except Exception as error:
            errors.append(f"{type(error).__name__}:{error}")
            continue
        for neighbor in neighbours:
            if _cancelled(cancel_check):
                return _pareto_result(
                    NonFifoSearchStatus.CANCELLED,
                    goals,
                    labels_by_key,
                    expanded,
                    generated,
                    queue_peak,
                    edge_evaluations,
                    pareto_pruned,
                    errors,
                    "cancelled",
                )
            edge_evaluations += 1
            if edge_evaluations > max_edge_evaluations:
                bounded = True
                break
            try:
                transition = evaluate_edge(label.node, neighbor, label.arrival_time)
                transition = _coerce_pareto_transition(transition, objective_count)
                if transition.arrival_time <= label.arrival_time:
                    raise NonFifoEvaluationError("arrival_not_strictly_later")
                if transition.arrival_time < departure:
                    raise NonFifoEvaluationError("arrival_before_departure")
            except Exception as error:
                errors.append(f"{type(error).__name__}:{error}")
                continue
            if maximum_elapsed is not None and (
                transition.arrival_time - departure > maximum_elapsed
            ):
                continue
            next_label = NonFifoParetoLabel(
                neighbor,
                transition.arrival_time,
                tuple(
                    left + right for left, right in zip(label.costs, transition.costs, strict=True)
                ),
                (*label.path, neighbor),
                (*label.transitions, transition),
            )
            frontier = labels_by_key.setdefault(next_label.exact_key, [])
            if pareto_pruning and any(existing.dominates(next_label) for existing in frontier):
                pareto_pruned += 1
                continue
            if total_labels >= max_labels:
                bounded = True
                break
            frontier.append(next_label)
            total_labels += 1
            serial += 1
            heappush(queue, (next_label.costs, next_label.arrival_time, serial, next_label))
            generated += 1
            queue_peak = max(queue_peak, len(queue))
            if len(queue) > max_queue:
                bounded = True
                break
        if bounded:
            break

    if bounded:
        status = NonFifoSearchStatus.RESOURCE_LIMIT
        reason = "search_limit_exceeded"
        selected: NonFifoParetoLabel | None = None
    elif errors:
        status = NonFifoSearchStatus.EVALUATOR_FAILURE
        reason = "evaluator_failure"
        selected = None
    elif goals:
        status = NonFifoSearchStatus.GOAL_FOUND
        reason = None
        selected = min(goals, key=lambda item: (item.costs, item.arrival_time, repr(item.path)))
    else:
        status = NonFifoSearchStatus.EXHAUSTED
        reason = "no_route"
        selected = None
    return _pareto_result(
        status,
        [selected] if selected is not None else goals,
        labels_by_key,
        expanded,
        generated,
        queue_peak,
        edge_evaluations,
        pareto_pruned,
        errors,
        reason,
    )


def _validate_non_fifo_limits(
    *,
    objective_count: int,
    max_expansions: int,
    max_labels: int,
    max_queue: int,
    max_edge_evaluations: int,
) -> None:
    if (
        isinstance(objective_count, bool)
        or not isinstance(objective_count, int)
        or objective_count < 1
    ):
        raise ValueError("objective_count must be a positive integer")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1
        for value in (
            max_expansions,
            max_labels,
            max_queue,
            max_edge_evaluations,
        )
    ):
        raise ValueError("non-FIFO search limits must be positive integers")


def _coerce_pareto_transition(
    transition: NonFifoTransition | NonFifoParetoTransition, objective_count: int
) -> NonFifoParetoTransition:
    if isinstance(transition, NonFifoParetoTransition):
        if len(transition.costs) != objective_count:
            raise NonFifoEvaluationError("objective_dimension_mismatch")
        if transition.business is not None and transition.business.hard_mask:
            raise NonFifoEvaluationError("hard_mask")
        return transition
    if isinstance(transition, NonFifoTransition):
        if objective_count != 1:
            raise NonFifoEvaluationError("scalar_transition_for_vector_objective")
        if transition.business is not None and transition.business.hard_mask:
            raise NonFifoEvaluationError("hard_mask")
        return NonFifoParetoTransition(
            transition.arrival_time,
            (transition.cost,),
            transition.payload,
            transition.business,
        )
    raise TypeError("non-FIFO evaluator must return a Pareto transition")


def _contains_label(labels: Iterable[NonFifoParetoLabel], candidate: NonFifoParetoLabel) -> bool:
    return any(existing == candidate for existing in labels)


def _flatten_labels(
    labels_by_key: Mapping[tuple[Any, datetime], Iterable[NonFifoParetoLabel]],
) -> tuple[NonFifoParetoLabel, ...]:
    return tuple(label for labels in labels_by_key.values() for label in labels)


def _cancelled(cancel_check: Callable[[], bool] | None) -> bool:
    return cancel_check is not None and cancel_check()


def _pareto_result(
    status: NonFifoSearchStatus,
    goals: Iterable[NonFifoParetoLabel],
    labels_by_key: Mapping[tuple[Any, datetime], Iterable[NonFifoParetoLabel]],
    expanded: int,
    generated: int,
    queue_peak: int,
    edge_evaluations: int,
    pareto_pruned: int,
    errors: Iterable[str],
    reason: str | None,
) -> NonFifoParetoSearchResult:
    goal_list = tuple(goals)
    selected = (
        min(goal_list, key=lambda item: (item.costs, item.arrival_time, repr(item.path)))
        if status is NonFifoSearchStatus.GOAL_FOUND and goal_list
        else None
    )
    ordered = tuple(
        sorted(
            _flatten_labels(labels_by_key),
            key=lambda item: (item.costs, item.arrival_time, repr(item.node), repr(item.path)),
        )
    )
    return NonFifoParetoSearchResult(
        status=status,
        label=selected,
        labels=ordered,
        expanded=expanded,
        generated=generated,
        queue_peak=queue_peak,
        edge_evaluations=edge_evaluations,
        pareto_pruned=pareto_pruned,
        evaluator_errors=tuple(errors),
        reason=reason,
    )


def _result(
    status: NonFifoSearchStatus,
    label: NonFifoLabel | None,
    by_key: Mapping[tuple[Any, datetime], NonFifoLabel],
    expanded: int,
    generated: int,
    queue_peak: int,
    edge_evaluations: int,
    errors: Iterable[str],
    reason: str | None,
) -> NonFifoSearchResult:
    result_label = label if status is NonFifoSearchStatus.GOAL_FOUND else None
    return NonFifoSearchResult(
        status=status,
        label=result_label,
        labels=tuple(sorted(by_key.values(), key=lambda item: (item.cost, item.arrival_time))),
        expanded=expanded,
        generated=generated,
        queue_peak=queue_peak,
        edge_evaluations=edge_evaluations,
        evaluator_errors=tuple(errors),
        reason=reason,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, NonFifoBusinessEvidence):
        return {
            "speed_knots": value.speed_knots,
            "risk_score": value.risk_score,
            "maximum_risk": value.maximum_risk,
            "confidence": value.confidence,
            "source_ids": value.source_ids,
            "hard_mask": value.hard_mask,
        }
    if isinstance(value, NonFifoTransition):
        return {
            "arrival_time": _jsonable(value.arrival_time),
            "cost": value.cost,
            "payload": _jsonable(value.payload),
            "business": _jsonable(value.business),
        }
    if isinstance(value, NonFifoParetoTransition):
        return {
            "arrival_time": _jsonable(value.arrival_time),
            "costs": value.costs,
            "payload": _jsonable(value.payload),
            "business": _jsonable(value.business),
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "NonFifoBusinessEvidence",
    "NonFifoEvaluationError",
    "NonFifoLabel",
    "NonFifoParetoLabel",
    "NonFifoParetoSearchResult",
    "NonFifoParetoTransition",
    "NonFifoSearchResult",
    "NonFifoSearchStatus",
    "NonFifoTransition",
    "search_non_fifo",
    "search_non_fifo_pareto",
]
