"""
utils/mapping.py
=================
Shared adaptive label placement AND marker display for admin-unit maps
(02_ghi_distribution.py, 03_optimal_zones.py), plus JSON-driven placement
for the north arrow and stats box annotations.

Centralized here so all scripts share the exact same collision-avoidance
logic and configuration schema, instead of copy-pasted, unfiltered
ax.text() / gdf.plot() calls drifting apart.

Design goal: EVERYTHING tunable lives in config/<country>.json under
"plot.labels" and "plot.annotations" - nothing country-specific is
hard-coded here. This allows the same scripts to handle countries with
different numbers of administrative units, from small territories such as
Réunion (24 communes) and Madagascar (22 ADM2 units) to larger datasets such
as Kenya (300 ADM2 units), without modifying the .py files.

config/<country>.json - "plot.labels" schema
----------------------------------------------
{
  "enabled": true,                  # bool, default True
  "max_labels": 30,                 # int, hard cap on labels drawn
  "min_distance_m": "auto",         # float in map units, or "auto"
  "min_candidate_area_km2": 0,      # float, pre-filter: ignore polygons
                                     # smaller than this before ranking.
                                     # Critical for large admin datasets
                                     # (e.g. Madagascar adm2, ~1600 units)
                                     # to keep candidate pool manageable.
  "placement": "representative_point",  # "representative_point" | "centroid"
  "style": {
    "fontsize": 6.5,
    "color": "navy",
    "halo_color": "white",
    "halo_width": 2.0,
    "fontweight": "bold"
  },
  "markers": {
    "show": "labeled_only",         # "all" | "labeled_only" | "none"
    "color": "blue",
    "alpha": 0.6,
    "size": 60,
    "edgecolor": "white"
  }
}

config/<country>.json - "plot.annotations" schema
----------------------------------------------------
{
  "north_arrow": {
    "xy": [0.95, 0.88],       # axes-fraction tip position (arrow head)
    "xytext": [0.95, 0.78]    # axes-fraction tail position (arrow base)
  },
  "stats_box": {
    "x": 0.5,                 # axes-fraction center of the stats box
    "y": 0.5
  }
}

All keys are optional; sane defaults are applied via DEFAULT_LABEL_CONFIG
and DEFAULT_ANNOTATION_CONFIG. "markers.show" is the key that solves the
"too many dots on a large country" problem: "labeled_only" (default)
draws a marker only for the communes that also got a text label, keeping
map density consistent regardless of admin-unit count. Use "all" for
small territories (Réunion) where every commune can be marked without
clutter, and "none" to drop markers entirely.

"plot.annotations" exists because a fixed axes-fraction position for the
north arrow or stats box can land on top of territory or text for an
irregularly-shaped country (e.g. Haiti's NW peninsula) even though it
looked fine for a compact one (e.g. Réunion) - see get_annotation_config.
"""

from __future__ import annotations

import numpy as np
import geopandas as gpd
from scipy.spatial import cKDTree
import matplotlib.patheffects as pe


DEFAULT_LABEL_CONFIG = {
    "enabled": True,
    "max_labels": 30,
    "min_distance_m": "auto",
    "min_candidate_area_km2": 0,
    "placement": "representative_point",
    "style": {
        "fontsize": 6.5,
        "color": "navy",
        "halo_color": "white",
        "halo_width": 2.0,
        "fontweight": "bold",
    },
    "markers": {
        "show": "labeled_only",
        "color": "blue",
        "alpha": 0.6,
        "size": 60,
        "edgecolor": "white",
    },
}

DEFAULT_ANNOTATION_CONFIG = {
    "north_arrow": {
        "xy": [0.95, 0.88],
        "xytext": [0.95, 0.78],
    },
    "stats_box": {
        "x": 0.5,
        "y": 0.5,
        "fontsize": 8,
        "alpha": 0.7,
    },
}


def get_label_config(cfg) -> dict:
    """
    Merge config/<country>.json's "plot.labels" block with DEFAULT_LABEL_CONFIG,
    so every country JSON can override only what it needs (or nothing at all).

    cfg is the object returned by config_loader.load_country_config(), expected
    to expose the raw parsed JSON via cfg.raw.
    """
    user_cfg = cfg.raw.get("plot", {}).get("labels", {})

    merged = dict(DEFAULT_LABEL_CONFIG)
    merged.update({k: v for k, v in user_cfg.items() if k not in ("style", "markers")})

    merged["style"] = dict(DEFAULT_LABEL_CONFIG["style"])
    merged["style"].update(user_cfg.get("style", {}))

    merged["markers"] = dict(DEFAULT_LABEL_CONFIG["markers"])
    merged["markers"].update(user_cfg.get("markers", {}))

    return merged


def get_annotation_config(cfg) -> dict:
    """
    Merge config/<country>.json's "plot.annotations" block with
    DEFAULT_ANNOTATION_CONFIG. Lets each country override where the north
    arrow and stats box land - necessary because "blank space" on the map
    depends entirely on the country's shape (Haiti's NW peninsulas leave
    blank space in different corners than Réunion's compact circle).
    """
    user_cfg = cfg.raw.get("plot", {}).get("annotations", {})

    merged = {
        "north_arrow": dict(DEFAULT_ANNOTATION_CONFIG["north_arrow"]),
        "stats_box": dict(DEFAULT_ANNOTATION_CONFIG["stats_box"]),
    }
    merged["north_arrow"].update(user_cfg.get("north_arrow", {}))
    merged["stats_box"].update(user_cfg.get("stats_box", {}))

    return merged


def _prefilter_by_area(gdf: gpd.GeoDataFrame, min_area_km2: float) -> gpd.GeoDataFrame:
    """
    Drop polygons below min_area_km2 BEFORE ranking/selection.

    Purely a candidate-pool reduction step for large admin datasets
    (e.g. Kenya ADM2, 300 units): small administrative units can be
    removed before ranking to keep the candidate pool manageable and
    the selection process efficient.
    """
    if min_area_km2 <= 0:
        return gdf

    area_km2 = gdf.geometry.area / 1e6
    filtered = gdf[area_km2 >= min_area_km2]

    # Never filter down to nothing - fall back to the unfiltered set if the
    # threshold was set too aggressively for a given country.
    if filtered.empty:
        return gdf

    return filtered


def _select_subset(
    gdf: gpd.GeoDataFrame,
    priority_field: str,
    max_labels: int,
    min_distance_m: float,
) -> gpd.GeoDataFrame:
    """
    Greedy candidate selection.

    gdf must already have '_label_x' / '_label_y' columns (anchor point)
    and the column named by priority_field (higher = higher priority,
    e.g. polygon area or zone area).

    Returns a subset of gdf, at most max_labels rows, with every pair of
    selected anchors at least min_distance_m apart.
    """
    if gdf.empty or max_labels <= 0:
        return gdf.iloc[0:0]

    candidates = gdf.sort_values(priority_field, ascending=False).reset_index(drop=True)

    accepted_idx = []
    accepted_coords = []

    for i, row in candidates.iterrows():
        x, y = row["_label_x"], row["_label_y"]

        if accepted_coords:
            tree = cKDTree(accepted_coords)
            nearest_dist, _ = tree.query([x, y])
            if nearest_dist < min_distance_m:
                continue

        accepted_coords.append([x, y])
        accepted_idx.append(i)

        if len(accepted_idx) >= max_labels:
            break

    return candidates.loc[accepted_idx]


def _resolve_min_distance(min_distance_cfg, ax, max_labels: int) -> float:
    """
    Supports a fixed value (float/int) or "auto" (string) in the config.
    "auto" derives spacing from the current map extent and max_labels, so
    the same JSON key works for a large country (Madagascar) and a small
    one (Réunion) without per-country hand-tuning.
    """
    if isinstance(min_distance_cfg, (int, float)):
        return float(min_distance_cfg)

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    area_m2 = abs(xmax - xmin) * abs(ymax - ymin)
    cell_side = np.sqrt(area_m2 / max(max_labels, 1))
    return cell_side * 0.6  # leaves breathing room, avoids over-spacing


def select_labels(
    gdf: gpd.GeoDataFrame,
    priority_field: str,
    label_config: dict,
    ax,
) -> gpd.GeoDataFrame:
    """
    Compute anchors, pre-filter, and select a non-overlapping subset -
    WITHOUT drawing anything. Callers use the returned subset both for
    label text (draw_labels) and, if desired, for marker placement
    (draw_markers), so the two stay in sync automatically.
    """
    if not label_config.get("enabled", True) or gdf.empty:
        return gdf.iloc[0:0]

    max_labels = label_config.get("max_labels", 30)
    min_distance_cfg = label_config.get("min_distance_m", "auto")
    min_candidate_area_km2 = label_config.get("min_candidate_area_km2", 0)
    placement = label_config.get("placement", "representative_point")

    gdf = gdf.copy()

    if priority_field not in gdf.columns:
        raise KeyError(
            f"priority_field '{priority_field}' not found in gdf columns "
            f"({list(gdf.columns)}). The calling script must compute it "
            f"before calling select_labels()."
        )

    gdf = _prefilter_by_area(gdf, min_candidate_area_km2)

    if placement == "centroid":
        pts = gdf.geometry.centroid
    else:
        pts = gdf.geometry.representative_point()

    gdf["_label_x"] = pts.x
    gdf["_label_y"] = pts.y

    min_distance_m = _resolve_min_distance(min_distance_cfg, ax, max_labels)
    selected = _select_subset(gdf, priority_field, max_labels, min_distance_m)

    print(
        f"Labels: showing {len(selected)}/{len(gdf)} eligible admin units "
        f"(max_labels={max_labels}, min_distance_m={min_distance_m:.0f}, "
        f"min_candidate_area_km2={min_candidate_area_km2})"
    )

    return selected


def draw_labels(ax, selected: gpd.GeoDataFrame, name_field: str, style: dict):
    """Draw text labels for an already-selected subset (see select_labels)."""
    for _, row in selected.iterrows():
        ax.annotate(
            row[name_field],
            xy=(row["_label_x"], row["_label_y"]),
            fontsize=style.get("fontsize", 6.5),
            color=style.get("color", "navy"),
            fontweight=style.get("fontweight", "bold"),
            ha="center",
            va="center",
            path_effects=[
                pe.withStroke(
                    linewidth=style.get("halo_width", 2.0),
                    foreground=style.get("halo_color", "white"),
                )
            ],
            zorder=6,
        )


def draw_markers(ax, gdf: gpd.GeoDataFrame, marker_config: dict):
    """
    Draw point markers for gdf (already the right subset - caller decides
    whether that's "all" units or only the "labeled_only" selection).
    """
    show = marker_config.get("show", "labeled_only")
    if show == "none" or gdf.empty:
        return

    gdf.plot(
        ax=ax,
        color=marker_config.get("color", "blue"),
        alpha=marker_config.get("alpha", 0.6),
        markersize=marker_config.get("size", 60),
        edgecolor=marker_config.get("edgecolor", "white"),
        zorder=3,
    )


def add_adaptive_labels(
    ax,
    gdf: gpd.GeoDataFrame,
    name_field: str,
    priority_field: str,
    label_config: dict,
) -> gpd.GeoDataFrame:
    """
    Convenience wrapper: select + draw labels in one call (no marker
    handling - used by 02_ghi_distribution.py, which has no markers).
    Returns the selected subset.
    """
    selected = select_labels(gdf, priority_field, label_config, ax)
    draw_labels(ax, selected, name_field, label_config.get("style", DEFAULT_LABEL_CONFIG["style"]))
    return selected
