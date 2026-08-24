"""Public formal BC ingress that joins a committed RiskSource to C planning."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from threading import RLock

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
from arctic_route_planning.errors import ContextMismatchError, ContractError, RiskCoverageError
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.layered import (
    FourLayerPlanningOutcome,
    FourLayerPlanningService,
    FourLayerReplanningOutcome,
)
from arctic_route_planning.planners import TimeDependentAStar
from arctic_route_planning.publishing import LayeredRoutePlanLatestStore
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


__all__ = ["PreparedRiskPlanning", "RiskSourcePlanningIngress"]
