"""Produce one formal four-layer route-motion set from a completed plan set."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.contracts.layered import (
    FourLayerRoutePlanSet,
    PlanLayer,
    RoutePlanV3,
)
from arctic_route_planning.contracts.route_motion import (
    CONTINUOUS_RASTER_MODEL_SCOPE,
    ROUTE_MOTION_CANDIDATE_SET_SCHEMA_VERSION,
    ROUTE_MOTION_INTERPOLATION,
    ROUTE_MOTION_SET_SCHEMA_VERSION,
    MotionSample,
    RouteMotionCandidateRecord,
    RouteMotionCandidateSet,
    RouteMotionMode,
    RouteMotionQualification,
    RouteMotionRecord,
    RouteMotionSet,
    WaypointMotionAnchor,
)
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.publishing.route_motion_serialization import (
    canonical_sha256,
    route_motion_candidate_set_semantic_digest,
    route_motion_set_semantic_digest,
)
from arctic_route_planning.risk.sampler import RiskSampler

from .anchoring import path_metric, project_waypoint_anchors, time_at_distance
from .any_angle import (
    AnyAngleDecision,
    AnyAngleEdge,
    AnyAngleRoute,
    build_any_angle_candidates,
    great_circle_distance_m,
    great_circle_interpolate,
)
from .geometry import (
    FORMAL_ROUTE_SMOOTHING_POLICY,
    MultiSpanRouteResult,
    RouteSmoothingPolicy,
)
from .joint_smoothing import JointBSplineResult, build_joint_bspline
from .profile import KNOT_TO_MPS, EngineeringRouteMotionProfile

Coordinate = tuple[float, float]
ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION = (
    "c.route-motion-qualification-evidence.v1"
)


class _CorridorHulls(tuple):
    """Tuple-compatible hull bundle carrying a clearance subset hint."""

    def __new__(
        cls,
        values: Sequence[Sequence[Coordinate]],
        clearance_hull_count: int,
    ) -> _CorridorHulls:
        instance = super().__new__(cls, values)
        instance.clearance_hull_count = clearance_hull_count
        return instance
_FORMAL_GATE_ORDER = (
    "sea_land_hard_mask",
    "temporal_risk_coverage",
    "corridor_allowed_area",
    "manoeuvring",
    "eta_speed",
    "risk_non_degradation",
    "adaptive_trust_deviation",
)
# The current frozen route has 22 waypoints (231 DAG edges).  The formal r17
# run must inspect every pair so a resource cap cannot masquerade as a safety
# decision.  Larger future routes still retain a deterministic upper bound;
# their evidence records any unexamined edge explicitly.
_FORMAL_ANY_ANGLE_EDGE_EVALUATION_LIMIT = 4096
# All waypoint pairs are still edge-screened.  Only this stable prefix enters
# the expensive full-curve qualification stage; raw is always reserved as the
# final comparison candidate.  Thirty-two gives the frozen 22-waypoint r17
# route enough deterministic alternatives to reach the safe southern channel,
# while the explicit edge-evaluation count remains exhaustive for that route.
_FORMAL_ANY_ANGLE_MAXIMUM_CANDIDATES = 32
_FORMAL_TRIM_FRACTIONS = (
    0.49,
    0.45,
    0.40,
    0.35,
    0.30,
    0.25,
    0.20,
    0.15,
    0.10,
    0.05,
)
CorridorValidator = Callable[
    [tuple[tuple[Coordinate, ...], ...], tuple[Coordinate, ...], tuple[datetime, ...], float],
    Mapping[str, Any] | bool,
]


def build_route_motion_set(
    plan_set: FourLayerRoutePlanSet,
    *,
    risk_window_id: str,
    risk_window_digest: str,
    vessel_profile_digest: str,
    producer_digest: str,
    profile: EngineeringRouteMotionProfile | None = None,
    risk_sampler: RiskSampler | None = None,
    corridor_validator: CorridorValidator | None = None,
    position_error_m: float = 0.0,
    transform_error_m: float = 0.0,
    chord_error_m: float = 0.0,
    generated_at: datetime | None = None,
    policy: RouteSmoothingPolicy | None = None,
    _evidence_sink: list[dict[str, Any]] | None = None,
) -> RouteMotionSet:
    """Build four recommended records without changing ``plan_set``.

    Missing or failed qualification produces an explicit raw passthrough for
    that layer.  The set itself remains complete and atomic, allowing old or
    constrained deployments to preserve the authoritative route.
    """

    chosen = profile or EngineeringRouteMotionProfile()
    if plan_set.vessel_profile_id != chosen.vessel_profile_id:
        raise ValueError("motion profile does not match plan-set vessel_profile_id")
    _digest(risk_window_digest, "risk_window_digest")
    _digest(vessel_profile_digest, "vessel_profile_digest")
    _digest(producer_digest, "producer_digest")
    if risk_sampler is not None:
        _validate_sampler_identity(plan_set, risk_sampler)
    chosen_policy = policy or FORMAL_ROUTE_SMOOTHING_POLICY
    records = tuple(
        _build_record(
            bundle.recommended,
            profile=chosen,
            risk_sampler=risk_sampler,
            corridor_validator=corridor_validator,
            corridor_buffer_m=chosen.corridor_buffer_m(
                position_error_m=position_error_m,
                transform_error_m=transform_error_m,
                chord_error_m=chord_error_m,
            ),
            policy=chosen_policy,
            evidence_sink=_evidence_sink,
        )
        for bundle in plan_set.layers
    )
    provisional = RouteMotionSet(
        schema_version=ROUTE_MOTION_SET_SCHEMA_VERSION,
        motion_set_id="route-motion-set-sha256-" + "0" * 64,
        layer_set_id=plan_set.layer_set_id,
        run_id=plan_set.run_id,
        scenario_id=plan_set.scenario_id,
        corridor_id=plan_set.corridor_id,
        generation_id=plan_set.generation_id,
        input_revision=plan_set.input_revision,
        risk_window_id=risk_window_id,
        risk_window_digest=risk_window_digest,
        vessel_profile_id=plan_set.vessel_profile_id,
        vessel_profile_version=chosen.vessel_profile_version,
        vessel_profile_digest=vessel_profile_digest,
        motion_profile_id=chosen.profile_id,
        motion_profile_digest=chosen.digest,
        config_digest=plan_set.config_digest,
        model_config_digest=plan_set.model_config_digest,
        planner_config_digest=plan_set.planner_config_digest,
        producer_digest=producer_digest,
        generated_at=(generated_at or plan_set.generated_at),
        records=records,
    )
    return replace(
        provisional,
        motion_set_id="route-motion-set-sha256-"
        + route_motion_set_semantic_digest(provisional),
    )


def build_route_motion_candidate_set(
    plan_set: FourLayerRoutePlanSet,
    *,
    risk_window_id: str,
    risk_window_digest: str,
    vessel_profile_digest: str,
    producer_digest: str,
    profile: EngineeringRouteMotionProfile | None = None,
    risk_sampler: RiskSampler | None = None,
    corridor_validator: CorridorValidator | None = None,
    position_error_m: float = 0.0,
    transform_error_m: float = 0.0,
    chord_error_m: float = 0.0,
    generated_at: datetime | None = None,
    policy: RouteSmoothingPolicy | None = None,
    _evidence_sink: list[dict[str, Any]] | None = None,
) -> RouteMotionCandidateSet:
    """Build C motion for the three complete-voyage objective candidates.

    The existing :func:`build_route_motion_set` deliberately remains a
    four-layer recommended-only contract.  This additive artifact is used by
    the Viewer only when a user explicitly chooses a full-voyage objective
    before playback.  Every record is produced by the same B-spline gates and
    raw fallback path as the formal sibling contract.
    """

    chosen = profile or EngineeringRouteMotionProfile()
    if plan_set.vessel_profile_id != chosen.vessel_profile_id:
        raise ValueError("motion profile does not match plan-set vessel_profile_id")
    _digest(risk_window_digest, "risk_window_digest")
    _digest(vessel_profile_digest, "vessel_profile_digest")
    _digest(producer_digest, "producer_digest")
    if risk_sampler is not None:
        _validate_sampler_identity(plan_set, risk_sampler)
    chosen_policy = policy or FORMAL_ROUTE_SMOOTHING_POLICY
    full = plan_set.bundle_for(PlanLayer.FULL_VOYAGE)
    records = tuple(
        RouteMotionCandidateRecord(
            objective_mode=objective,
            record=_build_record(
                full.plans[objective],
                profile=chosen,
                risk_sampler=risk_sampler,
                corridor_validator=corridor_validator,
                corridor_buffer_m=chosen.corridor_buffer_m(
                    position_error_m=position_error_m,
                    transform_error_m=transform_error_m,
                    chord_error_m=chord_error_m,
                ),
                policy=chosen_policy,
                evidence_sink=_evidence_sink,
            ),
        )
        for objective in ObjectiveMode
    )
    provisional = RouteMotionCandidateSet(
        schema_version=ROUTE_MOTION_CANDIDATE_SET_SCHEMA_VERSION,
        motion_candidate_set_id="route-motion-candidate-set-sha256-" + "0" * 64,
        layer_set_id=plan_set.layer_set_id,
        run_id=plan_set.run_id,
        scenario_id=plan_set.scenario_id,
        corridor_id=plan_set.corridor_id,
        generation_id=plan_set.generation_id,
        input_revision=plan_set.input_revision,
        risk_window_id=risk_window_id,
        risk_window_digest=risk_window_digest,
        vessel_profile_id=plan_set.vessel_profile_id,
        vessel_profile_version=chosen.vessel_profile_version,
        vessel_profile_digest=vessel_profile_digest,
        motion_profile_id=chosen.profile_id,
        motion_profile_digest=chosen.digest,
        config_digest=plan_set.config_digest,
        model_config_digest=plan_set.model_config_digest,
        planner_config_digest=plan_set.planner_config_digest,
        producer_digest=producer_digest,
        generated_at=(generated_at or plan_set.generated_at),
        records=records,
    )
    return replace(
        provisional,
        motion_candidate_set_id=(
            "route-motion-candidate-set-sha256-"
            + route_motion_candidate_set_semantic_digest(provisional)
        ),
    )


def build_route_motion_set_with_evidence(
    *args: Any, **kwargs: Any
) -> tuple[RouteMotionSet, dict[str, Any]]:
    """Build the formal motion set and its C-owned qualification sidecar."""

    evidence_sink: list[dict[str, Any]] = []
    kwargs["_evidence_sink"] = evidence_sink
    result = build_route_motion_set(*args, **kwargs)
    if len(evidence_sink) != len(result.records):
        raise RuntimeError("motion qualification evidence cardinality mismatch")
    evidence = _qualification_evidence_document(
        artifact_kind="motion_set",
        artifact_id=result.motion_set_id,
        producer_digest=result.producer_digest,
        risk_window_digest=result.risk_window_digest,
        records=tuple(
            (record, detail, None)
            for record, detail in zip(result.records, evidence_sink, strict=True)
        ),
    )
    return result, evidence


def build_route_motion_candidate_set_with_evidence(
    *args: Any, **kwargs: Any
) -> tuple[RouteMotionCandidateSet, dict[str, Any]]:
    """Build objective-specific motion and its C-owned qualification sidecar."""

    evidence_sink: list[dict[str, Any]] = []
    kwargs["_evidence_sink"] = evidence_sink
    result = build_route_motion_candidate_set(*args, **kwargs)
    if len(evidence_sink) != len(result.records):
        raise RuntimeError("candidate qualification evidence cardinality mismatch")
    evidence = _qualification_evidence_document(
        artifact_kind="motion_candidate_set",
        artifact_id=result.motion_candidate_set_id,
        producer_digest=result.producer_digest,
        risk_window_digest=result.risk_window_digest,
        records=tuple(
            (item.record, detail, item.objective_mode.value)
            for item, detail in zip(result.records, evidence_sink, strict=True)
        ),
    )
    return result, evidence


def merge_route_motion_qualification_evidence(
    motion_evidence: Mapping[str, Any],
    candidate_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Combine recommended and objective candidate evidence in one sidecar."""

    if motion_evidence.get("producer_digest") != candidate_evidence.get("producer_digest"):
        raise ValueError("qualification evidence producer digests differ")
    if motion_evidence.get("risk_window_digest") != candidate_evidence.get("risk_window_digest"):
        raise ValueError("qualification evidence risk window digests differ")
    body = {
        "schema_version": ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "producer_digest": motion_evidence["producer_digest"],
        "risk_window_digest": motion_evidence["risk_window_digest"],
        "motion_set_id": motion_evidence.get("motion_set_id"),
        "motion_candidate_set_id": candidate_evidence.get("motion_candidate_set_id"),
        "records": [
            *motion_evidence.get("records", []),
            *candidate_evidence.get("records", []),
        ],
    }
    return {
        **body,
        "evidence_id": "route-motion-qualification-evidence-sha256-"
        + canonical_sha256(body),
    }


def _qualification_evidence_document(
    *,
    artifact_kind: str,
    artifact_id: str,
    producer_digest: str,
    risk_window_digest: str,
    records: Sequence[tuple[RouteMotionRecord, Mapping[str, Any], str | None]],
) -> dict[str, Any]:
    if artifact_kind not in {"motion_set", "motion_candidate_set"}:
        raise ValueError("unsupported route-motion evidence artifact kind")
    entries = []
    for record, evidence, objective_mode in records:
        details_value = evidence.get("record_details")
        details = (
            dict(details_value)
            if isinstance(details_value, Mapping)
            else dict(evidence)
        )
        if canonical_sha256(details) != record.qualification.details_digest:
            raise ValueError(
                f"qualification details digest does not match record {record.plan_id}"
            )
        entries.append(
            {
                "artifact_kind": artifact_kind,
                "artifact_id": artifact_id,
                "objective_mode": objective_mode,
                "planning_layer": record.planning_layer.value,
                "plan_id": record.plan_id,
                "mode": record.mode.value,
                "fallback_reason": record.fallback_reason,
                "raw_route_digest": record.raw_route_digest,
                "details_digest": record.qualification.details_digest,
                "details": details,
                # ``details`` is the digest-bound canonical evidence body.
                # Keep diagnostics as a small index instead of embedding the
                # same multi-edge report twice in the sidecar.
                "diagnostics": {
                    "schema_version": ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
                    "details_digest": record.qualification.details_digest,
                    "qualification_result": record.qualification.result,
                },
            }
        )
    body: dict[str, Any] = {
        "schema_version": ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "producer_digest": producer_digest,
        "risk_window_digest": risk_window_digest,
        "records": entries,
    }
    body["motion_set_id" if artifact_kind == "motion_set" else "motion_candidate_set_id"] = (
        artifact_id
    )
    return {
        **body,
        "evidence_id": "route-motion-qualification-evidence-sha256-"
        + canonical_sha256(body),
    }


def _build_record(
    plan: RoutePlanV3,
    *,
    profile: EngineeringRouteMotionProfile,
    risk_sampler: RiskSampler | None,
    corridor_validator: CorridorValidator | None,
    corridor_buffer_m: float,
    policy: RouteSmoothingPolicy | None,
    evidence_sink: list[dict[str, Any]] | None = None,
) -> RouteMotionRecord:
    record, evidence = _build_record_with_evidence(
        plan,
        profile=profile,
        risk_sampler=risk_sampler,
        corridor_validator=corridor_validator,
        corridor_buffer_m=corridor_buffer_m,
        policy=policy,
    )
    if evidence_sink is not None:
        evidence_sink.append(evidence)
    return record


def _build_record_with_evidence(
    plan: RoutePlanV3,
    *,
    profile: EngineeringRouteMotionProfile,
    risk_sampler: RiskSampler | None,
    corridor_validator: CorridorValidator | None,
    corridor_buffer_m: float,
    policy: RouteSmoothingPolicy | None,
) -> tuple[RouteMotionRecord, dict[str, Any]]:
    """Build one record and its producer-owned qualification evidence."""

    raw_points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    raw_digest = canonical_sha256(
        [
            {
                "longitude": waypoint.longitude,
                "latitude": waypoint.latitude,
                "eta": _iso(waypoint.eta),
                "recommended_speed_mps": waypoint.recommended_speed_mps,
            }
            for waypoint in plan.waypoints
        ]
    )
    if risk_sampler is None:
        return _fallback_with_evidence(
            plan, raw_digest, "missing_risk_window", raw_points=raw_points
        )
    if corridor_validator is None:
        return _fallback_with_evidence(
            plan,
            raw_digest,
            "missing_continuous_corridor_evidence",
            raw_points=raw_points,
        )
    if len(raw_points) < 3:
        return _fallback_with_evidence(
            plan, raw_digest, "no_smoothing_turn", raw_points=raw_points
        )

    chosen_policy = policy or FORMAL_ROUTE_SMOOTHING_POLICY
    edge_probe_spacing_m = max(1_000.0, chosen_policy.sample_spacing_m)
    evidence_base: dict[str, Any] = {
        "schema_version": ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "planning_layer": plan.planning_layer.value,
        "raw_route_digest": raw_digest,
        "gate_order": list(_FORMAL_GATE_ORDER),
        "policy": {
            "max_trim_fraction": chosen_policy.max_trim_fraction,
            "maximum_overlap_fraction": chosen_policy.maximum_overlap_fraction,
            "sample_spacing_m": chosen_policy.sample_spacing_m,
            "edge_probe_spacing_m": edge_probe_spacing_m,
            "any_angle_edge_evaluation_limit": _FORMAL_ANY_ANGLE_EDGE_EVALUATION_LIMIT,
            "great_circle_edges": True,
        },
        "input": {
            "risk_window_identity": _risk_identity_dict(risk_sampler),
            "risk_interval_evaluator_digest": risk_sampler.interval_evaluator_digest,
            "corridor_buffer_m": corridor_buffer_m,
        },
    }
    try:
        raw_envelope = risk_sampler.sample_swept_temporal_envelope(
            _raw_motion_samples(plan),
            sample_spacing_m=chosen_policy.sample_spacing_m,
        )
    except Exception as exc:
        raw_envelope = None
        raw_failure = f"raw_temporal_evaluator_error:{type(exc).__name__}"
    else:
        raw_failure = None if raw_envelope.usable and not raw_envelope.hard_mask_possible else (
            "raw_route_hard_mask_or_temporal_coverage"
        )
    evidence_base["raw_baseline"] = (
        raw_envelope.to_dict() if raw_envelope is not None else {"failure_reason": raw_failure}
    )
    if raw_envelope is None or not raw_envelope.usable or raw_envelope.hard_mask_possible:
        return _fallback_with_evidence(
            plan,
            raw_digest,
            raw_failure or "raw_route_temporal_baseline_failed",
            raw_points=raw_points,
            evidence=evidence_base,
        )

    edge_evidence: list[dict[str, Any]] = []

    def edge_validator(edge: AnyAngleEdge) -> AnyAngleDecision:
        edge_details = {
            "edge": {
                "start_index": edge.start_index,
                "end_index": edge.end_index,
                "length_m": edge.length_m,
            }
        }
        if edge.end_index == edge.start_index + 1:
            # The raw route envelope above already proves every adjacent raw
            # segment at final density and across all RiskFrame boundaries.
            # Reusing that proof avoids re-sweeping the same edge during DAG
            # construction; any assembled candidate is still rechecked as a
            # complete final curve below.
            edge_details["raw_baseline_reused"] = True
            edge_evidence.append({
                **edge_details,
                "accepted": True,
                "reason": None,
            })
            return AnyAngleDecision(True, evidence=edge_details)
        if any(value is None for value in edge.sample_times):
            return AnyAngleDecision(False, "eta_missing_for_shortcut")
        edge_samples = tuple(
            {"lon": point[0], "lat": point[1], "eta": sampled_at}
            for point, sampled_at in zip(edge.points, edge.sample_times, strict=True)
        )
        direct_edge = edge.start_index == 0 and edge.end_index == len(raw_points) - 1
        if direct_edge:
            # Keep the frozen r17 direct-line diagnostic reproducible alongside
            # the real swept-cell proof.  The 1 km lattice is only a reported
            # diagnostic (not a qualification shortcut); the full envelope
            # below still checks grid crossings and every RiskFrame boundary.
            try:
                direct_samples = tuple(
                    risk_sampler.sample(sampled_at, point[0], point[1])
                    for point, sampled_at in zip(
                        edge.points, edge.sample_times, strict=True
                    )
                )
            except Exception as exc:
                edge_details["direct_1km_diagnostic"] = {
                    "sample_count": 0,
                    "hard_sample_count": None,
                    "first_hard_sample": None,
                    "failure_reason": f"{type(exc).__name__}",
                }
            else:
                hard_samples = tuple(
                    value for value in direct_samples if value.hard_mask
                )
                edge_details["direct_1km_diagnostic"] = {
                    "sample_count": len(direct_samples),
                    "hard_sample_count": len(hard_samples),
                    "first_hard_sample": (
                        {
                            "longitude": hard_samples[0].longitude,
                            "latitude": hard_samples[0].latitude,
                            "eta": _iso(hard_samples[0].sampled_at),
                        }
                        if hard_samples
                        else None
                    ),
                }
        envelope = risk_sampler.sample_swept_temporal_envelope(
            edge_samples,
            sample_spacing_m=edge_probe_spacing_m,
            # The direct edge is a required full proof and diagnostic.  Other
            # edges are only pre-screened here and receive their complete
            # envelope after joint geometry is assembled.
            fail_fast=not direct_edge,
        )
        details: dict[str, Any] = {
            **edge_details,
            "temporal_envelope": _compact_swept_envelope(envelope),
        }
        if envelope.coverage_complete and envelope.hard_mask_possible:
            decision = AnyAngleDecision(False, "hard_mask_or_unknown", details)
        elif not envelope.usable:
            decision = AnyAngleDecision(False, "temporal_risk_coverage", details)
        else:
            corridor = _line_corridor(
                corridor_validator,
                edge.points,
                tuple(value for value in edge.sample_times if value is not None),
                corridor_buffer_m,
            )
            details["corridor"] = corridor
            if corridor is None:
                decision = AnyAngleDecision(
                    False, "continuous_corridor_not_proved", details
                )
            else:
                # ETA/speed is a route-level gate.  Rejecting a shortcut here
                # based on its aggregate minimum factor used to discard edges
                # before joint geometry and local time parameterisation were
                # available.  Keep the edge screen limited to gates 1--3;
                # the complete candidate is checked in _qualified_joint_record.
                details["deferred_gates"] = [
                    "manoeuvring",
                    "eta_speed",
                    "risk_non_degradation",
                    "adaptive_trust_deviation",
                ]
                decision = AnyAngleDecision(True, evidence=details)
        edge_evidence.append({**details, "accepted": decision.accepted, "reason": decision.reason})
        return decision

    candidates = build_any_angle_candidates(
        raw_points,
        waypoint_times=tuple(waypoint.eta for waypoint in plan.waypoints),
        sample_spacing_m=edge_probe_spacing_m,
        edge_validator=edge_validator,
        maximum_edge_evaluations=_FORMAL_ANY_ANGLE_EDGE_EVALUATION_LIMIT,
        maximum_candidates=_FORMAL_ANY_ANGLE_MAXIMUM_CANDIDATES,
    )
    attempts: list[dict[str, Any]] = []
    qualified: list[tuple[RouteMotionRecord, dict[str, Any]]] = []
    last_reason = "no_qualified_any_angle_candidate"
    for route in candidates:
        route_evidence: dict[str, Any] = {
            "any_angle": route.to_dict(),
        }
        trim_attempts: list[dict[str, Any]] = []
        route_accepted = False
        attempted_fractions = (
            fraction
            for fraction in _FORMAL_TRIM_FRACTIONS
            if fraction <= chosen_policy.max_trim_fraction + 1.0e-12
        )
        for requested_fraction in attempted_fractions:
            joint_summary: dict[str, Any] = {
                "requested_max_trim_fraction": requested_fraction,
                "accepted": False,
            }
            try:
                joint = build_joint_bspline(
                    route,
                    sample_spacing_m=chosen_policy.sample_spacing_m,
                    max_trim_fraction=requested_fraction,
                    maximum_overlap_fraction=chosen_policy.maximum_overlap_fraction,
                    maximum_route_points=chosen_policy.maximum_route_points,
                )
                joint_summary = _compact_joint_evidence(joint, requested_fraction)
                if not joint.applied:
                    raise _QualificationFailure(
                        joint.fallback_reason or "joint_smoothness_gate",
                        gate="manoeuvring",
                        evidence={"joint": joint_summary},
                    )
                record, final_evidence = _qualified_joint_record(
                    plan,
                    route,
                    joint,
                    raw_digest=raw_digest,
                    profile=profile,
                    risk_sampler=risk_sampler,
                    corridor_validator=corridor_validator,
                    corridor_buffer_m=corridor_buffer_m,
                    raw_envelope=raw_envelope,
                    sample_spacing_m=chosen_policy.sample_spacing_m,
                )
            except _QualificationFailure as failure:
                last_reason = failure.reason
                trim_attempts.append(
                    {
                        **joint_summary,
                        "accepted": False,
                        "reason": failure.reason,
                        "failure_gate": failure.gate,
                        "failure_evidence": dict(failure.evidence),
                    }
                )
                continue
            # Qualification details must bind the selected any-angle route and
            # its edge decisions, not just the final curve gate measurements.
            final_evidence = {
                **final_evidence,
                "any_angle": route.to_dict(),
                "edge_evidence": list(edge_evidence),
                "joint_attempts": [*trim_attempts, {**joint_summary, "accepted": True}],
            }
            record = replace(
                record,
                qualification=replace(
                    record.qualification,
                    details_digest=canonical_sha256(final_evidence),
                ),
            )
            route_evidence.update(final_evidence)
            route_evidence["record_details"] = final_evidence
            route_evidence["accepted"] = True
            attempts.append(route_evidence)
            qualified.append((record, route_evidence))
            route_accepted = True
            break
        if not route_accepted:
            attempts.append(
                {
                    **route_evidence,
                    "joint_attempts": trim_attempts,
                    "accepted": False,
                    "reason": last_reason,
                }
            )

    if qualified:
        record, selected_evidence = min(
            qualified,
            key=lambda value: (
                float(value[1].get("geometry", {}).get("curve_length_m", math.inf)),
                tuple(value[1].get("any_angle", {}).get("waypoint_indices", ())),
            ),
        )
        evidence_base["candidates"] = attempts
        evidence_base["edge_evidence"] = list(edge_evidence)
        evidence_base.update(selected_evidence)
        evidence_base["selected"] = True
        evidence_base["details_digest"] = record.qualification.details_digest
        return record, evidence_base
    evidence_base["candidates"] = attempts
    evidence_base["edge_evidence"] = list(edge_evidence)
    evidence_base["selected"] = False
    return _fallback_with_evidence(
        plan,
        raw_digest,
        last_reason,
        raw_points=raw_points,
        evidence=evidence_base,
    )


def _fallback_with_evidence(
    plan: RoutePlanV3,
    raw_digest: str,
    reason: str,
    *,
    raw_points: Sequence[Coordinate],
    evidence: Mapping[str, Any] | None = None,
) -> tuple[RouteMotionRecord, dict[str, Any]]:
    details = dict(evidence or {})
    details.setdefault(
        "schema_version", ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION
    )
    details.setdefault("plan_id", plan.plan_id)
    details.setdefault("planning_layer", plan.planning_layer.value)
    details.setdefault("raw_route_digest", raw_digest)
    details.setdefault("gate_order", list(_FORMAL_GATE_ORDER))
    details["accepted"] = False
    details["fallback_reason"] = reason
    details["raw_waypoint_count"] = len(raw_points)
    record = _raw_record(plan, raw_digest, reason)
    qualification = replace(
        record.qualification,
        details_digest=canonical_sha256(details),
    )
    return replace(record, qualification=qualification), details


def _risk_identity_dict(sampler: RiskSampler) -> dict[str, Any]:
    identity = sampler.identity
    return {
        name: (
            getattr(identity, name).value
            if hasattr(getattr(identity, name), "value")
            else getattr(identity, name)
        )
        for name in (
            "run_id",
            "scenario_id",
            "corridor_id",
            "vessel_profile_id",
            "config_digest",
            "model_config_digest",
            "generation_id",
            "model_version",
            "grid_id",
            "coordinate_digest",
        )
    }


def _compact_swept_envelope(envelope: Any) -> dict[str, Any]:
    """Keep per-edge evidence bounded without weakening the full final gate."""

    value = dict(envelope.to_dict())
    source_ids = value.pop("source_risk_ids", ())
    boundaries = value.pop("covered_frame_boundaries", ())
    value["source_risk_id_count"] = len(source_ids)
    value["covered_frame_boundary_count"] = len(boundaries)
    return value


def _compact_joint_evidence(
    geometry: JointBSplineResult,
    requested_fraction: float,
) -> dict[str, Any]:
    """Record joint-window measurements without duplicating curve samples."""

    return {
        "requested_max_trim_fraction": requested_fraction,
        "status": geometry.status,
        "applied": geometry.applied,
        "fallback_reason": geometry.fallback_reason,
        "span_count": len(geometry.span_control_points_m),
        "joint_windows": [window.to_dict() for window in geometry.joint_windows],
        "substantive_turn_node_indices": list(
            geometry.substantive_turn_node_indices
        ),
        "c2_pass": geometry.c2_pass,
        "no_reverse_curvature_pass": geometry.no_reverse_curvature_pass,
        "no_self_intersection_pass": geometry.no_self_intersection_pass,
        "monotonic_pass": geometry.monotonic_pass,
        "full_route_g2_pass": geometry.full_route_g2_pass,
        "minimum_radius_m": (
            geometry.minimum_radius_m
            if math.isfinite(geometry.minimum_radius_m)
            else None
        ),
        "route_length_m": geometry.route_length_m,
        "maximum_deviation_to_base_m": geometry.maximum_deviation_to_base_m,
        "sample_count": len(geometry.points),
    }


def _line_corridor(
    validator: CorridorValidator,
    points: Sequence[Coordinate],
    times: Sequence[datetime],
    expansion_m: float,
) -> dict[str, Any] | None:
    if len(points) < 2 or len(points) != len(times):
        return None
    try:
        _, local_points, _ = path_metric(points)
        edge_validator = getattr(validator, "for_edge", None)
        value = (edge_validator if callable(edge_validator) else validator)(
            _CorridorHulls(
                tuple((first, second) for first, second in pairwise(local_points)),
                0,
            ),
            tuple(points),
            tuple(times),
            expansion_m,
        )
    except Exception:
        return None
    evidence = dict(value) if isinstance(value, Mapping) else {"accepted": value is True}
    if not (
        evidence.get("accepted") is True
        and evidence.get("complete") is True
        and evidence.get("coverage_complete") is True
        and evidence.get("hard_mask_envelope_complete") is True
        and evidence.get("continuous_containment_proved") is True
        and evidence.get("continuous_containment_scope") == CONTINUOUS_RASTER_MODEL_SCOPE
    ):
        return None
    return evidence


def _anchored_joint_motion(
    plan: RoutePlanV3,
    geometry: JointBSplineResult,
) -> tuple[
    tuple[MotionSample, ...],
    tuple[WaypointMotionAnchor, ...],
    tuple[float, ...],
    tuple[float, ...],
]:
    raw_points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    raw_times = tuple(waypoint.eta for waypoint in plan.waypoints)
    authoritative_points = list(geometry.points)
    if len(authoritative_points) != len(geometry.curvatures_m_inv):
        raise _QualificationFailure("joint_curvature_sample_mismatch")
    if len(authoritative_points) < 2:
        raise _QualificationFailure("motion_path_requires_two_points")
    authoritative_points[0] = raw_points[0]
    authoritative_points[-1] = raw_points[-1]
    authoritative_points_tuple = tuple(authoritative_points)

    # A skipped raw waypoint remains an ETA anchor, but is not written into
    # the selected geometry.  Project it onto an ordered curve segment and
    # insert a precise same-ETA sample at that segment fraction.  The
    # projection is deliberately done before geographic distance/time
    # parameterisation: nearest sampled-point binding can move an anchor by a
    # whole motion sample and manufacture an interval-speed violation.
    projections = project_waypoint_anchors(raw_points, authoritative_points_tuple)
    if projections is None or len(projections) != len(raw_points):
        raise _QualificationFailure("non_monotonic_curve_anchors")

    projections_by_segment: dict[int, list[Any]] = {}
    for projection in projections[1:-1]:
        projections_by_segment.setdefault(projection.segment_index, []).append(projection)

    expanded_points: list[Coordinate] = []
    expanded_curvatures: list[float] = []
    resolved_anchor_indices: list[int] = [-1] * len(raw_points)
    for segment_index, (first, second) in enumerate(
        pairwise(authoritative_points_tuple)
    ):
        if not expanded_points:
            expanded_points.append(first)
            expanded_curvatures.append(geometry.curvatures_m_inv[segment_index])
        for projection in sorted(
            projections_by_segment.get(segment_index, ()),
            key=lambda value: (value.fraction, value.waypoint_index),
        ):
            fraction = projection.fraction
            if fraction <= 1.0e-9:
                resolved_anchor_indices[projection.waypoint_index] = len(expanded_points) - 1
                continue
            if fraction >= 1.0 - 1.0e-9:
                # The endpoint is resolved below when the next segment (or
                # the final point) is appended.  Keeping this branch explicit
                # avoids creating a zero-length interval at a vertex.
                continue
            point = great_circle_interpolate(first, second, fraction)
            expanded_points.append(point)
            expanded_curvatures.append(
                max(
                    geometry.curvatures_m_inv[segment_index],
                    geometry.curvatures_m_inv[segment_index + 1],
                )
            )
            resolved_anchor_indices[projection.waypoint_index] = len(expanded_points) - 1
        expanded_points.append(second)
        expanded_curvatures.append(geometry.curvatures_m_inv[segment_index + 1])

    # Resolve anchors that fall exactly on an existing sampled vertex.  The
    # projection API is monotone, so the first matching vertex is stable and
    # all remaining anchors retain strict order.
    for projection in projections:
        if resolved_anchor_indices[projection.waypoint_index] >= 0:
            continue
        if projection.waypoint_index == 0:
            resolved_anchor_indices[0] = 0
        elif projection.waypoint_index == len(raw_points) - 1:
            resolved_anchor_indices[-1] = len(expanded_points) - 1
        elif projection.fraction >= 1.0 - 1.0e-9:
            segment_end = projection.segment_index + 1
            # Inserted points only occur before the endpoint, therefore the
            # geographic endpoint can be located by the projected segment's
            # original vertex and its stable expansion offset.
            matching = [
                index
                for index, point in enumerate(expanded_points)
                if great_circle_distance_m(point, authoritative_points_tuple[segment_end])
                <= 1.0e-3
            ]
            if not matching:
                raise _QualificationFailure("non_monotonic_curve_anchors")
            resolved_anchor_indices[projection.waypoint_index] = matching[-1]
        else:
            raise _QualificationFailure("non_monotonic_curve_anchors")

    if any(index < 0 for index in resolved_anchor_indices):
        raise _QualificationFailure("non_monotonic_curve_anchors")
    anchor_indices = tuple(resolved_anchor_indices)
    if any(current <= previous for previous, current in pairwise(anchor_indices)):
        raise _QualificationFailure("non_monotonic_curve_anchors")
    authoritative_points_tuple = tuple(expanded_points)
    # ETA/arc-length anchors use the same spherical metric as any-angle edge
    # construction and final interval-speed checks.  The local equirectangular
    # frame remains appropriate for spline control geometry and raster
    # corridor proofs, but using it here overstates long high-latitude spans
    # and can manufacture a speed violation that is not present on the
    # published geographic curve.
    distances = _geodesic_path_distances(authoritative_points_tuple)
    anchor_distances = tuple(distances[index] for index in anchor_indices)
    if any(current <= previous for previous, current in pairwise(anchor_distances)):
        raise _QualificationFailure("non_monotonic_curve_anchor_projection")
    times = tuple(
        time_at_distance(distance, anchor_distances, raw_times) for distance in distances
    )
    if any(current <= previous for previous, current in pairwise(times)):
        raise _QualificationFailure("non_monotonic_anchored_eta")
    courses = _courses(authoritative_points_tuple)
    speeds = _interval_speeds(distances, times)
    if len(expanded_curvatures) != len(authoritative_points_tuple):
        raise _QualificationFailure("joint_curvature_sample_mismatch")
    samples = tuple(
        MotionSample(point[0], point[1], eta, course, speed)
        for point, eta, course, speed in zip(
            authoritative_points_tuple, times, courses, speeds, strict=True
        )
    )
    anchors = tuple(
        WaypointMotionAnchor(
            waypoint_index=index,
            eta=raw_times[index],
            motion_sample_index=sample_index,
            arc_length_m=distances[sample_index],
        )
        for index, sample_index in enumerate(anchor_indices)
    )
    return samples, anchors, distances, tuple(expanded_curvatures)


def _geodesic_path_distances(points: Sequence[Coordinate]) -> tuple[float, ...]:
    """Return cumulative spherical arc length for an ordered motion path."""

    if len(points) < 2:
        raise _QualificationFailure("motion_path_requires_two_points")
    distances = [0.0]
    for first, second in pairwise(points):
        length = great_circle_distance_m(first, second)
        if not math.isfinite(length) or length <= 1.0e-9:
            raise _QualificationFailure("zero_length_motion_interval")
        distances.append(distances[-1] + length)
    return tuple(distances)


def _distance_metrics(
    candidate_points: Sequence[Coordinate], raw_points: Sequence[Coordinate]
) -> tuple[float, float]:
    _, candidate_local, _ = path_metric(candidate_points)
    _, raw_local, _ = path_metric(raw_points)
    candidate_to_raw = max(
        _distance_to_polyline(point, raw_local) for point in candidate_local
    )
    raw_to_candidate = max(
        _distance_to_polyline(point, candidate_local) for point in raw_local
    )
    return candidate_to_raw, raw_to_candidate


def _distance_to_polyline(point: Coordinate, polyline: Sequence[Coordinate]) -> float:
    def distance_to_segment(value: Coordinate, start: Coordinate, end: Coordinate) -> float:
        vector = (end[0] - start[0], end[1] - start[1])
        denominator = vector[0] ** 2 + vector[1] ** 2
        if denominator <= 1.0e-18:
            return math.hypot(value[0] - start[0], value[1] - start[1])
        fraction = max(
            0.0,
            min(
                1.0,
                ((value[0] - start[0]) * vector[0] + (value[1] - start[1]) * vector[1])
                / denominator,
            ),
        )
        projected = (start[0] + fraction * vector[0], start[1] + fraction * vector[1])
        return math.hypot(value[0] - projected[0], value[1] - projected[1])

    if len(polyline) < 2:
        raise _QualificationFailure("deviation_reference_route_invalid")
    return min(
        distance_to_segment(point, first, second) for first, second in pairwise(polyline)
    )


def _trust_deviation_limit(
    sampler: RiskSampler,
    profile: EngineeringRouteMotionProfile,
    corridor_buffer_m: float,
    corridor_evidence: Mapping[str, Any] | None = None,
) -> tuple[float, str]:
    try:
        grid = sampler.frames[0].payload
        latitudes = tuple(float(value) for value in grid.coords["latitude"].values)
        longitudes = tuple(float(value) for value in grid.coords["longitude"].values)
        if len(latitudes) < 2 or len(longitudes) < 2:
            raise ValueError("risk grid has fewer than two axes")
        latitude = math.radians(sum(latitudes) / len(latitudes))
        north_south = abs(latitudes[1] - latitudes[0]) * math.pi / 180.0 * 6_371_008.8
        east_west = (
            abs(longitudes[1] - longitudes[0])
            * math.pi
            / 180.0
            * 6_371_008.8
            * max(1.0e-6, math.cos(latitude))
        )
    except Exception as exc:
        raise _QualificationFailure("safety_clearance_unknown") from exc
    half_cell_diagonal = 0.5 * math.hypot(north_south, east_west)
    explicit_error = max(0.0, corridor_buffer_m - profile.primary_corridor_margin_m)
    limit = half_cell_diagonal - profile.beam_m / 2.0 - explicit_error
    clearance = (
        corridor_evidence.get("minimum_safe_clearance_m")
        if isinstance(corridor_evidence, Mapping)
        else None
    )
    if clearance is not None:
        if (
            isinstance(clearance, bool)
            or not isinstance(clearance, (int, float))
            or not math.isfinite(float(clearance))
            or float(clearance) <= 0.0
        ):
            raise _QualificationFailure("safety_clearance_unknown")
        limit = min(limit, float(clearance) - profile.beam_m / 2.0 - explicit_error)
        source = "corridor_raster_clearance"
    else:
        source = "risk_grid_half_cell_diagonal"
    if not math.isfinite(limit) or limit <= 0.0:
        raise _QualificationFailure("safety_clearance_unknown")
    return limit, source


def _distance_to_hull(point: Coordinate, hull: Sequence[Coordinate]) -> float:
    """Return a stable distance used only to associate a point with a span."""

    if len(hull) < 2:
        return math.inf
    edges = tuple(pairwise(hull))
    if len(hull) > 2:
        edges += ((hull[-1], hull[0]),)
    return min(
        _distance_to_polyline(point, (first, second)) for first, second in edges
    )


def _adaptive_trust_evidence(
    *,
    sampler: RiskSampler,
    profile: EngineeringRouteMotionProfile,
    corridor_buffer_m: float,
    corridor_evidence: Mapping[str, Any],
    geometry: JointBSplineResult,
    samples: Sequence[MotionSample],
    raw_points: Sequence[Coordinate],
) -> dict[str, Any]:
    """Check bidirectional deviation against nearby span clearance.

    The scalar half-cell limit remains the conservative compatibility fallback
    for custom validators that predate the span-clearance field.  The formal
    raster validator supplies one clearance per actual B-spline span; those
    local limits are used independently so open-water spans do not inherit a
    boundary span's smaller trust radius.
    """

    global_limit, source = _trust_deviation_limit(
        sampler, profile, corridor_buffer_m, corridor_evidence
    )
    _, candidate_local, _ = path_metric(
        tuple((sample.longitude, sample.latitude) for sample in samples)
    )
    _, raw_local, _ = path_metric(raw_points)
    spline_hulls = tuple(geometry.span_convex_hulls_m)
    raw_clearances = corridor_evidence.get("span_safe_clearance_m")
    local_clearances: tuple[float, ...] | None = None
    if isinstance(raw_clearances, Sequence) and not isinstance(
        raw_clearances, (str, bytes)
    ) and len(raw_clearances) >= len(spline_hulls) and spline_hulls:
        try:
            parsed = tuple(float(value) for value in raw_clearances[: len(spline_hulls)])
        except (TypeError, ValueError):
            parsed = ()
        if parsed and all(math.isfinite(value) and value > 0.0 for value in parsed):
            local_clearances = parsed

    if local_clearances is None:
        candidate_to_raw, raw_to_candidate = _distance_metrics(
            tuple((sample.longitude, sample.latitude) for sample in samples),
            raw_points,
        )
        if max(candidate_to_raw, raw_to_candidate) > global_limit + 1.0e-6:
            raise _QualificationFailure("adaptive_trust_deviation_exceeded")
        return {
            "candidate_to_raw_max_m": candidate_to_raw,
            "raw_to_candidate_max_m": raw_to_candidate,
            "trust_limit_m": global_limit,
            "clearance_source": f"{source}:coarse_compatibility_fallback",
            "local_clearance_evidence": False,
            "anchor_projection_count": len(samples),
        }

    explicit_error = max(0.0, corridor_buffer_m - profile.primary_corridor_margin_m)
    local_limits = tuple(
        min(
            global_limit,
            clearance - profile.beam_m / 2.0 - explicit_error,
        )
        for clearance in local_clearances
    )
    if any(not math.isfinite(value) or value <= 0.0 for value in local_limits):
        raise _QualificationFailure("safety_clearance_unknown")

    def nearest_span(point: Coordinate) -> int:
        return min(
            range(len(spline_hulls)),
            key=lambda index: (_distance_to_hull(point, spline_hulls[index]), index),
        )

    candidate_deviations = tuple(
        (
            _distance_to_polyline(point, candidate_local),
            nearest_span(point),
        )
        for point in candidate_local
    )
    raw_deviations = tuple(
        (
            _distance_to_polyline(point, raw_local),
            nearest_span(point),
        )
        for point in raw_local
    )
    violations = [
        {
            "direction": direction,
            "point_index": point_index,
            "span_index": span_index,
            "deviation_m": deviation,
            "trust_limit_m": local_limits[span_index],
        }
        for direction, values in (
            ("candidate_to_raw", candidate_deviations),
            ("raw_to_candidate", raw_deviations),
        )
        for point_index, (deviation, span_index) in enumerate(values)
        if deviation > local_limits[span_index] + 1.0e-6
    ]
    if violations:
        raise _QualificationFailure("adaptive_trust_deviation_exceeded")
    return {
        "candidate_to_raw_max_m": max(value[0] for value in candidate_deviations),
        "raw_to_candidate_max_m": max(value[0] for value in raw_deviations),
        "trust_limit_m": min(local_limits),
        "clearance_source": f"{source}:local_span_clearance",
        "local_clearance_evidence": True,
        "span_safe_clearance_m": list(local_clearances),
        "span_trust_limit_m": list(local_limits),
        "violations": [],
        "anchor_projection_count": len(samples),
    }


def _qualified_joint_record(
    plan: RoutePlanV3,
    route: AnyAngleRoute,
    geometry: JointBSplineResult,
    *,
    raw_digest: str,
    profile: EngineeringRouteMotionProfile,
    risk_sampler: RiskSampler,
    corridor_validator: CorridorValidator,
    corridor_buffer_m: float,
    raw_envelope: Any,
    sample_spacing_m: float,
) -> tuple[RouteMotionRecord, dict[str, Any]]:
    try:
        samples, anchors, curve_distances, motion_curvatures = _anchored_joint_motion(
            plan, geometry
        )
    except _QualificationFailure as failure:
        if failure.gate is not None:
            raise
        anchor_gate = (
            "adaptive_trust_deviation"
            if "anchor" in failure.reason
            else "manoeuvring"
        )
        raise _QualificationFailure(
            failure.reason,
            gate=anchor_gate,
            evidence=failure.evidence,
        ) from failure
    # Screen the candidate with the same swept-cell lattice before building
    # the full evidence envelope.  A hard/unknown point is terminal at gate 1
    # and need not spend time enumerating all 145 RiskFrame boundaries.  A
    # passing screen is never itself qualification: the complete envelope is
    # still rebuilt below for gates 1, 2 and 6.
    candidate_screen = risk_sampler.sample_swept_temporal_envelope(
        samples,
        sample_spacing_m=sample_spacing_m,
        fail_fast=True,
    )
    if candidate_screen.hard_mask_possible:
        raise _QualificationFailure(
            "hard_mask_or_unknown",
            gate="sea_land_hard_mask",
            evidence={"envelope": _compact_swept_envelope(candidate_screen)},
        )
    if not candidate_screen.usable:
        raise _QualificationFailure(
            "temporal_risk_coverage",
            gate="temporal_risk_coverage",
            evidence={"envelope": _compact_swept_envelope(candidate_screen)},
        )
    # A successful fail-fast run is complete: it only returns early on a
    # terminal hard/unknown/coverage failure.  Reuse that complete result so
    # a safe candidate is not swept through the same 145-frame window twice.
    candidate_envelope = candidate_screen
    gate_evidence: dict[str, Any] = {}
    gate_evidence["sea_land_hard_mask"] = {
        "risk_hard_mask_pass": (
            not candidate_envelope.hard_mask_possible
        ),
        "hard_mask_possible": candidate_envelope.hard_mask_possible,
        "unknown_fail_closed": True,
        "screening_envelope": _compact_swept_envelope(candidate_screen),
    }
    if candidate_envelope.hard_mask_possible:
        raise _QualificationFailure(
            "hard_mask_or_unknown",
            gate="sea_land_hard_mask",
            evidence={"envelope": _compact_swept_envelope(candidate_envelope)},
        )
    gate_evidence["temporal_risk_coverage"] = candidate_envelope.to_dict()
    if not candidate_envelope.usable:
        raise _QualificationFailure(
            "temporal_risk_coverage",
            gate="temporal_risk_coverage",
            evidence={"envelope": _compact_swept_envelope(candidate_envelope)},
        )
    corridor = _corridor(corridor_validator, geometry, samples, corridor_buffer_m)
    gate_evidence["corridor_allowed_area"] = corridor
    motion = _validate_manoeuvring(profile, geometry, samples, motion_curvatures)
    gate_evidence["manoeuvring"] = {
        "joint": {
            "window_count": len(geometry.joint_windows),
            "turn_count": len(geometry.substantive_turn_node_indices),
            "c2_pass": geometry.c2_pass,
            "no_reverse_curvature_pass": geometry.no_reverse_curvature_pass,
            "no_self_intersection_pass": geometry.no_self_intersection_pass,
            "monotonic_pass": geometry.monotonic_pass,
        },
        "motion": motion,
    }
    gate_evidence["eta_speed"] = _validate_eta_speed(
        profile,
        risk_sampler,
        samples,
        candidate_envelope,
        anchors,
        curve_distances,
    )
    candidate_to_raw, raw_to_candidate = _distance_metrics(
        tuple((sample.longitude, sample.latitude) for sample in samples),
        tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints),
    )
    raw_maximum = raw_envelope.max_risk_upper
    candidate_maximum = candidate_envelope.max_risk_upper
    raw_integrated = raw_envelope.integrated_risk_hours
    candidate_integrated = candidate_envelope.integrated_risk_hours
    if (
        raw_maximum is None
        or candidate_maximum is None
        or raw_integrated is None
        or candidate_integrated is None
        or candidate_maximum > raw_maximum + 1.0e-9
        or candidate_integrated > raw_integrated + 1.0e-9
    ):
        raise _QualificationFailure(
            "risk_non_degradation",
            gate="risk_non_degradation",
            evidence={
                "candidate_maximum_upper": candidate_maximum,
                "raw_maximum_upper": raw_maximum,
                "candidate_integrated_risk_hours": candidate_integrated,
                "raw_integrated_risk_hours": raw_integrated,
            },
        )
    gate_evidence["risk_non_degradation"] = {
        "candidate_maximum_upper": candidate_maximum,
        "raw_maximum_upper": raw_maximum,
        "candidate_integrated_risk_hours": candidate_integrated,
        "raw_integrated_risk_hours": raw_integrated,
        "same_sample_spacing_m": sample_spacing_m,
    }
    try:
        trust_evidence = _adaptive_trust_evidence(
            sampler=risk_sampler,
            profile=profile,
            corridor_buffer_m=corridor_buffer_m,
            corridor_evidence=corridor,
            geometry=geometry,
            samples=samples,
            raw_points=tuple(
                (waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints
            ),
        )
    except _QualificationFailure as failure:
        if failure.gate is not None:
            raise
        raise _QualificationFailure(
            failure.reason,
            gate="adaptive_trust_deviation",
            evidence=failure.evidence,
        ) from failure
    trust_evidence["anchor_projection_count"] = len(anchors)
    gate_evidence["adaptive_trust_deviation"] = trust_evidence
    details: dict[str, Any] = {
        "schema_version": ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "planning_layer": plan.planning_layer.value,
        "raw_route_digest": raw_digest,
        "gate_order": list(_FORMAL_GATE_ORDER),
        "selected_any_angle": route.to_dict(),
        "joint_window": {
            "windows": [window.to_dict() for window in geometry.joint_windows],
            "turn_node_indices": list(geometry.substantive_turn_node_indices),
            "c2_pass": geometry.c2_pass,
            "full_route_g2_pass": geometry.full_route_g2_pass,
        },
        "gates": gate_evidence,
        "geometry": {
            "curve_length_m": curve_distances[-1],
            # A straight route has an unbounded radius.  JSON evidence must
            # use null rather than non-standard Infinity so its digest and
            # schema validation remain deterministic.
            "minimum_radius_m": (
                geometry.minimum_radius_m
                if math.isfinite(geometry.minimum_radius_m)
                else None
            ),
            "maximum_deviation_to_base_m": geometry.maximum_deviation_to_base_m,
            "candidate_to_raw_max_m": candidate_to_raw,
            "raw_to_candidate_max_m": raw_to_candidate,
            "motion_sample_count": len(samples),
        },
        "input_digests": {
            "raw_route_digest": raw_digest,
            "risk_interval_evaluator_digest": risk_sampler.interval_evaluator_digest,
            "profile_digest": profile.digest,
        },
        "resource_counts": {
            "any_angle_edge_count": len(route.edges),
            "any_angle_evaluated_edge_count": route.evaluated_edge_count,
            "any_angle_edge_evaluation_limit": route.maximum_edge_evaluations,
            "shortcut_count": route.shortcut_count,
            "joint_span_count": len(geometry.span_control_points_m),
            "risk_sample_count": len(candidate_envelope.sampled_risks),
            "risk_interval_count": len(candidate_envelope.interval_samples),
        },
        "real_vessel_calibrated": False,
        "navigation_grade": False,
        "bathymetry_checked": False,
        "ukc_checked": False,
    }
    curve_payload = [[sample.longitude, sample.latitude] for sample in samples]
    motion_payload = [_sample_dict(sample) for sample in samples]
    record = RouteMotionRecord(
        planning_layer=plan.planning_layer,
        plan_id=plan.plan_id,
        raw_route_digest=raw_digest,
        mode=RouteMotionMode.CURVE,
        fallback_reason=None,
        curve_digest=canonical_sha256(curve_payload),
        motion_digest=canonical_sha256(motion_payload),
        interpolation=ROUTE_MOTION_INTERPOLATION,
        waypoint_anchors=anchors,
        motion_samples=samples,
        qualification=RouteMotionQualification(
            result="QUALIFIED_ENGINEERING_REFERENCE",
            risk_rechecked=True,
            hard_mask_rechecked=True,
            coverage_complete=True,
            eta_anchors_preserved=True,
            speed_checked=True,
            curvature_checked=True,
            corridor_checked=True,
            manoeuvring_checked=True,
            corridor_proof_scope=CONTINUOUS_RASTER_MODEL_SCOPE,
            evidence_kind=profile.evidence_kind,
            real_vessel_calibrated=False,
            details_digest=canonical_sha256(details),
        ),
    )
    return record, details


def _raw_record(plan: RoutePlanV3, raw_digest: str, reason: str) -> RouteMotionRecord:
    samples = _raw_motion_samples(plan)
    _, _, distances = path_metric(
        tuple((sample.longitude, sample.latitude) for sample in samples)
    )
    anchors = tuple(
        WaypointMotionAnchor(index, sample.eta, index, distances[index])
        for index, sample in enumerate(samples)
    )
    curve_digest = canonical_sha256(
        [[sample.longitude, sample.latitude] for sample in samples]
    )
    motion_digest = canonical_sha256([_sample_dict(sample) for sample in samples])
    return RouteMotionRecord(
        planning_layer=plan.planning_layer,
        plan_id=plan.plan_id,
        raw_route_digest=raw_digest,
        mode=RouteMotionMode.RAW_PASSTHROUGH,
        fallback_reason=reason,
        curve_digest=curve_digest,
        motion_digest=motion_digest,
        interpolation=ROUTE_MOTION_INTERPOLATION,
        waypoint_anchors=anchors,
        motion_samples=samples,
        qualification=RouteMotionQualification(
            result="RAW_FALLBACK",
            risk_rechecked=False,
            hard_mask_rechecked=False,
            coverage_complete=False,
            eta_anchors_preserved=True,
            speed_checked=False,
            curvature_checked=False,
            corridor_checked=False,
            manoeuvring_checked=False,
            corridor_proof_scope="NOT_PROVED",
            evidence_kind="FORMULA_DERIVED_ENGINEERING_REFERENCE",
            real_vessel_calibrated=False,
            details_digest=canonical_sha256({"fallback_reason": reason}),
        ),
    )


def _raw_motion_samples(plan: RoutePlanV3) -> tuple[MotionSample, ...]:
    points = tuple((waypoint.longitude, waypoint.latitude) for waypoint in plan.waypoints)
    courses = _courses(points)
    return tuple(
        MotionSample(
            waypoint.longitude,
            waypoint.latitude,
            waypoint.eta,
            course,
            waypoint.recommended_speed_mps / KNOT_TO_MPS,
        )
        for waypoint, course in zip(plan.waypoints, courses, strict=True)
    )


def _corridor(
    validator: CorridorValidator,
    geometry: MultiSpanRouteResult | JointBSplineResult,
    samples: Sequence[MotionSample],
    expansion_m: float,
) -> dict[str, Any]:
    if isinstance(geometry, JointBSplineResult):
        spline_hulls = geometry.span_convex_hulls_m
    else:
        spline_hulls = tuple(
            hull
            for segment in geometry.segments
            for hull in segment.span_convex_hulls_m
        )
    _, local_samples, _ = path_metric(
        tuple((sample.longitude, sample.latitude) for sample in samples)
    )
    line_hulls = tuple((first, second) for first, second in pairwise(local_samples))
    hulls = spline_hulls + line_hulls
    try:
        value = validator(
            _CorridorHulls(hulls, len(spline_hulls)),
            tuple((sample.longitude, sample.latitude) for sample in samples),
            tuple(sample.eta for sample in samples),
            expansion_m,
        )
    except Exception as exc:
        raise _QualificationFailure(
            "corridor_validator_error",
            gate="corridor_allowed_area",
            evidence={"exception_type": type(exc).__name__},
        ) from exc
    evidence = dict(value) if isinstance(value, Mapping) else {"accepted": value is True}
    evidence.setdefault("spline_span_hull_count", len(spline_hulls))
    evidence.setdefault("display_line_hull_count", len(line_hulls))
    if not (
        evidence.get("accepted") is True
        and evidence.get("complete") is True
        and evidence.get("coverage_complete") is True
        and evidence.get("hard_mask_envelope_complete") is True
        and evidence.get("continuous_containment_proved") is True
        and evidence.get("continuous_containment_scope") == CONTINUOUS_RASTER_MODEL_SCOPE
    ):
        raise _QualificationFailure(
            "continuous_corridor_not_proved",
            gate="corridor_allowed_area",
            evidence=evidence,
        )
    return evidence


def _validate_manoeuvring(
    profile: EngineeringRouteMotionProfile,
    geometry: MultiSpanRouteResult | JointBSplineResult,
    samples: Sequence[MotionSample],
    curvatures: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Validate curvature and motion response without a global speed cap.

    Environmental speed availability is a per-interval ETA gate.  It must not
    be collapsed to the minimum factor observed anywhere on a many-hour route;
    doing so rejected otherwise valid turns before their complete geometry was
    qualified.
    """

    speeds = tuple(sample.speed_knots for sample in samples)
    yaw_rates = []
    lateral_accelerations = []
    radii = []
    curvature_values = tuple(
        geometry.curvatures_m_inv if curvatures is None else curvatures
    )
    for speed_knots, curvature in zip(speeds, curvature_values, strict=True):
        speed_m_s = speed_knots * KNOT_TO_MPS
        curvature = abs(curvature)
        radius = math.inf if curvature == 0.0 else 1.0 / curvature
        if radius + 1.0e-9 < profile.minimum_radius_m(speed_knots):
            raise _QualificationFailure(
                "minimum_radius_exceeded", gate="manoeuvring"
            )
        yaw_rate = math.degrees(speed_m_s * curvature)
        lateral = speed_m_s**2 * curvature
        if yaw_rate > profile.maximum_yaw_rate_deg_s + 1.0e-12:
            raise _QualificationFailure(
                "maximum_yaw_rate_exceeded", gate="manoeuvring"
            )
        if lateral > profile.maximum_lateral_acceleration_m_s2 + 1.0e-12:
            raise _QualificationFailure(
                "lateral_acceleration_exceeded", gate="manoeuvring"
            )
        radii.append(radius)
        yaw_rates.append(yaw_rate)
        lateral_accelerations.append(lateral)
    if min(speeds) < profile.minimum_steerage_speed_knots - 1.0e-9:
        raise _QualificationFailure(
            "below_minimum_steerage_speed", gate="manoeuvring"
        )
    if max(speeds) > profile.maximum_speed_knots + 1.0e-9:
        raise _QualificationFailure("maximum_speed_exceeded", gate="manoeuvring")
    finite_radii = [value for value in radii if math.isfinite(value)]
    return {
        "minimum_speed_knots": min(speeds),
        "maximum_speed_knots": max(speeds),
        "minimum_radius_m": min(finite_radii) if finite_radii else None,
        "maximum_yaw_rate_deg_s": max(yaw_rates),
        "maximum_lateral_acceleration_m_s2": max(lateral_accelerations),
        "profile_digest": profile.digest,
    }


def _validate_eta_speed(
    profile: EngineeringRouteMotionProfile,
    risk_sampler: RiskSampler,
    samples: Sequence[MotionSample],
    envelope: Any | None = None,
    anchors: Sequence[WaypointMotionAnchor] | None = None,
    curve_distances: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Check every motion interval against its local temporal speed envelope.

    The swept envelope proves hard/unknown/coverage/risk for the densified
    moving path.  ETA feasibility is evaluated for every adjacent published
    motion sample along the moving great-circle segment.  Samples are taken at
    both endpoints and at every RiskFrame boundary crossed by that segment;
    this preserves the position/time pairing instead of applying a fixed
    endpoint's temporal minimum before the vessel reaches that endpoint.  The
    original waypoint anchors remain the authoritative interval boundaries and
    are recorded alongside the finer motion intervals.
    """

    if envelope is None:
        envelope = risk_sampler.sample_swept_temporal_envelope(samples)
    if not envelope.usable or envelope.hard_mask_possible:
        raise _QualificationFailure("temporal_risk_coverage", gate="temporal_risk_coverage")
    if anchors is None:
        anchor_indices = tuple(range(len(samples)))
    else:
        anchor_indices = tuple(anchor.motion_sample_index for anchor in anchors)
    if len(anchor_indices) < 2 or any(
        current <= previous for previous, current in pairwise(anchor_indices)
    ):
        raise _QualificationFailure(
            "non_monotonic_curve_anchors", gate="eta_speed"
        )
    if any(index >= len(samples) for index in anchor_indices):
        raise _QualificationFailure("temporal_risk_coverage", gate="eta_speed")
    if curve_distances is None:
        distances = _geodesic_path_distances(
            tuple((sample.longitude, sample.latitude) for sample in samples)
        )
    else:
        distances = tuple(float(value) for value in curve_distances)
    if len(distances) != len(samples):
        raise _QualificationFailure("temporal_risk_coverage", gate="eta_speed")

    if len(samples) < 2 or len(distances) != len(samples):
        raise _QualificationFailure("insufficient_motion_intervals", gate="eta_speed")

    def anchor_interval_for_motion_interval(index: int) -> int:
        for anchor_interval, (left_anchor, right_anchor) in enumerate(
            pairwise(anchor_indices)
        ):
            if left_anchor <= index < right_anchor:
                return anchor_interval
        raise _QualificationFailure(
            "motion_interval_outside_anchor_domain", gate="eta_speed"
        )

    required: list[float] = []
    available: list[float] = []
    factors: list[float] = []
    interval_evidence: list[dict[str, Any]] = []

    def moving_speed_lower(
        left: MotionSample, right: MotionSample
    ) -> tuple[float, int, tuple[str, ...]]:
        """Return a local lower bound while moving from ``left`` to ``right``.

        ``RiskSampler.sample_interval`` is intentionally a fixed-coordinate
        envelope and is already used by the swept-cell proof above.  It cannot
        be called at ``right`` for the whole ``[left.eta, right.eta]`` span:
        that would test the endpoint before the vessel arrives there.  The
        moving proof below samples the actual great-circle position at every
        crossed frame boundary.  Temporal interpolation is linear between
        compatible frames, so those boundaries plus the segment endpoints are
        the conservative temporal breakpoints for the local speed factor.
        """

        duration = (right.eta - left.eta).total_seconds()
        if duration <= 0.0:
            raise _QualificationFailure("invalid_motion_interval", gate="eta_speed")
        fractions = {0.0, 1.0}
        for frame in risk_sampler.frames[1:-1]:
            frame_time = frame.valid_time
            if left.eta < frame_time < right.eta:
                fractions.add(
                    (frame_time - left.eta).total_seconds() / duration
                )
        values = []
        source_ids: list[str] = []
        for fraction in sorted(fractions):
            point = great_circle_interpolate(
                (left.longitude, left.latitude),
                (right.longitude, right.latitude),
                fraction,
            )
            sampled_at = left.eta + (right.eta - left.eta) * fraction
            try:
                value = risk_sampler.sample(sampled_at, point[0], point[1])
            except Exception as exc:
                raise _QualificationFailure(
                    "temporal_risk_coverage",
                    gate="temporal_risk_coverage",
                    evidence={
                        "motion_interval_start": left.eta.isoformat(),
                        "motion_interval_end": right.eta.isoformat(),
                        "sample_fraction": fraction,
                        "exception_type": type(exc).__name__,
                    },
                ) from exc
            if value.hard_mask:
                raise _QualificationFailure(
                    "hard_mask_or_unknown",
                    gate="sea_land_hard_mask",
                    evidence={
                        "motion_interval_start": left.eta.isoformat(),
                        "motion_interval_end": right.eta.isoformat(),
                        "sample_fraction": fraction,
                    },
                )
            values.append(float(value.environment_speed_factor))
            source_ids.extend(value.source_risk_ids)
        factor = min(values) if values else math.nan
        if not math.isfinite(factor) or factor <= 0.0 or factor > 1.0:
            raise _QualificationFailure("environment_speed_unusable", gate="eta_speed")
        return factor, len(values), tuple(dict.fromkeys(source_ids))

    for interval_index, (left_index, right_index) in enumerate(
        pairwise(range(len(samples)))
    ):
        left = samples[left_index]
        right = samples[right_index]
        seconds = (right.eta - left.eta).total_seconds()
        distance_m = distances[right_index] - distances[left_index]
        if seconds <= 0.0 or distance_m <= 1.0e-6:
            raise _QualificationFailure("invalid_motion_interval", gate="eta_speed")
        required_knots = distance_m / seconds / KNOT_TO_MPS
        factor, speed_sample_count, speed_source_ids = moving_speed_lower(left, right)
        crosses_frame_boundary = speed_sample_count > 2
        speed_evaluation_method = "moving_segment_frame_boundary_lower"
        speed_limit = min(profile.maximum_speed_knots, profile.economic_speed_knots * factor)
        if speed_limit < profile.minimum_steerage_speed_knots - 1.0e-9:
            raise _QualificationFailure("environment_speed_unusable", gate="eta_speed")
        if required_knots < profile.minimum_steerage_speed_knots - 1.0e-9:
            raise _QualificationFailure(
                "below_minimum_steerage_speed", gate="eta_speed"
            )
        if required_knots > speed_limit + 1.0e-9:
            raise _QualificationFailure(
                "eta_speed_infeasible",
                gate="eta_speed",
                evidence={
                    "interval_index": interval_index,
                    "anchor_interval_index": anchor_interval_for_motion_interval(
                        interval_index
                    ),
                    "motion_sample_start": left_index,
                    "motion_sample_end": right_index,
                    "required_speed_knots": required_knots,
                    "available_speed_knots": speed_limit,
                    "environment_speed_factor_lower": factor,
                },
            )
        required.append(required_knots)
        available.append(speed_limit)
        factors.append(factor)
        interval_evidence.append({
            "index": interval_index,
            "anchor_interval_index": anchor_interval_for_motion_interval(
                interval_index
            ),
            "motion_sample_start": left_index,
            "motion_sample_end": right_index,
            "start": left.eta.isoformat().replace("+00:00", "Z"),
            "end": right.eta.isoformat().replace("+00:00", "Z"),
            "distance_m": distance_m,
            "duration_s": seconds,
            "required_speed_knots": required_knots,
            "environment_speed_factor_lower": factor,
            "available_speed_knots": speed_limit,
            "speed_evaluation_method": speed_evaluation_method,
            "speed_sample_count": speed_sample_count,
            "speed_source_risk_id_count": len(speed_source_ids),
            "crosses_frame_boundary": crosses_frame_boundary,
            "interval_evaluator_digest": risk_sampler.interval_evaluator_digest,
        })
    if not required:
        raise _QualificationFailure("insufficient_motion_intervals", gate="eta_speed")
    return {
        "required_speed_checked": True,
        "minimum_required_speed_knots": min(required),
        "maximum_required_speed_knots": max(required),
        "minimum_available_speed_knots": min(available),
        "minimum_environment_speed_factor_lower": min(factors),
        "speed_envelope": "moving_segment_frame_boundary_lower",
        "intervals": interval_evidence,
        "profile_digest": profile.digest,
    }


def _validate_sampler_identity(plan_set: FourLayerRoutePlanSet, sampler: RiskSampler) -> None:
    identity = sampler.identity
    for name in (
        "run_id", "scenario_id", "corridor_id", "vessel_profile_id",
        "model_config_digest", "generation_id",
    ):
        if getattr(identity, name) != getattr(plan_set, name):
            raise ValueError(f"RiskSampler identity differs from plan set: {name}")


def _courses(points: Sequence[Coordinate]) -> tuple[float, ...]:
    values = []
    for first, second in pairwise(points):
        lon1, lat1 = map(math.radians, first)
        lon2, lat2 = map(math.radians, second)
        delta_lon = lon2 - lon1
        x = math.sin(delta_lon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
        values.append(math.degrees(math.atan2(x, y)) % 360.0)
    values.append(values[-1])
    return tuple(values)


def _interval_speeds(
    distances_m: Sequence[float],
    times: Sequence[datetime],
) -> tuple[float, ...]:
    speeds = []
    for index in range(len(distances_m) - 1):
        seconds = (times[index + 1] - times[index]).total_seconds()
        distance = distances_m[index + 1] - distances_m[index]
        if seconds <= 0.0 or distance <= 0.0:
            raise _QualificationFailure("invalid_motion_interval")
        speeds.append(distance / seconds / KNOT_TO_MPS)
    speeds.append(speeds[-1])
    return tuple(speeds)


def _sample_dict(sample: MotionSample) -> dict[str, Any]:
    return {
        "lon": sample.longitude,
        "lat": sample.latitude,
        "eta": _iso(sample.eta),
        "course_degrees": sample.course_degrees,
        "speed_knots": sample.speed_knots,
    }


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: str, field: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")


class _QualificationFailure(Exception):
    def __init__(
        self,
        reason: str,
        *,
        gate: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.gate = gate
        self.evidence = dict(evidence or {})
        super().__init__(reason)


__all__ = [
    "ROUTE_MOTION_QUALIFICATION_EVIDENCE_SCHEMA_VERSION",
    "CorridorValidator",
    "build_route_motion_candidate_set",
    "build_route_motion_candidate_set_with_evidence",
    "build_route_motion_set",
    "build_route_motion_set_with_evidence",
    "merge_route_motion_qualification_evidence",
]
