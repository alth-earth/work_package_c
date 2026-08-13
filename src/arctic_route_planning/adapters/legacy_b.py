"""Strict development adapter for the nested, unverified legacy B delivery ZIP."""

from __future__ import annotations

import hashlib
import io
import math
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import xarray as xr

from arctic_route_planning.contracts.models import (
    ProvenanceKind,
    RiskFrame,
    SourceReference,
)
from arctic_route_planning.contracts.sources import InMemoryRiskSource
from arctic_route_planning.domain.models import RunContext, ScenarioDefinition, VesselProfile
from arctic_route_planning.errors import ContractError, LegacyDataError
from arctic_route_planning.timeutils import ensure_utc

_INNER_ARCHIVE_SUFFIX = "/综合风险.zip"
_MAX_INNER_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_DATASET_BYTES = 256 * 1024 * 1024
LEGACY_MODEL_CONFIG_DIGEST = hashlib.sha256(b"legacy-b-unversioned-model").hexdigest()


class LegacyBArchiveAdapter(InMemoryRiskSource):
    """Expose legacy comprehensive-risk files through the formal RiskSource API.

    This adapter is intentionally development-only. It never reads top-level
    ``route_cost_grid`` files, never infers an issue time from file metadata, and
    only treats the legacy ``time`` coordinate as ``valid_time`` after an explicit
    caller acknowledgement.
    """

    def __init__(
        self,
        *,
        archive_path: str | Path,
        scenario: ScenarioDefinition,
        vessel: VesselProfile,
        run_context: RunContext,
        model_config_digest: str = LEGACY_MODEL_CONFIG_DIGEST,
        generation_id: int,
        as_of_time: datetime,
        development_mode: bool,
        time_coordinate_semantics: str,
        dataset_variant: str = "7days",
        confidence: float = 0.20,
        generated_at: datetime | None = None,
        legacy_corridor_id: str | None = None,
    ) -> None:
        super().__init__()
        if not development_mode:
            raise LegacyDataError("旧 B 交付包只能在显式 development_mode=True 时读取")
        if time_coordinate_semantics != "valid_time":
            raise LegacyDataError(
                "必须显式声明 time_coordinate_semantics='valid_time'，不得猜测旧 time 语义"
            )
        if dataset_variant not in {"7days", "60days"}:
            raise LegacyDataError("dataset_variant 只能是 7days 或 60days")
        if not math.isfinite(confidence) or not 0 < confidence <= 0.40:
            raise LegacyDataError("旧 B 适配置信度必须显式保持在 (0, 0.40]")
        if generation_id < 0:
            raise ContractError("generation_id 不能为负")
        self.archive_path = Path(archive_path)
        self.scenario = scenario
        self.vessel = vessel
        self.run_id = run_context.run_id
        self.config_digest = run_context.config_digest
        self.model_config_digest = model_config_digest
        self.legacy_corridor_id = legacy_corridor_id or scenario.corridor_id
        self.generation_id = generation_id
        self.as_of_time = ensure_utc(as_of_time, field="legacy.as_of_time")
        self.generated_at = ensure_utc(
            generated_at or datetime.now(UTC), field="legacy.generated_at"
        )
        self.dataset_variant = dataset_variant
        self.confidence = confidence
        self._loaded_frames: tuple[RiskFrame, ...] | None = None
        self.inner_member_name: str | None = None

    def load(self) -> tuple[RiskFrame, ...]:
        """Read, validate, adapt, publish, and return all frames exactly once."""

        if self._loaded_frames is not None:
            return self._loaded_frames
        dataset_bytes, member_name = self._read_nested_dataset()
        self.inner_member_name = member_name
        dataset_checksum = hashlib.sha256(dataset_bytes).hexdigest()
        try:
            with xr.open_dataset(io.BytesIO(dataset_bytes), engine="h5netcdf") as opened:
                dataset = opened.load()
        except Exception as exc:
            raise LegacyDataError(f"无法读取旧 B NetCDF: {member_name}: {exc}") from exc
        frames = self._adapt_dataset(dataset, member_name, dataset_checksum)
        for frame in frames:
            self.publish(frame)
        self._loaded_frames = frames
        return frames

    def _read_nested_dataset(self) -> tuple[bytes, str]:
        if not self.archive_path.is_file():
            raise LegacyDataError(f"旧 B 交付包不存在: {self.archive_path}")
        try:
            with zipfile.ZipFile(self.archive_path) as outer:
                inner_candidates = [
                    info
                    for info in outer.infolist()
                    if info.filename.endswith(_INNER_ARCHIVE_SUFFIX)
                ]
                if len(inner_candidates) != 1:
                    raise LegacyDataError("外层交付包必须且只能包含一个嵌套 综合风险.zip")
                inner_info = inner_candidates[0]
                if inner_info.file_size > _MAX_INNER_ARCHIVE_BYTES:
                    raise LegacyDataError("嵌套 综合风险.zip 超过安全读取上限")
                inner_bytes = outer.read(inner_info)
            with zipfile.ZipFile(io.BytesIO(inner_bytes)) as inner:
                expected_suffix = (
                    f"/comprehensive_risk_{self.legacy_corridor_id}_{self.dataset_variant}.nc"
                )
                candidates = [
                    info for info in inner.infolist() if info.filename.endswith(expected_suffix)
                ]
                if len(candidates) != 1:
                    raise LegacyDataError(f"嵌套综合风险包中找不到唯一数据集: *{expected_suffix}")
                info = candidates[0]
                if "route_cost_grid" in info.filename or info.file_size > _MAX_DATASET_BYTES:
                    raise LegacyDataError("拒绝 route_cost_grid 或超过安全上限的数据集")
                return inner.read(info), info.filename
        except zipfile.BadZipFile as exc:
            raise LegacyDataError(f"旧 B 交付包不是有效嵌套 ZIP: {exc}") from exc

    def _adapt_dataset(
        self,
        dataset: xr.Dataset,
        member_name: str,
        dataset_checksum: str,
    ) -> tuple[RiskFrame, ...]:
        required = {"comprehensive_risk", "sea_mask"}
        missing = sorted(required - set(dataset.data_vars))
        if missing:
            raise LegacyDataError(f"旧 B 数据集缺少变量: {', '.join(missing)}")
        if not {"time", "lat", "lon"}.issubset(dataset.coords):
            raise LegacyDataError("旧 B 数据集必须携带 time/lat/lon 坐标")
        if dataset.attrs.get("route_name") != self.legacy_corridor_id:
            raise LegacyDataError("旧 B route_name 与场景 corridor_id 不匹配")
        try:
            risk_cube = dataset["comprehensive_risk"].transpose("time", "lat", "lon")
            sea_mask = np.asarray(dataset["sea_mask"].transpose("lat", "lon").values)
        except ValueError as exc:
            raise LegacyDataError("旧 B 风险或海陆掩膜维度不兼容") from exc
        if not np.all(np.isin(sea_mask, (0, 1))):
            raise LegacyDataError("旧 B sea_mask 必须只包含 0/1")
        land_mask = np.logical_not(sea_mask.astype(np.bool_))
        latitude = np.asarray(dataset["lat"].values, dtype=np.float64)
        longitude = np.asarray(dataset["lon"].values, dtype=np.float64)
        if land_mask.shape != (latitude.size, longitude.size):
            raise LegacyDataError("旧 B sea_mask 与坐标形状不一致")
        frames: list[RiskFrame] = []
        for index, raw_time in enumerate(np.asarray(dataset["time"].values)):
            valid_time = _numpy_datetime_to_utc(raw_time)
            raw_risk = np.asarray(risk_cube.isel(time=index).values, dtype=np.float32)
            if raw_risk.shape != land_mask.shape:
                raise LegacyDataError("旧 B comprehensive_risk 与 sea_mask 形状不一致")
            invalid_at_sea = ~np.isfinite(raw_risk) & ~land_mask
            if np.any(invalid_at_sea):
                raise LegacyDataError("旧 B 海域 risk_score 含未声明缺测")
            finite = raw_risk[np.isfinite(raw_risk)]
            if finite.size and np.any((finite < 0) | (finite > 1)):
                raise LegacyDataError("旧 B comprehensive_risk 超出 [0, 1]")
            risk_level = np.ones(raw_risk.shape, dtype=np.uint8)
            finite_cells = np.isfinite(raw_risk)
            risk_level[finite_cells] = np.clip(
                np.floor(raw_risk[finite_cells] * 5) + 1,
                1,
                5,
            ).astype(np.uint8)
            confidence = np.where(land_mask, 0.0, self.confidence).astype(np.float32)
            speed_factor = np.ones(raw_risk.shape, dtype=np.float32)
            payload = xr.Dataset(
                data_vars={
                    "risk_score": (("latitude", "longitude"), raw_risk),
                    "risk_level": (("latitude", "longitude"), risk_level),
                    "hard_mask": (("latitude", "longitude"), land_mask),
                    "confidence": (("latitude", "longitude"), confidence),
                    "environment_speed_factor": (
                        ("latitude", "longitude"),
                        speed_factor,
                    ),
                },
                coords={"latitude": latitude, "longitude": longitude},
                attrs={
                    "crs": "EPSG:4326",
                    "development_only": True,
                    "provenance": "legacy_unverified",
                    "legacy_member": member_name,
                    "legacy_mapping": (
                        "comprehensive_risk->risk_score; "
                        "logical_not(sea_mask)->hard_mask(land-only); "
                        "route_cost_grid ignored; issue_time unknown"
                    ),
                    "coordinate_snap_applied": False,
                    "speed_factor_defaulted": True,
                    "speed_factor_warning": (
                        "Legacy B has no environmental speed effect; adapter supplies neutral 1.0. "
                        "C must not infer speed reduction from risk_score."
                    ),
                },
            )
            identity = (
                f"{dataset_checksum}|{self.run_id}|{self.scenario.scenario_id}|"
                f"{self.generation_id}|{valid_time.isoformat()}|{self.config_digest}|"
                f"{self.model_config_digest}"
            )
            risk_id = f"legacy-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
            source = SourceReference(
                source_id="legacy_b_delivery_archive",
                data_id=f"{member_name}#time={index}",
                issue_time=None,
                valid_time=valid_time,
                version="legacy-unversioned",
                quality_flag="legacy_unverified",
                checksum=dataset_checksum,
            )
            frames.append(
                RiskFrame(
                    schema_version="bc.risk-frame.v2",
                    risk_id=risk_id,
                    run_id=self.run_id,
                    scenario_id=self.scenario.scenario_id,
                    corridor_id=self.scenario.corridor_id,
                    vessel_profile_id=self.vessel.vessel_profile_id,
                    config_digest=self.config_digest,
                    model_config_digest=self.model_config_digest,
                    generation_id=self.generation_id,
                    valid_time=valid_time,
                    as_of_time=self.as_of_time,
                    generated_at=self.generated_at,
                    model_version="legacy-b-adapter.v2",
                    payload=payload,
                    source_summary=(source,),
                    provenance=ProvenanceKind.LEGACY_UNVERIFIED,
                )
            )
        return tuple(frames)


def _numpy_datetime_to_utc(value: object) -> datetime:
    timestamp = np.datetime64(value, "us")
    if np.isnat(timestamp):
        raise LegacyDataError("旧 B time 坐标包含 NaT")
    microseconds = int(timestamp.astype(np.int64))
    return datetime.fromtimestamp(microseconds / 1_000_000, tz=UTC)
