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
    fig, ax = plt.subplots(figsize=(9.5, 12.5))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 13)
    ax.axis("off")
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")

    # Title band.
    title_zh = "图4-1 动态风险预测与风险约束自主规划总体技术框架"
    title_en = (
        "Fig.4-1 Overall framework: dynamic risk prediction and "
        "risk-constrained autonomous planning"
    )
    ax.text(
        5,
        12.5,
        title_en if english else title_zh,
        ha="center",
        va="center",
        fontsize=13,
        fontweight="bold",
        color="#0f2a4f",
    )

    # Main vertical chain (centred at x=3.5).
    chain_x = 2.2
    chain_w = 4.6
    block_h = 1.05
    gap = 0.45
    y_top = 11.4
    ys = []
    y = y_top
    for _ in MAIN_CHAIN:
        ys.append(y)
        y -= block_h + gap
    for i, (zh, en) in enumerate(MAIN_CHAIN):
        _draw_block(ax, chain_x, ys[i], chain_w, block_h, en if english else zh)
        if i < len(MAIN_CHAIN) - 1:
            _draw_arrow(
                ax,
                chain_x + chain_w / 2,
                ys[i],
                chain_x + chain_w / 2,
                ys[i + 1] + block_h,
            )

    # Side feedback loop (right column, x=7.4).
    fb_x = 7.0
    fb_w = 2.6
    fb_h = 0.9
    fb_y_top = 9.6
    fb_gap = 0.35
    fb_ys = []
    fy = fb_y_top
    for _ in FEEDBACK:
        fb_ys.append(fy)
        fy -= fb_h + fb_gap
    for i, (zh, en) in enumerate(FEEDBACK):
        _draw_block(
            ax,
            fb_x,
            fb_ys[i],
            fb_w,
            fb_h,
            en if english else zh,
            color="#a02020",
            face="#fbeaea",
        )
        if i < len(FEEDBACK) - 1:
            _draw_arrow(
                ax,
                fb_x + fb_w / 2,
                fb_ys[i],
                fb_x + fb_w / 2,
                fb_ys[i + 1] + fb_h,
                color="#a02020",
            )

    # Link the main chain to the feedback loop and back.
    # From "risk prediction model" (index 2) to "error feedback" (index 0).
    src_y = ys[2] + block_h / 2
    dst_y = fb_ys[0] + fb_h / 2
    _draw_arrow(
        ax,
        chain_x + chain_w,
        src_y,
        fb_x,
        dst_y,
        color="#a02020",
        style="-|>",
    )
    # From "planner parameter correction" (fb index 2) back to
    # "risk-constrained route planning" (main index 4).
    src_y_fb = fb_ys[2] + fb_h / 2
    dst_y_main = ys[4] + block_h / 2
    _draw_arrow(
        ax,
        fb_x,
        src_y_fb,
        chain_x + chain_w,
        dst_y_main,
        color="#a02020",
        style="-|>",
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
