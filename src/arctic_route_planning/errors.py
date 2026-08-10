"""Stable error types used across Work Package C."""


class PlanningError(RuntimeError):
    """Base class for planning failures with explicit semantics."""


class ContractError(PlanningError, ValueError):
    """A cross-package object violates the versioned contract."""


class RiskCoverageError(PlanningError):
    """The risk sequence cannot support the requested ETA or planning horizon."""


class ContextMismatchError(PlanningError):
    """Scenario, vessel, generation, or configuration identity does not match."""


class StalePlanningResultError(PlanningError):
    """An obsolete task attempted to publish after a newer task or generation."""


class PlanningCancelledError(PlanningError):
    """A cooperative cancellation request stopped the planner."""


class NoRouteError(PlanningError):
    """No route satisfying the hard constraints was found."""


class LegacyDataError(PlanningError):
    """A legacy B artifact is absent, ambiguous, or unsafe to adapt."""
