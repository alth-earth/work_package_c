"""Public formal BC ingress that joins a committed RiskSource to C planning."""

from __future__ import annotations

from dataclasses import dataclass
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
from arctic_route_planning.errors import ContextMismatchError, RiskCoverageError
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import TimeDependentAStar
from arctic_route_planning.replanning import (
    PlanningCoordinator,
    ReplanningPolicy,
    ReplanTriggerEvaluator,
    RouteSwitchGate,
)
from arctic_route_planning.risk import RiskSampler
from arctic_route_planning.service import (
    PlanningBatch,
    PlanningService,
    ServicePlanningRequest,
)


@dataclass(frozen=True, slots=True)
class PreparedRiskPlanning:
    """Verified preparation whose only execution path re-enters the source lease."""

    request: ServicePlanningRequest
    query: RiskWindowQuery
    window: CommittedRiskWindow
    source: CommittedRiskSource
    configuration: PlanningConfiguration
    coordinator: PlanningCoordinator

    def execute(self) -> PlanningBatch:
        """Invoke the unchanged planning service against the frozen preparation."""

        planner_config_digest = _verified_planning_configuration_digest(
            self.configuration
        )
        if self.request.planner_config_digest != planner_config_digest:
            raise ContextMismatchError(
                "请求 planner_config_digest 与执行时冻结配置不一致"
            )
        with self.source.lease_committed_window(self.query) as current:
            current.assert_matches(self.query)
            private_frames = tuple(
                risk_frame_from_document(risk_frame_to_document(frame))
                for frame in current.frames
            )
            private_window = CommittedRiskWindow.create(self.query, private_frames)
            if (
                private_window.commit_id != self.window.commit_id
                or private_window.content_digest != self.window.content_digest
            ):
                raise ContextMismatchError(
                    "RiskFrame 窗口在 prepare 与 execute 之间发生改变"
                )
            sampler = RiskSampler(
                private_frames,
                max_frame_gap=HOURLY_RISK_INTERVAL,
            )
            grid = RegularGrid.from_risk_frame(
                private_frames[0],
                allow_diagonal=self.configuration.planner.connectivity == 8,
            )
            vessel_model = VesselPerformanceModel.from_configuration(
                self.configuration.vessel_model
            )
            planner = TimeDependentAStar(
                grid,
                sampler,
                vessel_model,
                planner_config=self.configuration.planner,
            )
            replanning_policy = ReplanningPolicy.from_config(
                self.configuration.replanning
            )
            service = PlanningService(
                planner,
                planner_config=self.configuration.planner,
                coordinator=self.coordinator,
                switch_gate=RouteSwitchGate(replanning_policy),
                trigger_evaluator=ReplanTriggerEvaluator(replanning_policy),
            )
            return service.execute(self.request)


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
                "source 必须结构化实现 get_committed_window(query) 与 "
                "lease_committed_window(query)"
            )
        planner_config_digest = _verified_planning_configuration_digest(configuration)
        self.source = source
        self.configuration = configuration
        self.planner_config = configuration.planner
        self.planner_config_digest = planner_config_digest
        self.coordinator = coordinator or PlanningCoordinator()
        self._coordinators: dict[tuple[str, str], PlanningCoordinator] = {}
        self._coordinator_lock = RLock()

    def _coordinator_for(
        self,
        *,
        run_id: str,
        scenario_id: str,
    ) -> PlanningCoordinator:
        """Reuse fences within one run without cancelling an unrelated run."""

        key = (run_id, scenario_id)
        with self._coordinator_lock:
            coordinator = self._coordinators.get(key)
            if coordinator is None:
                # Preserve the public/injected coordinator for the first run;
                # every additional run receives independent active state.
                coordinator = (
                    self.coordinator
                    if not self._coordinators
                    else PlanningCoordinator()
                )
                self._coordinators[key] = coordinator
            return coordinator

    def prepare(self, request: ServicePlanningRequest) -> PreparedRiskPlanning:
        """Query, validate, and assemble one immutable formal planning input."""

        if request.risk_provenance is not ProvenanceKind.FORMAL:
            raise ContextMismatchError("正式 RiskSource 入口只接受 formal RiskFrame")
        current_planner_config_digest = _verified_planning_configuration_digest(
            self.configuration
        )
        if current_planner_config_digest != self.planner_config_digest:
            raise ContextMismatchError("入口 PlanningConfiguration 在构造后发生改变")
        if request.planner_config_digest != self.planner_config_digest:
            raise ContextMismatchError("请求 planner_config_digest 与入口配置不一致")
        for field, actual, expected in (
            ("scenario", request.scenario, self.configuration.scenario),
            ("corridor", request.corridor, self.configuration.corridor),
            ("vessel", request.vessel, self.configuration.vessel),
            ("vessel_model", request.vessel_model, self.configuration.vessel_model),
        ):
            if actual != expected:
                raise ContextMismatchError(f"请求 {field} 与入口冻结配置不一致")
        if request.maximum_elapsed is None:  # resolved by ServicePlanningRequest
            raise RuntimeError("maximum_elapsed was not resolved")
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
        coordinator = self._coordinator_for(
            run_id=request.run_context.run_id,
            scenario_id=request.scenario.scenario_id,
        )
        return PreparedRiskPlanning(
            request=request,
            query=query,
            window=window,
            source=self.source,
            configuration=self.configuration,
            coordinator=coordinator,
        )

    def execute(self, request: ServicePlanningRequest) -> PlanningBatch:
        """Prepare and run all existing C objective policies."""

        return self.prepare(request).execute()


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


__all__ = ["PreparedRiskPlanning", "RiskSourcePlanningIngress"]
