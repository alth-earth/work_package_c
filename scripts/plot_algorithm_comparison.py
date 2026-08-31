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
from collections import defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager

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
    "4 个在真实输入 / 正式 M2 门禁 FAIL",
    "2 个无剪枝增益或撤回（RETIRED）",
    "0 个被启用（candidate/Winter 全部默认关闭）",
)
FUNNEL_STAGES_EN = (
    "6 improvement candidates entered evaluation",
    "4 failed on real input / formal M2 gates",
    "2 retired or gained no pruning",
    "0 enabled (all candidates default-off)",
)
FUNNEL_COUNTS = (6, 4, 2, 0)


def _fig_funnel(english: bool, out: Path) -> None:
    """The "reverse argument" funnel: all candidates failed, incumbent stands.

    Does not depend on the CSV; the data are the candidate gate outcomes from
    the SSOT.  The annotation is deliberately conservative -- "not overtaken",
    never "optimal" or "production-grade advantage".
    """
    fig, ax = plt.subplots(figsize=(8, 4.6))
    stages = FUNNEL_STAGES_EN if english else FUNNEL_STAGES_ZH
    counts = FUNNEL_COUNTS
    # Funnel shape: each stage half the width of the previous.
    half_widths = [3.0, 2.2, 1.5, 0.8]
    center_x = 0.0
    y_positions = [0.0, -1.0, -2.0, -3.0]
    colors = ["#4C72B0", "#DD8452", "#CC5A4A", "#2F8F4F"]

    for y, half, count, stage, color in zip(
        y_positions, half_widths, counts, stages, colors, strict=True
    ):
        left = center_x - half
        width = 2 * half
        ax.barh(
            y,
            width,
            left=left,
            height=0.8,
            color=color,
            edgecolor="white",
            alpha=0.9,
        )
        count_text = f"{count}" if english else f"{count} 个"
        ax.text(
            center_x,
            y,
            count_text,
            ha="center",
            va="center",
            fontsize=18,
            fontweight="bold",
            color="white",
        )
        ax.text(
            center_x,
            y - 0.55,
            stage,
            ha="center",
            va="center",
            fontsize=11,
            color="#333333",
        )
    ax.set_xlim(-3.6, 3.6)
    ax.set_ylim(-4.0, 0.8)
    ax.axis("off")
    ax.set_title(
        "Candidate gate funnel: the incumbent was never overtaken"
        if english
        else "改进候选门禁漏斗：当前实现从未被超越",
        fontsize=13,
        pad=14,
    )
    annotation = (
        "All improvement candidates failed the correctness-first gates; "
        "the production default (time-dependent A*) remained unchanged."
        if english
        else "全部改进候选未通过正确性优先门禁；生产默认（时间依赖 A*）保持不变。"
    )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
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
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = _read_csv(args.csv)
    if not rows:
        raise SystemExit(f"no rows in {args.csv}")

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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
