"""Publication request identity layered on the shared C -> D contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass

from arctic_route_planning.contracts.models import RouteMetrics, RoutePlan, Waypoint
from arctic_route_planning.domain.models import ObjectiveMode, PlanKind
from arctic_route_planning.errors import ContractError

ROUTE_PLAN_SCHEMA_VERSION = "cd.route-plan.v2"
SELECTION_RATIONALE_SCHEMA_VERSION = "selection-rationale.v1"
OBJECTIVE_MODES = frozenset(mode.value for mode in ObjectiveMode)
PLAN_KINDS = frozenset(kind.value for kind in PlanKind)


def _require_digest(value: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("config_digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PublicationToken:
    """Identity frozen when a planning request starts.

    ``generation_id`` fences simulation seeks. ``input_revision`` and
    ``planning_request_id`` fence older work within the same generation. A config
    digest prevents output calculated with obsolete vessel/planner configuration
    from being published after a hot reload.
    """

    run_id: str
    scenario_id: str
    generation_id: int
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
    input_revision: int
    planning_request_id: str

    def __post_init__(self) -> None:
        for name in ("run_id", "scenario_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-empty string")
        for value in (
            self.config_digest,
            self.model_config_digest,
            self.planner_config_digest,
        ):
            _require_digest(value)
        if not self.planning_request_id.strip():
            raise ValueError("planning_request_id must be a non-empty string")
        if isinstance(self.generation_id, bool) or not isinstance(self.generation_id, int):
            raise ValueError("generation_id must be a non-negative integer")
        if self.generation_id < 0:
            raise ValueError("generation_id must be a non-negative integer")
        if isinstance(self.input_revision, bool) or not isinstance(self.input_revision, int):
            raise ValueError("input_revision must be a non-negative integer")
        if self.input_revision < 0:
            raise ValueError("input_revision must be a non-negative integer")


def token_for_plan(plan: RoutePlan) -> PublicationToken:
    return PublicationToken(
        run_id=plan.run_id,
        scenario_id=plan.scenario_id,
        generation_id=plan.generation_id,
        config_digest=plan.config_digest,
        model_config_digest=plan.model_config_digest,
        planner_config_digest=plan.planner_config_digest,
        input_revision=plan.input_revision,
        planning_request_id=plan.planning_request_id,
    )


@dataclass(frozen=True, slots=True)
class TradeoffDeltas:
    """Quantified difference between the selected route and the baseline route.

    All ``delta_*`` fields are ``selected - baseline``.  Negative risk deltas
    mean the selected route is safer; positive eta/distance deltas mean it is
    slower/longer.  ``*_reduction_pct`` is the relative reduction against the
    baseline (positive = improvement, negative = worse).
    """

    delta_distance_km: float
    delta_eta_hours: float
    delta_avg_risk: float
    delta_max_risk: float
    delta_integrated_risk_hours: float
    avg_risk_reduction_pct: float
    max_risk_reduction_pct: float

    def __post_init__(self) -> None:
        for name in (
            "delta_distance_km",
            "delta_eta_hours",
            "delta_avg_risk",
            "delta_max_risk",
            "delta_integrated_risk_hours",
            "avg_risk_reduction_pct",
            "max_risk_reduction_pct",
        ):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ContractError(f"TradeoffDeltas.{name} 必须是有限值")


@dataclass(frozen=True, slots=True)
class SelectionRationale:
    """Explains why C selected one objective route over a baseline alternative.

    This is a pure derived sidecar artifact: it references existing plan
    identities and metrics without altering ``cd.route-plan.v2`` or v3
    content.  The baseline is currently fixed to ``fastest`` so that the
    rationale answers "what does the recommended route trade off versus going
    as fast as possible".
    """

    schema_version: str
    run_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    model_config_digest: str
    planner_config_digest: str
    provenance: str
    generation_id: int
    planning_request_id: str
    input_revision: int
    selected_plan_id: str
    baseline_plan_id: str
    selected_objective: ObjectiveMode
    baseline_objective: ObjectiveMode
    tradeoffs: TradeoffDeltas
    summary_text: str

    def __post_init__(self) -> None:
        if self.schema_version != SELECTION_RATIONALE_SCHEMA_VERSION:
            raise ContractError(
                "SelectionRationale.schema_version 必须是 "
                f"{SELECTION_RATIONALE_SCHEMA_VERSION}"
            )
        for name in (
            "run_id",
            "scenario_id",
            "corridor_id",
            "vessel_profile_id",
            "planning_request_id",
            "selected_plan_id",
            "baseline_plan_id",
            "provenance",
            "summary_text",
        ):
            if not getattr(self, name).strip():
                raise ContractError(f"SelectionRationale.{name} 不能为空")
        _require_digest(self.config_digest)
        _require_digest(self.model_config_digest)
        _require_digest(self.planner_config_digest)
        if (
            isinstance(self.generation_id, bool)
            or not isinstance(self.generation_id, int)
            or self.generation_id < 0
        ):
            raise ContractError("generation_id 必须是非负整数且不能是 bool")
        if (
            isinstance(self.input_revision, bool)
            or not isinstance(self.input_revision, int)
            or self.input_revision < 0
        ):
            raise ContractError("input_revision 必须是非负整数且不能是 bool")
        object.__setattr__(
            self, "selected_objective", ObjectiveMode(self.selected_objective)
        )
        object.__setattr__(
            self, "baseline_objective", ObjectiveMode(self.baseline_objective)
        )


def build_selection_rationale(
    selected: RoutePlan,
    baseline: RoutePlan,
) -> SelectionRationale:
    """Derive a rationale sidecar from two already-published route plans.

    The two plans must share run identity (same run_id, scenario, generation,
    request).  The selected plan is the authoritative recommendation; the
    baseline is the fastest alternative used as the tradeoff reference.
    """

    _require_shared_identity(selected, baseline)
    if baseline.objective_mode is not ObjectiveMode.FASTEST:
        raise ContractError("baseline 路线必须是 fastest 目标")
    sm = selected.metrics
    bm = baseline.metrics
    delta_avg = sm.avg_risk - bm.avg_risk
    delta_max = sm.max_risk - bm.max_risk
    avg_reduction_pct = (
        ((bm.avg_risk - sm.avg_risk) / bm.avg_risk * 100.0)
        if bm.avg_risk > 0
        else 0.0
    )
    max_reduction_pct = (
        ((bm.max_risk - sm.max_risk) / bm.max_risk * 100.0)
        if bm.max_risk > 0
        else 0.0
    )
    tradeoffs = TradeoffDeltas(
        delta_distance_km=sm.distance_km - bm.distance_km,
        delta_eta_hours=sm.eta_hours - bm.eta_hours,
        delta_avg_risk=delta_avg,
        delta_max_risk=delta_max,
        delta_integrated_risk_hours=sm.integrated_risk_hours
        - bm.integrated_risk_hours,
        avg_risk_reduction_pct=avg_reduction_pct,
        max_risk_reduction_pct=max_reduction_pct,
    )
    summary_text = _format_summary(tradeoffs)
    return SelectionRationale(
        schema_version=SELECTION_RATIONALE_SCHEMA_VERSION,
        run_id=selected.run_id,
        scenario_id=selected.scenario_id,
        corridor_id=selected.corridor_id,
        vessel_profile_id=selected.vessel_profile_id,
        config_digest=selected.config_digest,
        model_config_digest=selected.model_config_digest,
        planner_config_digest=selected.planner_config_digest,
        provenance=selected.provenance.value,
        generation_id=selected.generation_id,
        planning_request_id=selected.planning_request_id,
        input_revision=selected.input_revision,
        selected_plan_id=selected.plan_id,
        baseline_plan_id=baseline.plan_id,
        selected_objective=selected.objective_mode,
        baseline_objective=baseline.objective_mode,
        tradeoffs=tradeoffs,
        summary_text=summary_text,
    )


def _require_shared_identity(selected: RoutePlan, baseline: RoutePlan) -> None:
    identity_fields = (
        "run_id",
        "scenario_id",
        "corridor_id",
        "vessel_profile_id",
        "config_digest",
        "model_config_digest",
        "planner_config_digest",
        "generation_id",
        "planning_request_id",
        "input_revision",
    )
    mismatched = [
        name
        for name in identity_fields
        if getattr(selected, name) != getattr(baseline, name)
    ]
    if mismatched:
        raise ContractError(
            "selected 与 baseline 路线身份不一致: " + ", ".join(mismatched)
        )
    if selected.plan_id == baseline.plan_id:
        raise ContractError("selected 与 baseline 不能是同一条路线")


def _format_summary(t: TradeoffDeltas) -> str:
    parts: list[str] = []
    if abs(t.delta_avg_risk) > 1e-9:
        direction = "减少" if t.delta_avg_risk < 0 else "增加"
        parts.append(f"平均风险{direction} {abs(t.avg_risk_reduction_pct):.1f}%")
    if abs(t.delta_eta_hours) > 1e-9:
        direction = "增加" if t.delta_eta_hours > 0 else "减少"
        parts.append(f"时间{direction} {abs(t.delta_eta_hours):.2f} 小时")
    if abs(t.delta_distance_km) > 1e-9:
        direction = "增加" if t.delta_distance_km > 0 else "减少"
        parts.append(f"距离{direction} {abs(t.delta_distance_km):.1f} km")
    if not parts:
        return "推荐路线与最快路线指标一致，无额外权衡"
    return "相比最快路线，推荐路线" + "，".join(parts)


__all__ = [
    "OBJECTIVE_MODES",
    "PLAN_KINDS",
    "ROUTE_PLAN_SCHEMA_VERSION",
    "SELECTION_RATIONALE_SCHEMA_VERSION",
    "PublicationToken",
    "RouteMetrics",
    "RoutePlan",
    "SelectionRationale",
    "TradeoffDeltas",
    "Waypoint",
    "build_selection_rationale",
    "token_for_plan",
]
