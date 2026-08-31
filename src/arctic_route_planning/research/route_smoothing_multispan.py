"""Research-only multi-span cubic B-spline geometry.

The implementation in this module is deliberately independent from the
existing one-span route-smoothing experiment.  It provides the mathematical
slice needed to study a local corner: a clamped degree-three B-spline with
four spans and seven control points.  Coordinates are Cartesian metres in a
local tangent frame; converting geographic coordinates is intentionally left
to the caller.

This module proves local curve properties only.  In particular,
``endpoint_g2_pass`` means that this curve can be joined to the two supplied
straight tangent segments with zero endpoint curvature.  It does not claim
that an entire route is G2, because neighbouring local windows still need a
route-level compatibility proof.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

Coordinate = tuple[float, float]

DEGREE = 3
SPAN_COUNT = 4
CONTROL_POINT_COUNT = 7
KNOT_VECTOR = (0.0, 0.0, 0.0, 0.0, 0.25, 0.5, 0.75, 1.0, 1.0, 1.0, 1.0)

# Descriptive aliases make the fixed mathematical shape explicit to callers.
CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR = KNOT_VECTOR
CLAMPED_CUBIC_KNOT_VECTOR = KNOT_VECTOR

_EPSILON = 1.0e-12
_GEOMETRY_EPSILON_M = 1.0e-9


def _finite_scalar(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _coordinate(value: Any, name: str = "point") -> Coordinate:
    if isinstance(value, Mapping):
        first = value.get("x", value.get("lon", value.get("longitude")))
        second = value.get("y", value.get("lat", value.get("latitude")))
    else:
        if isinstance(value, (str, bytes)):
            raise ValueError(f"{name} must contain exactly two coordinates")
        try:
            if len(value) != 2:
                raise ValueError(f"{name} must contain exactly two coordinates")
            first, second = value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must contain exactly two coordinates") from exc
    return (
        _finite_scalar(first, f"{name}[0]"),
        _finite_scalar(second, f"{name}[1]"),
    )


def _coordinates(
    values: Sequence[Coordinate], expected: int | None = None
) -> tuple[Coordinate, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("control_points must be a sequence of points")
    try:
        points = tuple(_coordinate(value, "control point") for value in values)
    except TypeError as exc:
        raise ValueError("control_points must be a sequence of points") from exc
    if expected is not None and len(points) != expected:
        raise ValueError(f"a degree-three four-span spline needs exactly {expected} control points")
    return points


def _validate_knots(knots: Sequence[float]) -> tuple[float, ...]:
    if isinstance(knots, (str, bytes)):
        raise ValueError("knot_vector must be a sequence of finite numbers")
    try:
        result = tuple(_finite_scalar(value, "knot") for value in knots)
    except TypeError as exc:
        raise ValueError("knot_vector must be a sequence of finite numbers") from exc
    if len(result) != CONTROL_POINT_COUNT + DEGREE + 1:
        raise ValueError("the fixed four-span spline requires an eleven-value knot vector")
    if any(right < left for left, right in pairwise(result)):
        raise ValueError("knot_vector must be nondecreasing")
    if result[DEGREE] >= result[CONTROL_POINT_COUNT]:
        raise ValueError("knot_vector has an empty parameter domain")
    if result != KNOT_VECTOR:
        raise ValueError("the mathematical slice requires the fixed four-span knot vector")
    return result


def _validate_parameter(parameter: Any, knots: Sequence[float]) -> float:
    value = _finite_scalar(parameter, "parameter")
    if value < knots[DEGREE] or value > knots[CONTROL_POINT_COUNT]:
        raise ValueError("parameter must lie in the clamped [0, 1] domain")
    return value


def _basis(index: int, degree: int, parameter: float, knots: Sequence[float]) -> float:
    """Evaluate a Cox--de Boor basis function, including the right endpoint."""

    if degree == 0:
        in_half_open_span = knots[index] <= parameter < knots[index + 1]
        # Recursion needs the left-hand limit at the clamped endpoint.  The
        # final positive-width knot interval is the only degree-zero basis
        # that should be active there; using the last zero-width interval
        # would make the endpoint derivative collapse to zero.
        at_right_endpoint = (
            parameter == knots[-1]
            and knots[index] < knots[-1]
            and knots[index + 1] == knots[-1]
        )
        return 1.0 if in_half_open_span or at_right_endpoint else 0.0

    left_denominator = knots[index + degree] - knots[index]
    right_denominator = knots[index + degree + 1] - knots[index + 1]
    left = 0.0
    right = 0.0
    if left_denominator > 0.0:
        left = (
            (parameter - knots[index])
            / left_denominator
            * _basis(index, degree - 1, parameter, knots)
        )
    if right_denominator > 0.0:
        right = (
            (knots[index + degree + 1] - parameter)
            / right_denominator
            * _basis(index + 1, degree - 1, parameter, knots)
        )
    return left + right


def _basis_derivative(
    index: int,
    degree: int,
    order: int,
    parameter: float,
    knots: Sequence[float],
) -> float:
    if order == 0:
        return _basis(index, degree, parameter, knots)
    if degree == 0 or order > degree:
        return 0.0

    left_denominator = knots[index + degree] - knots[index]
    right_denominator = knots[index + degree + 1] - knots[index + 1]
    left = 0.0
    right = 0.0
    if left_denominator > 0.0:
        left = (
            degree
            / left_denominator
            * _basis_derivative(index, degree - 1, order - 1, parameter, knots)
        )
    if right_denominator > 0.0:
        right = (
            degree
            / right_denominator
            * _basis_derivative(index + 1, degree - 1, order - 1, parameter, knots)
        )
    return left - right


def cox_de_boor_basis(
    index: int,
    degree: int,
    parameter: float,
    knot_vector: Sequence[float] = KNOT_VECTOR,
) -> float:
    """Return one fixed-slice Cox--de Boor basis value."""

    knots = _validate_knots(knot_vector)
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("basis index must be an integer")
    if not isinstance(degree, int) or isinstance(degree, bool) or degree < 0:
        raise ValueError("basis degree must be a non-negative integer")
    if degree > DEGREE:
        raise ValueError("basis degree cannot exceed three")
    max_index = len(knots) - degree - 2
    if index < 0 or index > max_index:
        raise ValueError("basis index is outside the knot vector")
    value = _validate_parameter(parameter, knots)
    return _basis(index, degree, value, knots)


def _evaluate_with_derivatives(
    control_points: Sequence[Coordinate],
    parameter: float,
    knot_vector: Sequence[float],
) -> tuple[Coordinate, Coordinate, Coordinate]:
    controls = _coordinates(control_points, CONTROL_POINT_COUNT)
    knots = _validate_knots(knot_vector)
    value = _validate_parameter(parameter, knots)
    points: list[Coordinate] = []
    for order in range(3):
        points.append(
            (
                sum(
                    _basis_derivative(index, DEGREE, order, value, knots) * controls[index][0]
                    for index in range(CONTROL_POINT_COUNT)
                ),
                sum(
                    _basis_derivative(index, DEGREE, order, value, knots) * controls[index][1]
                    for index in range(CONTROL_POINT_COUNT)
                ),
            )
        )
    if any(not math.isfinite(component) for point in points for component in point):
        raise ValueError("B-spline evaluation produced a non-finite value")
    return points[0], points[1], points[2]


def evaluate_clamped_cubic_bspline(
    control_points: Sequence[Coordinate],
    parameter: float,
    knot_vector: Sequence[float] = KNOT_VECTOR,
) -> Coordinate:
    """Evaluate the seven-control-point clamped cubic B-spline in metres."""

    return _evaluate_with_derivatives(control_points, parameter, knot_vector)[0]


def evaluate_clamped_cubic_bspline_derivatives(
    control_points: Sequence[Coordinate],
    parameter: float,
    knot_vector: Sequence[float] = KNOT_VECTOR,
) -> tuple[Coordinate, Coordinate]:
    """Return analytic first and second parameter derivatives in metres."""

    _, first, second = _evaluate_with_derivatives(control_points, parameter, knot_vector)
    return first, second


def _cross(first: Coordinate, second: Coordinate) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _norm(value: Coordinate) -> float:
    return math.hypot(value[0], value[1])


def _sub(first: Coordinate, second: Coordinate) -> Coordinate:
    return first[0] - second[0], first[1] - second[1]


def _add(first: Coordinate, second: Coordinate) -> Coordinate:
    return first[0] + second[0], first[1] + second[1]


def _multiply(value: Coordinate, scalar: float) -> Coordinate:
    return value[0] * scalar, value[1] * scalar


def _unit(value: Coordinate, name: str) -> Coordinate:
    length = _norm(value)
    if not math.isfinite(length) or length <= _GEOMETRY_EPSILON_M:
        raise ValueError(f"{name} must have positive finite length")
    return _multiply(value, 1.0 / length)


def _dot(first: Coordinate, second: Coordinate) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _curvature(first: Coordinate, second: Coordinate) -> float:
    speed = _norm(first)
    if speed <= _GEOMETRY_EPSILON_M or not math.isfinite(speed):
        raise ValueError("B-spline has a zero or non-finite derivative")
    value = abs(_cross(first, second)) / speed**3
    if not math.isfinite(value):
        raise ValueError("B-spline curvature is non-finite")
    return value


def _validate_turn_direction(
    first_derivatives: Sequence[Coordinate],
    second_derivatives: Sequence[Coordinate],
    expected_turn_cross: float,
) -> None:
    """Reject a local curve that turns back through an inflection.

    Endpoint curvature is intentionally zero, so a few sampled cross products
    are expected to be numerically zero.  The non-zero samples must nevertheless
    keep the sign of the raw incoming-to-outgoing turn.  This is a geometric
    safety invariant, not merely a rendering preference: a sign change means
    turn -> counter-turn -> turn and produces the inward dent seen in the old
    midpoint-control implementation.
    """

    cross_products = tuple(
        _cross(first, second)
        for first, second in zip(first_derivatives, second_derivatives, strict=True)
    )
    scale = max(abs(value) for value in cross_products)
    if scale <= _GEOMETRY_EPSILON_M:
        raise ValueError("constructed B-spline has no measurable turn")
    expected_sign = 1.0 if expected_turn_cross > 0.0 else -1.0
    threshold = scale * 1.0e-8
    if any(expected_sign * value < -threshold for value in cross_products):
        raise ValueError("constructed B-spline changes turn direction")


def _sample_parameters(sample_count: int) -> tuple[float, ...]:
    if not isinstance(sample_count, int) or isinstance(sample_count, bool) or sample_count < 5:
        raise ValueError("sample_count must be an integer of at least five")
    interval_count = sample_count - 1
    base, remainder = divmod(interval_count, SPAN_COUNT)
    if base < 1:
        raise ValueError("sample_count must allocate at least one interval per span")

    parameters = [0.0]
    start = 0.0
    for span in range(SPAN_COUNT):
        intervals = base + (1 if span < remainder else 0)
        end = (span + 1) / SPAN_COUNT
        width = (end - start) / intervals
        parameters.extend(start + width * index for index in range(1, intervals + 1))
        start = end
    return tuple(parameters)


def _angle_between(first: Coordinate, second: Coordinate) -> float:
    first_unit = _unit(first, "incoming segment")
    second_unit = _unit(second, "outgoing segment")
    return math.acos(max(-1.0, min(1.0, _dot(first_unit, second_unit))))


def _vector_error(first: Coordinate, second: Coordinate) -> float:
    first_length = _norm(first)
    second_length = _norm(second)
    if first_length <= _GEOMETRY_EPSILON_M or second_length <= _GEOMETRY_EPSILON_M:
        return math.inf
    cosine = max(-1.0, min(1.0, _dot(first, second) / (first_length * second_length)))
    return math.acos(cosine)


def _close_vector(first: Coordinate, second: Coordinate, tolerance: float) -> bool:
    return _norm(_sub(first, second)) <= tolerance


@dataclass(frozen=True, slots=True)
class G2Evidence:
    """Local smoothness evidence; ``full_route_g2_claimed`` is always false."""

    endpoint_position_error_m: tuple[float, float]
    endpoint_tangent_error_rad: tuple[float, float]
    endpoint_curvature_abs_m_inv: tuple[float, float]
    endpoint_position_pass: bool
    endpoint_tangent_pass: bool
    endpoint_curvature_zero_pass: bool
    endpoint_g2_pass: bool
    internal_knot_c2_pass: bool
    internal_knot_checks: tuple[dict[str, Any], ...]
    full_route_g2_claimed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_position_error_m": list(self.endpoint_position_error_m),
            "endpoint_tangent_error_rad": list(self.endpoint_tangent_error_rad),
            "endpoint_curvature_abs_m_inv": list(self.endpoint_curvature_abs_m_inv),
            "endpoint_position_pass": self.endpoint_position_pass,
            "endpoint_tangent_pass": self.endpoint_tangent_pass,
            "endpoint_curvature_zero_pass": self.endpoint_curvature_zero_pass,
            "endpoint_g2_pass": self.endpoint_g2_pass,
            "internal_knot_c2_pass": self.internal_knot_c2_pass,
            "internal_knot_checks": [dict(item) for item in self.internal_knot_checks],
            "full_route_g2_claimed": self.full_route_g2_claimed,
        }


@dataclass(frozen=True, slots=True)
class MultiSpanCubicBSpline:
    """A validated fixed-shape four-span cubic B-spline."""

    control_points: tuple[Coordinate, ...]
    knot_vector: tuple[float, ...] = KNOT_VECTOR
    degree: int = DEGREE

    def __post_init__(self) -> None:
        controls = _coordinates(self.control_points, CONTROL_POINT_COUNT)
        knots = _validate_knots(self.knot_vector)
        if self.degree != DEGREE:
            raise ValueError("the mathematical slice has degree three")
        object.__setattr__(self, "control_points", controls)
        object.__setattr__(self, "knot_vector", knots)

    @property
    def span_count(self) -> int:
        return SPAN_COUNT

    def evaluate(self, parameter: float) -> Coordinate:
        return evaluate_clamped_cubic_bspline(self.control_points, parameter, self.knot_vector)

    def derivatives(self, parameter: float) -> tuple[Coordinate, Coordinate]:
        return evaluate_clamped_cubic_bspline_derivatives(
            self.control_points, parameter, self.knot_vector
        )

    def curvature(self, parameter: float) -> float:
        first, second = self.derivatives(parameter)
        return _curvature(first, second)

    def metadata(self) -> dict[str, Any]:
        return {
            "degree": self.degree,
            "span_count": self.span_count,
            "control_point_count": len(self.control_points),
            "knot_vector": list(self.knot_vector),
            "control_points_m": [list(point) for point in self.control_points],
        }


@dataclass(frozen=True, slots=True)
class LocalCornerCurve:
    """A local curve and its sampled curvature and smoothness evidence."""

    entry: Coordinate
    vertex: Coordinate
    exit: Coordinate
    scale_m: float
    trim_m: float
    spline: MultiSpanCubicBSpline
    parameters: tuple[float, ...]
    samples: tuple[Coordinate, ...]
    first_derivatives: tuple[Coordinate, ...]
    second_derivatives: tuple[Coordinate, ...]
    curvatures_m_inv: tuple[float, ...]
    radii_m: tuple[float, ...]
    minimum_radius_m: float
    evidence: G2Evidence

    @property
    def control_points(self) -> tuple[Coordinate, ...]:
        return self.spline.control_points

    @property
    def control_points_m(self) -> tuple[Coordinate, ...]:
        return self.spline.control_points

    @property
    def knot_vector(self) -> tuple[float, ...]:
        return self.spline.knot_vector

    @property
    def knots(self) -> tuple[float, ...]:
        return self.spline.knot_vector

    @property
    def curvatures(self) -> tuple[float, ...]:
        return self.curvatures_m_inv

    @property
    def minimum_radius(self) -> float:
        return self.minimum_radius_m

    @property
    def endpoint_g2_pass(self) -> bool:
        return self.evidence.endpoint_g2_pass

    @property
    def internal_c2_pass(self) -> bool:
        return self.evidence.internal_knot_c2_pass

    def evaluate(self, parameter: float) -> Coordinate:
        return self.spline.evaluate(parameter)

    def derivatives(self, parameter: float) -> tuple[Coordinate, Coordinate]:
        return self.spline.derivatives(parameter)

    def metadata(self) -> dict[str, Any]:
        result = self.spline.metadata()
        result.update(
            {
                "entry_m": list(self.entry),
                "vertex_m": list(self.vertex),
                "exit_m": list(self.exit),
                "scale_m": self.scale_m,
                "trim_m": self.trim_m,
            }
        )
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata(),
            "parameters": list(self.parameters),
            "samples_m": [list(point) for point in self.samples],
            "first_derivatives_m": [list(point) for point in self.first_derivatives],
            "second_derivatives_m": [list(point) for point in self.second_derivatives],
            "curvatures_m_inv": list(self.curvatures_m_inv),
            "radii_m": [value if math.isfinite(value) else None for value in self.radii_m],
            "minimum_radius_m": self.minimum_radius_m
            if math.isfinite(self.minimum_radius_m)
            else None,
            "evidence": self.evidence.to_dict(),
        }


def _internal_c2_evidence(
    spline: MultiSpanCubicBSpline,
    scale_m: float,
) -> tuple[bool, tuple[dict[str, Any], ...]]:
    checks: list[dict[str, Any]] = []
    passed = True
    for knot in KNOT_VECTOR[DEGREE + 1 : CONTROL_POINT_COUNT]:
        # Compare the two one-sided analytic limits at a very small parameter
        # distance.  Simple internal knots of a cubic B-spline are C2.
        delta = min(1.0e-7, (1.0 / SPAN_COUNT) * 1.0e-4)
        left = spline.derivatives(knot - delta)
        right = spline.derivatives(knot + delta)
        first_error = _norm(_sub(left[0], right[0]))
        second_error = _norm(_sub(left[1], right[1]))
        first_scale = max(1.0, _norm(left[0]), _norm(right[0]))
        second_scale = max(1.0, _norm(left[1]), _norm(right[1]))
        first_pass = first_error <= first_scale * 2.0e-5
        second_pass = second_error <= second_scale * 2.0e-5
        check = {
            "parameter": knot,
            "first_derivative_error_m": first_error,
            "second_derivative_error_m": second_error,
            "first_derivative_tolerance_m": first_scale * 2.0e-5,
            "second_derivative_tolerance_m": second_scale * 2.0e-5,
            "c2_pass": first_pass and second_pass,
        }
        checks.append(check)
        passed = passed and check["c2_pass"]
    return passed, tuple(checks)


def _make_controls(
    entry: Coordinate,
    vertex: Coordinate,
    exit: Coordinate,
    trim_m: float,
    *,
    turn_direction_safe: bool = False,
) -> tuple[Coordinate, ...]:
    incoming = _unit(_sub(vertex, entry), "incoming segment")
    outgoing = _unit(_sub(exit, vertex), "outgoing segment")
    spacing = trim_m / 3.0
    curve_entry = _add(vertex, _multiply(incoming, -trim_m))
    curve_exit = _add(vertex, _multiply(outgoing, trim_m))
    # P3 is the only control point not constrained by endpoint G2.  The
    # historical research sidecar keeps it at the tangent-point midpoint to
    # preserve its frozen digest.  The formal motion facade opts into the raw
    # vertex, which avoids the midpoint's turn -> counter-turn -> turn
    # inflection and its visually inward dent.
    corner_cut = vertex if turn_direction_safe else _multiply(_add(curve_entry, curve_exit), 0.5)
    return (
        curve_entry,
        _add(curve_entry, _multiply(incoming, spacing)),
        _add(curve_entry, _multiply(incoming, 2.0 * spacing)),
        corner_cut,
        _add(curve_exit, _multiply(outgoing, -2.0 * spacing)),
        _add(curve_exit, _multiply(outgoing, -spacing)),
        curve_exit,
    )


def build_local_corner_curve(
    entry: Coordinate,
    vertex: Coordinate,
    exit: Coordinate,
    scale_m: float | None = None,
    *,
    trim_m: float | None = None,
    radius_m: float | None = None,
    sample_count: int = 65,
    turn_direction_safe: bool = False,
) -> LocalCornerCurve:
    """Construct a deterministic local four-span cubic corner.

    ``scale_m`` is interpreted as the distance from ``vertex`` to each curve
    endpoint (the local trim).  ``trim_m`` is an explicit spelling of the
    same quantity.  If ``radius_m`` is supplied instead, the trim is derived
    as ``radius_m * tan(turn_angle / 2)``.  Exactly one of these scale inputs
    must be supplied.  The scale must leave a local window on both adjacent
    segments; this constructor rejects a non-local or degenerate corner.
    """

    entry_point = _coordinate(entry, "entry")
    vertex_point = _coordinate(vertex, "vertex")
    exit_point = _coordinate(exit, "exit")
    incoming_vector = _sub(vertex_point, entry_point)
    outgoing_vector = _sub(exit_point, vertex_point)
    incoming_length = _norm(incoming_vector)
    outgoing_length = _norm(outgoing_vector)
    incoming = _unit(incoming_vector, "incoming segment")
    outgoing = _unit(outgoing_vector, "outgoing segment")
    angle = _angle_between(incoming_vector, outgoing_vector)
    if angle <= 1.0e-8 or angle >= math.pi - 1.0e-8:
        raise ValueError("entry, vertex and exit must form a non-degenerate local corner")

    supplied = [value is not None for value in (scale_m, trim_m, radius_m)]
    if sum(supplied) != 1:
        raise ValueError("supply exactly one of scale_m, trim_m or radius_m")
    if radius_m is not None:
        radius = _finite_scalar(radius_m, "radius_m")
        if radius <= 0.0:
            raise ValueError("radius_m must be positive")
        trim = radius * math.tan(angle / 2.0)
        scale = radius
    else:
        raw_scale = scale_m if scale_m is not None else trim_m
        scale = _finite_scalar(raw_scale, "scale_m")
        if scale <= 0.0:
            raise ValueError("scale_m must be positive")
        trim = scale
    if not math.isfinite(trim) or trim <= 0.0:
        raise ValueError("derived trim must be positive and finite")
    if trim >= 0.5 * min(incoming_length, outgoing_length):
        raise ValueError("trim must leave a local window on both adjacent segments")

    controls = _make_controls(
        entry_point,
        vertex_point,
        exit_point,
        trim,
        turn_direction_safe=turn_direction_safe,
    )
    spline = MultiSpanCubicBSpline(controls)
    parameters = _sample_parameters(sample_count)
    evaluations = tuple(
        _evaluate_with_derivatives(controls, parameter, KNOT_VECTOR) for parameter in parameters
    )
    samples = tuple(item[0] for item in evaluations)
    first_derivatives = tuple(item[1] for item in evaluations)
    second_derivatives = tuple(item[2] for item in evaluations)
    if turn_direction_safe:
        _validate_turn_direction(
            first_derivatives,
            second_derivatives,
            _cross(incoming_vector, outgoing_vector),
        )
    curvatures = tuple(
        _curvature(first, second)
        for first, second in zip(first_derivatives, second_derivatives, strict=True)
    )
    if any(_norm(first) <= _GEOMETRY_EPSILON_M for first in first_derivatives):
        raise ValueError("constructed B-spline has a degenerate tangent")
    radii = tuple(
        math.inf if curvature <= _EPSILON else 1.0 / curvature for curvature in curvatures
    )
    finite_radii = tuple(value for value in radii if math.isfinite(value))
    minimum_radius = min(finite_radii) if finite_radii else math.inf

    start_position_error = _norm(_sub(samples[0], controls[0]))
    end_position_error = _norm(_sub(samples[-1], controls[-1]))
    start_first, start_second = spline.derivatives(0.0)
    end_first, end_second = spline.derivatives(1.0)
    start_tangent_error = _vector_error(start_first, incoming)
    end_tangent_error = _vector_error(end_first, outgoing)
    start_curvature = _curvature(start_first, start_second)
    end_curvature = _curvature(end_first, end_second)
    position_tolerance = max(1.0e-8, trim * 1.0e-10)
    tangent_tolerance = 1.0e-9
    curvature_tolerance = 1.0e-12
    position_pass = (
        start_position_error <= position_tolerance
        and end_position_error <= position_tolerance
    )
    tangent_pass = (
        start_tangent_error <= tangent_tolerance and end_tangent_error <= tangent_tolerance
    )
    curvature_pass = start_curvature <= curvature_tolerance and end_curvature <= curvature_tolerance
    internal_pass, internal_checks = _internal_c2_evidence(spline, trim)
    evidence = G2Evidence(
        endpoint_position_error_m=(start_position_error, end_position_error),
        endpoint_tangent_error_rad=(start_tangent_error, end_tangent_error),
        endpoint_curvature_abs_m_inv=(start_curvature, end_curvature),
        endpoint_position_pass=position_pass,
        endpoint_tangent_pass=tangent_pass,
        endpoint_curvature_zero_pass=curvature_pass,
        endpoint_g2_pass=position_pass and tangent_pass and curvature_pass,
        internal_knot_c2_pass=internal_pass,
        internal_knot_checks=internal_checks,
    )
    if not evidence.endpoint_g2_pass or not evidence.internal_knot_c2_pass:
        raise ValueError("constructed B-spline failed its local smoothness evidence")

    return LocalCornerCurve(
        entry=entry_point,
        vertex=vertex_point,
        exit=exit_point,
        scale_m=scale,
        trim_m=trim,
        spline=spline,
        parameters=parameters,
        samples=samples,
        first_derivatives=first_derivatives,
        second_derivatives=second_derivatives,
        curvatures_m_inv=curvatures,
        radii_m=radii,
        minimum_radius_m=minimum_radius,
        evidence=evidence,
    )


# Explicit names for callers that describe the operation as construction or
# as a B-spline rather than as a generic local curve.
construct_local_corner_curve = build_local_corner_curve
build_local_corner_bspline = build_local_corner_curve


__all__ = [
    "CLAMPED_CUBIC_4_SPAN_KNOT_VECTOR",
    "CLAMPED_CUBIC_KNOT_VECTOR",
    "CONTROL_POINT_COUNT",
    "DEGREE",
    "KNOT_VECTOR",
    "SPAN_COUNT",
    "G2Evidence",
    "LocalCornerCurve",
    "MultiSpanCubicBSpline",
    "build_local_corner_bspline",
    "build_local_corner_curve",
    "construct_local_corner_curve",
    "cox_de_boor_basis",
    "evaluate_clamped_cubic_bspline",
    "evaluate_clamped_cubic_bspline_derivatives",
]
