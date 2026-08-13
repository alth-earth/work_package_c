"""Small deterministic RiskFrame factories for core-planner tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)

CONFIG_DIGEST = "0" * 64
MODEL_CONFIG_DIGEST = "1" * 64
RUN_ID = "run-unit-tests"
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_frame(
    valid_time: datetime,
    risk: np.ndarray,
    *,
    risk_id: str,
    confidence: np.ndarray | None = None,
    hard_mask: np.ndarray | None = None,
    environment_speed_factor: np.ndarray | None = None,
    latitudes: tuple[float, ...] | None = None,
    longitudes: tuple[float, ...] | None = None,
    scenario_id: str = "scenario-demo",
    corridor_id: str = "corridor-demo",
    vessel_profile_id: str = "vessel-demo",
    config_digest: str = CONFIG_DIGEST,
    model_config_digest: str = MODEL_CONFIG_DIGEST,
    run_id: str = RUN_ID,
    generation_id: int = 3,
    model_version: str = "risk-model-v1",
    grid_id: str = "fixture-grid",
) -> RiskFrame:
    risk = np.asarray(risk, dtype=np.float32)
    if latitudes is None:
        latitudes = tuple(float(index) for index in range(risk.shape[0]))
    if longitudes is None:
        longitudes = tuple(float(index) for index in range(risk.shape[1]))
    if confidence is None:
        confidence = np.full(risk.shape, 0.9, dtype=np.float32)
    if hard_mask is None:
        hard_mask = np.zeros(risk.shape, dtype=np.bool_)
    levels = np.minimum(5, np.floor(risk * 5).astype(np.uint8) + 1)
    variables: dict[str, tuple[tuple[str, str], np.ndarray]] = {
        "risk_score": (("latitude", "longitude"), risk),
        "risk_level": (("latitude", "longitude"), levels),
        "hard_mask": (("latitude", "longitude"), np.asarray(hard_mask, dtype=np.bool_)),
        "confidence": (
            ("latitude", "longitude"),
            np.asarray(confidence, dtype=np.float32),
        ),
    }
    if environment_speed_factor is not None:
        variables["environment_speed_factor"] = (
            ("latitude", "longitude"),
            np.asarray(environment_speed_factor, dtype=np.float32),
        )
    payload = xr.Dataset(
        variables,
        coords={
            "latitude": np.asarray(latitudes, dtype=np.float64),
            "longitude": np.asarray(longitudes, dtype=np.float64),
        },
        attrs={"crs": "EPSG:4326", "grid_id": grid_id},
    )
    source = SourceReference(
        source_id="synthetic-fixture",
        data_id=None,
        issue_time=None,
        valid_time=valid_time,
        version="v1",
        quality_flag="synthetic",
    )
    return RiskFrame(
        schema_version="bc.risk-frame.v2",
        risk_id=risk_id,
        run_id=run_id,
        scenario_id=scenario_id,
        corridor_id=corridor_id,
        vessel_profile_id=vessel_profile_id,
        config_digest=config_digest,
        model_config_digest=model_config_digest,
        generation_id=generation_id,
        valid_time=valid_time,
        as_of_time=T0,
        generated_at=T0,
        model_version=model_version,
        payload=payload,
        source_summary=(source,),
        provenance=ProvenanceKind.SYNTHETIC,
    )
