-- =============================================================================
-- verify_database.sql - solar-potential-toolkit
-- =============================================================================
-- Consolidated verification queries for the PostgreSQL/PostGIS export
-- (scripts/05_export_database.py). Run in pgAdmin's Query Tool, connected
-- to the "solar_potential_toolkit" database, schema "solar".
--
-- Column names below are the actual ones produced by the export pipeline
-- (utils/database.py) - confirmed once, ahead of time, via `\d solar.<table>`
-- in a direct psql session (PowerShell). This preparatory step is separate
-- from running this file itself: every query below is plain SQL, with no
-- psql-specific meta-commands, runnable in pgAdmin's Query Tool or any
-- other PostgreSQL client.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Table inventory
-- -----------------------------------------------------------------------------
-- Note: this replaces psql's \dt meta-command (a psql client feature, not
-- SQL), which does not run inside pgAdmin's Query Tool or any other
-- SQL-only client.
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'solar'
ORDER BY table_name;


-- -----------------------------------------------------------------------------
-- 2. GHI statistics per country
-- -----------------------------------------------------------------------------
SELECT country_code, country,
       ghi_min_kwh_m2_day, ghi_mean_kwh_m2_day, ghi_max_kwh_m2_day
FROM solar.ghi_statistics
ORDER BY country_code;


-- -----------------------------------------------------------------------------
-- 3. Optimal-zone area and coverage percentage per country
-- -----------------------------------------------------------------------------
SELECT country_code, country,
       area_km2, total_area_km2, percent_country, ghi_threshold_kwh_m2_day
FROM solar.optimal_zones_area
ORDER BY country_code;


-- -----------------------------------------------------------------------------
-- 4. Geometry counts per country, both geometry tables
-- -----------------------------------------------------------------------------
SELECT country_code, COUNT(*) AS optimal_zone_polygons
FROM solar.optimal_zones
GROUP BY country_code
ORDER BY country_code;

SELECT country_code, COUNT(*) AS admin_units_with_optimal_coverage
FROM solar.admin_centroids
GROUP BY country_code
ORDER BY country_code;


-- -----------------------------------------------------------------------------
-- 5. SRID consistency check (should return a single row: 4326)
--    Confirms every country's geometries share one canonical storage CRS,
--    per the README's "Storage CRS" note.
-- -----------------------------------------------------------------------------
SELECT DISTINCT ST_SRID(geometry) AS srid FROM solar.optimal_zones
UNION
SELECT DISTINCT ST_SRID(geometry) AS srid FROM solar.admin_centroids;


-- -----------------------------------------------------------------------------
-- 6. QC gate confirmation - every exported country must show a passed gate
-- -----------------------------------------------------------------------------
SELECT country_code, country, crs_target, qc_status, exported_at
FROM solar.processing_runs
ORDER BY country_code;


-- -----------------------------------------------------------------------------
-- 7. Artifact catalog spot check
-- -----------------------------------------------------------------------------
SELECT country_code, artifact_type, path
FROM solar.artifact_catalog
ORDER BY country_code, artifact_type;


-- -----------------------------------------------------------------------------
-- 8. Summary view - one query bringing GHI stats, zone coverage, and
--    geometry counts together per country. Created once, then reused for
--    any future check (and for the README screenshot - see query 9).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW solar.country_summary AS
SELECT
    g.country_code,
    g.country,
    r.crs_target,
    ROUND(g.ghi_mean_kwh_m2_day::numeric, 2)   AS ghi_mean,
    ROUND(a.area_km2::numeric, 1)              AS optimal_area_km2,
    ROUND(a.percent_country::numeric, 2)       AS optimal_pct,
    (SELECT COUNT(*) FROM solar.optimal_zones z WHERE z.country_code = g.country_code)
        AS optimal_zone_polygons,
    (SELECT COUNT(*) FROM solar.admin_centroids c WHERE c.country_code = g.country_code)
        AS admin_units_covered,
    r.qc_status
FROM solar.ghi_statistics g
JOIN solar.optimal_zones_area a USING (country_code)
JOIN solar.processing_runs r USING (country_code);


-- -----------------------------------------------------------------------------
-- 9. The screenshot query - run this one for the README capture (see
--    accompanying instructions).
-- -----------------------------------------------------------------------------
SELECT * FROM solar.country_summary ORDER BY country_code;


-- -----------------------------------------------------------------------------
-- 10. Idempotency check - formalizes the manual test performed on
--     2026-08-20 (re-running scripts/05_export_database.py --all, both
--     after dropping every solar.* table and without dropping anything
--     first) - both runs produced identical row counts with zero
--     duplicates. Each country must have EXACTLY ONE row in every
--     non-geometry table below; more than one means the per-country
--     delete-before-insert in utils/database.py's
--     _replace_country_rows() failed to prevent accumulation across
--     reruns. Expect ZERO rows returned from all three queries below -
--     any row returned identifies a country_code with duplicates.
-- -----------------------------------------------------------------------------
SELECT country_code, COUNT(*) FROM solar.ghi_statistics
GROUP BY country_code HAVING COUNT(*) <> 1;

SELECT country_code, COUNT(*) FROM solar.optimal_zones_area
GROUP BY country_code HAVING COUNT(*) <> 1;

SELECT country_code, COUNT(*) FROM solar.processing_runs
GROUP BY country_code HAVING COUNT(*) <> 1;