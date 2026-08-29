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
import sys
from pathlib import Path
from typing import Any

OURS = "time_dependent_astar"
DIJKSTRA = "dijkstra"
STATIC = "static_field"

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


SCHEMA_VERSION = "c.algorithm-comparison.v1"
DEFAULT_RUN_PREFIX = "c-algorithm-comparison-"


def _load_runs(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    """Load comparison runs, skipping foreign artefacts that share a filename.

    The experiments root also hosts unrelated ``comparison.json`` files (for
    example B grid validation runs).  They are rejected by schema version so a
    stray file can never silently pollute the summary.
    """
    runs: list[tuple[str, dict[str, Any]]] = []
    skipped: list[str] = []

    def accept(label: str, document: dict[str, Any], origin: str) -> None:
        if document.get("schema_version") != SCHEMA_VERSION:
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
                        round(reduction, 2) if reduction is not None else None
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
                    avg_risk_delta_pct=(round(avg_delta, 2) if avg_delta is not None else None),
                    ours_max_risk=ours["maximum_edge_risk_median"],
                    baseline_max_risk=base["maximum_edge_risk_median"],
                    max_risk_delta_pct=(round(max_delta, 2) if max_delta is not None else None),
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

    all_rows = eff_rows + qual_rows + synthetic_rows
    with (args.output_dir / "summary-data.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"wrote {args.output_dir / 'summary-tables.md'}")
    print(f"wrote {args.output_dir / 'summary-data.csv'} ({len(all_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
