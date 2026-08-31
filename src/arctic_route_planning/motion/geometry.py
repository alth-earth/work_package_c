"""Stable formal facade for the deterministic multi-span G2 geometry engine.

The historical research module remains import-compatible and owns its frozen
schema constants. Formal producers import this facade so research
serialization details do not become part of the C -> D contract surface.
"""

from arctic_route_planning.research.route_smoothing import RouteSmoothingPolicy
from arctic_route_planning.research.route_smoothing_v2 import (
    MultiSpanRouteResult,
    MultiSpanRouteSegment,
    build_multispan_route_smoothing,
)

__all__ = [
    "MultiSpanRouteResult",
    "MultiSpanRouteSegment",
    "RouteSmoothingPolicy",
    "build_multispan_route_smoothing",
]
