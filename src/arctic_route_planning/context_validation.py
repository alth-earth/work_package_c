"""Fail-closed validation of shared facts against one immutable RunContext."""

from __future__ import annotations

from arctic_route_contracts import (
    CorridorDefinition,
    RunContext,
    ScenarioDefinition,
    VesselProfile,
    canonical_sha256,
    configuration_digest,
    validate_scenario_for_corridor,
    validate_scenario_for_vessel,
)


def validate_run_context_binding(
    context: RunContext,
    *,
    scenario: ScenarioDefinition,
    corridor: CorridorDefinition,
    vessel: VesselProfile,
) -> None:
    """Require IDs, versions, content hashes, times, and public digest to agree."""

    if scenario.is_template or scenario.simulation_start is None or scenario.simulation_end is None:
        raise ValueError("RunContext validation requires a materialized scenario")
    validate_scenario_for_corridor(scenario, corridor)
    validate_scenario_for_vessel(scenario, vessel)

    expected = {
        "scenario_id": scenario.scenario_id,
        "scenario_version": scenario.version,
        "scenario_mode": scenario.mode,
        "simulation_start": scenario.simulation_start,
        "simulation_end": scenario.simulation_end,
        "scenario_digest": canonical_sha256(scenario),
        "corridor_id": corridor.corridor_id,
        "corridor_version": corridor.version,
        "corridor_digest": canonical_sha256(corridor),
        "vessel_profile_id": vessel.vessel_profile_id,
        "vessel_profile_version": vessel.version,
        "vessel_profile_digest": canonical_sha256(vessel),
        "config_digest": configuration_digest(
            scenario,
            corridor,
            vessel,
            dataset_bundle_id=context.dataset_bundle_id,
            dataset_bundle_digest=context.dataset_bundle_digest,
        ),
    }
    mismatched = [
        name
        for name, expected_value in expected.items()
        if getattr(context, name) != expected_value
    ]
    if mismatched:
        raise ValueError("RunContext does not match shared configuration: " + ", ".join(mismatched))


__all__ = ["validate_run_context_binding"]
