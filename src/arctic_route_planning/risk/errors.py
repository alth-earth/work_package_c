"""Errors raised while validating and sampling BC risk frames."""

from arctic_route_planning.errors import (
    ContextMismatchError,
    PlanningError,
)
from arctic_route_planning.errors import RiskCoverageError as BaseRiskCoverageError


class RiskSamplingError(PlanningError, ValueError):
    """Base class for deterministic risk-sampling failures."""


class IncompatibleRiskFramesError(ContextMismatchError, RiskSamplingError):
    """Raised when frames cannot safely be combined or interpolated."""


class RiskCoverageError(BaseRiskCoverageError, RiskSamplingError):
    """Raised when a requested time is outside the published risk window."""


class RiskOutOfBoundsError(RiskSamplingError):
    """Raised when a spatial sample lies outside the risk grid."""
