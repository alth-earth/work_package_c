"""Load versioned scenario, vessel, planner, and replanning TOML configuration."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from arctic_route_contracts import (
    CorridorDefinition,
    ScenarioDefinition,
    VesselProfile,
    canonical_sha256,
    materialize_frozen_forecast,
    validate_scenario_for_vessel,
)
from arctic_route_contracts import (
    default_config_root as default_shared_config_root,
)
from arctic_route_contracts import (
    load_corridor as load_shared_corridor,
)
from arctic_route_contracts import (
    load_scenario as load_shared_scenario,
)
from arctic_route_contracts import (
    load_vessel_profile as load_shared_vessel_profile,
)

from arctic_route_planning.domain.models import (
    CostWeights,
    ModelCalibrationStatus,
    PlannerConfig,
    ReplanningConfig,
    VesselModelConfig,
)
from arctic_route_planning.errors import ContractError

_SAFE_CONFIG_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class PlanningConfiguration:
    """Validated configuration snapshot shared by a single planning run."""

    scenario: ScenarioDefinition
    corridor: CorridorDefinition
    vessel: VesselProfile
    vessel_model: VesselModelConfig
    planner: PlannerConfig
    replanning: ReplanningConfig
    planner_config_digest: str


def load_configuration(
    config_root: str | Path,
    scenario_id: str,
    *,
    shared_config_root: str | Path | None = None,
    simulation_start: datetime | None = None,
    candidate_route_distance_nm: float | None = None,
    vessel_profile_id: str | None = None,
    planner_name: str = "default",
    replanning_name: str = "default",
) -> PlanningConfiguration:
    """Load one immutable, content-addressed configuration snapshot."""

    root = Path(config_root)
    shared_root = Path(shared_config_root) if shared_config_root else default_shared_config_root()
    scenario = load_shared_scenario(shared_root, scenario_id)
    corridor = load_shared_corridor(shared_root, scenario.corridor_id)
    vessel = load_shared_vessel_profile(
        shared_root,
        vessel_profile_id or scenario.default_vessel_profile_id,
    )
    if scenario.is_template:
        if simulation_start is None:
            raise ContractError("冻结预报模板必须显式提供 simulation_start")
        selected_horizon = None
        if candidate_route_distance_nm is not None:
            selected_horizon = corridor.horizon_policy.recommend_hours(
                great_circle_distance_nm=corridor.great_circle_distance_nm,
                nominal_speed_knots=vessel.nominal_speed_knots,
                candidate_route_distance_nm=candidate_route_distance_nm,
            )
        scenario = materialize_frozen_forecast(
            scenario,
            simulation_start,
            horizon_hours=selected_horizon,
        )
    elif simulation_start is not None or candidate_route_distance_nm is not None:
        raise ContractError("固定历史场景不得覆盖 simulation_start 或候选航线时域")
    if scenario.horizon_hours > 216:
        raise ContractError("场景请求超过 C 216 小时正式搜索硬上限")
    validate_scenario_for_vessel(scenario, vessel)
    vessel_model = load_vessel_model_config(root, vessel.vessel_profile_id)
    if vessel_model.vessel_profile_version != vessel.version:
        raise ContractError("C 船模版本与共享 VesselProfile 版本不一致")
    planner = load_planner_config(root, planner_name)
    replanning = load_replanning_config(root, replanning_name)
    # B owns model_config_digest. C's vessel-performance assumptions belong to
    # the C planner identity and must not masquerade as a B model digest.
    planner_digest = configuration_digest(vessel_model, planner, replanning)
    return PlanningConfiguration(
        scenario,
        corridor,
        vessel,
        vessel_model,
        planner,
        replanning,
        planner_digest,
    )


def load_vessel_model_config(config_root: str | Path, vessel_profile_id: str) -> VesselModelConfig:
    value = _load_named_toml(Path(config_root), "vessel_models", vessel_profile_id)
    try:
        value["calibration_status"] = ModelCalibrationStatus(value["calibration_status"])
        return VesselModelConfig(**value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"C 船模配置 {vessel_profile_id} 无效: {exc}") from exc


def load_planner_config(config_root: str | Path, name: str = "default") -> PlannerConfig:
    value = _load_named_toml(Path(config_root), "planner", name)
    try:
        for mode in ("fastest", "low_risk", "recommended"):
            value[mode] = CostWeights(**value[mode])
        return PlannerConfig(**value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"规划器配置 {name} 无效: {exc}") from exc


def load_replanning_config(config_root: str | Path, name: str = "default") -> ReplanningConfig:
    value = _load_named_toml(Path(config_root), "replanning", name)
    try:
        return ReplanningConfig(**value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"重规划配置 {name} 无效: {exc}") from exc


def configuration_digest(*objects: object) -> str:
    """Return a stable SHA-256 over C-owned algorithm configuration only."""

    return canonical_sha256(*objects)


def _load_named_toml(root: Path, section: str, config_id: str) -> dict[str, Any]:
    if not _SAFE_CONFIG_ID.fullmatch(config_id):
        raise ContractError(f"不安全的配置 ID: {config_id!r}")
    path = root / section / f"{config_id}.toml"
    try:
        with path.open("rb") as handle:
            return dict(tomllib.load(handle))
    except FileNotFoundError as exc:
        raise ContractError(f"配置文件不存在: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ContractError(f"配置文件不是有效 TOML: {path}: {exc}") from exc
