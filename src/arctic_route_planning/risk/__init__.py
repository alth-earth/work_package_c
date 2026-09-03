"""ETA-aware access to BC risk frames."""

from importlib import import_module

from .errors import (
    IncompatibleRiskFramesError,
    RiskCoverageError,
    RiskOutOfBoundsError,
    RiskSamplingError,
)
from .sampler import (
    RiskIdentity,
    RiskIntervalSample,
    RiskSampler,
    SampledRisk,
    SweptTemporalEnvelope,
)

__all__ = [
    "ExperimentalRiskSampler",
    "IncompatibleRiskFramesError",
    "RiskCoverageError",
    "RiskIdentity",
    "RiskIntervalSample",
    "RiskOutOfBoundsError",
    "RiskSampler",
    "RiskSamplingError",
    "SampleCacheMode",
    "SampledRisk",
    "SweptTemporalEnvelope",
]


def __getattr__(name: str):
    """Keep research cache compatibility without importing it in formal runs.

    The production ingress imports this package for ``RiskSampler``.  Eagerly
    importing the experimental cache made every formal application and frozen
    binary carry research-only code even when it was never selected.
    """
    if name in {"ExperimentalRiskSampler", "SampleCacheMode"}:
        module_name = ".".join((__package__, "experimental_cache"))
        return getattr(import_module(module_name), name)
    raise AttributeError(name)
