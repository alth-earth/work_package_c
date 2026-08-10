"""Load versioned scenario, vessel, planner, and replanning TOML configuration."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from arctic_route_planning.domain.models import (
    CalibrationStatus,
    CostWeights,
    GeoPoint,
    PlannerConfig,
    ReplanningConfig,
    ScenarioDefinition,
    VesselProfile,
)
from arctic_route_planning.errors import ContractError

_SAFE_CONFIG_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class PlanningConfiguration:
    """Validated configuration snapshot shared by a single planning run."""

    scenario: ScenarioDefinition
    vessel: VesselProfile
    planner: PlannerConfig
    replanning: ReplanningConfig
    config_digest: str


def load_configuration(
    config_root: str | Path,
    scenario_id: str,
    *,
    vessel_profile_id: str | None = None,
    planner_name: str = "default",
    replanning_name: str = "default",
) -> PlanningConfiguration:
    """Load one immutable, content-addressed configuration snapshot."""

    root = Path(config_root)
    scenario = load_scenario(root, scenario_id)
    vessel = load_vessel_profile(
        root,
        vessel_profile_id or scenario.default_vessel_profile_id,
    )
    planner = load_planner_config(root, planner_name)
    replanning = load_replanning_config(root, replanning_name)
    digest = configuration_digest(scenario, vessel, planner, replanning)
    return PlanningConfiguration(scenario, vessel, planner, replanning, digest)


def load_scenario(config_root: str | Path, scenario_id: str) -> ScenarioDefinition:
    value = _load_named_toml(Path(config_root), "scenarios", scenario_id)
    try:
        start = value.pop("start")
        destination = value.pop("destination")
        return ScenarioDefinition(
            **value,
            start=GeoPoint(**start),
            destination=GeoPoint(**destination),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"场景配置 {scenario_id} 无效: {exc}") from exc


def load_vessel_profile(config_root: str | Path, vessel_profile_id: str) -> VesselProfile:
    value = _load_named_toml(Path(config_root), "vessels", vessel_profile_id)
    try:
        value["calibration_status"] = CalibrationStatus(value["calibration_status"])
        return VesselProfile(**value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError(f"船型配置 {vessel_profile_id} 无效: {exc}") from exc


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
    """Return a stable SHA-256 over validated configuration values."""

    canonical = json.dumps(
        [_json_value(asdict(value)) for value in objects],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
