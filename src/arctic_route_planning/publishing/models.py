"""Publication request identity layered on the shared C -> D contracts."""

from __future__ import annotations

from dataclasses import dataclass

from arctic_route_planning.contracts.models import RouteMetrics, RoutePlan, Waypoint
from arctic_route_planning.domain.models import ObjectiveMode, PlanKind

ROUTE_PLAN_SCHEMA_VERSION = "cd.route-plan.v1"
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

    scenario_id: str
    generation_id: int
    config_digest: str
    input_revision: int
    planning_request_id: str

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id must be a non-empty string")
        _require_digest(self.config_digest)
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
        scenario_id=plan.scenario_id,
        generation_id=plan.generation_id,
        config_digest=plan.config_digest,
        input_revision=plan.input_revision,
        planning_request_id=plan.planning_request_id,
    )


__all__ = [
    "OBJECTIVE_MODES",
    "PLAN_KINDS",
    "ROUTE_PLAN_SCHEMA_VERSION",
    "PublicationToken",
    "RouteMetrics",
    "RoutePlan",
    "Waypoint",
    "token_for_plan",
]
