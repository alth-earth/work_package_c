"""Internal ARA* feasibility candidate.

This module is intentionally not imported from :mod:`arctic_route_planning.planners`.
It implements an anytime repairing weighted A* over the same approximate
``(node, time_bucket, heading)`` state graph as the control planner.  The
candidate is for M0 semantic checks only; it does not change the formal
planner, contracts, or publication path.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from heapq import heappop, heappush
from itertools import count, pairwise
from math import isfinite
from time import perf_counter

from arctic_route_planning.cost import UnnavigableSpeedError
from arctic_route_planning.risk import RiskCoverageError

from .errors import NoRouteError, PlanningHorizonExceeded
from .time_dependent_astar import (
    _COST_EPSILON,
    PlanningRequest,
    PlanningResult,
    State,
    TimeDependentAStar,
    _Counters,
    _EdgeTraversal,
    _RejectedEdge,
)

_DEFAULT_EPSILON_SCHEDULE = (2.5, 2.0, 1.5, 1.0)


@dataclass(frozen=True, slots=True)
class AraStage:
    """One incumbent snapshot produced at a fixed inflation factor."""

    epsilon: float
    result: PlanningResult
    expanded_since_previous: int
    first_solution_cost_hours: float
    first_solution_elapsed_ms: float
    lower_bound_hours: float
    observed_gap: float


@dataclass(frozen=True, slots=True)
class AraCandidateResult:
    """Final route plus all immutable anytime incumbent snapshots."""

    stages: tuple[AraStage, ...]

    @property
    def final_result(self) -> PlanningResult:
        if not self.stages:  # pragma: no cover - constructor validation below
            raise RuntimeError("ARA result has no stages")
        return self.stages[-1].result

    @property
    def nodes(self) -> tuple[tuple[int, int], ...]:
        return self.final_result.nodes


class AraSearchLimitExceeded(NoRouteError):
    """The internal ARA candidate exhausted the shared expansion budget."""


class AnytimeRepairingAStar(TimeDependentAStar):
    """Research-only ARA* candidate with a fixed epsilon schedule."""

    def plan(
        self,
        request: PlanningRequest,
        *,
        epsilon_schedule: Iterable[float] = _DEFAULT_EPSILON_SCHEDULE,
    ) -> AraCandidateResult:
        schedule = _validate_schedule(epsilon_schedule)
        started = perf_counter()
        self._last_counters = None
        self._heur_dist = {}
        self._calm_speed = self.vessel_model.effective_speed(1.0)
        self._progress_interval = None
        self._check_cancelled(request)
        self._validate_request_nodes(request)
        start_sample = self._sample_node(request.start, request.departure_time)
        if start_sample.hard_mask:
            raise NoRouteError(f"start node {request.start} is hard-blocked")

        start_state: State = (request.start, 0, None)
        if request.start == request.goal:
            return AraCandidateResult(
                stages=(
                    AraStage(
                        epsilon=1.0,
                        result=self._zero_length_result(request, start_sample, started),
                        expanded_since_previous=0,
                        first_solution_cost_hours=0.0,
                        first_solution_elapsed_ms=0.0,
                        lower_bound_hours=0.0,
                        observed_gap=0.0,
                    ),
                )
            )

        cost_model = self._cost_model(request.objective)
        labels: dict[State, tuple[float, datetime]] = {start_state: (0.0, request.departure_time)}
        predecessor: dict[State, tuple[State, _EdgeTraversal]] = {}
        closed: set[State] = set()
        inconsistent: set[State] = set()
        open_states: set[State] = {start_state}
        versions: dict[State, int] = {start_state: 0}
        serial = count()
        queue: list[tuple[float, float, int, int, State]] = []
        counters = _Counters()
        self._push_open(
            queue,
            open_states,
            versions,
            serial,
            start_state,
            0.0,
            request,
            cost_model,
            schedule[0],
            counters=counters,
        )
        incumbent_state: State | None = None
        incumbent_cost = float("inf")
        stages: list[AraStage] = []
        previous_expanded = 0
        first_solution_cost: float | None = None
        first_solution_elapsed_ms: float | None = None

        for stage_index, epsilon in enumerate(schedule):
            self._check_cancelled(request)
            if stage_index:
                # ARA* repairs the previous search tree: states improved while
                # closed are moved back to OPEN, and all existing OPEN keys are
                # refreshed for the lower epsilon.  Search-local state remains
                # isolated to this request.
                closed.clear()
                for state in inconsistent:
                    open_states.add(state)
                    self._push_open(
                        queue,
                        open_states,
                        versions,
                        serial,
                        state,
                        labels[state][0],
                        request,
                        cost_model,
                        epsilon,
                        counters=counters,
                    )
                inconsistent.clear()
                for state in tuple(open_states):
                    self._push_open(
                        queue,
                        open_states,
                        versions,
                        serial,
                        state,
                        labels[state][0],
                        request,
                        cost_model,
                        epsilon,
                        counters=counters,
                    )

            while queue:
                self._check_cancelled(request)
                best_key, queued_cost, version, _, state = heappop(queue)
                counters.heap_pops += 1
                if version != versions.get(state) or queued_cost != labels[state][0]:
                    counters.stale_pop += 1
                    continue
                open_states.discard(state)
                node, _, incoming_code = state
                if incumbent_state is not None:
                    incumbent_cost = labels[incumbent_state][0]
                minimum_open_key = best_key
                if (
                    incumbent_state is not None
                    and incumbent_cost <= minimum_open_key + _COST_EPSILON
                ):
                    # The incumbent satisfies the current weighted stopping
                    # condition; retain it and repair only at the next stage.
                    open_states.add(state)
                    self._push_open(
                        queue,
                        open_states,
                        versions,
                        serial,
                        state,
                        queued_cost,
                        request,
                        cost_model,
                        epsilon,
                        counters=counters,
                    )
                    break
                counters.expanded += 1
                counters.unique = len(labels)
                counters.best_f = min(counters.best_f, best_key)
                counters.last_f = best_key
                counters.last_g = queued_cost
                self._last_counters = counters
                if counters.expanded > request.max_expansions:
                    raise AraSearchLimitExceeded(
                        f"ARA planning exceeded max_expansions={request.max_expansions}"
                    )
                if node == request.goal:
                    if queued_cost < incumbent_cost - _COST_EPSILON:
                        incumbent_state = state
                        incumbent_cost = queued_cost
                        if first_solution_cost is None:
                            first_solution_cost = incumbent_cost
                            first_solution_elapsed_ms = (perf_counter() - started) * 1000.0
                    continue

                closed.add(state)
                arrival_time = labels[state][1]
                for neighbor in self.grid.neighbors(node):
                    self._check_cancelled(request)
                    try:
                        previous_heading = None
                        if incoming_code is not None:
                            previous_node = (
                                node[0] - incoming_code[0],
                                node[1] - incoming_code[1],
                            )
                            previous_heading = self._edge_geometry(previous_node, node)[1]
                        traversal = self._evaluate_edge(
                            node,
                            neighbor,
                            arrival_time,
                            previous_heading,
                            request,
                            cost_model,
                        )
                    except RiskCoverageError:
                        counters.coverage += 1
                        continue
                    except UnnavigableSpeedError:
                        counters.speed += 1
                        continue
                    except _RejectedEdge as rejection:
                        if rejection.reason == "hard":
                            counters.hard += 1
                        else:
                            counters.risk += 1
                        continue
                    elapsed = traversal.arrival_time - request.departure_time
                    if request.maximum_elapsed is not None and elapsed > request.maximum_elapsed:
                        counters.coverage += 1
                        continue
                    time_bucket = int(
                        elapsed.total_seconds() // request.time_bucket_size.total_seconds()
                    )
                    counters.max_bucket = max(counters.max_bucket, time_bucket)
                    heading_code = (neighbor[0] - node[0], neighbor[1] - node[1])
                    next_state: State = (neighbor, time_bucket, heading_code)
                    tentative_cost = queued_cost + traversal.cost.total_equivalent_hours
                    previous = labels.get(next_state)
                    if previous is not None and tentative_cost >= previous[0] - _COST_EPSILON:
                        continue
                    labels[next_state] = (tentative_cost, traversal.arrival_time)
                    predecessor[next_state] = (state, traversal)
                    counters.generated += 1
                    counters.unique = len(labels)
                    if next_state in closed:
                        inconsistent.add(next_state)
                    else:
                        self._push_open(
                            queue,
                            open_states,
                            versions,
                            serial,
                            next_state,
                            tentative_cost,
                            request,
                            cost_model,
                            epsilon,
                            counters=counters,
                        )
                    if neighbor == request.goal and tentative_cost < incumbent_cost - _COST_EPSILON:
                        incumbent_state = next_state
                        incumbent_cost = tentative_cost
                        if first_solution_cost is None:
                            first_solution_cost = incumbent_cost
                            first_solution_elapsed_ms = (perf_counter() - started) * 1000.0

            if incumbent_state is None:
                if not queue:
                    if counters.coverage:
                        raise PlanningHorizonExceeded(
                            "no complete route fits inside the available risk time window"
                        )
                    raise NoRouteError("ARA found no route satisfying constraints")
                continue
            result = self._build_result(
                request,
                incumbent_state,
                start_sample,
                labels,
                predecessor,
                counters,
                started,
            )
            lower_bound = self._open_lower_bound(
                queue,
                versions,
                labels,
                incumbent_cost,
                request,
                cost_model,
            )
            stages.append(
                AraStage(
                    epsilon=epsilon,
                    result=result,
                    expanded_since_previous=counters.expanded - previous_expanded,
                    first_solution_cost_hours=(
                        first_solution_cost if first_solution_cost is not None else incumbent_cost
                    ),
                    first_solution_elapsed_ms=(
                        first_solution_elapsed_ms
                        if first_solution_elapsed_ms is not None
                        else (perf_counter() - started) * 1000.0
                    ),
                    lower_bound_hours=lower_bound,
                    observed_gap=_observed_gap(incumbent_cost, lower_bound),
                )
            )
            previous_expanded = counters.expanded

        if not stages:
            raise NoRouteError("ARA found no route satisfying constraints")
        return AraCandidateResult(stages=tuple(stages))

    def _open_lower_bound(
        self,
        queue: list[tuple[float, float, int, int, State]],
        versions: dict[State, int],
        labels: dict[State, tuple[float, datetime]],
        incumbent_cost: float,
        request: PlanningRequest,
        cost_model,
    ) -> float:
        """Return an observational unweighted lower bound for the open set."""

        lower_bound = incumbent_cost
        for _, queued_cost, version, _, state in queue:
            if version != versions.get(state) or queued_cost != labels[state][0]:
                continue
            lower_bound = min(
                lower_bound,
                queued_cost + self._heuristic(state[0], request.goal, cost_model, request),
            )
        return lower_bound

    def _push_open(
        self,
        queue: list[tuple[float, float, int, int, State]],
        open_states: set[State],
        versions: dict[State, int],
        serial: Iterable[int],
        state: State,
        cost: float,
        request: PlanningRequest,
        cost_model,
        epsilon: float,
        counters: _Counters,
    ) -> None:
        version = versions.get(state, -1) + 1
        versions[state] = version
        open_states.add(state)
        key = cost + epsilon * self._heuristic(state[0], request.goal, cost_model, request)
        heappush(queue, (key, cost, version, next(serial), state))
        counters.heap_pushes += 1
        counters.queue_peak = max(counters.queue_peak, len(queue))


def _validate_schedule(values: Iterable[float]) -> tuple[float, ...]:
    schedule = tuple(float(value) for value in values)
    if not schedule:
        raise ValueError("epsilon_schedule must not be empty")
    if any(not isfinite(value) or value < 1.0 for value in schedule):
        raise ValueError("epsilon_schedule values must be finite and >= 1")
    if any(left < right for left, right in pairwise(schedule)):
        raise ValueError("epsilon_schedule must be non-increasing")
    if schedule[-1] != 1.0:
        raise ValueError("epsilon_schedule must terminate at epsilon=1.0")
    return schedule


def _observed_gap(incumbent_cost: float, lower_bound: float) -> float:
    """Return a non-negative incumbent/lower-bound gap for diagnostics."""

    if lower_bound <= _COST_EPSILON:
        return 0.0 if incumbent_cost <= _COST_EPSILON else float("inf")
    return max(0.0, incumbent_cost / lower_bound - 1.0)


__all__ = [
    "AnytimeRepairingAStar",
    "AraCandidateResult",
    "AraSearchLimitExceeded",
    "AraStage",
]
