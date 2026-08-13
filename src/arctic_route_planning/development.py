"""Development-only RunContext helpers for synthetic and legacy sources."""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

from arctic_route_contracts import (
    RunContext,
    canonical_sha256,
    configuration_digest,
)

from arctic_route_planning.config import PlanningConfiguration


def create_development_run_context(
    configuration: PlanningConfiguration,
    *,
    source_kind: str,
    source_checksum: str = "",
    as_of_time: datetime | None = None,
) -> RunContext:
    """Create a deterministic, non-formal context for tests and isolated adapters."""

    scenario = configuration.scenario
    if scenario.simulation_start is None or scenario.simulation_end is None:
        raise ValueError("development RunContext requires a materialized scenario")
    seed = "|".join((source_kind, source_checksum, scenario.scenario_id, scenario.version))
    bundle_digest = hashlib.sha256(f"development-only|{seed}".encode()).hexdigest()
    bundle_id = f"a-bundle-{bundle_digest[:24]}"
    public_digest = configuration_digest(
        scenario,
        configuration.corridor,
        configuration.vessel,
        dataset_bundle_id=bundle_id,
        dataset_bundle_digest=bundle_digest,
    )
    return RunContext(
        schema_version="run-context.v2",
        run_id=f"run-{uuid5(NAMESPACE_URL, seed)}",
        created_at=as_of_time or scenario.simulation_start,
        scenario_id=scenario.scenario_id,
        scenario_version=scenario.version,
        scenario_mode=scenario.mode,
        simulation_start=scenario.simulation_start,
        simulation_end=scenario.simulation_end,
        scenario_digest=canonical_sha256(scenario),
        corridor_id=configuration.corridor.corridor_id,
        corridor_version=configuration.corridor.version,
        corridor_digest=canonical_sha256(configuration.corridor),
        vessel_profile_id=configuration.vessel.vessel_profile_id,
        vessel_profile_version=configuration.vessel.version,
        vessel_profile_digest=canonical_sha256(configuration.vessel),
        dataset_bundle_id=bundle_id,
        dataset_bundle_digest=bundle_digest,
        config_digest=public_digest,
    )


__all__ = ["create_development_run_context"]
