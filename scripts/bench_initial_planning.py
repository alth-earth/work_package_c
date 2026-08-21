#!/usr/bin/env python3
"""Isolated C initial-planning benchmark on real committed risk data.

This harness intentionally bypasses the formal orchestrator/intake path so the
search itself can be measured without re-running 10-minute artifact hashing.
It uses the exact same components as the formal private planner:
``RiskSampler`` + ``RegularGrid`` + ``VesselPerformanceModel`` +
``TimeDependentAStar`` built from a committed r5 risk window.

Usage:
    python scripts/bench_initial_planning.py \
        --risk-store-root <A-output>/risk-store \
        --run-context <A-output>/run-context.json \
        --b-config ../work_package_b/configs/models/demo_unvalidated_smoke_grid_v4.json \
        --c-config-root configs --contracts-config-root ../arctic_route_contracts/configs \
        --scenario-id murmansk_dikson_august_2026_demo_v1 \
        --horizons 6,12,24,48 --objective fastest --profile 12
"""

from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
from arctic_route_contracts import load_run_context
from arctic_route_risk import PersistentRiskStore

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import HOURLY_RISK_INTERVAL, RiskWindowQuery
from arctic_route_planning.contracts.codec import risk_frame_from_document, risk_frame_to_document
from arctic_route_planning.cost import VesselPerformanceModel
from arctic_route_planning.domain.models import ObjectiveMode
from arctic_route_planning.endpoints import map_corridor_endpoints
from arctic_route_planning.grid import RegularGrid
from arctic_route_planning.planners import (
    NoRouteError,
    PlanningCancelled,
    PlanningHorizonExceeded,
    PlanningRequest,
    TimeDependentAStar,
)
from arctic_route_planning.risk import RiskSampler


def _partial_stats(planner: TimeDependentAStar) -> dict[str, object]:
    c = getattr(planner, "_last_counters", None)
    if c is None:
        return {}
    return {
        "expanded": c.expanded,
        "generated": c.generated,
        "unique": c.unique,
        "heap_pushes": c.heap_pushes,
        "heap_pops": c.heap_pops,
        "stale_pops": c.stale_pop,
        "reopened": c.reopened,
        "max_time_index": c.max_bucket,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-store-root", type=Path, required=True)
    parser.add_argument("--run-context", type=Path, required=True)
    parser.add_argument("--b-config", type=Path, required=True)
    parser.add_argument("--c-config-root", type=Path, required=True)
    parser.add_argument("--contracts-config-root", type=Path, required=True)
    parser.add_argument("--scenario-id", default="murmansk_dikson_august_2026_demo_v1")
    parser.add_argument("--horizons", default="96,120,144")
    parser.add_argument(
        "--objective",
        default="fastest",
        choices=("fastest", "low_risk", "recommended"),
    )
    parser.add_argument("--max-expansions", type=int, default=250_000)
    parser.add_argument("--progress-interval", type=float, default=2.0)
    parser.add_argument("--max-wait-seconds", type=float, default=600.0)
    parser.add_argument("--profile", type=float, help="profile one run with this horizon (hours)")
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args(argv)

    configuration = load_configuration(
        args.c_config_root,
        args.scenario_id,
        shared_config_root=args.contracts_config_root,
    )
    run_context = load_run_context(args.run_context)
    store = PersistentRiskStore(args.risk_store_root)

    start = run_context.simulation_start
    end = run_context.simulation_end
    frames_dir = args.risk_store_root / "frames"
    frame_files = sorted(frames_dir.glob("*.json"))
    if not frame_files:
        raise SystemExit(f"no frames under {frames_dir}")
    first_doc = json.loads(frame_files[0].read_text(encoding="utf-8"))
    model_digest = first_doc["model_config_digest"]
    as_of = datetime.fromisoformat(first_doc["as_of_time"].replace("Z", "+00:00")).astimezone(UTC)

    query = RiskWindowQuery(
        start=start,
        end=end,
        interval=HOURLY_RISK_INTERVAL,
        run_id=run_context.run_id,
        scenario_id=run_context.scenario_id,
        corridor_id=run_context.corridor_id,
        generation_id=0,
        vessel_profile_id=run_context.vessel_profile_id,
        config_digest=run_context.config_digest,
        model_config_digest=model_digest,
        as_of=as_of,
    )
    window = store.get_committed_window(query)
    if window is None:
        raise SystemExit("committed window lookup failed")

    frames = tuple(risk_frame_from_document(risk_frame_to_document(f)) for f in window.frames)
    sampler = RiskSampler(frames, max_frame_gap=HOURLY_RISK_INTERVAL)
    grid = RegularGrid.from_risk_frame(
        frames[0],
        allow_diagonal=configuration.planner.connectivity == 8,
    )
    vessel_model = VesselPerformanceModel.from_configuration(configuration.vessel_model)
    planner = TimeDependentAStar(
        grid,
        sampler,
        vessel_model,
        planner_config=configuration.planner,
    )
    endpoint = map_corridor_endpoints(configuration, frames[0], max_adjustment_km=150.0)
    hard_mask = np.asarray(frames[0].payload["hard_mask"].values, dtype=np.bool_)
    navigable = int((~hard_mask).sum())
    headings = 9  # 8 directions + start
    buckets_per_hour = 60.0 / configuration.planner.time_bucket_minutes
    print(f"grid={grid.shape} navigable_nodes={navigable} start={endpoint.start.node} "
          f"goal={endpoint.goal.node}", flush=True)

    results = []
    horizons = [float(h) for h in args.horizons.split(",") if h.strip()]
    objective = ObjectiveMode(args.objective)
    for H in horizons:
        theoretical = int(navigable * (H * buckets_per_hour) * headings)
        cancelled = {"flag": False}

        def cancel_check(cancelled_state: dict[str, bool] = cancelled) -> bool:
            return cancelled_state["flag"]

        def request_cancel(cancelled_state: dict[str, bool] = cancelled) -> None:
            cancelled_state["flag"] = True

        preq = PlanningRequest(
            start=endpoint.start.node,
            goal=endpoint.goal.node,
            departure_time=start,
            objective=objective,
            time_bucket_size=timedelta(minutes=configuration.planner.time_bucket_minutes),
            edge_sample_count=configuration.planner.edge_sample_count,
            maximum_elapsed=timedelta(hours=H),
            max_expansions=args.max_expansions,
            cancel_check=cancel_check,
            use_heuristic=True,
            progress_interval_seconds=args.progress_interval,
        )
        timer = threading.Timer(args.max_wait_seconds, request_cancel)
        timer.daemon = True
        timer.start()
        t0 = time.perf_counter()
        prof = (
            cProfile.Profile()
            if args.profile is not None and args.profile == H
            else None
        )
        if prof is not None:
            prof.enable()
        try:
            result = planner.plan(preq)
            m = result.metrics
            row = {
                "horizon_h": H,
                "status": "OK",
                "runtime_s": round(time.perf_counter() - t0, 3),
                "theoretical_states": theoretical,
                "expanded": m.expanded_states,
                "generated": m.generated_states,
                "unique": m.unique_states,
                "heap_pushes": m.heap_pushes,
                "heap_pops": m.heap_pops,
                "stale_pops": m.stale_pops,
                "reopened": m.reopened_states,
                "queue_peak": m.queue_peak,
                "max_time_index": m.max_time_index,
                "distance_km": round(result.distance_km, 2),
                "cost_h": round(result.total_cost_hours, 3),
                "exp_per_s": round(m.expanded_states / max(m.compute_ms / 1000.0, 1e-9), 1),
            }
        except PlanningCancelled:
            row = {
                "horizon_h": H,
                "status": "CANCELLED_AFTER_WAIT",
                "runtime_s": round(time.perf_counter() - t0, 1),
                **_partial_stats(planner),
            }
        except NoRouteError as exc:
            row = {
                "horizon_h": H,
                "status": f"NOMAXEXP: {exc}",
                "runtime_s": round(time.perf_counter() - t0, 1),
                **_partial_stats(planner),
            }
        except PlanningHorizonExceeded as exc:
            row = {
                "horizon_h": H,
                "status": f"HORIZON: {exc}",
                "runtime_s": round(time.perf_counter() - t0, 1),
                **_partial_stats(planner),
            }
        finally:
            timer.cancel()
            if prof is not None:
                prof.disable()
                prof_path = Path(f"bench_cprofile_{H}h.pstats")
                prof.dump_stats(str(prof_path))
                stats = pstats.Stats(str(prof_path))
                stats.sort_stats("cumulative").print_stats(30)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    if args.out_json:
        args.out_json.write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
