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


class PlanningCancelled(PlanningCancelledError):
    """Concrete cancellation signal raised by the planner and coordinator.

    This is the single canonical definition: the planner and replanning
    subpackages re-export it so historical import paths keep working.
    """


class NoRouteError(PlanningError):
    """No route satisfying the hard constraints was found."""


class LegacyDataError(PlanningError):
    """A legacy B artifact is absent, ambiguous, or unsafe to adapt."""
