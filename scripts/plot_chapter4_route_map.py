#!/usr/bin/env python3
"""Render the Chapter 4 route/replanning map from an immutable Viewer bundle.

The script is deliberately presentation-only: it exports coordinates, risk
cells and provenance exactly as published by the retrospective Viewer bundle.
It never invokes the planner and never recomputes risk values.

Run with::

    uv run --with matplotlib python scripts/plot_chapter4_route_map.py
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PACKAGE = (
    REPO_ROOT.parent
    / "work_package_d"
    / "output"
    / "formal-motion-original-dynamic-viewer-package-v1"
)
DEFAULT_BUNDLE = DEFAULT_PACKAGE / "bundle.json"
DEFAULT_BASEMAP = DEFAULT_PACKAGE / "gebco_basemap.png"
DEFAULT_OUTPUT_DIR = REPO_ROOT.parent / ".runtime" / "experiments" / "c-chapter4-route-map"

EXPECTED_BUNDLE_SHA256 = "7d513b40f0d82e6a31fc1dca24928f84eb7b55f5c3268dc297524e2061d8fd4f"
EXPECTED_BASEMAP_SHA256 = "924e8eea16eb5d850c74fb1306d7af9c2d5047f23ae803472487ae69f75f6ada"
EXPECTED_SCENARIO = "tromso_isfjorden_february_2026_research_v1"
EXPECTED_CORRIDOR = "tromso_to_isfjorden_outer"
DECISION_TIME = "2026-02-15T12:00:00Z"
ADOPTION_TIME = "2026-02-15T14:17:23.884922Z"
TARGET_REVISION = 3
LOCAL_EXTENT = (16.8, 18.7, 72.0, 73.5)


class EvidenceError(ValueError):
    """Raised when the immutable source does not match the figure contract."""


@dataclass(frozen=True)
class RouteMapEvidence:
    """Validated, source-native values needed by the route map."""

    bundle: dict[str, Any]
    risk_frame: dict[str, Any]
    latitudes: tuple[float, ...]
    longitudes: tuple[float, ...]
    completed_track: tuple[dict[str, Any], ...]
    superseded_route: tuple[dict[str, Any], ...]
    adopted_route: tuple[dict[str, Any], ...]
    decision_position: tuple[float, float]
    adoption_position: tuple[float, float]
    source_timeline_time: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _xy(point: dict[str, Any]) -> tuple[float, float]:
    lon = point.get("longitude", point.get("lon"))
    lat = point.get("latitude", point.get("lat"))
    _require(lon is not None and lat is not None, "route point is missing longitude/latitude")
    return float(lon), float(lat)


def _same_point(left: dict[str, Any], right: dict[str, Any], tolerance: float = 1e-9) -> bool:
    left_lon, left_lat = _xy(left)
    right_lon, right_lat = _xy(right)
    return math.isclose(left_lon, right_lon, abs_tol=tolerance) and math.isclose(
        left_lat, right_lat, abs_tol=tolerance
    )


def _suffix_from_point(
    points: list[dict[str, Any]], start: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    for index, point in enumerate(points):
        if _same_point(point, start):
            return tuple(points[index:])
    raise EvidenceError("superseded route does not contain the R3 adoption point")


def extract_evidence(bundle: dict[str, Any]) -> RouteMapEvidence:
    """Validate and extract the source-native R2/R3 replanning evidence."""

    replay = bundle.get("replay", {})
    risk = bundle.get("risk", {})
    risk_source = risk.get("source", {})
    basemap = bundle.get("basemap", {})
    _require(replay.get("scenario_id") == EXPECTED_SCENARIO, "unexpected replay scenario")
    _require(risk_source.get("scenario_id") == EXPECTED_SCENARIO, "risk scenario mismatch")
    _require(risk_source.get("corridor_id") == EXPECTED_CORRIDOR, "risk corridor mismatch")
    _require(
        replay.get("scenario_mode") == "research_navigation_simulation",
        "source is not the governed research navigation simulation",
    )
    _require(basemap.get("projection") == "EPSG:4326", "basemap projection must be EPSG:4326")

    grid = risk.get("grid", {})
    rows, cols = grid.get("rows"), grid.get("cols")
    _require((rows, cols) == (31, 11), "risk grid must be 31 x 11")
    frames = [frame for frame in risk.get("frames", []) if frame.get("valid_time") == DECISION_TIME]
    _require(len(frames) == 1, "decision-time risk frame is missing or duplicated")
    frame = frames[0]
    coordinates = frame.get("coordinates", {})
    latitudes = tuple(float(value) for value in coordinates.get("latitude", []))
    longitudes = tuple(float(value) for value in coordinates.get("longitude", []))
    _require(len(latitudes) == rows, "risk latitude coordinates do not match row count")
    _require(len(longitudes) == cols, "risk longitude coordinates do not match column count")
    cell_count = rows * cols
    for key in ("risk_scores", "risk_levels", "hard_mask", "hard_reasons", "confidences"):
        _require(len(frame.get(key, [])) == cell_count, f"{key} must contain {cell_count} cells")

    routes = {route.get("revision"): route for route in bundle.get("routes", [])}
    _require(2 in routes and TARGET_REVISION in routes, "R2/R3 routes are required")
    r3 = routes[TARGET_REVISION]
    _require(r3.get("decision_time") == DECISION_TIME, "R3 decision time changed")
    _require(r3.get("effective_adoption_time") == ADOPTION_TIME, "R3 adoption time changed")
    _require(r3.get("adoption_mode") == "NEXT_WAYPOINT_DEFERRED", "R3 must use deferred adoption")

    events = bundle.get("events", [])
    expected_events = {
        ("REPLAN_DECIDED", DECISION_TIME),
        ("REPLAN_ADOPTED", ADOPTION_TIME),
        ("ROUTE_CHANGED", ADOPTION_TIME),
    }
    observed_events = {
        (event.get("type"), event.get("t"))
        for event in events
        if event.get("rev") == str(TARGET_REVISION) and event.get("observed") is True
    }
    _require(expected_events <= observed_events, "R3 decision/adoption event chain is incomplete")

    decision_states = [
        item for item in bundle.get("timeline", []) if item.get("t") == DECISION_TIME
    ]
    _require(len(decision_states) == 1, "decision-time vessel state is missing or duplicated")
    decision_state = decision_states[0]
    _require(
        decision_state.get("arv") == 2
        and decision_state.get("prv") == TARGET_REVISION
        and decision_state.get("prs") == "PENDING",
        "timeline does not show R2 active and R3 pending at decision time",
    )
    decision_position = _xy(decision_state.get("v", {}))

    adoption_states = [
        item
        for item in bundle.get("timeline", [])
        if item.get("t", "") >= ADOPTION_TIME
        and item.get("arv") == TARGET_REVISION
        and item.get("track")
        and item.get("superseded")
    ]
    _require(adoption_states, "post-adoption timeline evidence is missing")
    adoption_state = adoption_states[0]
    completed_track = tuple(adoption_state["track"])
    adopted_route = tuple(r3.get("waypoints", []))
    _require(completed_track and adopted_route, "completed or adopted route is empty")
    _require(
        _same_point(completed_track[-1], adopted_route[0]), "track does not end at R3 adoption"
    )
    adoption_position = _xy(adopted_route[0])

    superseded_route = _suffix_from_point(adoption_state["superseded"], adopted_route[0])
    _require(_same_point(superseded_route[-1], adopted_route[-1]), "R2 and R3 endpoints differ")
    superseded_xy = tuple(_xy(point) for point in superseded_route)
    adopted_xy = tuple(_xy(point) for point in adopted_route)
    _require(superseded_xy != adopted_xy, "R3 does not differ from the superseded R2 route")

    return RouteMapEvidence(
        bundle=bundle,
        risk_frame=frame,
        latitudes=latitudes,
        longitudes=longitudes,
        completed_track=completed_track,
        superseded_route=superseded_route,
        adopted_route=adopted_route,
        decision_position=decision_position,
        adoption_position=adoption_position,
        source_timeline_time=adoption_state["t"],
    )


def load_evidence(bundle_path: Path, basemap_path: Path) -> RouteMapEvidence:
    """Load only the pinned Viewer package and reject any digest drift."""

    _require(bundle_path.is_file(), f"bundle not found: {bundle_path}")
    _require(basemap_path.is_file(), f"basemap not found: {basemap_path}")
    _require(_sha256(bundle_path) == EXPECTED_BUNDLE_SHA256, "bundle SHA256 does not match pin")
    _require(_sha256(basemap_path) == EXPECTED_BASEMAP_SHA256, "basemap SHA256 does not match pin")
    with bundle_path.open(encoding="utf-8") as handle:
        bundle = json.load(handle)
    return extract_evidence(bundle)


def _write_route_csv(path: Path, evidence: RouteMapEvidence) -> None:
    columns = ("route_role", "revision", "sequence", "longitude", "latitude", "eta")
    route_sets = (
        ("completed_track", "R1-R2", evidence.completed_track),
        ("superseded_route", "R2", evidence.superseded_route),
        ("adopted_route", "R3", evidence.adopted_route),
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for role, revision, points in route_sets:
            for sequence, point in enumerate(points):
                lon, lat = _xy(point)
                writer.writerow(
                    {
                        "route_role": role,
                        "revision": revision,
                        "sequence": sequence,
                        "longitude": f"{lon:.12g}",
                        "latitude": f"{lat:.12g}",
                        "eta": point.get("eta", ""),
                    }
                )


def _write_risk_csv(path: Path, evidence: RouteMapEvidence) -> None:
    frame = evidence.risk_frame
    columns = (
        "row",
        "column",
        "longitude",
        "latitude",
        "risk_score",
        "risk_level",
        "hard_mask",
        "hard_reason",
        "confidence",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        cols = len(evidence.longitudes)
        for row, latitude in enumerate(evidence.latitudes):
            for column, longitude in enumerate(evidence.longitudes):
                index = row * cols + column
                writer.writerow(
                    {
                        "row": row,
                        "column": column,
                        "longitude": f"{longitude:.12g}",
                        "latitude": f"{latitude:.12g}",
                        "risk_score": frame["risk_scores"][index],
                        "risk_level": frame["risk_levels"][index],
                        "hard_mask": frame["hard_mask"][index],
                        "hard_reason": frame["hard_reasons"][index],
                        "confidence": frame["confidences"][index],
                    }
                )


def write_evidence_tables(output_dir: Path, evidence: RouteMapEvidence) -> tuple[Path, Path]:
    """Export editable source tables without changing source values."""

    output_dir.mkdir(parents=True, exist_ok=True)
    route_csv = output_dir / "route-overlay.csv"
    risk_csv = output_dir / "risk-frame-20260215T120000Z.csv"
    _write_route_csv(route_csv, evidence)
    _write_risk_csv(risk_csv, evidence)
    return route_csv, risk_csv


def _coordinate_edges(values: tuple[float, ...]) -> list[float]:
    _require(len(values) >= 2, "at least two coordinates are required")
    midpoints = [(left + right) / 2 for left, right in pairwise(values)]
    return [
        values[0] - (midpoints[0] - values[0]),
        *midpoints,
        values[-1] + (values[-1] - midpoints[-1]),
    ]


def render_figure(
    basemap_path: Path, output_dir: Path, evidence: RouteMapEvidence
) -> tuple[Path, Path]:
    """Render the single-map academic figure and its local replanning inset."""

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.image as mpimg
    import matplotlib.patheffects as path_effects
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib import font_manager
    from matplotlib.lines import Line2D
    from matplotlib.patches import ConnectionPatch, Patch, Rectangle
    from matplotlib.ticker import FuncFormatter

    candidates = (
        "Noto Sans CJK SC",
        "Noto Serif CJK SC",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "SimHei",
        "Microsoft YaHei",
    )
    available = {font.name for font in font_manager.fontManager.ttflist}
    font = next((candidate for candidate in candidates if candidate in available), "DejaVu Sans")
    plt.rcParams.update(
        {
            "font.sans-serif": [font, "DejaVu Sans"],
            "axes.unicode_minus": False,
            "figure.dpi": 120,
            "savefig.dpi": 300,
            "svg.fonttype": "none",
        }
    )

    rows = len(evidence.latitudes)
    cols = len(evidence.longitudes)
    lat_edges = np.asarray(_coordinate_edges(evidence.latitudes))
    lon_edges = np.asarray(_coordinate_edges(evidence.longitudes))
    frame = evidence.risk_frame
    risks = np.asarray(frame["risk_scores"], dtype=float).reshape(rows, cols)
    hard = np.asarray(frame["hard_mask"], dtype=bool).reshape(rows, cols)
    reasons = np.asarray(frame["hard_reasons"], dtype=object).reshape(rows, cols)
    risk_visible = np.ma.masked_where(hard | ~np.isfinite(risks), risks)
    basemap = evidence.bundle["basemap"]
    bbox = basemap["bbox"]
    extent = (bbox["min_lon"], bbox["max_lon"], bbox["min_lat"], bbox["max_lat"])
    background = mpimg.imread(basemap_path)

    # Keep the source canvas close to the 14 cm Word insertion width so that
    # text remains readable after Word scales the bitmap.
    fig = plt.figure(figsize=(7.8, 5.25), facecolor="white")
    grid = fig.add_gridspec(
        2,
        4,
        width_ratios=(1.0, 1.0, 1.0, 1.18),
        height_ratios=(1.0, 1.1),
        left=0.07,
        right=0.98,
        bottom=0.12,
        top=0.93,
        wspace=0.32,
        hspace=0.30,
    )
    main = fig.add_subplot(grid[:, :3])
    inset = fig.add_subplot(grid[0, 3])
    legend_ax = fig.add_subplot(grid[1, 3])

    def draw_background(axis: Any) -> Any:
        axis.imshow(background, extent=extent, origin="upper", alpha=0.55, zorder=0)
        mesh = axis.pcolormesh(
            lon_edges,
            lat_edges,
            risk_visible,
            cmap="YlOrRd",
            vmin=0.05,
            vmax=0.40,
            shading="flat",
            alpha=0.68,
            zorder=1,
        )
        for row in range(rows):
            for col in range(cols):
                x = lon_edges[col]
                y = lat_edges[row]
                width = lon_edges[col + 1] - x
                height = lat_edges[row + 1] - y
                if reasons[row, col] == "LAND":
                    axis.add_patch(
                        Rectangle(
                            (x, y),
                            width,
                            height,
                            facecolor="#4b5563",
                            edgecolor="none",
                            alpha=0.78,
                            zorder=2,
                        )
                    )
                elif reasons[row, col] == "DATA_UNAVAILABLE":
                    axis.add_patch(
                        Rectangle(
                            (x, y),
                            width,
                            height,
                            facecolor=(1, 1, 1, 0.18),
                            edgecolor="#6b7280",
                            linewidth=0.0,
                            hatch="////",
                            zorder=2,
                        )
                    )
        return mesh

    mesh = draw_background(main)
    draw_background(inset)

    line_effect = [path_effects.Stroke(linewidth=4.8, foreground="white"), path_effects.Normal()]

    def draw_routes(axis: Any, *, labels: bool) -> None:
        route_specs = (
            (evidence.completed_track, "#16a34a", "-", 2.8, "已航行轨迹"),
            (evidence.superseded_route, "#6b7280", "--", 2.4, "被替代航线 R2"),
            (evidence.adopted_route, "#2563eb", "-", 2.8, "生效航线 R3"),
        )
        for points, color, linestyle, width, label in route_specs:
            coordinates = [_xy(point) for point in points]
            (line,) = axis.plot(
                [point[0] for point in coordinates],
                [point[1] for point in coordinates],
                color=color,
                linestyle=linestyle,
                linewidth=width,
                label=label if labels else None,
                zorder=6,
            )
            line.set_path_effects(line_effect)

        decision_lon, decision_lat = evidence.decision_position
        adoption_lon, adoption_lat = evidence.adoption_position
        axis.scatter(
            [decision_lon],
            [decision_lat],
            marker="D",
            s=58,
            color="#f59e0b",
            edgecolor="white",
            linewidth=0.9,
            zorder=9,
        )
        axis.scatter(
            [adoption_lon],
            [adoption_lat],
            marker="o",
            s=78,
            facecolor="white",
            edgecolor="#0f4c81",
            linewidth=2.0,
            zorder=9,
        )

    draw_routes(main, labels=True)
    draw_routes(inset, labels=False)

    start = _xy(evidence.completed_track[0])
    end = _xy(evidence.adopted_route[-1])
    main.scatter(
        *start, marker="*", s=170, color="#16a34a", edgecolor="white", linewidth=1.0, zorder=10
    )
    main.scatter(
        *end, marker="*", s=170, color="#dc2626", edgecolor="white", linewidth=1.0, zorder=10
    )
    annotation_box = {
        "boxstyle": "round,pad=0.22",
        "facecolor": "white",
        "edgecolor": "#9ca3af",
        "alpha": 0.92,
    }
    main.annotate(
        "起点\nTromsø外海",
        start,
        xytext=(8, -5),
        textcoords="offset points",
        fontsize=10,
        bbox=annotation_box,
    )
    main.annotate(
        "终点\nIsfjorden外缘",
        end,
        xytext=(8, -3),
        textcoords="offset points",
        fontsize=10,
        bbox=annotation_box,
    )

    decision_lon, decision_lat = evidence.decision_position
    adoption_lon, adoption_lat = evidence.adoption_position
    inset.annotate(
        "重规划决策\n12:00 UTC",
        (decision_lon, decision_lat),
        xytext=(-62, -35),
        textcoords="offset points",
        fontsize=9.5,
        arrowprops={"arrowstyle": "->", "color": "#b45309", "lw": 0.9},
        bbox=annotation_box,
    )
    inset.annotate(
        "R3生效\n14:17 UTC",
        (adoption_lon, adoption_lat),
        xytext=(-64, 31),
        textcoords="offset points",
        fontsize=9.5,
        arrowprops={"arrowstyle": "->", "color": "#0f4c81", "lw": 0.9},
        bbox=annotation_box,
    )

    main.set_xlim(extent[0], extent[1])
    main.set_ylim(extent[2], extent[3])
    inset.set_xlim(LOCAL_EXTENT[0], LOCAL_EXTENT[1])
    inset.set_ylim(LOCAL_EXTENT[2], LOCAL_EXTENT[3])
    main.set_title("决策时刻风险场与动态重规划航线", fontsize=14, fontweight="bold", pad=10)
    inset.set_title("重规划局部放大", fontsize=11.5, fontweight="bold", pad=6)

    degree_east = FuncFormatter(lambda value, _position: f"{value:g}°E")
    degree_north = FuncFormatter(lambda value, _position: f"{value:g}°N")
    for axis in (main, inset):
        axis.xaxis.set_major_formatter(degree_east)
        axis.yaxis.set_major_formatter(degree_north)
        axis.grid(color="#93c5fd", linestyle=":", linewidth=0.65, alpha=0.8)
        axis.tick_params(labelsize=9.5)
        for spine in axis.spines.values():
            spine.set_color("#1f2937")
            spine.set_linewidth(0.8)
    main.set_xlabel("经度", fontsize=11)
    main.set_ylabel("纬度", fontsize=11)
    inset.tick_params(axis="x", labelrotation=25)

    main.add_patch(
        Rectangle(
            (LOCAL_EXTENT[0], LOCAL_EXTENT[2]),
            LOCAL_EXTENT[1] - LOCAL_EXTENT[0],
            LOCAL_EXTENT[3] - LOCAL_EXTENT[2],
            fill=False,
            edgecolor="#0f4c81",
            linestyle="--",
            linewidth=1.2,
            zorder=8,
        )
    )
    fig.add_artist(
        ConnectionPatch(
            xyA=(LOCAL_EXTENT[1], LOCAL_EXTENT[3]),
            coordsA=main.transData,
            xyB=(LOCAL_EXTENT[0], LOCAL_EXTENT[3]),
            coordsB=inset.transData,
            color="#0f4c81",
            linestyle=":",
            linewidth=0.9,
        )
    )
    fig.add_artist(
        ConnectionPatch(
            xyA=(LOCAL_EXTENT[1], LOCAL_EXTENT[2]),
            coordsA=main.transData,
            xyB=(LOCAL_EXTENT[0], LOCAL_EXTENT[2]),
            coordsB=inset.transData,
            color="#0f4c81",
            linestyle=":",
            linewidth=0.9,
        )
    )

    legend_ax.axis("off")
    handles = [
        Line2D([0], [0], color="#16a34a", lw=2.8, label="已航行轨迹"),
        Line2D([0], [0], color="#6b7280", lw=2.4, ls="--", label="被替代航线 R2"),
        Line2D([0], [0], color="#2563eb", lw=2.8, label="生效航线 R3"),
        Line2D(
            [0],
            [0],
            marker="*",
            color="none",
            markerfacecolor="#16a34a",
            markeredgecolor="white",
            markersize=12,
            label="起点",
        ),
        Line2D(
            [0],
            [0],
            marker="*",
            color="none",
            markerfacecolor="#dc2626",
            markeredgecolor="white",
            markersize=12,
            label="终点",
        ),
        Line2D(
            [0],
            [0],
            marker="D",
            color="none",
            markerfacecolor="#f59e0b",
            markeredgecolor="white",
            markersize=7,
            label="重规划决策点",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="white",
            markeredgecolor="#0f4c81",
            markersize=8,
            label="重规划生效点",
        ),
        Patch(facecolor="#4b5563", edgecolor="none", alpha=0.78, label="陆地"),
        Patch(facecolor="white", edgecolor="#6b7280", hatch="////", label="数据不可用"),
    ]
    legend_ax.legend(
        handles=handles,
        loc="upper left",
        frameon=False,
        fontsize=9,
        handlelength=2.8,
        handletextpad=0.6,
        labelspacing=0.22,
        borderaxespad=0,
    )
    legend_ax.text(
        0.0,
        0.01,
        "风险场：2026-02-15 12:00 UTC\nR3于14:17延迟生效 · 回顾性动态回放",
        transform=legend_ax.transAxes,
        fontsize=8.4,
        color="#374151",
        linespacing=1.25,
        bbox={"boxstyle": "round,pad=0.38", "facecolor": "#eff6ff", "edgecolor": "#93c5fd"},
    )

    colorbar = fig.colorbar(
        mesh, ax=main, orientation="horizontal", fraction=0.046, pad=0.075, aspect=35
    )
    colorbar.set_label("风险分值（2026-02-15 12:00 UTC）", fontsize=10.5)
    colorbar.ax.tick_params(labelsize=9)

    output_dir.mkdir(parents=True, exist_ok=True)
    png = output_dir / "fig-route-replanning-map.png"
    svg = output_dir / "fig-route-replanning-map.svg"
    fig.savefig(png, dpi=300, bbox_inches="tight", facecolor="white")
    fig.savefig(svg, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return png, svg


def _write_metadata(
    path: Path,
    bundle_path: Path,
    basemap_path: Path,
    evidence: RouteMapEvidence,
    outputs: tuple[Path, ...],
) -> None:
    bundle = evidence.bundle
    frame = evidence.risk_frame
    metadata = {
        "schema_version": "chapter4.route-map-evidence.v1",
        "evidence_kind": "RETROSPECTIVE_DYNAMIC_REPLAY_PRESENTATION",
        "qualification_boundary": "NOT_LIVE_CAUSAL_OR_NAVIGATION_GRADE",
        "source": {
            "bundle": str(bundle_path.resolve()),
            "bundle_sha256": EXPECTED_BUNDLE_SHA256,
            "basemap": str(basemap_path.resolve()),
            "basemap_sha256": EXPECTED_BASEMAP_SHA256,
            "replay_id": bundle["replay"]["replay_id"],
            "scenario_id": EXPECTED_SCENARIO,
            "corridor_id": EXPECTED_CORRIDOR,
            "risk_window_id": bundle["risk"]["source"]["risk_window_id"],
            "risk_frame_id": frame["risk_id"],
            "timeline_time": evidence.source_timeline_time,
        },
        "replanning": {
            "superseded_revision": "R2",
            "adopted_revision": "R3",
            "decision_time": DECISION_TIME,
            "effective_adoption_time": ADOPTION_TIME,
            "adoption_mode": "NEXT_WAYPOINT_DEFERRED",
            "decision_position": {
                "longitude": evidence.decision_position[0],
                "latitude": evidence.decision_position[1],
            },
            "adoption_position": {
                "longitude": evidence.adoption_position[0],
                "latitude": evidence.adoption_position[1],
            },
        },
        "grid": {
            "projection": "EPSG:4326",
            "rows": len(evidence.latitudes),
            "columns": len(evidence.longitudes),
            "cells": len(evidence.latitudes) * len(evidence.longitudes),
        },
        "figure": {
            "local_extent": list(LOCAL_EXTENT),
            "risk_colormap": "YlOrRd",
            "risk_range": [0.05, 0.40],
            "route_points": {
                "completed_track": len(evidence.completed_track),
                "superseded_route": len(evidence.superseded_route),
                "adopted_route": len(evidence.adopted_route),
            },
        },
        "outputs": {item.name: _sha256(item) for item in outputs},
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--basemap", type=Path, default=DEFAULT_BASEMAP)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = load_evidence(args.bundle, args.basemap)
        route_csv, risk_csv = write_evidence_tables(args.output_dir, evidence)
        png, svg = render_figure(args.basemap, args.output_dir, evidence)
        metadata = args.output_dir / "metadata.json"
        _write_metadata(
            metadata,
            args.bundle,
            args.basemap,
            evidence,
            (png, svg, route_csv, risk_csv),
        )
    except (EvidenceError, json.JSONDecodeError, OSError) as exc:
        print(f"route-map generation failed: {exc}", file=sys.stderr)
        return 2
    for item in (png, svg, route_csv, risk_csv, metadata):
        print(f"wrote {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
