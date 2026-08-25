"""
utils/database.py
===================
Optional PostgreSQL/PostGIS export layer for the Solar Potential Toolkit.

Design principle, consistent with utils/reporting.py: this module never
re-runs any processing. It only reads artifacts already written to disk
by scripts/01-03 (CSV tables, GeoPackages) and writes them to the
database - called from scripts/05_export_database.py.

Two safety mechanisms keep the database consistent with the pipeline's
own state:

    1. QC gate (check_qc_gate): refuses to export a country whose QC
       run shows an execution failure, a QC error status, or a
       CRS/pixel plausibility problem - never a silent pass on missing
       or inconclusive QC evidence.

    2. Config/database synchronization (sync_database_to_configs):
       export_country()'s idempotency (delete-then-insert) is scoped to
       the one country it is called for - it never touches rows for a
       country that has since been removed from config/*.json. Without
       an explicit sync step, a country temporarily added (e.g. for an
       extensibility test) then later removed would leave stale rows in
       the database indefinitely. sync_database_to_configs closes that
       gap by comparing the database's country_code values against the
       currently-configured countries and deleting anything no longer
       present - called from scripts/05_export_database.py --all only,
       after every currently-configured country has been (re-)exported.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

# .env lives in env/.env (not env/.env.example), one level below the
# project root - NOT at the project root itself, and NOT auto-discovered
# by python-dotenv's default upward search, so the path is given
# explicitly here. See env/.env.example for the template to copy.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / "env" / ".env"

if not ENV_PATH.exists():
    raise EnvironmentError(
        f"{ENV_PATH} not found. Copy env/.env.example to env/.env and "
        f"fill in your local database credentials before running "
        f"database export."
    )

load_dotenv(dotenv_path=ENV_PATH)

REQUIRED_ENV_VARS = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"]


# ---------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------

def get_engine() -> Engine:
    """
    Build a SQLAlchemy engine from environment variables. Raises a clear
    error (rather than a raw connection failure) if .env is missing or
    incomplete.
    """
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {missing}. "
            f"Check env/.env against env/.env.example - a variable may be "
            f"present but left empty."
        )

    schema = os.getenv("DB_SCHEMA", "public")
    url = (
        f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}"
        f"@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
    )
    # search_path includes "public" alongside the target schema, because
    # PostGIS's "geometry" type lives in the "public" schema (where the
    # extension was created) - without it, an unqualified "geometry"
    # column type in a CREATE TABLE (as emitted by geopandas.to_postgis())
    # fails with "type geometry does not exist", even though every table
    # access in this module is already schema-qualified explicitly.
    return create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})


def _ensure_postgis(engine: Engine) -> None:
    """
    Fails fast with a clear, actionable message if the PostGIS extension
    is not enabled in the target database, rather than letting the first
    geometry-table write fail deep inside geopandas.to_postgis() with the
    far more cryptic 'type "geometry" does not exist'. This matters
    especially for anyone cloning this repo without a geospatial-database
    background, who may not know PostGIS needs to be enabled explicitly
    per database - it is not on by default even when the PostGIS
    extension package itself is installed on the server.
    """
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT PostGIS_Version();"))
        except Exception as e:
            raise EnvironmentError(
                "PostGIS extension is not enabled in the target database. "
                "Connect to it (e.g. via psql or pgAdmin) as a database "
                "owner/superuser and run once:\n\n"
                "    CREATE EXTENSION IF NOT EXISTS postgis;\n\n"
                f"Original error: {e}"
            ) from e


# ---------------------------------------------------------------------
# QC gate
# ---------------------------------------------------------------------

def check_qc_gate(cfg) -> tuple[bool, str]:
    """
    Reads this country's qc_report.json (produced by run_pipeline.py
    --check --report) and refuses export if a CRS/pixel plausibility
    problem is present, or if QC was never executed at all.

    Returns (passed, reason). This is deliberately conservative: any
    inability to confirm the check passed results in a refusal, not a
    silent pass.

    Reads utils/qc.py's dedicated "raster_crs_plausible" field
    (True/False/None) directly - confirmed present in qc_report.json via
    reporting.py's build_qc_report(). An earlier version of this function
    instead searched a "qc_errors" field for a "crs"/"pixel" substring,
    assuming it was a list of error messages; it is actually an integer
    error COUNT (qc.py sums check-level error counts into one number),
    so that substring search never matched anything - it was
    unreachable whenever qc_status == "ERROR" (handled above already)
    and a harmless no-op otherwise. This direct field read replaces that
    dead logic with something that actually inspects plausibility.
    """
    qc_path = cfg.reports_dir / "qc_report.json"
    if not qc_path.exists():
        return False, (
            f"{qc_path} not found. Run: python scripts/run_pipeline.py "
            f"--country {cfg.country_code.lower()} --check --report"
        )

    with qc_path.open(encoding="utf-8") as f:
        qc_report = json.load(f)

    if not qc_report.get("qc_executed"):
        return False, "QC was not executed for this run (qc_executed=false)."

    if qc_report.get("qc_status") == "ERROR":
        return False, f"QC status is ERROR ({qc_report.get('qc_errors')} error(s))."

    if qc_report.get("raster_crs_plausible") is False:
        return False, (
            "CRS/pixel plausibility check failed - the declared raster CRS "
            "is inconsistent with its actual pixel coordinates."
        )

    return True, "QC gate passed."


# ---------------------------------------------------------------------
# Idempotent per-country write helper
# ---------------------------------------------------------------------

def _table_exists(engine: Engine, table: str, schema: str) -> bool:
    return inspect(engine).has_table(table, schema=schema)


def _replace_country_rows(
    engine: Engine,
    table: str,
    schema: str,
    country_code: str,
    data,
    geometry: bool = False,
):
    """
    Deletes any existing rows for this country_code in `table` (if the
    table exists yet), then inserts `data` fresh - inside one transaction
    per call, so a failed insert does not leave the table half-deleted.
    """
    with engine.begin() as conn:
        if _table_exists(engine, table, schema):
            conn.execute(
                text(f'DELETE FROM "{schema}"."{table}" WHERE country_code = :cc'),
                {"cc": country_code},
            )
        if geometry:
            data.to_postgis(table, conn, schema=schema, if_exists="append", index=False)
        else:
            data.to_sql(table, conn, schema=schema, if_exists="append", index=False)


# ---------------------------------------------------------------------
# Config/database synchronization (removes stale countries)
# ---------------------------------------------------------------------

MANAGED_TABLES = [
    "ghi_statistics",
    "optimal_zones_area",
    "optimal_zones",
    "admin_centroids",
    "artifact_catalog",
    "processing_runs",
]


def sync_database_to_configs(engine: Engine, current_country_codes: list[str], schema: str | None = None) -> dict:
    """
    Removes rows for any country_code present in the database but no
    longer present in current_country_codes (i.e. its config/<country>.json
    no longer exists).

    Necessary because export_country()'s idempotency is scoped to the
    country it is called for (delete-then-insert on that one
    country_code) - it never touches rows for a country that simply
    isn't passed in anymore. Unlike 04_global_report.py, which rebuilds
    its output entirely from the current config/*.json set each run (so
    a removed country's config silently drops it from the report), the
    database is additive across separate export_country() calls and has
    no equivalent "rebuild from scratch" step - without this function,
    a country removed from config/ stays in the database indefinitely,
    e.g. after a temporary extensibility test with extra countries that
    are later removed.

    Call this explicitly (e.g. from 05_export_database.py --all) after
    exporting every currently-configured country - never automatically
    on every single-country export, since --country <x> is a legitimate
    way to update just one country without implying every other
    configured (or previously configured) country should be touched.
    """
    schema = schema or os.getenv("DB_SCHEMA", "public")
    removed = {}

    with engine.begin() as conn:
        for table in MANAGED_TABLES:
            if not _table_exists(engine, table, schema):
                continue

            existing_codes = {
                row[0] for row in conn.execute(
                    text(f'SELECT DISTINCT country_code FROM "{schema}"."{table}"')
                )
            }
            stale_codes = existing_codes - set(current_country_codes)

            if stale_codes:
                conn.execute(
                    text(f'DELETE FROM "{schema}"."{table}" WHERE country_code = ANY(:codes)'),
                    {"codes": list(stale_codes)},
                )
                removed[table] = sorted(stale_codes)

    if removed:
        print(f"Database sync: removed stale country rows -> {removed}")
    else:
        print("Database sync: no stale countries found - database already matches config/*.json.")

    return removed


# ---------------------------------------------------------------------
# Per-artifact loaders (read only - never re-run processing)
# ---------------------------------------------------------------------

def _load_ghi_statistics(cfg) -> pd.DataFrame | None:
    path = cfg.tables_dir / "ghi_statistics.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df.insert(0, "country_code", cfg.country_code)
    df.insert(1, "country", cfg.country)
    return df


def _load_optimal_zones_area(cfg) -> pd.DataFrame | None:
    path = cfg.tables_dir / "optimal_solar_zones_area.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path)
    df.insert(0, "country_code", cfg.country_code)
    df.insert(1, "country", cfg.country)
    return df


# Canonical storage CRS for every geometry table in the database. Each
# country's own outputs/<code>/maps/*.gpkg are in that country's local
# UTM/projected CRS (cfg.crs_target - e.g. EPSG:32618 for Haïti,
# EPSG:32637 for Kenya), which differs country to country. A shared
# PostGIS table has a single fixed SRID per geometry column, so every
# geometry is reprojected to this common CRS before being written -
# otherwise the second country written after the first (which fixes the
# table's SRID on creation) fails with a CRS mismatch. WGS84 (EPSG:4326)
# is the standard choice for a canonical, country-agnostic storage CRS;
# use ST_Transform() in SQL to reproject back to a local CRS for any
# analysis that needs it (e.g. accurate area/distance in meters).
DB_STORAGE_CRS = "EPSG:4326"


def _load_optimal_zones_geom(cfg) -> gpd.GeoDataFrame | None:
    path = cfg.maps_dir / "optimal_zones.gpkg"
    if not path.exists():
        return None
    gdf = gpd.read_file(path, engine="pyogrio")
    gdf = gdf.to_crs(DB_STORAGE_CRS)
    gdf.insert(0, "country_code", cfg.country_code)
    return gdf


def _load_admin_centroids(cfg) -> gpd.GeoDataFrame | None:
    """
    admin_centroids.gpkg retains every raw attribute column from each
    country's source administrative file (03_optimal_zones.py copies the
    whole admin GeoDataFrame through). Those raw schemas differ by
    provider - GADM-sourced countries carry columns like GID_2/HASC_2/
    ENGTYPE_2, while Haïti's OCHA/CNIGS source does not - so a shared
    cross-country table cannot use them directly: whichever country is
    exported first fixes the table's columns, and any other country
    whose source file has different raw columns then fails on insert
    with "column ... does not exist".

    This keeps only the toolkit-computed, country-agnostic columns for
    the shared table - admin_name (via cfg.admin_name_field, the same
    abstraction the rest of the pipeline already uses instead of a
    hardcoded field name) and zone_area_km2 - rather than the raw
    per-provider attributes.
    """
    path = cfg.maps_dir / "admin_centroids.gpkg"
    if not path.exists():
        return None
    gdf = gpd.read_file(path, engine="pyogrio")

    keep_cols = {cfg.admin_name_field: "admin_name", "_zone_area_km2": "zone_area_km2"}
    missing = [c for c in keep_cols if c not in gdf.columns]
    if missing:
        raise KeyError(
            f"admin_centroids.gpkg for {cfg.country_code} is missing expected "
            f"column(s) {missing} - check cfg.admin_name_field and the "
            f"03_optimal_zones.py output schema."
        )

    gdf = gdf[list(keep_cols) + ["geometry"]].rename(columns=keep_cols)
    gdf = gdf.to_crs(DB_STORAGE_CRS)
    gdf.insert(0, "country_code", cfg.country_code)
    return gdf


def _build_artifact_catalog(cfg) -> pd.DataFrame:
    """
    Catalogs file paths for the raster and the map/report artifacts,
    rather than embedding them as bytea/PostGIS Raster in v1. Keeps the
    database as the queryable index; large binary artifacts stay on the
    filesystem. See the README's Database export section for the
    rationale and how to extend this to PostGIS Raster later.
    """
    clipped_raster = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
    candidates = {
        "clipped_raster": clipped_raster,
        "ghi_distribution_map": cfg.maps_dir / "Final_GHI_Distribution_Map.png",
        "optimal_zones_map": cfg.maps_dir / "Optimal_Solar_Zones.png",
        "final_report_html": cfg.reports_dir / "final_report.html",
    }
    rows = [
        {"country_code": cfg.country_code, "artifact_type": name, "path": str(path.resolve())}
        for name, path in candidates.items()
        if path.exists()
    ]
    return pd.DataFrame(rows)


def _build_run_metadata(cfg, qc_status: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "country_code": cfg.country_code,
        "country": cfg.country,
        "crs_target": cfg.crs_target,
        "ghi_threshold_kwh_m2_day": cfg.ghi_threshold_kwh_m2_day,
        "qc_status": qc_status,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }])


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def export_country(cfg, schema: str | None = None) -> dict:
    """
    Exports one country's already-produced artifacts to PostgreSQL/PostGIS,
    refusing to proceed if the QC gate fails. Deletes and re-inserts this
    country's rows in every table (see module docstring) - safe to call
    repeatedly during development.
    """
    schema = schema or os.getenv("DB_SCHEMA", "public")

    passed, reason = check_qc_gate(cfg)
    if not passed:
        print(f"[{cfg.country_code}] Database export SKIPPED: {reason}")
        return {"exported": False, "reason": reason}

    engine = get_engine()
    _ensure_postgis(engine)

    tables_written = []

    ghi_stats = _load_ghi_statistics(cfg)
    if ghi_stats is not None:
        _replace_country_rows(engine, "ghi_statistics", schema, cfg.country_code, ghi_stats)
        tables_written.append("ghi_statistics")

    zone_area = _load_optimal_zones_area(cfg)
    if zone_area is not None:
        _replace_country_rows(engine, "optimal_zones_area", schema, cfg.country_code, zone_area)
        tables_written.append("optimal_zones_area")

    zones_geom = _load_optimal_zones_geom(cfg)
    if zones_geom is not None and not zones_geom.empty:
        _replace_country_rows(engine, "optimal_zones", schema, cfg.country_code, zones_geom, geometry=True)
        tables_written.append("optimal_zones")

    centroids = _load_admin_centroids(cfg)
    if centroids is not None and not centroids.empty:
        _replace_country_rows(engine, "admin_centroids", schema, cfg.country_code, centroids, geometry=True)
        tables_written.append("admin_centroids")

    catalog = _build_artifact_catalog(cfg)
    if not catalog.empty:
        _replace_country_rows(engine, "artifact_catalog", schema, cfg.country_code, catalog)
        tables_written.append("artifact_catalog")

    # Read the actual qc_status (PASS/WARNING/ERROR) from qc_report.json,
    # rather than reusing check_qc_gate()'s human-readable `reason` string
    # ("QC gate passed.") - the gate's reason describes whether export was
    # ALLOWED, not the underlying QC status itself, and collapsing both
    # into one field would make every successfully-exported country show
    # the same generic message here regardless of whether its QC run was
    # a clean PASS or a WARNING - losing exactly the distinction
    # qc_report.json preserves.
    qc_path = cfg.reports_dir / "qc_report.json"
    with qc_path.open(encoding="utf-8") as f:
        qc_status = json.load(f).get("qc_status", "UNKNOWN")

    run_meta = _build_run_metadata(cfg, qc_status=qc_status)
    _replace_country_rows(engine, "processing_runs", schema, cfg.country_code, run_meta)
    tables_written.append("processing_runs")

    print(f"[{cfg.country_code}] Database export complete -> tables: {tables_written}")
    return {"exported": True, "tables": tables_written}