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
class NonFifoTransition:
    """One exact-arrival edge result supplied by a test fixture."""

    arrival_time: datetime
    cost: float
    payload: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.arrival_time.tzinfo is None or self.arrival_time.utcoffset() is None:
            raise ValueError("non-FIFO transition arrival must be timezone-aware")
        object.__setattr__(self, "arrival_time", self.arrival_time.astimezone(UTC))
        if not isfinite(self.cost) or self.cost < 0.0:
            raise ValueError("non-FIFO transition cost must be finite and non-negative")
        object.__setattr__(self, "payload", dict(self.payload or {}))


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


@dataclass(frozen=True, slots=True)
class NonFifoSearchResult:
    status: NonFifoSearchStatus
    label: NonFifoLabel | None
    labels: tuple[NonFifoLabel, ...]
    expanded: int
    generated: int
    queue_peak: int
    evaluator_errors: tuple[str, ...] = ()
    reason: str | None = None

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
    if any(
        isinstance(value, bool) or value < 1
        for value in (max_expansions, max_labels, max_queue)
    ):
        raise ValueError("non-FIFO search limits must be positive integers")
    departure = departure_time.astimezone(UTC)
    initial = NonFifoLabel(start, departure, 0.0, (start,))
    queue: list[tuple[float, datetime, int, NonFifoLabel]] = [(0.0, departure, 0, initial)]
    by_key: dict[tuple[Any, datetime], NonFifoLabel] = {initial.exact_key: initial}
    serial = 0
    expanded = 0
    generated = 0
    queue_peak = 1
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
                    errors,
                    "cancelled",
                )
            try:
                transition = evaluate_edge(label.node, neighbor, label.arrival_time)
                if not isinstance(transition, NonFifoTransition):
                    raise TypeError("non-FIFO evaluator must return NonFifoTransition")
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
    return _result(status, best_goal, by_key, expanded, generated, queue_peak, errors, reason)


def _result(
    status: NonFifoSearchStatus,
    label: NonFifoLabel | None,
    by_key: Mapping[tuple[Any, datetime], NonFifoLabel],
    expanded: int,
    generated: int,
    queue_peak: int,
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
        evaluator_errors=tuple(errors),
        reason=reason,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, NonFifoTransition):
        return {
            "arrival_time": value.arrival_time,
            "cost": value.cost,
            "payload": value.payload,
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "NonFifoEvaluationError",
    "NonFifoLabel",
    "NonFifoSearchResult",
    "NonFifoSearchStatus",
    "NonFifoTransition",
    "search_non_fifo",
]
