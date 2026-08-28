"""Partitioned ETA evidence for the C research sidecar.

The formal planner deliberately keeps its historical point sampler and ETA
policy.  This module adds a small, opt-in proof layer which partitions a
travel-time domain at the immutable RiskFrame boundaries before asking the
existing analytic evaluator for evidence.  The evaluator identity is derived
from the actual :class:`RiskSampler`; callers cannot mark a real evaluator as
certified by setting a boolean or by choosing a scope string.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from math import isfinite
from typing import Any

from arctic_route_planning.grid import GeoPoint
from arctic_route_planning.risk.sampler import RiskSampler

from .eta_analytic import NavigabilityStatus
from .eta_interval import EtaInterval, EtaIntervalStatus
from .eta_interval_evaluator import EtaOperatorIntervalEvidence, TemporalEtaIntervalEvaluator
from .temporal_qualification import FifoStatus, TemporalScope, canonical_digest


class EvaluatorCertificateStatus(StrEnum):
    """Status of the mechanically identified interval evaluator."""

    CERTIFIED = "CERTIFIED"
    UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True, slots=True)
class RiskEvaluatorCertificate:
    """Identity and rule proof for one immutable RiskSampler window."""

    sampler_digest: str
    frame_times: tuple[datetime, ...]
    frame_risk_ids: tuple[str, ...]
    rule_version: str = "RiskSampler.linear-time-bilinear-space.v2"
    status: EvaluatorCertificateStatus = EvaluatorCertificateStatus.CERTIFIED
    reason: str | None = None
    schema_version: str = "c.p0.1-temporal-risk-evaluator-certificate.v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "frame_times",
            tuple(value.astimezone(UTC) for value in self.frame_times),
        )
        object.__setattr__(self, "frame_risk_ids", tuple(self.frame_risk_ids))
        object.__setattr__(self, "status", EvaluatorCertificateStatus(self.status))
        if self.schema_version != "c.p0.1-temporal-risk-evaluator-certificate.v1":
            raise ValueError("unsupported risk evaluator certificate schema")
        if not self.sampler_digest or not self.rule_version:
            raise ValueError("risk evaluator certificate requires stable sampler identity")
        if len(self.frame_times) != len(self.frame_risk_ids):
            raise ValueError("frame times and risk ids must have the same length")
        if len(self.frame_times) < 2 and self.status is EvaluatorCertificateStatus.CERTIFIED:
            raise ValueError("a certified interval evaluator requires at least two frames")

    @classmethod
    def from_sampler(cls, sampler: RiskSampler) -> RiskEvaluatorCertificate:
        """Construct evidence from sampler internals after its window fence.

        Payload validity is checked again by ``_sample_interval`` for each
        spatial point.  A certificate therefore identifies the evaluator rule
        but never turns a failed point sample into a safe value.
        """

        frames = sampler.frames
        if len(frames) < 2:
            return cls(
                sampler_digest=sampler.interval_evaluator_digest,
                frame_times=tuple(frame.valid_time for frame in frames),
                frame_risk_ids=tuple(frame.risk_id for frame in frames),
                status=EvaluatorCertificateStatus.UNCERTAIN,
                reason="insufficient_risk_frames",
            )
        return cls(
            sampler_digest=sampler.interval_evaluator_digest,
            frame_times=tuple(frame.valid_time for frame in frames),
            frame_risk_ids=tuple(frame.risk_id for frame in frames),
        )

    @property
    def proof_digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "sampler_digest": self.sampler_digest,
                "frame_times": self.frame_times,
                "frame_risk_ids": self.frame_risk_ids,
                "rule_version": self.rule_version,
                "status": self.status,
                "reason": self.reason,
            }
        )

    @property
    def digest(self) -> str:
        return self.proof_digest

    @property
    def identity(self) -> str:
        return f"certified:{self.rule_version}:{self.proof_digest[:16]}"

    def bind_scope(self, scope: TemporalScope | Mapping[str, Any]) -> TemporalScope:
        """Add the certificate identity to a copied scope."""

        values = dict(TemporalScope.from_mapping(scope).mapping)
        values["risk_interval_evaluator_digest"] = self.proof_digest
        values["evaluator_certification"] = self.identity
        return TemporalScope.from_mapping(values)

    def permits(self, scope: TemporalScope | Mapping[str, Any]) -> bool:
        active = TemporalScope.from_mapping(scope)
        return bool(
            self.status is EvaluatorCertificateStatus.CERTIFIED
            and active.mapping.get("risk_interval_evaluator_digest") == self.proof_digest
            and active.mapping.get("evaluator_certification") == self.identity
        )


@dataclass(frozen=True, slots=True)
class EtaPartitionBoundaryEvidence:
    """One-sided evidence at a travel-domain partition boundary."""

    boundary_hours: float
    left_image: EtaInterval | None
    right_image: EtaInterval | None
    status: str
    reason: str | None = None
    tolerance_seconds: float = 1.0

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "boundary_hours": self.boundary_hours,
                "left_image": self.left_image,
                "right_image": self.right_image,
                "status": self.status,
                "reason": self.reason,
                "tolerance_seconds": self.tolerance_seconds,
            }
        )


@dataclass(frozen=True, slots=True)
class PartitionedEtaEvidence:
    """Auditable partition results; never enables production dominance."""

    departure: datetime
    domain: EtaInterval
    boundaries: tuple[float, ...]
    partitions: tuple[EtaOperatorIntervalEvidence, ...]
    boundary_evidence: tuple[EtaPartitionBoundaryEvidence, ...]
    evaluator_certificate: RiskEvaluatorCertificate
    scope: TemporalScope
    status: str
    reason: str | None = None
    schema_version: str = "c.p0.1-temporal-eta-partition-evidence.v1"

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "departure": self.departure,
                "domain": self.domain,
                "boundaries": self.boundaries,
                "partitions": tuple(item.digest for item in self.partitions),
                "boundary_evidence": tuple(item.digest for item in self.boundary_evidence),
                "evaluator_certificate": self.evaluator_certificate.digest,
                "scope": self.scope.digest,
                "status": self.status,
                "reason": self.reason,
            }
        )

    @property
    def certified_partition_count(self) -> int:
        return sum(
            item.analytic_certificate is not None and item.analytic_certificate.root_authorized
            for item in self.partitions
        )

    @property
    def blocked_partition_count(self) -> int:
        return sum(
            item.analytic_certificate is not None
            and item.analytic_certificate.navigation is NavigabilityStatus.ALWAYS_BLOCKED
            for item in self.partitions
        )

    @property
    def coverage_ratio(self) -> float:
        if not self.partitions or self.domain.width_hours <= 0.0:
            return 0.0
        covered = sum(
            item.travel_domain.width_hours
            for item in self.partitions
            if item.analytic_certificate is not None
            and item.analytic_certificate.root_status
            in {EtaIntervalStatus.ROOT_EXISTS_UNIQUE, EtaIntervalStatus.ROOT_EXCLUDED}
        )
        return min(1.0, max(0.0, covered / self.domain.width_hours))

    @property
    def fifo_status(self) -> FifoStatus:
        for boundary in self.boundary_evidence:
            if boundary.status == "FIFO_VIOLATED":
                return FifoStatus.FIFO_VIOLATED
            if boundary.status != "FIFO_CERTIFIED":
                return FifoStatus.FIFO_UNCERTAIN
        if not self.partitions:
            return FifoStatus.FIFO_UNCERTAIN
        if all(item.fifo_status == FifoStatus.FIFO_CERTIFIED.value for item in self.partitions):
            return FifoStatus.FIFO_CERTIFIED
        return FifoStatus.FIFO_UNCERTAIN

    @property
    def permits_dominance(self) -> bool:
        # A fixed departure partition is not a proof over a departure
        # interval.  Keeping this false prevents accidental promotion while
        # still making the partition evidence useful to a future 2-D proof.
        return False


def partition_travel_domain(
    sampler: RiskSampler,
    departure: datetime,
    domain: EtaInterval,
    edge_sample_points: Sequence[GeoPoint],
) -> tuple[float, ...]:
    """Return deterministic travel-hour cuts induced by frame boundaries."""

    if departure.tzinfo is None or departure.utcoffset() != timedelta(0):
        raise ValueError("departure must be timezone-aware UTC")
    boundaries = [domain.lower_hours, domain.upper_hours]
    for point_index in range(len(edge_sample_points)):
        fraction = point_index / max(1, len(edge_sample_points) - 1)
        if fraction <= 0.0:
            continue
        for frame_time in sampler.frames:
            value = (frame_time.valid_time - departure).total_seconds() / 3600.0 / fraction
            if domain.lower_hours < value < domain.upper_hours and isfinite(value):
                boundaries.append(value)
    return tuple(sorted(set(boundaries)))


class TemporalEtaPartitionEvaluator:
    """Evaluate stable travel-time partitions with a sampler-derived proof."""

    def __init__(
        self,
        evaluator: TemporalEtaIntervalEvaluator,
        *,
        certificate: RiskEvaluatorCertificate | None = None,
        tolerance_seconds: float = 1.0,
    ) -> None:
        if tolerance_seconds < 0.0 or not isfinite(tolerance_seconds):
            raise ValueError("tolerance_seconds must be finite and non-negative")
        self.evaluator = evaluator
        self.certificate = certificate or RiskEvaluatorCertificate.from_sampler(
            evaluator.risk_sampler
        )
        self.tolerance_seconds = tolerance_seconds

    def evaluate(
        self,
        departure: datetime,
        domain: EtaInterval,
        *,
        edge_sample_points: Sequence[GeoPoint] | None = None,
        scope: TemporalScope | Mapping[str, Any] | None = None,
    ) -> PartitionedEtaEvidence:
        active_scope = TemporalScope.from_mapping(scope or self.evaluator.scope)
        if not active_scope.matches(self.evaluator.scope):
            return PartitionedEtaEvidence(
                departure=departure,
                domain=domain,
                boundaries=(),
                partitions=(),
                boundary_evidence=(),
                evaluator_certificate=self.certificate,
                scope=active_scope,
                status="UNCERTAIN",
                reason="scope_mismatch",
            )
        bound_scope = self.certificate.bind_scope(active_scope)
        points = tuple(edge_sample_points or self.evaluator.edge_sample_points)
        if len(points) < 2:
            return PartitionedEtaEvidence(
                departure=departure,
                domain=domain,
                boundaries=(),
                partitions=(),
                boundary_evidence=(),
                evaluator_certificate=self.certificate,
                scope=bound_scope,
                status="UNCERTAIN",
                reason="insufficient_edge_sample_points",
            )
        cuts = partition_travel_domain(self.evaluator.risk_sampler, departure, domain, points)
        inner = TemporalEtaIntervalEvaluator(
            self.evaluator.risk_sampler,
            self.evaluator.vessel_model,
            self.evaluator.request,
            bound_scope,
            edge_sample_points=points,
            edge_distance_km=self.evaluator.edge_distance_km,
            planner_config=self.evaluator.planner_config,
            eta_policy=self.evaluator.eta_policy,
            evaluator_digest=self.evaluator.evaluator_digest,
        )
        partitions: list[EtaOperatorIntervalEvidence] = []
        for lower, upper in pairwise(cuts):
            if upper <= lower:
                continue
            partitions.append(
                inner.evaluate_analytic(
                    departure,
                    EtaInterval(lower, upper),
                    edge_sample_points=points,
                    scope=bound_scope,
                    evaluator_certificate=self.certificate,
                )
            )
        boundaries: list[EtaPartitionBoundaryEvidence] = []
        for index, boundary in enumerate(cuts[1:-1], start=1):
            left = partitions[index - 1] if index - 1 < len(partitions) else None
            right = partitions[index] if index < len(partitions) else None
            left_image = left.image if left is not None else None
            right_image = right.image if right is not None else None
            if left_image is None or right_image is None:
                status = "FIFO_UNCERTAIN"
                reason = "missing_one_sided_interval_image"
            elif right_image.lower_hours + self.tolerance_seconds / 3600.0 < left_image.upper_hours:
                status = "FIFO_VIOLATED"
                reason = "negative_travel_operator_jump"
            else:
                status = "FIFO_CERTIFIED"
                reason = None
            boundaries.append(
                EtaPartitionBoundaryEvidence(
                    boundary_hours=boundary,
                    left_image=left_image,
                    right_image=right_image,
                    status=status,
                    reason=reason,
                    tolerance_seconds=self.tolerance_seconds,
                )
            )
        if self.certificate.status is not EvaluatorCertificateStatus.CERTIFIED:
            status = "UNCERTAIN"
            reason = self.certificate.reason or "risk_evaluator_certificate_uncertain"
        elif any(item.status is EtaIntervalStatus.UNCERTAIN_COVERAGE for item in partitions):
            status = "UNCERTAIN"
            reason = "interval_domain_coverage_incomplete"
        elif any(item.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY for item in partitions):
            status = "UNCERTAIN"
            reason = "partition_discontinuity_unresolved"
        elif any(item.status == "FIFO_VIOLATED" for item in boundaries):
            status = "FIFO_VIOLATED"
            reason = "negative_travel_operator_jump"
        else:
            root_statuses = tuple(
                item.analytic_certificate.root_status
                for item in partitions
                if item.analytic_certificate is not None
            )
            root_candidates = sum(
                status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE for status in root_statuses
            )
            root_safe = bool(root_statuses) and all(
                status in {EtaIntervalStatus.ROOT_EXISTS_UNIQUE, EtaIntervalStatus.ROOT_EXCLUDED}
                for status in root_statuses
            )
            if root_safe and root_candidates == 1:
                status = "PARTITION_CERTIFIED"
                reason = None
            elif root_safe and root_candidates == 0:
                status = "ROOT_EXCLUDED"
                reason = "all_partitions_exclude_root"
            else:
                status = "UNCERTAIN"
                reason = "partition_root_or_fifo_proof_incomplete"
        return PartitionedEtaEvidence(
            departure=departure,
            domain=domain,
            boundaries=cuts[1:-1],
            partitions=tuple(partitions),
            boundary_evidence=tuple(boundaries),
            evaluator_certificate=self.certificate,
            scope=bound_scope,
            status=status,
            reason=reason,
        )


__all__ = [
    "EtaPartitionBoundaryEvidence",
    "EvaluatorCertificateStatus",
    "PartitionedEtaEvidence",
    "RiskEvaluatorCertificate",
    "TemporalEtaPartitionEvaluator",
    "partition_travel_domain",
]
