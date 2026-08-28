"""C-internal bridge from the actual temporal session to non-FIFO research.

The finite transition search in :mod:`non_fifo_feasibility` is intentionally
small and independent of the route planner.  This module verifies the next
boundary: the active exact-arrival session can be exercised with the real
``_EdgeTraversal`` evaluator while retaining the same conservative non-FIFO
rules.  It is a research adapter, not a planner API.  Importing this module is
therefore always explicit and it is not re-exported from ``planners``.

The adapter requires zero-heuristic search, disabled temporal dominance and
no state-bound certificate.  Those requirements make the research claim
auditable: no FIFO assumption, heuristic lower-bound proof, or unreviewed
state exclusion can affect the result.  The production planner and its
default path remain unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any

from arctic_route_planning.errors import NoRouteError, PlanningCancelled

from .errors import PlanningHorizonExceeded
from .non_fifo_feasibility import NonFifoSearchStatus
from .temporal_label_astar import (
    TemporalCandidateResult,
    TemporalLabelAStar,
    TemporalSearchLimitExceeded,
)
from .temporal_session import TemporalSessionIdentity, TemporalSessionIdentityMismatch
from .time_dependent_astar import PlanningRequest, PlanningResult


class NonFifoTemporalAdapterError(ValueError):
    """The explicit research adapter was invoked outside its safe mode."""


class NonFifoTemporalSafetyViolation(RuntimeError):
    """The active session reported pruning that the adapter forbids."""


@dataclass(frozen=True, slots=True)
class NonFifoTemporalStepEvidence:
    """Business fields retained from one actual temporal route step."""

    node: tuple[int, int]
    eta: datetime
    recommended_speed_knots: float | None
    edge_distance_km: float
    edge_risk_score: float
    edge_maximum_risk: float
    edge_confidence: float
    edge_cost: Any
    source_risk_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NonFifoTemporalResearchResult:
    """Status and evidence returned by the actual-session research bridge."""

    status: NonFifoSearchStatus
    candidate: TemporalCandidateResult | None
    session_id: str | None
    reason: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    semantic_digest: str | None = None
    diagnostics: Any = None

    @property
    def planning_result(self) -> PlanningResult | None:
        """Return the complete C route result only for a successful search."""

        return self.candidate.planning_result if self.candidate is not None else None

    @property
    def business_evidence(self) -> tuple[NonFifoTemporalStepEvidence, ...]:
        """Expose route business observations without changing C contracts."""

        result = self.planning_result
        if result is None:
            return ()
        return tuple(
            NonFifoTemporalStepEvidence(
                node=step.node,
                eta=step.eta,
                recommended_speed_knots=step.recommended_speed_knots,
                edge_distance_km=step.edge_distance_km,
                edge_risk_score=step.edge_risk_score,
                edge_maximum_risk=step.edge_maximum_risk,
                edge_confidence=step.edge_confidence,
                edge_cost=step.edge_cost,
                source_risk_ids=step.source_risk_ids,
            )
            for step in result.steps
        )


def run_non_fifo_temporal_search(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchResult:
    """Run the active exact-arrival session under a conservative research fence.

    ``request.use_heuristic`` must be false so that the adapter is a direct
    label-correcting/Dijkstra-style evidence path.  A caller must also leave
    temporal dominance disabled and omit state bounds.  The function never
    mutates either object.  Expected bounded-search outcomes are returned as
    explicit statuses; a malformed or mismatched identity is raised as an
    adapter error because treating it as a route failure would hide evidence
    invalidity.
    """

    _validate_research_mode(planner, request, identity)
    try:
        session = planner.create_session(request, identity=identity)
    except PlanningCancelled as error:
        return _failure(
            NonFifoSearchStatus.CANCELLED,
            None,
            None,
            reason="cancelled",
            error=error,
        )
    except NoRouteError as error:
        return _failure(
            NonFifoSearchStatus.EXHAUSTED,
            None,
            None,
            reason="no_route",
            error=error,
        )
    except TemporalSessionIdentityMismatch as error:
        raise NonFifoTemporalAdapterError(
            f"temporal session identity fence rejected: {error}"
        ) from error
    except Exception as error:  # pragma: no cover - defensive creation fence
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            None,
            None,
            reason="session_creation_failure",
            error=error,
        )

    session_id = session.session_id
    try:
        candidate = planner.advance_session(session)
    except TemporalSearchLimitExceeded as error:
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.RESOURCE_LIMIT,
            session_id,
            diagnostics,
            reason="search_limit_exceeded",
            error=error,
        )
    except PlanningCancelled as error:
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.CANCELLED,
            session_id,
            diagnostics,
            reason="cancelled",
            error=error,
        )
    except PlanningHorizonExceeded as error:
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.EXHAUSTED,
            session_id,
            diagnostics,
            reason="horizon_exceeded",
            error=error,
        )
    except NoRouteError as error:
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.EXHAUSTED,
            session_id,
            diagnostics,
            reason="no_route",
            error=error,
        )
    except Exception as error:  # pragma: no cover - exercised by evaluator fixture
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="evaluator_failure",
            error=error,
        )

    if candidate is None:
        # A full adapter run does not supply an expansion slice, so this is an
        # invariant violation rather than a partial success.
        diagnostics = session.context.diagnostics.freeze()
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="session_not_terminal",
            error=RuntimeError("unbounded session returned no terminal result"),
        )

    diagnostics = candidate.diagnostics
    if (
        diagnostics.dominance_policy != "none"
        or diagnostics.dominance_scope_match
        or diagnostics.dominance_checks
        or diagnostics.dominance_pruned
        or diagnostics.state_bound_checks
        or diagnostics.state_bound_pruned
    ):
        violation = NonFifoTemporalSafetyViolation(
            "non-FIFO adapter observed forbidden dominance/state-bound pruning"
        )
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="unexpected_pruning",
            error=violation,
        )

    route_digest = _route_semantic_digest(candidate.planning_result)
    return NonFifoTemporalResearchResult(
        status=NonFifoSearchStatus.GOAL_FOUND,
        candidate=candidate,
        session_id=session_id,
        semantic_digest=route_digest,
        diagnostics=diagnostics,
    )


def _validate_research_mode(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    identity: TemporalSessionIdentity | None,
) -> None:
    if not isinstance(planner, TemporalLabelAStar):
        raise NonFifoTemporalAdapterError(
            "actual temporal adapter requires a TemporalLabelAStar instance"
        )
    if request.use_heuristic:
        raise NonFifoTemporalAdapterError(
            "non-FIFO adapter requires request.use_heuristic=False"
        )
    if planner.dominance_policy.enabled:
        raise NonFifoTemporalAdapterError(
            "non-FIFO adapter requires TemporalDominancePolicy.disabled()"
        )
    if planner.state_bound_certificate is not None:
        raise NonFifoTemporalAdapterError(
            "non-FIFO adapter does not accept a state-bound certificate"
        )
    if identity is not None and not isinstance(identity, TemporalSessionIdentity):
        raise NonFifoTemporalAdapterError(
            "identity must be a TemporalSessionIdentity when supplied"
        )


def _failure(
    status: NonFifoSearchStatus,
    session_id: str | None,
    diagnostics: Any,
    *,
    reason: str,
    error: Exception,
) -> NonFifoTemporalResearchResult:
    return NonFifoTemporalResearchResult(
        status=status,
        candidate=None,
        session_id=session_id,
        reason=reason,
        error_type=type(error).__name__,
        error_message=str(error),
        diagnostics=diagnostics,
    )


def _route_semantic_digest(result: PlanningResult) -> str:
    """Digest route/business semantics while excluding runtime metrics."""

    payload = {
        "objective": result.objective,
        "steps": result.steps,
        "total_cost_hours": result.total_cost_hours,
        "distance_km": result.distance_km,
        "travel_hours": result.travel_hours,
        "average_risk": result.average_risk,
        "maximum_risk": result.maximum_risk,
        "minimum_confidence": result.minimum_confidence,
        "source_risk_ids": result.source_risk_ids,
    }
    encoded = json.dumps(
        _jsonable(payload),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("route digest datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("route digest values must be finite")
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


__all__ = [
    "NonFifoTemporalAdapterError",
    "NonFifoTemporalResearchResult",
    "NonFifoTemporalSafetyViolation",
    "NonFifoTemporalStepEvidence",
    "run_non_fifo_temporal_search",
]
