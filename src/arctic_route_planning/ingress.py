"""Public formal BC ingress that joins a committed RiskSource to C planning."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from threading import RLock
from typing import Any

import numpy as np

from arctic_route_planning.config import PlanningConfiguration, configuration_digest
from arctic_route_planning.contracts import (
    HOURLY_RISK_INTERVAL,
    CommittedRiskSource,
    CommittedRiskWindow,
    ProvenanceKind,
    RiskWindowQuery,
    risk_frame_from_document,
    risk_frame_to_document,
    validate_canonical_risk_id,
)
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.errors import (
    ContextMismatchError,
    ContractError,
    PlanningCancelled,
    RiskCoverageError,
)
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.layered import (
    FourLayerPlanningOutcome,
    FourLayerPlanningService,
    FourLayerReplanningOutcome,
)
from arctic_route_planning.planners import TimeDependentAStar
from arctic_route_planning.publishing import LayeredRoutePlanLatestStore, LayeredStoreSnapshot
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanningPolicy,
    ReplanObservation,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.risk import RiskSampler
from arctic_route_planning.service import (
    PlanningBatch,
    PlanningService,
    ReplanningOutcome,
    ServicePlanningRequest,
)

_TEMPORAL_SHADOW_EXACT_MODE = "exact_temporal"
_TEMPORAL_SHADOW_CONTROL_TRACE_MODE = "control_trace"


def _normalize_temporal_shadow_mode(value: Any) -> str:
    """Resolve the private shadow candidate mode without changing defaults."""

    raw = getattr(value, "value", value)
    if not isinstance(raw, str):
        raise ValueError("candidate_mode must be 'exact_temporal' or 'control_trace'")
    aliases = {
        "exact": _TEMPORAL_SHADOW_EXACT_MODE,
        "exact-temporal": _TEMPORAL_SHADOW_EXACT_MODE,
        "exact_temporal": _TEMPORAL_SHADOW_EXACT_MODE,
        "temporal": _TEMPORAL_SHADOW_EXACT_MODE,
        "control": _TEMPORAL_SHADOW_CONTROL_TRACE_MODE,
        "control-trace": _TEMPORAL_SHADOW_CONTROL_TRACE_MODE,
        "control_trace": _TEMPORAL_SHADOW_CONTROL_TRACE_MODE,
    }
    try:
        return aliases[raw.strip().lower()]
    except KeyError as exc:
        raise ValueError(
            "candidate_mode must be 'exact_temporal' or 'control_trace'"
        ) from exc


@dataclass(slots=True)
class _PlanningSession:
    coordinator: PlanningCoordinator
    policy: ReplanningPolicy
    trigger_evaluator: ReplanTriggerEvaluator
    switch_gate: RouteSwitchGate
    layered_store: LayeredRoutePlanLatestStore
    generation_id: int | None = None
    state_lock: RLock = field(default_factory=RLock)

    def enter_generation(self, generation_id: int) -> None:
        """Reset trigger state on a forward generation transition only."""

        with self.state_lock:
            if self.generation_id is not None and generation_id < self.generation_id:
                raise ContextMismatchError("旧 generation 不得重新进入正式规划会话")
            if self.generation_id == generation_id:
                return
            self.generation_id = generation_id
            self.trigger_evaluator = ReplanTriggerEvaluator(self.policy)
            self.switch_gate = RouteSwitchGate(self.policy)


@dataclass(frozen=True, slots=True)
class TemporalShadowStrategyResult:
    """One isolated result from the non-publishing temporal shadow run.

    ``outcome`` is deliberately marked ``published=False`` before it leaves
    the shadow boundary.  The underlying scratch store may have accepted the
    complete set, but that store is not the formal session/latest store.
    Planner errors are captured per strategy so a candidate failure remains a
    diagnostic result and cannot affect the control or production path.
    """

    outcome: FourLayerPlanningOutcome | None = None
    scratch_published: bool = False
    error_type: str | None = None
    error_message: str | None = None
    reuse_outcomes: tuple[TemporalShadowReuseObservation, ...] = ()
    goal_certificates: tuple[TemporalShadowCertificateObservation, ...] = ()
    trace_observations: tuple[TemporalShadowTraceObservation, ...] = ()

    @property
    def status(self) -> str:
        if self.outcome is not None:
            return "SUCCEEDED"
        return "FAILED"

    @property
    def snapshot(self) -> LayeredStoreSnapshot | None:
        return self.outcome.snapshot if self.outcome is not None else None


@dataclass(frozen=True, slots=True)
class TemporalShadowOutcome:
    """Control/candidate comparison payload for the opt-in C shadow entry."""

    control: TemporalShadowStrategyResult
    candidate: TemporalShadowStrategyResult
    risk_window_commit_id: str
    risk_window_content_digest: str
    # This is an explicit non-publication fence, rather than an inference from
    # the nested scratch outcomes.
    production_published: bool = False
    candidate_mode: str = _TEMPORAL_SHADOW_EXACT_MODE

    @property
    def control_outcome(self) -> FourLayerPlanningOutcome | None:
        return self.control.outcome

    @property
    def candidate_outcome(self) -> FourLayerPlanningOutcome | None:
        return self.candidate.outcome

    @property
    def reuse_outcomes(self) -> tuple[TemporalShadowReuseObservation, ...]:
        """P2 reuse sidecar emitted by the candidate strategy."""

        return self.candidate.reuse_outcomes

    @property
    def trace_observations(self) -> tuple[TemporalShadowTraceObservation, ...]:
        """Control-trace capture/reuse sidecar for the opt-in mode."""

        return self.candidate.trace_observations


@dataclass(frozen=True, slots=True)
class TemporalShadowReuseObservation:
    """Immutable, non-contract diagnostic for one candidate reuse attempt."""

    objective: str
    source_goal: tuple[int, int]
    target_goal: tuple[int, int]
    api_available: bool
    reused: bool
    status: str
    fallback_reason: str | None = None
    certificate_status: str | None = None
    expanded_labels: int | None = None
    edge_evaluations: int | None = None
    mode: str = _TEMPORAL_SHADOW_EXACT_MODE
    used_search: bool = False
    trace_digest: str | None = None
    trace_write_count: int | None = None


@dataclass(frozen=True, slots=True)
class TemporalShadowTraceObservation:
    """Immutable view of one captured control trace."""

    objective: str
    status: str
    digest: str | None = None
    identity_digest: str | None = None
    write_count: int | None = None
    route_elapsed_seconds: float | None = None
    route_max_edge_risk: float | None = None


@dataclass(frozen=True, slots=True)
class TemporalShadowCertificateObservation:
    """Small immutable view of the full-voyage certificate per objective."""

    objective: str
    status: str
    digest: str | None = None
    upper_bound: float | None = None
    open_lower_bound: float | None = None
    epsilon: float | None = None


class _TemporalShadowCandidatePlanner:
    """Adapt the exact temporal or control-trace shadow to layered planning."""

    def __init__(
        self,
        planner: Any,
        *,
        request: ServicePlanningRequest,
        window: CommittedRiskWindow,
        candidate_mode: str = _TEMPORAL_SHADOW_EXACT_MODE,
        control_planner: Any | None = None,
    ) -> None:
        self._planner = planner
        self._request = request
        self._window = window
        self._candidate_mode = _normalize_temporal_shadow_mode(candidate_mode)
        if self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE and (
            control_planner is None
        ):
            raise ValueError("control_trace mode requires an isolated control planner")
        self._control_planner = control_planner
        self._layer_index = 0
        self._full_sessions: dict[Any, Any] = {}
        self._full_certificates: dict[Any, Any] = {}
        self._full_traces: dict[Any, Any] = {}
        self._reuse_records: list[TemporalShadowReuseObservation] = []
        self._trace_records: list[TemporalShadowTraceObservation] = []
        self._trace_identity = _shadow_trace_identity(request, window)

    @property
    def risk_identity(self) -> Any:
        return self._planner.risk_identity

    def plan_candidates(
        self,
        request: Any,
        objectives: tuple[Any, ...],
    ) -> Mapping[Any, Any]:
        # Importing the session identity here keeps P1 absent from the normal
        # formal ingress import graph and makes this adapter shadow-only.
        from arctic_route_planning.planners.temporal_session import TemporalSessionIdentity

        results: dict[Any, Any] = {}
        layer_index = self._layer_index
        self._layer_index += 1
        for objective in objectives:
            objective_request = replace(request, objective=objective)
            source_session = self._full_sessions.get(objective)
            source_goal = self._request.goal
            if (
                self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                and layer_index == 0
            ):
                result, trace = _trace_plan(
                    self._planner,
                    objective_request,
                    identity=self._trace_identity,
                )
                self._full_traces[objective] = trace
                self._trace_records.append(
                    _trace_observation(objective, trace, status="TRACE_CAPTURED")
                )
                results[objective] = result
                continue
            same_goal = objective_request.goal == source_goal
            # The control-trace experiment is deliberately narrower than the
            # existing exact temporal shadow: only the full -> main transition
            # may try a source certificate.  Rolling/executable calls remain
            # independent cold control searches even when their anchor happens
            # to equal the destination.
            trace_transition = (
                self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                and layer_index == 1
                and same_goal
            )
            exact_transition = (
                self._candidate_mode == _TEMPORAL_SHADOW_EXACT_MODE and same_goal
            )
            source_available = (
                self._full_traces.get(objective)
                if self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                else source_session
            )
            if source_available is not None and (trace_transition or exact_transition):
                if trace_transition:
                    reuse = _try_control_trace_reuse(
                        self._full_traces.get(objective),
                        self._planner,
                        objective_request,
                        identity=self._trace_identity,
                    )
                else:
                    reuse = _try_temporal_goal_reuse(
                        self._planner,
                        source_session,
                        objective_request,
                    )
                if reuse is not None:
                    result, observation = reuse
                    self._reuse_records.append(
                        _reuse_observation(
                            objective=objective,
                            source_goal=source_goal,
                            target_goal=objective_request.goal,
                            outcome=observation,
                            api_available=True,
                            mode=self._candidate_mode,
                        )
                    )
                    if result is not None:
                        results[objective] = result
                        continue
                    if trace_transition:
                        self._reuse_records.append(
                            TemporalShadowReuseObservation(
                                objective=str(getattr(objective, "value", objective)),
                                source_goal=source_goal,
                                target_goal=objective_request.goal,
                                api_available=True,
                                reused=False,
                                status="COLD_CONTROL",
                                fallback_reason=_reuse_reason(observation),
                                mode=self._candidate_mode,
                                used_search=True,
                                trace_digest=getattr(
                                    getattr(observation, "trace", None),
                                    "digest",
                                    getattr(self._full_traces.get(objective), "digest", None),
                                ),
                                trace_write_count=getattr(
                                    getattr(observation, "trace", None),
                                    "write_count",
                                    getattr(self._full_traces.get(objective), "write_count", None),
                                ),
                            )
                        )
                elif _shadow_reuse_api_available(self._candidate_mode, self._planner):
                    self._reuse_records.append(
                        TemporalShadowReuseObservation(
                            objective=str(getattr(objective, "value", objective)),
                            source_goal=source_goal,
                            target_goal=objective_request.goal,
                            api_available=True,
                            reused=False,
                            status="MISS_FALLBACK_COLD",
                            fallback_reason="P2 reuse returned no planning result",
                            certificate_status=_certificate_status(
                                self._full_certificates.get(objective)
                            ),
                            mode=self._candidate_mode,
                        )
                    )
            elif not same_goal:
                # A lower layer with a different anchor is intentionally a
                # cold search; P2's exact-goal proof is not portable across
                # destination identities.  The control-trace mode uses the
                # formal control implementation for this fallback path.
                cold_status = (
                    "COLD_CONTROL"
                    if self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                    else "COLD_ANCHOR"
                )
                self._reuse_records.append(
                    TemporalShadowReuseObservation(
                        objective=str(getattr(objective, "value", objective)),
                        source_goal=source_goal,
                        target_goal=objective_request.goal,
                        api_available=_shadow_reuse_api_available(
                            self._candidate_mode,
                            self._planner,
                        ),
                        reused=False,
                        status=cold_status,
                        fallback_reason="target goal differs from full-voyage goal",
                        mode=self._candidate_mode,
                        used_search=True,
                    )
                )
            elif self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE:
                # Same-goal rolling/executable layers are intentionally not
                # treated as another full->main reuse opportunity.
                self._reuse_records.append(
                    TemporalShadowReuseObservation(
                        objective=str(getattr(objective, "value", objective)),
                        source_goal=source_goal,
                        target_goal=objective_request.goal,
                        api_available=_shadow_reuse_api_available(
                            self._candidate_mode,
                            self._planner,
                        ),
                        reused=False,
                        status="COLD_CONTROL",
                        fallback_reason="trace reuse is limited to full-to-main",
                        certificate_status=_certificate_status(
                            self._full_certificates.get(objective)
                        ),
                        mode=self._candidate_mode,
                        used_search=True,
                    )
                )
            if (
                self._candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                and layer_index > 0
            ):
                result = self._control_cold_search(objective_request)
            else:
                result = self._cold_search(
                    objective_request,
                    identity_factory=TemporalSessionIdentity,
                )
            results[objective] = result
        return results

    def _cold_search(self, request: Any, *, identity_factory: Any) -> Any:
        identity = identity_factory.from_planner(
            self._planner,
            request,
            input_revision=self._request.input_revision,
            risk_window_content_digest=self._window.content_digest,
            risk_window_commit_id=self._window.commit_id,
        )
        session = self._planner.create_session(request, identity=identity)
        result = self._planner.advance_session(session)
        if result is None:
            raise RuntimeError("temporal shadow candidate did not reach a terminal state")
        # Full-voyage sessions are retained per objective.  Other anchors are
        # intentionally cold and never enter this map.
        if request.goal == self._request.goal and request.objective not in self._full_sessions:
            self._full_sessions[request.objective] = session
            self._full_certificates[request.objective] = _goal_certificate(session)
        return getattr(result, "planning_result", result)

    def _control_cold_search(self, request: Any) -> Any:
        if self._control_planner is None:  # pragma: no cover - guarded in init
            raise RuntimeError("control-trace shadow has no control planner")
        result = self._control_planner.plan(request)
        return getattr(result, "planning_result", result)

    @property
    def reuse_observations(self) -> tuple[TemporalShadowReuseObservation, ...]:
        return tuple(self._reuse_records)

    @property
    def trace_observations(self) -> tuple[TemporalShadowTraceObservation, ...]:
        return tuple(self._trace_records)

    @property
    def goal_certificates(self) -> tuple[TemporalShadowCertificateObservation, ...]:
        return tuple(
            _certificate_observation(objective, certificate)
            for objective, certificate in self._full_certificates.items()
        )

    @property
    def candidate_mode(self) -> str:
        return self._candidate_mode


@dataclass(frozen=True, slots=True)
class PreparedRiskPlanning:
    """Verified preparation whose only execution path re-enters the source lease."""

    request: ServicePlanningRequest
    query: RiskWindowQuery
    window: CommittedRiskWindow
    source: CommittedRiskSource
    configuration: PlanningConfiguration
    session: _PlanningSession

    @property
    def coordinator(self) -> PlanningCoordinator:
        """Compatibility view of the persistent per-run coordinator."""

        return self.session.coordinator

    def execute(self) -> PlanningBatch:
        """Invoke the unchanged planning service against the frozen preparation."""

        result = self._with_private_planner(observation=None, layered=False)
        if not isinstance(result, PlanningBatch):
            raise TypeError("planning service returned a non-batch result")
        return result

    def replan_if_needed(self, observation: ReplanObservation) -> ReplanningOutcome:
        """Evaluate and execute one v2 replan under the same source lease."""

        return self._with_private_planner(observation=observation, layered=False)

    def execute_four_layer(self) -> FourLayerPlanningOutcome:
        """Build and atomically publish all four v3 layers under one lease."""

        return self._execute_layered(observation=None)

    def execute_four_layer_temporal_shadow(
        self,
        *,
        candidate_mode: str = _TEMPORAL_SHADOW_EXACT_MODE,
    ) -> TemporalShadowOutcome:
        """Run control and P1 temporal-label strategies in isolated scratch stores.

        This is an explicit research/shadow entry, not a second formal
        publication path.  Both planners are reconstructed from the leased
        committed window and each strategy receives a fresh coordinator and
        layered latest store.  ``exact_temporal`` is the original P1/P2
        candidate behavior.  ``control_trace`` is an explicit, default-off
        experiment: only the full-to-main same-goal transition attempts P2
        reuse; misses and all other layer anchors run cold control.  The
        persistent production session (including its baseline and
        latest/frozen publication state) is not touched, even when the
        candidate fails or is cancelled.
        """

        candidate_mode = _normalize_temporal_shadow_mode(candidate_mode)
        planner_config_digest = _verified_planning_configuration_digest(self.configuration)
        if self.request.planner_config_digest != planner_config_digest:
            raise ContextMismatchError("请求 planner_config_digest 与执行时冻结配置不一致")
        with self.source.lease_committed_window(self.query) as current:
            # Both constructors re-check the query identity and the prepared
            # content digest while the same committed-window lease is held.
            control_planner = self._private_planner(current)
            control = _run_shadow_strategy(
                control_planner,
                request=self.request,
                configuration=self.configuration,
                planner_version="time-dependent-a-star.shadow-control.v1",
            )
            try:
                if candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE:
                    # The trace candidate is itself a separately constructed
                    # control planner; its writes are collected only through
                    # the opt-in internal API.  A third planner is reserved
                    # for cold control fallback so counters/state never cross
                    # the tracks.
                    candidate_planner = self._private_planner(current)
                    trace_control_planner = self._private_planner(current)
                else:
                    candidate_planner = self._private_temporal_candidate(current)
                    trace_control_planner = None
                candidate = _run_shadow_strategy(
                    _TemporalShadowCandidatePlanner(
                        candidate_planner,
                        request=self.request,
                        window=current,
                        candidate_mode=candidate_mode,
                        control_planner=trace_control_planner,
                    ),
                    request=self.request,
                    configuration=self.configuration,
                    planner_version=(
                        "time-dependent-a-star.shadow-control-trace.v1"
                        if candidate_mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE
                        else "temporal-label-a-star.shadow-candidate.v1"
                    ),
                )
            except PlanningCancelled:
                raise
            except Exception as exc:
                candidate = TemporalShadowStrategyResult(
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            # Recompute the leased content identity after both scratch runs as
            # a final fence against mutation of a mutable frame payload while
            # the lease was held.
            self._assert_leased_window_identity(current)
            return TemporalShadowOutcome(
                control=control,
                candidate=candidate,
                risk_window_commit_id=current.commit_id,
                risk_window_content_digest=current.content_digest,
                production_published=False,
                candidate_mode=candidate_mode,
            )

    def execute_four_layer_control_trace_shadow(self) -> TemporalShadowOutcome:
        """Explicit alias for the non-publishing control-trace experiment."""

        return self.execute_four_layer_temporal_shadow(candidate_mode="control_trace")

    def replan_four_layer_if_needed(
        self,
        observation: ReplanObservation,
    ) -> FourLayerReplanningOutcome:
        """Evaluate and atomically replace one complete four-layer set."""

        result = self._execute_layered(observation=observation)
        if not isinstance(result, FourLayerReplanningOutcome):
            raise TypeError("four-layer replanning returned a non-outcome result")
        return result

    def _execute_layered(
        self,
        *,
        observation: ReplanObservation | None,
    ) -> FourLayerPlanningOutcome | FourLayerReplanningOutcome:
        return self._with_private_planner(observation=observation, layered=True)

    def _with_private_planner(
        self,
        *,
        observation: ReplanObservation | None,
        layered: bool,
    ):
        planner_config_digest = _verified_planning_configuration_digest(self.configuration)
        if self.request.planner_config_digest != planner_config_digest:
            raise ContextMismatchError("请求 planner_config_digest 与执行时冻结配置不一致")
        # The source lease fences B's generation/content for the duration of
        # planning.  Do not also hold the session state lock here: the shared
        # PlanningCoordinator must remain able to activate a newer revision
        # and cancel an older in-flight request.  Session generation changes
        # and both publication stores provide their own narrow atomic locks.
        with self.source.lease_committed_window(self.query) as current:
            self.session.enter_generation(self.request.generation_id)
            planner = self._private_planner(current)
            if layered:
                service = FourLayerPlanningService(
                    planner,
                    planner_config=self.configuration.planner,
                    coordinator=self.session.coordinator,
                    store=self.session.layered_store,
                    switch_gate=self.session.switch_gate,
                    trigger_evaluator=self.session.trigger_evaluator,
                )
                if observation is None:
                    outcome = service.execute(self.request)
                    _mark_replanning_baseline(
                        self.session,
                        request=self.request,
                        risk_revision=current.commit_id,
                        route_avg_risk=outcome.plan_set.recommended.metrics.avg_risk,
                        route_max_risk=outcome.plan_set.recommended.metrics.max_risk,
                    )
                    return outcome
                _validate_formal_replan(
                    self.session,
                    request=self.request,
                    observation=observation,
                    risk_revision=current.commit_id,
                    layered=True,
                )
                return service.replan_if_needed(self.request, observation)
            service = PlanningService(
                planner,
                planner_config=self.configuration.planner,
                coordinator=self.session.coordinator,
                switch_gate=self.session.switch_gate,
                trigger_evaluator=self.session.trigger_evaluator,
            )
            if observation is None:
                batch = service.execute(self.request)
                _mark_replanning_baseline(
                    self.session,
                    request=self.request,
                    risk_revision=current.commit_id,
                    route_avg_risk=batch.selected.metrics.avg_risk,
                    route_max_risk=batch.selected.metrics.max_risk,
                )
                return batch
            _validate_formal_replan(
                self.session,
                request=self.request,
                observation=observation,
                risk_revision=current.commit_id,
                layered=False,
            )
            return service.replan_if_needed(self.request, observation)

    def _private_planner(self, current: CommittedRiskWindow) -> TimeDependentAStar:
        current.assert_matches(self.query)
        private_frames = tuple(
            risk_frame_from_document(risk_frame_to_document(frame)) for frame in current.frames
        )
        private_window = CommittedRiskWindow.create(self.query, private_frames)
        if (
            private_window.commit_id != self.window.commit_id
            or private_window.content_digest != self.window.content_digest
        ):
            raise ContextMismatchError("RiskFrame 窗口在 prepare 与 execute 之间发生改变")
        sampler = RiskSampler(private_frames, max_frame_gap=HOURLY_RISK_INTERVAL)
        grid = RegularGrid.from_risk_frame(
            private_frames[0],
            allow_diagonal=self.configuration.planner.connectivity == 8,
        )
        vessel_model = VesselPerformanceModel.from_configuration(self.configuration.vessel_model)
        return TimeDependentAStar(
            grid,
            sampler,
            vessel_model,
            planner_config=self.configuration.planner,
        )

    def _private_temporal_candidate(self, current: CommittedRiskWindow) -> Any:
        """Build the P1 planner from the same leased, canonical frame copy."""

        # Keep the experimental module out of the normal planners package and
        # out of the default formal execution path.
        from arctic_route_planning.planners.temporal_label_astar import TemporalLabelAStar

        self._assert_leased_window_identity(current)
        private_frames = tuple(
            risk_frame_from_document(risk_frame_to_document(frame)) for frame in current.frames
        )
        sampler = RiskSampler(private_frames, max_frame_gap=HOURLY_RISK_INTERVAL)
        grid = RegularGrid.from_risk_frame(
            private_frames[0],
            allow_diagonal=self.configuration.planner.connectivity == 8,
        )
        vessel_model = VesselPerformanceModel.from_configuration(self.configuration.vessel_model)
        return TemporalLabelAStar(
            grid,
            sampler,
            vessel_model,
            planner_config=self.configuration.planner,
        )

    def _assert_leased_window_identity(self, current: CommittedRiskWindow) -> None:
        current.assert_matches(self.query)
        private_frames = tuple(
            risk_frame_from_document(risk_frame_to_document(frame)) for frame in current.frames
        )
        private_window = CommittedRiskWindow.create(self.query, private_frames)
        if (
            private_window.commit_id != self.window.commit_id
            or private_window.content_digest != self.window.content_digest
        ):
            raise ContextMismatchError("RiskFrame 窗口在 prepare 与 execute 之间发生改变")


# Upper bound on retained planning sessions; older runs are evicted LRU once
# exceeded so long-running processes do not leak memory.
_MAX_SESSIONS = 64


class RiskSourcePlanningIngress:
    """Fail-closed formal adapter from a structural B store to C's planner.

    The source is queried exactly once with the full BC identity and knowledge
    cutoff.  Only an explicitly committed, exact hourly, inclusive window is
    accepted.  This class constructs existing C sampler/grid/vessel/planner
    components; it does not change their algorithms.
    """

    def __init__(
        self,
        source: CommittedRiskSource,
        *,
        configuration: PlanningConfiguration,
        coordinator: PlanningCoordinator | None = None,
    ) -> None:
        if not hasattr(source, "get_committed_window") or not hasattr(
            source, "lease_committed_window"
        ):
            raise TypeError(
                "source 必须结构化实现 get_committed_window(query) 与 lease_committed_window(query)"
            )
        planner_config_digest = _verified_planning_configuration_digest(configuration)
        self.source = source
        self.configuration = configuration
        self.planner_config = configuration.planner
        self.planner_config_digest = planner_config_digest
        self.coordinator = coordinator or PlanningCoordinator()
        # Bounded LRU: long-running processes publish many runs; the oldest
        # sessions are evicted once ``_MAX_SESSIONS`` is exceeded.
        self._sessions: OrderedDict[tuple[str, str], _PlanningSession] = OrderedDict()
        self._coordinator_lock = RLock()

    def _session_for(
        self,
        *,
        run_id: str,
        scenario_id: str,
    ) -> _PlanningSession:
        """Reuse fences within one run without cancelling an unrelated run."""

        key = (run_id, scenario_id)
        with self._coordinator_lock:
            session = self._sessions.get(key)
            if session is None:
                # Preserve the public/injected coordinator for the first run;
                # every additional run receives independent active state.
                coordinator = self.coordinator if not self._sessions else PlanningCoordinator()
                policy = ReplanningPolicy.from_config(self.configuration.replanning)
                session = _PlanningSession(
                    coordinator=coordinator,
                    policy=policy,
                    trigger_evaluator=ReplanTriggerEvaluator(policy),
                    switch_gate=RouteSwitchGate(policy),
                    layered_store=LayeredRoutePlanLatestStore(),
                )
                self._sessions[key] = session
                if len(self._sessions) > _MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(key)
            return session

    def prepare(self, request: ServicePlanningRequest) -> PreparedRiskPlanning:
        """Query, validate, and assemble one immutable formal planning input."""

        if request.risk_provenance is not ProvenanceKind.FORMAL:
            raise ContextMismatchError("正式 RiskSource 入口只接受 formal RiskFrame")
        current_planner_config_digest = _verified_planning_configuration_digest(self.configuration)
        if current_planner_config_digest != self.planner_config_digest:
            raise ContextMismatchError("入口 PlanningConfiguration 在构造后发生改变")
        if request.planner_config_digest != self.planner_config_digest:
            raise ContextMismatchError("请求 planner_config_digest 与入口配置不一致")
        for name, actual, expected in (
            ("scenario", request.scenario, self.configuration.scenario),
            ("corridor", request.corridor, self.configuration.corridor),
            ("vessel", request.vessel, self.configuration.vessel),
            ("vessel_model", request.vessel_model, self.configuration.vessel_model),
        ):
            if actual != expected:
                raise ContextMismatchError(f"请求 {name} 与入口冻结配置不一致")
        if request.maximum_elapsed is None:  # resolved by ServicePlanningRequest
            raise ContractError("maximum_elapsed was not resolved")
        query = RiskWindowQuery(
            start=request.start_time,
            end=request.start_time + request.maximum_elapsed,
            interval=HOURLY_RISK_INTERVAL,
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            corridor_id=request.corridor.corridor_id,
            generation_id=request.generation_id,
            vessel_profile_id=request.vessel.vessel_profile_id,
            config_digest=request.run_context.config_digest,
            model_config_digest=request.model_config_digest,
            as_of=request.as_of_time,
        )
        window = self.source.get_committed_window(query)
        if not isinstance(window, CommittedRiskWindow):
            raise TypeError("get_committed_window 必须返回 CommittedRiskWindow")
        window.assert_matches(query)
        if window.interval != HOURLY_RISK_INTERVAL or window.count != query.count:
            raise RiskCoverageError("正式 BC 窗口必须完整提交严格逐小时闭区间")
        for frame in window.frames:
            if frame.provenance is not ProvenanceKind.FORMAL:
                raise ContextMismatchError("正式入口不能混入非 formal RiskFrame")
            validate_canonical_risk_id(frame)
        sampler = RiskSampler(
            window.frames,
            max_frame_gap=HOURLY_RISK_INTERVAL,
        )
        if sampler.start_time != query.start or sampler.end_time != query.end:
            raise RiskCoverageError("RiskSampler 时窗与已提交闭区间不一致")
        grid = RegularGrid.from_risk_frame(
            window.frames[0],
            allow_diagonal=self.configuration.planner.connectivity == 8,
        )
        for name, node in (("start", request.start), ("goal", request.goal)):
            if not grid.contains(node):
                raise ContextMismatchError(
                    f"请求 {name} node {node} 不属于已提交 RiskFrame 网格 {grid.shape}"
                )
        hard_mask = np.asarray(window.frames[0].payload["hard_mask"].values, dtype=np.bool_)
        if hard_mask[request.start] or hard_mask[request.goal]:
            raise ContextMismatchError("请求起终点在已提交窗口首帧中不可通航")
        # Validate the vessel configuration without exposing a lease-free
        # PlanningService backed by these inspectable prepare-time frames. The
        # safe execute path rebuilds every planning component from its private
        # canonical snapshot while holding the source lease.
        VesselPerformanceModel.from_configuration(request.vessel_model)
        session = self._session_for(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
        )
        return PreparedRiskPlanning(
            request=request,
            query=query,
            window=window,
            source=self.source,
            configuration=self.configuration,
            session=session,
        )

    def execute(self, request: ServicePlanningRequest) -> PlanningBatch:
        """Prepare and run all existing C objective policies."""

        return self.prepare(request).execute()

    def replan_if_needed(
        self,
        request: ServicePlanningRequest,
        observation: ReplanObservation,
    ) -> ReplanningOutcome:
        """Formal v2 replanning with persistent per-run policy state."""

        prepared = self.prepare(request)
        return prepared._with_private_planner(observation=observation, layered=False)

    def execute_four_layer(
        self,
        request: ServicePlanningRequest,
    ) -> FourLayerPlanningOutcome:
        return self.prepare(request).execute_four_layer()

    def replan_four_layer_if_needed(
        self,
        request: ServicePlanningRequest,
        observation: ReplanObservation,
    ) -> FourLayerReplanningOutcome:
        return self.prepare(request).replan_four_layer_if_needed(observation)


def _verified_planning_configuration_digest(
    configuration: PlanningConfiguration,
) -> str:
    planner_config_digest = configuration_digest(
        configuration.vessel_model,
        configuration.planner,
        configuration.replanning,
    )
    if configuration.planner_config_digest != planner_config_digest:
        raise ValueError(
            "PlanningConfiguration.planner_config_digest does not match its "
            "vessel/planner/replanning objects"
        )
    return planner_config_digest


def _run_shadow_strategy(
    planner: Any,
    *,
    request: ServicePlanningRequest,
    configuration: PlanningConfiguration,
    planner_version: str,
) -> TemporalShadowStrategyResult:
    """Execute one strategy with coordinator/store objects owned by the call."""

    policy = ReplanningPolicy.from_config(configuration.replanning)
    scratch_coordinator = PlanningCoordinator()
    scratch_store = LayeredRoutePlanLatestStore()
    service = FourLayerPlanningService(
        planner,
        planner_config=configuration.planner,
        coordinator=scratch_coordinator,
        store=scratch_store,
        switch_gate=RouteSwitchGate(policy),
        trigger_evaluator=ReplanTriggerEvaluator(policy),
        planner_version=planner_version,
    )
    try:
        scratch_outcome = service.execute(request)
    except PlanningCancelled:
        # User/generation cancellation is a control-flow fence, not candidate
        # evidence and never a reason to continue with another scratch track.
        raise
    except Exception as exc:  # shadow failures are data, never formal output
        return TemporalShadowStrategyResult(
            error_type=type(exc).__name__,
            error_message=str(exc),
            reuse_outcomes=tuple(getattr(planner, "reuse_observations", ())),
            goal_certificates=tuple(getattr(planner, "goal_certificates", ())),
            trace_observations=tuple(getattr(planner, "trace_observations", ())),
        )
    return TemporalShadowStrategyResult(
        outcome=replace(scratch_outcome, published=False),
        scratch_published=scratch_outcome.published,
        reuse_outcomes=tuple(getattr(planner, "reuse_observations", ())),
        goal_certificates=tuple(getattr(planner, "goal_certificates", ())),
        trace_observations=tuple(getattr(planner, "trace_observations", ())),
    )


def _temporal_goal_reuse_available(planner: Any) -> bool:
    method = getattr(planner, "reuse_exact_goal", None)
    if callable(method):
        return True
    try:
        from arctic_route_planning.planners import temporal_reuse
    except ImportError:  # pragma: no cover - candidate module is optional
        return False
    return callable(getattr(temporal_reuse, "try_reuse", None)) or callable(
        getattr(temporal_reuse, "reuse_goal", None)
    )


def _control_trace_reuse_available() -> bool:
    try:
        from arctic_route_planning.planners import control_trace_reuse
    except ImportError:  # pragma: no cover - candidate module is optional
        return False
    return callable(getattr(control_trace_reuse, "trace_plan", None)) and callable(
        getattr(control_trace_reuse, "try_reuse", None)
    )


def _shadow_reuse_api_available(mode: str, planner: Any) -> bool:
    if mode == _TEMPORAL_SHADOW_CONTROL_TRACE_MODE:
        return _control_trace_reuse_available()
    return _temporal_goal_reuse_available(planner)


def _try_temporal_goal_reuse(
    planner: Any,
    source_session: Any,
    request: Any,
) -> tuple[Any | None, Any] | None:
    """Invoke P2 if present; a miss remains a candidate cold-search decision."""

    method = getattr(planner, "reuse_exact_goal", None)
    if not callable(method):
        try:
            from arctic_route_planning.planners import temporal_reuse
        except ImportError:  # pragma: no cover - candidate module is optional
            return None
        method = getattr(temporal_reuse, "try_reuse", None)
        if callable(method):
            certificate = source_session
            if not _is_reuse_certificate(certificate):
                certificate = _goal_certificate(source_session)
            outcome = method(certificate, planner, request)
        else:
            method = getattr(temporal_reuse, "reuse_goal", None)
            if not callable(method):
                return None
            certificate = source_session
            if not _is_reuse_certificate(certificate):
                certificate = _goal_certificate(source_session)
            outcome = method(certificate, planner, request)
    else:
        outcome = method(source_session, request)
    if not _reuse_hit(outcome):
        return None, outcome
    result = _reuse_result(outcome)
    return (result, outcome)


def _trace_plan(
    planner: Any,
    request: Any,
    *,
    identity: Any,
) -> tuple[Any, Any]:
    """Run one control search through the private trace collector."""

    from arctic_route_planning.planners import control_trace_reuse

    result, trace = control_trace_reuse.trace_plan(
        planner,
        request,
        identity=identity,
    )
    if trace is None:
        raise RuntimeError("control trace planner returned no completed trace")
    return result, trace


def _try_control_trace_reuse(
    trace: Any,
    planner: Any,
    request: Any,
    *,
    identity: Any,
) -> tuple[Any | None, Any] | None:
    """Invoke the private conservative control-trace proof, if available."""

    try:
        from arctic_route_planning.planners import control_trace_reuse
    except ImportError:  # pragma: no cover - candidate module is optional
        return None
    method = getattr(control_trace_reuse, "try_reuse", None)
    if not callable(method):
        return None
    outcome = method(trace, planner, request, identity=identity)
    return _reuse_result(outcome), outcome


def _shadow_trace_identity(
    request: ServicePlanningRequest,
    window: CommittedRiskWindow,
) -> Mapping[str, Any]:
    """External generation/window fence carried by every control trace."""

    return {
        "run_id": request.run_context.run_id,
        "scenario_id": request.scenario.scenario_id,
        "corridor_id": request.corridor.corridor_id,
        "vessel_profile_id": request.vessel.vessel_profile_id,
        "generation_id": request.generation_id,
        "input_revision": request.input_revision,
        "config_digest": request.run_context.config_digest,
        "model_config_digest": request.model_config_digest,
        "planner_config_digest": request.planner_config_digest,
        "risk_provenance": request.risk_provenance.value,
        "risk_window_commit_id": window.commit_id,
        "risk_window_content_digest": window.content_digest,
    }


def _trace_observation(
    objective: Any,
    trace: Any,
    *,
    status: str,
) -> TemporalShadowTraceObservation:
    identity = getattr(trace, "identity", None)
    return TemporalShadowTraceObservation(
        objective=str(getattr(objective, "value", objective)),
        status=status,
        digest=getattr(trace, "digest", getattr(trace, "trace_digest", None)),
        identity_digest=getattr(identity, "digest", None),
        write_count=getattr(trace, "write_count", getattr(trace, "count", None)),
        route_elapsed_seconds=_as_float(getattr(trace, "route_elapsed_seconds", None)),
        route_max_edge_risk=_as_float(getattr(trace, "route_max_edge_risk", None)),
    )


def _reuse_reason(outcome: Any) -> str | None:
    value = getattr(outcome, "fallback_reason", None)
    if value is None:
        value = getattr(outcome, "reason", None)
    if value is None:
        return None
    return _status_text(value)


def _reuse_hit(outcome: Any) -> bool:
    for name in ("reused", "hit", "is_hit"):
        value = getattr(outcome, name, None)
        if value is not None:
            return bool(value)
    status = _status_text(getattr(outcome, "status", None))
    return any(token in status for token in ("HIT", "REUSED", "CERTIFIED"))


def _reuse_result(outcome: Any) -> Any | None:
    for name in ("planning_result", "result", "candidate_result"):
        value = getattr(outcome, name, None)
        if value is not None:
            return getattr(value, "planning_result", value)
    value = getattr(outcome, "steps", None)
    if value is not None and getattr(outcome, "objective", None) is not None:
        return outcome
    return None


def _reuse_observation(
    *,
    objective: Any,
    source_goal: tuple[int, int],
    target_goal: tuple[int, int],
    outcome: Any,
    api_available: bool,
    mode: str = _TEMPORAL_SHADOW_EXACT_MODE,
) -> TemporalShadowReuseObservation:
    status = _status_text(getattr(outcome, "status", None)) or (
        "HIT" if _reuse_hit(outcome) else "MISS"
    )
    raw_result = getattr(outcome, "result", None)
    if raw_result is None:
        raw_result = getattr(outcome, "planning_result", None)
    result = _reuse_result(outcome)
    diagnostics = getattr(outcome, "diagnostics", None)
    if diagnostics is None and raw_result is not None:
        diagnostics = getattr(raw_result, "diagnostics", None)
    if diagnostics is None and result is not None:
        diagnostics = getattr(result, "diagnostics", None)
    metrics = getattr(result, "metrics", None)
    certificate = getattr(outcome, "certificate", None)
    if certificate is None:
        certificate = getattr(outcome, "goal_certificate", None)
    return TemporalShadowReuseObservation(
        objective=str(getattr(objective, "value", objective)),
        source_goal=source_goal,
        target_goal=target_goal,
        api_available=api_available,
        reused=_reuse_hit(outcome),
        status=status,
        fallback_reason=_reuse_reason(outcome),
        certificate_status=_certificate_status(certificate),
        expanded_labels=_first_int(
            diagnostics,
            "expanded_labels",
            fallback=metrics and getattr(metrics, "expanded_states", None),
        ),
        edge_evaluations=_first_int(diagnostics, "edge_evaluations"),
        mode=mode,
        used_search=bool(getattr(outcome, "used_search", False)),
        trace_digest=getattr(
            getattr(outcome, "trace", None),
            "digest",
            getattr(getattr(outcome, "trace", None), "trace_digest", None),
        ),
        trace_write_count=getattr(
            getattr(outcome, "trace", None),
            "write_count",
            getattr(getattr(outcome, "trace", None), "count", None),
        ),
    )


def _goal_certificate(source: Any) -> Any:
    for name in ("goal_certificate", "get_goal_certificate", "inspect_goal_certificate"):
        candidate = getattr(source, name, None)
        if candidate is None:
            continue
        return candidate() if callable(candidate) else candidate
    try:
        from arctic_route_planning.planners import temporal_reuse
    except ImportError:  # pragma: no cover - candidate module is optional
        return None
    certify = getattr(temporal_reuse, "certify_session", None)
    if callable(certify):
        return certify(source)
    return None


def _is_reuse_certificate(value: Any) -> bool:
    return value is not None and (
        hasattr(value, "certificate")
        or value.__class__.__name__ in {"TemporalGoalCertificate", "TemporalCertifiedGoal"}
    )


def _certificate_status(certificate: Any) -> str | None:
    if certificate is None:
        return None
    nested = getattr(certificate, "certificate", None)
    if nested is not None:
        certificate = nested
    for name in ("status", "termination_reason", "reason", "open_status"):
        value = getattr(certificate, name, None)
        if value is not None:
            return _status_text(value)
    return None


def _certificate_observation(
    objective: Any,
    certificate: Any,
) -> TemporalShadowCertificateObservation:
    nested = getattr(certificate, "certificate", None)
    if nested is not None:
        certificate = nested
    return TemporalShadowCertificateObservation(
        objective=str(getattr(objective, "value", objective)),
        status=_certificate_status(certificate) or "UNAVAILABLE",
        digest=getattr(certificate, "certificate_digest", getattr(certificate, "digest", None)),
        upper_bound=_as_float(
            getattr(certificate, "upper_bound", getattr(certificate, "incumbent_cost", None))
        ),
        open_lower_bound=_as_float(
            getattr(certificate, "open_lower_bound", getattr(certificate, "lower_bound", None))
        ),
        epsilon=_as_float(getattr(certificate, "epsilon", None)),
    )


def _status_text(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _first_int(value: Any, name: str, *, fallback: Any = None) -> int | None:
    candidate = getattr(value, name, None) if value is not None else None
    if candidate is None:
        candidate = fallback
    return int(candidate) if isinstance(candidate, int) else None


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _mark_replanning_baseline(
    session: _PlanningSession,
    *,
    request: ServicePlanningRequest,
    risk_revision: str,
    route_avg_risk: float,
    route_max_risk: float,
) -> None:
    session.trigger_evaluator.mark_replanned(
        ReplanObservation(
            observed_at=request.start_time,
            risk_valid_time=request.start_time,
            data_revision=request.input_revision,
            risk_revision=risk_revision,
            route_avg_risk=route_avg_risk,
            route_max_risk=route_max_risk,
        )
    )


def _validate_formal_replan(
    session: _PlanningSession,
    *,
    request: ServicePlanningRequest,
    observation: ReplanObservation,
    risk_revision: str,
    layered: bool,
) -> None:
    if observation.observed_at != request.start_time:
        raise ContextMismatchError("正式重规划 observed_at 必须等于新请求 start_time")
    if observation.risk_valid_time != request.start_time:
        raise ContextMismatchError("正式重规划 risk_valid_time 必须等于新请求 start_time")
    if observation.data_revision != request.input_revision:
        raise ContextMismatchError("正式重规划 data_revision 必须等于 input_revision")
    if observation.risk_revision != risk_revision:
        raise ContextMismatchError("正式重规划 risk_revision 必须等于当前窗口 commit_id")
    if layered:
        previous = session.layered_store.latest(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
        )
    else:
        previous = session.coordinator.store.latest(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
            generation_id=request.generation_id,
        )
    if previous is None:
        raise ContextMismatchError("正式重规划前必须已有同代次已发布计划")
    if request.input_revision <= previous.input_revision:
        raise ContextMismatchError("正式重规划 input_revision 必须严格递增")


__all__ = [
    "PreparedRiskPlanning",
    "RiskSourcePlanningIngress",
    "TemporalShadowCertificateObservation",
    "TemporalShadowOutcome",
    "TemporalShadowReuseObservation",
    "TemporalShadowStrategyResult",
]
