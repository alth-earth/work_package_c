"""Rolling-replanning triggers, debounce, hysteresis, and route-switch policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING

from arctic_route_planning.domain.models import ReplanReason
from arctic_route_planning.publishing import RoutePlan

if TYPE_CHECKING:
    from arctic_route_planning.domain.models import ReplanningConfig


@dataclass(frozen=True, slots=True)
class ReplanningPolicy:
    min_interval: timedelta = timedelta(minutes=30)
    risk_change_threshold: float = 0.10
    risk_trigger_high: float = 0.65
    risk_clear_below: float = 0.55
    deviation_threshold_km: float = 10.0
    min_switch_improvement: float = 0.03
    risk_hysteresis: float = 0.02
    urgent_bypasses_min_interval: bool = True

    @classmethod
    def from_config(cls, config: ReplanningConfig) -> ReplanningPolicy:
        """Translate the public package configuration into runtime thresholds."""

        return cls(
            min_interval=timedelta(minutes=config.minimum_interval_minutes),
            risk_change_threshold=config.hysteresis,
            risk_trigger_high=config.risk_trigger_threshold,
            risk_clear_below=max(0.0, config.risk_trigger_threshold - config.hysteresis),
            deviation_threshold_km=config.deviation_trigger_km,
            min_switch_improvement=config.route_switch_gain_threshold,
            risk_hysteresis=config.hysteresis,
        )

    def __post_init__(self) -> None:
        if self.min_interval < timedelta(0):
            raise ValueError("min_interval cannot be negative")
        for name in (
            "risk_change_threshold",
            "risk_trigger_high",
            "risk_clear_below",
            "min_switch_improvement",
            "risk_hysteresis",
        ):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.risk_clear_below > self.risk_trigger_high:
            raise ValueError("risk_clear_below must not exceed risk_trigger_high")
        if not isfinite(self.deviation_threshold_km) or self.deviation_threshold_km < 0.0:
            raise ValueError("deviation_threshold_km must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ReplanObservation:
    """Inputs used to evaluate all five replan trigger classes."""

    observed_at: datetime
    risk_valid_time: datetime
    data_revision: int
    risk_revision: str
    route_avg_risk: float
    route_max_risk: float
    deviation_km: float = 0.0
    event_revision: str | None = None
    manual_requested: bool = False
    hard_constraint_detected: bool = False

    def __post_init__(self) -> None:
        for name in ("observed_at", "risk_valid_time"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError(f"{name} must be timezone-aware UTC")
        if isinstance(self.data_revision, bool) or self.data_revision < 0:
            raise ValueError("data_revision must be a non-negative integer")
        if not self.risk_revision.strip():
            raise ValueError("risk_revision must be non-empty")
        for name in ("route_avg_risk", "route_max_risk"):
            value = getattr(self, name)
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.route_avg_risk > self.route_max_risk:
            raise ValueError("route_avg_risk cannot exceed route_max_risk")
        if not isfinite(self.deviation_km) or self.deviation_km < 0.0:
            raise ValueError("deviation_km must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    triggered: bool
    reasons: tuple[ReplanReason, ...]
    suppressed_by_min_interval: bool = False
    retry_at: datetime | None = None


class ReplanTriggerEvaluator:
    """Stateful trigger evaluator whose baseline changes only after a committed plan."""

    def __init__(self, policy: ReplanningPolicy | None = None) -> None:
        self.policy = policy or ReplanningPolicy()
        self._last_replan_at: datetime | None = None
        self._last_risk_valid_time: datetime | None = None
        self._last_data_revision: int | None = None
        self._last_risk_revision: str | None = None
        self._baseline_avg_risk: float | None = None
        self._baseline_max_risk: float | None = None
        self._last_event_revision: str | None = None
        self._risk_latched = False

    def evaluate(self, observation: ReplanObservation) -> ReplanDecision:
        if (
            self._last_data_revision is not None
            and observation.data_revision < self._last_data_revision
        ):
            raise ValueError("data_revision cannot move backwards within a generation")
        if (
            self._last_risk_valid_time is not None
            and observation.risk_valid_time < self._last_risk_valid_time
        ):
            raise ValueError("risk_valid_time cannot move backwards within a generation")
        reasons: list[ReplanReason] = []
        if (
            self._last_risk_valid_time is not None
            and observation.risk_valid_time > self._last_risk_valid_time
        ):
            reasons.append(ReplanReason.TIME)
        if self._last_data_revision is not None and (
            observation.data_revision > self._last_data_revision
            or observation.risk_revision != self._last_risk_revision
        ):
            reasons.append(ReplanReason.DATA)

        risk_delta = 0.0
        if self._baseline_max_risk is not None and self._baseline_avg_risk is not None:
            risk_delta = max(
                abs(observation.route_max_risk - self._baseline_max_risk),
                abs(observation.route_avg_risk - self._baseline_avg_risk),
            )
        crossed_high = observation.route_max_risk >= self.policy.risk_trigger_high
        if (
            observation.hard_constraint_detected
            or (crossed_high and not self._risk_latched)
            or risk_delta >= self.policy.risk_change_threshold
        ):
            reasons.append(ReplanReason.RISK)
        if observation.deviation_km >= self.policy.deviation_threshold_km:
            reasons.append(ReplanReason.DEVIATION)
        event_changed = (
            observation.event_revision is not None
            and observation.event_revision != self._last_event_revision
        )
        if event_changed:
            reasons.append(ReplanReason.EVENT)
        if observation.manual_requested:
            reasons.append(ReplanReason.MANUAL)

        # Preserve enum declaration order and merge simultaneous causes.
        unique_reasons = tuple(dict.fromkeys(reasons))
        if not unique_reasons:
            return ReplanDecision(False, ())

        urgent = observation.manual_requested or observation.hard_constraint_detected
        if self._last_replan_at is not None:
            retry_at = self._last_replan_at + self.policy.min_interval
            if observation.observed_at < retry_at and not (
                urgent and self.policy.urgent_bypasses_min_interval
            ):
                return ReplanDecision(
                    triggered=False,
                    reasons=unique_reasons,
                    suppressed_by_min_interval=True,
                    retry_at=retry_at,
                )
        return ReplanDecision(True, unique_reasons)

    def mark_replanned(self, observation: ReplanObservation) -> None:
        """Commit the observation as the debounce/hysteresis baseline."""

        if self._last_replan_at is not None and observation.observed_at < self._last_replan_at:
            raise ValueError("cannot move the replanning baseline backwards")
        self._last_replan_at = observation.observed_at
        self._last_risk_valid_time = observation.risk_valid_time
        self._last_data_revision = observation.data_revision
        self._last_risk_revision = observation.risk_revision
        self._baseline_avg_risk = observation.route_avg_risk
        self._baseline_max_risk = observation.route_max_risk
        self._last_event_revision = observation.event_revision
        if observation.route_max_risk >= self.policy.risk_trigger_high:
            self._risk_latched = True
        elif observation.route_max_risk <= self.policy.risk_clear_below:
            self._risk_latched = False


@dataclass(frozen=True, slots=True)
class SwitchDecision:
    accepted: bool
    reason: str
    relative_improvement: float


class RouteSwitchGate:
    """Apply route benefit and risk hysteresis before changing the displayed plan."""

    def __init__(self, policy: ReplanningPolicy | None = None) -> None:
        self.policy = policy or ReplanningPolicy()

    def evaluate(
        self,
        current: RoutePlan | None,
        candidate: RoutePlan,
        *,
        reasons: tuple[ReplanReason, ...] = (),
        force: bool = False,
    ) -> SwitchDecision:
        if current is None:
            return SwitchDecision(True, "no current route", 1.0)
        if candidate.scenario_id != current.scenario_id:
            return SwitchDecision(False, "scenario mismatch", 0.0)
        if candidate.generation_id != current.generation_id:
            return SwitchDecision(False, "generation mismatch", 0.0)
        if candidate.corridor_id != current.corridor_id:
            return SwitchDecision(False, "corridor mismatch", 0.0)
        if candidate.vessel_profile_id != current.vessel_profile_id:
            return SwitchDecision(False, "vessel mismatch", 0.0)
        denominator = max(abs(current.metrics.objective_cost), 1e-12)
        improvement = (
            current.metrics.objective_cost - candidate.metrics.objective_cost
        ) / denominator
        if force or ReplanReason.EVENT in reasons or ReplanReason.MANUAL in reasons:
            return SwitchDecision(True, "forced by event/manual change", improvement)
        if ReplanReason.RISK in reasons and (
            candidate.metrics.max_risk <= current.metrics.max_risk - self.policy.risk_hysteresis
        ):
            return SwitchDecision(True, "risk reduced beyond hysteresis", improvement)
        if candidate.metrics.max_risk > current.metrics.max_risk + self.policy.risk_hysteresis:
            return SwitchDecision(False, "candidate increases risk beyond hysteresis", improvement)
        if improvement < self.policy.min_switch_improvement:
            return SwitchDecision(False, "benefit below route-switch threshold", improvement)
        return SwitchDecision(True, "benefit threshold met", improvement)
