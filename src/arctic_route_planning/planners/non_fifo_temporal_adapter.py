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
from .temporal_bounds import TemporalStateBoundCertificate
from .temporal_heuristic_bounds import TemporalHeuristicCertificate
from .temporal_label_astar import (
    TemporalCandidateResult,
    TemporalLabelAStar,
    TemporalSearchLimitExceeded,
)
from .temporal_session import (
    TemporalSessionCheckpoint,
    TemporalSessionIdentity,
    TemporalSessionIdentityMismatch,
    TemporalSessionRestoreError,
    TemporalSessionState,
    checkpoint_session,
    restore_session,
)
from .time_dependent_astar import PlanningRequest, PlanningResult

_ADAPTER_SCHEMA_VERSION = "c.p0.2-nonfifo-temporal-adapter.v2"
_ADAPTER_MODE_DIGEST = hashlib.sha256(_ADAPTER_SCHEMA_VERSION.encode("utf-8")).hexdigest()


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


@dataclass(frozen=True, slots=True)
class NonFifoTemporalResearchCheckpoint:
    """Adapter checkpoint with an independent research-mode identity fence."""

    session_checkpoint: TemporalSessionCheckpoint
    mode_digest: str = _ADAPTER_MODE_DIGEST
    schema_version: str = _ADAPTER_SCHEMA_VERSION
    state_bound_policy_digest: str = "temporal-state-bound-disabled"
    heuristic_policy_digest: str = "temporal-heuristic-default"
    state_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.session_checkpoint, TemporalSessionCheckpoint):
            raise TypeError("session_checkpoint must be a TemporalSessionCheckpoint")
        if self.schema_version != _ADAPTER_SCHEMA_VERSION:
            raise NonFifoTemporalAdapterError("unsupported non-FIFO adapter checkpoint schema")
        if self.mode_digest != _ADAPTER_MODE_DIGEST:
            raise NonFifoTemporalAdapterError("non-FIFO adapter mode digest mismatch")
        state_bound_digest = getattr(self, "state_bound_policy_digest", None)
        if state_bound_digest != self.session_checkpoint.identity.state_bound_policy_digest:
            raise NonFifoTemporalAdapterError("non-FIFO adapter state-bound policy digest mismatch")
        heuristic_digest = getattr(self, "heuristic_policy_digest", None)
        if heuristic_digest != self.session_checkpoint.identity.heuristic_policy_digest:
            raise NonFifoTemporalAdapterError("non-FIFO adapter heuristic policy digest mismatch")
        if self.session_checkpoint.state not in (
            TemporalSessionState.READY,
            TemporalSessionState.PAUSED,
        ):
            raise NonFifoTemporalAdapterError(
                "only READY or PAUSED temporal sessions can be checkpointed"
            )
        expected = self._calculated_state_digest()
        if self.state_digest and self.state_digest != expected:
            raise TemporalSessionRestoreError("non-FIFO adapter checkpoint digest mismatch")
        object.__setattr__(self, "state_digest", expected)

    def _calculated_state_digest(self) -> str:
        return _digest(
            {
                "schema_version": self.schema_version,
                "mode_digest": self.mode_digest,
                "state_bound_policy_digest": getattr(self, "state_bound_policy_digest", None),
                "heuristic_policy_digest": getattr(self, "heuristic_policy_digest", None),
                "session_checkpoint": self.session_checkpoint.state_digest,
            }
        )

    def assert_valid(self) -> None:
        """Re-check the wrapper and nested checkpoint before restoration."""

        if self.schema_version != _ADAPTER_SCHEMA_VERSION:
            raise NonFifoTemporalAdapterError("unsupported non-FIFO adapter checkpoint schema")
        if self.mode_digest != _ADAPTER_MODE_DIGEST:
            raise NonFifoTemporalAdapterError("non-FIFO adapter mode digest mismatch")
        state_bound_digest = getattr(self, "state_bound_policy_digest", None)
        if state_bound_digest != self.session_checkpoint.identity.state_bound_policy_digest:
            raise NonFifoTemporalAdapterError("non-FIFO adapter state-bound policy digest mismatch")
        heuristic_digest = getattr(self, "heuristic_policy_digest", None)
        if heuristic_digest != self.session_checkpoint.identity.heuristic_policy_digest:
            raise NonFifoTemporalAdapterError("non-FIFO adapter heuristic policy digest mismatch")
        if self.state_digest != self._calculated_state_digest():
            raise TemporalSessionRestoreError("non-FIFO adapter checkpoint digest mismatch")
        self.session_checkpoint.assert_valid()

    @property
    def digest(self) -> str:
        return self.state_digest


class NonFifoTemporalResearchSession:
    """Resumable wrapper around one actual exact-arrival temporal session."""

    __slots__ = ("allow_heuristic", "allow_state_bound", "planner", "request", "session")

    def __init__(
        self,
        planner: TemporalLabelAStar,
        request: PlanningRequest,
        session: Any,
        *,
        allow_state_bound: bool = False,
        allow_heuristic: bool = False,
    ) -> None:
        self.planner = planner
        self.request = request
        self.session = session
        self.allow_state_bound = allow_state_bound
        self.allow_heuristic = allow_heuristic

    @property
    def state(self) -> TemporalSessionState:
        return self.session.state

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def identity(self) -> TemporalSessionIdentity:
        return self.session.identity

    def advance(self, expansion_slice: int | None = None) -> NonFifoTemporalResearchResult | None:
        """Advance the bounded search; return ``None`` only while paused."""

        try:
            candidate = self.planner.advance_session(
                self.session,
                expansion_slice=expansion_slice,
            )
        except TemporalSearchLimitExceeded as error:
            return _failure(
                NonFifoSearchStatus.RESOURCE_LIMIT,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="search_limit_exceeded",
                error=error,
            )
        except PlanningCancelled as error:
            return _failure(
                NonFifoSearchStatus.CANCELLED,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="cancelled",
                error=error,
            )
        except PlanningHorizonExceeded as error:
            return _failure(
                NonFifoSearchStatus.EXHAUSTED,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="horizon_exceeded",
                error=error,
            )
        except NoRouteError as error:
            return _failure(
                NonFifoSearchStatus.EXHAUSTED,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="no_route",
                error=error,
            )
        except Exception as error:  # pragma: no cover - defensive evaluator boundary
            return _failure(
                NonFifoSearchStatus.EVALUATOR_FAILURE,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="evaluator_failure",
                error=error,
            )

        if candidate is None:
            if self.state is TemporalSessionState.PAUSED:
                return None
            return _failure(
                NonFifoSearchStatus.EVALUATOR_FAILURE,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="session_not_terminal",
                error=RuntimeError("temporal session returned no result outside PAUSED state"),
            )
        return _candidate_result(
            self.session_id,
            candidate,
            allow_state_bound=self.allow_state_bound,
            allow_heuristic=self.allow_heuristic,
        )

    def run(self) -> NonFifoTemporalResearchResult:
        """Run until a terminal result, never treating a pause as success."""

        result = self.advance()
        if result is None:
            return _failure(
                NonFifoSearchStatus.EVALUATOR_FAILURE,
                self.session_id,
                self.session.context.diagnostics.freeze(),
                reason="session_not_terminal",
                error=RuntimeError("unbounded temporal session returned PAUSED"),
            )
        return result

    def checkpoint(self) -> NonFifoTemporalResearchCheckpoint:
        """Capture a resumable adapter checkpoint only at READY/PAUSED."""

        if self.state not in (TemporalSessionState.READY, TemporalSessionState.PAUSED):
            raise NonFifoTemporalAdapterError(
                "only READY or PAUSED temporal sessions can be checkpointed"
            )
        return NonFifoTemporalResearchCheckpoint(
            checkpoint_session(self.session),
            state_bound_policy_digest=self.identity.state_bound_policy_digest,
            heuristic_policy_digest=self.identity.heuristic_policy_digest,
        )


def create_non_fifo_temporal_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Create a fenced, resumable actual-session research wrapper."""

    _validate_research_mode(planner, request, identity)
    try:
        session = planner.create_session(request, identity=identity)
    except TemporalSessionIdentityMismatch as error:
        raise NonFifoTemporalAdapterError(
            f"temporal session identity fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(planner, request, session)


def restore_non_fifo_temporal_session(
    planner: TemporalLabelAStar,
    checkpoint: NonFifoTemporalResearchCheckpoint,
    request: PlanningRequest,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Restore a wrapper after checking adapter and active-session fences."""

    if not isinstance(checkpoint, NonFifoTemporalResearchCheckpoint):
        raise NonFifoTemporalAdapterError("checkpoint must be a NonFifoTemporalResearchCheckpoint")
    try:
        checkpoint.assert_valid()
    except (NonFifoTemporalAdapterError, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    _validate_research_mode(planner, request, identity)
    try:
        session = restore_session(
            planner,
            checkpoint.session_checkpoint,
            request=request,
            identity=identity,
        )
    except (TemporalSessionIdentityMismatch, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(planner, request, session)


def create_non_fifo_temporal_certified_heuristic_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalHeuristicCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Create the explicit non-FIFO session that may use a certified heuristic."""

    _validate_heuristic_certificate(planner, certificate, request=request)
    _validate_research_mode(planner, request, identity, allow_heuristic=True)
    try:
        session = planner.create_session(request, identity=identity)
    except TemporalSessionIdentityMismatch as error:
        raise NonFifoTemporalAdapterError(
            f"temporal session identity fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(
        planner,
        request,
        session,
        allow_heuristic=True,
    )


def restore_non_fifo_temporal_certified_heuristic_session(
    planner: TemporalLabelAStar,
    checkpoint: NonFifoTemporalResearchCheckpoint,
    request: PlanningRequest,
    certificate: TemporalHeuristicCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Restore the certified-heuristic session after all identity fences."""

    if not isinstance(checkpoint, NonFifoTemporalResearchCheckpoint):
        raise NonFifoTemporalAdapterError("checkpoint must be a NonFifoTemporalResearchCheckpoint")
    _validate_heuristic_certificate(planner, certificate, request=request)
    try:
        checkpoint.assert_valid()
    except (NonFifoTemporalAdapterError, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    if checkpoint.heuristic_policy_digest != certificate.digest:
        raise NonFifoTemporalAdapterError("non-FIFO temporal checkpoint heuristic digest mismatch")
    _validate_research_mode(planner, request, identity, allow_heuristic=True)
    try:
        session = restore_session(
            planner,
            checkpoint.session_checkpoint,
            request=request,
            identity=identity,
        )
    except (TemporalSessionIdentityMismatch, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(
        planner,
        request,
        session,
        allow_heuristic=True,
    )


def create_non_fifo_temporal_bounded_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Create an explicitly certified, state-bound non-FIFO session.

    The ordinary adapter deliberately rejects state bounds.  This separate
    entry point is the only research path that may use one, and it requires
    the immutable certificate to be installed on the supplied planner.  A
    scope mismatch is left to the active session's fail-closed authorization
    (and therefore produces zero pruning plus an explicit rejection).
    """

    _validate_bound_certificate(planner, certificate, request=request)
    _validate_research_mode(planner, request, identity, allow_state_bound=True)
    try:
        session = planner.create_session(request, identity=identity)
    except TemporalSessionIdentityMismatch as error:
        raise NonFifoTemporalAdapterError(
            f"temporal session identity fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(
        planner,
        request,
        session,
        allow_state_bound=True,
    )


def create_non_fifo_temporal_arrival_bounded_session(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Create a bounded session that requires a complete arrival envelope."""

    _validate_arrival_bound_certificate(planner, certificate, request=request)
    return create_non_fifo_temporal_bounded_session(
        planner,
        request,
        certificate,
        identity=identity,
    )


def restore_non_fifo_temporal_bounded_session(
    planner: TemporalLabelAStar,
    checkpoint: NonFifoTemporalResearchCheckpoint,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Restore a bounded session after checking its certificate identity."""

    if not isinstance(checkpoint, NonFifoTemporalResearchCheckpoint):
        raise NonFifoTemporalAdapterError("checkpoint must be a NonFifoTemporalResearchCheckpoint")
    _validate_bound_certificate(planner, certificate, request=request)
    try:
        checkpoint.assert_valid()
    except (NonFifoTemporalAdapterError, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    if checkpoint.state_bound_policy_digest != certificate.digest:
        raise NonFifoTemporalAdapterError(
            "non-FIFO temporal checkpoint state-bound digest mismatch"
        )
    _validate_research_mode(planner, request, identity, allow_state_bound=True)
    try:
        session = restore_session(
            planner,
            checkpoint.session_checkpoint,
            request=request,
            identity=identity,
        )
    except (TemporalSessionIdentityMismatch, TemporalSessionRestoreError) as error:
        raise NonFifoTemporalAdapterError(
            f"non-FIFO temporal checkpoint fence rejected: {error}"
        ) from error
    return NonFifoTemporalResearchSession(
        planner,
        request,
        session,
        allow_state_bound=True,
    )


def restore_non_fifo_temporal_arrival_bounded_session(
    planner: TemporalLabelAStar,
    checkpoint: NonFifoTemporalResearchCheckpoint,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchSession:
    """Restore an arrival-bounded session after checking envelope completeness."""

    _validate_arrival_bound_certificate(planner, certificate, request=request)
    return restore_non_fifo_temporal_bounded_session(
        planner,
        checkpoint,
        request,
        certificate,
        identity=identity,
    )


def _candidate_result(
    session_id: str,
    candidate: TemporalCandidateResult,
    *,
    allow_state_bound: bool = False,
    allow_heuristic: bool = False,
) -> NonFifoTemporalResearchResult:
    diagnostics = candidate.diagnostics
    if (
        diagnostics.dominance_policy != "none"
        or diagnostics.dominance_scope_match
        or diagnostics.dominance_checks
        or diagnostics.dominance_pruned
        or (not allow_state_bound and diagnostics.state_bound_checks)
        or (not allow_state_bound and diagnostics.state_bound_pruned)
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
    if allow_state_bound and diagnostics.state_bound_rejected:
        violation = NonFifoTemporalSafetyViolation(
            "non-FIFO bounded adapter observed rejected state-bound certificate"
        )
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="state_bound_rejected",
            error=violation,
        )
    if allow_state_bound and diagnostics.state_bound_pruned > diagnostics.state_bound_checks:
        violation = NonFifoTemporalSafetyViolation(
            "non-FIFO bounded adapter observed invalid state-bound counters"
        )
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="state_bound_counter_inconsistent",
            error=violation,
        )
    if allow_heuristic and (
        diagnostics.heuristic_policy != "certified"
        or not diagnostics.heuristic_scope_match
        or diagnostics.heuristic_rejected
    ):
        violation = NonFifoTemporalSafetyViolation(
            "certified heuristic adapter observed an unauthorized or rejected heuristic"
        )
        return _failure(
            NonFifoSearchStatus.EVALUATOR_FAILURE,
            session_id,
            diagnostics,
            reason="heuristic_rejected",
            error=violation,
        )
    return NonFifoTemporalResearchResult(
        status=NonFifoSearchStatus.GOAL_FOUND,
        candidate=candidate,
        session_id=session_id,
        semantic_digest=_route_semantic_digest(candidate.planning_result),
        diagnostics=diagnostics,
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

    try:
        research_session = create_non_fifo_temporal_session(
            planner,
            request,
            identity=identity,
        )
    except NonFifoTemporalAdapterError:
        raise
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

    return research_session.run()


def run_non_fifo_temporal_certified_heuristic_search(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalHeuristicCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchResult:
    """Run exact-arrival non-FIFO search with certified ordering only.

    Unlike the ordinary adapter this path permits ``use_heuristic=True`` only
    when the planner carries a complete admissible/consistent certificate.  It
    still forbids dominance and state-bound pruning; the heuristic changes the
    queue order, not the set of retained labels.
    """

    try:
        research_session = create_non_fifo_temporal_certified_heuristic_session(
            planner,
            request,
            certificate,
            identity=identity,
        )
    except NonFifoTemporalAdapterError:
        raise
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

    return research_session.run()


def run_non_fifo_temporal_bounded_search(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchResult:
    """Run actual non-FIFO search with one explicit proof-carrying bound.

    This opt-in function is intentionally separate from
    :func:`run_non_fifo_temporal_search`.  It never enables temporal
    dominance and it reports a rejected certificate as a failed research
    result rather than treating an unbounded run as a bounded success.
    """

    try:
        research_session = create_non_fifo_temporal_bounded_session(
            planner,
            request,
            certificate,
            identity=identity,
        )
    except NonFifoTemporalAdapterError:
        raise
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

    return research_session.run()


def run_non_fifo_temporal_arrival_bounded_search(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    certificate: TemporalStateBoundCertificate,
    *,
    identity: TemporalSessionIdentity | None = None,
) -> NonFifoTemporalResearchResult:
    """Run the explicit bounded adapter with a complete arrival envelope."""

    try:
        research_session = create_non_fifo_temporal_arrival_bounded_session(
            planner,
            request,
            certificate,
            identity=identity,
        )
    except NonFifoTemporalAdapterError:
        raise
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

    return research_session.run()


def _validate_research_mode(
    planner: TemporalLabelAStar,
    request: PlanningRequest,
    identity: TemporalSessionIdentity | None,
    *,
    allow_state_bound: bool = False,
    allow_heuristic: bool = False,
) -> None:
    if not isinstance(planner, TemporalLabelAStar):
        raise NonFifoTemporalAdapterError(
            "actual temporal adapter requires a TemporalLabelAStar instance"
        )
    if request.use_heuristic and not allow_heuristic:
        raise NonFifoTemporalAdapterError("non-FIFO adapter requires request.use_heuristic=False")
    if allow_heuristic and not request.use_heuristic:
        raise NonFifoTemporalAdapterError(
            "certified heuristic adapter requires request.use_heuristic=True"
        )
    if planner.dominance_policy.enabled:
        raise NonFifoTemporalAdapterError(
            "non-FIFO adapter requires TemporalDominancePolicy.disabled()"
        )
    if not allow_state_bound and planner.state_bound_certificate is not None:
        raise NonFifoTemporalAdapterError(
            "non-FIFO adapter does not accept a state-bound certificate"
        )
    if allow_state_bound and planner.state_bound_certificate is None:
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter requires an explicit state-bound certificate"
        )
    if allow_heuristic and planner.heuristic_certificate is None:
        raise NonFifoTemporalAdapterError(
            "certified heuristic adapter requires an explicit heuristic certificate"
        )
    if identity is not None and not isinstance(identity, TemporalSessionIdentity):
        raise NonFifoTemporalAdapterError(
            "identity must be a TemporalSessionIdentity when supplied"
        )


def _validate_bound_certificate(
    planner: TemporalLabelAStar,
    certificate: TemporalStateBoundCertificate,
    *,
    request: PlanningRequest,
) -> None:
    """Validate the immutable certificate/planner pairing before execution."""

    if not isinstance(certificate, TemporalStateBoundCertificate):
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter requires a TemporalStateBoundCertificate"
        )
    installed = planner.state_bound_certificate
    if installed is None:
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter requires the certificate on the planner"
        )
    if installed.digest != certificate.digest:
        raise NonFifoTemporalAdapterError("bounded non-FIFO adapter certificate digest mismatch")
    if not certificate.usable:
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter requires a usable certified bound"
        )
    allowed = tuple(certificate.allowed_nodes)
    excluded = tuple(certificate.excluded_nodes)
    if len(set(allowed)) != len(allowed) or len(set(excluded)) != len(excluded):
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter certificate has duplicate nodes"
        )
    if set(allowed).intersection(excluded):
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter certificate overlaps allowed and excluded nodes"
        )
    universe = {
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    }
    if set(allowed).union(excluded) != universe:
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter certificate does not cover the finite grid"
        )
    if request.start not in set(allowed) or request.goal not in set(allowed):
        raise NonFifoTemporalAdapterError(
            "bounded non-FIFO adapter certificate excludes request endpoints"
        )


def _validate_arrival_bound_certificate(
    planner: TemporalLabelAStar,
    certificate: TemporalStateBoundCertificate,
    *,
    request: PlanningRequest,
) -> None:
    """Require a complete per-node envelope before arrival-level pruning."""

    _validate_bound_certificate(planner, certificate, request=request)
    if not certificate.arrival_bound_complete:
        raise NonFifoTemporalAdapterError(
            "arrival-bounded adapter requires a complete arrival envelope"
        )


def _validate_heuristic_certificate(
    planner: TemporalLabelAStar,
    certificate: TemporalHeuristicCertificate,
    *,
    request: PlanningRequest,
) -> None:
    """Validate the explicit lower-bound heuristic before starting a search."""

    if not isinstance(certificate, TemporalHeuristicCertificate):
        raise NonFifoTemporalAdapterError(
            "certified heuristic adapter requires a TemporalHeuristicCertificate"
        )
    installed = planner.heuristic_certificate
    if installed is None:
        raise NonFifoTemporalAdapterError(
            "certified heuristic adapter requires the certificate on the planner"
        )
    if installed.digest != certificate.digest:
        raise NonFifoTemporalAdapterError("certified heuristic certificate digest mismatch")
    if not certificate.usable:
        raise NonFifoTemporalAdapterError(
            "certified heuristic adapter requires a usable certified heuristic"
        )
    expected_scope = planner.temporal_scope(request)
    if not certificate.permits(expected_scope):
        raise NonFifoTemporalAdapterError("certified heuristic adapter certificate scope mismatch")
    if certificate.objective != request.objective.value:
        raise NonFifoTemporalAdapterError("certified heuristic adapter objective mismatch")
    universe = {
        (row, column)
        for row in range(planner.grid.shape[0])
        for column in range(planner.grid.shape[1])
    }
    if set(certificate.universe_nodes) != universe:
        raise NonFifoTemporalAdapterError(
            "certified heuristic certificate does not cover the finite grid"
        )
    if request.start not in universe or request.goal not in universe:
        raise NonFifoTemporalAdapterError("certified heuristic request endpoints are invalid")


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
    return _digest(payload)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _jsonable(value),
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
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
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
    "NonFifoTemporalResearchCheckpoint",
    "NonFifoTemporalResearchResult",
    "NonFifoTemporalResearchSession",
    "NonFifoTemporalSafetyViolation",
    "NonFifoTemporalStepEvidence",
    "create_non_fifo_temporal_arrival_bounded_session",
    "create_non_fifo_temporal_bounded_session",
    "create_non_fifo_temporal_certified_heuristic_session",
    "create_non_fifo_temporal_session",
    "restore_non_fifo_temporal_arrival_bounded_session",
    "restore_non_fifo_temporal_bounded_session",
    "restore_non_fifo_temporal_certified_heuristic_session",
    "restore_non_fifo_temporal_session",
    "run_non_fifo_temporal_arrival_bounded_search",
    "run_non_fifo_temporal_bounded_search",
    "run_non_fifo_temporal_certified_heuristic_search",
    "run_non_fifo_temporal_search",
]
