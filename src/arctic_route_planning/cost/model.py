"""Explainable route-edge costs expressed in equivalent hours."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from arctic_route_planning.domain.models import CostWeights


@dataclass(frozen=True, slots=True)
class EdgeCostInput:
    distance_km: float
    travel_hours: float
    risk_score: float
    confidence: float
    heading_change_degrees: float = 0.0
    deviation_fraction: float = 0.0


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Raw equivalent-hour components and their weighted total."""

    travel_hours: float
    risk_exposure_hours: float
    distance_equivalent_hours: float
    turn_equivalent_hours: float
    deviation_equivalent_hours: float
    low_confidence_hours: float
    total_equivalent_hours: float


@dataclass(frozen=True, slots=True)
class CostModel:
    weights: CostWeights
    maximum_speed_km_per_hour: float
    full_turn_penalty_hours: float = 0.25
    full_deviation_penalty_hours: float = 1.0
    deviation_weight: float = 0.0
    policy_version: str = "equivalent-hours-v1"

    def __post_init__(self) -> None:
        if self.maximum_speed_km_per_hour <= 0:
            raise ValueError("maximum_speed_km_per_hour must be positive")
        if self.full_turn_penalty_hours < 0 or self.full_deviation_penalty_hours < 0:
            raise ValueError("penalty hours must be non-negative")
        if not isfinite(self.deviation_weight) or self.deviation_weight < 0:
            raise ValueError("deviation_weight must be finite and non-negative")

    def evaluate(self, value: EdgeCostInput) -> CostBreakdown:
        _validate_edge_input(value)
        risk_hours = value.travel_hours * value.risk_score
        distance_hours = value.distance_km / self.maximum_speed_km_per_hour
        turn_hours = value.heading_change_degrees / 180.0 * self.full_turn_penalty_hours
        deviation_hours = value.deviation_fraction * self.full_deviation_penalty_hours
        low_confidence_hours = value.travel_hours * (1.0 - value.confidence)
        total = (
            self.weights.travel_time * value.travel_hours
            + self.weights.risk * risk_hours
            + self.weights.distance * distance_hours
            + self.weights.turn * turn_hours
            + self.deviation_weight * deviation_hours
            + self.weights.uncertainty * low_confidence_hours
        )
        return CostBreakdown(
            travel_hours=value.travel_hours,
            risk_exposure_hours=risk_hours,
            distance_equivalent_hours=distance_hours,
            turn_equivalent_hours=turn_hours,
            deviation_equivalent_hours=deviation_hours,
            low_confidence_hours=low_confidence_hours,
            total_equivalent_hours=total,
        )

    def lower_bound(self, remaining_distance_km: float) -> float:
        """Admissible lower bound used by A*: risk/turn/deviation lower bounds are zero.

        Admissibility argument (C-ALG-04 correctness debt, 2026-08-28):
        let ``D`` be the remaining straight-line distance, ``d`` the true path
        distance (``d >= D``), and ``v_max`` the vessel maximum speed.  For any
        traversal, ``travel_hours >= D / v_max`` because the realised speed never
        exceeds ``v_max``.  The true cost decomposes into non-negative
        equivalent-hour terms:

            cost = w_travel*travel_hours
                 + w_risk*risk_hours
                 + w_distance*distance_hours
                 + w_turn*turn_hours
                 + w_dev*deviation_hours
                 + w_unc*uncertainty_hours

        with ``distance_hours = d / v_max >= D / v_max``.  Hence

            (w_travel + w_distance) * D / v_max
                <= w_travel*travel_hours + w_distance*distance_hours
                <= cost

        because every weight is non-negative and every remaining term is
        non-negative.  ``lower_bound`` is therefore admissible.  When called with
        ``remaining_distance_km`` = straight-line distance (as ``_heuristic``
        does), the bound is additionally conservative because straight-line
        distance never exceeds the true path distance; consistency follows from
        the triangle inequality on straight-line distances.
        """

        if remaining_distance_km < 0 or not isfinite(remaining_distance_km):
            raise ValueError("remaining_distance_km must be finite and non-negative")
        fastest_hours = remaining_distance_km / self.maximum_speed_km_per_hour
        return (self.weights.travel_time + self.weights.distance) * fastest_hours


def _validate_edge_input(value: EdgeCostInput) -> None:
    values = (
        value.distance_km,
        value.travel_hours,
        value.risk_score,
        value.confidence,
        value.heading_change_degrees,
        value.deviation_fraction,
    )
    if any(not isfinite(item) for item in values):
        raise ValueError("edge cost inputs must be finite")
    if value.distance_km < 0 or value.travel_hours < 0:
        raise ValueError("distance and travel time must be non-negative")
    if not 0 <= value.risk_score <= 1 or not 0 <= value.confidence <= 1:
        raise ValueError("risk_score and confidence must be in [0, 1]")
    if not 0 <= value.heading_change_degrees <= 180:
        raise ValueError("heading_change_degrees must be in [0, 180]")
    if value.deviation_fraction < 0:
        raise ValueError("deviation_fraction must be non-negative")
