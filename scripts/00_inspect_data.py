"""
00_inspect_data.py
===================
Quality control entry point for the solar potential pipeline.

Runs the full QC suite (utils/qc.py) on a country's raster + vector
inputs, then prints an explicit reprojection decision summary - useful
before running 01_reproject_clip.py, to know whether the raster's pixel
grid actually needs rio.reproject(), or whether the CRS just needs to be
assigned/declared (e.g. a raster already reprojected upstream in QGIS).

Also passes the country's GHI threshold (for the threshold-plausibility
check) and, if it already exists on disk, the country boundary produced
by 01_reproject_clip.py (for the boundary/raster area-consistency check).
The boundary file won't exist on a first-ever run for a country - in
that case the corresponding check is skipped (not failed), and will
start running once 01 has been executed at least once.

Usage:
    python scripts/00_inspect_data.py --country haiti
"""

import sys
from pathlib import Path

# Ajoute la racine du projet au sys.path (parent du dossier scripts/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import argparse

from config_loader import load_country_config
from utils.qc import run_quality_control


parser = argparse.ArgumentParser(
    description="Inspect and validate geospatial input data."
)

parser.add_argument(
    "--country",
    required=True,
)

args = parser.parse_args()

cfg = load_country_config(args.country)

# Only pass the boundary if 01_reproject_clip.py has already produced it -
# on a first-ever run for this country it won't exist yet, and the
# boundary/raster area-consistency check will simply be skipped (INFO,
# not a failure) until it does.
boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
boundary_path = boundary_path if boundary_path.exists() else None

report = run_quality_control(
    raster_path=cfg.raster_path,
    vector_path=cfg.admin_vector_path,
    target_crs=cfg.crs_target,
    required_field=cfg.admin_name_field,
    ghi_threshold=cfg.ghi_threshold_kwh_m2_day,
    boundary_path=boundary_path,
)

# Explicit, script-level summary - useful before deciding whether
# 01_reproject_clip.py needs to actually call rio.reproject(), or
# whether the raster's CRS just needs to be assigned/declared (as for
# a raster already reprojected upstream in QGIS, e.g. Réunion).
raster_needs_reproj = report["raster_reprojection_required"]
vector_needs_reproj = report["vector_reprojection_required"]

print("\n" + "=" * 70)
print("REPROJECTION DECISION SUMMARY")
print("=" * 70)
print(f"Raster reprojection : {'REQUIRED' if raster_needs_reproj else 'NOT REQUIRED'}")
print(f"Vector reprojection : {'REQUIRED' if vector_needs_reproj else 'NOT REQUIRED'}")
print("=" * 70)

# Optional: fail the QC run explicitly if data quality issues were found,
# useful when this script is chained in a larger workflow (CI, run_pipeline.py)
if report["status"] == "ERROR":
    sys.exit(1)