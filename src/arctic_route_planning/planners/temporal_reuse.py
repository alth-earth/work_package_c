"""Internal same-goal certificate reuse for the temporal candidate planner.

This module is deliberately private to Work Package C.  It is not imported by
``planners.__init__`` and it does not change the formal planner, ingress, or
layered contracts.  A reuse hit is a proof-carrying read of an already
finished temporal session: it never advances a session and never evaluates an
edge.  When the proof cannot be established, callers receive an explicit
``FALLBACK_CONTROL`` outcome and may run a fresh control search.

The source query is allowed to be wider than the target query only for the
two constraints represented by :class:`PlanningRequest`: ``maximum_elapsed``
and ``maximum_risk``.  Every other identity component remains exact.  This is
safe because a source optimum which itself satisfies the narrower target
constraints is also an optimum in that target subset.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from heapq import heappop
from math import isfinite
from typing import TYPE_CHECKING, Any

from arctic_route_planning.errors import PlanningCancelled

from .temporal_session import (
    TemporalSessionCheckpoint,
    TemporalSessionIdentity,
    TemporalSessionIdentityMismatch,
    TemporalSessionRestoreError,
    TemporalSessionState,
    checkpoint_session,
)

if TYPE_CHECKING:
    from .temporal_label_astar import TemporalCandidateResult
    from .temporal_session import TemporalSession
    from .time_dependent_astar import PlanningRequest


_CERTIFICATE_EPSILON = 1e-12
_CONSTRAINT_FIELDS = frozenset({"maximum_elapsed_seconds", "maximum_risk"})
_UNSET = object()


class TemporalReuseStatus(StrEnum):
    """Observable result of a certificate lookup."""

    HIT_EXACT = "HIT_EXACT"
    HIT_MONOTONIC = "HIT_MONOTONIC"
    MISS_INCOMPATIBLE = "MISS_INCOMPATIBLE"
    COLD_CANDIDATE = "COLD_CANDIDATE"
    FALLBACK_CONTROL = "FALLBACK_CONTROL"
    # Short alias retained for callers that use the noun rather than the
    # explicit control-search status.  Both values serialize identically.
    FALLBACK = "FALLBACK_CONTROL"


class TemporalCertificateStatus(StrEnum):
    """Lifecycle status of a goal certificate."""

    CERTIFIED_REUSABLE = "CERTIFIED_REUSABLE"


class TemporalReuseReason(StrEnum):
    """Stable diagnostic reasons for a non-hit."""

    NO_CERTIFICATE = "NO_CERTIFICATE"
    CERTIFICATE_ONLY = "CERTIFICATE_ONLY"
    CERTIFICATE_INVALID = "CERTIFICATE_INVALID"
    SESSION_NOT_CERTIFIED = "SESSION_NOT_CERTIFIED"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    CONSTRAINT_WIDENING = "CONSTRAINT_WIDENING"
    ROUTE_VIOLATES_TARGET = "ROUTE_VIOLATES_TARGET"
    UNSUPPORTED_CUMULATIVE_RISK = "UNSUPPORTED_CUMULATIVE_RISK"
    IDENTITY_UNAVAILABLE = "IDENTITY_UNAVAILABLE"


class TemporalOpenTermination(StrEnum):
    """How the source session's OPEN set completed its proof."""

    OPEN_BOUND = "OPEN_BOUND"
    OPEN_EMPTY = "OPEN_EMPTY"


class TemporalReuseCertificateError(RuntimeError):
    """The source session cannot supply a sound reusable certificate."""


def _json_value(value: Any) -> Any:
    """Build a deterministic, process-independent semantic value."""

    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list, frozenset, set)):
        items = [_json_value(item) for item in value]
        if isinstance(value, (frozenset, set)):
            return sorted(items, key=lambda item: repr(item))
        return items
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float) and not isfinite(value):
        return {"non_finite": str(value)}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _canonical_value(value: Any) -> str:
    # Importing the session module's canonicalizer would couple the certificate
    # digest to a private implementation detail.  The small local serializer
    # intentionally has the same UTC/dataclass semantics.
    import json

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_canonical_value(value).encode("utf-8")).hexdigest()


def _as_seconds(value: timedelta | float | int | None) -> float | None:
    if value is None:
        return None
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    if not isfinite(seconds) or seconds <= 0:
        raise ValueError("maximum_elapsed must be a finite positive duration")
    return seconds


def _validate_risk(value: float | None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not isfinite(value) or not 0 <= value <= 1:
        raise ValueError("maximum_risk must be finite and in [0, 1]")
    return value


@dataclass(frozen=True, slots=True)
class TemporalReuseConstraints:
    """Only the two request constraints that P2 can monotonically tighten."""

    maximum_elapsed_seconds: float | None = None
    maximum_risk: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "maximum_elapsed_seconds",
            _as_seconds(self.maximum_elapsed_seconds),
        )
        object.__setattr__(self, "maximum_risk", _validate_risk(self.maximum_risk))

    @classmethod
    def from_identity(cls, identity: TemporalSessionIdentity) -> TemporalReuseConstraints:
        return cls(identity.maximum_elapsed_seconds, identity.maximum_risk)

    @classmethod
    def from_request(cls, request: PlanningRequest) -> TemporalReuseConstraints:
        return cls(request.maximum_elapsed, request.maximum_risk)

    @property
    def maximum_elapsed(self) -> timedelta | None:
        if self.maximum_elapsed_seconds is None:
            return None
        return timedelta(seconds=self.maximum_elapsed_seconds)


@dataclass(frozen=True, slots=True)
class TemporalGoalCertificate:
    """Recomputable proof that a terminal temporal session is reusable.

    ``upper_bound`` is the incumbent ``U``.  ``lower_bound`` is the first
    non-stale OPEN priority, or ``None`` when OPEN is empty.  The state and
    route digests are independent: the former protects the search proof and
    the latter protects the returned business route semantics while ignoring
    runtime-only metrics such as ``compute_ms``.
    """

    identity: TemporalSessionIdentity
    source_constraints: TemporalReuseConstraints
    upper_bound: float
    lower_bound: float | None
    epsilon: float
    open_termination: TemporalOpenTermination
    state_digest: str
    route_digest: str
    route_elapsed_seconds: float
    route_maximum_risk: float
    status: TemporalCertificateStatus = TemporalCertificateStatus.CERTIFIED_REUSABLE
    certificate_digest: str = ""
    # Keeping the immutable checkpoint with the proof makes a certificate
    # directly reusable while still allowing ``TemporalCertifiedGoal`` to be
    # the explicit result+proof carrier.  These fields never enter equality
    # or the public semantic digest.
    checkpoint: TemporalSessionCheckpoint | None = None
    result: Any = None

    def __post_init__(self) -> None:
        expected = _digest(self._seal_payload())
        if self.certificate_digest and self.certificate_digest != expected:
            raise TemporalReuseCertificateError("certificate digest mismatch")
        object.__setattr__(self, "certificate_digest", expected)

    def _seal_payload(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "source_constraints": self.source_constraints,
            "upper_bound": self.upper_bound,
            "lower_bound": self.lower_bound,
            "epsilon": self.epsilon,
            "open_termination": self.open_termination,
            "state_digest": self.state_digest,
            "route_digest": self.route_digest,
            "route_elapsed_seconds": self.route_elapsed_seconds,
            "route_maximum_risk": self.route_maximum_risk,
            "status": self.status,
        }

    @classmethod
    def from_session(cls, session: TemporalSession) -> TemporalGoalCertificate:
        """Create and independently validate a certificate from a terminal session."""

        return cls._from_checkpoint(checkpoint_session(session), keep_snapshot=True)

    @classmethod
    def recompute(cls, session: TemporalSession) -> TemporalGoalCertificate:
        """Recompute all proof fields from the session's current snapshot."""

        return cls.from_session(session)

    @classmethod
    def _from_checkpoint(
        cls,
        checkpoint: TemporalSessionCheckpoint,
        *,
        keep_snapshot: bool,
    ) -> TemporalGoalCertificate:
        checkpoint.assert_valid()
        if checkpoint.state is not TemporalSessionState.GOAL_CERTIFIED:
            raise TemporalReuseCertificateError(
                "only a GOAL_CERTIFIED session can produce a goal certificate"
            )
        if checkpoint.result is None:
            raise TemporalReuseCertificateError("goal-certified session has no result")
        checkpoint.identity.assert_complete()

        planning_result = _planning_result(checkpoint.result)
        if planning_result.objective != checkpoint.identity.objective:
            raise TemporalReuseCertificateError("result objective does not match session identity")
        nodes = planning_result.nodes
        if (
            not nodes
            or nodes[0] != checkpoint.identity.start
            or nodes[-1] != checkpoint.identity.goal
        ):
            raise TemporalReuseCertificateError("result endpoints do not match session identity")

        upper_bound = float(checkpoint.incumbent_cost)
        result_cost = float(planning_result.total_cost_hours)
        if not isfinite(upper_bound) or not isfinite(result_cost):
            raise TemporalReuseCertificateError("goal certificate has a non-finite upper bound")
        if abs(upper_bound - result_cost) > _CERTIFICATE_EPSILON:
            raise TemporalReuseCertificateError("incumbent and result costs disagree")

        labels = dict(checkpoint.labels)
        open_entry = _first_valid_open(checkpoint.queue, labels)
        if open_entry is None:
            lower_bound = None
            termination = TemporalOpenTermination.OPEN_EMPTY
        else:
            lower_bound = float(open_entry[0])
            if not isfinite(lower_bound):
                raise TemporalReuseCertificateError("OPEN lower bound is non-finite")
            termination = TemporalOpenTermination.OPEN_BOUND
            if upper_bound > lower_bound + _CERTIFICATE_EPSILON:
                raise TemporalReuseCertificateError(
                    "goal-certified session does not satisfy its OPEN lower-bound proof"
                )

        route_elapsed = _route_elapsed_seconds(planning_result, checkpoint.identity.departure_time)
        route_maximum_risk = float(planning_result.maximum_risk)
        if not isfinite(route_elapsed) or route_elapsed < -_CERTIFICATE_EPSILON:
            raise TemporalReuseCertificateError("route elapsed time is invalid")
        if not isfinite(route_maximum_risk) or not 0 <= route_maximum_risk <= 1:
            raise TemporalReuseCertificateError("route maximum risk is invalid")

        source_constraints = TemporalReuseConstraints.from_identity(checkpoint.identity)
        _assert_route_fits(route_elapsed, route_maximum_risk, source_constraints)
        certificate = cls(
            identity=checkpoint.identity,
            source_constraints=source_constraints,
            upper_bound=upper_bound,
            lower_bound=lower_bound,
            epsilon=_CERTIFICATE_EPSILON,
            open_termination=termination,
            state_digest=checkpoint.state_digest,
            route_digest=route_semantic_digest(checkpoint.result),
            route_elapsed_seconds=route_elapsed,
            route_maximum_risk=route_maximum_risk,
            checkpoint=checkpoint if keep_snapshot else None,
            result=checkpoint.result if keep_snapshot else None,
        )
        return certificate

    def assert_valid(self) -> None:
        """Recalculate the stored proof and reject tampered snapshots."""

        if self.status is not TemporalCertificateStatus.CERTIFIED_REUSABLE:
            raise TemporalReuseCertificateError("certificate is not reusable")
        if self.certificate_digest != _digest(self._seal_payload()):
            raise TemporalReuseCertificateError("certificate digest mismatch")
        if self.checkpoint is None or self.result is None:
            raise TemporalReuseCertificateError("certificate has no reusable result snapshot")
        if self.result is not self.checkpoint.result and (
            route_semantic_digest(self.result) != route_semantic_digest(self.checkpoint.result)
        ):
            raise TemporalReuseCertificateError("certificate result digest mismatch")
        rebuilt = self._from_checkpoint(self.checkpoint, keep_snapshot=False)
        if self.proof_fields != rebuilt.proof_fields:
            raise TemporalReuseCertificateError("certificate proof digest mismatch")

    @property
    def proof_fields(self) -> tuple[Any, ...]:
        return (
            self.identity,
            self.source_constraints,
            self.upper_bound,
            self.lower_bound,
            self.epsilon,
            self.open_termination,
            self.state_digest,
            self.route_digest,
            self.route_elapsed_seconds,
            self.route_maximum_risk,
            self.status,
            self.certificate_digest,
        )

    @property
    def U(self) -> float:
        """Notation used by the P2 specification for the incumbent."""

        return self.upper_bound

    @property
    def LB(self) -> float | None:
        """Notation used by the P2 specification for the OPEN lower bound."""

        return self.lower_bound

    @property
    def open_lower_bound(self) -> float:
        """Use ``+inf`` as a numeric view of ``OPEN_EMPTY``'s absent LB."""

        return float("inf") if self.lower_bound is None else self.lower_bound

    @property
    def certificate_status(self) -> str:
        return self.status.value

    @property
    def session_digest(self) -> str:
        return self.state_digest

    @property
    def digest(self) -> str:
        return self.certificate_digest


@dataclass(frozen=True, slots=True)
class TemporalCertifiedGoal:
    """Immutable result-plus-certificate carrier used by the reuse API."""

    certificate: TemporalGoalCertificate
    result: TemporalCandidateResult
    checkpoint: TemporalSessionCheckpoint

    @classmethod
    def from_session(cls, session: TemporalSession) -> TemporalCertifiedGoal:
        checkpoint = checkpoint_session(session)
        certificate = TemporalGoalCertificate._from_checkpoint(checkpoint, keep_snapshot=True)
        return cls(certificate, checkpoint.result, checkpoint)

    @classmethod
    def from_certificate(cls, certificate: TemporalGoalCertificate) -> TemporalCertifiedGoal:
        certificate.assert_valid()
        assert certificate.checkpoint is not None  # guarded by assert_valid
        assert certificate.result is not None
        return cls(certificate, certificate.result, certificate.checkpoint)

    def __post_init__(self) -> None:
        if self.checkpoint.state is not TemporalSessionState.GOAL_CERTIFIED:
            raise TemporalReuseCertificateError("certified goal checkpoint is not terminal")
        if self.checkpoint.identity != self.certificate.identity:
            raise TemporalReuseCertificateError("certified goal identity mismatch")
        if self.checkpoint.result is not self.result and (
            route_semantic_digest(self.checkpoint.result) != route_semantic_digest(self.result)
        ):
            raise TemporalReuseCertificateError("certified goal route mismatch")
        self.certificate.assert_valid()

    def assert_valid(self) -> None:
        self.certificate.assert_valid()
        self.checkpoint.assert_valid()

    def recompute_certificate(self) -> TemporalGoalCertificate:
        self.assert_valid()
        return TemporalGoalCertificate._from_checkpoint(self.checkpoint, keep_snapshot=True)

    @property
    def route_digest(self) -> str:
        return self.certificate.route_digest


@dataclass(frozen=True, slots=True)
class TemporalReuseOutcome:
    """Explicit hit/fallback result; no failure is hidden as a cache miss."""

    status: TemporalReuseStatus
    result: Any | None
    certificate: TemporalGoalCertificate | None
    reason: TemporalReuseReason | str
    used_search: bool = False

    @property
    def hit(self) -> bool:
        return self.status in (
            TemporalReuseStatus.HIT_EXACT,
            TemporalReuseStatus.HIT_MONOTONIC,
        )

    @property
    def reused(self) -> bool:
        return self.hit and not self.used_search

    @property
    def fallback(self) -> bool:
        return self.status is TemporalReuseStatus.FALLBACK_CONTROL

    @property
    def miss(self) -> bool:
        return self.status is TemporalReuseStatus.MISS_INCOMPATIBLE

    @property
    def cold_candidate(self) -> bool:
        return self.status is TemporalReuseStatus.COLD_CANDIDATE

    @property
    def fallback_reason(self) -> str:
        return self.reason.value if isinstance(self.reason, StrEnum) else str(self.reason)


def route_semantic_digest(result: Any) -> str:
    """Digest route semantics while excluding runtime-only search metrics."""

    planning_result = _planning_result(result)
    return _digest(
        {
            "objective": planning_result.objective,
            "steps": planning_result.steps,
            "total_cost_hours": planning_result.total_cost_hours,
            "distance_km": planning_result.distance_km,
            "travel_hours": planning_result.travel_hours,
            "average_risk": planning_result.average_risk,
            "maximum_risk": planning_result.maximum_risk,
            "minimum_confidence": planning_result.minimum_confidence,
            "source_risk_ids": planning_result.source_risk_ids,
        }
    )


def certify_goal(session: TemporalSession) -> TemporalGoalCertificate:
    """Functional alias for :meth:`TemporalGoalCertificate.from_session`."""

    return TemporalGoalCertificate.from_session(session)


def certify_session(session: TemporalSession) -> TemporalCertifiedGoal:
    """Build the reusable result-plus-proof carrier for a session."""

    return TemporalCertifiedGoal.from_session(session)


def try_reuse(
    certified: TemporalCertifiedGoal | TemporalGoalCertificate | None,
    planner: Any,
    request: PlanningRequest,
    *,
    maximum_elapsed: timedelta | float | int | object | None = _UNSET,
    max_elapsed_time: timedelta | float | int | object | None = _UNSET,
    maximum_risk: float | object | None = _UNSET,
    max_cumulative_risk: float | object | None = _UNSET,
) -> TemporalReuseOutcome:
    """Attempt a no-search certificate hit.

    ``max_cumulative_risk`` is accepted only to fail closed for callers using
    the earlier research wording.  C's current formal request has
    ``maximum_risk`` (per-sample hard threshold), not a cumulative-risk
    constraint; silently treating the two as equivalent would invalidate the
    identity fence.
    """

    _check_cancelled(planner, request)
    effective_request, unsupported = _effective_request(
        request,
        maximum_elapsed=maximum_elapsed,
        max_elapsed_time=max_elapsed_time,
        maximum_risk=maximum_risk,
        max_cumulative_risk=max_cumulative_risk,
    )
    if unsupported:
        return _fallback(TemporalReuseReason.UNSUPPORTED_CUMULATIVE_RISK, certified)
    if certified is None:
        return _fallback(TemporalReuseReason.NO_CERTIFICATE, None)

    try:
        if isinstance(certified, TemporalCertifiedGoal):
            certified.assert_valid()
            certificate = certified.certificate
            result = certified.result
        elif isinstance(certified, TemporalGoalCertificate):
            certified.assert_valid()
            certificate = certified
            result = certified.result
        else:
            return _fallback(TemporalReuseReason.CERTIFICATE_INVALID, None)
    except (
        TemporalReuseCertificateError,
        TemporalSessionIdentityMismatch,
        TemporalSessionRestoreError,
        ValueError,
        TypeError,
    ):
        return _fallback(TemporalReuseReason.CERTIFICATE_INVALID, certified)

    if result is None:
        return _fallback(TemporalReuseReason.CERTIFICATE_ONLY, certificate)

    try:
        target_identity = _target_identity(planner, effective_request, certificate.identity)
    except (TemporalSessionIdentityMismatch, ValueError, TypeError) as error:
        return _fallback(f"{TemporalReuseReason.IDENTITY_UNAVAILABLE}: {error}", certificate)
    if not _same_identity_except_constraints(certificate.identity, target_identity):
        return _fallback(TemporalReuseReason.IDENTITY_MISMATCH, certificate)

    target_constraints = TemporalReuseConstraints.from_request(effective_request)
    source_constraints = certificate.source_constraints
    if not _is_tightening(source_constraints, target_constraints):
        return _fallback(TemporalReuseReason.CONSTRAINT_WIDENING, certificate)
    try:
        _assert_route_fits(
            certificate.route_elapsed_seconds,
            certificate.route_maximum_risk,
            target_constraints,
        )
    except TemporalReuseCertificateError:
        return _fallback(TemporalReuseReason.ROUTE_VIOLATES_TARGET, certificate)

    exact = source_constraints == target_constraints
    return TemporalReuseOutcome(
        status=(TemporalReuseStatus.HIT_EXACT if exact else TemporalReuseStatus.HIT_MONOTONIC),
        result=result,
        certificate=certificate,
        reason=("EXACT_IDENTITY" if exact else "MONOTONIC_TIGHTENING"),
        used_search=False,
    )


def reuse_or_plan(
    certified: TemporalCertifiedGoal | TemporalGoalCertificate | None,
    planner: Any,
    request: PlanningRequest,
    *,
    maximum_elapsed: timedelta | float | int | object | None = _UNSET,
    max_elapsed_time: timedelta | float | int | object | None = _UNSET,
    maximum_risk: float | object | None = _UNSET,
    max_cumulative_risk: float | object | None = _UNSET,
    fallback_planner: Any | None = None,
) -> TemporalReuseOutcome:
    """Reuse when proven, otherwise run one explicit scratch control search.

    Cancellation is checked before any fallback decision and is never changed
    into a fallback status.  Other candidate rejection reasons remain visible
    in the returned ``COLD_CANDIDATE`` outcome.  A
    ``FALLBACK_CONTROL`` status is used only when the caller supplies a
    separate explicit ``fallback_planner``.
    """

    _check_cancelled(planner, request)
    effective_request, unsupported = _effective_request(
        request,
        maximum_elapsed=maximum_elapsed,
        max_elapsed_time=max_elapsed_time,
        maximum_risk=maximum_risk,
        max_cumulative_risk=max_cumulative_risk,
    )
    if unsupported:
        # There is no cumulative-risk field in PlanningRequest, so neither the
        # candidate nor the control planner can safely enforce this research
        # constraint.  Stop before starting a search rather than returning a
        # route that could violate the caller's requested bound.
        return _fallback(TemporalReuseReason.UNSUPPORTED_CUMULATIVE_RISK, certified)
    outcome = try_reuse(certified, planner, effective_request)
    if outcome.hit:
        return outcome

    # The check is repeated immediately before the scratch run so a request
    # cancelled during identity/reuse validation cannot start new work.
    _check_cancelled(planner, effective_request)
    control = fallback_planner is not None
    search_planner = fallback_planner if control else planner
    _check_cancelled(search_planner, effective_request)
    result = search_planner.plan(effective_request)
    return TemporalReuseOutcome(
        status=(
            TemporalReuseStatus.FALLBACK_CONTROL
            if control
            else TemporalReuseStatus.COLD_CANDIDATE
        ),
        result=result,
        certificate=outcome.certificate,
        reason=outcome.reason,
        used_search=True,
    )


# Explicit names make the internal API discoverable without exposing it from
# the public package namespace.
reuse_goal = try_reuse
reuse_temporal_goal = try_reuse
reuse_goal_or_plan = reuse_or_plan


def _effective_request(
    request: PlanningRequest,
    *,
    maximum_elapsed: timedelta | float | int | object | None,
    max_elapsed_time: timedelta | float | int | object | None,
    maximum_risk: float | object | None,
    max_cumulative_risk: float | object | None,
) -> tuple[PlanningRequest, bool]:
    if max_cumulative_risk is not _UNSET:
        # Validate the supplied research-side value so NaN/negative values do
        # not turn into an ambiguous fallback reason.
        if max_cumulative_risk is not None:
            value = float(max_cumulative_risk)
            if not isfinite(value) or value < 0:
                raise ValueError("max_cumulative_risk must be finite and non-negative")
        return request, True

    elapsed = maximum_elapsed
    if (
        elapsed is not _UNSET
        and max_elapsed_time is not _UNSET
        and _as_seconds(elapsed) != _as_seconds(max_elapsed_time)
    ):
        raise ValueError("maximum_elapsed and max_elapsed_time disagree")
    if elapsed is _UNSET:
        elapsed = max_elapsed_time
    if elapsed is _UNSET:
        elapsed = request.maximum_elapsed
    else:
        elapsed = _as_seconds(elapsed)
        elapsed = timedelta(seconds=elapsed) if elapsed is not None else None

    risk = request.maximum_risk if maximum_risk is _UNSET else _validate_risk(maximum_risk)
    return replace(request, maximum_elapsed=elapsed, maximum_risk=risk), False


def _target_identity(
    planner: Any,
    request: PlanningRequest,
    source_identity: TemporalSessionIdentity,
) -> TemporalSessionIdentity:
    committed_digest = (
        source_identity.risk_window_content_digest
        if source_identity.risk_window_digest_kind == "committed_window_v1"
        else None
    )
    committed_id = (
        source_identity.risk_window_commit_id
        if source_identity.risk_window_digest_kind == "committed_window_v1"
        else None
    )
    return TemporalSessionIdentity.from_planner(
        planner,
        request,
        input_revision=source_identity.input_revision,
        risk_window_content_digest=committed_digest,
        risk_window_commit_id=committed_id,
    )


def _same_identity_except_constraints(
    source: TemporalSessionIdentity,
    target: TemporalSessionIdentity,
) -> bool:
    return all(
        getattr(source, field.name) == getattr(target, field.name)
        for field in fields(TemporalSessionIdentity)
        if field.name not in _CONSTRAINT_FIELDS
    )


def _is_tightening(
    source: TemporalReuseConstraints,
    target: TemporalReuseConstraints,
) -> bool:
    return _is_tighter_or_equal(
        source.maximum_elapsed_seconds,
        target.maximum_elapsed_seconds,
    ) and _is_tighter_or_equal(
        source.maximum_risk,
        target.maximum_risk,
    )


def _is_tighter_or_equal(source: float | None, target: float | None) -> bool:
    # None is an unbounded source/target.  A finite target is therefore a
    # tightening of an unbounded source, while an unbounded target widens a
    # finite source and is not eligible for reuse.
    if source is None:
        return True
    if target is None:
        return False
    # Constraint order is deliberately exact.  Treating a slightly wider
    # target as a tightening would make a source optimum unsound for the
    # newly admitted boundary cases.
    return target <= source


def _assert_route_fits(
    route_elapsed_seconds: float,
    route_maximum_risk: float,
    constraints: TemporalReuseConstraints,
) -> None:
    if constraints.maximum_elapsed_seconds is not None and (
        route_elapsed_seconds > constraints.maximum_elapsed_seconds
    ):
        raise TemporalReuseCertificateError("certified route exceeds maximum_elapsed")
    if constraints.maximum_risk is not None and (
        route_maximum_risk > constraints.maximum_risk
    ):
        raise TemporalReuseCertificateError("certified route exceeds maximum_risk")


def _route_elapsed_seconds(result: Any, departure_time: datetime | None) -> float:
    if departure_time is None:
        raise TemporalReuseCertificateError("identity has no departure time")
    last_eta = result.steps[-1].eta
    return (last_eta - departure_time).total_seconds()


def _planning_result(result: Any) -> Any:
    planning_result = getattr(result, "planning_result", result)
    if not hasattr(planning_result, "steps") or not hasattr(planning_result, "nodes"):
        raise TemporalReuseCertificateError("result is not a temporal planning result")
    return planning_result


def _first_valid_open(
    queue: tuple[Any, ...],
    labels: Mapping[Any, float],
) -> tuple[Any, ...] | None:
    pending = list(queue)
    while pending:
        entry = heappop(pending)
        if len(entry) < 2:
            raise TemporalReuseCertificateError("malformed OPEN entry")
        state = entry[-1]
        queued_cost = entry[1]
        current_cost = labels.get(state)
        if current_cost is not None and queued_cost == current_cost:
            return entry
    return None


def _check_cancelled(planner: Any, request: PlanningRequest) -> None:
    checker = getattr(planner, "_check_cancelled", None)
    if checker is not None:
        checker(request)
    elif request.cancel_check is not None and request.cancel_check():
        raise PlanningCancelled("planning request was cancelled")


def _fallback(
    reason: TemporalReuseReason | str,
    certified: TemporalCertifiedGoal | TemporalGoalCertificate | None,
) -> TemporalReuseOutcome:
    certificate = None
    if isinstance(certified, TemporalCertifiedGoal):
        certificate = certified.certificate
    elif isinstance(certified, TemporalGoalCertificate):
        certificate = certified
    return TemporalReuseOutcome(
        status=TemporalReuseStatus.MISS_INCOMPATIBLE,
        result=None,
        certificate=certificate,
        reason=reason,
        used_search=False,
    )


__all__ = [
    "TemporalCertificateStatus",
    "TemporalCertifiedGoal",
    "TemporalGoalCertificate",
    "TemporalOpenTermination",
    "TemporalReuseCertificateError",
    "TemporalReuseConstraints",
    "TemporalReuseOutcome",
    "TemporalReuseReason",
    "TemporalReuseStatus",
    "certify_goal",
    "certify_session",
    "reuse_goal",
    "reuse_goal_or_plan",
    "reuse_or_plan",
    "reuse_temporal_goal",
    "route_semantic_digest",
    "try_reuse",
]
