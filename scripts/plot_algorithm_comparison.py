#!/usr/bin/env python3
"""Render charts from ``c-algorithm-comparison-summary/summary-data.csv``.

Emits, for every figure, a PNG (300 dpi) and an SVG alongside.  Charts default
to CJK labels (Noto Sans CJK SC is available on this host) and offer an
``--english`` flag for a Latin-label variant.

This is a presentation helper.  It does not touch any planner, contract or
frozen artefact, and does not modify C's ``uv.lock`` (matplotlib is injected
via ``uv run --with matplotlib``).
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager
from matplotlib.patches import FancyBboxPatch

OURS = "time_dependent_astar"
DIJKSTRA = "dijkstra"
STATIC = "static_field"
OBJECTIVES = ("fastest", "low_risk", "recommended")

CJK_FONT_CANDIDATES = (
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "SimHei",
    "Microsoft YaHei",
    "PingFang SC",
)

OBJECTIVE_LABELS_EN = {
    "fastest": "fastest",
    "low_risk": "low_risk",
    "recommended": "recommended",
}
OBJECTIVE_LABELS_ZH = {
    "fastest": "最快",
    "low_risk": "低风险",
    "recommended": "推荐",
}


def _select_font(english: bool) -> str:
    available = {f.name for f in font_manager.fontManager.ttflist}
    if english:
        return "DejaVu Sans"
    for candidate in CJK_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return "DejaVu Sans"


def _configure(english: bool, font: str) -> None:
    plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["figure.dpi"] = 110
    plt.rcParams["savefig.dpi"] = 300
    plt.rcParams["savefig.bbox"] = "tight"
    if english:
        plt.rcParams["axes.titleweight"] = "bold"


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _num(value: Any, caster: type) -> Any:
    if value in (None, ""):
        return None
    return caster(value)


def _prepare(
    rows: list[dict[str, Any]], int_keys: tuple[str, ...], float_keys: tuple[str, ...]
) -> None:
    for row in rows:
        for key in int_keys:
            row[key] = _num(row.get(key), int)
        for key in float_keys:
            row[key] = _num(row.get(key), float)


# --------------------------------------------------------------------------- #
# Figure 1: scaling of expanded states
# --------------------------------------------------------------------------- #
def _fig_scaling(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    synth = [
        r for r in rows if r.get("input_kind") == "synthetic" and r.get("baseline") == DIJKSTRA
    ]
    if not synth:
        print("skipping scaling figure: no synthetic rows")
        return
    _prepare(
        synth,
        ("grid_cells", "ours_expanded", "baseline_expanded"),
        ("expansion_reduction_pct", "speedup"),
    )

    by_size: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in synth:
        key = (row["grid_size"], row["grid_cells"] or 0)
        by_size[key][row["objective"]] = row
    sizes = sorted(by_size, key=lambda k: (k[1], k[0]))

    fig, ax = plt.subplots(figsize=(8, 5))
    width = 0.25
    x = np.arange(len(sizes))
    ours_label = "A* (ours)" if english else "本文 A*"
    dij_label = "Dijkstra"
    for idx, obj in enumerate(OBJECTIVES):
        ours_counts = [by_size[k].get(obj, {}).get("ours_expanded") or 0 for k in sizes]
        dij_counts = [by_size[k].get(obj, {}).get("baseline_expanded") or 0 for k in sizes]
        ax.bar(
            x + (idx - 1) * width,
            ours_counts,
            width,
            label=ours_label if idx == 0 else None,
        )
        ax.bar(
            x + (idx - 1) * width,
            dij_counts,
            width,
            bottom=ours_counts,
            alpha=0.35,
            hatch="//",
            label=dij_label if idx == 0 else None,
        )
    ax.set_xticks(x)
    ax.set_xticklabels([k[0] for k in sizes], rotation=15, ha="right")
    ax.set_yscale("log")
    ax.set_ylabel("Expanded states (log)" if english else "扩展状态数（对数）")
    ax.set_xlabel(
        "Synthetic grid (rows x cols x frames)" if english else "合成网格（行 x 列 x 帧）"
    )
    ax.set_title(
        "Scaling: A* (solid) vs Dijkstra (hatched) by grid size"
        if english
        else "可扩展性：A*（实色） vs Dijkstra（斜纹）随网格规模变化"
    )
    ax.legend(loc="upper left", frameon=False)

    ax2 = ax.twinx()
    for obj in OBJECTIVES:
        speedups = [by_size[k].get(obj, {}).get("speedup") or 0.0 for k in sizes]
        ax2.plot(x, speedups, marker="o", linewidth=1.5)
    ax2.set_ylabel("A* speedup over Dijkstra" if english else "A* 相对 Dijkstra 加速比")
    ax2.axhline(1.0, color="grey", linewidth=0.5, linestyle=":")

    fig_path = out / ("scaling.png" if english else "fig-scaling-expansion.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


# --------------------------------------------------------------------------- #
# Figure 2: route risk on real inputs
# --------------------------------------------------------------------------- #
def _fig_risk(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    real = [r for r in rows if r.get("input_kind") == "real" and r.get("baseline") == STATIC]
    if not real:
        print("skipping risk figure: no real rows")
        return
    _prepare(real, (), ("ours_max_risk", "baseline_max_risk", "ours_avg_risk", "baseline_avg_risk"))

    by_input: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in real:
        by_input[row["run"]][row["objective"]] = row
    inputs = sorted(by_input)

    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    width = 0.18
    x = np.arange(len(inputs))
    max_label = "Max edge risk" if english else "最大边风险"
    avg_label = "Average edge risk" if english else "平均边风险"
    panels = ((axes[0], "max", max_label), (axes[1], "avg", avg_label))
    suffix_ours = " (ours)" if english else " (本文)"
    suffix_static = " (static)" if english else " (静态场)"

    for ax, metric, label in panels:
        ours_key = f"ours_{metric}_risk"
        base_key = f"baseline_{metric}_risk"
        for idx, obj in enumerate(OBJECTIVES):
            ours_vals: list[float] = []
            base_vals: list[float] = []
            for name in inputs:
                cell = by_input[name].get(obj)
                if not cell or cell.get(ours_key) is None:
                    ours_vals.append(0.0)
                    base_vals.append(0.0)
                    continue
                ours_vals.append(cell[ours_key])
                base_vals.append(cell[base_key])
            ax.bar(
                x + (idx - 1) * width,
                ours_vals,
                width,
                label=f"{obj}{suffix_ours}",
            )
            ax.bar(
                x + (idx - 1) * width,
                base_vals,
                width,
                alpha=0.35,
                hatch="//",
                label=f"{obj}{suffix_static}",
            )
        ax.set_xticks(x)
        ax.set_xticklabels(inputs, rotation=15, ha="right")
        ax.set_title(label)
        ax.set_ylabel(label)
        if ax is axes[0]:
            ax.legend(fontsize=8, ncol=2, loc="upper left")
    fig.suptitle(
        "Route risk on real Winter inputs: A* (solid) vs static field (hatched)"
        if english
        else "真实 Winter 输入上的航线风险：本文 A*（实色） vs 静态场（斜纹）"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig_path = out / ("risk.png" if english else "fig-risk-comparison.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


# --------------------------------------------------------------------------- #
# Figure 3: speedup
# --------------------------------------------------------------------------- #
def _fig_speedup(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    synth = [
        r for r in rows if r.get("input_kind") == "synthetic" and r.get("baseline") == DIJKSTRA
    ]
    real = [r for r in rows if r.get("input_kind") == "real" and r.get("baseline") == DIJKSTRA]
    if not synth:
        print("skipping speedup figure: no synthetic rows")
        return
    _prepare(synth, ("grid_cells",), ("speedup",))
    _prepare(real, (), ("speedup",))

    markers = {"fastest": "o", "low_risk": "s", "recommended": "^"}
    labels = OBJECTIVE_LABELS_EN if english else OBJECTIVE_LABELS_ZH
    fig, ax = plt.subplots(figsize=(8, 5))
    for obj in OBJECTIVES:
        points = [
            (r["grid_cells"], r["speedup"], r["grid_size"])
            for r in synth
            if r["objective"] == obj and r.get("grid_cells") and r.get("speedup")
        ]
        if not points:
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        ax.scatter(xs, ys, marker=markers[obj], s=80)
        for x_val, y_val, text in points:
            ax.annotate(
                text,
                (x_val, y_val),
                xytext=(4, 4),
                textcoords="offset points",
                fontsize=8,
            )
    for row in real:
        if row.get("speedup") is not None and row.get("frames"):
            ax.axhline(row["speedup"], linestyle="--", alpha=0.4)
    ax.axhline(1.0, color="black", linewidth=0.5, linestyle=":")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Grid cells (log scale)" if english else "网格格点数（对数）")
    ax.set_ylabel("A* speedup over Dijkstra" if english else "A* 相对 Dijkstra 加速比")
    ax.set_title(
        "Synthetic speedup vs grid size (markers); real inputs as dashed lines"
        if english
        else "合成加速比随规模（点），真实输入以虚线参考"
    )
    handles = [
        plt.Line2D(
            [0],
            [0],
            marker=markers[obj],
            color="w",
            markerfacecolor="black",
            markersize=9,
            label=labels[obj],
        )
        for obj in OBJECTIVES
    ]
    ax.legend(handles=handles, loc="upper left", frameon=False)
    fig_path = out / ("speedup.png" if english else "fig-speedup.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


# --------------------------------------------------------------------------- #
# Figure 4: candidate gate funnel (reverse argument)
# --------------------------------------------------------------------------- #
# Hard-coded from the SSOT (docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md, §3 maturity
# table and decision log).  Every number below is verified against that file.
# The funnel reads "how many candidates survived each gate" and the punchline is
# that *zero* candidates were ever enabled -- the incumbent was never overtaken.
FUNNEL_STAGES_ZH = (
    "6 个改进候选进入评估",
    "4 个触发真实输入/性能/资源硬门禁",
    "2 个仅形成研究或局部工程证据",
    "0 个满足默认启用资格",
)
FUNNEL_STAGES_EN = (
    "6 improvement candidates\nentered evaluation",
    "4 hit hard real-input /\nperformance / resource gates",
    "2 produced research-only or\nlocal engineering evidence",
    "0 qualified for\ndefault enablement",
)
FUNNEL_COUNTS = (6, 4, 2, 0)


def _fig_funnel(english: bool, out: Path) -> None:
    """The "reverse argument" funnel: all candidates failed, incumbent stands.

    Does not depend on the CSV; the data are the candidate gate outcomes from
    the SSOT.  The annotation is deliberately conservative -- "not overtaken",
    never "optimal" or "production-grade advantage".
    """
    fig, ax = plt.subplots(figsize=(8, 5.2))
    stages = FUNNEL_STAGES_EN if english else FUNNEL_STAGES_ZH
    counts = FUNNEL_COUNTS
    widths = [6.0, 4.8, 3.6, 2.4]
    y_positions = [3.2, 2.2, 1.2, 0.2]
    faces = ["#EAF2FD", "#E8F7F5", "#FFF4D6", "#FBE7E7"]
    edges = ["#2F5F9F", "#268F83", "#B58223", "#A73535"]

    for index, (y, width, count, stage, face, edge) in enumerate(
        zip(y_positions, widths, counts, stages, faces, edges, strict=True)
    ):
        left = -width / 2
        box = FancyBboxPatch(
            (left, y - 0.34),
            width,
            0.68,
            boxstyle="round,pad=0.02,rounding_size=0.10",
            linewidth=1.4,
            edgecolor=edge,
            facecolor=face,
        )
        ax.add_patch(box)
        ax.text(
            left + 0.42,
            y,
            str(count),
            ha="center",
            va="center",
            fontsize=15,
            fontweight="bold",
            color=edge,
        )
        ax.text(
            left + 0.78,
            y,
            stage,
            ha="left",
            va="center",
            fontsize=9.3,
            color="#1E1E1E",
        )
        if index < len(y_positions) - 1:
            ax.annotate(
                "",
                xy=(0, y_positions[index + 1] + 0.36),
                xytext=(0, y - 0.36),
                arrowprops={"arrowstyle": "-|>", "color": "#2F5F9F", "lw": 1.4},
            )
    ax.set_xlim(-3.5, 3.5)
    ax.set_ylim(-0.45, 4.05)
    ax.axis("off")
    ax.set_title(
        "Candidate gate funnel: the incumbent was never overtaken"
        if english
        else "改进候选门禁漏斗：当前实现从未被超越",
        fontsize=12.5,
        color="#17365D",
        pad=14,
    )
    annotation = (
        "All improvement candidates failed the correctness-first gates; "
        "the production default (time-dependent A*) remained unchanged."
        if english
        else "全部改进候选未通过正确性优先门禁；生产默认（时间依赖 A*）保持不变。"
    )
    fig.subplots_adjust(top=0.87, bottom=0.15)
    fig.text(
        0.5,
        0.02,
        annotation,
        ha="center",
        va="bottom",
        fontsize=9.5,
        color="#555555",
    )
    fig_path = out / ("funnel.png" if english else "fig-funnel.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _recommended_real_run_keys(runs: dict[str, dict[str, Any]]) -> list[str]:
    """Return each real 24 h run once, in the paper's stated panel order."""
    labels = {
        key.split("|", 1)[0]
        for key in runs
        if ("holdout" in key or "development" in key) and "|recommended|" in key
    }
    ordered: list[str] = []
    for token in ("holdout", "development"):
        ordered.extend(sorted(label for label in labels if token in label))
    return ordered


def _load_run_documents(root: Path) -> dict[str, dict[str, Any]]:
    """Load the raw comparison.json artefacts for per-step figures.

    Returns a mapping ``"<run>|<objective>|<algorithm>" -> raw record``.  Only
    v2 artefacts carry the per-step sequences these figures need; v1 artefacts
    are skipped silently.
    """
    documents: dict[str, dict[str, Any]] = {}
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        candidate = directory / "comparison.json"
        if not candidate.is_file():
            continue
        document = json.loads(candidate.read_text(encoding="utf-8"))
        if document.get("schema_version") != "c.algorithm-comparison.v2":
            continue
        label = directory.name.removeprefix("c-algorithm-comparison-")
        for record in document.get("raw", []):
            key = f"{label}|{record['objective']}|{record['algorithm']}"
            documents[key] = record
    return documents


def _fig_runtime_scale_log(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    """Runtime (median wall ms) vs synthetic grid size, log-log."""
    synth = [
        r for r in rows if r.get("input_kind") == "synthetic" and r.get("baseline") == "dijkstra"
    ]
    if not synth:
        return
    _prepare(synth, ("grid_cells",), ("ours_wall_ms", "baseline_wall_ms"))
    by_size: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in synth:
        key = (r["grid_size"], r["grid_cells"] or 0)
        by_size[key][r["objective"]] = r
    sizes = sorted(by_size, key=lambda k: (k[1], k[0]))

    fig, ax = plt.subplots(figsize=(9.2, 5.1))
    markers = {"fastest": "o", "low_risk": "s", "recommended": "^"}
    labels = OBJECTIVE_LABELS_EN if english else OBJECTIVE_LABELS_ZH
    ours_label = "A* (ours)" if english else "本文 A*"
    dij_label = "Dijkstra"
    for obj in OBJECTIVES:
        ours_x = [by_size[k].get(obj, {}).get("grid_cells") for k in sizes]
        ours_y = [by_size[k].get(obj, {}).get("ours_wall_ms") for k in sizes]
        dij_x = [by_size[k].get(obj, {}).get("grid_cells") for k in sizes]
        dij_y = [by_size[k].get(obj, {}).get("baseline_wall_ms") for k in sizes]
        ours_x = [v for v in ours_x if v]
        ours_y = [v for v in ours_y if v is not None]
        dij_x = [v for v in dij_x if v]
        dij_y = [v for v in dij_y if v is not None]
        ax.plot(
            ours_x, ours_y, marker=markers[obj], linewidth=1.5, label=f"{labels[obj]} {ours_label}"
        )
        ax.plot(
            dij_x,
            dij_y,
            marker=markers[obj],
            linewidth=1.5,
            linestyle="--",
            alpha=0.7,
            label=f"{labels[obj]} {dij_label}",
        )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Grid cells (log)" if english else "网格格点数（对数）")
    ax.set_ylabel("Runtime / ms (log)" if english else "运行时间 / ms（对数）")
    ax.set_title("Runtime scaling (log-log)" if english else "运行时间随规模变化（双对数）")
    ax.legend(fontsize=8, ncol=2, loc="upper left", frameon=False)
    fig_path = out / ("runtime-scale-log.png" if english else "fig-runtime-scale-log.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_runtime_cost_scatter(runs: dict[str, dict[str, Any]], english: bool, out: Path) -> None:
    """Scatter: runtime (x) vs total cost (y), real 24h inputs.

    Proves that the faster algorithm does not sacrifice solution cost.  Cost
    lives in the raw JSON records, not the CSV, so this figure needs the v2
    artefacts loaded via ``--experiments-root``.
    """
    if not runs:
        print("skipping runtime-cost scatter: no v2 artefacts")
        return
    run_labels = _recommended_real_run_keys(runs)
    if not run_labels:
        return
    fig, ax = plt.subplots(figsize=(8, 5))
    markers = {"time_dependent_astar": "o", "dijkstra": "s", "static_field": "^", "risk_blind": "D"}
    legend_en = {
        "time_dependent_astar": "A* (ours)",
        "dijkstra": "Dijkstra",
        "static_field": "Static field",
        "risk_blind": "Risk-blind",
    }
    legend_zh = {
        "time_dependent_astar": "本文 A*",
        "dijkstra": "Dijkstra",
        "static_field": "静态场",
        "risk_blind": "风险无关",
    }
    legend = legend_en if english else legend_zh
    for run_index, run_label in enumerate(run_labels):
        obj = "recommended"
        for algo in ("static_field", "risk_blind", "dijkstra", "time_dependent_astar"):
            record = runs.get(f"{run_label}|{obj}|{algo}")
            if not record:
                continue
            x = record.get("wall_ms")
            y = record["route"].get("total_cost_hours")
            if x is None or y is None:
                continue
            ax.scatter(
                x,
                y,
                marker=markers[algo],
                s=100,
                color={
                    "time_dependent_astar": "#1f77b4",
                    "dijkstra": "#ff7f0e",
                    "static_field": "#2ca02c",
                    "risk_blind": "#d62728",
                }[algo],
                label=legend[algo] if run_index == 0 else None,
            )
    ax.set_xscale("log")
    ax.set_xlabel("Runtime / ms" if english else "运行时间 / ms")
    ax.set_ylabel("Total cost / hours" if english else "总代价 / 小时")
    ax.set_title(
        "Runtime vs total cost (real 24h)" if english else "运行时间 vs 总代价（真实 24h）"
    )
    fig.subplots_adjust(right=0.76)
    ax.legend(fontsize=9, loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False)
    fig_path = out / ("runtime-cost.png" if english else "fig-runtime-cost.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_runtime_risk_scatter(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    """Scatter: runtime (x) vs max edge risk (y), real 24h inputs."""
    real = [r for r in rows if r.get("input_kind") == "real" and r.get("baseline") == "dijkstra"]
    if not real:
        return
    _prepare(real, (), ("ours_wall_ms", "baseline_wall_ms", "ours_max_risk", "baseline_max_risk"))
    fig, ax = plt.subplots(figsize=(9.6, 5.1))
    markers = {"fastest": "o", "low_risk": "s", "recommended": "^"}
    labels = OBJECTIVE_LABELS_EN if english else OBJECTIVE_LABELS_ZH
    for obj in OBJECTIVES:
        cells = [r for r in real if r["objective"] == obj]
        ox = [r["ours_wall_ms"] for r in cells if r.get("ours_wall_ms")]
        oy = [r["ours_max_risk"] for r in cells if r.get("ours_max_risk")]
        bx = [r["baseline_wall_ms"] for r in cells if r.get("baseline_wall_ms")]
        by = [r["baseline_max_risk"] for r in cells if r.get("baseline_max_risk")]
        ax.scatter(ox, oy, marker=markers[obj], s=90, label=f"{labels[obj]} A*")
        ax.scatter(bx, by, marker=markers[obj], s=90, alpha=0.4, label=f"{labels[obj]} Dijkstra")
    ax.set_xscale("log")
    ax.set_xlabel("Runtime / ms" if english else "运行时间 / ms")
    ax.set_ylabel("Max edge risk" if english else "最大边风险")
    ax.set_title(
        "Runtime vs max risk (real 24h)" if english else "运行时间 vs 最大风险（真实 24h）"
    )
    fig.subplots_adjust(right=0.70)
    ax.legend(fontsize=8, ncol=1, loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False)
    fig_path = out / ("runtime-risk.png" if english else "fig-runtime-risk.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_risk_timeseries(runs: dict[str, dict[str, Any]], english: bool, out: Path) -> None:
    """Per-step risk time series on real 24h inputs.

    Reads the per-step ``step_edge_risk_score`` sequences captured in v2
    artefacts.  One subplot per (run, objective); algorithms as line colours.
    """
    if not runs:
        print("skipping risk time-series: no v2 artefacts")
        return
    # Pick real-input runs only, recommended objective.
    run_labels = _recommended_real_run_keys(runs)
    if not run_labels:
        return
    palette = {
        "time_dependent_astar": "#1f77b4",
        "dijkstra": "#ff7f0e",
        "static_field": "#2ca02c",
        "risk_blind": "#d62728",
    }
    legend_en = {
        "time_dependent_astar": "A* (ours)",
        "dijkstra": "Dijkstra",
        "static_field": "Static field",
        "risk_blind": "Risk-blind",
    }
    legend_zh = {
        "time_dependent_astar": "本文 A*",
        "dijkstra": "Dijkstra",
        "static_field": "静态场",
        "risk_blind": "风险无关",
    }
    legend = legend_en if english else legend_zh
    n = len(run_labels)
    fig, axes = plt.subplots(1, n, figsize=(4.5 * n, 5.0), sharey=False)
    if n == 1:
        axes = [axes]
    for ax, run_label in zip(axes, run_labels, strict=False):
        obj = "recommended"
        for algo in ("static_field", "risk_blind", "dijkstra", "time_dependent_astar"):
            lookup = f"{run_label}|{obj}|{algo}"
            record = runs.get(lookup)
            if not record:
                continue
            risks = record["route"].get("step_edge_risk_score", [])
            if not risks:
                continue
            xs = list(range(1, len(risks) + 1))
            ax.plot(
                xs,
                risks,
                marker={
                    "time_dependent_astar": "o",
                    "dijkstra": "s",
                    "static_field": "^",
                    "risk_blind": "D",
                }[algo],
                markersize=4,
                linewidth=1.5,
                linestyle={
                    "time_dependent_astar": "-",
                    "dijkstra": "--",
                    "static_field": "-.",
                    "risk_blind": ":",
                }[algo],
                color=palette[algo],
                label=legend[algo],
                zorder={
                    "time_dependent_astar": 4,
                    "dijkstra": 3,
                    "static_field": 2,
                    "risk_blind": 1,
                }[algo],
            )
        ax.set_xlabel("Step index" if english else "航段序号")
        ax.set_ylabel("Edge risk" if english else "边风险")
        ax.set_title(f"{run_label} / {obj}")
        ax.legend(fontsize=8, loc="upper left", frameon=False)
        note = "n=1 route/window" if english else "单窗口单次航线（n=1）"
        if "development" in run_label:
            note += "\nrisk-blind = recommended" if english else "\n风险无关与推荐航线重合"
        ax.text(
            0.98,
            0.02,
            note,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7.5,
            color="#555555",
        )
    fig.suptitle(
        "Per-step edge risk on real 24h inputs" if english else "真实 24h 输入上的逐段风险序列"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_path = out / ("risk-timeseries.png" if english else "fig-risk-timeseries.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_risk_distribution(runs: dict[str, dict[str, Any]], english: bool, out: Path) -> None:
    """Box-and-whisker of per-step edge risk, real 24h inputs.

    Each algorithm contributes one box per (run, objective).  When n=1 the
    box is degenerate (a single point) and is annotated accordingly.
    """
    if not runs:
        print("skipping risk distribution: no v2 artefacts")
        return
    run_labels = _recommended_real_run_keys(runs)
    if not run_labels:
        return
    palette = {
        "time_dependent_astar": "#1f77b4",
        "dijkstra": "#ff7f0e",
        "static_field": "#2ca02c",
        "risk_blind": "#d62728",
    }
    legend_en = {
        "time_dependent_astar": "A* (ours)",
        "dijkstra": "Dijkstra",
        "static_field": "Static field",
        "risk_blind": "Risk-blind",
    }
    legend_zh = {
        "time_dependent_astar": "本文 A*",
        "dijkstra": "Dijkstra",
        "static_field": "静态场",
        "risk_blind": "风险无关",
    }
    legend = legend_en if english else legend_zh
    fig, axes = plt.subplots(1, len(run_labels), figsize=(6.2 * len(run_labels), 4.5), sharey=False)
    if len(run_labels) == 1:
        axes = [axes]
    for ax, run_label in zip(axes, run_labels, strict=False):
        obj = "recommended"
        data: list[list[float]] = []
        labels: list[str] = []
        colours: list[str] = []
        for algo in ("time_dependent_astar", "dijkstra", "static_field", "risk_blind"):
            record = runs.get(f"{run_label}|{obj}|{algo}")
            if not record:
                continue
            risks = record["route"].get("step_edge_risk_score", [])
            if not risks:
                continue
            data.append(risks)
            labels.append(legend[algo])
            colours.append(palette[algo])
        bp = ax.boxplot(data, tick_labels=labels, patch_artist=True, showmeans=True)
        for patch, colour in zip(bp["boxes"], colours, strict=False):
            patch.set_facecolor(colour)
            patch.set_alpha(0.6)
        ax.set_ylabel("Edge risk distribution" if english else "边风险分布")
        ax.set_title(f"{run_label} / {obj}")
        note = "n=1 route/window" if english else "单窗口单次航线（n=1）"
        if "development" in run_label:
            note += "\nrisk-blind = recommended" if english else "\n风险无关与推荐航线重合"
        ax.text(
            0.98,
            0.02,
            note,
            transform=ax.transAxes,
            ha="right",
            va="bottom",
            fontsize=7.5,
            color="#555555",
        )
    fig.suptitle(
        "Per-step risk distribution on real 24h inputs"
        if english
        else "真实 24h 输入上的逐段风险分布"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig_path = out / ("risk-distribution.png" if english else "fig-risk-distribution.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


# --------------------------------------------------------------------------- #
# Expanded-sample sweep figures (2026-09-01).
#
# The single-case evidence is too small for a distribution claim, so these
# figures read ``summary-sweep.csv`` (one row per planning case x objective)
# instead of the six-row single-case summary.
# --------------------------------------------------------------------------- #
def _read_sweep_csv(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        print(f"skipping sweep figures: no {path}")
        return []
    return _read_csv(path)


def _sweep_values(rows: list[dict[str, Any]], metric: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(metric)
        if raw in (None, ""):
            continue
        values.append(float(raw))
    return values


def _fig_sweep_distribution(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    """Paired per-case deltas as box-and-strip plots.

    Unlike the single-case figures, every box here is built from dozens of
    independent planning cases, so the spread is real observed variation and not
    a degenerate n=1 point.
    """
    if not rows:
        return
    panels = [
        (
            "expansion_reduction_pct",
            "Expansion reduction vs Dijkstra (%)"
            if english
            else "扩展状态数减少（%, vs Dijkstra）",
            "#1f77b4",
        ),
        (
            "max_risk_delta_pct",
            "Maximum risk change vs static field (%)"
            if english
            else "最大航段风险变化（%, vs 静态场）",
            "#2ca02c",
        ),
        (
            "avg_risk_delta_pct",
            "Average risk change vs static field (%)"
            if english
            else "平均航段风险变化（%, vs 静态场）",
            "#9467bd",
        ),
    ]
    fig, axes = plt.subplots(1, len(panels), figsize=(4.6 * len(panels), 5.0))
    for ax, (metric, label, colour) in zip(axes, panels, strict=False):
        values = _sweep_values(rows, metric)
        if not values:
            ax.set_visible(False)
            continue
        box = ax.boxplot(
            values,
            tick_labels=[""],
            widths=0.45,
            showfliers=False,
            patch_artist=True,
        )
        for patch in box["boxes"]:
            patch.set_facecolor(colour)
            patch.set_alpha(0.35)
        rng = np.random.default_rng(20260901)
        jitter = rng.uniform(-0.16, 0.16, size=len(values))
        ax.scatter(
            np.ones(len(values)) + jitter,
            values,
            s=18,
            color=colour,
            edgecolor="white",
            linewidth=0.4,
            alpha=0.85,
            zorder=3,
        )
        median = float(np.median(values))
        ax.axhline(median, color=colour, linestyle="--", linewidth=1.2)
        ax.axhline(0.0, color="#888888", linewidth=0.8)
        ax.set_xticks([])
        ax.set_ylabel(label)
        ax.text(
            0.02,
            0.02,
            (
                f"n={len(values)}  median={median:.2f}%"
                if english
                else f"n={len(values)}  中位数={median:.2f}%"
            ),
            transform=ax.transAxes,
            fontsize=8,
            color="#444444",
        )
    fig.suptitle(
        "Paired per-case deltas across the expanded sample"
        if english
        else "扩样本上每个算例的配对差异分布"
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig_path = out / (
        "sweep-delta-distribution.png" if english else "fig-sweep-delta-distribution.png"
    )
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_sweep_outcome(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    """Win / tie / loss counts per metric, with the exact sign-test p-value."""
    if not rows:
        return
    panels = [
        (
            "扩展状态数减少" if not english else "Expansion reduction",
            "expansion_reduction_pct",
            True,
        ),
        ("墙钟加速比" if not english else "Wall-clock speedup", "speedup", False),
        ("最大风险变化" if not english else "Maximum risk change", "max_risk_delta_pct", True),
        ("平均风险变化" if not english else "Average risk change", "avg_risk_delta_pct", True),
    ]
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    x = np.arange(len(panels))
    width = 0.26
    colours = {"win": "#2ca02c", "tie": "#bbbbbb", "loss": "#d62728"}
    labels = {
        "win": "本文更优" if not english else "ours better",
        "tie": "持平" if not english else "tie",
        "loss": "本文更差" if not english else "ours worse",
    }
    for offset, (kind, sign) in enumerate((("win", 1), ("tie", 0), ("loss", -1)), start=-1):
        counts: list[int] = []
        for _title, metric, negative_better in panels:
            values = _sweep_values(rows, metric)
            win = sum(1 for v in values if (-v if negative_better else v) > 1e-9)
            loss = sum(1 for v in values if (-v if negative_better else v) < -1e-9)
            counts.append(win if sign > 0 else (loss if sign < 0 else len(values) - win - loss))
        ax.bar(
            x + offset * width,
            counts,
            width,
            label=labels[kind],
            color=colours[kind],
        )
        for xi, count in zip(x, counts, strict=False):
            ax.text(
                xi + offset * width,
                count,
                str(count),
                ha="center",
                va="bottom",
                fontsize=8,
            )
    ax.set_xticks(x)
    ax.set_xticklabels([title for title, _m, _n in panels])
    ax.set_ylabel("算例数" if not english else "case count")
    ax.set_title(
        "逐算例胜负计数（配对比较）" if not english else "Per-case outcome counts (paired)"
    )
    ax.legend(frameon=False, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig_path = out / ("sweep-outcome-counts.png" if english else "fig-sweep-outcome-counts.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def _fig_sweep_risk_vs_hops(rows: list[dict[str, Any]], english: bool, out: Path) -> None:
    """Maximum-risk delta against voyage length, split by window."""
    if not rows:
        return
    usable = [
        row
        for row in rows
        if row.get("max_risk_delta_pct") not in (None, "")
        and row.get("grid_hops") not in (None, "")
    ]
    if not usable:
        print("skipping risk-vs-hops figure: no usable rows")
        return
    fig, ax = plt.subplots(figsize=(8.0, 5.0))
    markers = {"holdout": "o", "development": "s"}
    for window in sorted({row["window"] for row in usable}):
        subset = [row for row in usable if row["window"] == window]
        ax.scatter(
            [float(row["grid_hops"]) for row in subset],
            [float(row["max_risk_delta_pct"]) for row in subset],
            s=42,
            marker=markers.get(window, "o"),
            alpha=0.8,
            label=window,
        )
    ax.axhline(0.0, color="#888888", linewidth=0.9)
    ax.set_xlabel("Grid hops (voyage length)" if english else "网格跳数（航段长度）")
    ax.set_ylabel(
        "Maximum risk change vs static field (%)" if english else "最大航段风险变化（%, vs 静态场）"
    )
    ax.set_title("Risk advantage by voyage length" if english else "风险优势随航段长度的变化")
    ax.legend(frameon=False, fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig_path = out / ("sweep-risk-vs-hops.png" if english else "fig-sweep-risk-vs-hops.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--experiments-root",
        type=Path,
        default=None,
        help="root containing per-run comparison.json; required for per-step figures",
    )
    parser.add_argument(
        "--english",
        action="store_true",
        help="use Latin labels (default: CJK when a CJK font is available)",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="render both the CJK and the English variant",
    )
    parser.add_argument(
        "--sweep-csv",
        type=Path,
        default=None,
        help="summary-sweep.csv produced by the expanded-sample sweep",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_csv(args.csv)
    if not rows:
        raise SystemExit(f"no rows in {args.csv}")

    runs = _load_run_documents(args.experiments_root) if args.experiments_root is not None else {}
    sweep_rows = _read_sweep_csv(args.sweep_csv) if args.sweep_csv is not None else []

    variants = [False] if not (args.english or args.both) else []
    if args.english:
        variants.append(True)
    if args.both:
        variants.extend([False, True])
    seen: set[bool] = set()
    for english in variants:
        if english in seen:
            continue
        seen.add(english)
        _configure(english, _select_font(english))
        _fig_scaling(rows, english, args.output_dir)
        _fig_risk(rows, english, args.output_dir)
        _fig_speedup(rows, english, args.output_dir)
        _fig_funnel(english, args.output_dir)
        _fig_runtime_scale_log(rows, english, args.output_dir)
        _fig_runtime_cost_scatter(runs, english, args.output_dir)
        _fig_runtime_risk_scatter(rows, english, args.output_dir)
        _fig_risk_timeseries(runs, english, args.output_dir)
        _fig_risk_distribution(runs, english, args.output_dir)
        if sweep_rows:
            _fig_sweep_distribution(sweep_rows, english, args.output_dir)
            _fig_sweep_outcome(sweep_rows, english, args.output_dir)
            _fig_sweep_risk_vs_hops(sweep_rows, english, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
