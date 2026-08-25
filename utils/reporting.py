"""
utils/reporting.py
====================
Generates machine-readable and human-readable reports from the artifacts
already produced by the pipeline (config metadata + QC results + the CSV
tables already written by 02_ghi_distribution.py and 03_optimal_zones.py).

Design principle: this module does NOT re-run any processing. It only
reads what already exists on disk (or was already computed in memory by
run_pipeline.py, e.g. the QC report) and assembles it into:

    outputs/<country_code>/reports/qc_report.json
    outputs/<country_code>/reports/processing_report.json
    outputs/<country_code>/reports/final_report.html
    outputs/global/maps/GHI_Comparative_Grid.png
    outputs/global/reports/global_report.html
    outputs/global/reports/global_report.json

This keeps 01_reproject_clip.py, 02_ghi_distribution.py, and
03_optimal_zones.py completely untouched - reporting is a pure
post-processing step over already-produced artifacts, called from
run_pipeline.py after a country's pipeline run succeeds, and from
04_global_report.py to aggregate across every already-processed country.

Design note on scientific warnings (added after the original three
report types above): utils/qc.py's checks each return a structured
{"errors", "warnings", "message"} dict, not just a printed console line.
_extract_qc_warnings() below reads these generically - for any check
name, any country - to populate a "Scientific Warnings" section in both
the per-country final_report.html and the cross-country
global_report.html, without either report ever hardcoding a country name
or a specific check's wording. A country not among the six currently
configured, run through this same pipeline, would have its own warnings
surfaced automatically the same way, with zero code changes required
anywhere in this module.
"""

from __future__ import annotations

import csv
import html as html_lib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import rioxarray
from matplotlib import colormaps
from mpl_toolkits.axes_grid1.anchored_artists import AnchoredSizeBar


# ---------------------------------------------------------------------
# Friendly labels for known CSV/JSON keys
# ---------------------------------------------------------------------
# Raw column/key names are snake_case and machine-oriented (e.g.
# "percent_country", "ghi_min_kwh_m2_day"). Left to naive title-casing,
# "percent_country" reads as "Percent Country" - which implies a
# percentage of the country's total OFFICIAL area, when it is actually
# the percentage of the ANALYZED polygon (the administrative vector
# layer's dissolved extent), which can differ slightly from an official
# national area figure (see the README's own note on this exact point:
# boundary tracing choices shift the total by a small margin). This
# mapping makes the report say what the number actually measures,
# rather than what a naive rendering of its column name would suggest.
FRIENDLY_LABELS = {
    "ghi_min_kwh_m2_day": "GHI minimum (kWh/m²/day)",
    "ghi_max_kwh_m2_day": "GHI maximum (kWh/m²/day)",
    "ghi_mean_kwh_m2_day": "GHI mean (kWh/m²/day)",
    "ghi_median_kwh_m2_day": "GHI median (kWh/m²/day)",
    "area_km2": "Optimal zone area (km²)",
    "total_area_km2": "Analyzed territory area (km²)",
    "percent_country": "Percentage of analyzed territory",
    "ghi_threshold_kwh_m2_day": "GHI threshold (kWh/m²/day)",
    "source_url": "Source URL",
}

# Friendly names for utils/qc.py's "additional_checks" keys, used both
# in each country's own QA/QC table and in the generic scientific
# warnings sections below.
FRIENDLY_LABELS.update({
    "finite_pixel_full_raster": "Finite Pixel Values (Full Raster)",
    "ghi_physical_plausibility": "GHI Physical Plausibility",
    "resolution_consistency": "Resolution Consistency",
    "boundary_raster_area": "Boundary/Raster Area Consistency",
    "admin_topology": "Admin Unit Topology",
    "threshold_plausibility": "Threshold Plausibility",
})

# Keys whose value should be rendered as a clickable <a href="..."> link
# instead of plain text, whenever a table is built from a dict that
# contains one of them (e.g. provenance.raster, provenance.vector).
LINK_KEYS = {"source_url"}


def _friendly(key: str) -> str:
    return FRIENDLY_LABELS.get(key, key.replace("_", " ").title())


def _render_cell_value(key: str, value) -> str:
    """
    Render a single table-cell value. If the key is a known link field
    (source_url) and the value looks like a real URL, wrap it in an
    anchor tag that opens in a new tab. Otherwise fall back to escaped
    plain text.
    """
    if value is None:
        return ""

    text = str(value)

    if key in LINK_KEYS and text.startswith(("http://", "https://")):
        safe_url = html_lib.escape(text, quote=True)
        return (
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">'
            f'{safe_url}'
            f'</a>'
        )

    return html_lib.escape(text)


# ---------------------------------------------------------------------
# Reading already-produced artifacts
# ---------------------------------------------------------------------

def read_csv_as_dict(csv_path: Path) -> dict | None:
    """
    Read a single-row CSV (as produced by 02_ghi_distribution.py and
    03_optimal_zones.py) into a flat dict of {column_name: value}.
    Returns None if the file doesn't exist yet (e.g. --report used
    without having actually run 02/03 first) - callers throughout this
    module treat None as "no data available", never as an error to
    surface loudly, since a missing artifact is an expected possibility
    depending on which flags a given pipeline run used.
    """
    if not csv_path.exists():
        return None

    with csv_path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    return rows[0] if rows else None


def read_ghi_statistics(cfg) -> dict | None:
    return read_csv_as_dict(cfg.tables_dir / "ghi_statistics.csv")


def read_optimal_zone_statistics(cfg) -> dict | None:
    return read_csv_as_dict(cfg.tables_dir / "optimal_solar_zones_area.csv")


# ---------------------------------------------------------------------
# Generic QC warning extraction - shared by individual and global reports
# ---------------------------------------------------------------------

def _extract_qc_warnings(
    qc_data: dict,
    ghi_stats: dict | None,
    exclude_checks: frozenset = frozenset(),
) -> list[dict]:
    """
    Generic extraction of warning-level messages from a QC result — this
    is the single mechanism behind BOTH the per-country "Scientific
    Warnings" section (render_final_report_html) and the cross-country
    one (build_scientific_warnings). It works identically whether
    qc_data is the in-memory dict returned directly by
    utils.qc.run_quality_control() (used right after a country's own
    pipeline run, before its qc_report.json even exists on disk yet) or
    that same structure reloaded from qc_report.json on disk (used by
    the global report, aggregating countries that were processed in
    earlier, separate runs) - both share the same "additional_checks"
    and "raster_crs_plausible" keys, by construction, since
    build_qc_report() writes qc_report.json directly from this same
    shape.

    Never references a specific country, check name, or message text -
    any check added to utils/qc.py's additional_checks dict in the
    future is picked up automatically, for any country run through the
    pipeline, including a country not among the six currently
    configured. This was the explicit design goal behind writing this
    function at all: before it existed, only two of QA/QC's ten checks
    (CRS/pixel plausibility and territory homogeneity) had their
    messages surfaced in reports at all, and every other check's
    warning - including the near-universal "finite pixel values (full
    raster)" one - was invisible outside the console log, forcing a
    hand-written, static paragraph to stand in for what should have been
    a dynamic, per-run message.

    exclude_checks optionally filters out specific check keys by name -
    used by the global (cross-country) report to omit
    "finite_pixel_full_raster" specifically, since that warning is
    present for essentially every country by construction (no raster's
    rectangular bounding box exactly matches an irregular national
    boundary), and would otherwise dilute the genuinely country-specific
    warnings in a cross-country view without adding any differentiating
    information there. The per-country report calls this function with
    no exclusion at all, since that same warning IS contextually
    meaningful when read alongside one specific country's own data.
    """
    warnings = []

    additional = qc_data.get("additional_checks") or {}
    for check_name, result in additional.items():
        if check_name in exclude_checks:
            continue
        if result and result.get("warnings", 0) > 0:
            warnings.append({"check": _friendly(check_name), "message": result.get("message", "")})

    if qc_data.get("raster_crs_plausible") is False:
        warnings.append({
            "check": "CRS/pixel plausibility",
            "message": "Declared raster CRS is inconsistent with its actual pixel coordinates.",
        })

    if ghi_stats:
        ghi_min = float(ghi_stats["ghi_min_kwh_m2_day"])
        ghi_max = float(ghi_stats["ghi_max_kwh_m2_day"])
        if (ghi_max - ghi_min) < 1.0:
            warnings.append({
                "check": "Territory homogeneity",
                "message": (f"GHI range across the territory is unusually narrow "
                            f"({ghi_min:.2f}-{ghi_max:.2f} kWh/m²/day) - expect limited "
                            f"internal color variation on the map; this is a genuine "
                            f"physical signal, not a rendering issue."),
            })

    return warnings


def _scientific_warnings_html(warnings: list[dict]) -> str:
    """
    Renders a list of {"check", "message"} dicts (from
    _extract_qc_warnings) as an HTML unordered list, or an explicit,
    reassuring statement when the list is empty - a clean run should
    say so plainly, not simply omit the section and leave a reader
    wondering whether warnings were checked for at all.
    """
    if not warnings:
        return "<p><em>No scientific warnings for this run - every applicable QA/QC check passed cleanly.</em></p>"
    items = "".join(f"<li><strong>{w['check']}:</strong> {w['message']}</li>" for w in warnings)
    return f"<ul>{items}</ul>"


# ---------------------------------------------------------------------
# qc_report.json
# ---------------------------------------------------------------------

def build_qc_report(cfg, qc_result: dict | None, metadata_warnings: list[str]) -> dict:
    """
    Assemble the machine-readable QC report.

    qc_result is the dict returned by utils.qc.run_quality_control(), or
    None if this pipeline run didn't include --check. metadata_warnings
    is the list of dotted paths returned by
    config_loader.find_placeholder_fields().

    "additional_checks" is included here in full — not just the
    aggregate error/warning COUNTS ("qc_errors"/"qc_warnings" below,
    which are plain integers, not lists of messages; see
    utils/database.py's check_qc_gate() for a concrete case where that
    distinction mattered, after an earlier version of that function
    mistakenly treated qc_errors as if it were a searchable list) - so
    that qc_report.json is a complete, standalone, self-describing
    record of every check qc.py actually ran, with its full message
    text, not merely a pass/fail summary that a reader would have to
    correlate with a separate console log to fully understand.
    """
    return {
        "country": cfg.country,
        "country_code": cfg.country_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qc_executed": qc_result is not None,
        "qc_status": qc_result["status"] if qc_result else "NOT_RUN",
        "qc_errors": qc_result["errors"] if qc_result else None,
        "qc_warnings": qc_result["warnings"] if qc_result else None,
        "raster_crs_plausible": qc_result["raster_crs_plausible"] if qc_result else None,
        "raster_reprojection_required": qc_result["raster_reprojection_required"] if qc_result else None,
        "vector_reprojection_required": qc_result["vector_reprojection_required"] if qc_result else None,
        "territory_stats": qc_result["territory_stats"] if qc_result else None,
        "additional_checks": qc_result["additional_checks"] if qc_result else None,
        "metadata_incomplete_fields": metadata_warnings,
    }


def save_qc_report(cfg, qc_result: dict | None, metadata_warnings: list[str]) -> Path:
    report = build_qc_report(cfg, qc_result, metadata_warnings)
    output_path = cfg.reports_dir / "qc_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return output_path


# ---------------------------------------------------------------------
# processing_report.json
# ---------------------------------------------------------------------

def build_processing_report(cfg, qc_result: dict | None, post_clip_check: dict | None = None) -> dict:
    """
    Assemble the machine-readable processing report: what CRS transform
    was actually needed (independent of whether QC ran at all - a
    country can be processed without --check, in which case these
    fields report "unknown" rather than silently guessing), the
    post-clip boundary/raster area check (see utils/qc.py's
    verify_clipped_raster_area, called from run_pipeline.py after
    01_reproject_clip.py has actually reprojected the raster - this is
    intentionally distinct from qc_report.json's pre-processing checks),
    and which final output artifacts currently exist on disk.
    """
    clipped_raster = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
    boundary = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
    optimal_zones = cfg.maps_dir / "optimal_zones.gpkg"

    return {
        "country": cfg.country,
        "country_code": cfg.country_code,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_crs": cfg.crs_target,
        "raster_reprojection_required": qc_result["raster_reprojection_required"] if qc_result else "unknown (run with --check)",
        "vector_reprojection_required": qc_result["vector_reprojection_required"] if qc_result else "unknown (run with --check)",
        "ghi_threshold_kwh_m2_day": cfg.ghi_threshold_kwh_m2_day,
        "post_clip_boundary_raster_area_check": post_clip_check,
        "outputs_present": {
            "clipped_raster": clipped_raster.exists(),
            "boundary": boundary.exists(),
            "optimal_zones_vector": optimal_zones.exists(),
            "ghi_distribution_map": (cfg.maps_dir / "Final_GHI_Distribution_Map.png").exists(),
            "optimal_zones_map": (cfg.maps_dir / "Optimal_Solar_Zones.png").exists(),
        },
    }


def save_processing_report(cfg, qc_result: dict | None, post_clip_check: dict | None = None) -> Path:
    report = build_processing_report(cfg, qc_result, post_clip_check)
    output_path = cfg.reports_dir / "processing_report.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    return output_path


# ---------------------------------------------------------------------
# final_report.html
# ---------------------------------------------------------------------

def _kv_table_rows(d: dict) -> str:
    if not d:
        return '<tr><td colspan="2"><em>No data available</em></td></tr>'
    rows = []
    for key, value in d.items():
        label = _friendly(key)
        cell = _render_cell_value(key, value)
        rows.append(f"<tr><td>{label}</td><td>{cell}</td></tr>")
    return "\n".join(rows)


def _provenance_table(provenance: dict) -> str:
    if not provenance:
        return "<p><em>No provenance metadata recorded for this country yet.</em></p>"

    blocks = []
    for source_type in ("raster", "vector"):
        entry = provenance.get(source_type)
        if not entry:
            continue
        blocks.append(f"<h3>{source_type.title()}</h3><table>{_kv_table_rows(entry)}</table>")
    return "\n".join(blocks) if blocks else "<p><em>No provenance metadata recorded for this country yet.</em></p>"


def _administrative_unit_table(administrative_unit: dict) -> str:
    """
    Renders the administrative_unit block (source, level, level_name) as
    its own report section - distinct from provenance.vector, which
    covers licensing/dataset identity. This section answers "what
    granularity was the analysis performed at", a methodological detail
    a reader needs even if they don't care about licensing terms at all.
    """
    if not administrative_unit:
        return "<p><em>No administrative unit metadata recorded for this country yet.</em></p>"
    return f"<table>{_kv_table_rows(administrative_unit)}</table>"


def _status_badge(status: str) -> str:
    colors = {"PASS": "#2e7d32", "WARNING": "#e65100", "ERROR": "#c62828", "NOT_RUN": "#757575"}
    color = colors.get(status, "#757575")
    return f'<span style="background:{color};color:white;padding:3px 10px;border-radius:4px;font-weight:bold;">{status}</span>'


def render_final_report_html(
    cfg,
    qc_result: dict | None,
    metadata_warnings: list[str],
    ghi_stats: dict | None,
    zone_stats: dict | None,
) -> str:
    """
    Build the full HTML report as a single self-contained string, in
    eight sections: provenance, administrative unit, QA/QC summary,
    scientific warnings (see _extract_qc_warnings above - this section
    is what replaced the old, single hardcoded paragraph about the
    "finite pixel values" warning with a dynamic, per-run, per-check
    list), GHI statistics, optimal zone statistics, maps, and output
    file paths.
    """

    qc_status = qc_result["status"] if qc_result else "NOT_RUN"
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    warnings_html = ""
    if metadata_warnings:
        items = "".join(f"<li><code>{w}</code></li>" for w in metadata_warnings)
        warnings_html = f"""
        <div class="warning-box">
            <strong>Incomplete metadata:</strong> the following fields are still
            placeholders in config/{cfg.country_code.lower()}.json and should be
            finalized before this report is considered publication-ready.
            <ul>{items}</ul>
        </div>
        """

    if qc_result is None:
        scientific_warnings_block = "<p><em>QC was not executed for this run (generated without --check) - no warnings to report.</em></p>"
    else:
        scientific_warnings_block = _scientific_warnings_html(_extract_qc_warnings(qc_result, ghi_stats))

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Solar Potential Report - {cfg.country}</title>
<style>
    body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px;
            margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.5; }}
    h1 {{ border-bottom: 3px solid #2E4B3C; padding-bottom: 8px; }}
    h2 {{ color: #2E4B3C; margin-top: 40px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
    h3 {{ color: #444; margin-top: 20px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    td {{ padding: 6px 10px; border-bottom: 1px solid #eee; }}
    td:first-child {{ font-weight: 600; width: 40%; color: #444; }}
    td a {{ color: #2E4B3C; word-break: break-all; }}
    img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
    .warning-box {{ background: #fff3e0; border-left: 4px solid #e65100; padding: 12px 16px; margin: 16px 0; }}
    .meta {{ color: #777; font-size: 0.9em; }}
    ul {{ padding-left: 22px; }}
    li {{ margin-bottom: 8px; }}
</style>
</head>
<body>

<h1>Solar Potential Toolkit - {cfg.country}</h1>
<p class="meta">Generated: {generated_at} · Target CRS: {cfg.crs_target} · GHI threshold: {cfg.ghi_threshold_kwh_m2_day} kWh/m²/day</p>

{warnings_html}

<h2>1. Data Provenance</h2>
{_provenance_table(cfg.provenance)}

<h2>2. Administrative Unit</h2>
{_administrative_unit_table(cfg.administrative_unit)}

<h2>3. QA / QC</h2>
<p>Status: {_status_badge(qc_status)}</p>
<table>
{_kv_table_rows({
    "Errors": qc_result["errors"] if qc_result else "n/a",
    "Warnings": qc_result["warnings"] if qc_result else "n/a",
    "Raster reprojection required": qc_result["raster_reprojection_required"] if qc_result else "n/a (run with --check)",
    "Vector reprojection required": qc_result["vector_reprojection_required"] if qc_result else "n/a (run with --check)",
})}
</table>

<h2>4. Scientific Warnings</h2>
<p class="meta">Auto-generated directly from this run's QA/QC results - not written by hand.
A warning here does not necessarily indicate a data error: it flags a result worth
double-checking against intent (e.g. a threshold sitting near the extreme of a
territory's observed range, or a raster whose bounding box extends well beyond
the territory itself).</p>
{scientific_warnings_block}

<h2>5. GHI Statistics</h2>
<table>{_kv_table_rows(ghi_stats or {})}</table>

<h2>6. Optimal Solar Zones</h2>
<table>{_kv_table_rows(zone_stats or {})}</table>
<p class="meta">"Percentage of analyzed territory" is computed against the dissolved
extent of the administrative vector layer used for this run - not necessarily an
official national area figure (see Administrative Unit section above for the source).</p>

<h2>7. Maps</h2>
<img src="../maps/Final_GHI_Distribution_Map.png" alt="GHI distribution map">
<img src="../maps/Optimal_Solar_Zones.png" alt="Optimal solar zones map">

<h2>8. Output Files</h2>
<table>
{_kv_table_rows({
    "GHI statistics": "tables/ghi_statistics.csv",
    "Optimal zones area": "tables/optimal_solar_zones_area.csv",
    "Optimal zones (vector)": "maps/optimal_zones.gpkg",
    "Admin centroids": "maps/admin_centroids.gpkg",
})}
</table>

</body>
</html>"""
    return html


def save_final_report_html(
    cfg,
    qc_result: dict | None,
    metadata_warnings: list[str],
    ghi_stats: dict | None,
    zone_stats: dict | None,
) -> Path:
    html = render_final_report_html(cfg, qc_result, metadata_warnings, ghi_stats, zone_stats)
    output_path = cfg.reports_dir / "final_report.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# ---------------------------------------------------------------------
# Top-level per-country orchestrator, called from run_pipeline.py
# ---------------------------------------------------------------------

def generate_country_report(cfg, qc_result: dict | None, metadata_warnings: list[str], post_clip_check: dict | None = None) -> dict:
    """
    Orchestrates the three per-country report writes. Reads GHI/zone
    statistics directly from the CSVs already written by
    02_ghi_distribution.py / 03_optimal_zones.py - never recomputes
    them, in keeping with this module's core design principle that
    reporting is pure post-processing over existing artifacts.
    """
    ghi_stats = read_ghi_statistics(cfg)
    zone_stats = read_optimal_zone_statistics(cfg)

    qc_path = save_qc_report(cfg, qc_result, metadata_warnings)
    processing_path = save_processing_report(cfg, qc_result, post_clip_check)
    html_path = save_final_report_html(cfg, qc_result, metadata_warnings, ghi_stats, zone_stats)

    print(f"[{cfg.country_code}] Reports written:")
    print(f"    {qc_path}")
    print(f"    {processing_path}")
    print(f"    {html_path}")

    return {"qc_report": qc_path, "processing_report": processing_path, "final_report_html": html_path}


# ---------------------------------------------------------------------
# Multi-country comparative report (outputs/global/)
# ---------------------------------------------------------------------
# Design principle, same as the rest of this module: read what already
# exists on disk (per-country ghi_statistics.csv, qc_report.json,
# processing_report.json, clipped rasters) - do not re-run any
# processing. A country only appears in the global report if it has
# already been through the pipeline at least once with --check --report.


def _country_configs_with_reports(all_configs: list):
    """
    Filter a list of CountryConfig objects down to those that have
    already produced a qc_report.json - i.e., have actually been run
    through the pipeline, not merely configured in config/*.json. This
    is what lets 04_global_report.py be called against ALL configured
    countries indiscriminately (list_available_countries() scans
    config/*.json regardless of processing state) while still only
    aggregating the ones that are actually ready.
    """
    return [cfg for cfg in all_configs if (cfg.reports_dir / "qc_report.json").exists()]


def _load_all_country_stats(configs: list) -> dict[str, dict]:
    """
    Single source of truth for per-country GHI/zone statistics used
    across the entire global report: read once per country here, then
    passed as a parameter to every function below that needs them
    (compute_atlas_scale, render_comparative_grid_png,
    build_global_summary_rows, build_scientific_warnings) instead of
    each one calling read_ghi_statistics()/read_optimal_zone_statistics()
    independently. Reading independently in multiple places was not an
    active bug - all reads happen against the same on-disk CSVs within
    one save_global_report() call, so values always agreed in practice -
    but it was a latent fragility: nothing enforced that they stayed in
    sync if one of the reading sites were ever changed later to read
    from a different source. Centralizing the read removes that
    possibility structurally, rather than relying on convention.

    Returns {country_code: {"ghi": dict | None, "zone": dict | None}}.
    """
    stats_by_country = {}
    for cfg in configs:
        stats_by_country[cfg.country_code] = {
            "ghi": read_ghi_statistics(cfg),
            "zone": read_optimal_zone_statistics(cfg),
        }
    return stats_by_country


def compute_atlas_scale(configs: list, stats_by_country: dict[str, dict]) -> tuple[float, float]:
    """
    Derive a shared vmin/vmax for the multi-country comparison map from
    the ACTUALLY OBSERVED GHI min/max across every already-processed
    country - not a round number picked by eye. This is intentionally
    separate from each country's own config "plot.vmin"/"plot.vmax",
    which stay per-country and tuned to that country's own data range
    for the INDIVIDUAL map (see 02_ghi_distribution.py) - a shared scale
    only makes sense for the side-by-side comparison, where flattening
    every country onto the same color range is the whole point; using
    each country's own tuned scale there would make different countries'
    colors incomparable to one another.

    Reads from the pre-loaded stats_by_country (see
    _load_all_country_stats) rather than re-reading CSVs itself.
    """
    mins, maxs = [], []
    for cfg in configs:
        ghi_stats = stats_by_country.get(cfg.country_code, {}).get("ghi")
        if ghi_stats:
            mins.append(float(ghi_stats["ghi_min_kwh_m2_day"]))
            maxs.append(float(ghi_stats["ghi_max_kwh_m2_day"]))

    if not mins:
        return 1.4, 6.0  # fallback if nothing has been run yet

    return min(mins), max(maxs)


# ---------------------------------------------------------------------
# Comparative grid helpers: uniform panel sizing, scale bars, display
# downsampling
# ---------------------------------------------------------------------
# These helpers exist to fix specific readability and scalability
# problems in the small-multiples grid:
#   - matplotlib's default aspect="equal" behaviour (adjustable="box")
#     SHRINKS each subplot's spines/border to fit the data, so a wide,
#     flat country (e.g. Haïti) ends up with a visibly smaller frame
#     than a taller one (e.g. Kenya) even though both were given
#     identical figure cells - adjustable="datalim" is used instead, to
#     keep every panel's frame the same physical size.
#   - plotting a very large raster at full resolution, as-is, for a
#     small preview image can exhaust memory once several countries are
#     composed into one figure - _downsample_for_display() decimates the
#     DISPLAY COPY only, never the actual analysis data.


def _nice_scale_bar_length_km(width_km: float) -> float:
    """Pick a round scale-bar length, roughly a quarter of the panel width."""
    target = width_km / 4
    if target <= 0:
        return 10.0
    magnitude = 10 ** math.floor(math.log10(target))
    for mult in (1, 2, 5, 10):
        candidate = mult * magnitude
        if candidate >= target:
            return candidate
    return 10 * magnitude


def _add_scale_bar(ax, raster) -> None:
    """
    Draw a small km scale bar in the bottom-left of a panel, sized from
    the raster's own extent and CRS. Geographic (lat/lon) rasters are
    converted to an approximate km-per-degree at the raster's own center
    latitude; projected rasters are assumed to already be in meters.
    Wrapped in a broad try/except: a scale bar is a visual nicety, and a
    failure here (e.g. an unusual or missing CRS) must never break
    report generation as a whole.
    """
    try:
        crs = raster.rio.crs
        x_vals = raster.x.values
        xmin, xmax = float(x_vals.min()), float(x_vals.max())
        width_native = xmax - xmin

        if crs is not None and crs.is_geographic:
            lat_center = float(raster.y.values.mean())
            meters_per_native_unit = 111_320 * math.cos(math.radians(lat_center))
        else:
            meters_per_native_unit = 1.0

        width_km = width_native * meters_per_native_unit / 1000
        if width_km <= 0:
            return

        bar_km = _nice_scale_bar_length_km(width_km)
        bar_native = bar_km * 1000 / meters_per_native_unit

        scalebar = AnchoredSizeBar(
            ax.transData,
            bar_native,
            f"{bar_km:.0f} km",
            loc="lower left",
            pad=0.4,
            borderpad=0.6,
            color="black",
            frameon=False,
            size_vertical=width_native * 0.008,
            fontproperties={"size": 8},
        )
        ax.add_artist(scalebar)
    except Exception:
        pass


def _resolution_label(raster) -> str:
    """Approximate ground pixel size (m/px or km/px), for the stats box."""
    try:
        crs = raster.rio.crs
        res_x, res_y = raster.rio.resolution()
        res_x, res_y = abs(res_x), abs(res_y)
        if crs is not None and crs.is_geographic:
            lat_center = float(raster.y.values.mean())
            meters_per_degree = 111_320 * math.cos(math.radians(lat_center))
            res_m = ((res_x + res_y) / 2) * meters_per_degree
        else:
            res_m = (res_x + res_y) / 2
        if res_m >= 1000:
            return f"~{res_m / 1000:.1f} km/px"
        return f"~{res_m:.0f} m/px"
    except Exception:
        return ""


def _downsample_for_display(raster, max_dim: int = 1000):
    """
    Decimate a raster (pixel skipping, not resampling/averaging) purely
    for the comparative grid's on-screen render, to bound the memory a
    matplotlib colormap-mapped render needs regardless of how large a
    given country's raster is. This does NOT touch any analysis data:
    it applies only to the in-memory copy used inside this function,
    never to the raster read/written by 01-03, and never to the
    statistics in ghi_statistics.csv / optimal_solar_zones_area.csv,
    which remain full-resolution and unaffected. Added after an
    ArrayMemoryError was encountered composing a large-raster country
    into a nine-country grid during an extensibility test - a scalability
    concern generic to any sufficiently large raster, not specific to
    the six countries this toolkit ships with.
    """
    ny, nx = raster.shape[-2], raster.shape[-1]
    factor = max(1, max(ny, nx) // max_dim)
    if factor <= 1:
        return raster
    return raster.isel(y=slice(None, None, factor), x=slice(None, None, factor))


def render_comparative_grid_png(
    configs: list,
    vmin: float,
    vmax: float,
    output_path: Path,
    stats_by_country: dict[str, dict],
) -> Path | None:
    """
    Render a small-multiples grid: one subplot per already-processed
    country, each showing its own clipped GHI raster (downsampled for
    display only - see _downsample_for_display) with its national
    boundary outline overlaid, plus a compact stats box (mean, min, max,
    % >= threshold, approximate resolution, and a "homogeneous
    territory" note when the observed GHI range is unusually narrow -
    see Sénégal's own case in the README's methodological discussion)
    read from stats_by_country (see _load_all_country_stats - never
    re-read here). All panels share the same vmin/vmax and colormap so
    colors are directly comparable across countries - without
    mosaicking rasters that live in different CRS into a single
    continuous map, a much heavier operation not justified for this
    use case.

    Panel frames are all forced to the same physical size (adjustable=
    "datalim" instead of matplotlib's default "box") so countries with
    very different shapes - a wide, flat Haïti next to a taller Kenya -
    line up in a clean grid instead of each frame shrinking to its own
    data's aspect ratio. Each panel also gets a small km scale bar,
    since the shared color scale alone doesn't convey that every
    country here is shown at a different physical scale.

    The boundary overlay makes the figure's attribution line accurate:
    it credits boundary sources because boundaries are now actually
    drawn on the figure, not merely used internally for clipping as in
    an earlier version of this function.
    """
    rasters = []
    for cfg in configs:
        clipped_path = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
        if clipped_path.exists():
            rasters.append((cfg, clipped_path))

    if not rasters:
        return None

    n = len(rasters)
    ncols = math.ceil(math.sqrt(n))
    nrows = math.ceil(n / ncols)

    custom_cmap = colormaps["YlOrRd"].copy()
    custom_cmap.set_bad(color="white")

    # Extra row height + hspace/wspace: panel frames are now forced to a
    # uniform size (adjustable="datalim", see below), so - unlike a
    # naive default - they no longer shrink to leave natural gaps
    # between rows on their own. Without this, a bottom-row country
    # title would collide with the stats box of the row above it.
    fig, axes = plt.subplots(
        nrows, ncols, figsize=(4.8 * ncols, 5.3 * nrows),
        gridspec_kw={"hspace": 0.65, "wspace": 0.3},
    )
    axes = np.atleast_1d(axes).flatten()

    last_im = None
    for ax, (cfg, clipped_path) in zip(axes, rasters):
        raster = rioxarray.open_rasterio(clipped_path, masked=True).squeeze()

        # True resolution/scale bar are computed from the FULL raster
        # (extent and pixel size are unaffected by decimation - only
        # sample count changes); only the plotted array is downsampled.
        res_label = _resolution_label(raster)
        display_raster = _downsample_for_display(raster)

        last_im = display_raster.plot(
            ax=ax, cmap=custom_cmap, vmin=vmin, vmax=vmax, add_colorbar=False,
        )

        boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
        if boundary_path.exists():
            boundary = gpd.read_file(boundary_path, engine="pyogrio")
            boundary.boundary.plot(ax=ax, color="black", linewidth=0.8)

        # Keep every panel's frame the same physical size (grid cell),
        # padding the shorter axis with whitespace instead of shrinking
        # the frame - this is what fixes the Haïti/Kenya size mismatch.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_anchor("C")

        _add_scale_bar(ax, raster)

        ax.set_title(cfg.country, fontsize=12, fontweight="bold", pad=12)
        ax.set_xlabel("")
        ax.set_ylabel("")
        ax.set_xticks([])
        ax.set_yticks([])

        country_stats = stats_by_country.get(cfg.country_code, {})
        ghi_stats = country_stats.get("ghi")
        zone_stats = country_stats.get("zone")
        if ghi_stats and zone_stats:
            ghi_min = float(ghi_stats["ghi_min_kwh_m2_day"])
            ghi_max = float(ghi_stats["ghi_max_kwh_m2_day"])
            # A near-flat range renders as a solid color block on the
            # shared scale (e.g. Sénégal at 5.3-5.9 kWh/m²/day): that's
            # a real, correct result, not a rendering bug, so it's
            # called out explicitly rather than left to look empty.
            homogeneous_note = "  ·  homogeneous territory" if (ghi_max - ghi_min) < 1.0 else ""
            res_line = f"\n{res_label}{homogeneous_note}" if (res_label or homogeneous_note) else ""

            stats_text = (
                f"Mean {float(ghi_stats['ghi_mean_kwh_m2_day']):.2f}  ·  "
                f"Min {ghi_min:.2f}  ·  Max {ghi_max:.2f}\n"
                f"\u2265 {float(zone_stats['ghi_threshold_kwh_m2_day']):.0f} kWh/m\u00b2/day: "
                f"{float(zone_stats['percent_country']):.1f}% of territory"
                f"{res_line}"
            )
            ax.text(
                0.5, -0.10, stats_text,
                transform=ax.transAxes, ha="center", va="top",
                fontsize=8, fontfamily="monospace",
                bbox=dict(facecolor="white", edgecolor="#ccc", alpha=0.85, boxstyle="round,pad=0.3"),
            )

    for ax in axes[len(rasters):]:
        ax.axis("off")

    fig.suptitle(
        f"Global GHI Comparison Across Study Countries\n"
        f"(shared scale: {vmin:.1f}\u2013{vmax:.1f} kWh/m\u00b2/day)",
        fontsize=15, fontweight="bold", y=0.98,
    )
    fig.subplots_adjust(top=0.90)

    if last_im is not None:
        cbar = fig.colorbar(last_im, ax=axes.tolist(), shrink=0.8, label="GHI (kWh/m²/day)")

    fig.text(
        0.5, 0.01,
        "Solar Potential Toolkit · GHI comparative analysis · Lenz Arly Ch\u00e9ry\n"
        "GHI data: Global Solar Atlas · Boundaries: country-specific sources (see each country's final_report.html for full provenance) · "
        "Panels share a color scale but each is at its own physical scale (see per-panel scale bar)",
        ha="center", fontsize=8, color="#666",
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=200, bbox_inches="tight")
    plt.close(fig)

    return output_path


def build_global_summary_rows(configs: list, stats_by_country: dict[str, dict]) -> list[dict]:
    """
    One row per already-processed country, for the global report's
    summary table. zone_stats comes from stats_by_country (see
    _load_all_country_stats) - the same pre-loaded values used by
    render_comparative_grid_png, so both cannot silently disagree even
    if one of them is edited later to change how it sources its data.

    _to_float converts each raw CSV string to a real float at the
    source: csv.DictReader always returns strings, never numbers, and
    an earlier version of this function passed those raw strings
    straight through, which crashed the first time this table's
    formatting code tried to apply a numeric format specifier to a
    string value.
    """
    rows = []
    for cfg in configs:
        qc_path = cfg.reports_dir / "qc_report.json"
        qc_data = json.loads(qc_path.read_text(encoding="utf-8")) if qc_path.exists() else {}
        zone_stats = stats_by_country.get(cfg.country_code, {}).get("zone") or {}

        def _to_float(value):
            return float(value) if value not in (None, "") else None

        rows.append({
            "country": cfg.country,
            "country_code": cfg.country_code,
            "qc_status": qc_data.get("qc_status", "NOT_RUN"),
            "raster_crs_plausible": qc_data.get("raster_crs_plausible"),
            "analyzed_territory_km2": _to_float(zone_stats.get("total_area_km2")),
            "optimal_zone_km2": _to_float(zone_stats.get("area_km2")),
            "optimal_zone_pct": _to_float(zone_stats.get("percent_country")),
            "vector_provider": cfg.provenance.get("vector", {}).get("provider", "n/a"),
            "vector_version": cfg.provenance.get("vector", {}).get("version", "n/a"),
        })
    return rows


def build_scientific_warnings(cfg, stats_by_country: dict[str, dict]) -> list[dict]:
    """
    Cross-country (global report) version of warning extraction: reads
    qc_report.json from disk (this country was processed in a separate,
    earlier pipeline run - not in the current process's memory), and
    excludes "finite_pixel_full_raster" specifically - present for
    essentially every country by construction (no raster's rectangular
    bounding box exactly matches an irregular national boundary), so it
    would add repetitive noise rather than country-differentiating
    signal at this cross-country comparison level. It remains fully
    visible in each country's own individual report (see
    render_final_report_html), where it is contextually meaningful on
    its own.
    """
    qc_path = cfg.reports_dir / "qc_report.json"
    if not qc_path.exists():
        return []
    qc_data = json.loads(qc_path.read_text(encoding="utf-8"))
    ghi_stats = stats_by_country.get(cfg.country_code, {}).get("ghi")
    return _extract_qc_warnings(qc_data, ghi_stats, exclude_checks=frozenset({"finite_pixel_full_raster"}))


def _plausibility_badge(value) -> str:
    if value is True:
        return '<span style="color:#2e7d32;font-weight:bold;">PASS</span>'
    if value is False:
        return '<span style="color:#c62828;font-weight:bold;">FAIL</span>'
    return '<span style="color:#757575;">n/a</span>'


def render_global_report_html(
    configs: list,
    grid_png_path: Path | None,
    vmin: float,
    vmax: float,
    stats_by_country: dict[str, dict],
) -> str:
    """
    Build the full cross-country HTML report, in three sections: the
    shared-scale comparative grid, per-country scientific warnings (see
    build_scientific_warnings above), and a summary table including an
    automatically generated data-vintage heterogeneity note whenever
    more than one distinct vector provider/version is present among the
    processed countries - never written by hand, so it stays accurate
    even as countries with different data vintages are added or removed.
    """
    rows = build_global_summary_rows(configs, stats_by_country)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    table_rows = ""
    for r in rows:
        area_str = f"{r['analyzed_territory_km2']:.0f}" if r["analyzed_territory_km2"] is not None else "n/a"
        zone_str = f"{r['optimal_zone_km2']:.0f}" if r["optimal_zone_km2"] is not None else "n/a"
        pct_str = f"{r['optimal_zone_pct']:.1f}%" if r["optimal_zone_pct"] is not None else "n/a"
        table_rows += f"""
        <tr>
            <td><a href="../../{r['country_code'].lower()}/reports/final_report.html">{r['country']}</a></td>
            <td>{r['qc_status']}</td>
            <td>{_plausibility_badge(r['raster_crs_plausible'])}</td>
            <td>{area_str}</td>
            <td>{zone_str}</td>
            <td>{pct_str}</td>
            <td>{r['vector_provider']} ({r['vector_version']})</td>
        </tr>"""

    # Note on source heterogeneity, generated from actual provenance data
    # rather than written by hand - see the discussion on data vintage
    # differing between GADM v2.8 (Réunion), OCHA/CNIGS (Haïti), and
    # GADM v4.1 (Madagascar, Maroc, Kenya, Sénégal).
    versions = sorted({f"{r['vector_provider']} ({r['vector_version']})" for r in rows})
    heterogeneity_note = ""
    if len(versions) > 1:
        items = "".join(f"<li>{v}</li>" for v in versions)
        heterogeneity_note = f"""
        <div class="warning-box">
            <strong>Data vintage note:</strong> administrative boundary sources differ
            across countries in this comparison. Any cross-country reading of
            boundary-dependent figures (e.g. per-commune area) should account for
            this rather than treat it as a geographic signal.
            <ul>{items}</ul>
        </div>
        """

    # Auto-generated per-country scientific warnings - never hardcoded,
    # works for any country present in `configs`, including ones added
    # purely to test the pipeline's extensibility (verified against a
    # nine-country run, see the README's Extensibility Test).
    scientific_warnings_html = ""
    any_warnings = False
    for cfg in configs:
        country_warnings = build_scientific_warnings(cfg, stats_by_country)
        if country_warnings:
            any_warnings = True
            items = "".join(
                f"<li><strong>{w['check']}:</strong> {w['message']}</li>"
                for w in country_warnings
            )
            scientific_warnings_html += f"<h3>{cfg.country}</h3><ul>{items}</ul>"

    if not any_warnings:
        scientific_warnings_html = "<p><em>No scientific warnings flagged for any processed country.</em></p>"

    grid_html = ""
    if grid_png_path is not None:
        grid_html = f'<img src="../maps/{grid_png_path.name}" alt="GHI comparison grid">'
    else:
        grid_html = "<p><em>No comparative map available yet - run the pipeline for at least one country with --report first.</em></p>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Solar Potential Toolkit - Global Report</title>
<style>
    body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 1100px;
            margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.5; }}
    h1 {{ border-bottom: 3px solid #2E4B3C; padding-bottom: 8px; }}
    h2 {{ color: #2E4B3C; margin-top: 40px; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
    h3 {{ color: #444; margin-top: 20px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ padding: 8px 10px; border-bottom: 1px solid #eee; text-align: left; }}
    th {{ background: #2E4B3C; color: white; }}
    img {{ max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin: 10px 0; }}
    .warning-box {{ background: #fff3e0; border-left: 4px solid #e65100; padding: 12px 16px; margin: 16px 0; }}
    .meta {{ color: #777; font-size: 0.9em; }}
</style>
</head>
<body>

<h1>Solar Potential Toolkit - Global Report</h1>
<p class="meta">Generated: {generated_at} · {len(rows)} country(ies) · Shared comparison scale: {vmin:.1f}-{vmax:.1f} kWh/m²/day</p>

<h2>1. GHI Comparison (shared scale)</h2>
{grid_html}
<p class="meta">Countries are shown in their own coordinate system (no cross-country
mosaicking); only the color scale is shared, to keep GHI values visually comparable
without the additional complexity and precision loss of reprojecting every raster
into one continuous map.</p>

<h2>2. Scientific Warnings by Country</h2>
<p class="meta">Auto-generated from each country's qc_report.json and ghi_statistics.csv -
not written by hand. The near-universal "finite pixel values (full raster)" warning
(present for every country, since no raster's bounding box exactly matches its
territory) is intentionally omitted here to keep this section focused on
genuinely country-specific findings - it remains fully visible in each country's
own individual report.</p>
{scientific_warnings_html}

<h2>3. Country Summary</h2>
{heterogeneity_note}
<table>
<tr><th>Country</th><th>QC Status</th><th>CRS/Pixel Plausibility</th><th>Analyzed Territory (km²)</th><th>Optimal Zone (km²)</th><th>Optimal %</th><th>Admin Boundary Source</th></tr>
{table_rows}
</table>

</body>
</html>"""
    return html


def save_global_report(all_configs: list) -> dict:
    """
    Top-level entry point, called from 04_global_report.py. Filters
    all_configs down to countries that have already been processed
    (_country_configs_with_reports), loads each processed country's
    GHI/zone statistics ONCE (_load_all_country_stats), computes the
    shared atlas scale from that same pre-loaded data, renders the
    comparative grid PNG and the global HTML report - passing the same
    stats_by_country dict everywhere it's needed, never re-reading it -
    and writes everything under outputs/global/.
    """
    processed = _country_configs_with_reports(all_configs)

    if not processed:
        print("No countries have been processed yet (no qc_report.json found). "
              "Run the pipeline for at least one country with --check --report first.")
        return {}

    stats_by_country = _load_all_country_stats(processed)

    global_root = processed[0].reports_dir.parent.parent / "global"
    maps_dir = global_root / "maps"
    reports_dir = global_root / "reports"
    maps_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    vmin, vmax = compute_atlas_scale(processed, stats_by_country)
    grid_path = render_comparative_grid_png(
        processed, vmin, vmax, maps_dir / "GHI_Comparative_Grid.png", stats_by_country
    )

    html = render_global_report_html(processed, grid_path, vmin, vmax, stats_by_country)
    html_path = reports_dir / "global_report.html"
    html_path.write_text(html, encoding="utf-8")

    summary_json = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "countries_processed": [cfg.country for cfg in processed],
        "shared_scale_vmin": vmin,
        "shared_scale_vmax": vmax,
        "rows": build_global_summary_rows(processed, stats_by_country),
    }
    json_path = reports_dir / "global_report.json"
    json_path.write_text(json.dumps(summary_json, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Global report written:\n    {grid_path}\n    {html_path}\n    {json_path}")

    return {"grid_png": grid_path, "html": html_path, "json": json_path}