"""
run_pipeline.py
================
Orchestrates the full pipeline for one country - or all configured
countries at once - by running, in order:

    01_reproject_clip.py     -> reproject (if needed) + clip the raw raster
    02_ghi_distribution.py   -> descriptive stats + full GHI distribution map
    03_optimal_zones.py      -> threshold, vectorize, intersect, map

Optionally runs a QC pass (qc.run_quality_control) before the pipeline,
via --check. QC status ERROR aborts that country's run; WARNING or PASS
lets it proceed.

Optionally enforces a strict CRS/pixel plausibility gate via --strict-crs
(requires --check): aborts the country's run if the declared raster CRS
is inconsistent with its actual pixel coordinates. Does NOT abort on
"reprojection required" alone - that is expected, not an anomaly.

Optionally generates qc_report.json, processing_report.json, and
final_report.html for each successfully-completed country, via --report.
Report generation reads the CSV/PNG artifacts already produced by 02/03 -
it does not re-run any processing, so it works whether or not --check
was also used (the QC section of the report simply notes "NOT_RUN" if
--check was omitted).

Each step is invoked as a subprocess (rather than imported and called
directly) so that a failure in one country's run prints a clear error
and - with --keep-going - doesn't stop the others from running.

Usage:
    # Run the full pipeline for a single country
    python scripts/run_pipeline.py --country reunion

    # Run QC first, abort on ERROR, proceed on WARNING/PASS
    python scripts/run_pipeline.py --country haiti --check

    # Run QC with strict CRS/pixel plausibility enforcement
    python scripts/run_pipeline.py --country haiti --check --strict-crs

    # Run the pipeline and generate reports afterwards
    python scripts/run_pipeline.py --country haiti --check --report

    # Full combo
    python scripts/run_pipeline.py --country haiti --check --strict-crs --report

    # Run it for every country that has a config/*.json file
    python scripts/run_pipeline.py --all --check --report

    # Run for all countries, but don't stop on the first failure
    python scripts/run_pipeline.py --all --keep-going

    # Override the GHI threshold for the whole run (passed through to step 3)
    python scripts/run_pipeline.py --country reunion --threshold 4.5
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# --- Ensure project root is on sys.path so `utils.qc`, `utils.reporting`,
#     and `config_loader` resolve regardless of the working directory ---
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from config_loader import list_available_countries, load_country_config, find_placeholder_fields
from utils.qc import run_quality_control, verify_clipped_raster_area
from utils.reporting import generate_country_report

SRC_DIR = SCRIPTS_DIR

PIPELINE_STEPS = [
    "01_reproject_clip.py",
    "02_ghi_distribution.py",
    "03_optimal_zones.py",
]


def run_qc_for_country(country: str) -> tuple[str, dict]:
    """
    Run QC for one country. Returns (status, full_qc_report_dict).

    Passes the country's GHI threshold (threshold-plausibility check) and,
    if it already exists on disk, the country boundary produced by
    01_reproject_clip.py (boundary/raster area-consistency check). On a
    first-ever --check run for a country, the boundary won't exist yet -
    that specific check is then skipped (INFO), not failed.
    """
    cfg = load_country_config(country)

    boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
    boundary_path = boundary_path if boundary_path.exists() else None

    print(f"\n--- QC: {country} ---")
    qc_report = run_quality_control(
        raster_path=cfg.raster_path,
        vector_path=cfg.admin_vector_path,
        target_crs=cfg.crs_target,
        required_field=cfg.admin_name_field,
        ghi_threshold=cfg.ghi_threshold_kwh_m2_day,
        boundary_path=boundary_path,
    )
    return qc_report["status"], qc_report


def run_pipeline_for_country(
    country: str, threshold: float | None, check: bool, report: bool, strict_crs: bool = False
) -> bool:
    """Run QC (optional) then all three steps for one country, then reports (optional).
    Returns True if all steps succeeded."""
    print(f"\n{'=' * 60}\nRunning pipeline for: {country}\n{'=' * 60}")

    cfg = load_country_config(country)
    qc_report = None
    post_clip_check = None

    if check:
        status, qc_report = run_qc_for_country(country)

        if status == "ERROR":
            print(f"[{country}] QC FAILED (status=ERROR) - pipeline aborted for this country.")
            return False

        if strict_crs:
            plausible = qc_report.get("raster_crs_plausible")
            if plausible is False:
                print(f"[{country}] [STRICT-CRS] CRS/pixel plausibility check FAILED - "
                      f"pipeline aborted (declared CRS inconsistent with actual pixel coordinates).")
                return False
            elif plausible is None:
                print(f"[{country}] [STRICT-CRS] Plausibility check could not run "
                      f"(no area_of_use metadata) - continuing, but verify manually.")
            else:
                print(f"[{country}] [STRICT-CRS] CRS/pixel plausibility check passed.")

        if status == "WARNING":
            print(f"[{country}] QC passed with warnings - continuing pipeline.")
        else:
            print(f"[{country}] QC passed - continuing pipeline.")

    # Runs 01 -> 02 -> 03 exactly once each. (A previous version of this
    # function accidentally contained this loop twice in a row, silently
    # discarding failures from the first pass and then re-running every
    # step a second time regardless - each country's pipeline was
    # executing twice per invocation. Fixed: single pass, failures handled
    # and reported immediately, post_clip_check computed once after step 01.)
    for step in PIPELINE_STEPS:
        script_path = SRC_DIR / step
        cmd = [sys.executable, str(script_path), "--country", country]
        if step == "03_optimal_zones.py" and threshold is not None:
            cmd += ["--threshold", str(threshold)]

        print(f"\n--- {' '.join(cmd[1:])} ---")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"[{country}] FAILED at {step} (exit code {result.returncode})")
            return False

        if step == "01_reproject_clip.py":
            clipped_path = cfg.maps_dir / f"ghi_{cfg.country_code.lower()}_clip.tif"
            boundary_path = cfg.maps_dir / f"{cfg.country_code.lower()}_boundary.gpkg"
            if clipped_path.exists() and boundary_path.exists():
                post_clip_check = verify_clipped_raster_area(clipped_path, boundary_path)
                status_word = "WARNING" if post_clip_check["warnings"] > 0 else "PASS"
                print(f"[{country}] Post-clip boundary/raster area check: {status_word}")

    print(f"\n[{country}] pipeline completed successfully.")

    if report:
        metadata_warnings = find_placeholder_fields(cfg)
        generate_country_report(cfg, qc_result=qc_report, metadata_warnings=metadata_warnings, post_clip_check=post_clip_check)

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--country",
        help=f"Run the pipeline for a single country. One of: {', '.join(list_available_countries())}",
    )
    group.add_argument(
        "--all", action="store_true",
        help="Run the pipeline for every country with a config/*.json file.",
    )
    parser.add_argument(
        "--keep-going", action="store_true",
        help="With --all: continue with the next country even if one fails "
             "(default: stop at the first failure).",
    )
    parser.add_argument(
        "--threshold", type=float, default=None,
        help="Override the GHI threshold (kWh/m²/day) used in step 3 for every "
             "country run this way (default: each country's own config value).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Run QC before the pipeline for each country. Aborts that country's "
             "run if QC status is ERROR; proceeds on WARNING or PASS.",
    )
    parser.add_argument(
        "--strict-crs", action="store_true",
        help="Abort the pipeline for a country if the CRS/pixel plausibility "
             "check explicitly fails (declared CRS inconsistent with actual "
             "pixel coordinates). Requires --check. Does NOT abort on "
             "'reprojection required' alone - that is expected, not an anomaly.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate qc_report.json, processing_report.json, and final_report.html "
             "for each successfully-completed country, in outputs/<code>/reports/. "
             "Reads already-produced CSV/PNG artifacts - works with or without --check "
             "(the QC section of the report notes 'NOT_RUN' if --check was omitted).",
    )
    args = parser.parse_args()

    if args.strict_crs and not args.check:
        parser.error("--strict-crs requires --check (it evaluates the QC report).")

    countries = list_available_countries() if args.all else [args.country]
    if not countries:
        print("No countries found in config/. Nothing to run.")
        sys.exit(1)

    results = {}
    for country in countries:
        success = run_pipeline_for_country(country, args.threshold, args.check, args.report, args.strict_crs)
        results[country] = success
        if not success and not args.keep_going:
            break

    print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
    for country, success in results.items():
        status = "OK" if success else "FAILED"
        print(f"  {country:<12} {status}")

    if not all(results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
