"""
qc.py - Geospatial Quality Control Module
=========================================

This module provides reusable QA/QC utilities for raster and vector data.
It is used by scripts/00_inspect_data.py before running the processing pipeline.

Features:
    - Raster QC (CRS, resolution, bounds, finite values)
    - Vector QC (CRS, geometry validity, required fields)
    - Raster/Vector spatial compatibility
    - Raster statistics computed ONLY inside the vector territory
    - CRS/pixel plausibility check (catches a declared-but-not-reprojected
      raster - write_crs() used where reproject() was actually needed)
    - Additional scientific checks: finite pixel coverage (full raster),
      GHI physical plausibility, pixel resolution consistency,
      boundary/raster area consistency, admin unit topology (overlap
      detection), GHI threshold plausibility
    - PASS / WARNING / ERROR reporting

This module does NOT modify input data. It does NOT make execution
decisions (e.g. stopping a pipeline) - that responsibility belongs to
the calling script (run_pipeline.py), which decides whether a given
WARNING or ERROR should halt processing (see --strict-crs).

Design note on message structure: every check below - the five in
"additional scientific checks", plus finite pixel coverage - returns a
dict shaped {"errors": int, "warnings": int, "message": str}, never just
a printed line. This is deliberate: utils/reporting.py's
_extract_qc_warnings() reads these structured messages generically, for
any check and any country, to populate both the per-country
final_report.html and the cross-country global_report.html - without
ever hardcoding a country name or a specific check's wording anywhere in
the reporting layer. A check that only printed to console would be
invisible to that mechanism.
"""

from pathlib import Path
import numpy as np
import geopandas as gpd
import rasterio
import rasterio.mask
from rasterio.features import geometry_mask
from pyproj import CRS, Transformer
from shapely.geometry import box


# ---------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------

def _normalize_crs(crs):
    if crs is None:
        return None
    return CRS.from_user_input(crs)


def _status_symbol(status):
    symbols = {
        "PASS": "[PASS]",
        "WARNING": "[WARN]",
        "ERROR": "[ERROR]",
        "INFO": "[INFO]",
    }
    return symbols.get(status, "[INFO]")


def _print_check(label, status, message):
    print(f"{_status_symbol(status):8} {label:<35} {message}")


# ---------------------------------------------------------------------
# CRS / pixel plausibility check
# ---------------------------------------------------------------------

def _to_pyproj_crs(crs):
    """
    Convert any CRS-like input (rasterio CRS, EPSG int, WKT string, pyproj
    CRS, ...) into a pyproj CRS that is properly linked to its EPSG
    registry entry when one exists.

    Necessary because CRS.from_user_input() on a rasterio CRS object can
    silently lose the EPSG authority link during WKT round-tripping,
    which causes area_of_use to return None even for a standard, fully
    defined CRS like EPSG:4326. Rebuilding the CRS explicitly from its
    numeric EPSG code (rather than trusting the round-tripped object)
    restores that link, so downstream code that relies on area_of_use
    (see check_crs_pixel_plausibility below) works reliably.
    """
    crs_obj = CRS.from_user_input(crs)
    epsg_code = crs_obj.to_epsg()
    if epsg_code is not None:
        crs_obj = CRS.from_epsg(epsg_code)
    return crs_obj


def check_crs_pixel_plausibility(bounds, crs):
    """
    Heuristic consistency check between a raster's DECLARED CRS and its
    actual pixel coordinate bounds - catches the case where a CRS was
    manually assigned to a file (e.g. via rio.write_crs()) without the
    pixels having actually been reprojected. This is the central
    diagnostic of this project's QC discipline: a raster can carry a
    perfectly valid-looking CRS tag while its pixels are still, in
    reality, in a completely different coordinate system - a silent
    failure mode that write_crs() cannot itself detect, because it
    trusts whatever CRS it is told to assign.

    Uses the CRS's own "area of use" (the geographic region for which
    that CRS is valid, per the EPSG registry) as reference: the raster's
    bounds are converted back to WGS84 lon/lat using the DECLARED CRS
    (via always_xy=True, to force a consistent lon/lat axis order
    regardless of the CRS's own historical axis convention), then
    checked against that area of use. If they fall far outside it, the
    declared CRS is very likely inconsistent with the actual pixel grid.

    This is a PLAUSIBILITY check, not a proof of correctness: a bug could
    theoretically still pass it (e.g. a raster mistakenly reprojected to
    a neighboring, still-plausible CRS). It exists specifically to catch
    the write_crs()-instead-of-reproject() failure mode, not every
    possible reprojection bug.
    """
    crs_obj = _to_pyproj_crs(crs)
    area = crs_obj.area_of_use

    if area is None:
        return {
            "checked": False,
            "plausible": None,
            "reason": "No area_of_use metadata available for this CRS.",
        }

    try:
        transformer = Transformer.from_crs(crs_obj, "EPSG:4326", always_xy=True)
        lon_min, lat_min = transformer.transform(bounds.left, bounds.bottom)
        lon_max, lat_max = transformer.transform(bounds.right, bounds.top)
    except Exception as exc:
        return {
            "checked": False,
            "plausible": None,
            "reason": f"Coordinate transform failed: {exc}",
        }

    margin = 2.0  # degrees of tolerance, to avoid false positives at the CRS's edge
    plausible = (
        (area.west - margin) <= lon_min <= (area.east + margin)
        and (area.west - margin) <= lon_max <= (area.east + margin)
        and (area.south - margin) <= lat_min <= (area.north + margin)
        and (area.south - margin) <= lat_max <= (area.north + margin)
    )

    return {
        "checked": True,
        "plausible": plausible,
        "raster_bounds_wgs84": (lon_min, lat_min, lon_max, lat_max),
        "crs_area_of_use": (area.west, area.south, area.east, area.north),
    }


# ---------------------------------------------------------------------
# Raster QC
# ---------------------------------------------------------------------

def inspect_raster(raster_path, finite_warn_threshold=90):
    """
    First-pass raster diagnostics: CRS, dimensions, bands, resolution,
    bounds, CRS/pixel plausibility, and finite-pixel coverage over the
    FULL raster (its rectangular bounding box, not the actual territory
    shape - see compute_raster_stats_on_vector() below for the
    territory-scoped equivalent, which is the scientifically meaningful
    figure for an irregularly-shaped or concave country).
    """
    raster_path = Path(raster_path)

    if not raster_path.exists():
        _print_check("Raster file", "ERROR", f"File not found: {raster_path}")
        return {"exists": False, "errors": 1, "warnings": 0}

    results = {
        "exists": True,
        "errors": 0,
        "warnings": 0,
        "crs": None,
        "bounds": None,
        "resolution": None,
        "finite_pct": None,
        "crs_pixel_plausibility": None,
    }

    with rasterio.open(raster_path) as src:
        print("\n=== RASTER QUALITY CONTROL ===")

        results["crs"] = src.crs
        results["bounds"] = src.bounds
        results["resolution"] = src.res

        # CRS
        if src.crs is None:
            results["errors"] += 1
            _print_check("Raster CRS", "ERROR", "No CRS embedded.")
        else:
            _print_check("Raster CRS", "PASS", str(src.crs))

        # Dimensions
        _print_check("Raster dimensions", "PASS", f"{src.width} x {src.height}")

        # Bands
        _print_check("Raster bands", "PASS", str(src.count))

        # Resolution
        if src.res[0] <= 0 or src.res[1] <= 0:
            results["errors"] += 1
            _print_check("Raster resolution", "ERROR", str(src.res))
        else:
            _print_check("Raster resolution", "PASS", f"{src.res[0]} x {src.res[1]}")

        # Bounds
        _print_check("Raster bounds", "PASS", str(src.bounds))

        # CRS / pixel plausibility check - catches a declared-but-not-
        # reprojected raster (write_crs() used where reproject() was needed)
        if src.crs is not None:
            plausibility = check_crs_pixel_plausibility(src.bounds, src.crs)
            results["crs_pixel_plausibility"] = plausibility
            if plausibility["checked"]:
                if plausibility["plausible"]:
                    _print_check("CRS/pixel plausibility", "PASS", "Bounds consistent with declared CRS.")
                else:
                    results["warnings"] += 1
                    wgs84 = plausibility["raster_bounds_wgs84"]
                    _print_check(
                        "CRS/pixel plausibility", "WARNING",
                        f"Bounds fall outside the CRS's valid area of use "
                        f"(reconverted to WGS84: {wgs84[0]:.2f}, {wgs84[1]:.2f} -> "
                        f"{wgs84[2]:.2f}, {wgs84[3]:.2f}). CRS may have been "
                        f"assigned without actually reprojecting the pixels."
                    )
            else:
                _print_check("CRS/pixel plausibility", "INFO", plausibility["reason"])
        else:
            results["crs_pixel_plausibility"] = {"checked": False, "plausible": None, "reason": "No CRS declared."}

        # Finite values (whole raster). A low percentage here is EXPECTED
        # whenever the raster's rectangular bounding box extends beyond
        # the territory itself (over ocean, neighboring countries, etc.)
        # — it does not, by itself, indicate a data quality problem. The
        # scientifically valid figure is the territory-scoped finite_pct
        # computed later, in compute_raster_stats_on_vector(). This
        # check exists mainly to catch the case where even the
        # territory-scoped figure would be affected — e.g. a genuinely
        # corrupted or mostly-empty source file.
        data = src.read(1, masked=True).filled(np.nan)
        finite = np.isfinite(data)

        finite_pct = finite.mean() * 100
        results["finite_pct"] = finite_pct

        status = "WARNING" if finite_pct < finite_warn_threshold else "PASS"
        message = (
            f"{finite_pct:.2f} % (expected to be low when the raster's bounding box "
            f"extends beyond the territory, e.g. over ocean or neighboring countries; "
            f"see 'Finite pixel values (territory)' below for the scientifically valid figure)."
        )
        if status == "WARNING":
            results["warnings"] += 1
        _print_check("Finite pixel values (full raster)", status, message)

        # Stored structurally (not just printed) so downstream reporting
        # can surface this exact message generically, for any country,
        # the same way it already does for the additional_checks below -
        # see utils/reporting.py's _extract_qc_warnings(). Before this
        # was added, this particular warning's explanation only ever
        # existed as a hardcoded paragraph duplicated across every
        # report template, disconnected from the actual check that
        # produced it.
        results["finite_pixel_check"] = {
            "errors": 0,
            "warnings": 1 if status == "WARNING" else 0,
            "message": message,
        }

        valid = data[finite]
        _print_check("Value range (full raster)", "PASS", f"{np.min(valid):.4f} → {np.max(valid):.4f}")
        _print_check("Mean value (full raster)", "PASS", f"{np.mean(valid):.4f}")

    return results


# ---------------------------------------------------------------------
# Raster QC inside vector territory
# ---------------------------------------------------------------------

def compute_raster_stats_on_vector(raster_path, vector_path, finite_warn_threshold=90):
    """
    Compute raster statistics ONLY inside the vector territory, rather
    than over the raster's full rectangular bounding box.

    finite_pct here is computed relative to pixels actually inside the
    polygon (not relative to the full bounding-box crop), so an
    irregular or concave country shape (e.g. Haïti's peninsulas) doesn't
    produce an artificially low finite_pct just because of ocean or
    background pixels that fall inside the bbox but outside the actual
    polygon. This distinction - territory-scoped vs. bbox-scoped
    coverage - was the original motivation for adding this function:
    without it, a perfectly healthy raster for a concave country could
    misleadingly look like it had large gaps in coverage.

    Also returns total_area_km2: the raster-derived area of the
    territory (all pixels inside the polygon, finite or not, times pixel
    area) - used by check_boundary_raster_area_consistency() to
    cross-check against the administrative boundary's own geometric
    area, as an independent sanity check on the clip/reprojection step.
    """

    with rasterio.open(raster_path) as src:
        gdf = gpd.read_file(vector_path, engine="pyogrio")

        # Reproject vector to raster CRS
        gdf = gdf.to_crs(src.crs)

        # Mask raster with vector geometry
        out_image, out_transform = rasterio.mask.mask(
            src,
            gdf.geometry,
            crop=True,
            filled=True,
            nodata=np.nan
        )

        data = out_image[0]

        # True = pixel outside polygon (to invert below)
        outside_polygon = geometry_mask(
            gdf.geometry,
            out_shape=data.shape,
            transform=out_transform,
            invert=False,  # True where OUTSIDE the geometry
        )
        inside_polygon = ~outside_polygon

        pixel_area_km2 = abs(out_transform.a * out_transform.e) / 1e6
        total_area_km2 = float(inside_polygon.sum()) * pixel_area_km2

        # Denominator = only pixels actually inside the country shape
        finite_inside = np.isfinite(data) & inside_polygon
        total_inside = inside_polygon.sum()

        if total_inside == 0:
            return {
                "finite_pct": 0.0,
                "min": None,
                "max": None,
                "mean": None,
                "total_area_km2": total_area_km2,
                "warnings": 1,
            }

        finite_pct = finite_inside.sum() / total_inside * 100
        valid = data[finite_inside]
        warnings = 1 if finite_pct < finite_warn_threshold else 0

        return {
            "finite_pct": finite_pct,
            "min": float(np.min(valid)),
            "max": float(np.max(valid)),
            "mean": float(np.mean(valid)),
            "total_area_km2": total_area_km2,
            "warnings": warnings,
        }


# ---------------------------------------------------------------------
# Vector QC
# ---------------------------------------------------------------------

def inspect_vector(vector_path, required_field=None):
    """
    Basic vector diagnostics: CRS presence, feature count, geometry
    validity (via Shapely's is_valid, which catches self-intersections
    and other topological errors), and presence of the administrative
    name field the rest of the pipeline expects (cfg.admin_name_field).
    """
    vector_path = Path(vector_path)

    if not vector_path.exists():
        _print_check("Vector file", "ERROR", f"File not found: {vector_path}")
        return {"exists": False, "errors": 1, "warnings": 0}

    gdf = gpd.read_file(vector_path, engine="pyogrio")

    results = {"exists": True, "errors": 0, "warnings": 0, "crs": gdf.crs}

    print("\n=== VECTOR QUALITY CONTROL ===")

    # CRS
    if gdf.crs is None:
        results["errors"] += 1
        _print_check("Vector CRS", "ERROR", "No CRS embedded.")
    else:
        _print_check("Vector CRS", "PASS", str(gdf.crs))

    # Features
    _print_check("Vector features", "PASS", str(len(gdf)))

    # Geometry validity
    invalid = (~gdf.geometry.is_valid).sum()
    if invalid > 0:
        results["errors"] += 1
        _print_check("Invalid geometries", "ERROR", str(invalid))
    else:
        _print_check("Invalid geometries", "PASS", "0")

    # Required field
    if required_field:
        if required_field in gdf.columns:
            _print_check("Required attribute", "PASS", required_field)
        else:
            results["errors"] += 1
            _print_check("Required attribute", "ERROR", f"Missing: {required_field}")

    return results


# ---------------------------------------------------------------------
# CRS QC
# ---------------------------------------------------------------------

def check_reprojection_required(source_crs, target_crs, name):
    """
    Compares two CRS objects by SEMANTIC equivalence (via pyproj's
    CRS.equals()), not by string representation. Two CRS definitions can
    be identical in meaning while stringifying differently (e.g. an EPSG
    code vs. an equivalent WKT/PROJ string) - a naive string comparison
    would misread that as "different" and trigger an unnecessary
    reprojection. This function's result feeds directly into
    run_pipeline.py's decision of whether 01_reproject_clip.py actually
    needs to call .rio.reproject(), or whether the source is already in
    the target CRS.
    """
    source = _normalize_crs(source_crs)
    target = _normalize_crs(target_crs)

    if source is None:
        _print_check(f"{name} CRS", "ERROR", "Undefined.")
        return None

    if source.equals(target):
        _print_check(f"{name} reprojection", "PASS", "Not required.")
        return False

    _print_check(f"{name} reprojection", "INFO", f"{source.to_string()} → {target.to_string()}")
    return True


# ---------------------------------------------------------------------
# Spatial compatibility
# ---------------------------------------------------------------------

def check_spatial_compatibility(raster_path, vector_path, target_crs, coverage_warn_threshold=95):
    """
    Confirms the raster and vector actually overlap spatially, and
    computes what fraction of the vector's own area falls within the
    raster's bounding box. A complete mismatch (no overlap at all) is a
    hard ERROR - it usually means the wrong raster/vector pair was
    configured for a country. A partial overlap below the warning
    threshold is a WARNING, not an error, since some legitimate
    scenarios (a raster download that clips at a coastline slightly
    differently than the administrative vector) can produce a coverage
    slightly below 100% without being a real problem.
    """
    print("\n=== RASTER / VECTOR COMPATIBILITY ===")

    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
        raster_bounds = src.bounds
        raster_box = box(raster_bounds.left, raster_bounds.bottom,
                         raster_bounds.right, raster_bounds.top)

    vector = gpd.read_file(vector_path, engine="pyogrio")

    vector_projected = vector.to_crs(raster_crs)

    # Fuse all administrative polygons into a single geometry (works
    # across geopandas versions: union_all() where available, unary_union
    # otherwise)
    vector_geometry = (
        vector_projected.geometry.union_all() if hasattr(vector_projected.geometry, "union_all")
        else vector_projected.geometry.unary_union
    )

    overlap = vector_geometry.intersection(raster_box)

    if overlap.is_empty:
        _print_check("Raster/vector overlap", "ERROR", "No intersection.")
        return {"overlap": False, "coverage_pct": 0.0, "errors": 1, "warnings": 0}

    vector_area = vector_geometry.area
    overlap_area = overlap.area

    coverage_pct = (overlap_area / vector_area) * 100

    _print_check("Raster/vector overlap", "PASS", "Intersection detected.")

    coverage_status = "PASS" if coverage_pct > coverage_warn_threshold else "WARNING"
    warnings = 1 if coverage_status == "WARNING" else 0
    _print_check("Territory coverage", coverage_status, f"{coverage_pct:.2f} %")

    return {"overlap": True, "coverage_pct": coverage_pct, "errors": 0, "warnings": warnings}


# ---------------------------------------------------------------------
# Additional scientific checks (physical plausibility, resolution,
# boundary/raster area consistency, admin topology, threshold plausibility)
# ---------------------------------------------------------------------

def check_ghi_physical_plausibility(min_val, max_val, min_expected=0.0, max_expected=8.0):
    """
    Sanity check on GHI values themselves, independent of geometry or
    CRS entirely. GHI is a non-negative physical quantity; values above
    roughly 8 kWh/m²/day are implausible even in the sunniest deserts on
    Earth and usually indicate a unit error, a corrupted file, or a
    wrong NoData sentinel value that slipped through masking upstream.
    This check operates purely on the numeric distribution of the data,
    with no dependency on the raster's CRS, resolution, or the
    administrative vector at all - it would catch a bad value even if
    every other check in this module passed.
    """
    errors, warnings = 0, 0
    if min_val is not None and min_val < min_expected:
        errors += 1
        message = f"Minimum value {min_val:.3f} is below the physical floor ({min_expected})."
        _print_check("GHI physical plausibility", "ERROR", message)
    elif max_val is not None and max_val > max_expected:
        warnings += 1
        message = f"Maximum value {max_val:.3f} exceeds the typical physical ceiling ({max_expected}); verify units/source."
        _print_check("GHI physical plausibility", "WARNING", message)
    else:
        message = f"Range [{min_val:.2f}, {max_val:.2f}] is physically plausible."
        _print_check("GHI physical plausibility", "PASS", message)
    return {"errors": errors, "warnings": warnings, "message": message}


def check_resolution_consistency(res_x, res_y, tolerance_pct=2.0):
    """
    Flags a distorted pixel grid (res_x significantly different from
    res_y) - a classic, silent symptom of a reprojection or warp bug.
    Square-ish pixels are expected for this pipeline's UTM-based target
    CRS. Non-square pixels most often mean the raster was reprojected
    OUTSIDE this pipeline (e.g. by hand in QGIS), producing a different
    output grid than this pipeline's own .rio.reproject() would compute
    - expected and documented for such a raster (see La Réunion's
    methodological note in the README), but worth flagging so it can be
    cross-checked against any threshold-based figure derived from it,
    since a threshold classification is more sensitive to grid placement
    than a simple mean.
    """
    diff_pct = abs(res_x - res_y) / max(abs(res_x), 1e-12) * 100
    if diff_pct > tolerance_pct:
        message = (
            f"res_x={res_x:.4f} vs res_y={res_y:.4f} differ by {diff_pct:.1f}% "
            f"(tolerance {tolerance_pct}%). Non-square pixels often mean the raster "
            f"was reprojected outside this pipeline (e.g. in GIS software), producing "
            f"a different output grid than .rio.reproject() would - expected if so, "
            f"but worth cross-checking any threshold-based figures derived from it."
        )
        _print_check("Resolution consistency", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}
    message = f"res_x={res_x:.4f}, res_y={res_y:.4f} (within tolerance)."
    _print_check("Resolution consistency", "PASS", message)
    return {"errors": 0, "warnings": 0, "message": message}


def check_boundary_raster_area_consistency(boundary_geom, raster_total_area_km2, tolerance_pct=5.0):
    """
    Compares the administrative boundary's own geometric area to the
    raster-derived area (pixel count times pixel area) covering that
    same territory. A large mismatch is a real signal of a clipping or
    reprojection problem - this is the direct methodological descendant
    of the original investigation into Haïti's finite_pct behavior,
    generalized into a reusable check.

    Only numerically meaningful once BOTH inputs are in a projected CRS
    (meters) - see run_quality_control() below, which deliberately skips
    calling this during the pre-processing phase (while the raster is
    still in its raw geographic CRS) and instead calls
    verify_clipped_raster_area() after 01_reproject_clip.py has actually
    reprojected the raster.
    """
    boundary_area_km2 = boundary_geom.area / 1e6
    if boundary_area_km2 <= 0:
        message = "Boundary area is zero or undefined; skipped."
        _print_check("Boundary/raster area consistency", "INFO", message)
        return {"errors": 0, "warnings": 0, "message": message}

    diff_pct = abs(boundary_area_km2 - raster_total_area_km2) / boundary_area_km2 * 100
    if diff_pct > tolerance_pct:
        message = (f"Boundary area {boundary_area_km2:.1f} km² vs raster-derived area "
                    f"{raster_total_area_km2:.1f} km² differ by {diff_pct:.1f}% (tolerance {tolerance_pct}%).")
        _print_check("Boundary/raster area consistency", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}
    message = (f"Boundary {boundary_area_km2:.1f} km² vs raster {raster_total_area_km2:.1f} km² "
               f"({diff_pct:.1f}% difference).")
    _print_check("Boundary/raster area consistency", "PASS", message)
    return {"errors": 0, "warnings": 0, "message": message}


def check_admin_topology(gdf, target_crs=None, overlap_tolerance_pct=1.0):
    """
    Detects overlapping or duplicated polygons in the administrative
    layer via an O(n) area comparison: sum(individual polygon areas) vs.
    area(dissolved union). If polygons overlap or are duplicated, the
    sum exceeds the union area; if they tile cleanly with no overlap,
    the two values match. This scales to a large admin dataset (e.g.
    Kenya's ~300 units) without the pairwise O(n²) comparison a naive
    "check every pair of polygons for overlap" approach would require.

    Reprojects gdf to target_crs before computing area, when provided.
    This check runs in the pre-processing QC phase, before
    01_reproject_clip.py has reprojected anything - so without this
    reprojection step, gdf.geometry.area would be computed directly on
    the source vector's CRS, which for most sources here (GADM, OCHA) is
    geographic (EPSG:4326). geopandas raises a UserWarning in that case
    ("Results from 'area' are likely incorrect") because degree-based
    area is not a real area - the individual/union RATIO used here is
    fairly tolerant of this distortion (the same distortion affects both
    numerator and denominator similarly, so it largely cancels out), but
    it is not fully rigorous, especially for a large, high-latitude-
    spread country. Reprojecting first removes both the warning and the
    residual inaccuracy at their source, rather than merely tolerating
    them.
    """
    if target_crs is not None and gdf.crs is not None:
        try:
            gdf = gdf.to_crs(target_crs)
        except Exception:
            pass  # fall back to computing on the original CRS rather than failing the check

    individual_sum = gdf.geometry.area.sum()
    union_area = gdf.geometry.union_all().area if hasattr(gdf.geometry, "union_all") else gdf.geometry.unary_union.area

    if union_area <= 0:
        message = "Union area is zero or undefined; skipped."
        _print_check("Admin unit topology", "INFO", message)
        return {"errors": 0, "warnings": 0, "message": message}

    overlap_pct = (individual_sum - union_area) / union_area * 100
    if overlap_pct > overlap_tolerance_pct:
        message = (f"Individual polygon areas exceed the dissolved union area by {overlap_pct:.2f}% "
                    f"(tolerance {overlap_tolerance_pct}%) - possible overlapping or duplicated polygons.")
        _print_check("Admin unit topology", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}
    message = f"No significant overlap detected ({overlap_pct:.2f}%)."
    _print_check("Admin unit topology", "PASS", message)
    return {"errors": 0, "warnings": 0, "message": message}


def check_threshold_plausibility(threshold, ghi_min, ghi_max):
    """
    Diagnoses how much spatial discrimination a GHI threshold can
    provide within a territory's observed GHI range - not whether the
    threshold is scientifically appropriate. The threshold itself is a
    modeling choice (see the README's discussion of 5.0 kWh/m²/day as an
    operational criterion, not a physical limit); this check only
    reports where that choice falls relative to what the data can
    actually distinguish.

    A threshold sitting outside, or near the edge of, the observed range
    does not classify incorrectly - it classifies almost the entire
    territory the same way, which may be the correct, intended result
    (a genuinely sun-rich, homogeneous territory, as with Sénégal here)
    or may signal that the threshold should be revisited for that
    specific territory. Position is expressed as a percentage of the
    observed range: 0% = territory minimum, 100% = territory maximum.
    This distinction - diagnosing discriminating power vs. judging
    scientific validity - matters because a WARNING here must never be
    read as "this threshold is wrong": Sénégal's territory minimum
    (5.34) sitting above the 5.0 threshold, producing exactly 100%
    optimal coverage, is a correct and physically meaningful result, not
    a defect this check should be understood to be flagging as an error.
    """
    if ghi_min is None or ghi_max is None or ghi_max <= ghi_min:
        message = "Insufficient GHI range data; threshold plausibility check skipped."
        _print_check("Threshold plausibility", "INFO", message)
        return {"errors": 0, "warnings": 0, "message": message}

    span = ghi_max - ghi_min
    position_pct = (threshold - ghi_min) / span * 100

    if threshold <= ghi_min:
        message = (
            f"Threshold {threshold:.2f} is at or below the observed territory minimum "
            f"({ghi_min:.2f}). Every valid pixel already meets or exceeds it, so the "
            f"threshold provides no spatial discrimination here - the entire analyzed "
            f"territory is classified as optimal. This may be the correct, intended "
            f"result for a sun-rich territory; verify it matches intent."
        )
        _print_check("Threshold plausibility", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}

    if threshold >= ghi_max:
        message = (
            f"Threshold {threshold:.2f} is at or above the observed territory maximum "
            f"({ghi_max:.2f}). No pixel meets it, so the threshold provides no spatial "
            f"discrimination here - little to no area will be classified as optimal. "
            f"Verify this matches intent."
        )
        _print_check("Threshold plausibility", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}

    if position_pct < 10 or position_pct > 90:
        message = (
            f"Threshold {threshold:.2f} sits at {position_pct:.0f}% of the observed "
            f"range [{ghi_min:.2f}, {ghi_max:.2f}] - near the edge of what the data can "
            f"discriminate. Near-total or near-empty optimal coverage is likely; verify "
            f"this matches intent."
        )
        _print_check("Threshold plausibility", "WARNING", message)
        return {"errors": 0, "warnings": 1, "message": message}

    message = (
        f"Threshold {threshold:.2f} sits at {position_pct:.0f}% of the observed range "
        f"[{ghi_min:.2f}, {ghi_max:.2f}] - within the range where the data can "
        f"meaningfully discriminate optimal from non-optimal zones."
    )
    _print_check("Threshold plausibility", "PASS", message)
    return {"errors": 0, "warnings": 0, "message": message}


# ---------------------------------------------------------------------
# Post-clip verification (run AFTER 01_reproject_clip.py, from run_pipeline.py)
# ---------------------------------------------------------------------

def verify_clipped_raster_area(clipped_raster_path, boundary_path, tolerance_pct=5.0):
    """
    Post-processing verification, run AFTER 01_reproject_clip.py has
    produced the clipped, reprojected raster and boundary - NOT during
    the pre-processing QC pass (run_quality_control), which still works
    on the raw input raster in its original, often geographic
    (degree-based), CRS.

    On a geographic-CRS raster, "pixel area" expressed in degrees² and
    divided by 1e6 is not a real area - it is a near-zero number
    regardless of the true territory size - so comparing it directly to
    the boundary's real geometric area (computed in a projected CRS,
    meters) always produces a spurious ~100% mismatch. This was
    discovered as an actual bug during development: every one of the six
    countries showed this same misleading warning, traced back to this
    exact CRS-mismatch cause, not a real data problem. This function
    instead reads the ALREADY reprojected and clipped raster (meters)
    together with the freshly-written boundary (also meters), so the
    comparison this performs is numerically meaningful from the start.
    """
    with rasterio.open(clipped_raster_path) as src:
        data = src.read(1, masked=True)
        pixel_area_km2 = abs(src.res[0] * src.res[1]) / 1e6
        finite_mask = ~np.ma.getmaskarray(data) if np.ma.is_masked(data) else np.isfinite(data)
        raster_area_km2 = float(np.count_nonzero(finite_mask)) * pixel_area_km2

    boundary_gdf = gpd.read_file(boundary_path, engine="pyogrio")
    boundary_geom = (
        boundary_gdf.geometry.union_all() if hasattr(boundary_gdf.geometry, "union_all")
        else boundary_gdf.geometry.unary_union
    )

    return check_boundary_raster_area_consistency(boundary_geom, raster_area_km2, tolerance_pct=tolerance_pct)


# ---------------------------------------------------------------------
# Full QC
# ---------------------------------------------------------------------

def run_quality_control(raster_path, vector_path, target_crs, required_field=None,
                         ghi_threshold=None, boundary_path=None):
    """
    Orchestrates the full ten-check QA/QC suite for one country, in two
    phases:

    PRE-PROCESSING phase (on the raw, un-reprojected input data):
      raster/vector CRS presence, CRS/pixel plausibility, finite pixel
      coverage (full raster and territory-scoped), geometry validity,
      raster/vector spatial compatibility, GHI physical plausibility,
      resolution consistency, admin unit topology.

    POST-PROCESSING gate: boundary/raster area consistency is skipped
    here if the raster is still in a geographic CRS (see the docstring
    of check_boundary_raster_area_consistency and
    verify_clipped_raster_area above for why) - the real post-clip
    verification happens later, from run_pipeline.py, after
    01_reproject_clip.py has actually run.

    Returns a single dict combining all of this into one qc_status
    (PASS/WARNING/ERROR), aggregate error/warning counts, and a nested
    "additional_checks" dict - the latter is what utils/reporting.py
    reads generically to populate both per-country and cross-country
    reports, without this function or reporting.py ever needing to know
    which specific country is being processed.
    """

    print("\n" + "=" * 70)
    print("GEOSPATIAL DATA QUALITY CONTROL")
    print("=" * 70)

    print(f"Target CRS: {target_crs}")

    raster = inspect_raster(raster_path)
    vector = inspect_vector(vector_path, required_field)

    print("\n=== RASTER VALUES (TERRITORY ONLY) ===")
    territory = compute_raster_stats_on_vector(raster_path, vector_path)

    _print_check("Finite pixel values (territory)",
                 "PASS" if territory["finite_pct"] > 90 else "WARNING",
                 f"{territory['finite_pct']:.2f} %")

    _print_check("Value range (territory)", "PASS",
                 f"{territory['min']:.4f} → {territory['max']:.4f}")

    _print_check("Mean value (territory)", "PASS",
                 f"{territory['mean']:.4f}")

    print("\n=== CRS / REPROJECTION CHECK ===")
    raster_rep = check_reprojection_required(raster["crs"], target_crs, "Raster")
    vector_rep = check_reprojection_required(vector["crs"], target_crs, "Vector")

    # Explicit, unambiguous summary line for downstream scripts / quick reading
    reprojection_needed = bool(raster_rep) or bool(vector_rep)
    reprojection_label = "REPROJECTION REQUIRED" if reprojection_needed else "REPROJECTION NOT REQUIRED"
    _print_check("Reprojection summary", "INFO" if reprojection_needed else "PASS", reprojection_label)

    spatial = check_spatial_compatibility(raster_path, vector_path, target_crs)

    print("\n=== ADDITIONAL SCIENTIFIC CHECKS ===")

    physical = check_ghi_physical_plausibility(territory["min"], territory["max"])
    res_check = check_resolution_consistency(*raster["resolution"])
    topology = check_admin_topology(gpd.read_file(vector_path, engine="pyogrio"), target_crs=target_crs)

    boundary_check = {"errors": 0, "warnings": 0, "message": "Skipped (raster still geographic; see post-clip check)."}
    raster_is_geographic = _to_pyproj_crs(raster["crs"]).is_geographic if raster.get("crs") else True

    if boundary_path is not None and raster_is_geographic:
        # This check is only numerically meaningful once the raster is in
        # a projected CRS (meters) - see verify_clipped_raster_area(),
        # run AFTER 01_reproject_clip.py, for the real post-clip check.
        _print_check(
            "Boundary/raster area consistency", "INFO",
            "Skipped: raster is still in a geographic CRS (pre-reprojection) - "
            "degree-based pixel area is not comparable to the boundary's "
            "geometric area. See processing_report.json for the post-clip check."
        )
    elif boundary_path is not None and not raster_is_geographic:
        boundary_gdf = gpd.read_file(boundary_path, engine="pyogrio")
        boundary_geom = (
            boundary_gdf.geometry.union_all() if hasattr(boundary_gdf.geometry, "union_all")
            else boundary_gdf.geometry.unary_union
        )
        boundary_check = check_boundary_raster_area_consistency(
            boundary_geom, territory.get("total_area_km2", 0) or 0,
        )

    threshold_check = {"errors": 0, "warnings": 0, "message": "Skipped (no threshold provided)."}
    if ghi_threshold is not None:
        threshold_check = check_threshold_plausibility(ghi_threshold, territory["min"], territory["max"])

    errors = (
        raster["errors"] + vector["errors"] + spatial["errors"]
        + physical["errors"] + res_check["errors"] + topology["errors"]
        + boundary_check["errors"] + threshold_check["errors"]
    )
    warnings = (
        raster["warnings"] + vector["warnings"] + spatial["warnings"] + territory["warnings"]
        + physical["warnings"] + res_check["warnings"] + topology["warnings"]
        + boundary_check["warnings"] + threshold_check["warnings"]
    )

    print("\n" + "=" * 70)
    status = "ERROR" if errors > 0 else "WARNING" if warnings > 0 else "PASS"
    print(f"QC STATUS: {status}")
    print(f"Errors: {errors} | Warnings: {warnings}")
    print("=" * 70)

    plausibility = raster.get("crs_pixel_plausibility", {}) or {}
    raster_crs_plausible = plausibility.get("plausible")  # True / False / None (None = not checked)

    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "territory_stats": territory,
        "spatial": spatial,
        "raster_reprojection_required": raster_rep,
        "vector_reprojection_required": vector_rep,
        "raster_crs_plausible": raster_crs_plausible,
        "additional_checks": {
            "finite_pixel_full_raster": raster.get(
                "finite_pixel_check", {"errors": 0, "warnings": 0, "message": ""}
            ),
            "ghi_physical_plausibility": physical,
            "resolution_consistency": res_check,
            "admin_topology": topology,
            "boundary_raster_area": boundary_check,
            "threshold_plausibility": threshold_check,
        },
    }