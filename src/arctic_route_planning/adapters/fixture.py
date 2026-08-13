"""Deterministic synthetic RiskFrame sequence for contract and planner tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)
from arctic_route_planning.contracts.sources import InMemoryRiskSource
from arctic_route_planning.domain.models import (
    CorridorDefinition,
    RunContext,
    ScenarioDefinition,
    VesselProfile,
)
from arctic_route_planning.errors import ContractError
from arctic_route_planning.timeutils import ensure_utc

SYNTHETIC_MODEL_CONFIG_DIGEST = hashlib.sha256(
    b"deterministic-analytic-risk-v1"
).hexdigest()


class FixtureRiskSource(InMemoryRiskSource):
    """An in-memory source populated by a deterministic analytic risk field."""

    def __init__(
        self,
        *,
        scenario: ScenarioDefinition,
        corridor: CorridorDefinition,
        vessel: VesselProfile,
        run_context: RunContext,
        model_config_digest: str = SYNTHETIC_MODEL_CONFIG_DIGEST,
        generation_id: int = 0,
        as_of_time: datetime | None = None,
        frame_count: int = 25,
        interval: timedelta = timedelta(hours=1),
        shape: tuple[int, int] = (21, 31),
    ) -> None:
        super().__init__()
        if generation_id < 0:
            raise ContractError("generation_id 不能为负")
        if frame_count < 2 or interval <= timedelta(0):
            raise ContractError("合成夹具至少需要两帧且时间间隔必须为正")
        if len(shape) != 2 or min(shape) < 3:
            raise ContractError("合成夹具 shape 必须是两个不小于 3 的整数")
        cutoff = ensure_utc(
            as_of_time or scenario.simulation_start,
            field="fixture.as_of_time",
        )
        self.frames = tuple(
            _make_frame(
                scenario=scenario,
                corridor=corridor,
                vessel=vessel,
                run_id=run_context.run_id,
                config_digest=run_context.config_digest,
                model_config_digest=model_config_digest,
                generation_id=generation_id,
                as_of_time=cutoff,
                valid_time=scenario.simulation_start + index * interval,
                frame_index=index,
                shape=shape,
            )
            for index in range(frame_count)
        )
        for frame in self.frames:
            self.publish(frame)


def _make_frame(
    *,
    scenario: ScenarioDefinition,
    corridor: CorridorDefinition,
    vessel: VesselProfile,
    run_id: str,
    config_digest: str,
    model_config_digest: str,
    generation_id: int,
    as_of_time: datetime,
    valid_time: datetime,
    frame_index: int,
    shape: tuple[int, int],
) -> RiskFrame:
    ny, nx = shape
    west = corridor.data_bbox.west
    south = corridor.data_bbox.south
    east = corridor.data_bbox.east
    north = corridor.data_bbox.north
    latitude = np.linspace(south, north, ny, dtype=np.float64)
    longitude = np.linspace(west, east, nx, dtype=np.float64)
    y_axis = np.linspace(0.0, 1.0, ny, dtype=np.float32)[:, None]
    x_axis = np.linspace(0.0, 1.0, nx, dtype=np.float32)[None, :]
    wave = np.sin((x_axis * 2.7 + y_axis * 1.3 + frame_index * 0.11) * np.pi)
    risk = np.clip(0.28 + 0.20 * x_axis + 0.12 * y_axis + 0.14 * wave, 0, 1).astype(np.float32)
    hard_mask = np.zeros((ny, nx), dtype=np.bool_)
    hard_mask[ny // 2, nx // 3 : (2 * nx) // 3] = True
    hard_mask[ny // 2, nx // 2] = False
    confidence_value = max(0.55, 0.92 - frame_index * 0.01)
    confidence = np.full((ny, nx), confidence_value, dtype=np.float32)
    environment_wave = np.cos((x_axis * 1.1 - y_axis * 0.7 + frame_index * 0.07) * np.pi)
    environment_speed_factor = np.clip(
        0.86 + 0.10 * environment_wave,
        0.72,
        0.96,
    ).astype(np.float32)
    risk_level = np.clip(np.floor(risk * 5) + 1, 1, 5).astype(np.uint8)
    payload = xr.Dataset(
        data_vars={
            "risk_score": (("latitude", "longitude"), risk),
            "risk_level": (("latitude", "longitude"), risk_level),
            "hard_mask": (("latitude", "longitude"), hard_mask),
            "confidence": (("latitude", "longitude"), confidence),
            "environment_speed_factor": (
                ("latitude", "longitude"),
                environment_speed_factor,
            ),
        },
        coords={"latitude": latitude, "longitude": longitude},
        attrs={
            "crs": "EPSG:4326",
            "development_only": True,
            "fixture_model": "deterministic-analytic-risk-v1",
            "environment_effect_model": "independent-analytic-speed-factor-v1",
        },
    )
    identity = (
        f"{run_id}|{scenario.scenario_id}|{vessel.vessel_profile_id}|{generation_id}|"
        f"{valid_time.isoformat()}|{config_digest}|{model_config_digest}"
    )
    risk_id = f"fixture-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    source = SourceReference(
        source_id="fixture.synthetic.v1",
        data_id=f"fixture-{frame_index:04d}",
        issue_time=as_of_time,
        valid_time=valid_time,
        version="deterministic-analytic-risk-v1",
        quality_flag="synthetic",
        checksum=config_digest,
    )
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=risk_id,
        run_id=run_id,
        scenario_id=scenario.scenario_id,
        corridor_id=scenario.corridor_id,
        vessel_profile_id=vessel.vessel_profile_id,
        config_digest=config_digest,
        model_config_digest=model_config_digest,
        generation_id=generation_id,
        valid_time=valid_time,
        as_of_time=as_of_time,
        generated_at=as_of_time,
        model_version="fixture-risk.v1",
        payload=payload,
        source_summary=(source,),
        provenance=ProvenanceKind.SYNTHETIC,
    )
