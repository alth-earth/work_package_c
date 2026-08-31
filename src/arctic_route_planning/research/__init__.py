"""Research-only planning helpers.

Nothing in this package is part of the public C route contract.  Research
artifacts must be explicitly consumed by an experiment or replay adapter.
"""

from .route_smoothing import (
    CLAMPED_CUBIC_KNOT_VECTOR,
    CandidateDecision,
    CandidateValidator,
    CurveSegment,
    RouteSmoothingPolicy,
    RouteSmoothingResult,
    build_route_smoothing,
    build_route_smoothing_sidecar,
    evaluate_clamped_cubic_bspline,
    evaluate_clamped_cubic_bspline_derivatives,
)
from .route_smoothing_qualification import (
    CorridorValidator,
    RouteSmoothingQualificationError,
    build_qualified_route_smoothing_sidecar,
)
from .route_smoothing_qualification_v2 import (
    RasterCorridorValidator,
    build_qualified_route_smoothing_sidecar_v2,
)
from .route_smoothing_v2 import (
    MultiSpanRouteResult,
    MultiSpanRouteSegment,
    build_multispan_route_smoothing,
)

__all__ = [
    "CLAMPED_CUBIC_KNOT_VECTOR",
    "CandidateDecision",
    "CandidateValidator",
    "CorridorValidator",
    "CurveSegment",
    "MultiSpanRouteResult",
    "MultiSpanRouteSegment",
    "RasterCorridorValidator",
    "RouteSmoothingPolicy",
    "RouteSmoothingQualificationError",
    "RouteSmoothingResult",
    "build_multispan_route_smoothing",
    "build_qualified_route_smoothing_sidecar",
    "build_qualified_route_smoothing_sidecar_v2",
    "build_route_smoothing",
    "build_route_smoothing_sidecar",
    "evaluate_clamped_cubic_bspline",
    "evaluate_clamped_cubic_bspline_derivatives",
]
