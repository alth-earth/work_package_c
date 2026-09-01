#!/usr/bin/env python3
"""Chapter 4 overall technical framework diagram.

A single-page, hand-drawn-style architecture figure following the reference
layout the user supplied (white background, dark-bordered rounded boxes, blue
flow arrows on the main vertical chain, red feedback loop on the side).

The diagram is intentionally descriptive -- it shows the *planning* pipeline
of work package C and does not claim any algorithmic novelty beyond what
``ALGORITHM_COMPARISON_REPORT.md`` already documents.

Renders a Chinese (paper main) and an English (optional) variant, PNG 300 dpi
+ SVG.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

CJK_FONT_CANDIDATES = (
    "Noto Sans CJK SC",
    "Noto Serif CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "SimHei",
    "Microsoft YaHei",
    "PingFang SC",
)


def _select_font(english: bool) -> str:
    if english:
        return "DejaVu Sans"
    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in CJK_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return "DejaVu Sans"


def _configure(english: bool, font: str) -> None:
    plt.rcParams["font.sans-serif"] = [font, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["savefig.dpi"] = 300
    plt.rcParams["savefig.bbox"] = "tight"


# (zh, en) labels for each block in the main vertical chain.
MAIN_CHAIN = [
    ("多源环境与状态输入", "Multi-source env & state inputs"),
    ("时空状态构建", "Spatio-temporal state construction"),
    ("风险预测模型", "Risk prediction model"),
    ("风险代价场", "Risk cost field"),
    ("风险约束航迹规划", "Risk-constrained route planning"),
    ("在线重规划与控制接口", "Online replanning & control interface"),
    ("仿真/实测验证", "Simulation / field validation"),
]

# Side feedback loop boxes.
FEEDBACK = [
    ("误差反馈", "Error feedback"),
    ("风险场更新", "Risk field update"),
    ("规划参数修正", "Planner parameter correction"),
]


def _draw_block(ax, x, y, w, h, text, *, color="#1f3a68", face="#eef3fb"):
    """Draw a rounded box with a dark border and a light fill."""
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.02,rounding_size=0.18",
        linewidth=1.6,
        edgecolor=color,
        facecolor=face,
    )
    ax.add_patch(box)
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=11.5,
        color="#1a1a1a",
        wrap=True,
    )


def _draw_arrow(ax, x1, y1, x2, y2, *, color="#1f3a68", style="-|>"):
    arrow = FancyArrowPatch(
        (x1, y1),
        (x2, y2),
        arrowstyle=style,
        mutation_scale=18,
        linewidth=1.8,
        color=color,
    )
    ax.add_patch(arrow)


def _draw_framework(english: bool, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(10.0, 7.2))
    ax.set_xlim(0, 13)
    ax.set_ylim(0, 10.2)
    ax.axis("off")
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")

    title_zh = "风险约束时变航路规划技术框架"
    title_en = "Technical framework for risk-constrained time-dependent route planning"
    ax.text(
        6.7,
        9.75,
        title_en if english else title_zh,
        ha="center",
        va="center",
        fontsize=13.5,
        fontweight="bold",
        color="#0f2a4f",
    )

    rows = (
        (
            "数据输入",
            "Inputs",
            7.65,
            ("时变风险场", "Time-varying risk field"),
            ("船舶性能模型", "Vessel performance model"),
            ("航行任务与可航约束", "Mission and navigability constraints"),
        ),
        (
            "边代价建模",
            "Edge-cost model",
            5.55,
            ("时间展开状态", "Time-expanded state"),
            ("风险—速度—ETA耦合", "Risk-speed-ETA coupling"),
            ("全局失效保护约束", "Global fail-closed constraints"),
        ),
        (
            "航路规划",
            "Route planning",
            3.45,
            ("时间依赖 A* 搜索", "Time-dependent A* search"),
            ("最快 / 低风险 / 推荐", "Fastest / low-risk / recommended"),
            ("全程 / 走廊 / 滚动 / 执行", "Voyage / corridor / rolling / executable"),
        ),
        (
            "验证与输出",
            "Validation & output",
            1.35,
            ("同图基线与消融验证", "Same-graph baselines and ablations"),
            ("事件触发动态重规划", "Event-triggered dynamic replanning"),
            ("12 条航路与 ETA/风险摘要", "12 routes with ETA/risk summaries"),
        ),
    )
    label_x, label_w = 0.18, 1.42
    content_x, content_w = 1.85, 10.70
    box_w, box_h, gap = 3.05, 1.05, 0.48
    faces = ("#EAF2FD", "#E8F7F5", "#EDF7E8", "#FFF4D6")
    for row_index, row in enumerate(rows):
        zh_label, en_label, y, *blocks = row
        label_box = FancyBboxPatch(
            (label_x, y),
            label_w,
            box_h,
            boxstyle="round,pad=0.02,rounding_size=0.10",
            linewidth=1.2,
            edgecolor="#3C78B5",
            facecolor="#EFF5FD",
        )
        ax.add_patch(label_box)
        ax.text(
            label_x + label_w / 2,
            y + box_h / 2,
            en_label if english else zh_label,
            ha="center",
            va="center",
            fontsize=8.7,
            fontweight="bold",
            color="#17365D",
            wrap=True,
        )
        group = FancyBboxPatch(
            (content_x - 0.15, y - 0.15),
            content_w,
            box_h + 0.30,
            boxstyle="round,pad=0.02,rounding_size=0.08",
            linewidth=1.0,
            linestyle=(0, (4, 3)) if row_index in (1, 2) else "solid",
            edgecolor="#5B9BD5",
            facecolor="white",
        )
        ax.add_patch(group)
        for block_index, (zh, en) in enumerate(blocks):
            x = content_x + block_index * (box_w + gap)
            _draw_block(
                ax,
                x,
                y,
                box_w,
                box_h,
                en if english else zh,
                color="#3C78B5",
                face=faces[row_index],
            )
            if block_index < 2:
                _draw_arrow(
                    ax,
                    x + box_w,
                    y + box_h / 2,
                    x + box_w + gap - 0.08,
                    y + box_h / 2,
                    color="#2F6FB0",
                )
        if row_index < len(rows) - 1:
            _draw_arrow(
                ax,
                content_x + content_w / 2 - 0.15,
                y - 0.15,
                content_x + content_w / 2 - 0.15,
                rows[row_index + 1][2] + box_h + 0.15,
                color="#2F6FB0",
            )

    # Validation and replanning feed back into state and edge-cost construction.
    feedback = FancyArrowPatch(
        (8.25, 1.32),
        (4.8, 6.62),
        connectionstyle="arc3,rad=-0.28",
        arrowstyle="-|>",
        mutation_scale=16,
        linewidth=1.5,
        linestyle=(0, (4, 3)),
        color="#A73535",
    )
    ax.add_patch(feedback)
    ax.text(
        10.9,
        5.05,
        "Replanning feedback" if english else "重规划反馈",
        ha="center",
        va="center",
        fontsize=9,
        color="#A73535",
    )

    fig_path = out / ("framework.png" if english else "fig-framework.png")
    fig.savefig(fig_path)
    fig.savefig(fig_path.with_suffix(".svg"))
    plt.close(fig)
    print(f"wrote {fig_path} and {fig_path.with_suffix('.svg')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--english", action="store_true")
    parser.add_argument("--both", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

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
        _draw_framework(english, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
