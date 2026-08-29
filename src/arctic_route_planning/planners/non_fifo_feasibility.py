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
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from heapq import heapify, heappop, heappush
from math import isfinite
from types import CodeType
from typing import Any

_INCUMBENT_BOUND_DISABLED_DIGEST = "non-fifo-pareto-incumbent-bound-disabled"
_PARETO_PRIORITY_DEFAULT_DIGEST = "non-fifo-pareto-priority-cost-vector-v1"
_PARETO_PRIORITY_AFTER_GOAL_NONE_DIGEST = "non-fifo-pareto-priority-after-goal-none"


def _digest(value: Any) -> str:
    """Return a deterministic digest for session/checkpoint evidence."""

    encoded = json.dumps(
        _jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class NonFifoSearchStatus(StrEnum):
    GOAL_FOUND = "GOAL_FOUND"
    EXHAUSTED = "EXHAUSTED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    CANCELLED = "CANCELLED"
    EVALUATOR_FAILURE = "EVALUATOR_FAILURE"


class NonFifoEvaluationError(RuntimeError):
    """A transition cannot be evaluated in the finite reference domain."""


class NonFifoEvaluationSkipped(NonFifoEvaluationError):
    """A known domain rejection means that one edge is unavailable.

    The actual temporal bridge uses this marker only for classified hard,
    coverage, speed, or ETA-domain rejections.  Unknown evaluator exceptions
    remain fatal evidence and are never converted to this marker.
    """


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


class NonFifoParetoIncumbentBoundStatus(StrEnum):
    """Fail-closed states for an explicit multi-objective lower-bound proof."""

    DISABLED = "DISABLED"
    CERTIFIED = "CERTIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class NonFifoParetoIncumbentBoundCertificate:
    """Proof-carrying lower bounds for safe incumbent Pareto pruning.

    Each ``state_lower_bounds[(node, exact_arrival)]`` entry also names the
    *exact* goal arrival that every certified suffix must produce.  A lower
    bound without that arrival proof would incorrectly compare different
    exact-arrival goal labels in a non-FIFO system, so it is deliberately not
    accepted here.  The certificate never deletes an expanded label and is
    only consulted before a newly generated label enters the session.
    """

    status: NonFifoParetoIncumbentBoundStatus
    scope_digest: str
    goal: Any
    objective_count: int
    state_lower_bounds: tuple[
        tuple[tuple[Any, datetime], tuple[datetime, tuple[float, ...]]], ...
    ]
    coverage_complete: bool
    evaluator_certified: bool
    proof_digest: str
    reason: str | None = None
    schema_version: str = "c.p0.2-nonfifo-pareto-incumbent-bound.v1"
    certificate_digest: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "c.p0.2-nonfifo-pareto-incumbent-bound.v1":
            raise ValueError("unsupported incumbent-bound certificate schema")
        if not isinstance(self.status, NonFifoParetoIncumbentBoundStatus):
            raise ValueError("incumbent-bound certificate status is invalid")
        if not isinstance(self.scope_digest, str) or not self.scope_digest:
            raise ValueError("incumbent-bound certificate scope is empty")
        if (
            isinstance(self.objective_count, bool)
            or not isinstance(self.objective_count, int)
            or self.objective_count < 1
        ):
            raise ValueError("incumbent-bound objective_count must be positive")
        if not isinstance(self.coverage_complete, bool) or not isinstance(
            self.evaluator_certified, bool
        ):
            raise ValueError("incumbent-bound coverage/evaluator flags are invalid")
        if not isinstance(self.proof_digest, str) or not self.proof_digest:
            raise ValueError("incumbent-bound proof digest is empty")
        normalized: list[
            tuple[tuple[Any, datetime], tuple[datetime, tuple[float, ...]]]
        ] = []
        seen: set[tuple[Any, datetime]] = set()
        for state, evidence in self.state_lower_bounds:
            if not isinstance(state, tuple) or len(state) != 2:
                raise ValueError("incumbent-bound state key is invalid")
            node, arrival = state
            if arrival.tzinfo is None or arrival.utcoffset() is None:
                raise ValueError("incumbent-bound state arrival must be timezone-aware")
            arrival = arrival.astimezone(UTC)
            state = (node, arrival)
            try:
                if state in seen:
                    raise ValueError("incumbent-bound state is duplicated")
                seen.add(state)
            except TypeError as error:
                raise ValueError("incumbent-bound state node is not hashable") from error
            if not isinstance(evidence, tuple) or len(evidence) != 2:
                raise ValueError("incumbent-bound evidence is invalid")
            goal_arrival, values = evidence
            if goal_arrival.tzinfo is None or goal_arrival.utcoffset() is None:
                raise ValueError("incumbent-bound goal arrival must be timezone-aware")
            goal_arrival = goal_arrival.astimezone(UTC)
            if goal_arrival < arrival:
                raise ValueError("incumbent-bound goal arrival precedes state arrival")
            vector = tuple(values)
            if len(vector) != self.objective_count or any(
                not isfinite(value) or value < 0.0 for value in vector
            ):
                raise ValueError("incumbent-bound lower bounds are invalid")
            normalized.append((state, (goal_arrival, vector)))
        normalized.sort(key=lambda item: _canonical_token(item[0]))
        object.__setattr__(self, "state_lower_bounds", tuple(normalized))
        expected = self._calculated_digest()
        if self.certificate_digest and self.certificate_digest != expected:
            raise ValueError("incumbent-bound certificate digest mismatch")
        object.__setattr__(self, "certificate_digest", expected)

    @classmethod
    def certified(
        cls,
        *,
        scope_digest: str,
        goal: Any,
        objective_count: int,
        state_lower_bounds: Mapping[
            tuple[Any, datetime], tuple[datetime, Iterable[float]]
        ],
        proof_digest: str,
    ) -> NonFifoParetoIncumbentBoundCertificate:
        """Build a usable certificate after validating all finite bounds."""

        return cls(
            status=NonFifoParetoIncumbentBoundStatus.CERTIFIED,
            scope_digest=scope_digest,
            goal=goal,
            objective_count=objective_count,
            state_lower_bounds=tuple(state_lower_bounds.items()),
            coverage_complete=True,
            evaluator_certified=True,
            proof_digest=proof_digest,
        )

    @classmethod
    def rejected(
        cls,
        *,
        scope_digest: str,
        goal: Any,
        objective_count: int,
        reason: str,
        proof_digest: str = "rejected-proof",
        state_lower_bounds: Mapping[
            tuple[Any, datetime], tuple[datetime, Iterable[float]]
        ] | None = None,
        coverage_complete: bool = False,
        evaluator_certified: bool = False,
    ) -> NonFifoParetoIncumbentBoundCertificate:
        """Build an auditable non-authorizing certificate."""

        return cls(
            status=NonFifoParetoIncumbentBoundStatus.REJECTED,
            scope_digest=scope_digest,
            goal=goal,
            objective_count=objective_count,
            state_lower_bounds=tuple(
                (state, (goal_arrival, tuple(values)))
                for state, (goal_arrival, values) in (state_lower_bounds or {}).items()
            ),
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            proof_digest=proof_digest,
            reason=reason,
        )

    @property
    def digest(self) -> str:
        return self.certificate_digest

    @property
    def usable(self) -> bool:
        return (
            self.status is NonFifoParetoIncumbentBoundStatus.CERTIFIED
            and self.coverage_complete
            and self.evaluator_certified
            and self.reason is None
        )

    def permits(self, *, scope_digest: str, goal: Any, objective_count: int) -> bool:
        return (
            self.usable
            and self.scope_digest == scope_digest
            and self.scope_digest != "unspecified-scope"
            and scope_digest != "unspecified-scope"
            and self.goal == goal
            and self.objective_count == objective_count
        )

    def lower_bound(
        self, node: Any, arrival_time: datetime
    ) -> tuple[datetime, tuple[float, ...]] | None:
        if arrival_time.tzinfo is None or arrival_time.utcoffset() is None:
            raise ValueError("incumbent-bound lookup arrival must be timezone-aware")
        key = (node, arrival_time.astimezone(UTC))
        for candidate, evidence in self.state_lower_bounds:
            if candidate == key:
                return evidence
        return None

    def _calculated_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "scope_digest": self.scope_digest,
                "goal": self.goal,
                "objective_count": self.objective_count,
                "state_lower_bounds": self.state_lower_bounds,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "proof_digest": self.proof_digest,
                "reason": self.reason,
            }
        )


class NonFifoParetoTerminalBoundStatus(StrEnum):
    """Fail-closed states for selected-route terminal bounds."""

    DISABLED = "DISABLED"
    CERTIFIED = "CERTIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class NonFifoParetoTerminalBoundCertificate:
    """A lower-bound proof for selected-route-only terminal pruning.

    Unlike :class:`NonFifoParetoIncumbentBoundCertificate`, this certificate
    does not claim that a suffix has one exact goal arrival.  It is therefore
    usable only by the explicit selected-route mode, where the goal is a
    terminal state and the objective vector is compared lexicographically.
    The full exact-arrival Pareto frontier is intentionally not certifiable
    from this evidence.
    """

    status: NonFifoParetoTerminalBoundStatus
    scope_digest: str
    goal: Any
    objective_count: int
    node_lower_bounds: tuple[tuple[Any, tuple[float, ...]], ...]
    coverage_complete: bool
    evaluator_certified: bool
    proof_digest: str
    reason: str | None = None
    selection_only: bool = True
    schema_version: str = "c.p0.2-nonfifo-pareto-terminal-bound.v1"
    certificate_digest: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "c.p0.2-nonfifo-pareto-terminal-bound.v1":
            raise ValueError("unsupported terminal-bound certificate schema")
        if not isinstance(self.status, NonFifoParetoTerminalBoundStatus):
            raise ValueError("terminal-bound certificate status is invalid")
        if not isinstance(self.scope_digest, str) or not self.scope_digest:
            raise ValueError("terminal-bound certificate scope is empty")
        if (
            isinstance(self.objective_count, bool)
            or not isinstance(self.objective_count, int)
            or self.objective_count < 1
        ):
            raise ValueError("terminal-bound objective_count must be positive")
        if not isinstance(self.coverage_complete, bool) or not isinstance(
            self.evaluator_certified, bool
        ):
            raise ValueError("terminal-bound coverage/evaluator flags are invalid")
        if not isinstance(self.selection_only, bool) or not self.selection_only:
            raise ValueError("terminal-bound certificate must be selection-only")
        if not isinstance(self.proof_digest, str) or not self.proof_digest:
            raise ValueError("terminal-bound proof digest is empty")
        normalized: list[tuple[Any, tuple[float, ...]]] = []
        seen: set[Any] = set()
        for node, values in self.node_lower_bounds:
            try:
                if node in seen:
                    raise ValueError("terminal-bound node is duplicated")
                seen.add(node)
            except TypeError as error:
                raise ValueError("terminal-bound node is not hashable") from error
            vector = tuple(values)
            if len(vector) != self.objective_count or any(
                not isfinite(value) or value < 0.0 for value in vector
            ):
                raise ValueError("terminal-bound lower bounds are invalid")
            normalized.append((node, vector))
        normalized.sort(key=lambda item: _canonical_token(item[0]))
        object.__setattr__(self, "node_lower_bounds", tuple(normalized))
        expected = self._calculated_digest()
        if self.certificate_digest and self.certificate_digest != expected:
            raise ValueError("terminal-bound certificate digest mismatch")
        object.__setattr__(self, "certificate_digest", expected)

    @classmethod
    def certified(
        cls,
        *,
        scope_digest: str,
        goal: Any,
        objective_count: int,
        node_lower_bounds: Mapping[Any, Iterable[float]],
        proof_digest: str,
    ) -> NonFifoParetoTerminalBoundCertificate:
        return cls(
            status=NonFifoParetoTerminalBoundStatus.CERTIFIED,
            scope_digest=scope_digest,
            goal=goal,
            objective_count=objective_count,
            node_lower_bounds=tuple(node_lower_bounds.items()),
            coverage_complete=True,
            evaluator_certified=True,
            proof_digest=proof_digest,
        )

    @classmethod
    def rejected(
        cls,
        *,
        scope_digest: str,
        goal: Any,
        objective_count: int,
        reason: str,
        proof_digest: str = "rejected-proof",
        node_lower_bounds: Mapping[Any, Iterable[float]] | None = None,
        coverage_complete: bool = False,
        evaluator_certified: bool = False,
    ) -> NonFifoParetoTerminalBoundCertificate:
        return cls(
            status=NonFifoParetoTerminalBoundStatus.REJECTED,
            scope_digest=scope_digest,
            goal=goal,
            objective_count=objective_count,
            node_lower_bounds=tuple(
                (node, tuple(values))
                for node, values in (node_lower_bounds or {}).items()
            ),
            coverage_complete=coverage_complete,
            evaluator_certified=evaluator_certified,
            proof_digest=proof_digest,
            reason=reason,
        )

    @property
    def digest(self) -> str:
        return self.certificate_digest

    @property
    def usable(self) -> bool:
        return (
            self.status is NonFifoParetoTerminalBoundStatus.CERTIFIED
            and self.coverage_complete
            and self.evaluator_certified
            and self.reason is None
            and self.selection_only
            and bool(self.node_lower_bounds)
        )

    def permits(self, *, scope_digest: str, goal: Any, objective_count: int) -> bool:
        return (
            self.usable
            and self.scope_digest == scope_digest
            and self.scope_digest != "unspecified-scope"
            and scope_digest != "unspecified-scope"
            and self.goal == goal
            and self.objective_count == objective_count
        )

    def lower_bound(self, node: Any) -> tuple[float, ...] | None:
        for candidate, values in self.node_lower_bounds:
            if candidate == node:
                return values
        return None

    def _calculated_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "scope_digest": self.scope_digest,
                "goal": self.goal,
                "objective_count": self.objective_count,
                "node_lower_bounds": self.node_lower_bounds,
                "coverage_complete": self.coverage_complete,
                "evaluator_certified": self.evaluator_certified,
                "proof_digest": self.proof_digest,
                "reason": self.reason,
                "selection_only": self.selection_only,
            }
        )


NonFifoParetoBoundCertificate = (
    NonFifoParetoIncumbentBoundCertificate | NonFifoParetoTerminalBoundCertificate
)


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
    goal_node: Any | None = None
    pareto_pruning: bool = False
    search_limits: tuple[int, int, int, int] = ()
    incumbent_bound_digest: str = _INCUMBENT_BOUND_DISABLED_DIGEST
    incumbent_bound_pruned: int = 0
    incumbent_bound_rejected: int = 0
    incumbent_bound_rejection_reasons: tuple[tuple[str, int], ...] = ()
    priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST
    # A terminal-bound search deliberately proves only the selected route,
    # not the complete exact-arrival goal frontier.  Keep this explicit in
    # the result so callers cannot accidentally promote it to a frontier
    # certificate.
    frontier_complete: bool = True
    selection_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.frontier_complete, bool) or not isinstance(
            self.selection_only, bool
        ):
            raise ValueError("Pareto result frontier policy flags are invalid")
        if self.selection_only and self.frontier_complete:
            raise ValueError("selection-only result cannot claim a complete frontier")

    @property
    def goal_labels(self) -> tuple[NonFifoParetoLabel, ...]:
        """All retained goal labels, ordered by objective vector."""

        if self.status is not NonFifoSearchStatus.GOAL_FOUND or self.label is None:
            return ()
        goal = self.goal_node if self.goal_node is not None else self.label.node
        return tuple(label for label in self.labels if label.node == goal)

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

    @property
    def frontier_digest(self) -> str:
        """Digest the complete successful frontier and its search policy.

        The digest is deliberately distinct from ``semantic_digest``: it binds
        every non-dominated goal label, exact arrival, transition evidence and
        the explicit Pareto policy/limits used to produce the result.  Failed
        or cancelled searches therefore produce an evidence digest with an
        empty frontier and can never be mistaken for a successful route.
        """

        frontier = tuple(
            sorted(
                (_pareto_label_payload(label) for label in self.goal_frontier),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
        )
        payload = {
            "schema_version": "c.p0.2-nonfifo-pareto-frontier.v1",
            "status": self.status.value,
            "reason": self.reason,
            "pareto_pruning": self.pareto_pruning,
            "incumbent_bound_digest": self.incumbent_bound_digest,
            "priority_policy_digest": self.priority_policy_digest,
            "frontier_complete": self.frontier_complete,
            "selection_only": self.selection_only,
            "search_limits": self.search_limits,
            "frontier": frontier,
        }
        return hashlib.sha256(
            json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def pareto_frontier_digest(self) -> str:
        """Compatibility alias for callers using the longer evidence name."""

        return self.frontier_digest


class NonFifoFrontierCertificateError(ValueError):
    """A finite Pareto frontier cannot be certified under the research fence."""


class NonFifoFrontierComparisonStatus(StrEnum):
    """Fail-closed outcomes for comparing two exact-arrival frontiers."""

    MATCH = "MATCH"
    INCOMPLETE = "INCOMPLETE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    FRONTIER_MISMATCH = "FRONTIER_MISMATCH"


def _frontier_label_token(label: NonFifoParetoLabel) -> str:
    """Return the canonical token for one complete goal-frontier label."""

    return json.dumps(
        _pareto_label_payload(label),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_frontier_digest(labels: Iterable[NonFifoParetoLabel]) -> str:
    """Digest the complete frontier without embedding search-policy metadata."""

    tokens = tuple(sorted(_frontier_label_token(label) for label in labels))
    return _digest(
        {
            "schema_version": "c.p0.2-nonfifo-frontier-labels.v1",
            "labels": tokens,
        }
    )


@dataclass(frozen=True, slots=True)
class NonFifoParetoFrontierCertificate:
    """Certificate that one finite Pareto result has a complete goal frontier.

    The certificate is intentionally stricter than a selected-route result.  A
    resource-limited, cancelled, evaluator-failed, or otherwise incomplete
    search is never usable, even if it happened to discover a goal label.  The
    callback-specific session digest remains bound through
    ``session_identity_digest``; ``comparison_identity_digest`` is the shared
    input/config fence used when independently implemented searches are
    compared.
    """

    session_identity_digest: str
    comparison_identity_digest: str
    scope_digest: str
    policy_digest: str
    status: NonFifoSearchStatus
    pareto_pruning: bool
    search_limits: tuple[int, int, int, int]
    result_frontier_digest: str
    frontier_digest: str
    frontier_count: int
    goal_label_count: int
    complete: bool
    rejection_reason: str | None = None
    schema_version: str = "c.p0.2-nonfifo-frontier-certificate.v1"
    certificate_digest: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "c.p0.2-nonfifo-frontier-certificate.v1":
            raise NonFifoFrontierCertificateError("unsupported frontier certificate schema")
        for name in (
            "session_identity_digest",
            "comparison_identity_digest",
            "scope_digest",
            "policy_digest",
            "result_frontier_digest",
            "frontier_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise NonFifoFrontierCertificateError(f"frontier certificate {name} is empty")
        if not isinstance(self.status, NonFifoSearchStatus):
            raise NonFifoFrontierCertificateError("frontier certificate status is invalid")
        if not isinstance(self.pareto_pruning, bool) or not isinstance(self.complete, bool):
            raise NonFifoFrontierCertificateError("frontier certificate flags are invalid")
        if (
            isinstance(self.frontier_count, bool)
            or not isinstance(self.frontier_count, int)
            or self.frontier_count < 0
            or isinstance(self.goal_label_count, bool)
            or not isinstance(self.goal_label_count, int)
            or self.goal_label_count < 0
        ):
            raise NonFifoFrontierCertificateError("frontier certificate counts are invalid")
        limits = tuple(self.search_limits)
        if len(limits) != 4 or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in limits
        ):
            raise NonFifoFrontierCertificateError("frontier certificate limits are invalid")
        object.__setattr__(self, "search_limits", limits)
        if self.complete and (
            self.status is not NonFifoSearchStatus.GOAL_FOUND
            or self.frontier_count < 1
            or self.goal_label_count < self.frontier_count
            or self.rejection_reason is not None
        ):
            raise NonFifoFrontierCertificateError(
                "complete frontier certificate has an invalid success state"
            )
        expected = self._calculated_digest()
        if self.certificate_digest and self.certificate_digest != expected:
            raise NonFifoFrontierCertificateError("frontier certificate digest mismatch")
        object.__setattr__(self, "certificate_digest", expected)

    def _calculated_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "session_identity_digest": self.session_identity_digest,
                "comparison_identity_digest": self.comparison_identity_digest,
                "scope_digest": self.scope_digest,
                "policy_digest": self.policy_digest,
                "status": self.status,
                "pareto_pruning": self.pareto_pruning,
                "search_limits": self.search_limits,
                "result_frontier_digest": self.result_frontier_digest,
                "frontier_digest": self.frontier_digest,
                "frontier_count": self.frontier_count,
                "goal_label_count": self.goal_label_count,
                "complete": self.complete,
                "rejection_reason": self.rejection_reason,
            }
        )

    @property
    def digest(self) -> str:
        return self.certificate_digest

    @property
    def usable(self) -> bool:
        """Whether this certificate may authorize a frontier comparison."""

        return self.complete

    def assert_usable(self) -> None:
        if not self.usable:
            raise NonFifoFrontierCertificateError(
                self.rejection_reason or "frontier certificate is incomplete"
            )

    @classmethod
    def from_result(
        cls,
        result: NonFifoParetoSearchResult,
        *,
        identity: NonFifoParetoSessionIdentity,
        scope_digest: str,
    ) -> NonFifoParetoFrontierCertificate:
        """Build a certificate and retain an explicit reason when unusable."""

        if not isinstance(result, NonFifoParetoSearchResult):
            raise NonFifoFrontierCertificateError("frontier result type is invalid")
        if not isinstance(identity, NonFifoParetoSessionIdentity):
            raise NonFifoFrontierCertificateError("frontier identity type is invalid")
        identity.assert_valid()
        if not isinstance(scope_digest, str) or not scope_digest:
            raise NonFifoFrontierCertificateError("frontier scope digest is empty")
        frontier = result.goal_frontier
        expected_limits = (
            identity.max_expansions,
            identity.max_labels,
            identity.max_queue,
            identity.max_edge_evaluations,
        )
        reasons: list[str] = []
        if result.pareto_pruning is not identity.pareto_pruning:
            reasons.append("policy_mismatch")
        if tuple(result.search_limits) != expected_limits:
            reasons.append("search_limits_mismatch")
        if result.status is not NonFifoSearchStatus.GOAL_FOUND:
            reasons.append(f"status:{result.status.value}")
        if result.label is None:
            reasons.append("selected_label_missing")
        if result.reason is not None:
            reasons.append(f"reason:{result.reason}")
        if not result.frontier_complete or result.selection_only:
            reasons.append("frontier_incomplete_by_policy")
        if result.evaluator_errors:
            reasons.append("evaluator_errors")
        if result.goal_node is not None and result.goal_node != identity.goal:
            reasons.append("goal_node_mismatch")
        if result.label is not None and result.label.node != identity.goal:
            reasons.append("selected_goal_mismatch")
        if not frontier:
            reasons.append("frontier_empty")
        complete = not reasons
        return cls(
            session_identity_digest=identity.digest,
            comparison_identity_digest=identity.comparison_digest,
            scope_digest=scope_digest,
            policy_digest=identity.policy_digest,
            status=result.status,
            pareto_pruning=identity.pareto_pruning,
            search_limits=expected_limits,
            result_frontier_digest=result.frontier_digest,
            frontier_digest=_canonical_frontier_digest(frontier),
            frontier_count=len(frontier),
            goal_label_count=len(result.goal_labels),
            complete=complete,
            rejection_reason=None if complete else ";".join(reasons),
        )


@dataclass(frozen=True, slots=True)
class NonFifoParetoFrontierComparison:
    """Auditable comparison of two independently produced complete frontiers."""

    status: NonFifoFrontierComparisonStatus
    matched: bool
    candidate_certificate_digest: str
    reference_certificate_digest: str
    candidate_frontier_digest: str
    reference_frontier_digest: str
    missing_label_digests: tuple[str, ...] = ()
    unexpected_label_digests: tuple[str, ...] = ()
    reason: str | None = None
    comparison_digest: str = ""
    schema_version: str = "c.p0.2-nonfifo-frontier-comparison.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "c.p0.2-nonfifo-frontier-comparison.v1":
            raise NonFifoFrontierCertificateError("unsupported frontier comparison schema")
        for name in (
            "candidate_certificate_digest",
            "reference_certificate_digest",
            "candidate_frontier_digest",
            "reference_frontier_digest",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise NonFifoFrontierCertificateError(f"frontier comparison {name} is empty")
        if not isinstance(self.status, NonFifoFrontierComparisonStatus):
            raise NonFifoFrontierCertificateError("frontier comparison status is invalid")
        if not isinstance(self.matched, bool):
            raise NonFifoFrontierCertificateError("frontier comparison matched flag is invalid")
        object.__setattr__(self, "missing_label_digests", tuple(self.missing_label_digests))
        object.__setattr__(self, "unexpected_label_digests", tuple(self.unexpected_label_digests))
        if (self.status is NonFifoFrontierComparisonStatus.MATCH) != self.matched:
            raise NonFifoFrontierCertificateError(
                "frontier comparison status and matched flag disagree"
            )
        if self.matched and (
            self.missing_label_digests
            or self.unexpected_label_digests
            or self.reason is not None
        ):
            raise NonFifoFrontierCertificateError(
                "matched frontier comparison contains rejection evidence"
            )
        expected = self._calculated_digest()
        if self.comparison_digest and self.comparison_digest != expected:
            raise NonFifoFrontierCertificateError("frontier comparison digest mismatch")
        object.__setattr__(self, "comparison_digest", expected)

    def _calculated_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "matched": self.matched,
                "candidate_certificate_digest": self.candidate_certificate_digest,
                "reference_certificate_digest": self.reference_certificate_digest,
                "candidate_frontier_digest": self.candidate_frontier_digest,
                "reference_frontier_digest": self.reference_frontier_digest,
                "missing_label_digests": self.missing_label_digests,
                "unexpected_label_digests": self.unexpected_label_digests,
                "reason": self.reason,
            }
        )

    @property
    def digest(self) -> str:
        return self.comparison_digest


def certify_non_fifo_pareto_frontier(
    result: NonFifoParetoSearchResult,
    *,
    identity: NonFifoParetoSessionIdentity,
    scope_digest: str,
) -> NonFifoParetoFrontierCertificate:
    """Create a scope-bound complete-frontier certificate for a result."""

    return NonFifoParetoFrontierCertificate.from_result(
        result, identity=identity, scope_digest=scope_digest
    )


def compare_non_fifo_pareto_frontiers(
    candidate: NonFifoParetoSearchResult,
    reference: NonFifoParetoSearchResult,
    *,
    candidate_identity: NonFifoParetoSessionIdentity,
    reference_identity: NonFifoParetoSessionIdentity,
    candidate_scope_digest: str,
    reference_scope_digest: str,
) -> NonFifoParetoFrontierComparison:
    """Compare complete exact-arrival frontiers with fail-closed identity fences.

    Independent implementations normally have different callback bytecode, so
    the comparison uses each session's shared ``comparison_digest`` plus the
    explicit scope digest, while retaining full callback-bound certificate
    digests in the returned evidence.  It never compares only a selected
    route, and it never applies a numeric tolerance to exact arrival or costs.
    """

    candidate_certificate = NonFifoParetoFrontierCertificate.from_result(
        candidate, identity=candidate_identity, scope_digest=candidate_scope_digest
    )
    reference_certificate = NonFifoParetoFrontierCertificate.from_result(
        reference, identity=reference_identity, scope_digest=reference_scope_digest
    )
    common_identity = (
        candidate_identity.comparison_digest == reference_identity.comparison_digest
        and candidate_scope_digest == reference_scope_digest
    )
    if not common_identity:
        status = NonFifoFrontierComparisonStatus.IDENTITY_MISMATCH
        reason = "comparison identity or scope mismatch"
        return NonFifoParetoFrontierComparison(
            status=status,
            matched=False,
            candidate_certificate_digest=candidate_certificate.digest,
            reference_certificate_digest=reference_certificate.digest,
            candidate_frontier_digest=candidate_certificate.frontier_digest,
            reference_frontier_digest=reference_certificate.frontier_digest,
            reason=reason,
        )
    if not candidate_certificate.usable or not reference_certificate.usable:
        status = NonFifoFrontierComparisonStatus.INCOMPLETE
        reason = "; ".join(
            value
            for value in (
                f"candidate:{candidate_certificate.rejection_reason}"
                if not candidate_certificate.usable
                else None,
                f"reference:{reference_certificate.rejection_reason}"
                if not reference_certificate.usable
                else None,
            )
            if value
        )
        return NonFifoParetoFrontierComparison(
            status=status,
            matched=False,
            candidate_certificate_digest=candidate_certificate.digest,
            reference_certificate_digest=reference_certificate.digest,
            candidate_frontier_digest=candidate_certificate.frontier_digest,
            reference_frontier_digest=reference_certificate.frontier_digest,
            reason=reason or "frontier incomplete",
        )

    candidate_tokens = sorted(_frontier_label_token(label) for label in candidate.goal_frontier)
    reference_tokens = sorted(_frontier_label_token(label) for label in reference.goal_frontier)
    candidate_counts = Counter(candidate_tokens)
    reference_counts = Counter(reference_tokens)
    missing_tokens = sorted((reference_counts - candidate_counts).elements())
    unexpected_tokens = sorted((candidate_counts - reference_counts).elements())
    missing = tuple(_digest(token) for token in missing_tokens)
    unexpected = tuple(_digest(token) for token in unexpected_tokens)
    matched = not missing and not unexpected
    status = (
        NonFifoFrontierComparisonStatus.MATCH
        if matched
        else NonFifoFrontierComparisonStatus.FRONTIER_MISMATCH
    )
    return NonFifoParetoFrontierComparison(
        status=status,
        matched=matched,
        candidate_certificate_digest=candidate_certificate.digest,
        reference_certificate_digest=reference_certificate.digest,
        candidate_frontier_digest=candidate_certificate.frontier_digest,
        reference_frontier_digest=reference_certificate.frontier_digest,
        missing_label_digests=missing,
        unexpected_label_digests=unexpected,
        reason=None if matched else "complete frontier labels differ",
    )


class NonFifoParetoSessionState(StrEnum):
    """Lifecycle states for the finite Pareto research session."""

    READY = "READY"
    PAUSED = "PAUSED"
    GOAL_FOUND = "GOAL_FOUND"
    EXHAUSTED = "EXHAUSTED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    CANCELLED = "CANCELLED"
    EVALUATOR_FAILURE = "EVALUATOR_FAILURE"


class NonFifoParetoSessionError(RuntimeError):
    """Base error for session lifecycle and checkpoint fences."""


class NonFifoParetoSessionIdentityMismatch(NonFifoParetoSessionError):
    """The supplied callbacks or policy do not match a session identity."""


class NonFifoParetoSessionRestoreError(NonFifoParetoSessionError):
    """A checkpoint is not restorable in its current lifecycle state."""


@dataclass(frozen=True, slots=True)
class NonFifoParetoSessionIdentity:
    """Complete identity fence for one finite non-FIFO Pareto session."""

    start: Any
    goal: Any
    departure_time: datetime
    objective_count: int
    pareto_pruning: bool
    max_expansions: int
    max_labels: int
    max_queue: int
    max_edge_evaluations: int
    maximum_elapsed_seconds: float | None
    neighbor_digest: str
    evaluator_digest: str
    fixture_digest: str
    config_digest: str = "unspecified-config"
    scope_digest: str = "unspecified-scope"
    incumbent_bound_digest: str = _INCUMBENT_BOUND_DISABLED_DIGEST
    priority_callback_digest: str = "non-fifo-pareto-priority-none"
    priority_after_goal_callback_digest: str = _PARETO_PRIORITY_AFTER_GOAL_NONE_DIGEST
    priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST
    schema_version: str = "c.p0.2-nonfifo-pareto-session.v1"

    @classmethod
    def from_callbacks(
        cls,
        *,
        start: Any,
        goal: Any,
        departure_time: datetime,
        objective_count: int,
        pareto_pruning: bool,
        max_expansions: int,
        max_labels: int,
        max_queue: int,
        max_edge_evaluations: int,
        maximum_elapsed: timedelta | None,
        neighbors: Callable[[Any], Iterable[Any]],
        evaluate_edge: Callable[
            [Any, Any, datetime], NonFifoTransition | NonFifoParetoTransition
        ],
        fixture_digest: str,
        config_digest: str = "unspecified-config",
        scope_digest: str = "unspecified-scope",
        incumbent_bound_digest: str = _INCUMBENT_BOUND_DISABLED_DIGEST,
        priority: Callable[[NonFifoParetoLabel], float] | None = None,
        priority_after_goal: Callable[[NonFifoParetoLabel], float] | None = None,
        priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST,
    ) -> NonFifoParetoSessionIdentity:
        return cls(
            start=start,
            goal=goal,
            departure_time=departure_time,
            objective_count=objective_count,
            pareto_pruning=pareto_pruning,
            max_expansions=max_expansions,
            max_labels=max_labels,
            max_queue=max_queue,
            max_edge_evaluations=max_edge_evaluations,
            maximum_elapsed_seconds=(
                maximum_elapsed.total_seconds() if maximum_elapsed is not None else None
            ),
            neighbor_digest=_callback_digest(neighbors),
            evaluator_digest=_callback_digest(evaluate_edge),
            fixture_digest=fixture_digest,
            config_digest=config_digest,
            scope_digest=scope_digest,
            incumbent_bound_digest=incumbent_bound_digest,
            priority_callback_digest=_priority_callback_digest(priority),
            priority_after_goal_callback_digest=_priority_after_goal_callback_digest(
                priority_after_goal
            ),
            priority_policy_digest=priority_policy_digest,
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "start": self.start,
                "goal": self.goal,
                "departure_time": self.departure_time,
                "objective_count": self.objective_count,
                "pareto_pruning": self.pareto_pruning,
                "max_expansions": self.max_expansions,
                "max_labels": self.max_labels,
                "max_queue": self.max_queue,
                "max_edge_evaluations": self.max_edge_evaluations,
                "maximum_elapsed_seconds": self.maximum_elapsed_seconds,
                "neighbor_digest": self.neighbor_digest,
                "evaluator_digest": self.evaluator_digest,
                "fixture_digest": self.fixture_digest,
                "config_digest": self.config_digest,
                "scope_digest": self.scope_digest,
                "incumbent_bound_digest": self.incumbent_bound_digest,
                "priority_callback_digest": self.priority_callback_digest,
                "priority_after_goal_callback_digest": self.priority_after_goal_callback_digest,
                "priority_policy_digest": self.priority_policy_digest,
            }
        )

    @property
    def policy_digest(self) -> str:
        return _digest(
            {
                "objective_count": self.objective_count,
                "pareto_pruning": self.pareto_pruning,
                "max_expansions": self.max_expansions,
                "max_labels": self.max_labels,
                "max_queue": self.max_queue,
                "max_edge_evaluations": self.max_edge_evaluations,
                "maximum_elapsed_seconds": self.maximum_elapsed_seconds,
                "scope_digest": self.scope_digest,
                "incumbent_bound_digest": self.incumbent_bound_digest,
                "priority_callback_digest": self.priority_callback_digest,
                "priority_after_goal_callback_digest": self.priority_after_goal_callback_digest,
                "priority_policy_digest": self.priority_policy_digest,
            }
        )

    @property
    def comparison_digest(self) -> str:
        """Shared input/config fence for independently implemented searches.

        ``digest`` intentionally includes callback bytecode so a checkpoint
        cannot silently restore with another evaluator.  An independent
        reference has different callback code by design, therefore frontier
        comparison uses this second digest for the common request, fixture,
        configuration and frozen limits while retaining each full identity in
        its certificate.
        """

        return _digest(
            {
                "schema_version": self.schema_version,
                "start": self.start,
                "goal": self.goal,
                "departure_time": self.departure_time,
                "objective_count": self.objective_count,
                "max_expansions": self.max_expansions,
                "max_labels": self.max_labels,
                "max_queue": self.max_queue,
                "max_edge_evaluations": self.max_edge_evaluations,
                "maximum_elapsed_seconds": self.maximum_elapsed_seconds,
                "fixture_digest": self.fixture_digest,
                "config_digest": self.config_digest,
                "scope_digest": self.scope_digest,
                "incumbent_bound_digest": self.incumbent_bound_digest,
            }
        )

    def assert_valid(self) -> None:
        if self.schema_version != "c.p0.2-nonfifo-pareto-session.v1":
            raise NonFifoParetoSessionIdentityMismatch("unsupported session schema")
        if self.departure_time.tzinfo is None or self.departure_time.utcoffset() is None:
            raise NonFifoParetoSessionIdentityMismatch("departure_time must be timezone-aware")
        if (
            isinstance(self.objective_count, bool)
            or not isinstance(self.objective_count, int)
            or self.objective_count < 1
        ):
            raise NonFifoParetoSessionIdentityMismatch("objective_count must be positive")
        if not isinstance(self.pareto_pruning, bool):
            raise NonFifoParetoSessionIdentityMismatch("pareto_pruning must be boolean")
        limits = (
            self.max_expansions,
            self.max_labels,
            self.max_queue,
            self.max_edge_evaluations,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in limits
        ):
            raise NonFifoParetoSessionIdentityMismatch(
                "session limits must be positive integers"
            )
        if self.maximum_elapsed_seconds is not None and self.maximum_elapsed_seconds <= 0:
            raise NonFifoParetoSessionIdentityMismatch("maximum_elapsed must be positive")
        if any(
            not isinstance(value, str) or not value
            for value in (
                self.neighbor_digest,
                self.evaluator_digest,
                self.fixture_digest,
                self.config_digest,
                self.scope_digest,
                self.incumbent_bound_digest,
                self.priority_callback_digest,
                self.priority_after_goal_callback_digest,
                self.priority_policy_digest,
            )
        ):
            raise NonFifoParetoSessionIdentityMismatch("session identity digests are incomplete")


@dataclass(frozen=True, slots=True)
class NonFifoParetoCheckpoint:
    """Immutable in-process snapshot for a READY/PAUSED Pareto session."""

    identity: NonFifoParetoSessionIdentity
    state: NonFifoParetoSessionState
    labels: tuple[NonFifoParetoLabel, ...]
    goals: tuple[NonFifoParetoLabel, ...]
    queue: tuple[tuple[tuple[float, ...], datetime, int, NonFifoParetoLabel], ...]
    serial: int
    expanded: int
    generated: int
    queue_peak: int
    edge_evaluations: int
    total_labels: int
    pareto_pruned: int
    evaluator_errors: tuple[str, ...] = ()
    incumbent_bound_pruned: int = 0
    incumbent_bound_rejected: int = 0
    incumbent_bound_rejection_reasons: tuple[tuple[str, int], ...] = ()
    priority_phase: str = "pre_goal"
    state_digest: str = ""

    def __post_init__(self) -> None:
        self.identity.assert_valid()
        if self.state not in (
            NonFifoParetoSessionState.READY,
            NonFifoParetoSessionState.PAUSED,
        ):
            raise NonFifoParetoSessionRestoreError(
                "only READY or PAUSED sessions can be checkpointed"
            )
        if self.priority_phase not in {"pre_goal", "post_goal"}:
            raise NonFifoParetoSessionRestoreError("checkpoint priority phase is invalid")
        post_goal_callback_installed = (
            self.identity.priority_after_goal_callback_digest
            != _PARETO_PRIORITY_AFTER_GOAL_NONE_DIGEST
        )
        if self.priority_phase == "post_goal" and not post_goal_callback_installed:
            raise NonFifoParetoSessionRestoreError(
                "post-goal phase requires a post-goal priority callback"
            )
        if self.priority_phase == "post_goal" and not self.goals:
            raise NonFifoParetoSessionRestoreError(
                "post-goal checkpoint is missing its goal evidence"
            )
        if self.priority_phase == "pre_goal" and self.goals and post_goal_callback_installed:
            raise NonFifoParetoSessionRestoreError(
                "pre-goal checkpoint already contains goal evidence"
            )
        expected = self._calculated_state_digest()
        if self.state_digest and self.state_digest != expected:
            raise NonFifoParetoSessionRestoreError("checkpoint state digest mismatch")
        for name in ("incumbent_bound_pruned", "incumbent_bound_rejected"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise NonFifoParetoSessionRestoreError(f"checkpoint {name} is invalid")
        reasons = tuple(self.incumbent_bound_rejection_reasons)
        if any(
            not isinstance(item, (tuple, list))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or item[1] < 0
            for item in reasons
        ):
            raise NonFifoParetoSessionRestoreError(
                "checkpoint incumbent-bound rejection reasons are invalid"
            )
        object.__setattr__(self, "incumbent_bound_rejection_reasons", reasons)
        object.__setattr__(self, "state_digest", expected)

    def _calculated_state_digest(self) -> str:
        return _digest(
            {
                "identity": self.identity.digest,
                "state": self.state,
                "labels": tuple(_pareto_label_payload(label) for label in self.labels),
                "goals": tuple(_pareto_label_payload(label) for label in self.goals),
                "queue": tuple(
                    (
                        costs,
                        arrival,
                        serial,
                        _pareto_label_payload(label),
                    )
                    for costs, arrival, serial, label in self.queue
                ),
                "serial": self.serial,
                "expanded": self.expanded,
                "generated": self.generated,
                "queue_peak": self.queue_peak,
                "edge_evaluations": self.edge_evaluations,
                "total_labels": self.total_labels,
                "pareto_pruned": self.pareto_pruned,
                "evaluator_errors": self.evaluator_errors,
                "incumbent_bound_pruned": self.incumbent_bound_pruned,
                "incumbent_bound_rejected": self.incumbent_bound_rejected,
                "incumbent_bound_rejection_reasons": self.incumbent_bound_rejection_reasons,
                "priority_phase": self.priority_phase,
            }
        )

    def assert_valid(self) -> None:
        if self.state_digest != self._calculated_state_digest():
            raise NonFifoParetoSessionRestoreError("checkpoint state digest mismatch")

    @property
    def digest(self) -> str:
        return self.state_digest


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
    if maximum_elapsed is not None and maximum_elapsed <= timedelta(0):
        raise ValueError("maximum_elapsed must be positive")
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
        try:
            ordered_neighbors = _ordered_neighbors(neighbors(label.node))
        except Exception as error:
            errors.append(f"{type(error).__name__}:{error}")
            continue
        for neighbor in ordered_neighbors:
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


def _legacy_search_non_fifo_pareto(
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
    search_limits = (max_expansions, max_labels, max_queue, max_edge_evaluations)

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
                goal_node=goal,
                pareto_pruning=pareto_pruning,
                search_limits=search_limits,
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
            neighbours = _ordered_neighbors(neighbors(label.node))
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
                    goal_node=goal,
                    pareto_pruning=pareto_pruning,
                    search_limits=search_limits,
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
            except NonFifoEvaluationSkipped:
                # A caller may classify one unavailable edge (for example a
                # hard mask or incomplete risk coverage) without turning the
                # entire finite search into an evaluator failure.  Unknown
                # exceptions still enter ``errors`` below and remain fatal.
                continue
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
        goal_node=goal,
        pareto_pruning=pareto_pruning,
        search_limits=search_limits,
    )


def _callback_digest(callback: Any) -> str:
    """Fingerprint a finite-session callback without using process identity."""

    explicit = getattr(callback, "__non_fifo_identity__", None)
    if isinstance(explicit, str) and explicit.strip():
        return f"explicit:{explicit.strip()}"
    function = getattr(callback, "__func__", callback)
    code = getattr(function, "__code__", None)
    if code is None:
        return f"type:{type(callback).__module__}.{type(callback).__qualname__}"
    closure = []
    for cell in getattr(function, "__closure__", ()) or ():
        try:
            value = cell.cell_contents
        except ValueError:
            value = None
        closure.append(_jsonable(value))
    payload = {
        "module": getattr(function, "__module__", ""),
        "qualname": getattr(function, "__qualname__", ""),
        "code": code.co_code.hex(),
        "consts": _jsonable(code.co_consts),
        "names": _jsonable(code.co_names),
        "defaults": _jsonable(getattr(function, "__defaults__", None)),
        "closure": closure,
    }
    return f"code:{_digest(payload)}"


def _priority_callback_digest(callback: Any) -> str:
    """Fingerprint the optional queue-priority callback, including absence."""

    if callback is None:
        return "non-fifo-pareto-priority-none"
    return _callback_digest(callback)


def _priority_after_goal_callback_digest(callback: Any) -> str:
    """Fingerprint the optional post-goal queue-priority callback."""

    if callback is None:
        return _PARETO_PRIORITY_AFTER_GOAL_NONE_DIGEST
    return _callback_digest(callback)


class NonFifoParetoSession:
    """A resumable finite exact-arrival Pareto research session.

    The callbacks remain process-local and are never serialized.  A restore
    therefore requires the caller to provide callbacks whose deterministic
    digests match the checkpoint identity.  This class is intentionally not
    exported through the production planner or any C/D contract.
    """

    __slots__ = (
        "cancel_check",
        "edge_evaluations",
        "evaluate_edge",
        "evaluator_errors",
        "expanded",
        "generated",
        "goals",
        "identity",
        "incumbent_bound_authorized",
        "incumbent_bound_certificate",
        "incumbent_bound_pruned",
        "incumbent_bound_rejected",
        "incumbent_bound_rejection_reasons",
        "incumbent_bound_selection_only",
        "labels_by_key",
        "maximum_elapsed",
        "neighbors",
        "pareto_pruned",
        "priority",
        "priority_after_goal",
        "priority_phase",
        "queue",
        "queue_peak",
        "result",
        "serial",
        "state",
        "total_labels",
    )

    def __init__(
        self,
        *,
        start: Any,
        goal: Any,
        departure_time: datetime,
        neighbors: Callable[[Any], Iterable[Any]],
        evaluate_edge: Callable[
            [Any, Any, datetime], NonFifoTransition | NonFifoParetoTransition
        ],
        objective_count: int = 1,
        pareto_pruning: bool = False,
        max_expansions: int = 50_000,
        max_labels: int = 100_000,
        max_queue: int = 50_000,
        max_edge_evaluations: int = 400_000,
        maximum_elapsed: timedelta | None = None,
        fixture_digest: str = "unspecified-fixture",
        config_digest: str = "unspecified-config",
        scope_digest: str = "unspecified-scope",
        incumbent_bound_certificate: NonFifoParetoBoundCertificate | None = None,
        priority: Callable[[NonFifoParetoLabel], float] | None = None,
        priority_after_goal: Callable[[NonFifoParetoLabel], float] | None = None,
        priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST,
        cancel_check: Callable[[], bool] | None = None,
        identity: NonFifoParetoSessionIdentity | None = None,
    ) -> None:
        candidate_identity = NonFifoParetoSessionIdentity.from_callbacks(
            start=start,
            goal=goal,
            departure_time=departure_time,
            objective_count=objective_count,
            pareto_pruning=pareto_pruning,
            max_expansions=max_expansions,
            max_labels=max_labels,
            max_queue=max_queue,
            max_edge_evaluations=max_edge_evaluations,
            maximum_elapsed=maximum_elapsed,
            neighbors=neighbors,
            evaluate_edge=evaluate_edge,
            fixture_digest=fixture_digest,
            config_digest=config_digest,
            scope_digest=scope_digest,
            incumbent_bound_digest=(
                incumbent_bound_certificate.digest
                if incumbent_bound_certificate is not None
                else _INCUMBENT_BOUND_DISABLED_DIGEST
            ),
            priority=priority,
            priority_after_goal=priority_after_goal,
            priority_policy_digest=priority_policy_digest,
        )
        expected_identity = candidate_identity if identity is None else identity
        expected_identity.assert_valid()
        if identity is not None and identity != candidate_identity:
            raise NonFifoParetoSessionIdentityMismatch(
                "session parameters or callback digest do not match identity"
            )
        self.identity = expected_identity
        if incumbent_bound_certificate is not None and not isinstance(
            incumbent_bound_certificate,
            (NonFifoParetoIncumbentBoundCertificate, NonFifoParetoTerminalBoundCertificate),
        ):
            raise NonFifoParetoSessionIdentityMismatch(
                "incumbent-bound certificate type is invalid"
            )
        self.incumbent_bound_certificate = incumbent_bound_certificate
        self.incumbent_bound_selection_only = isinstance(
            incumbent_bound_certificate, NonFifoParetoTerminalBoundCertificate
        )
        self.neighbors = neighbors
        self.priority = priority
        self.priority_after_goal = priority_after_goal
        self.evaluate_edge = evaluate_edge
        self.cancel_check = cancel_check
        self.maximum_elapsed = (
            timedelta(seconds=expected_identity.maximum_elapsed_seconds)
            if expected_identity.maximum_elapsed_seconds is not None
            else None
        )
        self._initialize()

    def _initialize(self) -> None:
        departure = self.identity.departure_time.astimezone(UTC)
        initial = NonFifoParetoLabel(
            self.identity.start,
            departure,
            (0.0,) * self.identity.objective_count,
            (self.identity.start,),
        )
        self.state = NonFifoParetoSessionState.READY
        self.priority_phase = "pre_goal"
        self.labels_by_key = {initial.exact_key: [initial]}
        self.queue = [self._queue_entry(initial, 0)]
        self.serial = 0
        self.expanded = 0
        self.generated = 0
        self.queue_peak = 1
        self.edge_evaluations = 0
        self.total_labels = 1
        self.pareto_pruned = 0
        self.incumbent_bound_pruned = 0
        self.incumbent_bound_rejected = 0
        self.incumbent_bound_rejection_reasons: dict[str, int] = {}
        self.incumbent_bound_authorized = self._authorize_incumbent_bound()
        self.evaluator_errors = []
        self.goals = []
        self.result = None

    @classmethod
    def _restore(
        cls,
        checkpoint: NonFifoParetoCheckpoint,
        *,
        neighbors: Callable[[Any], Iterable[Any]],
        evaluate_edge: Callable[
            [Any, Any, datetime], NonFifoTransition | NonFifoParetoTransition
        ],
        cancel_check: Callable[[], bool] | None,
        incumbent_bound_certificate: NonFifoParetoBoundCertificate | None,
        priority: Callable[[NonFifoParetoLabel], float] | None,
        priority_after_goal: Callable[[NonFifoParetoLabel], float] | None,
    ) -> NonFifoParetoSession:
        checkpoint.assert_valid()
        identity = checkpoint.identity
        if _callback_digest(neighbors) != identity.neighbor_digest:
            raise NonFifoParetoSessionIdentityMismatch("neighbor callback digest mismatch")
        if _callback_digest(evaluate_edge) != identity.evaluator_digest:
            raise NonFifoParetoSessionIdentityMismatch("evaluator callback digest mismatch")
        if _priority_callback_digest(priority) != identity.priority_callback_digest:
            raise NonFifoParetoSessionIdentityMismatch("priority callback digest mismatch")
        if (
            _priority_after_goal_callback_digest(priority_after_goal)
            != identity.priority_after_goal_callback_digest
        ):
            raise NonFifoParetoSessionIdentityMismatch(
                "post-goal priority callback digest mismatch"
            )
        session = cls.__new__(cls)
        session.identity = identity
        if incumbent_bound_certificate is not None and not isinstance(
            incumbent_bound_certificate,
            (NonFifoParetoIncumbentBoundCertificate, NonFifoParetoTerminalBoundCertificate),
        ):
            raise NonFifoParetoSessionIdentityMismatch(
                "incumbent-bound certificate type is invalid"
            )
        expected_bound_digest = (
            incumbent_bound_certificate.digest
            if incumbent_bound_certificate is not None
            else _INCUMBENT_BOUND_DISABLED_DIGEST
        )
        if identity.incumbent_bound_digest != expected_bound_digest:
            raise NonFifoParetoSessionIdentityMismatch(
                "incumbent-bound certificate digest mismatch"
            )
        session.incumbent_bound_certificate = incumbent_bound_certificate
        session.incumbent_bound_selection_only = isinstance(
            incumbent_bound_certificate, NonFifoParetoTerminalBoundCertificate
        )
        session.neighbors = neighbors
        session.priority = priority
        session.priority_after_goal = priority_after_goal
        session.evaluate_edge = evaluate_edge
        session.cancel_check = cancel_check
        session.maximum_elapsed = (
            timedelta(seconds=identity.maximum_elapsed_seconds)
            if identity.maximum_elapsed_seconds is not None
            else None
        )
        session.state = checkpoint.state
        session.priority_phase = checkpoint.priority_phase
        session.labels_by_key = {}
        for label in checkpoint.labels:
            session.labels_by_key.setdefault(label.exact_key, []).append(label)
        session.queue = list(checkpoint.queue)
        heapify(session.queue)
        session.serial = checkpoint.serial
        session.expanded = checkpoint.expanded
        session.generated = checkpoint.generated
        session.queue_peak = checkpoint.queue_peak
        session.edge_evaluations = checkpoint.edge_evaluations
        session.total_labels = checkpoint.total_labels
        session.pareto_pruned = checkpoint.pareto_pruned
        session.incumbent_bound_pruned = checkpoint.incumbent_bound_pruned
        session.incumbent_bound_rejected = checkpoint.incumbent_bound_rejected
        session.incumbent_bound_rejection_reasons = dict(
            checkpoint.incumbent_bound_rejection_reasons
        )
        session.incumbent_bound_authorized = session._authorize_incumbent_bound(record=False)
        session.evaluator_errors = list(checkpoint.evaluator_errors)
        session.goals = list(checkpoint.goals)
        session.result = None
        return session

    def _queue_key(self, label: NonFifoParetoLabel) -> tuple[float, ...]:
        """Return a deterministic ordering key without changing label semantics."""

        callback = (
            self.priority_after_goal
            if self.priority_phase == "post_goal"
            else self.priority
        )
        if callback is None:
            return label.costs
        value = float(callback(label))
        if not isfinite(value) or value < 0.0:
            raise NonFifoParetoSessionError("priority callback returned an invalid value")
        # Keep the original vector after the scalar ordering value so ties are
        # resolved exactly as in the historical queue.
        return (value, *label.costs)

    def _queue_entry(
        self, label: NonFifoParetoLabel, serial: int
    ) -> tuple[tuple[float, ...], datetime, int, NonFifoParetoLabel]:
        return (self._queue_key(label), label.arrival_time, serial, label)

    def _activate_post_goal_priority(self) -> None:
        """Re-key queued labels once, after the first terminal label appears.

        Re-keying is an ordering-only operation.  Every queued label retains
        its original object, exact arrival, and insertion serial; only the
        heap key changes.  If the callback fails, the phase is rolled back so
        callers cannot continue with a partially re-keyed queue.
        """

        if self.priority_after_goal is None or self.priority_phase == "post_goal":
            return
        previous_phase = self.priority_phase
        self.priority_phase = "post_goal"
        try:
            rebuilt = [
                self._queue_entry(label, serial)
                for _key, _arrival, serial, label in self.queue
            ]
            heapify(rebuilt)
        except Exception:
            self.priority_phase = previous_phase
            raise
        self.queue = rebuilt

    def _record_incumbent_bound_rejection(self, reason: str) -> None:
        self.incumbent_bound_rejected += 1
        self.incumbent_bound_rejection_reasons[reason] = (
            self.incumbent_bound_rejection_reasons.get(reason, 0) + 1
        )

    def _authorize_incumbent_bound(self, *, record: bool = True) -> bool:
        """Authorize the explicit lower-bound proof, otherwise fail closed."""

        certificate = self.incumbent_bound_certificate
        if certificate is None:
            return False
        if not certificate.usable:
            if record:
                self._record_incumbent_bound_rejection(
                    certificate.reason or "certificate_unusable"
                )
            return False
        if not certificate.permits(
            scope_digest=self.identity.scope_digest,
            goal=self.identity.goal,
            objective_count=self.identity.objective_count,
        ):
            if record:
                self._record_incumbent_bound_rejection("scope_or_policy_mismatch")
            return False
        return True

    def _should_prune_incumbent_bound(
        self,
        candidate: NonFifoParetoLabel,
    ) -> bool:
        """Prune only a new label whose every completion is dominated.

        The lower bound is independent of arrival time and therefore cannot
        erase non-FIFO exact-arrival alternatives by itself.  It is applied
        against an already observed goal label only; no label already in the
        queue or already expanded is removed.
        """

        if not self.incumbent_bound_authorized:
            return False
        certificate = self.incumbent_bound_certificate
        if certificate is None:  # defensive fence for malformed restores
            self._record_incumbent_bound_rejection("certificate_missing")
            self.incumbent_bound_authorized = False
            return False
        if isinstance(certificate, NonFifoParetoTerminalBoundCertificate):
            lower_bound = certificate.lower_bound(candidate.node)
            if lower_bound is None:
                # A complete certificate must cover every reachable state.  A
                # missing entry is therefore a fail-closed rejection, not a
                # reason to guess a zero bound.
                self._record_incumbent_bound_rejection("state_uncovered")
                self.incumbent_bound_authorized = False
                return False
            if len(lower_bound) != len(candidate.costs):
                self._record_incumbent_bound_rejection("objective_dimension_mismatch")
                self.incumbent_bound_authorized = False
                return False
            completion = tuple(
                current + bound
                for current, bound in zip(candidate.costs, lower_bound, strict=True)
            )
            if any(not isfinite(value) for value in completion):
                self._record_incumbent_bound_rejection("non_finite_completion")
                self.incumbent_bound_authorized = False
                return False
            # The terminal state has no successors, so arrival time is not a
            # future-evaluator input.  In this explicit selection-only mode,
            # compare the conservative completion bound to an observed goal
            # lexicographically.  Equal vectors are retained for deterministic
            # path/arrival tie-breaking and auditability.
            if any(goal.costs < completion for goal in self.goals):
                self.incumbent_bound_pruned += 1
                return True
            return False
        evidence = certificate.lower_bound(candidate.node, candidate.arrival_time)
        if evidence is None:
            self._record_incumbent_bound_rejection("state_uncovered")
            self.incumbent_bound_authorized = False
            return False
        goal_arrival, lower_bound = evidence
        completion = tuple(
            current + bound
            for current, bound in zip(candidate.costs, lower_bound, strict=True)
        )
        if any(not isfinite(value) for value in completion):
            self._record_incumbent_bound_rejection("non_finite_completion")
            self.incumbent_bound_authorized = False
            return False
        if any(
            goal.arrival_time == goal_arrival
            and all(left <= right for left, right in zip(goal.costs, completion, strict=True))
            and any(left < right for left, right in zip(goal.costs, completion, strict=True))
            for goal in self.goals
        ):
            self.incumbent_bound_pruned += 1
            return True
        return False

    @property
    def session_id(self) -> str:
        return self.identity.digest

    @property
    def policy_digest(self) -> str:
        return self.identity.policy_digest

    def _finish(
        self,
        status: NonFifoSearchStatus,
        reason: str | None,
    ) -> NonFifoParetoSearchResult:
        self.state = NonFifoParetoSessionState(status.value)
        self.result = _pareto_result(
            status,
            self.goals,
            self.labels_by_key,
            self.expanded,
            self.generated,
            self.queue_peak,
            self.edge_evaluations,
            self.pareto_pruned,
            self.evaluator_errors,
            reason,
            goal_node=self.identity.goal,
            pareto_pruning=self.identity.pareto_pruning,
            search_limits=(
                self.identity.max_expansions,
                self.identity.max_labels,
                self.identity.max_queue,
                self.identity.max_edge_evaluations,
            ),
            incumbent_bound_digest=self.identity.incumbent_bound_digest,
            incumbent_bound_pruned=self.incumbent_bound_pruned,
            incumbent_bound_rejected=self.incumbent_bound_rejected,
            incumbent_bound_rejection_reasons=tuple(
                sorted(self.incumbent_bound_rejection_reasons.items())
            ),
            priority_policy_digest=self.identity.priority_policy_digest,
            selection_only=self.incumbent_bound_selection_only,
        )
        return self.result

    def advance(self, expansion_slice: int | None = None) -> NonFifoParetoSearchResult | None:
        """Advance up to a slice; return ``None`` only while paused."""

        if expansion_slice is not None and (
            isinstance(expansion_slice, bool) or expansion_slice < 1
        ):
            raise ValueError("expansion_slice must be a positive integer or None")
        if self.state in (
            NonFifoParetoSessionState.GOAL_FOUND,
            NonFifoParetoSessionState.EXHAUSTED,
            NonFifoParetoSessionState.RESOURCE_LIMIT,
            NonFifoParetoSessionState.CANCELLED,
            NonFifoParetoSessionState.EVALUATOR_FAILURE,
        ):
            return self.result
        if self.state is NonFifoParetoSessionState.PAUSED:
            self.state = NonFifoParetoSessionState.READY
        expanded_this_call = 0
        limits = self.identity
        while self.queue:
            if _cancelled(self.cancel_check):
                return self._finish(NonFifoSearchStatus.CANCELLED, "cancelled")
            if expansion_slice is not None and expanded_this_call >= expansion_slice:
                self.state = NonFifoParetoSessionState.PAUSED
                return None
            _, _, _, label = heappop(self.queue)
            if not _contains_label(self.labels_by_key[label.exact_key], label):
                continue
            self.expanded += 1
            expanded_this_call += 1
            if self.expanded > limits.max_expansions:
                return self._finish(NonFifoSearchStatus.RESOURCE_LIMIT, "search_limit_exceeded")
            if label.node == limits.goal:
                self.goals.append(label)
                self._activate_post_goal_priority()
                continue
            try:
                neighbours = _ordered_neighbors(self.neighbors(label.node))
            except Exception as error:
                self.evaluator_errors.append(f"{type(error).__name__}:{error}")
                continue
            for neighbor in neighbours:
                if _cancelled(self.cancel_check):
                    return self._finish(NonFifoSearchStatus.CANCELLED, "cancelled")
                self.edge_evaluations += 1
                if self.edge_evaluations > limits.max_edge_evaluations:
                    return self._finish(NonFifoSearchStatus.RESOURCE_LIMIT, "search_limit_exceeded")
                try:
                    transition = self.evaluate_edge(label.node, neighbor, label.arrival_time)
                    transition = _coerce_pareto_transition(
                        transition, limits.objective_count
                    )
                    if transition.arrival_time <= label.arrival_time:
                        raise NonFifoEvaluationError("arrival_not_strictly_later")
                    if transition.arrival_time < limits.departure_time:
                        raise NonFifoEvaluationError("arrival_before_departure")
                except NonFifoEvaluationSkipped:
                    # Classified edge-domain rejections are unavailable
                    # transitions, not global evaluator failures.  The
                    # bridge records their reason in its planner diagnostics;
                    # unknown exceptions remain fail-closed below.
                    continue
                except Exception as error:
                    self.evaluator_errors.append(f"{type(error).__name__}:{error}")
                    continue
                if self.maximum_elapsed is not None and (
                    transition.arrival_time - limits.departure_time > self.maximum_elapsed
                ):
                    continue
                next_label = NonFifoParetoLabel(
                    neighbor,
                    transition.arrival_time,
                    tuple(
                        left + right
                        for left, right in zip(
                            label.costs, transition.costs, strict=True
                        )
                    ),
                    (*label.path, neighbor),
                    (*label.transitions, transition),
                )
                if self._should_prune_incumbent_bound(next_label):
                    continue
                frontier = self.labels_by_key.setdefault(next_label.exact_key, [])
                if self.identity.pareto_pruning and any(
                    existing.dominates(next_label) for existing in frontier
                ):
                    self.pareto_pruned += 1
                    continue
                if self.total_labels >= limits.max_labels:
                    return self._finish(NonFifoSearchStatus.RESOURCE_LIMIT, "search_limit_exceeded")
                frontier.append(next_label)
                self.total_labels += 1
                self.serial += 1
                heappush(
                    self.queue,
                    self._queue_entry(next_label, self.serial),
                )
                self.generated += 1
                self.queue_peak = max(self.queue_peak, len(self.queue))
                if len(self.queue) > limits.max_queue:
                    return self._finish(NonFifoSearchStatus.RESOURCE_LIMIT, "search_limit_exceeded")
        if self.evaluator_errors:
            return self._finish(NonFifoSearchStatus.EVALUATOR_FAILURE, "evaluator_failure")
        if self.goals:
            return self._finish(NonFifoSearchStatus.GOAL_FOUND, None)
        return self._finish(NonFifoSearchStatus.EXHAUSTED, "no_route")

    def run(self) -> NonFifoParetoSearchResult:
        result = self.advance()
        if result is None:
            raise RuntimeError("unbounded Pareto session unexpectedly paused")
        return result

    def checkpoint(self) -> NonFifoParetoCheckpoint:
        if self.state not in (
            NonFifoParetoSessionState.READY,
            NonFifoParetoSessionState.PAUSED,
        ):
            raise NonFifoParetoSessionRestoreError(
                "only READY or PAUSED sessions can be checkpointed"
            )
        labels = tuple(
            label for values in self.labels_by_key.values() for label in values
        )
        return NonFifoParetoCheckpoint(
            identity=self.identity,
            state=self.state,
            labels=labels,
            goals=tuple(self.goals),
            queue=tuple(self.queue),
            serial=self.serial,
            expanded=self.expanded,
            generated=self.generated,
            queue_peak=self.queue_peak,
            edge_evaluations=self.edge_evaluations,
            total_labels=self.total_labels,
            pareto_pruned=self.pareto_pruned,
            evaluator_errors=tuple(self.evaluator_errors),
            incumbent_bound_pruned=self.incumbent_bound_pruned,
            incumbent_bound_rejected=self.incumbent_bound_rejected,
            incumbent_bound_rejection_reasons=tuple(
                sorted(self.incumbent_bound_rejection_reasons.items())
            ),
            priority_phase=self.priority_phase,
        )


def create_non_fifo_pareto_session(
    **kwargs: Any,
) -> NonFifoParetoSession:
    """Create an explicit finite Pareto session with a complete identity."""

    return NonFifoParetoSession(**kwargs)


def restore_non_fifo_pareto_session(
    checkpoint: NonFifoParetoCheckpoint,
    *,
    neighbors: Callable[[Any], Iterable[Any]],
    evaluate_edge: Callable[
        [Any, Any, datetime], NonFifoTransition | NonFifoParetoTransition
    ],
    cancel_check: Callable[[], bool] | None = None,
    identity: NonFifoParetoSessionIdentity | None = None,
    incumbent_bound_certificate: NonFifoParetoBoundCertificate | None = None,
    priority: Callable[[NonFifoParetoLabel], float] | None = None,
    priority_after_goal: Callable[[NonFifoParetoLabel], float] | None = None,
) -> NonFifoParetoSession:
    """Restore a paused finite session after all identity fences."""

    if not isinstance(checkpoint, NonFifoParetoCheckpoint):
        raise NonFifoParetoSessionRestoreError("checkpoint type is invalid")
    checkpoint.assert_valid()
    if identity is not None and identity != checkpoint.identity:
        raise NonFifoParetoSessionIdentityMismatch("restore identity mismatch")
    return NonFifoParetoSession._restore(
        checkpoint,
        neighbors=neighbors,
        evaluate_edge=evaluate_edge,
        cancel_check=cancel_check,
        incumbent_bound_certificate=incumbent_bound_certificate,
        priority=priority,
        priority_after_goal=priority_after_goal,
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
    fixture_digest: str = "one-shot-fixture",
    config_digest: str = "one-shot-config",
    scope_digest: str = "unspecified-scope",
    incumbent_bound_certificate: NonFifoParetoBoundCertificate | None = None,
    priority: Callable[[NonFifoParetoLabel], float] | None = None,
    priority_after_goal: Callable[[NonFifoParetoLabel], float] | None = None,
    priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST,
) -> NonFifoParetoSearchResult:
    """Run one finite session to completion with the historical API."""

    return create_non_fifo_pareto_session(
        start=start,
        goal=goal,
        departure_time=departure_time,
        neighbors=neighbors,
        evaluate_edge=evaluate_edge,
        objective_count=objective_count,
        pareto_pruning=pareto_pruning,
        max_expansions=max_expansions,
        max_labels=max_labels,
        max_queue=max_queue,
        max_edge_evaluations=max_edge_evaluations,
        cancel_check=cancel_check,
        maximum_elapsed=maximum_elapsed,
        fixture_digest=fixture_digest,
        config_digest=config_digest,
        scope_digest=scope_digest,
        incumbent_bound_certificate=incumbent_bound_certificate,
        priority=priority,
        priority_after_goal=priority_after_goal,
        priority_policy_digest=priority_policy_digest,
    ).run()


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


def _ordered_neighbors(neighbors: Iterable[Any]) -> tuple[Any, ...]:
    """Materialize neighbors in a stable order without collapsing duplicates."""

    values = tuple(neighbors)
    return tuple(sorted(values, key=_canonical_token))


def _canonical_token(value: Any) -> str:
    """Return a deterministic token for arbitrary finite fixture nodes."""

    try:
        return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _pareto_label_payload(label: NonFifoParetoLabel) -> dict[str, Any]:
    """Serialize one label for the auditable frontier digest."""

    return {
        "node": _jsonable(label.node),
        "arrival_time": _jsonable(label.arrival_time),
        "costs": label.costs,
        "path": _jsonable(label.path),
        "transitions": _jsonable(label.transitions),
    }


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
    *,
    goal_node: Any | None = None,
    pareto_pruning: bool = False,
    search_limits: tuple[int, int, int, int] = (),
    incumbent_bound_digest: str = _INCUMBENT_BOUND_DISABLED_DIGEST,
    incumbent_bound_pruned: int = 0,
    incumbent_bound_rejected: int = 0,
    incumbent_bound_rejection_reasons: Iterable[tuple[str, int]] = (),
    priority_policy_digest: str = _PARETO_PRIORITY_DEFAULT_DIGEST,
    selection_only: bool = False,
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
        goal_node=goal_node,
        pareto_pruning=pareto_pruning,
        search_limits=search_limits,
        incumbent_bound_digest=incumbent_bound_digest,
        incumbent_bound_pruned=incumbent_bound_pruned,
        incumbent_bound_rejected=incumbent_bound_rejected,
        incumbent_bound_rejection_reasons=tuple(incumbent_bound_rejection_reasons),
        priority_policy_digest=priority_policy_digest,
        frontier_complete=not selection_only,
        selection_only=selection_only,
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
    if isinstance(value, CodeType):
        return {
            "co_code": value.co_code.hex(),
            "co_consts": _jsonable(value.co_consts),
            "co_names": _jsonable(value.co_names),
            "co_varnames": _jsonable(value.co_varnames),
        }
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, NonFifoParetoLabel):
        return _pareto_label_payload(value)
    if isinstance(value, NonFifoParetoIncumbentBoundCertificate):
        return {
            "schema_version": value.schema_version,
            "status": value.status,
            "scope_digest": value.scope_digest,
            "goal": _jsonable(value.goal),
            "objective_count": value.objective_count,
            "state_lower_bounds": _jsonable(value.state_lower_bounds),
            "coverage_complete": value.coverage_complete,
            "evaluator_certified": value.evaluator_certified,
            "proof_digest": value.proof_digest,
            "reason": value.reason,
            "certificate_digest": value.digest,
        }
    if isinstance(value, NonFifoParetoTerminalBoundCertificate):
        return {
            "schema_version": value.schema_version,
            "status": value.status,
            "scope_digest": value.scope_digest,
            "goal": _jsonable(value.goal),
            "objective_count": value.objective_count,
            "node_lower_bounds": _jsonable(value.node_lower_bounds),
            "coverage_complete": value.coverage_complete,
            "evaluator_certified": value.evaluator_certified,
            "proof_digest": value.proof_digest,
            "reason": value.reason,
            "selection_only": value.selection_only,
            "certificate_digest": value.digest,
        }
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
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


__all__ = [
    "NonFifoBusinessEvidence",
    "NonFifoEvaluationError",
    "NonFifoEvaluationSkipped",
    "NonFifoFrontierCertificateError",
    "NonFifoFrontierComparisonStatus",
    "NonFifoLabel",
    "NonFifoParetoCheckpoint",
    "NonFifoParetoFrontierCertificate",
    "NonFifoParetoFrontierComparison",
    "NonFifoParetoIncumbentBoundCertificate",
    "NonFifoParetoIncumbentBoundStatus",
    "NonFifoParetoLabel",
    "NonFifoParetoSearchResult",
    "NonFifoParetoSession",
    "NonFifoParetoSessionError",
    "NonFifoParetoSessionIdentity",
    "NonFifoParetoSessionIdentityMismatch",
    "NonFifoParetoSessionRestoreError",
    "NonFifoParetoSessionState",
    "NonFifoParetoTerminalBoundCertificate",
    "NonFifoParetoTerminalBoundStatus",
    "NonFifoParetoTransition",
    "NonFifoSearchResult",
    "NonFifoSearchStatus",
    "NonFifoTransition",
    "certify_non_fifo_pareto_frontier",
    "compare_non_fifo_pareto_frontiers",
    "create_non_fifo_pareto_session",
    "restore_non_fifo_pareto_session",
    "search_non_fifo",
    "search_non_fifo_pareto",
]
