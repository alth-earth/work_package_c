"""Planner-specific failure modes."""

from arctic_route_planning.errors import (
    NoRouteError,
    PlanningCancelled,
    PlanningCancelledError,
    PlanningError,
)

__all__ = [
    "EndpointBlockedError",
    "NoRouteError",
    "PlanningCancelled",
    "PlanningCancelledError",
    "PlanningError",
    "PlanningHorizonExceeded",
]


class EndpointBlockedError(NoRouteError):
    """Raised when the exact requested start or goal is hard-blocked."""


class PlanningHorizonExceeded(NoRouteError):
    """Raised when every remaining route would leave the risk coverage window."""
