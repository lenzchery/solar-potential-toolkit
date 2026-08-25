"""
01_reproject_clip.py

Preprocessing step for the solar potential analysis workflow - generalized
to run for any configured country (see config/<country>.json).

This script:
- loads the Global Solar Atlas GHI raster for the given country;
- detects its actual CRS and reprojects it to the country's target CRS
  (see "CRS fix" note below - this replaces the original write_crs() call);
- creates the country boundary by dissolving administrative polygons;
- clips the raster using that boundary;
- exports raster and vector outputs for further analysis.

CRS fix (why this differs from the original single-country script):
    The original script called `raster.rio.write_crs(target_crs)`, which
    only *labels* a raster with a CRS - it does not transform coordinates.
    That worked for Reunion only because the raw raster had already been
    reprojected by hand in QGIS (WGS 84 -> RGR92) before this script ever
    ran; write_crs() was just re-attaching a CRS tag GDAL had dropped on
    export. Tested against a Haiti raster still in its native WGS 84, the
    same write_crs() call silently mislabeled un-transformed coordinates
    as if they were already in the target CRS.
    Here, the raster's real CRS is read directly (`raster.rio.crs`), and
    `raster.rio.reproject(target_crs)` is called whenever it differs from
    the target - an actual reprojection, not a label. This removes the
    manual QGIS pre-processing step entirely; every country's raw,
    as-downloaded raster can be dropped into data/raster/ and run as-is.

CRS comparison (raster and vector, kept consistent):
    Both the raster's CRS and the administrative vector's CRS are compared
    against the target CRS via pyproj.CRS objects (CRS.from_user_input(...)),
    not via string representations. Two CRS definitions can be semantically
    identical (e.g. an EPSG code vs. an equivalent WKT/PROJ string) while
    stringifying differently, which a naive str() comparison would misread
    as "different" and trigger an unnecessary reprojection. This does not
    change correctness (an unnecessary reproject is a costly no-op, not a
    silent error), but it makes the reprojection decision itself robust
    rather than incidentally correct - and keeps this script's comparison
    logic identical to the one used for administrative vectors in
    02_ghi_distribution.py and 03_optimal_zones.py.

Inputs (paths resolved from config/<country>.json):
    data/raster/<raster_filename>
    data/vectors/<admin_vector_filename>

Outputs:
    outputs/<country_code>/maps/ghi_<country_code>_clip.tif
    outputs/<country_code>/maps/<country_code>_boundary.gpkg

Usage:
    python src/01_reproject_clip.py --country reunion
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import geopandas as gpd
import numpy as np
import rioxarray

from config_loader import load_country_config, list_available_countries

from pyproj import CRS

NODATA_VALUE = 1.17549e-38  # Global Solar Atlas float32 NoData sentinel


def load_ghi_raster(raster_path, target_crs):
    """
    Load the GHI raster, remove NoData values, and ensure it ends up in
    the target CRS - reprojecting if its actual CRS differs, rather than
    assuming it already matches.

    CRS equality is checked via pyproj.CRS objects rather than string
    representations - see the module docstring's "CRS comparison" note.
    """
    raster = rioxarray.open_rasterio(raster_path, masked=True).squeeze()
    raster = raster.where(raster != NODATA_VALUE, np.nan)

    source_crs = raster.rio.crs
    if source_crs is None:
        raise ValueError(
            f"{raster_path} has no CRS embedded. This pipeline no longer "
            f"assumes a CRS for you - set one explicitly (e.g. with "
            f"raster.rio.write_crs(<known_source_crs>)) before proceeding, "
            f"once you've confirmed what that source CRS actually is."
        )

    print(f"source raster CRS detected: {source_crs}")
    if CRS.from_user_input(source_crs) != CRS.from_user_input(target_crs):
        print(f"reprojecting to target CRS: {target_crs}")
        raster = raster.rio.reproject(target_crs)
    else:
        print("source CRS already matches target CRS - no reprojection needed")

    return raster


def load_island_boundary(admin_vector_path, target_crs):
    """
    Create a single country/island polygon from administrative polygons.

    Same CRS-matching logic as 02_ghi_distribution.py's
    load_admin_units() and 03_optimal_zones.py's load_admin_units() -
    pyproj.CRS-based comparison, explicit failure on a missing source
    CRS - kept identical across all three scripts.
    """
    admin = gpd.read_file(admin_vector_path, engine="pyogrio")

    if admin.crs is None:
        sys.exit(
            f"Administrative vector ({admin_vector_path}) has no CRS "
            f"defined - cannot reproject without a known source CRS."
        )

    if CRS.from_user_input(admin.crs) != CRS.from_user_input(target_crs):
        admin = admin.to_crs(target_crs)

    # Dissolve all administrative units into one boundary polygon
    if hasattr(admin, "union_all"):
        boundary = admin.union_all()
    else:
        boundary = admin.unary_union

    return gpd.GeoDataFrame(geometry=[boundary], crs=admin.crs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--country", required=True,
        help=f"One of: {', '.join(list_available_countries())}",
    )
    args = parser.parse_args()
    cfg = load_country_config(args.country)

    cfg.maps_dir.mkdir(parents=True, exist_ok=True)

    if not cfg.raster_path.exists():
        raise FileNotFoundError(f"Raster not found: {cfg.raster_path}")
    if not cfg.admin_vector_path.exists():
        raise FileNotFoundError(f"Vector layer not found: {cfg.admin_vector_path}")

    raster = load_ghi_raster(cfg.raster_path, cfg.crs_target)
    boundary_gdf = load_island_boundary(cfg.admin_vector_path, cfg.crs_target)

    raster_clip = raster.rio.clip(boundary_gdf.geometry, boundary_gdf.crs)

    clipped_path = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
    raster_clip.rio.to_raster(clipped_path)
    print(f"Clipped raster exported -> {clipped_path}")

    boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
    boundary_gdf.to_file(boundary_path, driver="GPKG", engine="pyogrio", mode="w")
    print(f"Country boundary exported -> {boundary_path}")

    assert clipped_path.exists(), "Output raster was not created"
    assert boundary_path.exists(), "Output GeoPackage was not created"

    print(f"[{cfg.country_code}] Pipeline step 1 completed successfully")


if __name__ == "__main__":
    main()