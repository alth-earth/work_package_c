"""Planner-specific failure modes."""

from arctic_route_planning.errors import (
    NoRouteError,
    PlanningCancelledError,
    PlanningError,
)

__all__ = [
    "EndpointBlockedError",
    "NoRouteError",
    "PlanningCancelled",
    "PlanningError",
    "PlanningHorizonExceeded",
]


class EndpointBlockedError(NoRouteError):
    """Raised when the exact requested start or goal is hard-blocked."""


class PlanningHorizonExceeded(NoRouteError):
    """Raised when every remaining route would leave the risk coverage window."""


class PlanningCancelled(PlanningCancelledError):
    """Raised promptly after the caller's cancellation predicate becomes true."""
