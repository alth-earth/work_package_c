"""Research-only planning helpers.

Nothing in this package is part of the public C route contract.  Research
artifacts must be explicitly consumed by an experiment or replay adapter.
"""

from .route_smoothing import (
    CandidateDecision,
    CurveSegment,
    RouteSmoothingPolicy,
    RouteSmoothingResult,
    build_route_smoothing,
    build_route_smoothing_sidecar,
)

__all__ = [
    "CandidateDecision",
    "CurveSegment",
    "RouteSmoothingPolicy",
    "RouteSmoothingResult",
    "build_route_smoothing",
    "build_route_smoothing_sidecar",
]
