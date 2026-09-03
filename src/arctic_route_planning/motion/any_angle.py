"""Deterministic any-angle geometry for the formal route-motion producer.

The control planner remains an eight-neighbour grid search.  This module is a
derived-geometry layer: its graph vertices are *only* authoritative waypoint
indices and every edge is a great-circle segment between two such vertices.
Safety is deliberately supplied by a caller-owned validator.  An unvalidated
edge is geometry only and must not be interpreted as navigable.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any

from arctic_route_planning.timeutils import ensure_utc

Coordinate = tuple[float, float]
EdgeValidator = Callable[["AnyAngleEdge"], "AnyAngleDecision | bool | Mapping[str, Any]"]
EARTH_RADIUS_M = 6_371_008.8


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        .encode("utf-8")
    ).hexdigest()


def _coordinate(value: Any) -> Coordinate:
    if isinstance(value, Mapping):
        lon = value.get("lon", value.get("longitude"))
        lat = value.get("lat", value.get("latitude"))
    else:
        try:
            lon, lat = value
        except (TypeError, ValueError) as exc:
            raise ValueError("a coordinate must contain longitude and latitude") from exc
    if isinstance(lon, bool) or isinstance(lat, bool):
        raise ValueError("coordinates must be numeric")
    try:
        longitude = float(lon)
        latitude = float(lat)
    except (TypeError, ValueError) as exc:
        raise ValueError("coordinates must be numeric") from exc
    if (
        not math.isfinite(longitude)
        or not math.isfinite(latitude)
        or not -180.0 <= longitude <= 180.0
        or not -90.0 <= latitude <= 90.0
    ):
        raise ValueError("coordinates are outside the geographic domain")
    return longitude, latitude


def great_circle_distance_m(first: Coordinate, second: Coordinate) -> float:
    """Return the spherical great-circle distance in metres."""

    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    delta_lat = lat2 - lat1
    delta_lon = (lon2 - lon1 + math.pi) % (2.0 * math.pi) - math.pi
    haversine = (
        math.sin(delta_lat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2.0) ** 2
    )
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(max(0.0, haversine))))


def great_circle_interpolate(
    first: Coordinate, second: Coordinate, fraction: float
) -> Coordinate:
    """Interpolate on the unit-sphere great circle, including exact endpoints."""

    if not math.isfinite(fraction) or not 0.0 <= fraction <= 1.0:
        raise ValueError("great-circle fraction must be finite and in [0, 1]")
    if fraction == 0.0:
        return first
    if fraction == 1.0:
        return second
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    vectors = (
        (
            math.cos(lat1) * math.cos(lon1),
            math.cos(lat1) * math.sin(lon1),
            math.sin(lat1),
        ),
        (
            math.cos(lat2) * math.cos(lon2),
            math.cos(lat2) * math.sin(lon2),
            math.sin(lat2),
        ),
    )
    dot = max(-1.0, min(1.0, sum(a * b for a, b in zip(vectors[0], vectors[1], strict=True))))
    angle = math.acos(dot)
    if angle <= 1.0e-12:
        return first
    sine = math.sin(angle)
    left = math.sin((1.0 - fraction) * angle) / sine
    right = math.sin(fraction * angle) / sine
    vector = tuple(
        left * vectors[0][index] + right * vectors[1][index] for index in range(3)
    )
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 1.0e-15 or not math.isfinite(norm):
        raise ValueError("great-circle interpolation became degenerate")
    x, y, z = (value / norm for value in vector)
    longitude = math.degrees(math.atan2(y, x))
    latitude = math.degrees(math.atan2(z, math.hypot(x, y)))
    return longitude, latitude


def _sample_great_circle(
    first: Coordinate,
    second: Coordinate,
    *,
    spacing_m: float,
) -> tuple[Coordinate, ...]:
    distance = great_circle_distance_m(first, second)
    if not math.isfinite(distance) or distance <= 1.0e-6:
        raise ValueError("any-angle edge must have positive length")
    count = max(1, math.ceil(distance / spacing_m))
    return tuple(
        great_circle_interpolate(first, second, index / count)
        for index in range(count + 1)
    )


def _edge_times(
    points: Sequence[Coordinate],
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime | None, ...]:
    if start is None or end is None:
        return tuple(None for _ in points)
    start = ensure_utc(start, field="any-angle edge start")
    end = ensure_utc(end, field="any-angle edge end")
    if end <= start:
        raise ValueError("any-angle edge ETA must increase")
    distances = [0.0]
    for first, second in pairwise(points):
        distances.append(distances[-1] + great_circle_distance_m(first, second))
    total = distances[-1]
    if total <= 0.0:
        raise ValueError("any-angle edge has no measurable length")
    duration = end - start
    return tuple(start + duration * (distance / total) for distance in distances)


@dataclass(frozen=True, slots=True)
class AnyAngleDecision:
    """Fail-closed result returned by an any-angle edge validator."""

    accepted: bool
    reason: str | None = None
    evidence: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise TypeError("AnyAngleDecision.accepted must be bool")
        if not self.accepted and not self.reason:
            raise ValueError("rejected any-angle edges require a reason")


@dataclass(frozen=True, slots=True)
class AnyAngleEdge:
    start_index: int
    end_index: int
    start: Coordinate
    end: Coordinate
    points: tuple[Coordinate, ...]
    sample_times: tuple[datetime | None, ...]
    length_m: float
    accepted: bool = False
    rejection_reason: str | None = None
    validation_evidence: Mapping[str, Any] | None = None

    @property
    def is_direct_endpoint_edge(self) -> bool:
        return self.start_index == 0

    @property
    def skipped_waypoint_indices(self) -> tuple[int, ...]:
        return tuple(range(self.start_index + 1, self.end_index))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start": list(self.start),
            "end": list(self.end),
            "length_m": self.length_m,
            "sample_count": len(self.points),
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "skipped_waypoint_indices": list(self.skipped_waypoint_indices),
            "validation_evidence": (
                dict(self.validation_evidence)
                if self.validation_evidence is not None
                else None
            ),
        }


@dataclass(frozen=True, slots=True)
class AnyAngleRoute:
    """One accepted DAG route assembled from authoritative waypoint vertices."""

    raw_points: tuple[Coordinate, ...]
    waypoint_indices: tuple[int, ...]
    boundary_indices: tuple[int, ...]
    points: tuple[Coordinate, ...]
    sample_times: tuple[datetime | None, ...]
    edges: tuple[AnyAngleEdge, ...]
    rejected_edges: tuple[dict[str, Any], ...]
    raw_route_digest: str
    evaluated_edge_count: int = 0
    maximum_edge_evaluations: int = 0

    @property
    def accepted(self) -> bool:
        return bool(self.edges) and all(edge.accepted for edge in self.edges)

    @property
    def length_m(self) -> float:
        return sum(edge.length_m for edge in self.edges)

    @property
    def shortcut_count(self) -> int:
        return sum(max(0, edge.end_index - edge.start_index - 1) for edge in self.edges)

    @property
    def direct_attempted(self) -> bool:
        return any(edge.start_index == 0 and edge.end_index == len(self.raw_points) - 1
                   for edge in self.edges) or any(
            item.get("start_index") == 0
            and item.get("end_index") == len(self.raw_points) - 1
            for item in self.rejected_edges
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_route_digest": self.raw_route_digest,
            "waypoint_indices": list(self.waypoint_indices),
            "boundary_indices": list(self.boundary_indices),
            "length_m": self.length_m,
            "shortcut_count": self.shortcut_count,
            "direct_attempted": self.direct_attempted,
            "evaluated_edge_count": self.evaluated_edge_count,
            "maximum_edge_evaluations": self.maximum_edge_evaluations,
            "edges": [edge.to_dict() for edge in self.edges],
            "rejected_edges": [dict(item) for item in self.rejected_edges],
        }


def _decision(value: Any) -> AnyAngleDecision:
    if isinstance(value, AnyAngleDecision):
        return value
    if isinstance(value, bool):
        return AnyAngleDecision(value, None if value else "edge_validator_rejected")
    if isinstance(value, Mapping):
        accepted = value.get("accepted") is True
        return AnyAngleDecision(
            accepted,
            None if accepted else str(value.get("reason") or "edge_validator_rejected"),
            value.get("evidence") if isinstance(value.get("evidence"), Mapping) else None,
        )
    raise TypeError("edge validator must return AnyAngleDecision, bool or mapping")


def _raw_digest(points: Sequence[Coordinate]) -> str:
    return _canonical_digest([list(point) for point in points])


def _route_from_indices(
    points: tuple[Coordinate, ...],
    times: tuple[datetime, ...] | None,
    indices: tuple[int, ...],
    cache: Mapping[tuple[int, int], AnyAngleEdge],
    rejected: Sequence[Mapping[str, Any]],
    evaluated_edge_count: int,
    maximum_edge_evaluations: int,
    *,
    force: bool = False,
) -> AnyAngleRoute:
    selected_edges: list[AnyAngleEdge] = []
    assembled_points: list[Coordinate] = []
    assembled_times: list[datetime | None] = []
    boundaries: list[int] = []
    for path_position, (start_index, end_index) in enumerate(pairwise(indices)):
        edge = cache[(start_index, end_index)]
        if not edge.accepted and not force:
            raise ValueError("cannot assemble a rejected any-angle edge")
        if force and not edge.accepted:
            edge = AnyAngleEdge(
                start_index=edge.start_index,
                end_index=edge.end_index,
                start=edge.start,
                end=edge.end,
                points=edge.points,
                sample_times=edge.sample_times,
                length_m=edge.length_m,
                accepted=True,
                validation_evidence={"forced_raw_route": True},
            )
        selected_edges.append(edge)
        if path_position == 0:
            assembled_points.extend(edge.points)
            assembled_times.extend(edge.sample_times)
        else:
            assembled_points.extend(edge.points[1:])
            assembled_times.extend(edge.sample_times[1:])
        boundaries.append(len(assembled_points) - 1)
    return AnyAngleRoute(
        raw_points=points,
        waypoint_indices=indices,
        boundary_indices=tuple([0, *boundaries]),
        points=tuple(assembled_points),
        sample_times=tuple(assembled_times),
        edges=tuple(selected_edges),
        rejected_edges=tuple(dict(item) for item in rejected),
        raw_route_digest=_raw_digest(points),
        evaluated_edge_count=evaluated_edge_count,
        maximum_edge_evaluations=maximum_edge_evaluations,
    )


def build_any_angle_candidates(
    points: Sequence[Any],
    *,
    waypoint_times: Sequence[datetime] | None = None,
    sample_spacing_m: float = 250.0,
    edge_validator: EdgeValidator | None = None,
    maximum_edge_evaluations: int = 4096,
    maximum_candidates: int = 8,
) -> tuple[AnyAngleRoute, ...]:
    """Enumerate deterministic any-angle routes.

    The direct start-to-end edge is evaluated first.  Further candidates use
    only accepted edges in the waypoint-index DAG, followed by bounded-hop
    greedy routes.  The raw adjacent route is always returned as the final
    candidate so the caller can apply its complete final gates before deciding
    to use a shortcut.
    """

    if isinstance(points, (str, bytes)):
        raise ValueError("points must be a sequence")
    raw_points = tuple(_coordinate(point) for point in points)
    if len(raw_points) < 2:
        raise ValueError("any-angle route needs at least two points")
    if not math.isfinite(sample_spacing_m) or sample_spacing_m <= 0.0:
        raise ValueError("sample_spacing_m must be positive and finite")
    if waypoint_times is not None:
        if len(waypoint_times) != len(raw_points):
            raise ValueError("waypoint_times must match points")
        times = tuple(ensure_utc(value, field="waypoint_time") for value in waypoint_times)
        if any(current <= previous for previous, current in pairwise(times)):
            raise ValueError("waypoint_times must increase strictly")
    else:
        times = None
    if maximum_edge_evaluations < 1 or maximum_candidates < 1:
        raise ValueError("any-angle resource limits must be positive")

    cache: dict[tuple[int, int], AnyAngleEdge] = {}
    rejected: list[dict[str, Any]] = []
    evaluations = 0

    def evaluate(
        start_index: int,
        end_index: int,
        *,
        build_geometry_when_limited: bool = False,
        validate_when_limited: bool = False,
    ) -> AnyAngleEdge:
        nonlocal evaluations
        key = (start_index, end_index)
        cached = cache.get(key)
        if cached is not None:
            return cached
        start = raw_points[start_index]
        end = raw_points[end_index]
        length = great_circle_distance_m(start, end)
        limited = (
            edge_validator is not None
            and evaluations >= maximum_edge_evaluations
            and not validate_when_limited
        )
        if limited and not build_geometry_when_limited:
            edge = AnyAngleEdge(
                start_index=start_index,
                end_index=end_index,
                start=start,
                end=end,
                points=(),
                sample_times=(),
                length_m=length,
                accepted=False,
                rejection_reason="edge_evaluation_resource_limit",
            )
            cache[key] = edge
            rejected.append({
                "start_index": start_index,
                "end_index": end_index,
                "reason": edge.rejection_reason,
            })
            return edge
        try:
            edge_points = _sample_great_circle(start, end, spacing_m=sample_spacing_m)
            edge_times = _edge_times(
                edge_points,
                None if times is None else times[start_index],
                None if times is None else times[end_index],
            )
        except ValueError as exc:
            edge = AnyAngleEdge(
                start_index, end_index, start, end, (), (), 0.0,
                False, str(exc), None,
            )
            cache[key] = edge
            rejected.append({"start_index": start_index, "end_index": end_index,
                             "reason": "geometry_invalid"})
            return edge
        if edge_validator is None:
            decision = AnyAngleDecision(True)
        elif limited:
            decision = AnyAngleDecision(False, "edge_evaluation_resource_limit")
        else:
            evaluations += 1
            try:
                decision = _decision(
                    edge_validator(
                        AnyAngleEdge(
                            start_index, end_index, start, end, edge_points, edge_times, length
                        )
                    )
                )
            except Exception as exc:
                decision = AnyAngleDecision(False, f"edge_validator_error:{type(exc).__name__}")
        edge = AnyAngleEdge(
            start_index=start_index,
            end_index=end_index,
            start=start,
            end=end,
            points=edge_points,
            sample_times=edge_times,
            length_m=length,
            accepted=decision.accepted,
            rejection_reason=decision.reason,
            validation_evidence=decision.evidence,
        )
        cache[key] = edge
        if not edge.accepted:
            rejected.append({
                "start_index": start_index,
                "end_index": end_index,
                "reason": edge.rejection_reason or "edge_rejected",
            })
        return edge

    # This explicit first call is part of the evidence contract even when the
    # resource budget later prevents evaluating every DAG edge.
    destination = len(raw_points) - 1
    evaluate(
        0,
        destination,
        build_geometry_when_limited=True,
        validate_when_limited=True,
    )
    # Adjacent edges are the mandatory raw fallback.  They always receive
    # geometry, but once the explicit validation budget is exhausted they are
    # recorded as resource-limited and are only available to the forced raw
    # route below.  This keeps a small budget from making raw assembly
    # impossible while ensuring no unvalidated shortcut can enter the DAG.
    for start_index in range(len(raw_points) - 1):
        end_index = start_index + 1
        if end_index <= destination:
            evaluate(start_index, end_index, build_geometry_when_limited=True)

    # Explore non-adjacent edges in a stable farthest-first row order after the
    # direct attempt and mandatory fallback edges.  Farthest-first makes a
    # bounded budget useful for finding a long safe splice around a blocked
    # direct edge.  Limited edges do not allocate sampled geometry, which
    # makes the resource limit a real bound rather than merely a bound on
    # validator calls.
    for start_index in range(len(raw_points) - 1):
        for end_index in range(destination, start_index + 1, -1):
            if start_index == 0 and end_index == destination:
                continue
            evaluate(start_index, end_index)

    accepted_edges = {
        key: edge for key, edge in cache.items() if edge.accepted
    }
    paths: list[tuple[int, ...]] = []
    direct = (0, destination)
    if direct in accepted_edges:
        paths.append(direct)

    # Keep a bounded K-shortest suffix beam in the waypoint DAG.  The edge
    # screen is exhaustive for the formal r17 route, while final qualification
    # is much more expensive than graph assembly.  Retaining several suffixes
    # is important: the shortest safe edge combination can still fail a
    # route-level ETA, curvature, risk, or trust gate after its skipped anchors
    # are projected onto the joint curve.
    suffix_limit = max(16, maximum_candidates * 4)

    def shortcut_count(path: tuple[int, ...]) -> int:
        return sum(max(0, right - left - 1) for left, right in pairwise(path))

    def path_key(value: tuple[float, tuple[int, ...]]) -> tuple[float, int, tuple[int, ...]]:
        return value[0], -shortcut_count(value[1]), value[1]

    suffixes: dict[int, list[tuple[float, tuple[int, ...]]]] = {
        destination: [(0.0, (destination,))]
    }
    for start_index in range(destination - 1, -1, -1):
        options: list[tuple[float, tuple[int, ...]]] = []
        for end_index in range(start_index + 1, destination + 1):
            edge = accepted_edges.get((start_index, end_index))
            if edge is None:
                continue
            for tail_length, tail_path in suffixes.get(end_index, ()):
                options.append(
                    (edge.length_m + tail_length, (start_index, *tail_path))
                )
        options.sort(key=path_key)
        unique_options: list[tuple[float, tuple[int, ...]]] = []
        seen: set[tuple[int, ...]] = set()
        for option in options:
            if option[1] in seen:
                continue
            seen.add(option[1])
            unique_options.append(option)
            if len(unique_options) >= suffix_limit:
                break
        suffixes[start_index] = unique_options
    paths.extend(path for _, path in suffixes.get(0, ()))

    # Greedy bounded-hop routes intentionally provide alternatives when the
    # globally shortest safe path later fails a curve/risk gate.
    for maximum_hop in range(destination, 0, -1):
        path = [0]
        while path[-1] < destination:
            start_index = path[-1]
            options = [
                end_index
                for end_index in range(
                    start_index + 1, min(destination, start_index + maximum_hop) + 1
                )
                if (start_index, end_index) in accepted_edges
            ]
            if not options:
                break
            path.append(max(options))
        if path[-1] == destination:
            paths.append(tuple(path))
        if len(paths) >= suffix_limit:
            break

    unique_paths: list[tuple[int, ...]] = []
    for path in paths:
        if path not in unique_paths:
            unique_paths.append(path)
    raw_indices = tuple(range(len(raw_points)))

    def path_length(path: tuple[int, ...]) -> float:
        return sum(
            great_circle_distance_m(raw_points[first], raw_points[second])
            for first, second in pairwise(path)
        )

    # Candidate qualification is expensive because each selected route is
    # swept through the complete ETA/risk window.  Keep the graph evaluation
    # exhaustive, but pass only a deterministic, quality-ordered prefix to
    # that final stage.  Length is primary; among equal-length paths prefer
    # the one with more genuine any-angle shortcuts, then the waypoint tuple.
    non_raw_paths = [path for path in unique_paths if path != raw_indices]
    non_raw_paths.sort(
        key=lambda path: (
            path_length(path),
            -sum(max(0, right - left - 1) for left, right in pairwise(path)),
            path,
        )
    )

    # The shortest safe suffixes can all converge on one late shortcut and
    # therefore hide a longer, but geometrically usable, approach channel.
    # Keep one shortest path for each accepted predecessor of the destination
    # in the qualification pool.  This is still a length/shortcut/index
    # ordered candidate set after the diversity union; it only prevents the
    # bounded prefix from becoming a single-terminal-edge tunnel.  The prefix
    # route itself is computed from the already screened DAG, so no unvalidated
    # edge is introduced and no extra edge evaluation is performed.
    best_prefix: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, (0,))}
    for end_index in range(1, destination):
        options: list[tuple[float, tuple[int, ...]]] = []
        for start_index in range(end_index):
            prefix = best_prefix.get(start_index)
            edge = accepted_edges.get((start_index, end_index))
            if prefix is None or edge is None:
                continue
            options.append((prefix[0] + edge.length_m, (*prefix[1], end_index)))
        if options:
            options.sort(key=path_key)
            best_prefix[end_index] = options[0]
    terminal_representatives: list[tuple[int, ...]] = []
    for predecessor in range(1, destination):
        prefix = best_prefix.get(predecessor)
        edge = accepted_edges.get((predecessor, destination))
        if prefix is None or edge is None:
            continue
        terminal_representatives.append((*prefix[1], destination))
    terminal_representatives.sort(
        key=lambda path: (
            path_length(path),
            -sum(max(0, right - left - 1) for left, right in pairwise(path)),
            path,
        )
    )

    # Preserve a deterministic family that reaches an accepted waypoint with
    # one screened shortcut and then follows the authoritative raw tail.  A
    # shortest-prefix-only representative can otherwise replace, for example,
    # ``[0, 19, 20, 21]`` with ``[0, 19, 21]`` even though the extra safe
    # channel waypoint may be what lets the final time/geometry gates qualify
    # the route.  These paths contain no new edge: the prefix shortcut and all
    # raw-tail edges have already passed the gate-1--3 edge screen above.
    channel_representatives: list[tuple[int, ...]] = []
    for end_index in range(2, destination):
        if (0, end_index) not in accepted_edges:
            continue
        path = (0, end_index, *range(end_index + 1, destination + 1))
        if all((start, end) in accepted_edges for start, end in pairwise(path)):
            channel_representatives.append(path)
    channel_representatives.sort(
        key=lambda path: (
            path_length(path),
            -sum(max(0, right - left - 1) for left, right in pairwise(path)),
            path,
        )
    )

    candidate_capacity = max(0, maximum_candidates - 1)
    if len(channel_representatives) <= candidate_capacity:
        # Channel representatives are deliberately reserved before the
        # terminal union.  The final list remains stable by the same
        # length/shortcut/index key, but a bounded pool cannot erase all
        # routes that retain a proven approach channel.
        remaining = [
            path
            for path in [*terminal_representatives, *non_raw_paths]
            if path not in channel_representatives
        ]
        selected_non_raw = [
            *channel_representatives,
            *remaining[: max(0, candidate_capacity - len(channel_representatives))],
        ]
        selected_non_raw.sort(
            key=lambda path: (
                path_length(path),
                -sum(max(0, right - left - 1) for left, right in pairwise(path)),
                path,
            )
        )
    else:
        # Very small caller budgets cannot cover every channel representative.
        # Select the shortest stable representatives in that case, retaining the
        # documented resource bound rather than silently expanding it.
        selected_non_raw = channel_representatives[:candidate_capacity]
    candidate_paths = [
        *selected_non_raw[:candidate_capacity],
        raw_indices,
    ]

    routes: list[AnyAngleRoute] = []
    for path in candidate_paths:
        force = path == raw_indices
        try:
            routes.append(
                _route_from_indices(
                    raw_points,
                    times,
                    path,
                    cache,
                    rejected,
                    evaluations,
                    maximum_edge_evaluations,
                    force=force,
                )
            )
        except ValueError:
            continue
    if not routes:
        routes.append(
            _route_from_indices(
                raw_points,
                times,
                raw_indices,
                cache,
                rejected,
                evaluations,
                maximum_edge_evaluations,
                force=True,
            )
        )
    return tuple(routes)


def build_any_angle_route(*args: Any, **kwargs: Any) -> AnyAngleRoute:
    """Return the first deterministic any-angle candidate."""

    return build_any_angle_candidates(*args, **kwargs)[0]


__all__ = [
    "EARTH_RADIUS_M",
    "AnyAngleDecision",
    "AnyAngleEdge",
    "AnyAngleRoute",
    "build_any_angle_candidates",
    "build_any_angle_route",
    "great_circle_distance_m",
    "great_circle_interpolate",
]
