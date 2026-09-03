"""Joint-window cubic B-spline assembly for formal route motion.

The older research smoother replaces isolated corners.  This module instead
qualifies a complete any-angle centreline as a route-level piecewise cubic
B-spline: overlapping turns share one trim budget and are assembled into one
ordered span sequence.  Each cubic span exposes four Bezier-equivalent control
points, and analytic first/second derivatives are used for curvature and C2
checks.  Every substantive transition between any-angle edges is assigned to
one or more joint windows; no raw C0 corner is appended to an accepted result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from arctic_route_planning.research.route_smoothing import EARTH_RADIUS_M
from arctic_route_planning.research.route_smoothing_multispan import (
    build_local_corner_curve,
)

from .any_angle import AnyAngleRoute

Coordinate = tuple[float, float]


@dataclass(frozen=True, slots=True)
class JointWindow:
    """A compatible group of neighbouring turns smoothed together."""

    start_node_index: int
    end_node_index: int
    turn_node_indices: tuple[int, ...]
    trim_by_turn_m: tuple[float, ...]
    overlap_constraints_pass: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_node_index": self.start_node_index,
            "end_node_index": self.end_node_index,
            "turn_node_indices": list(self.turn_node_indices),
            "trim_by_turn_m": list(self.trim_by_turn_m),
            "overlap_constraints_pass": self.overlap_constraints_pass,
        }


@dataclass(frozen=True, slots=True)
class JointBSplineResult:
    """Sampled joint B-spline and analytic qualification evidence."""

    status: str
    points: tuple[Coordinate, ...]
    curvatures_m_inv: tuple[float, ...]
    span_control_points_m: tuple[tuple[Coordinate, ...], ...]
    span_node_indices: tuple[int, ...]
    node_output_indices: tuple[int, ...]
    joint_windows: tuple[JointWindow, ...]
    substantive_turn_node_indices: tuple[int, ...]
    c2_pass: bool
    no_reverse_curvature_pass: bool
    no_self_intersection_pass: bool
    monotonic_pass: bool
    full_route_g2_pass: bool
    minimum_radius_m: float
    route_length_m: float
    maximum_deviation_to_base_m: float
    raw_route_digest: str
    fallback_reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.status == "ACCEPTED"

    @property
    def span_convex_hulls_m(self) -> tuple[tuple[Coordinate, ...], ...]:
        return self.span_control_points_m

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "c.route-motion-joint-bspline.v1",
            "status": self.status,
            "applied": self.applied,
            "fallback_reason": self.fallback_reason,
            "raw_route_digest": self.raw_route_digest,
            "span_count": len(self.span_control_points_m),
            "degree": 3,
            "points": [list(point) for point in self.points],
            "curvatures_m_inv": list(self.curvatures_m_inv),
            "span_control_points_m": [
                [list(point) for point in span] for span in self.span_control_points_m
            ],
            "span_node_indices": list(self.span_node_indices),
            "node_output_indices": list(self.node_output_indices),
            "joint_windows": [window.to_dict() for window in self.joint_windows],
            "substantive_turn_node_indices": list(self.substantive_turn_node_indices),
            "c2_pass": self.c2_pass,
            "no_reverse_curvature_pass": self.no_reverse_curvature_pass,
            "no_self_intersection_pass": self.no_self_intersection_pass,
            "monotonic_pass": self.monotonic_pass,
            "full_route_g2_pass": self.full_route_g2_pass,
            "minimum_radius_m": (
                self.minimum_radius_m
                if math.isfinite(self.minimum_radius_m)
                else None
            ),
            "route_length_m": self.route_length_m,
            "maximum_deviation_to_base_m": self.maximum_deviation_to_base_m,
        }


@dataclass(frozen=True, slots=True)
class _Frame:
    lon0: float
    lat0_rad: float
    cos_lat0: float

    def to_local(self, point: Coordinate) -> Coordinate:
        delta_lon = (math.radians(point[0] - self.lon0) + math.pi) % (2.0 * math.pi) - math.pi
        return (
            EARTH_RADIUS_M * delta_lon * self.cos_lat0,
            EARTH_RADIUS_M * (math.radians(point[1]) - self.lat0_rad),
        )

    def to_geo(self, point: Coordinate) -> Coordinate:
        longitude = self.lon0 + math.degrees(point[0] / (EARTH_RADIUS_M * self.cos_lat0))
        latitude = math.degrees(self.lat0_rad + point[1] / EARTH_RADIUS_M)
        return (((longitude + 180.0) % 360.0) - 180.0, latitude)


@dataclass(frozen=True, slots=True)
class _Polynomial:
    a: Coordinate
    b: Coordinate
    c: Coordinate
    d: Coordinate
    width_m: float

    def point(self, offset_m: float) -> Coordinate:
        return _add(
            self.a,
            _add(
                _mul(self.b, offset_m),
                _add(_mul(self.c, offset_m**2), _mul(self.d, offset_m**3)),
            ),
        )

    def first(self, offset_m: float) -> Coordinate:
        return _add(self.b, _add(_mul(self.c, 2.0 * offset_m), _mul(self.d, 3.0 * offset_m**2)))

    def second(self, offset_m: float) -> Coordinate:
        return (
            2.0 * self.c[0] + 6.0 * self.d[0] * offset_m,
            2.0 * self.c[1] + 6.0 * self.d[1] * offset_m,
        )


def _add(first: Coordinate, second: Coordinate) -> Coordinate:
    return first[0] + second[0], first[1] + second[1]


def _sub(first: Coordinate, second: Coordinate) -> Coordinate:
    return first[0] - second[0], first[1] - second[1]


def _mul(first: Coordinate, scalar: float) -> Coordinate:
    return first[0] * scalar, first[1] * scalar


def _norm(value: Coordinate) -> float:
    return math.hypot(value[0], value[1])


def _cross(first: Coordinate, second: Coordinate) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _distance_to_segment(point: Coordinate, start: Coordinate, end: Coordinate) -> float:
    vector = _sub(end, start)
    denominator = vector[0] ** 2 + vector[1] ** 2
    if denominator <= 1.0e-18:
        return _norm(_sub(point, start))
    fraction = max(0.0, min(1.0, (
        (point[0] - start[0]) * vector[0] + (point[1] - start[1]) * vector[1]
    ) / denominator))
    return _norm(_sub(point, _add(start, _mul(vector, fraction))))


def _distance_to_polyline(point: Coordinate, polyline: Sequence[Coordinate]) -> float:
    return min(
        _distance_to_segment(point, start, end) for start, end in pairwise(polyline)
    )


def _unit(value: Coordinate) -> Coordinate:
    length = _norm(value)
    if length <= 1.0e-9 or not math.isfinite(length):
        raise ValueError("joint spline has a zero-length tangent")
    return value[0] / length, value[1] / length


def _natural_coefficients(
    values: Sequence[float], distances: Sequence[float]
) -> tuple[tuple[float, float, float, float], ...]:
    """Return natural cubic coefficients in distance parameterisation."""

    count = len(values)
    if count < 2:
        raise ValueError("joint spline needs at least two nodes")
    widths = tuple(right - left for left, right in pairwise(distances))
    if any(width <= 1.0e-6 or not math.isfinite(width) for width in widths):
        raise ValueError("joint spline nodes must have strictly increasing distance")
    if count == 2:
        width = widths[0]
        return ((values[0], (values[1] - values[0]) / width, 0.0, 0.0),)

    alpha = [0.0] * count
    for index in range(1, count - 1):
        alpha[index] = (
            3.0 / widths[index] * (values[index + 1] - values[index])
            - 3.0 / widths[index - 1] * (values[index] - values[index - 1])
        )
    lower = [1.0] + [0.0] * (count - 1)
    diagonal = [0.0] * count
    upper = [0.0] * count
    rhs = [0.0] * count
    diagonal[0] = 1.0
    for index in range(1, count - 1):
        lower[index] = widths[index - 1]
        diagonal[index] = 2.0 * (widths[index - 1] + widths[index])
        upper[index] = widths[index]
        rhs[index] = alpha[index]
    diagonal[-1] = 1.0
    c = [0.0] * count
    b = [0.0] * (count - 1)
    d = [0.0] * (count - 1)
    for index in range(1, count):
        factor = lower[index] / diagonal[index - 1]
        diagonal[index] -= factor * upper[index - 1]
        rhs[index] -= factor * rhs[index - 1]
    c[-1] = rhs[-1] / diagonal[-1]
    for index in range(count - 2, -1, -1):
        c[index] = (rhs[index] - upper[index] * c[index + 1]) / diagonal[index]
    for index, width in enumerate(widths):
        b[index] = (
            (values[index + 1] - values[index]) / width
            - width * (c[index + 1] + 2.0 * c[index]) / 3.0
        )
        d[index] = (c[index + 1] - c[index]) / (3.0 * width)
    return tuple(
        (values[index], b[index], c[index], d[index]) for index in range(count - 1)
    )


def _polynomials(
    nodes: Sequence[Coordinate], distances: Sequence[float]
) -> tuple[_Polynomial, ...]:
    x = _natural_coefficients(tuple(point[0] for point in nodes), distances)
    y = _natural_coefficients(tuple(point[1] for point in nodes), distances)
    return tuple(
        _Polynomial(
            a=(x[index][0], y[index][0]),
            b=(x[index][1], y[index][1]),
            c=(x[index][2], y[index][2]),
            d=(x[index][3], y[index][3]),
            width_m=distances[index + 1] - distances[index],
        )
        for index in range(len(nodes) - 1)
    )


def _clamped_first_derivatives(
    values: Sequence[float], distances: Sequence[float]
) -> tuple[float, ...]:
    """Solve the C2 cubic-spline first derivatives with clamped ends.

    The endpoint derivatives use the adjacent chord direction.  Internal
    derivatives are solved from the exact C2 continuity equations.  Unlike a
    nearest-sample projection, the resulting spline interpolates every
    selected waypoint exactly, which keeps the authoritative ETA anchors at
    their original coordinates.
    """

    count = len(values)
    if count < 2 or len(distances) != count:
        raise ValueError("clamped spline values and distances must match")
    widths = tuple(right - left for left, right in pairwise(distances))
    if any(width <= 1.0e-6 or not math.isfinite(width) for width in widths):
        raise ValueError("clamped spline nodes must have strictly increasing distance")
    slopes = tuple(
        (right - left) / width
        for left, right, width in zip(
            values[:-1], values[1:], widths, strict=True
        )
    )
    if count == 2:
        return slopes[0], slopes[0]

    internal_count = count - 2
    lower = [0.0] * internal_count
    diagonal = [0.0] * internal_count
    upper = [0.0] * internal_count
    rhs = [0.0] * internal_count
    for position, node_index in enumerate(range(1, count - 1)):
        left_width = widths[node_index - 1]
        right_width = widths[node_index]
        # For node i the C2 equation is
        #   h_i m_{i-1} + 2(h_{i-1}+h_i)m_i
        #       + h_{i-1}m_{i+1} = 3(h_i d_{i-1}+h_{i-1}d_i).
        lower[position] = right_width
        diagonal[position] = 2.0 * (left_width + right_width)
        upper[position] = left_width
        rhs[position] = 3.0 * (
            right_width * slopes[node_index - 1]
            + left_width * slopes[node_index]
        )
    rhs[0] -= lower[0] * slopes[0]
    rhs[-1] -= upper[-1] * slopes[-1]
    for index in range(1, internal_count):
        factor = lower[index] / diagonal[index - 1]
        diagonal[index] -= factor * upper[index - 1]
        rhs[index] -= factor * rhs[index - 1]
    internal = [0.0] * internal_count
    internal[-1] = rhs[-1] / diagonal[-1]
    for index in range(internal_count - 2, -1, -1):
        internal[index] = (
            rhs[index] - upper[index] * internal[index + 1]
        ) / diagonal[index]
    return (slopes[0], *internal, slopes[-1])


def _clamped_coefficients(
    values: Sequence[float], distances: Sequence[float]
) -> tuple[tuple[float, float, float, float], ...]:
    derivatives = _clamped_first_derivatives(values, distances)
    coefficients = []
    for index, width in enumerate(
        right - left for left, right in pairwise(distances)
    ):
        delta = values[index + 1] - values[index]
        start_derivative = derivatives[index]
        end_derivative = derivatives[index + 1]
        coefficients.append(
            (
                values[index],
                start_derivative,
                3.0 * delta / width**2
                - (2.0 * start_derivative + end_derivative) / width,
                -2.0 * delta / width**3
                + (start_derivative + end_derivative) / width**2,
            )
        )
    return tuple(coefficients)


def _clamped_polynomials(
    nodes: Sequence[Coordinate], distances: Sequence[float]
) -> tuple[_Polynomial, ...]:
    x = _clamped_coefficients(tuple(point[0] for point in nodes), distances)
    y = _clamped_coefficients(tuple(point[1] for point in nodes), distances)
    return tuple(
        _Polynomial(
            a=(x[index][0], y[index][0]),
            b=(x[index][1], y[index][1]),
            c=(x[index][2], y[index][2]),
            d=(x[index][3], y[index][3]),
            width_m=distances[index + 1] - distances[index],
        )
        for index in range(len(nodes) - 1)
    )


def _bezier_controls(polynomial: _Polynomial) -> tuple[Coordinate, ...]:
    """Return the true cubic Bezier controls for the polynomial span."""

    width = polynomial.width_m
    first = polynomial.point(0.0)
    second = _add(first, _mul(polynomial.b, width / 3.0))
    third = _add(
        first,
        _add(
            _mul(polynomial.b, 2.0 * width / 3.0),
            _mul(polynomial.c, width**2 / 3.0),
        ),
    )
    return (
        first,
        second,
        third,
        polynomial.point(width),
    )


def _node_turns(nodes: Sequence[Coordinate], boundary_indices: Sequence[int]) -> tuple[int, ...]:
    turns: list[int] = []
    for node_index in boundary_indices[1:-1]:
        incoming = _sub(nodes[node_index], nodes[node_index - 1])
        outgoing = _sub(nodes[node_index + 1], nodes[node_index])
        if _norm(incoming) <= 1.0e-6 or _norm(outgoing) <= 1.0e-6:
            continue
        angle = math.degrees(math.acos(max(-1.0, min(1.0,
            (incoming[0] * outgoing[0] + incoming[1] * outgoing[1])
            / (_norm(incoming) * _norm(outgoing))
        ))))
        if angle > 1.0:
            turns.append(node_index)
    return tuple(turns)


def _joint_windows(
    nodes: Sequence[Coordinate],
    distances: Sequence[float],
    boundary_indices: Sequence[int],
    turns: Sequence[int],
    *,
    max_trim_fraction: float,
    maximum_overlap_fraction: float,
) -> tuple[JointWindow, ...]:
    if not turns:
        return ()
    trim_by_node: dict[int, float] = {}
    for node_index in turns:
        boundary_position = boundary_indices.index(node_index)
        left_length = distances[node_index] - distances[boundary_indices[boundary_position - 1]]
        right_length = distances[boundary_indices[boundary_position + 1]] - distances[node_index]
        trim_by_node[node_index] = max_trim_fraction * min(left_length, right_length)

    # Keep the historical strict per-corner < 0.5 rule while enforcing the
    # joint shared-leg <= 0.90 rule by reducing the conceptual trim window,
    # never by increasing a local trim beyond the policy.
    for first, second in pairwise(turns):
        shared = distances[second] - distances[first]
        allowed = maximum_overlap_fraction * shared
        total = trim_by_node[first] + trim_by_node[second]
        if total > allowed and total > 0.0:
            factor = allowed / total
            trim_by_node[first] *= factor
            trim_by_node[second] *= factor

    windows: list[JointWindow] = []
    current_turns: list[int] = []
    current_start = 0
    current_end = 0
    for turn in turns:
        trim = trim_by_node[turn]
        start_distance = distances[turn] - trim
        end_distance = distances[turn] + trim
        start_node = max(
            0,
            next(
                index for index, value in enumerate(distances)
                if value >= start_distance
            ) - 1,
        )
        end_node = min(
            len(nodes) - 1,
            next(
                (index for index, value in enumerate(distances) if value > end_distance),
                len(nodes),
            ) - 1,
        )
        if current_turns and start_node > current_end:
            windows.append(
                JointWindow(
                    current_start,
                    current_end,
                    tuple(current_turns),
                    tuple(trim_by_node[index] for index in current_turns),
                    _window_overlap_pass(current_turns, trim_by_node, distances,
                                         maximum_overlap_fraction),
                )
            )
            current_turns = []
        if not current_turns:
            current_start = start_node
        current_turns.append(turn)
        current_end = max(current_end, end_node)
    if current_turns:
        windows.append(
            JointWindow(
                current_start,
                current_end,
                tuple(current_turns),
                tuple(trim_by_node[index] for index in current_turns),
                _window_overlap_pass(current_turns, trim_by_node, distances,
                                     maximum_overlap_fraction),
            )
        )
    return tuple(windows)


def _window_overlap_pass(
    turns: Sequence[int], trims: Mapping[int, float], distances: Sequence[float], fraction: float
) -> bool:
    for first, second in pairwise(turns):
        if (
            trims[first] + trims[second]
            > fraction * (distances[second] - distances[first]) + 1.0e-6
        ):
            return False
    return True

def _intersection(
    first: Coordinate, second: Coordinate, third: Coordinate, fourth: Coordinate
) -> bool:
    def signed(value: float) -> float:
        return 0.0 if abs(value) <= 1.0e-7 else value

    def orientation(a: Coordinate, b: Coordinate, c: Coordinate) -> float:
        return signed(_cross(_sub(b, a), _sub(c, a)))

    def on_segment(a: Coordinate, b: Coordinate, c: Coordinate) -> bool:
        return (
            min(a[0], b[0]) - 1.0e-7 <= c[0] <= max(a[0], b[0]) + 1.0e-7
            and min(a[1], b[1]) - 1.0e-7 <= c[1] <= max(a[1], b[1]) + 1.0e-7
        )

    first_orientation = orientation(first, second, third)
    second_orientation = orientation(first, second, fourth)
    third_orientation = orientation(third, fourth, first)
    fourth_orientation = orientation(third, fourth, second)
    if first_orientation == 0.0 and on_segment(first, second, third):
        return True
    if second_orientation == 0.0 and on_segment(first, second, fourth):
        return True
    if third_orientation == 0.0 and on_segment(third, fourth, first):
        return True
    if fourth_orientation == 0.0 and on_segment(third, fourth, second):
        return True
    return (first_orientation > 0.0) != (second_orientation > 0.0) and (
        third_orientation > 0.0
    ) != (fourth_orientation > 0.0)


def _self_intersection_pass(points: Sequence[Coordinate]) -> bool:
    segment_count = len(points) - 1
    if segment_count < 2:
        return True

    # A quadratic all-pairs check is needlessly expensive for a 900 km route
    # sampled at 250 m.  A deterministic uniform spatial index keeps the
    # exact segment intersection predicate while only comparing segments whose
    # bounding boxes share a bucket.  The bucket-size cap prevents a single
    # long segment from allocating an unbounded number of cells.
    minimum_x = min(point[0] for point in points)
    maximum_x = max(point[0] for point in points)
    minimum_y = min(point[1] for point in points)
    maximum_y = max(point[1] for point in points)
    diagonal = math.hypot(maximum_x - minimum_x, maximum_y - minimum_y)
    cell_size = max(1.0, diagonal / math.sqrt(max(1, segment_count)))
    bucket_limit = 20_000
    while True:
        span_x = max(1, math.floor((maximum_x - minimum_x) / cell_size) + 1)
        span_y = max(1, math.floor((maximum_y - minimum_y) / cell_size) + 1)
        if span_x * span_y <= bucket_limit:
            break
        cell_size *= math.sqrt(span_x * span_y / bucket_limit)

    buckets: dict[tuple[int, int], list[int]] = {}

    def bucket_range(first: Coordinate, second: Coordinate) -> tuple[range, range]:
        lower_x = math.floor(min(first[0], second[0]) / cell_size)
        upper_x = math.floor(max(first[0], second[0]) / cell_size)
        lower_y = math.floor(min(first[1], second[1]) / cell_size)
        upper_y = math.floor(max(first[1], second[1]) / cell_size)
        return range(lower_x, upper_x + 1), range(lower_y, upper_y + 1)

    for index, (first, second) in enumerate(pairwise(points)):
        x_range, y_range = bucket_range(first, second)
        for x_index in x_range:
            for y_index in y_range:
                buckets.setdefault((x_index, y_index), []).append(index)

    for first_index, (first, second) in enumerate(pairwise(points)):
        x_range, y_range = bucket_range(first, second)
        candidates = {
            second_index
            for x_index in x_range
            for y_index in y_range
            for second_index in buckets.get((x_index, y_index), ())
            if second_index > first_index + 1
        }
        first_min_x = min(first[0], second[0])
        first_max_x = max(first[0], second[0])
        first_min_y = min(first[1], second[1])
        first_max_y = max(first[1], second[1])
        for second_index in candidates:
            third = points[second_index]
            fourth = points[second_index + 1]
            if (
                max(first_min_x, min(third[0], fourth[0]))
                > min(first_max_x, max(third[0], fourth[0])) + 1.0e-7
                or max(first_min_y, min(third[1], fourth[1]))
                > min(first_max_y, max(third[1], fourth[1])) + 1.0e-7
            ):
                continue
            if _intersection(first, second, third, fourth):
                return False
    return True


def _fallback(route: AnyAngleRoute, reason: str) -> JointBSplineResult:
    return JointBSplineResult(
        status="FALLBACK",
        points=route.points,
        curvatures_m_inv=tuple(0.0 for _ in route.points),
        span_control_points_m=(),
        span_node_indices=(),
        node_output_indices=(),
        joint_windows=(),
        substantive_turn_node_indices=(),
        c2_pass=False,
        no_reverse_curvature_pass=False,
        no_self_intersection_pass=False,
        monotonic_pass=False,
        full_route_g2_pass=False,
        minimum_radius_m=0.0,
        route_length_m=route.length_m,
        maximum_deviation_to_base_m=0.0,
        raw_route_digest=route.raw_route_digest,
        fallback_reason=reason,
    )


def _line_samples(
    first: Coordinate, second: Coordinate, spacing_m: float
) -> tuple[Coordinate, ...]:
    distance = _norm(_sub(second, first))
    count = max(1, math.ceil(distance / spacing_m))
    return tuple(
        _add(first, _mul(_sub(second, first), index / count))
        for index in range(count + 1)
    )


def _line_controls(first: Coordinate, second: Coordinate) -> tuple[Coordinate, ...]:
    """Return the four Bezier controls of a straight cubic span."""

    delta = _sub(second, first)
    return (
        first,
        _add(first, _mul(delta, 1.0 / 3.0)),
        _add(first, _mul(delta, 2.0 / 3.0)),
        second,
    )


def _build_non_overlapping_local_windows(
    route: AnyAngleRoute,
    local_nodes: Sequence[Coordinate],
    distances: Sequence[float],
    boundary_indices: Sequence[int],
    turns: Sequence[int],
    windows: Sequence[JointWindow],
    *,
    sample_spacing_m: float,
    max_trim_fraction: float,
    maximum_route_points: int,
    interpolate_selected_vertices: bool,
) -> JointBSplineResult | None:
    """Assemble route-level G2 pieces for the computed joint windows.

    Each window is assembled at route level: raw centreline pieces are
    replaced by turn-safe multi-span cubic pieces and exact tangent lines, so
    no raw C0 corner is appended. The shared-leg trim budget has already been
    reduced by ``_joint_windows``; a grouped window therefore has a real
    non-overlapping line span between its local corner pieces.
    """

    frame = _Frame(
        lon0=route.points[0][0],
        lat0_rad=math.radians(route.points[0][1]),
        cos_lat0=math.cos(math.radians(route.points[0][1])),
    )
    curves: dict[int, Any] = {}
    trim_by_turn = {
        turn: trim
        for window in windows
        for turn, trim in zip(
            window.turn_node_indices, window.trim_by_turn_m, strict=True
        )
    }
    for turn in turns:
        position = boundary_indices.index(turn)
        previous_boundary = boundary_indices[position - 1]
        next_boundary = boundary_indices[position + 1]
        trim = trim_by_turn.get(turn)
        if trim is None:
            left_length = distances[turn] - distances[previous_boundary]
            right_length = distances[next_boundary] - distances[turn]
            trim = max_trim_fraction * min(left_length, right_length)
        if trim <= 1.0e-6:
            return None
        try:
            curve = build_local_corner_curve(
                local_nodes[previous_boundary],
                local_nodes[turn],
                local_nodes[next_boundary],
                trim_m=trim,
                sample_count=max(17, math.ceil(2.0 * trim / sample_spacing_m) + 1),
                turn_direction_safe=True,
                interpolate_vertex=interpolate_selected_vertices,
            )
        except ValueError:
            return None
        curves[turn] = curve

    assembled: list[Coordinate] = [local_nodes[0]]
    curvatures: list[float] = [0.0]
    controls: list[tuple[Coordinate, ...]] = []
    span_tangents: list[tuple[Coordinate, Coordinate]] = []
    span_endpoint_curvature_zero: list[bool] = []

    def append_line(start: Coordinate, end: Coordinate) -> bool:
        if _norm(_sub(end, start)) <= 1.0e-6:
            return False
        line = _line_samples(start, end, sample_spacing_m)
        assembled.extend(line[1:])
        curvatures.extend(0.0 for _ in line[1:])
        line_controls = _line_controls(start, end)
        controls.append(line_controls)
        tangent = _sub(end, start)
        span_tangents.append((tangent, tangent))
        span_endpoint_curvature_zero.append(True)
        return True

    def append_curve(curve: Any) -> None:
        assembled.extend(curve.samples[1:])
        curvatures.extend(curve.curvatures_m_inv[1:])
        controls.append(curve.control_points)
        span_tangents.append((curve.first_derivatives[0], curve.first_derivatives[-1]))
        span_endpoint_curvature_zero.append(curve.evidence.endpoint_curvature_zero_pass)

    if not turns:
        for point in local_nodes[1:]:
            if not append_line(assembled[-1], point):
                return None
    else:
        # A turn's curve occupies the neighbourhood of its boundary vertex.
        # Preserve every raw line span before, between, and after the joint
        # curves.  Adjacent turns share one line span; non-adjacent turns keep
        # all intermediate waypoint segments instead of creating an untested
        # chord that skips the authoritative centreline.
        for turn_position, turn in enumerate(turns):
            if turn_position == 0:
                for node_index in range(1, turn):
                    if not append_line(assembled[-1], local_nodes[node_index]):
                        return None
            else:
                previous_turn = turns[turn_position - 1]
                if turn > previous_turn + 1:
                    if not append_line(
                        assembled[-1], local_nodes[previous_turn + 1]
                    ):
                        return None
                    for node_index in range(previous_turn + 2, turn):
                        if not append_line(assembled[-1], local_nodes[node_index]):
                            return None
            turn_curve = curves[turn]
            if not append_line(assembled[-1], turn_curve.samples[0]):
                return None
            append_curve(turn_curve)
        last_turn = turns[-1]
        if not append_line(assembled[-1], local_nodes[last_turn + 1]):
            return None
        for node_index in range(last_turn + 2, len(local_nodes)):
            if not append_line(assembled[-1], local_nodes[node_index]):
                return None
    if len(assembled) > maximum_route_points:
        return None
    if any(_norm(_sub(right, left)) <= 1.0e-6 for left, right in pairwise(assembled)):
        return None
    monotonic = True
    # The local constructor proves zero normal curvature and endpoint tangent
    # direction.  The connecting pieces are straight raw-segment spans.  A
    # geometric C2 join therefore reduces to checking that each line uses
    # the same positively oriented tangent as its neighbouring corner piece;
    # tangential parameter-speed changes do not change the arc-length curve.
    def same_direction(first: Coordinate, second: Coordinate) -> bool:
        first_norm = _norm(first)
        second_norm = _norm(second)
        if first_norm <= 1.0e-9 or second_norm <= 1.0e-9:
            return False
        first_unit = _mul(first, 1.0 / first_norm)
        second_unit = _mul(second, 1.0 / second_norm)
        return (
            _cross(first_unit, second_unit) <= 1.0e-8
            and _cross(first_unit, second_unit) >= -1.0e-8
            and first_unit[0] * second_unit[0] + first_unit[1] * second_unit[1]
            >= 1.0 - 1.0e-8
        )

    c2_pass = all(span_endpoint_curvature_zero) and all(
        curve.evidence.endpoint_g2_pass and curve.evidence.internal_knot_c2_pass
        for curve in curves.values()
    )
    if c2_pass:
        for (_, outgoing), (incoming, _) in pairwise(span_tangents):
            if not same_direction(outgoing, incoming):
                c2_pass = False
                break
    no_self_intersection = _self_intersection_pass(assembled)
    finite_radii = [
        1.0 / value for value in curvatures if value > 1.0e-12 and math.isfinite(value)
    ]
    raw_nodes = tuple(local_nodes)
    maximum_deviation = max(_distance_to_polyline(point, raw_nodes) for point in assembled)
    geo_points = list(frame.to_geo(point) for point in assembled)
    geo_points[0] = route.points[0]
    geo_points[-1] = route.points[-1]
    return JointBSplineResult(
        status="ACCEPTED" if c2_pass and no_self_intersection and monotonic else "FALLBACK",
        points=tuple(geo_points),
        curvatures_m_inv=tuple(curvatures),
        span_control_points_m=tuple(controls),
        span_node_indices=tuple(range(len(controls))),
        node_output_indices=(),
        joint_windows=tuple(windows),
        substantive_turn_node_indices=tuple(turns),
        c2_pass=c2_pass,
        no_reverse_curvature_pass=True,
        no_self_intersection_pass=no_self_intersection,
        monotonic_pass=monotonic,
        full_route_g2_pass=c2_pass and no_self_intersection and monotonic,
        minimum_radius_m=min(finite_radii) if finite_radii else math.inf,
        route_length_m=sum(
            _norm(_sub(right, left)) for left, right in pairwise(assembled)
        ),
        maximum_deviation_to_base_m=maximum_deviation,
        raw_route_digest=route.raw_route_digest,
        fallback_reason=(
            None
            if c2_pass and no_self_intersection and monotonic
            else "local_joint_assembly_gate"
        ),
    )


def build_joint_bspline(
    route: AnyAngleRoute,
    *,
    sample_spacing_m: float = 250.0,
    max_trim_fraction: float = 0.49,
    maximum_overlap_fraction: float = 0.90,
    maximum_route_points: int = 10_000,
    interpolate_selected_vertices: bool = False,
    preserve_waypoint_anchors: bool = False,
) -> JointBSplineResult:
    """Build and analytically validate one route-level joint cubic spline."""

    if not route.accepted:
        return _fallback(route, "any_angle_route_not_accepted")
    if not 0.0 < max_trim_fraction < 0.5:
        raise ValueError("max_trim_fraction must remain strictly below 0.5")
    if not 0.0 < maximum_overlap_fraction <= 1.0:
        raise ValueError("maximum_overlap_fraction must be in (0, 1]")
    if not math.isfinite(sample_spacing_m) or sample_spacing_m <= 0.0:
        raise ValueError("sample_spacing_m must be positive and finite")
    if not isinstance(interpolate_selected_vertices, bool):
        raise ValueError("interpolate_selected_vertices must be boolean")
    if not isinstance(preserve_waypoint_anchors, bool):
        raise ValueError("preserve_waypoint_anchors must be boolean")
    # Any-angle edge samples are a validation lattice, not additional
    # geometric anchors.  Fit the joint spline over the selected original
    # waypoint endpoints only; skipped waypoints therefore remain available
    # to the producer's ETA/anchor proof without being written into curve
    # geometry.
    try:
        base_points = tuple(route.raw_points[index] for index in route.waypoint_indices)
    except (IndexError, TypeError):
        return _fallback(route, "invalid_any_angle_waypoint_indices")
    if len(base_points) < 2:
        return _fallback(route, "insufficient_any_angle_points")

    frame = _Frame(
        lon0=base_points[0][0],
        lat0_rad=math.radians(base_points[0][1]),
        cos_lat0=math.cos(math.radians(base_points[0][1])),
    )
    if abs(frame.cos_lat0) <= 1.0e-6:
        return _fallback(route, "invalid_joint_local_frame")
    local_nodes = tuple(frame.to_local(point) for point in base_points)
    distances = [0.0]
    for first, second in pairwise(local_nodes):
        length = _norm(_sub(second, first))
        if length <= 1.0e-6 or not math.isfinite(length):
            return _fallback(route, "duplicate_any_angle_point")
        distances.append(distances[-1] + length)
    distances_tuple = tuple(distances)
    polynomials = (
        _clamped_polynomials(local_nodes, distances_tuple)
        if preserve_waypoint_anchors
        else _polynomials(local_nodes, distances_tuple)
    )
    controls = tuple(_bezier_controls(polynomial) for polynomial in polynomials)
    boundaries = tuple(range(len(local_nodes)))
    if any(index <= previous for previous, index in pairwise(boundaries)):
        return _fallback(route, "non_monotonic_any_angle_boundaries")
    turns = _node_turns(local_nodes, boundaries)
    # Construct the complete route from a single ordered set of G2 spans.
    # Some highly asymmetric turns have a valid geometric window only below
    # the requested local trim (the fixed four-span kernel has a finite
    # numerical/turn-direction domain).  Back off deterministically from the
    # largest requested trim; never increase a corner beyond the strict
    # ``< 0.5`` rule and never accept a partially smoothed route.
    trim_attempts = [max_trim_fraction]
    for fraction in (0.45, 0.40, 0.35, 0.30, 0.25, 0.20, 0.15, 0.10, 0.05):
        if fraction < trim_attempts[-1] - 1.0e-12:
            trim_attempts.append(fraction)
    for attempted_fraction in trim_attempts:
        windows = _joint_windows(
            local_nodes,
            distances_tuple,
            boundaries,
            turns,
            max_trim_fraction=attempted_fraction,
            maximum_overlap_fraction=maximum_overlap_fraction,
        )
        if len({turn for window in windows for turn in window.turn_node_indices}) != len(turns):
            continue
        if any(not window.overlap_constraints_pass for window in windows):
            continue
        if turns and not preserve_waypoint_anchors:
            local_result = _build_non_overlapping_local_windows(
                route,
                local_nodes,
                distances_tuple,
                boundaries,
                turns,
                windows,
                sample_spacing_m=sample_spacing_m,
                max_trim_fraction=attempted_fraction,
                maximum_route_points=maximum_route_points,
                interpolate_selected_vertices=interpolate_selected_vertices,
            )
            if local_result is not None and local_result.applied:
                return local_result
        else:
            break

    # If no local window could be assembled, the natural interpolating fit
    # below is still checked independently.  It is not allowed to hide an
    # un-smoothed C0 turn or to claim a joint result when its gates fail.
    windows = _joint_windows(
        local_nodes,
        distances_tuple,
        boundaries,
        turns,
        max_trim_fraction=max_trim_fraction,
        maximum_overlap_fraction=maximum_overlap_fraction,
    )

    sampled_local: list[Coordinate] = []
    sampled_curvatures: list[float] = []
    node_output_indices: list[int] = []
    sample_index = 0
    for span_index, polynomial in enumerate(polynomials):
        count = max(1, math.ceil(polynomial.width_m / sample_spacing_m))
        offsets = tuple(polynomial.width_m * index / count for index in range(count + 1))
        if span_index == 0:
            node_output_indices.append(sample_index)
        for offset_index, offset in enumerate(offsets):
            if span_index > 0 and offset_index == 0:
                continue
            point = polynomial.point(offset)
            first = polynomial.first(offset)
            second = polynomial.second(offset)
            speed = _norm(first)
            if speed <= 1.0e-9 or not math.isfinite(speed):
                return _fallback(route, "joint_spline_zero_tangent")
            curvature = abs(_cross(first, second)) / speed**3
            if not math.isfinite(curvature):
                return _fallback(route, "joint_spline_non_finite_curvature")
            sampled_local.append(point)
            sampled_curvatures.append(curvature)
            sample_index += 1
        node_output_indices.append(sample_index - 1)
    if len(sampled_local) > maximum_route_points:
        return _fallback(route, "joint_route_point_limit")
    sampled_geo = list(frame.to_geo(point) for point in sampled_local)
    sampled_geo[0] = base_points[0]
    sampled_geo[-1] = base_points[-1]
    sampled_local[0] = local_nodes[0]
    sampled_local[-1] = local_nodes[-1]

    c2_pass = True
    c2_tolerance = 1.0e-6
    for left, right in pairwise(polynomials):
        left_width = left.width_m
        left_first = left.first(left_width)
        right_first = right.first(0.0)
        left_second = left.second(left_width)
        right_second = right.second(0.0)
        scale_first = max(1.0, _norm(left_first), _norm(right_first))
        scale_second = max(1.0, _norm(left_second), _norm(right_second))
        if (
            _norm(_sub(left_first, right_first)) > c2_tolerance * scale_first
            or _norm(_sub(left_second, right_second)) > c2_tolerance * scale_second
        ):
            c2_pass = False
            break

    no_reverse = True
    for window in windows:
        for turn in window.turn_node_indices:
            incoming = _sub(local_nodes[turn], local_nodes[turn - 1])
            outgoing = _sub(local_nodes[turn + 1], local_nodes[turn])
            expected = _cross(incoming, outgoing)
            if abs(expected) <= 1.0e-9:
                continue
            expected_sign = 1.0 if expected > 0.0 else -1.0
            crosses = []
            for span_index in range(max(0, turn - 1), min(len(polynomials), turn + 1) + 1):
                if span_index >= len(polynomials):
                    continue
                polynomial = polynomials[span_index]
                count = max(1, math.ceil(polynomial.width_m / sample_spacing_m))
                for sample_offset in range(count + 1):
                    offset = polynomial.width_m * sample_offset / count
                    crosses.append(_cross(polynomial.first(offset), polynomial.second(offset)))
            scale = max((abs(value) for value in crosses), default=0.0)
            threshold = max(1.0e-12, scale * 1.0e-8)
            if any(expected_sign * value < -threshold for value in crosses):
                no_reverse = False
                break
        if not no_reverse:
            break

    monotonic_pass = all(
        _norm(_sub(second, first)) > 1.0e-6
        for first, second in pairwise(sampled_local)
    )
    no_self_intersection = _self_intersection_pass(sampled_local)
    full_route_g2 = c2_pass and no_reverse and monotonic_pass and no_self_intersection
    if not full_route_g2:
        local_result = None if preserve_waypoint_anchors else _build_non_overlapping_local_windows(
            route,
            local_nodes,
            distances_tuple,
            boundaries,
            turns,
            windows,
            sample_spacing_m=sample_spacing_m,
            max_trim_fraction=max_trim_fraction,
            maximum_route_points=maximum_route_points,
            interpolate_selected_vertices=interpolate_selected_vertices,
        )
        if local_result is not None and local_result.applied:
            return local_result
        return JointBSplineResult(
            status="FALLBACK",
            points=tuple(sampled_geo),
            curvatures_m_inv=tuple(sampled_curvatures),
            span_control_points_m=controls,
            span_node_indices=tuple(range(len(polynomials))),
            node_output_indices=tuple(node_output_indices),
            joint_windows=windows,
            substantive_turn_node_indices=turns,
            c2_pass=c2_pass,
            no_reverse_curvature_pass=no_reverse,
            no_self_intersection_pass=no_self_intersection,
            monotonic_pass=monotonic_pass,
            full_route_g2_pass=False,
            minimum_radius_m=(
                min(1.0 / value for value in sampled_curvatures if value > 1.0e-12)
                if any(value > 1.0e-12 for value in sampled_curvatures)
                else math.inf
            ),
            route_length_m=sum(
                _norm(_sub(second, first)) for first, second in pairwise(sampled_local)
            ),
            maximum_deviation_to_base_m=max(
                _distance_to_polyline(point, local_nodes) for point in sampled_local
            ),
            raw_route_digest=route.raw_route_digest,
            fallback_reason="joint_smoothness_gate",
        )

    finite_radii = [1.0 / value for value in sampled_curvatures if value > 1.0e-12]
    return JointBSplineResult(
        status="ACCEPTED",
        points=tuple(sampled_geo),
        curvatures_m_inv=tuple(sampled_curvatures),
        span_control_points_m=controls,
        span_node_indices=tuple(range(len(polynomials))),
        node_output_indices=tuple(node_output_indices),
        joint_windows=windows,
        substantive_turn_node_indices=turns,
        c2_pass=c2_pass,
        no_reverse_curvature_pass=no_reverse,
        no_self_intersection_pass=no_self_intersection,
        monotonic_pass=monotonic_pass,
        full_route_g2_pass=full_route_g2,
        minimum_radius_m=min(finite_radii) if finite_radii else math.inf,
        route_length_m=sum(_norm(_sub(second, first)) for first, second in pairwise(sampled_local)),
        maximum_deviation_to_base_m=max(
            _distance_to_polyline(point, local_nodes) for point in sampled_local
        ),
        raw_route_digest=route.raw_route_digest,
    )


__all__ = ["JointBSplineResult", "JointWindow", "build_joint_bspline"]
