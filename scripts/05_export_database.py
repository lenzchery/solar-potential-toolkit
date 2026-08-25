"""
05_export_database.py
=======================
Optional PostgreSQL/PostGIS export step for the Solar Potential Toolkit.

Prerequisite:
    Run the pipeline with --check --report first (this produces the
    qc_report.json this script gates on, plus the tables/GeoPackages it
    reads):
        python scripts/run_pipeline.py --country haiti --check --report

This script never re-runs processing - it only reads already-produced
artifacts and writes them to the database. See utils/database.py for the
QC gate and the per-country overwrite behavior.

With --all, also removes database rows for any country no longer present
in config/*.json (see utils/database.py's sync_database_to_configs) -
otherwise a country temporarily added for an extensibility test, then
later removed, would leave stale rows in the database indefinitely,
since export_country()'s delete-then-insert is scoped only to the
countries actually passed to it.

Usage:
    python scripts/05_export_database.py --country haiti
    python scripts/05_export_database.py --all
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config_loader import load_country_config, list_available_countries
from utils.database import export_country, get_engine, sync_database_to_configs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--country",
        help=f"One of: {', '.join(list_available_countries())}",
    )
    group.add_argument(
        "--all", action="store_true",
        help="Export every configured country (skipping any that fails the QC gate), "
             "then remove database rows for any country no longer in config/*.json.",
    )
    args = parser.parse_args()

    countries = list_available_countries() if args.all else [args.country]

    summary = {}
    for country in countries:
        cfg = load_country_config(country)
        result = export_country(cfg)
        summary[cfg.country_code] = "OK" if result["exported"] else "SKIPPED"

    if args.all:
        engine = get_engine()
        sync_database_to_configs(engine, current_country_codes=list(summary.keys()))

    print("\n" + "=" * 60)
    print("Database export summary")
    print("=" * 60)
    for code, status in summary.items():
        print(f"  {code:<5} {status}")


if __name__ == "__main__":
    main()