"""CLI for producing a bounded, research-only route smoothing sidecar."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .route_smoothing import (
    RouteSmoothingPolicy,
    build_route_smoothing_sidecar,
)

DEFAULT_RADIUS_SENSITIVITY_M = (1_000.0, 2_000.0, 4_000.0)


def build_research_sidecar(
    route: Any,
    *,
    experiment_id: str,
    minimum_radius_m: float = 2_000.0,
    radius_sensitivity_m: Sequence[float] = DEFAULT_RADIUS_SENSITIVITY_M,
    input_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the default adaptive-radius sidecar used by R0 research runs."""

    return build_route_smoothing_sidecar(
        route,
        experiment_id=experiment_id,
        policy=RouteSmoothingPolicy(minimum_radius_m=minimum_radius_m),
        radius_sensitivity_m=radius_sensitivity_m,
        input_identity=input_identity,
    )


def _read_route(path: Path) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read route JSON {path}: {exc}") from exc
    if isinstance(value, dict) and isinstance(value.get("route"), dict):
        return value["route"]
    if isinstance(value, dict) and isinstance(value.get("routes"), list):
        routes = value["routes"]
        if routes and isinstance(routes[0], dict):
            return routes[0]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arctic_route_planning.research.route_smoothing_runner"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--minimum-radius-m", type=float, default=2_000.0)
    parser.add_argument(
        "--sensitivity-radius-m",
        type=float,
        action="append",
        default=None,
        help="repeatable minimum-radius scenario; defaults to 1000, 2000 and 4000 m",
    )
    args = parser.parse_args(argv)
    route = _read_route(args.input)
    sidecar = build_research_sidecar(
        route,
        experiment_id=args.experiment_id,
        minimum_radius_m=args.minimum_radius_m,
        radius_sensitivity_m=(
            tuple(args.sensitivity_radius_m)
            if args.sensitivity_radius_m is not None
            else DEFAULT_RADIUS_SENSITIVITY_M
        ),
        input_identity={"source_path": str(args.input)},
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(sidecar, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        f"wrote {args.output} status={sidecar.get('status')} "
        f"applied={sidecar.get('applied')} digest={sidecar.get('sidecar_digest')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
