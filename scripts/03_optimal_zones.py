"""
03_optimal_zones.py
=====================
Extraction of optimal solar zones (GHI >= threshold), vectorization,
area calculation, and intersection with administrative boundaries -
generalized to run for any configured country (see config/<country>.json).

Prerequisite:
    Run 01_reproject_clip.py --country <country> first. It produces the
    clipped raster and boundary used here.

Vectorization is performed directly in memory (raster.values +
raster.rio.transform()), without writing temporary .tif files to disk -
same approach as the original single-country script.

Inputs:
    outputs/<country_code>/maps/ghi_<country_code>_clip.tif   (from step 01)
    outputs/<country_code>/maps/<country_code>_boundary.gpkg  (from step 01)
    data/vectors/<admin_vector_filename>                       (from config)

Outputs:
    outputs/<country_code>/maps/Optimal_Solar_Zones.png
    outputs/<country_code>/maps/optimal_zones.gpkg
    outputs/<country_code>/maps/admin_centroids.gpkg
    outputs/<country_code>/tables/optimal_solar_zones_area.csv

Label AND marker placement (administrative-unit names + markers on the map)
is fully driven by config/<country>.json -> "plot.labels" (see
utils/mapping.py for the schema). The priority used for selecting labels
is the optimal-zone area within each administrative unit.

No country-specific number of labels or administrative-unit type is
hard-coded here: the same logic works whether the configured layer
contains communes, districts, or another administrative level.

Scalability note (Kenya incident): per-administrative-unit optimal-zone
area is computed via raster-based zonal statistics (rasterize admin
polygons onto the GHI grid, then count masked pixels per admin ID) - NOT
via gpd.overlay(admin, optimal_zone_vector). A full vector/vector
intersection scales with the COMPLEXITY of the optimal-zone geometry,
which exploded for a large, near-total-coverage country (Kenya: ~16M
pixels, ~99.5% of the territory above threshold) because the zone was
previously vectorized from the CONTINUOUS GHI raster rather than a binary
mask, producing a near-per-pixel polygon count. Vectorizing the binary
mask (this version) fixes the root cause; the raster-based zonal stats
additionally make the per-admin-unit area computation itself immune to
vector complexity entirely, scaling only with pixel count regardless of
country size or coverage.

The metadata box credits both raster and vector providers, read from
cfg.provenance - see 02_ghi_distribution.py for the rationale (admin
boundaries and centroid markers are visible content derived from the
vector layer, not just an internal clipping step).

Usage:
    python scripts/03_optimal_zones.py --country reunion --threshold 5.0
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
from matplotlib.patches import FancyBboxPatch
from matplotlib_scalebar.scalebar import ScaleBar
from rasterio.features import shapes, rasterize

from config_loader import load_country_config, list_available_countries
from utils.mapping import get_label_config, select_labels, draw_labels, draw_markers, get_annotation_config

from pyproj import CRS

MIN_POLYGON_AREA_M2 = 1000  # filter small polygons (vectorization noise)


# ---------------------------------------------------------------------------
# Load raster / boundary / admin units
# ---------------------------------------------------------------------------
def load_clipped_raster(cfg):
    clipped_path = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
    if not clipped_path.exists():
        sys.exit(
            f"Missing raster: {clipped_path}\n"
            f"Run: python scripts/01_reproject_clip.py --country {cfg.country_code.lower()}"
        )
    return rioxarray.open_rasterio(clipped_path, masked=True).squeeze()


def load_boundary(cfg):
    boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
    if not boundary_path.exists():
        sys.exit(
            f"Missing boundary: {boundary_path}\n"
            f"Run: python scripts/01_reproject_clip.py --country {cfg.country_code.lower()}"
        )
    return gpd.read_file(boundary_path, engine="pyogrio")


def load_admin_units(cfg):
    """
    Load administrative boundaries, then ensure they are in the
    pipeline's target CRS. Same CRS-matching logic as
    02_ghi_distribution.py's load_admin_units() - pyproj.CRS-based
    comparison, explicit failure on a missing source CRS - kept
    identical between the two scripts so they never silently diverge.
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
# Compute area statistics (national total - raster-based, already fast)
# ---------------------------------------------------------------------------
def compute_area_stats(raster_clip, mask_ghi):
    res_x, res_y = raster_clip.rio.resolution()
    pixel_area_m2 = abs(res_x * res_y)

    zone_area_km2 = (np.count_nonzero(mask_ghi) * pixel_area_m2) / 1e6
    total_area_km2 = (raster_clip.count().item() * pixel_area_m2) / 1e6
    percent_zone = (zone_area_km2 / total_area_km2) * 100

    return zone_area_km2, total_area_km2, percent_zone


# ---------------------------------------------------------------------------
# Per-admin-unit area statistics - raster-based zonal statistics
# ---------------------------------------------------------------------------
def compute_zone_area_by_admin(admin, raster_clip, mask_ghi):
    """
    Compute the optimal-zone area within each administrative unit directly
    from the raster grid, via zonal statistics: rasterize admin polygons
    onto the same grid as the GHI raster (each pixel gets the row-index of
    the admin polygon it falls in), then count masked (optimal) pixels per
    admin ID with a single vectorized NumPy pass.

    This REPLACES gpd.overlay(admin, gdf_zone, "intersection") + dissolve().
    That approach does a full vector/vector intersection between the
    administrative layer and the optimal-zone vector layer, which scales
    with the COMPLEXITY of the zone geometry (number of vertices/parts) -
    for a large, near-total-coverage country this geometry can be huge,
    causing the operation to hang or exhaust memory (see Kenya incident,
    16M pixels / ~99.5% optimal coverage).

    This raster-based approach scales with PIXEL COUNT only, which NumPy
    handles for tens of millions of pixels in well under a second,
    regardless of the zone geometry's complexity - and gives an EXACT
    pixel-count-based area (no geometric simplification/precision loss).

    Returns admin (GeoDataFrame, reset index) with a new "_zone_area_km2"
    column.
    """
    res_x, res_y = raster_clip.rio.resolution()
    pixel_area_km2 = abs(res_x * res_y) / 1e6

    admin = admin.reset_index(drop=True)
    shapes_iter = ((geom, i) for i, geom in enumerate(admin.geometry))
    admin_id_raster = rasterize(
        shapes_iter,
        out_shape=mask_ghi.shape,
        transform=raster_clip.rio.transform(),
        fill=-1,
        dtype="int32",
    )

    mask_arr = mask_ghi.values if hasattr(mask_ghi, "values") else np.asarray(mask_ghi)
    valid = (admin_id_raster >= 0) & mask_arr
    ids_in_zone = admin_id_raster[valid]

    counts = np.bincount(ids_in_zone, minlength=len(admin))
    admin["_zone_area_km2"] = counts[: len(admin)] * pixel_area_km2

    return admin


# ---------------------------------------------------------------------------
# Vectorization (for the .gpkg export and the optional zone outline only -
# NOT used for area calculations or for the map's raster fill, both of
# which are handled elsewhere without needing this vector layer at all)
# ---------------------------------------------------------------------------
def vectorize_zone(ghi_zone, mask_ghi):
    """
    Vectorize the optimal-zone MASK (binary), not the continuous GHI
    values, directly in memory (no temporary .tif).

    Root-cause note: rasterio.features.shapes() traces one polygon per
    contiguous run of EQUAL values in its input array. Vectorizing the
    continuous GHI raster (as the original implementation did) traces a
    near-separate polygon at almost every pixel-to-pixel value change -
    for a large, near-total-coverage country this produces an enormous,
    highly fragmented polygon count, which is what made Kenya's overlay
    step hang. Vectorizing the binary mask instead collapses the output to
    one polygon per CONTIGUOUS REGION of the zone's actual boundary -
    independent of how finely the underlying GHI values vary inside it.
    """
    transform = ghi_zone.rio.transform()
    mask_arr = (mask_ghi.values if hasattr(mask_ghi, "values") else np.asarray(mask_ghi)).astype("uint8")

    results = (
        {"properties": {"optimal": 1}, "geometry": geom}
        for geom, v in shapes(mask_arr, mask=mask_arr.astype(bool), transform=transform)
        if v == 1
    )

    gdf_zone = gpd.GeoDataFrame.from_features(results, crs=ghi_zone.rio.crs)
    if gdf_zone.empty:
        return gdf_zone

    # Dissolve into as few polygons as possible, then explode back into
    # individual (multi-part-safe) polygons for a clean, small export.
    gdf_zone = gdf_zone.dissolve()
    gdf_zone = gdf_zone.explode(index_parts=False).reset_index(drop=True)
    gdf_zone = gdf_zone[gdf_zone.area > MIN_POLYGON_AREA_M2]

    return gdf_zone


# ---------------------------------------------------------------------------
# Plot map
# ---------------------------------------------------------------------------
def plot_optimal_zones_map(
    ghi_zone, boundary, admin, admin_points,
    zone_area_km2, percent_zone, threshold, cfg, output_png
):
    custom_cmap = colormaps["YlOrRd"].copy()
    custom_cmap.set_bad(color="white")

    fig, ax = plt.subplots(figsize=cfg.figsize_in)

    im = ghi_zone.plot(
        cmap=custom_cmap,
        add_colorbar=True,
        vmin=threshold,
        vmax=threshold + 1,
        ax=ax,
        zorder=2,
    )

    admin.boundary.plot(ax=ax, color="lightgray", linewidth=0.3, zorder=3)
    boundary.boundary.plot(ax=ax, color="black", linewidth=1)

    cbar = im.colorbar
    cbar.set_label("GHI (kWh/m²/day)", fontsize=10, fontweight="bold")

    # Select the labeled subset FIRST, then decide marker display from it -
    # this keeps markers and labels synchronized and makes the map scalable
    # across countries with different numbers of administrative units
    # (e.g. Réunion: 24 communes, Madagascar: 22 districts,
    # Kenya: ~300 administrative units), entirely through
    # config/<country>.json -> "plot.labels".
    label_cfg = get_label_config(cfg)
    selected_labels = select_labels(
        gdf=admin_points,
        priority_field="_zone_area",
        label_config=label_cfg,
        ax=ax,
    )

    marker_cfg = label_cfg["markers"]
    markers_gdf = admin_points if marker_cfg.get("show") == "all" else selected_labels
    draw_markers(ax, markers_gdf, marker_cfg)

    draw_labels(ax, selected_labels, cfg.admin_name_field, label_cfg["style"])

    ax.set_xlabel("Easting (m)", fontsize=12, fontweight="bold", color="darkred")
    ax.set_ylabel("Northing (m)", fontsize=12, fontweight="bold", color="darkred")
    ax.tick_params(labelsize=10)

    ax.set_title(
        f"Optimal Solar Cooking Zones (\u2265 {threshold:.0f} kWh/m²/day) - {cfg.country}",
        fontsize=14,
        fontweight="bold",
    )

    stats_text = (
        f"Area \u2265 {threshold:.0f} kWh/m²/day: {zone_area_km2:.1f} km²\n"
        f"({percent_zone:.1f} % of {cfg.country})"
    )

    # Positions from config/<country>.json -> "plot.annotations"
    annot_cfg = get_annotation_config(cfg)

    stats_cfg = annot_cfg["stats_box"]
    box_x, box_y = stats_cfg["x"], stats_cfg["y"]
    # fontsize/alpha are now JSON-configurable (plot.annotations.stats_box),
    # defaulting to the original hard-coded values (8, 0.7) when a country's
    # config doesn't override them - needed once several countries (Kenya,
    # Maroc, Madagascar) required a larger/more opaque box for readability
    # against a dark-colored or busy background, which a single hard-coded
    # value couldn't serve for every country at once.
    stats_fontsize = stats_cfg.get("fontsize", 8)
    stats_alpha = stats_cfg.get("alpha", 0.7)

    bbox = FancyBboxPatch(
        (box_x - 0.19, box_y - 0.045),
        width=0.38,
        height=0.09,
        boxstyle="round,pad=0.02",
        ec="black",
        fc="white",
        alpha=stats_alpha,
        transform=ax.transAxes,
    )
    ax.add_patch(bbox)

    ax.text(
        box_x, box_y, stats_text,
        transform=ax.transAxes,
        ha="center", va="center",
        fontsize=stats_fontsize, fontweight="bold",
    )

    # Metadata box - credits both raster and vector sources (see rationale
    # in 02_ghi_distribution.py's docstring)
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
        ha="left", va="top",
        fontsize=7, color="gray", fontweight="bold", style="italic",
        bbox=dict(facecolor="white", alpha=0.5, boxstyle="round,pad=0.3"),
    )

    arrow_cfg = annot_cfg["north_arrow"]
    ax.annotate(
        "N", xy=tuple(arrow_cfg["xy"]), xytext=tuple(arrow_cfg["xytext"]),
        arrowprops=dict(facecolor="black", width=2, headwidth=8),
        ha="center", va="center", fontsize=12, fontweight="bold",
        xycoords="axes fraction",
    )

    scalebar = ScaleBar(
        1, units="m", dimension="si-length",
        location="lower left", scale_loc="bottom",
        font_properties={"size": 8},
    )
    ax.add_artist(scalebar)

    xmin, xmax = ax.get_xlim()
    ymin, ymax = ax.get_ylim()
    tick = cfg.tick_interval_m
    ax.set_xticks(np.arange(np.floor(xmin / tick) * tick, xmax, tick))
    ax.set_yticks(np.arange(np.floor(ymin / tick) * tick, ymax, tick))

    ax.ticklabel_format(style="plain")
    ax.tick_params(axis="both", labelsize=9)
    ax.tick_params(top=True, right=True, direction="in", length=5, width=1)

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
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="GHI threshold in kWh/m²/day (default: value from the country's config)",
    )
    args = parser.parse_args()
    cfg = load_country_config(args.country)
    threshold = args.threshold if args.threshold is not None else cfg.ghi_threshold_kwh_m2_day

    cfg.maps_dir.mkdir(parents=True, exist_ok=True)
    cfg.tables_dir.mkdir(parents=True, exist_ok=True)

    raster_clip = load_clipped_raster(cfg)
    admin = load_admin_units(cfg)
    boundary = load_boundary(cfg)

    mask_ghi = raster_clip >= threshold
    ghi_zone = raster_clip.where(mask_ghi)

    zone_area_km2, total_area_km2, percent_zone = compute_area_stats(raster_clip, mask_ghi)

    print(f"[{cfg.country_code}] Area >= {threshold} kWh/m²/day: {zone_area_km2:.2f} km² ({percent_zone:.2f} %)")

    pd.DataFrame([{
        "area_km2": zone_area_km2,
        "total_area_km2": total_area_km2,
        "percent_country": percent_zone,
        "ghi_threshold_kwh_m2_day": threshold,
    }]).to_csv(cfg.tables_dir / "optimal_solar_zones_area.csv", index=False)
    print(f"Statistics exported -> {cfg.tables_dir / 'optimal_solar_zones_area.csv'}")

    gdf_zone = vectorize_zone(ghi_zone, mask_ghi)
    gdf_zone.to_file(cfg.maps_dir / "optimal_zones.gpkg", driver="GPKG", engine="pyogrio")
    print(f"Optimal zones exported -> {cfg.maps_dir / 'optimal_zones.gpkg'} ({len(gdf_zone)} polygon(s))")

    # Per-admin-unit optimal area via raster-based zonal statistics -
    # replaces gpd.overlay(admin, gdf_zone, "intersection") entirely.
    admin = compute_zone_area_by_admin(admin, raster_clip, mask_ghi)
    admin_with_zone = admin[admin["_zone_area_km2"] > 0].copy()

    admin_points = admin_with_zone.copy()
    admin_points["geometry"] = admin_points.representative_point()
    admin_points["_zone_area"] = admin_points["_zone_area_km2"] * 1e6  # m2, for consistency with mapping.py's priority_field units
    admin_points.to_file(cfg.maps_dir / "admin_centroids.gpkg", driver="GPKG", engine="pyogrio")
    print(f"Centroids exported -> {cfg.maps_dir / 'admin_centroids.gpkg'} ({len(admin_points)} admin unit(s) with optimal coverage)")

    plot_optimal_zones_map(
        ghi_zone, boundary, admin, admin_points,
        zone_area_km2, percent_zone, threshold, cfg,
        cfg.maps_dir / "Optimal_Solar_Zones.png",
    )


if __name__ == "__main__":
    main()
