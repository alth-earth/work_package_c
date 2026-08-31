"""Stable formal facade for the deterministic multi-span G2 geometry engine.

The historical research module remains import-compatible and owns its frozen
schema constants. Formal producers import this facade so research
serialization details do not become part of the C -> D contract surface.
"""

from typing import Any

from arctic_route_planning.research.route_smoothing import RouteSmoothingPolicy
from arctic_route_planning.research.route_smoothing_v2 import (
    MultiSpanRouteResult,
    MultiSpanRouteSegment,
)
from arctic_route_planning.research.route_smoothing_v2 import (
    build_multispan_route_smoothing as _build_research_multispan_route_smoothing,
)


def build_multispan_route_smoothing(*args: Any, **kwargs: Any) -> MultiSpanRouteResult:
    """Build formal motion geometry with the no-inflection control policy.

    The research v2 function remains the compatibility path for frozen R1
    sidecars.  Formal C motion uses the corrected central control point and
    its explicit turn-direction fail-closed check.
    """

    kwargs["turn_direction_safe"] = True
    return _build_research_multispan_route_smoothing(*args, **kwargs)

__all__ = [
    "MultiSpanRouteResult",
    "MultiSpanRouteSegment",
    "RouteSmoothingPolicy",
    "build_multispan_route_smoothing",
]
