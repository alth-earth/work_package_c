"""Fail-closed qualification and dominance certificates for temporal labels.

This module is intentionally internal to work package C.  A FIFO result is a
finite-domain qualification record, not a proof about an arbitrary ocean
model.  A dominance policy requires both that qualification and an explicit
suffix-monotonicity/coverage assertion; FIFO alone is never sufficient to
discard an exact-arrival label.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from arctic_route_planning.contracts.codec import risk_frame_content_digest


class FifoStatus(StrEnum):
    """Finite-domain result of the FIFO qualification pass."""

    FIFO_CERTIFIED = "FIFO_CERTIFIED"
    FIFO_VIOLATED = "FIFO_VIOLATED"
    FIFO_UNCERTAIN = "FIFO_UNCERTAIN"


class DominanceMode(StrEnum):
    """Internal temporal-label pruning modes."""

    NONE = "none"
    CERTIFIED_ONLY = "certified_only"


def _jsonable(value: Any) -> Any:
    """Convert scope values to deterministic JSON primitives.

    Process-local object identities and ``repr`` are deliberately excluded;
    callers must provide stable primitive identity fields for certificates.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("certificate datetimes must be timezone-aware")
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
        return sorted((_jsonable(item) for item in value), key=_canonical_json)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("certificate scope values must be finite")
        return value
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 digest used by qualification identities."""

    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TemporalScope:
    """Immutable identity fence for one finite temporal qualification domain."""

    values: tuple[tuple[str, Any], ...]
    schema_version: str = "c.temporal-scope.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "c.temporal-scope.v1":
            raise ValueError("unsupported temporal scope schema")
        normalized = tuple(
            sorted(
                ((str(key), _jsonable(value)) for key, value in self.values),
                key=lambda pair: pair[0],
            )
        )
        if len({key for key, _ in normalized}) != len(normalized):
            raise ValueError("temporal scope keys must be unique")
        object.__setattr__(self, "values", normalized)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any] | TemporalScope) -> TemporalScope:
        if isinstance(values, cls):
            return values
        return cls(tuple(values.items()))

    @property
    def mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(dict(self.values))

    @property
    def digest(self) -> str:
        return canonical_digest(
            {"schema_version": self.schema_version, "values": self.values}
        )

    def matches(self, other: Mapping[str, Any] | TemporalScope) -> bool:
        return self.digest == TemporalScope.from_mapping(other).digest

    @property
    def evaluator_identity_known(self) -> bool:
        """Whether an injected edge evaluator has a stable identity.

        A scope assembled by :func:`temporal_scope_from_planner` records the
        evaluator identity under ``edge_evaluator_digest``.  A value marked
        ``unknown:`` is deliberately not sufficient for a dominance
        certificate: two callables with the same type can still have
        different mutable behaviour.  Hand-built test scopes which omit the
        optional field retain their historical semantics.
        """

        value = self.mapping.get("edge_evaluator_digest")
        return not (isinstance(value, str) and value.startswith("unknown:"))


@dataclass(frozen=True, slots=True)
class FifoCounterexample:
    """The first finite-domain FIFO violation observed by the classifier."""

    edge_id: Any
    earlier_departure: datetime
    earlier_arrival: datetime
    later_departure: datetime
    later_arrival: datetime
    slack_seconds: float

    def __post_init__(self) -> None:
        for name in (
            "earlier_departure",
            "earlier_arrival",
            "later_departure",
            "later_arrival",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), field=name))
        if not isfinite(self.slack_seconds):
            raise ValueError("FIFO counterexample slack must be finite")


@dataclass(frozen=True, slots=True)
class FifoCertificate:
    """Auditable finite-domain FIFO qualification result."""

    status: FifoStatus
    scope: TemporalScope
    edge_ids: tuple[Any, ...]
    probe_times: tuple[datetime, ...]
    tolerance_seconds: float
    probes_evaluated: int
    minimum_slack_seconds: float | None = None
    counterexample: FifoCounterexample | None = None
    reason: str | None = None
    schema_version: str = "c.temporal-fifo-certificate.v1"

    def __post_init__(self) -> None:
        status = FifoStatus(self.status)
        object.__setattr__(self, "status", status)
        object.__setattr__(
            self,
            "scope",
            TemporalScope.from_mapping(self.scope),
        )
        object.__setattr__(self, "edge_ids", tuple(self.edge_ids))
        object.__setattr__(
            self,
            "probe_times",
            tuple(sorted({_utc(value, field="probe_time") for value in self.probe_times})),
        )
        if self.schema_version != "c.temporal-fifo-certificate.v1":
            raise ValueError("unsupported FIFO certificate schema")
        if not isfinite(self.tolerance_seconds) or self.tolerance_seconds < 0:
            raise ValueError("FIFO tolerance must be finite and non-negative")
        if isinstance(self.probes_evaluated, bool) or self.probes_evaluated < 0:
            raise ValueError("FIFO probe count must be non-negative")
        if self.minimum_slack_seconds is not None and not isfinite(
            self.minimum_slack_seconds
        ):
            raise ValueError("FIFO slack must be finite")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "status": self.status,
                "scope": self.scope.digest,
                "edge_ids": self.edge_ids,
                "probe_times": self.probe_times,
                "tolerance_seconds": self.tolerance_seconds,
                "probes_evaluated": self.probes_evaluated,
                "minimum_slack_seconds": self.minimum_slack_seconds,
                "counterexample": self.counterexample,
                "reason": self.reason,
            }
        )

    @property
    def certificate_digest(self) -> str:
        return self.digest

    @property
    def usable(self) -> bool:
        expected_probes = len(self.edge_ids) * len(self.probe_times)
        return (
            self.status is FifoStatus.FIFO_CERTIFIED
            and bool(self.edge_ids)
            and len(self.probe_times) >= 2
            and self.probes_evaluated == expected_probes
            and self.reason is None
            and self.counterexample is None
        )


@dataclass(frozen=True, slots=True)
class TemporalDominanceCertificate:
    """Certificate combining FIFO with objective-specific suffix closure."""

    fifo_certificate: FifoCertificate
    suffix_monotone: bool
    coverage_complete: bool
    scope: TemporalScope | None = None
    objective: str | None = None
    reason: str | None = None
    schema_version: str = "c.temporal-dominance-certificate.v1"

    def __post_init__(self) -> None:
        if self.schema_version != "c.temporal-dominance-certificate.v1":
            raise ValueError("unsupported temporal dominance certificate schema")
        if not isinstance(self.suffix_monotone, bool) or not isinstance(
            self.coverage_complete, bool
        ):
            raise ValueError("dominance certificate closure flags must be boolean")
        if not isinstance(self.fifo_certificate, FifoCertificate):
            raise TypeError("fifo_certificate must be a FifoCertificate")
        scope = self.scope or self.fifo_certificate.scope
        object.__setattr__(self, "scope", TemporalScope.from_mapping(scope))
        if self.objective is None:
            value = self.scope.mapping.get("objective")
            if value is not None:
                object.__setattr__(self, "objective", str(value))

    @classmethod
    def from_fifo(
        cls,
        fifo_certificate: FifoCertificate,
        *,
        suffix_monotone: bool,
        coverage_complete: bool,
        scope: TemporalScope | Mapping[str, Any] | None = None,
        objective: str | None = None,
        reason: str | None = None,
    ) -> TemporalDominanceCertificate:
        return cls(
            fifo_certificate=fifo_certificate,
            suffix_monotone=suffix_monotone,
            coverage_complete=coverage_complete,
            scope=scope,
            objective=objective,
            reason=reason,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "fifo_digest": self.fifo_certificate.digest,
                "scope": self.scope.digest,
                "objective": self.objective,
                "suffix_monotone": self.suffix_monotone,
                "coverage_complete": self.coverage_complete,
                "reason": self.reason,
            }
        )

    @property
    def usable(self) -> bool:
        scope_objective = self.scope.mapping.get("objective")
        objective_matches = scope_objective is None or self.objective == str(scope_objective)
        return (
            self.fifo_certificate.usable
            and self.fifo_certificate.scope.matches(self.scope)
            and self.fifo_certificate.scope.evaluator_identity_known
            and self.scope.evaluator_identity_known
            and objective_matches
            and self.suffix_monotone
            and self.coverage_complete
        )

    def permits(self, expected_scope: TemporalScope | Mapping[str, Any]) -> bool:
        return self.usable and self.scope.matches(expected_scope)


@dataclass(frozen=True, slots=True)
class TemporalDominancePolicy:
    """Default-off policy used by :class:`TemporalLabelAStar`."""

    mode: DominanceMode = DominanceMode.NONE
    certificate: TemporalDominanceCertificate | None = None

    def __post_init__(self) -> None:
        mode = DominanceMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if mode is DominanceMode.NONE and self.certificate is not None:
            raise ValueError("disabled dominance policy cannot carry a certificate")
        if mode is DominanceMode.CERTIFIED_ONLY and self.certificate is None:
            raise ValueError("certified dominance policy requires a certificate")

    @classmethod
    def disabled(cls) -> TemporalDominancePolicy:
        return cls()

    @classmethod
    def certified_only(
        cls, certificate: TemporalDominanceCertificate
    ) -> TemporalDominancePolicy:
        return cls(DominanceMode.CERTIFIED_ONLY, certificate)

    @property
    def enabled(self) -> bool:
        return self.mode is DominanceMode.CERTIFIED_ONLY

    @property
    def certificate_digest(self) -> str | None:
        return self.certificate.digest if self.certificate is not None else None

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "mode": self.mode,
                "certificate": self.certificate.digest
                if self.certificate is not None
                else None,
            }
        )

    def permits(self, scope: TemporalScope | Mapping[str, Any]) -> bool:
        return bool(self.certificate and self.certificate.permits(scope))

    can_prune = permits


def _coerce_arrival(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    arrival = getattr(value, "arrival_time", None)
    return arrival if isinstance(arrival, datetime) else None


def qualify_fifo(
    edges: Iterable[Any],
    probe_times: Iterable[datetime],
    evaluate_arrival: Callable[[Any, datetime], Any],
    *,
    scope: TemporalScope | Mapping[str, Any] | None = None,
    tolerance_seconds: float = 1e-6,
) -> FifoCertificate:
    """Qualify FIFO over a finite edge/time probe set.

    Evaluation failures, missing coverage, malformed arrivals, and an
    insufficient probe domain return ``FIFO_UNCERTAIN``.  A later departure
    arriving earlier by more than ``tolerance_seconds`` returns
    ``FIFO_VIOLATED`` with a concrete counterexample.
    """

    if not isfinite(tolerance_seconds) or tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be finite and non-negative")
    edge_values = tuple(edges)
    times = tuple(sorted({_utc(value, field="probe_time") for value in probe_times}))
    active_scope = TemporalScope.from_mapping(scope or {"scope": "unbound"})
    if not edge_values:
        return FifoCertificate(
            status=FifoStatus.FIFO_UNCERTAIN,
            scope=active_scope,
            edge_ids=(),
            probe_times=times,
            tolerance_seconds=tolerance_seconds,
            probes_evaluated=0,
            reason="empty_edge_domain",
        )
    if len(times) < 2:
        return FifoCertificate(
            status=FifoStatus.FIFO_UNCERTAIN,
            scope=active_scope,
            edge_ids=edge_values,
            probe_times=times,
            tolerance_seconds=tolerance_seconds,
            probes_evaluated=0,
            reason="insufficient_probe_times",
        )

    evaluated = 0
    minimum_slack: float | None = None
    for edge in edge_values:
        previous_departure: datetime | None = None
        previous_arrival: datetime | None = None
        for departure in times:
            try:
                raw_arrival = evaluate_arrival(edge, departure)
                arrival = _coerce_arrival(raw_arrival)
                if arrival is None:
                    raise ValueError("arrival evaluator returned no datetime")
                arrival = _utc(arrival, field="arrival_time")
            except Exception as error:
                return FifoCertificate(
                    status=FifoStatus.FIFO_UNCERTAIN,
                    scope=active_scope,
                    edge_ids=edge_values,
                    probe_times=times,
                    tolerance_seconds=tolerance_seconds,
                    probes_evaluated=evaluated,
                    minimum_slack_seconds=minimum_slack,
                    reason=f"evaluation_failed:{type(error).__name__}",
                )
            evaluated += 1
            if arrival < departure - timedelta(seconds=tolerance_seconds):
                return FifoCertificate(
                    status=FifoStatus.FIFO_UNCERTAIN,
                    scope=active_scope,
                    edge_ids=edge_values,
                    probe_times=times,
                    tolerance_seconds=tolerance_seconds,
                    probes_evaluated=evaluated,
                    minimum_slack_seconds=minimum_slack,
                    reason="arrival_before_departure",
                )
            if previous_arrival is not None and previous_departure is not None:
                slack = (arrival - previous_arrival).total_seconds()
                minimum_slack = slack if minimum_slack is None else min(minimum_slack, slack)
                if slack < -tolerance_seconds:
                    return FifoCertificate(
                        status=FifoStatus.FIFO_VIOLATED,
                        scope=active_scope,
                        edge_ids=edge_values,
                        probe_times=times,
                        tolerance_seconds=tolerance_seconds,
                        probes_evaluated=evaluated,
                        minimum_slack_seconds=minimum_slack,
                        counterexample=FifoCounterexample(
                            edge_id=edge,
                            earlier_departure=previous_departure,
                            earlier_arrival=previous_arrival,
                            later_departure=departure,
                            later_arrival=arrival,
                            slack_seconds=slack,
                        ),
                        reason="later_departure_arrives_earlier",
                    )
            previous_departure = departure
            previous_arrival = arrival

    return FifoCertificate(
        status=FifoStatus.FIFO_CERTIFIED,
        scope=active_scope,
        edge_ids=edge_values,
        probe_times=times,
        tolerance_seconds=tolerance_seconds,
        probes_evaluated=evaluated,
        minimum_slack_seconds=minimum_slack,
    )


def _callable_digest(callback: Any) -> str:
    explicit = getattr(callback, "__temporal_identity__", None)
    if isinstance(explicit, str) and explicit.strip():
        return f"explicit:{explicit.strip()}"
    # Bound methods capture mutable instance state that cannot be represented
    # by a code-object digest.  Unless the owner supplies an explicit stable
    # identity, fail closed instead of authorizing a certificate for an
    # evaluator whose behaviour may differ between processes/checkpoints.
    if getattr(callback, "__self__", None) is not None:
        return (
            "unknown:bound-method:"
            f"{type(callback).__module__}.{type(callback).__qualname__}"
        )
    function = getattr(callback, "__func__", callback)
    code = getattr(function, "__code__", None)
    if code is None:
        return f"unknown:type:{type(callback).__module__}.{type(callback).__qualname__}"
    closure = []
    unknown_closure = False
    for cell in getattr(function, "__closure__", ()) or ():
        try:
            item = cell.cell_contents
        except ValueError:
            item = None
        closure.append(
            {"callable": _callable_digest(item)}
            if callable(item)
            else _jsonable(item)
        )
        if callable(item) and closure[-1]["callable"].startswith("unknown:"):
            unknown_closure = True
    if unknown_closure:
        return "unknown:closure:" + canonical_digest(closure)
    return "code:" + canonical_digest(
        {
            "module": getattr(function, "__module__", ""),
            "qualname": getattr(function, "__qualname__", ""),
            "code": code.co_code.hex(),
            "consts": code.co_consts,
            "names": code.co_names,
            "defaults": getattr(function, "__defaults__", None),
            "closure": closure,
        }
    )


def temporal_scope_from_planner(
    planner: Any,
    request: Any,
    *,
    input_revision: int = 0,
    edge_ids: Iterable[Any] | None = None,
    probe_times: Iterable[datetime] | None = None,
) -> TemporalScope:
    """Build a complete identity fence from a C planner/request pair."""

    risk_identity = planner.risk_identity
    frames = planner.risk_sampler.frames
    risk_window_digest = canonical_digest(
        tuple(
            {
                "valid_time": frame.valid_time,
                "risk_id": frame.risk_id,
                "content_digest": risk_frame_content_digest(frame),
            }
            for frame in frames
        )
    )
    grid = planner.grid
    grid_digest = canonical_digest(
        {
            "latitudes": grid.latitudes,
            "longitudes": grid.longitudes,
            "allow_diagonal": grid.allow_diagonal,
        }
    )
    evaluator = getattr(planner, "_injected_edge_evaluator", None)
    return TemporalScope.from_mapping(
        {
            "risk_frame_content_digest": risk_window_digest,
            "risk_identity_digest": canonical_digest(risk_identity),
            "generation_id": risk_identity.generation_id,
            "input_revision": input_revision,
            "max_frame_gap_seconds": (
                planner.risk_sampler._max_frame_gap.total_seconds()
                if getattr(planner.risk_sampler, "_max_frame_gap", None) is not None
                else None
            ),
            "grid_digest": grid_digest,
            "vessel_model_digest": canonical_digest(planner.vessel_model),
            "planner_config_digest": canonical_digest(
                {
                    "planner_config": planner.planner_config,
                    "weights": planner._weights,
                    "full_turn_penalty_hours": planner._full_turn_penalty_hours,
                }
            ),
            "eta_policy_digest": canonical_digest(planner.eta_policy),
            "search_limits_digest": canonical_digest(planner.limits),
            "edge_evaluator_digest": (
                "default:temporal-label-edge-v1"
                if evaluator is None
                else _callable_digest(evaluator)
            ),
            "objective": getattr(request.objective, "value", request.objective),
            "start": request.start,
            "goal": request.goal,
            "departure_time": request.departure_time,
            "maximum_elapsed_seconds": (
                request.maximum_elapsed.total_seconds()
                if request.maximum_elapsed is not None
                else None
            ),
            "maximum_risk": request.maximum_risk,
            "time_bucket_seconds": request.time_bucket_size.total_seconds(),
            "edge_sample_count": request.edge_sample_count,
            "edge_ids": tuple(edge_ids) if edge_ids is not None else (),
            "probe_times": tuple(probe_times) if probe_times is not None else (),
        }
    )


# The descriptive alias keeps call sites readable when they only need the
# classifier result; both names intentionally share the same fail-closed code.
classify_fifo = qualify_fifo


__all__ = [
    "DominanceMode",
    "FifoCertificate",
    "FifoCounterexample",
    "FifoStatus",
    "TemporalDominanceCertificate",
    "TemporalDominancePolicy",
    "TemporalScope",
    "canonical_digest",
    "classify_fifo",
    "qualify_fifo",
    "temporal_scope_from_planner",
]
