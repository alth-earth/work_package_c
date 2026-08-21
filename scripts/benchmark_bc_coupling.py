#!/usr/bin/env python3
"""Measure C planning over B formal-grid experiment RiskFrames."""

from __future__ import annotations

import argparse
import gc
import json
from pathlib import Path

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts.codec import risk_frame_from_document
from arctic_route_planning.coupling_benchmark import benchmark_planning_on_risk_frames
from arctic_route_planning.endpoints import map_corridor_endpoints


def _profile(value: str) -> tuple[str, Path]:
    try:
        name, raw_path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("profile must be NAME=PATH") from exc
    if not name:
        raise argparse.ArgumentTypeError("profile name cannot be empty")
    return name, Path(raw_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", type=_profile, required=True)
    parser.add_argument("--c-config-root", type=Path, required=True)
    parser.add_argument("--contracts-config-root", type=Path, required=True)
    parser.add_argument(
        "--scenario-id",
        default="tromso_isfjorden_august_2026_demo_v1",
    )
    parser.add_argument("--max-snap-km", type=float, default=30.0)
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    configuration = load_configuration(
        args.c_config_root,
        args.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    results = []
    for name, path in args.profile:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("schema_version") != "b.formal-grid-experiment-frames.v1":
            raise ValueError(f"unsupported experiment frame document: {path}")
        frames = tuple(risk_frame_from_document(item) for item in document["frames"])
        endpoint = map_corridor_endpoints(
            configuration,
            frames[0],
            max_adjustment_km=args.max_snap_km,
        )
        summary = benchmark_planning_on_risk_frames(
            frames,
            start=endpoint.start.node,
            goal=endpoint.goal.node,
            planner_config=configuration.planner,
            vessel_config=configuration.vessel_model,
            max_expansions=args.max_expansions,
        )
        summary["name"] = name
        summary["endpoint_mapping"] = endpoint.to_document()
        results.append(summary)
        del frames, summary
        gc.collect()

    report = {
        "schema_version": "bc.coupling-performance.v1",
        "status": "EXPERIMENTAL",
        "source_contract": "bc.risk-frame.v2",
        "production_defaults_changed": False,
        "profiles": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
