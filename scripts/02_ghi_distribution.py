"""
02_ghi_distribution.py
========================
Descriptive statistics and spatial distribution map of Global Horizontal
Irradiance (GHI), generalized to run for any configured country (see
config/<country>.json).

Prerequisite:
    Run 01_reproject_clip.py --country <country> first. It produces the
    clipped raster used here.

Inputs:
    outputs/<country_code>/maps/ghi_<country_code>_clip.tif   (from step 01)
    data/vectors/<admin_vector_filename>                       (from config)

Outputs:
    outputs/<country_code>/maps/Final_GHI_Distribution_Map.png
    outputs/<country_code>/tables/ghi_statistics.csv

Label placement (administrative unit names on the map) is fully driven
by config/<country>.json -> "plot.labels" (see utils/mapping.py for the
schema). Nothing country-specific is hard-coded here: label behaviour,
including the number of displayed labels and their placement strategy,
is controlled by the country configuration. The same script therefore
runs without modification across datasets with very different numbers
of administrative units, from Réunion (24 communes) and Madagascar
(22 districts at ADM2 level) to Kenya (approximately 300 administrative
units).

The metadata box credits both the raster AND vector providers, read from
cfg.provenance - because the admin boundary lines and commune-name labels
drawn on this map are themselves content derived from the vector layer
(not just used internally for clipping), which makes attribution a
license requirement (CC BY 4.0 / CC BY-IGO), not merely a documentation
nicety kept only in the HTML report.

Usage:
    python scripts/02_ghi_distribution.py --country reunion
"""

import argparse
import sys
from pathlib import Path

# --- Ensure project root is on sys.path so `utils.mapping` resolves,
#     regardless of the working directory the script is launched from ---
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rioxarray
from matplotlib import colormaps
from matplotlib_scalebar.scalebar import ScaleBar

from config_loader import load_country_config, list_available_countries
from utils.mapping import add_adaptive_labels, get_label_config, get_annotation_config

from pyproj import CRS


# ---------------------------------------------------------------------------
# Load raster
# ---------------------------------------------------------------------------
def load_clipped_raster(cfg):
    """Load the clipped and reprojected GHI raster produced in step 01."""
    clipped_path = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
    if not clipped_path.exists():
        sys.exit(
            f"Missing raster: {clipped_path}\n"
            f"Run: python scripts/01_reproject_clip.py --country {cfg.country_code.lower()}"
        )
    return rioxarray.open_rasterio(clipped_path, masked=True).squeeze()


# ---------------------------------------------------------------------------
# Load admin boundaries (vector)
# ---------------------------------------------------------------------------
def load_admin_units(cfg):
    """
    Load administrative boundaries using pyogrio (safer than Fiona on
    Windows), then ensure they are in the pipeline's target CRS.

    CRS equality is checked via pyproj.CRS objects rather than string
    representations - two CRS definitions can be semantically identical
    (e.g. an EPSG code vs. an equivalent WKT/PROJ string) while
    stringifying differently, which a naive str() comparison would
    misread as "different" and reproject unnecessarily. This does not
    change correctness (an unnecessary reproject is a costly no-op, not
    a silent error), but it makes the reprojection decision itself
    robust rather than incidentally correct.

    A missing source CRS is treated as an explicit failure, naming this
    file, rather than letting geopandas' own to_crs() raise a generic
    ValueError with no context about which input caused it. This should
    normally never trigger in practice - a missing CRS is expected to be
    caught earlier by the pipeline's QA/QC pre-processing phase.
    """
    if not cfg.admin_vector_path.exists():
        sys.exit(f"Missing vector file: {cfg.admin_vector_path}")

    admin = gpd.read_file(cfg.admin_vector_path, engine="pyogrio")

    if admin.crs is None:
        sys.exit(
            f"Administrative vector ({cfg.admin_vector_path}) has no CRS "
            f"defined - cannot reproject without a known source CRS. Run "
            f"scripts/00_inspect_data.py to confirm."
        )

    if CRS.from_user_input(admin.crs) != CRS.from_user_input(cfg.crs_target):
        admin = admin.to_crs(cfg.crs_target)

    return admin


# ---------------------------------------------------------------------------
# Compute statistics
# ---------------------------------------------------------------------------
def compute_statistics(raster) -> dict:
    """Compute descriptive GHI statistics for the entire clipped area."""
    return {
        "ghi_min_kwh_m2_day": float(raster.min()),
        "ghi_max_kwh_m2_day": float(raster.max()),
        "ghi_mean_kwh_m2_day": float(raster.mean()),
        "ghi_median_kwh_m2_day": float(raster.median()),
    }


def save_statistics(stats: dict, cfg):
    cfg.tables_dir.mkdir(parents=True, exist_ok=True)
    output_csv = cfg.tables_dir / "ghi_statistics.csv"
    pd.DataFrame([stats]).to_csv(output_csv, index=False)
    print(f"GHI statistics exported -> {output_csv}")


# ---------------------------------------------------------------------------
# Plot map
# ---------------------------------------------------------------------------
def plot_ghi_map(raster, admin, cfg, output_png):
    """Generate the final spatial distribution map of GHI."""
    custom_cmap = colormaps["YlOrRd"].copy()
    custom_cmap.set_bad(color="white")

    fig, ax = plt.subplots(figsize=cfg.figsize_in)

    im = raster.plot(
        cmap=custom_cmap,
        add_colorbar=False,
        vmin=cfg.vmin,
        vmax=cfg.vmax,
        ax=ax,
    )

    admin.boundary.plot(ax=ax, color="black", linewidth=0.8)

    # Adaptive, non-overlapping admin unit labels - priority by
    # administrative area (readability-first overview map), fully
    # configured via config/<country>.json -> "plot.labels"
    admin["_priority_area"] = admin.geometry.area
    label_cfg = get_label_config(cfg)
    add_adaptive_labels(
        ax=ax,
        gdf=admin,
        name_field=cfg.admin_name_field,
        priority_field="_priority_area",
        label_config=label_cfg,
    )

    plt.colorbar(im, label="GHI (kWh/m²/day)", ax=ax)

    ax.set_xlabel("Easting (m)", fontsize=10, fontweight="bold", color="darkred")
    ax.set_ylabel("Northing (m)", fontsize=10, fontweight="bold", color="darkred")
    ax.set_title(
        f"Spatial distribution of Global Horizontal Irradiance (GHI)\n"
        f"{cfg.country} - Daily average (1999–2018)",
        fontweight="bold",
    )

    # Metadata box - credits both raster and vector sources, since the
    # admin boundary lines and the commune-name labels are both content
    # drawn directly from the vector layer (not just used internally for
    # clipping), which makes attribution a license requirement (CC BY 4.0
    # / CC BY-IGO), not merely a documentation nicety kept in the report.
    raster_provider = cfg.provenance.get("raster", {}).get("provider", "Unknown raster source")
    vector_provider = cfg.provenance.get("vector", {}).get("provider", "Unknown vector source")

    ax.text(
        0.63, -0.05,
        f"Author: Lenz Arly Chery\n"
        f"CRS: {cfg.raw.get('crs_target_name', cfg.crs_target)}\n"
        f"Sources:\n"
        f"  {raster_provider} (raster)\n"
        f"  {vector_provider} (boundaries)",
        transform=ax.transAxes,
        ha="left",
        va="top",
        fontsize=7,
        color="gray",
        fontweight="bold",
        style="italic",
        bbox=dict(facecolor="white", alpha=0.5, boxstyle="round,pad=0.3"),
    )

    # North arrow - position from config/<country>.json -> "plot.annotations.north_arrow"
    annot_cfg = get_annotation_config(cfg)
    arrow_cfg = annot_cfg["north_arrow"]
    ax.annotate(
        "N",
        xy=tuple(arrow_cfg["xy"]),
        xytext=tuple(arrow_cfg["xytext"]),
        arrowprops=dict(facecolor="black", width=2, headwidth=8),
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        xycoords="axes fraction",
    )

    # Scale bar
    scalebar = ScaleBar(
        1,
        units="m",
        dimension="si-length",
        location="lower left",
        scale_loc="bottom",
        font_properties={"size": 8},
    )
    ax.add_artist(scalebar)

    # Grid ticks - spacing read from config instead of hard-coded to 10 km
    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    tick = cfg.tick_interval_m
    ax.set_xticks(np.arange(np.floor(xmin / tick) * tick, xmax, tick))
    ax.set_yticks(np.arange(np.floor(ymin / tick) * tick, ymax, tick))
    ax.ticklabel_format(style="plain")
    ax.tick_params(axis="both", labelsize=9)
    ax.tick_params(top=True, right=True, direction="in", length=5, width=1)

    # Frame
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(1.2)
        spine.set_color("black")

    cfg.maps_dir.mkdir(parents=True, exist_ok=True)
    plt.savefig(output_png, dpi=300, bbox_inches="tight")
    print(f"Map exported -> {output_png}")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--country", required=True,
        help=f"One of: {', '.join(list_available_countries())}",
    )
    args = parser.parse_args()
    cfg = load_country_config(args.country)

    raster = load_clipped_raster(cfg)
    admin = load_admin_units(cfg)

    stats = compute_statistics(raster)
    save_statistics(stats, cfg)
    print(stats)

    plot_ghi_map(raster, admin, cfg, cfg.maps_dir / "Final_GHI_Distribution_Map.png")


if __name__ == "__main__":
    main()
