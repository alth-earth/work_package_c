#!/usr/bin/env python3
"""Aggregate algorithm-comparison runs into presentation-ready tables.

Reads the ``comparison.json`` artefacts produced by
``benchmark_algorithm_comparison.py`` and emits:

  * ``summary-tables.md``  - Markdown tables for the report / slides.
  * ``summary-data.csv``   - flat CSV with a **fixed** schema for plotting.

The CSV schema is deliberately fixed (every row carries every column, missing
values are written as an empty field) so downstream plotting never has to deal
with a ragged union of key sets.

This is a presentation helper only.  It does not alter any planner, contract
or frozen artefact.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

OURS = "time_dependent_astar"
DIJKSTRA = "dijkstra"
STATIC = "static_field"
RISK_BLIND = "risk_blind"

OBJECTIVES = ("fastest", "low_risk", "recommended")

# Fixed CSV schema.  Every emitted row carries every column in this order so
# that plotting code can rely on a stable header.
CSV_FIELDS: tuple[str, ...] = (
    "run",
    "input_kind",
    "segment",
    "grid_size",
    "rows",
    "cols",
    "frames",
    "grid_cells",
    "scale_order",
    "objective",
    "baseline",
    "ours_expanded",
    "baseline_expanded",
    "expansion_gap",
    "expansion_reduction_pct",
    "ours_wall_ms",
    "baseline_wall_ms",
    "speedup",
    "cost_identical",
    "ours_avg_risk",
    "baseline_avg_risk",
    "avg_risk_delta_pct",
    "ours_max_risk",
    "baseline_max_risk",
    "max_risk_delta_pct",
    "ours_distance_km",
    "baseline_distance_km",
    "ours_travel_hours",
    "baseline_travel_hours",
)

# Ordered synthetic profiles, used to give the scaling curve a deterministic
# x-axis.  Real inputs have no profile and keep ``scale_order`` empty.
PROFILE_ORDER = ("small", "medium", "large", "stress")


def _cell(summary: list[dict[str, Any]], objective: str, algorithm: str) -> dict[str, Any] | None:
    for row in summary:
        if row["objective"] == objective and row["algorithm"] == algorithm:
            return row
    return None


def _pct(ours: float, base: float) -> float | None:
    if not base:
        return None
    return 100.0 * (ours - base) / base


def _fmt(value: float | None, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _blank_row(**kwargs: Any) -> dict[str, Any]:
    """Build a CSV row with the fixed schema; unspecified columns stay empty."""
    row: dict[str, Any] = {field: None for field in CSV_FIELDS}
    row.update(kwargs)
    return row


def _meta(doc: dict[str, Any]) -> dict[str, Any]:
    """Extract plotting-friendly identity columns from a comparison document."""
    identity = doc.get("input_identity", {})
    kind = identity.get("kind", "unknown")
    config = identity.get("config") or {}
    rows = config.get("rows")
    cols = config.get("cols")
    frames = config.get("frame_count") or identity.get("frame_count")
    grid_size = ""
    grid_cells = rows * cols if (rows and cols) else None
    scale_order = None
    if kind == "synthetic" and rows and cols and frames:
        grid_size = f"{rows}×{cols}×{frames}"
        profile = identity.get("profile")
        if profile in PROFILE_ORDER:
            scale_order = PROFILE_ORDER.index(profile)
    segment = identity.get("segment") or doc.get("label", "")
    return {
        "input_kind": kind,
        "segment": segment,
        "grid_size": grid_size,
        "rows": rows,
        "cols": cols,
        "frames": frames,
        "grid_cells": grid_cells,
        "scale_order": scale_order,
    }


SCHEMA_VERSIONS = (
    "c.algorithm-comparison.v1",
    "c.algorithm-comparison.v2",
    "c.algorithm-comparison.v3",
)
DEFAULT_RUN_PREFIX = "c-algorithm-comparison-"
SWEEP_MANIFEST = "sweep-manifest.json"
SWEEP_SCHEMA = "c.algorithm-comparison-sweep.v1"


def _load_runs(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    """Load comparison runs, skipping foreign artefacts that share a filename.

    The experiments root also hosts unrelated ``comparison.json`` files (for
    example B grid validation runs).  They are rejected by schema version so a
    stray file can never silently pollute the summary.
    """
    runs: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []

    def accept(label: str, document: dict[str, Any], origin: str) -> None:
        if document.get("schema_version") not in SCHEMA_VERSIONS:
            skipped.append(f"{origin} (schema={document.get('schema_version')!r})")
            return
        runs.append((label, document))

    for label, path in args.run or []:
        accept(label, json.loads(Path(path).read_text(encoding="utf-8")), str(path))
    if args.runs_root:
        root = Path(args.runs_root)
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if not directory.name.startswith(args.run_prefix):
                continue
            candidate = directory / "comparison.json"
            if not candidate.is_file():
                continue
            label = directory.name.removeprefix(args.run_prefix)
            accept(
                label,
                json.loads(candidate.read_text(encoding="utf-8")),
                str(candidate),
            )
    if not runs:
        raise SystemExit("no comparison runs provided")
    for origin in skipped:
        print(f"skipped foreign artefact: {origin}", file=sys.stderr)
    return runs


def _efficiency_section(
    runs: list[tuple[str, dict[str, Any]]], real_only: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    md: list[str] = []
    rows: list[dict[str, Any]] = []
    for label, doc in runs:
        meta = _meta(doc)
        if real_only and meta["input_kind"] != "real":
            continue
        for objective in OBJECTIVES:
            ours = _cell(doc["summary"], objective, OURS)
            base = _cell(doc["summary"], objective, DIJKSTRA)
            if not ours or not base:
                continue
            ours_exp = ours["expanded_states_median"]
            base_exp = base["expanded_states_median"]
            reduction = 100.0 * (1 - ours_exp / base_exp) if base_exp else None
            speedup = (
                base["wall_ms_median"] / ours["wall_ms_median"] if ours["wall_ms_median"] else None
            )
            identical = (
                abs(ours["total_cost_hours_median"] - base["total_cost_hours_median"]) < 1e-9
            )
            md.append(
                f"| {label} | {objective} | {ours_exp:.0f} | {base_exp:.0f} | "
                f"**-{reduction:.1f}%** | {ours['wall_ms_median']:.1f} ms | "
                f"{base['wall_ms_median']:.1f} ms | **{speedup:.2f}×** | "
                f"{'✅' if identical else '❌'} |"
            )
            rows.append(
                _blank_row(
                    run=label,
                    objective=objective,
                    baseline=DIJKSTRA,
                    ours_expanded=ours_exp,
                    baseline_expanded=base_exp,
                    expansion_gap=base_exp - ours_exp,
                    expansion_reduction_pct=(
                        round(reduction, 4) if reduction is not None else None
                    ),
                    ours_wall_ms=round(ours["wall_ms_median"], 2),
                    baseline_wall_ms=round(base["wall_ms_median"], 2),
                    speedup=round(speedup, 3) if speedup else None,
                    cost_identical=identical,
                    ours_avg_risk=ours["average_edge_risk_median"],
                    baseline_avg_risk=base["average_edge_risk_median"],
                    ours_max_risk=ours["maximum_edge_risk_median"],
                    baseline_max_risk=base["maximum_edge_risk_median"],
                    **meta,
                )
            )
    return md, rows


def _quality_section(
    runs: list[tuple[str, dict[str, Any]]], real_only: bool
) -> tuple[list[str], list[dict[str, Any]]]:
    md: list[str] = []
    rows: list[dict[str, Any]] = []
    for label, doc in runs:
        meta = _meta(doc)
        if real_only and meta["input_kind"] != "real":
            continue
        for objective in OBJECTIVES:
            ours = _cell(doc["summary"], objective, OURS)
            base = _cell(doc["summary"], objective, STATIC)
            if not ours or not base:
                continue
            avg_delta = _pct(ours["average_edge_risk_median"], base["average_edge_risk_median"])
            max_delta = _pct(ours["maximum_edge_risk_median"], base["maximum_edge_risk_median"])
            md.append(
                f"| {label} | {objective} | {ours['average_edge_risk_median']:.5f} | "
                f"{base['average_edge_risk_median']:.5f} | "
                f"**{_fmt(avg_delta, 1, '%')}** | "
                f"{ours['maximum_edge_risk_median']:.5f} | "
                f"{base['maximum_edge_risk_median']:.5f} | "
                f"**{_fmt(max_delta, 1, '%')}** | {ours['distance_km_median']:.1f} | "
                f"{base['distance_km_median']:.1f} | "
                f"{ours['travel_hours_median']:.2f} | "
                f"{base['travel_hours_median']:.2f} |"
            )
            rows.append(
                _blank_row(
                    run=label,
                    objective=objective,
                    baseline=STATIC,
                    ours_expanded=ours["expanded_states_median"],
                    baseline_expanded=base["expanded_states_median"],
                    ours_wall_ms=round(ours["wall_ms_median"], 2),
                    baseline_wall_ms=round(base["wall_ms_median"], 2),
                    ours_avg_risk=ours["average_edge_risk_median"],
                    baseline_avg_risk=base["average_edge_risk_median"],
                    avg_risk_delta_pct=(round(avg_delta, 4) if avg_delta is not None else None),
                    ours_max_risk=ours["maximum_edge_risk_median"],
                    baseline_max_risk=base["maximum_edge_risk_median"],
                    max_risk_delta_pct=(round(max_delta, 4) if max_delta is not None else None),
                    ours_distance_km=ours["distance_km_median"],
                    baseline_distance_km=base["distance_km_median"],
                    ours_travel_hours=ours["travel_hours_median"],
                    baseline_travel_hours=base["travel_hours_median"],
                    **meta,
                )
            )
    return md, rows


def _motivation_section(
    runs: list[tuple[str, dict[str, Any]]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Risk-blind baseline as *motivation* evidence, not an advantage claim.

    ``risk_blind`` changes the objective function (ice terms removed from that
    objective's own weights), so it is **not** comparable on search effort.  Its
    only honest reading is the realised route-quality trade-off: how much risk a
    geometry/speed-driven routing would accept for how much time.
    """
    md: list[str] = []
    rows: list[dict[str, Any]] = []
    for label, doc in runs:
        meta = _meta(doc)
        if meta["input_kind"] != "real":
            continue
        for objective in OBJECTIVES:
            ours = _cell(doc["summary"], objective, OURS)
            base = _cell(doc["summary"], objective, RISK_BLIND)
            if not ours or not base:
                continue
            # Read the authoritative deltas from the runner's comparison block
            # (with *ours* as the reference denominator, exactly as the JSON
            # artefacts and the report cite them), instead of recomputing with a
            # possibly different denominator.
            comparison = next(
                (
                    c
                    for c in doc.get("comparisons", [])
                    if c["objective"] == objective and c["baseline"] == RISK_BLIND
                ),
                None,
            )
            risk_paid_avg = comparison["average_risk_delta_pct"] if comparison is not None else None
            risk_paid_max = comparison["max_risk_delta_pct"] if comparison is not None else None
            travel_paid = comparison["travel_hours_delta_pct"] if comparison is not None else None
            distance_paid = comparison["distance_delta_pct"] if comparison is not None else None
            md.append(
                f"| {label} | {objective} | "
                f"{ours['average_edge_risk_median']:.5f} | "
                f"{base['average_edge_risk_median']:.5f} | "
                f"**{_fmt(risk_paid_avg, 1, '%')}** | "
                f"{ours['maximum_edge_risk_median']:.5f} | "
                f"{base['maximum_edge_risk_median']:.5f} | "
                f"**{_fmt(risk_paid_max, 1, '%')}** | "
                f"{ours['travel_hours_median']:.2f} | "
                f"{base['travel_hours_median']:.2f} | "
                f"{_fmt(travel_paid, 1, '%')} | "
                f"{ours['distance_km_median']:.1f} | "
                f"{base['distance_km_median']:.1f} | "
                f"{_fmt(distance_paid, 1, '%')} |"
            )
            rows.append(
                _blank_row(
                    run=label,
                    objective=objective,
                    baseline=RISK_BLIND,
                    ours_expanded=ours["expanded_states_median"],
                    baseline_expanded=base["expanded_states_median"],
                    expansion_reduction_pct=None,
                    ours_wall_ms=round(ours["wall_ms_median"], 2),
                    baseline_wall_ms=round(base["wall_ms_median"], 2),
                    speedup=None,
                    cost_identical=None,
                    ours_avg_risk=ours["average_edge_risk_median"],
                    baseline_avg_risk=base["average_edge_risk_median"],
                    avg_risk_delta_pct=(
                        round(risk_paid_avg, 4) if risk_paid_avg is not None else None
                    ),
                    ours_max_risk=ours["maximum_edge_risk_median"],
                    baseline_max_risk=base["maximum_edge_risk_median"],
                    max_risk_delta_pct=(
                        round(risk_paid_max, 4) if risk_paid_max is not None else None
                    ),
                    ours_distance_km=ours["distance_km_median"],
                    baseline_distance_km=base["distance_km_median"],
                    ours_travel_hours=ours["travel_hours_median"],
                    baseline_travel_hours=base["travel_hours_median"],
                    **meta,
                )
            )
    return md, rows


def _scaling_section(runs: list[tuple[str, dict[str, Any]]]) -> list[str]:
    scale_rows = [
        (label, doc, _meta(doc)) for label, doc in runs if _meta(doc)["input_kind"] == "synthetic"
    ]
    if not scale_rows:
        return []
    scale_rows.sort(key=lambda item: (item[2]["scale_order"] is None, item[2]["scale_order"]))
    md = [
        "## 3. 可扩展性（扩展状态数随网格规模的变化）",
        "",
        "| 网格规模 | 目标 | 扩展(本文) | 扩展(Dijkstra) | 绝对差值 | 扩展减少 | 加速比 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for _label, doc, meta in scale_rows:
        for objective in OBJECTIVES:
            ours = _cell(doc["summary"], objective, OURS)
            base = _cell(doc["summary"], objective, DIJKSTRA)
            if not ours or not base:
                continue
            ours_exp = ours["expanded_states_median"]
            base_exp = base["expanded_states_median"]
            reduction = 100.0 * (1 - ours_exp / base_exp) if base_exp else None
            speedup = (
                base["wall_ms_median"] / ours["wall_ms_median"] if ours["wall_ms_median"] else None
            )
            md.append(
                f"| {meta['grid_size']} | {objective} | {ours_exp:.0f} | "
                f"{base_exp:.0f} | {base_exp - ours_exp:.0f} | "
                f"**-{reduction:.1f}%** | **{speedup:.2f}×** |"
            )
    md.append("")
    return md


# --------------------------------------------------------------------------- #
# expanded-sample sweep (2026-09-01)
# --------------------------------------------------------------------------- #
# One row per (planning case, objective).  A case is one frozen window plus one
# origin/destination pair plus one departure offset; the single-case runs above
# yield 6 rows per window, which is too few to support a distribution claim.
SWEEP_CSV_FIELDS: tuple[str, ...] = (
    "case_id",
    "window",
    "axis",
    "length_bucket",
    "grid_hops",
    "departure_offset_hours",
    "objective",
    "case_status",
    "ours_feasible",
    "dijkstra_feasible",
    "static_feasible",
    "risk_blind_feasible",
    "ours_expanded",
    "dijkstra_expanded",
    "expansion_reduction_pct",
    "ours_wall_ms",
    "dijkstra_wall_ms",
    "static_wall_ms",
    "risk_blind_wall_ms",
    "speedup",
    "cost_identical",
    "ours_cost_hours",
    "dijkstra_cost_hours",
    "static_cost_hours",
    "risk_blind_cost_hours",
    "ours_avg_risk",
    "static_avg_risk",
    "avg_risk_delta_pct",
    "ours_max_risk",
    "dijkstra_max_risk",
    "static_max_risk",
    "max_risk_delta_pct",
    "ours_distance_km",
    "static_distance_km",
    "ours_travel_hours",
    "static_travel_hours",
    "risk_blind_avg_risk",
    "avg_risk_paid_pct",
    "risk_blind_max_risk",
    "max_risk_paid_pct",
    "risk_blind_travel_hours",
)

TIE_TOLERANCE = 1e-9


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; no interpolation, so every value is observed."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _sign_test_p_value(wins: int, losses: int) -> float:
    """Two-sided exact sign test over the non-tied cases.

    The sweep is *paired* (every algorithm sees the same case), and the per-case
    deltas are not assumed to be normal, so the distribution-free sign test is
    the honest choice.  Ties are excluded from the sample, exactly as the
    standard test requires, and the tie count is reported alongside the p-value.
    """
    total = wins + losses
    if total == 0:
        return float("nan")
    tail = min(wins, losses)
    cumulative = sum(math.comb(total, index) for index in range(tail + 1))
    return min(1.0, 2.0 * cumulative / (2**total))


def _describe(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": min(values),
        "p25": _percentile(values, 0.25),
        "median": _median(values),
        "p75": _percentile(values, 0.75),
        "max": max(values),
    }


def _fetch(cells: dict[str, dict[str, Any]], key: str) -> Any:
    return cells.get(key)


def _load_sweep(sweep_root: Path) -> list[dict[str, Any]]:
    """Read every case artefact produced by the sweep driver."""
    manifest_path = sweep_root / SWEEP_MANIFEST
    if not manifest_path.is_file():
        raise SystemExit(f"no sweep manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SWEEP_SCHEMA:
        raise SystemExit(f"{manifest_path} is not {SWEEP_SCHEMA}")
    rows: list[dict[str, Any]] = []
    for entry in manifest["cases"]:
        case_dir = sweep_root / "cases" / entry["case_id"]
        artefact = case_dir / "comparison-summary.json"
        if not artefact.is_file():
            continue
        document = json.loads(artefact.read_text(encoding="utf-8"))
        if document.get("schema_version") not in SCHEMA_VERSIONS:
            continue
        identity = document.get("input_identity", {})
        cells = {f"{cell['objective']}|{cell['algorithm']}": cell for cell in document["summary"]}
        for objective in OBJECTIVES:
            ours = _fetch(cells, f"{objective}|{OURS}")
            dij = _fetch(cells, f"{objective}|{DIJKSTRA}")
            static = _fetch(cells, f"{objective}|{STATIC}")
            blind = _fetch(cells, f"{objective}|{RISK_BLIND}")
            row: dict[str, Any] = {field: None for field in SWEEP_CSV_FIELDS}
            row.update(
                {
                    "case_id": entry["case_id"],
                    "window": entry["window"],
                    "axis": entry["axis"],
                    "length_bucket": entry.get("length_bucket") or "",
                    "grid_hops": entry.get("grid_hops"),
                    "departure_offset_hours": entry.get("departure_offset_hours"),
                    "objective": objective,
                    "case_status": document.get("status", "ok"),
                    "ours_feasible": ours is not None,
                    "dijkstra_feasible": dij is not None,
                    "static_feasible": static is not None,
                    "risk_blind_feasible": blind is not None,
                }
            )
            if ours is not None and dij is not None:
                row["ours_expanded"] = ours["expanded_states_median"]
                row["dijkstra_expanded"] = dij["expanded_states_median"]
                row["expansion_reduction_pct"] = round(
                    100.0 * (1.0 - ours["expanded_states_median"] / dij["expanded_states_median"]),
                    4,
                )
                row["ours_wall_ms"] = round(ours["wall_ms_median"], 2)
                row["dijkstra_wall_ms"] = round(dij["wall_ms_median"], 2)
                row["speedup"] = round(dij["wall_ms_median"] / ours["wall_ms_median"], 3)
                row["cost_identical"] = (
                    abs(ours["total_cost_hours_median"] - dij["total_cost_hours_median"]) < 1e-9
                )
                row["ours_cost_hours"] = ours["total_cost_hours_median"]
                row["dijkstra_cost_hours"] = dij["total_cost_hours_median"]
                row["dijkstra_max_risk"] = dij["maximum_edge_risk_median"]
            if static is not None:
                row["static_wall_ms"] = round(static["wall_ms_median"], 2)
                row["static_cost_hours"] = static["total_cost_hours_median"]
            if blind is not None:
                row["risk_blind_wall_ms"] = round(blind["wall_ms_median"], 2)
                row["risk_blind_cost_hours"] = blind["total_cost_hours_median"]
            if ours is not None and static is not None:
                row["ours_avg_risk"] = ours["average_edge_risk_median"]
                row["static_avg_risk"] = static["average_edge_risk_median"]
                row["avg_risk_delta_pct"] = round(
                    _pct(
                        ours["average_edge_risk_median"],
                        static["average_edge_risk_median"],
                    ),
                    4,
                )
                row["ours_max_risk"] = ours["maximum_edge_risk_median"]
                row["static_max_risk"] = static["maximum_edge_risk_median"]
                row["max_risk_delta_pct"] = round(
                    _pct(
                        ours["maximum_edge_risk_median"],
                        static["maximum_edge_risk_median"],
                    ),
                    4,
                )
                row["ours_distance_km"] = ours["distance_km_median"]
                row["static_distance_km"] = static["distance_km_median"]
                row["ours_travel_hours"] = ours["travel_hours_median"]
                row["static_travel_hours"] = static["travel_hours_median"]
            if ours is not None and blind is not None:
                row["risk_blind_avg_risk"] = blind["average_edge_risk_median"]
                row["avg_risk_paid_pct"] = round(
                    _pct(
                        blind["average_edge_risk_median"],
                        ours["average_edge_risk_median"],
                    ),
                    4,
                )
                row["risk_blind_max_risk"] = blind["maximum_edge_risk_median"]
                row["max_risk_paid_pct"] = round(
                    _pct(
                        blind["maximum_edge_risk_median"],
                        ours["maximum_edge_risk_median"],
                    ),
                    4,
                )
                row["risk_blind_travel_hours"] = blind["travel_hours_median"]
            row["_identity"] = identity
            rows.append(row)
    return rows


def _tally(
    rows: list[dict[str, Any]], metric: str, *, better_when_negative: bool
) -> dict[str, Any]:
    """Aggregate one paired metric across cases, counting wins/ties/losses."""
    values = [
        float(row[metric])
        for row in rows
        if row.get(metric) is not None and isinstance(row[metric], (int, float))
    ]
    wins = sum(1 for value in values if (-value if better_when_negative else value) > TIE_TOLERANCE)
    losses = sum(
        1 for value in values if (-value if better_when_negative else value) < -TIE_TOLERANCE
    )
    summary = _describe(values)
    summary.update(
        {
            "wins": wins,
            "ties": len(values) - wins - losses,
            "losses": losses,
            "p_value": _sign_test_p_value(wins, losses),
        }
    )
    return summary


def _fmt_stat(value: Any, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{value:.{digits}f}"


def _sweep_markdown(rows: list[dict[str, Any]]) -> list[str]:
    md: list[str] = ["# 扩样本扫描聚合统计（多样本证据）", ""]
    md.append(
        "> 冻结真实窗口只有 2 个，且单窗口只有一对起终点、一个出发时刻，"
        "因此单算例证据最多只有 4 条独立航线。本扫描把每个窗口拆成多个"
        "**起终点算例**（走廊内 5 个起点 × 短/中/长三档航段）与多个"
        "**出发时刻**（窗口内 0/36/72/108 h），使配对样本量扩大一个数量级。"
    )
    md.append("")

    total_cases = len({row["case_id"] for row in rows})
    feasible_ours = len({row["case_id"] for row in rows if row["ours_feasible"]})
    feasible_static = len({row["case_id"] for row in rows if row["static_feasible"]})
    md.append("## 1. 样本量与可行性")
    md.append("")
    md.append("| 项 | 数值 |")
    md.append("|---|---:|")
    md.append(f"| 扫描算例数（窗口 × 起终点 × 出发时刻） | {total_cases} |")
    md.append(f"| 算例 × 目标单元数 | {len(rows)} |")
    md.append(f"| 本文算法在 24h 时限内求得航线的算例数 | {feasible_ours} |")
    md.append(f"| 静态场基线在 24h 时限内求得航线的算例数 | {feasible_static} |")
    md.append(f"| 静态场基线不可行（本文算法可行）的算例数 | {feasible_ours - feasible_static} |")
    md.append("")

    # NOTE on polarity: ``expansion_reduction_pct`` is stored as a *positive*
    # number ("86.6" means a 86.6% reduction), so larger is better; the risk
    # deltas are stored as signed percentages where more negative is better.
    metrics = [
        # NOTE on polarity: ``expansion_reduction_pct`` is stored as a *positive*
        # number ("86.6" means a 86.6% reduction), so larger is better; the risk
        # deltas are signed percentages where more negative is better.
        ("扩展状态数减少（vs Dijkstra）", "expansion_reduction_pct", False, "%"),
        ("墙钟加速比（vs Dijkstra）", "speedup", False, "×"),
        ("平均航段风险变化（vs 静态场）", "avg_risk_delta_pct", True, "%"),
        ("最大航段风险变化（vs 静态场）", "max_risk_delta_pct", True, "%"),
    ]
    # ``risk_blind`` is an objective-function ablation (it prices time and
    # distance, zeroing ice risk/uncertainty), so its deltas are **motivation**
    # evidence, not a paired win/loss claim.  They are reported as a distribution
    # and excluded from the sign-test tally.
    motivation_metrics = [
        ("风险无关路径多付的平均风险", "avg_risk_paid_pct", "%"),
        ("风险无关路径多付的最大风险", "max_risk_paid_pct", "%"),
    ]
    md.append("## 2. 配对指标聚合（全部算例，不分窗口）")
    md.append("")
    md.append(
        "| 指标 | n | 最小 | P25 | 中位数 | P75 | 最大 | 本文更优 | 持平 | 本文更差 | 符号检验 p |"
    )
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for title, metric, negative_better, _unit in metrics:
        stats = _tally(rows, metric, better_when_negative=negative_better)
        if not stats.get("n"):
            continue
        md.append(
            f"| {title} | {stats['n']} | {_fmt_stat(stats['min'])} | "
            f"{_fmt_stat(stats['p25'])} | **{_fmt_stat(stats['median'])}** | "
            f"{_fmt_stat(stats['p75'])} | {_fmt_stat(stats['max'])} | "
            f"{stats['wins']} | {stats['ties']} | {stats['losses']} | "
            f"{_fmt_stat(stats['p_value'], 4)} |"
        )
    md.append("")
    md.append("**动机证据（风险无关基线，非配对优势主张）**：")
    md.append("")
    md.append("| 指标 | n | 最小 | P25 | 中位数 | P75 | 最大 | 其中风险无关多付 > 0 | 其中持平 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for title, metric, _unit in motivation_metrics:
        values = [
            float(row[metric])
            for row in rows
            if row.get(metric) is not None and isinstance(row[metric], (int, float))
        ]
        if not values:
            continue
        summary = _describe(values)
        positive = sum(1 for value in values if value > 1e-9)
        ties = sum(1 for value in values if abs(value) <= 1e-9)
        md.append(
            f"| {title} | {summary['n']} | {_fmt_stat(summary['min'])} | "
            f"{_fmt_stat(summary['p25'])} | **{_fmt_stat(summary['median'])}** | "
            f"{_fmt_stat(summary['p75'])} | {_fmt_stat(summary['max'])} | "
            f"{positive} | {ties} |"
        )
    md.append(
        "> 注：risk_blind 是目标函数消融，与本文目标不同，差异不构成配对优势主张；"
        "上表只报告它相对本文多付风险的**分布**，作为『为什么必须用时变风险感知规划』的动机。"
    )
    md.append("")

    md.append("## 3. 按窗口拆分（两窗口独立复现）")
    md.append("")
    md.append("| 窗口 | 指标 | n | 中位数 | P25 | P75 | 本文更优 | 持平 | 本文更差 | p |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for window in sorted({row["window"] for row in rows}):
        subset = [row for row in rows if row["window"] == window]
        for title, metric, negative_better, _unit in metrics:
            stats = _tally(subset, metric, better_when_negative=negative_better)
            if not stats.get("n"):
                continue
            md.append(
                f"| {window} | {title} | {stats['n']} | "
                f"**{_fmt_stat(stats['median'])}** | {_fmt_stat(stats['p25'])} | "
                f"{_fmt_stat(stats['p75'])} | {stats['wins']} | {stats['ties']} | "
                f"{stats['losses']} | {_fmt_stat(stats['p_value'], 4)} |"
            )
    md.append("")

    md.append("## 4. 最优性守卫（代价一致性）")
    md.append("")
    comparable = [row for row in rows if row.get("cost_identical") is not None]
    identical = sum(1 for row in comparable if row["cost_identical"])
    md.append(
        f"- 与无信息 Dijkstra 可直接比较的单元：**{len(comparable)}**；"
        f"总代价严格相同：**{identical}**（{100.0 * identical / max(len(comparable), 1):.1f}%）。"
    )
    md.append(
        "- 其余单元为某一方在 24h 时限内 fail-closed 不可行，不构成最优性反例，也不计入质量对比。"
    )
    md.append("")
    return md


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        nargs=2,
        metavar=("LABEL", "PATH"),
        help="explicit run; repeatable",
    )
    parser.add_argument(
        "--runs-root",
        type=Path,
        help="directory containing one sub-directory per comparison run "
        "(each holding comparison.json); auto-discovers runs",
    )
    parser.add_argument(
        "--run-prefix",
        default=DEFAULT_RUN_PREFIX,
        help="directory-name prefix used when auto-discovering runs",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--sweep-root",
        type=Path,
        help="expanded-sample sweep directory holding " + SWEEP_MANIFEST,
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    runs = _load_runs(args)

    md: list[str] = ["# 算法对比汇总", ""]
    md.append(
        "> 所有算法共享同一 RiskFrame、网格、船模、边评估器、时间桶、硬约束与 "
        "fail-closed 语义；仅搜索策略不同。代价相同即证明启发式未牺牲最优性。"
    )
    md.append("")

    md.append("## 1. 搜索效率对比（对比无信息 Dijkstra，代价严格相同）")
    md.append("")
    md.append(
        "| 算例 | 目标 | 扩展数(本文) | 扩展数(Dijkstra) | 扩展减少 "
        "| 耗时(本文) | 耗时(Dijkstra) | 加速比 | 代价相同 |"
    )
    md.append("|---|---|---:|---:|---:|---:|---:|---:|:--:|")
    eff_md, eff_rows = _efficiency_section(runs, real_only=True)
    md.extend(eff_md)
    md.append("")

    md.append("## 2. 航线质量对比（对比静态场规划的常规做法）")
    md.append("")
    md.append(
        "| 算例 | 目标 | 平均风险(本文) | 平均风险(静态) | 降幅 "
        "| 最大风险(本文) | 最大风险(静态) | 降幅 "
        "| 航程km(本文) | 航程km(静态) | 航行h(本文) | 航行h(静态) |"
    )
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    qual_md, qual_rows = _quality_section(runs, real_only=True)
    md.extend(qual_md)
    md.append("")

    # Motivation evidence: the risk-blind objective is *not* an advantage claim
    # (it changes the objective function), so it gets its own clearly-labelled
    # table and is excluded from the search-efficiency narrative.
    md.append("## 2.5 动机证据：为什么必须用时变风险感知规划（风险无关常规路径）")
    md.append("")
    md.append(
        "> 常规做法（只看时间/距离、忽略冰情风险与不确定性）在 **holdout 24h** 上"
        "为省 0.5% 航行时间承担 15.4% 峰值冰情风险；在 **development 24h** 上两条"
        "路线完全相同（差异 0.0%）。因此本条**不是优势主张**，仅作为动机证据，"
        "实际有效样本 **n=1**。"
    )
    md.append("")
    md.append(
        "| 算例 | 目标 | 平均风险(本文) | 平均风险(风险无关) | 风险无关Δ "
        "| 最大风险(本文) | 最大风险(风险无关) | 风险无关Δ "
        "| 航行h(本文) | 航行h(风险无关) | 风险无关Δ "
        "| 航程km(本文) | 航程km(风险无关) | 风险无关Δ |"
    )
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    motiv_md, motiv_rows = _motivation_section(runs)
    md.extend(motiv_md)
    md.append("")

    md.extend(_scaling_section(runs))

    # Synthetic efficiency/quality rows are produced for the CSV (they feed the
    # scaling plot) but are intentionally excluded from the narrative tables,
    # because the synthetic field is too smooth to support quality claims.
    _synth_eff_md, synth_eff_rows = _efficiency_section(runs, real_only=False)
    _synth_qual_md, synth_qual_rows = _quality_section(runs, real_only=False)
    synthetic_rows = [
        row for row in synth_eff_rows + synth_qual_rows if row["input_kind"] != "real"
    ]

    md.append("## 4. 复现方式")
    md.append("")
    md.append("```bash")
    md.append("# 合成算例（规模曲线）")
    md.append(
        "uv run python scripts/benchmark_algorithm_comparison.py "
        "--synthetic-profile {small|medium|large|stress} --repetitions 5 "
        "--output-dir <dir>"
    )
    md.append("# 真实 Winter 输入（145 帧）")
    md.append(
        "uv run python scripts/benchmark_algorithm_comparison.py "
        "--real-commit <risk-window-commit.json> "
        "--real-route-plan-set <route-plan-set.json> "
        "--real-segment rolling_0_24h --repetitions 3 --output-dir <dir>"
    )
    md.append("```")
    md.append("")

    (args.output_dir / "summary-tables.md").write_text("\n".join(md), encoding="utf-8")

    all_rows = eff_rows + qual_rows + motiv_rows + synthetic_rows
    with (args.output_dir / "summary-data.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"wrote {args.output_dir / 'summary-tables.md'}")
    print(f"wrote {args.output_dir / 'summary-data.csv'} ({len(all_rows)} rows)")

    if args.sweep_root:
        sweep_rows = _load_sweep(args.sweep_root)
        with (args.output_dir / "summary-sweep.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(SWEEP_CSV_FIELDS))
            writer.writeheader()
            for row in sweep_rows:
                row.pop("_identity", None)
                writer.writerow(row)
        (args.output_dir / "summary-sweep.md").write_text(
            "\n".join(_sweep_markdown(sweep_rows)), encoding="utf-8"
        )
        print(f"wrote {args.output_dir / 'summary-sweep.csv'} ({len(sweep_rows)} rows)")
        print(f"wrote {args.output_dir / 'summary-sweep.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
