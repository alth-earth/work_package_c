"""Stable formal facade for the deterministic multi-span G2 geometry engine.

The historical research module remains import-compatible and owns its frozen
schema constants. Formal producers import this facade so research
serialization details do not become part of the C -> D contract surface.
"""

from typing import Any

from arctic_route_planning.research.route_smoothing import (
    CandidateDecision,
    RouteSmoothingPolicy,
)
from arctic_route_planning.research.route_smoothing_v2 import (
    MultiSpanRouteResult,
    MultiSpanRouteSegment,
)
from arctic_route_planning.research.route_smoothing_v2 import (
    build_multispan_route_smoothing as _build_research_multispan_route_smoothing,
)

# Formal motion uses the largest tested local trim that remains strictly
# below one half of either adjacent authoritative leg.  The frozen research
# sidecar default stays at 0.45; this formal-only policy was qualified against
# the exact Winter A/B/C identity with risk, hard-mask, continuous-corridor,
# ETA, speed, curvature, yaw-rate and lateral-acceleration gates enabled.
FORMAL_ROUTE_SMOOTHING_POLICY = RouteSmoothingPolicy(
    max_trim_fraction=0.49,
    sample_spacing_m=250.0,
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
    "FORMAL_ROUTE_SMOOTHING_POLICY",
    "CandidateDecision",
    "MultiSpanRouteResult",
    "MultiSpanRouteSegment",
    "RouteSmoothingPolicy",
    "build_multispan_route_smoothing",
]
