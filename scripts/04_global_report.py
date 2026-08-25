"""
04_global_report.py
=====================
Generates the multi-country comparative report (outputs/global/) from
every country that has already been processed at least once with
--check --report. Does not re-run any per-country processing - purely
reads already-produced qc_report.json / ghi_statistics.csv / clipped
rasters, exactly like utils/reporting.py does for a single country.

Usage:
    python scripts/04_global_report.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config_loader import list_available_countries, load_country_config
from utils.reporting import save_global_report


def main():
    countries = list_available_countries()
    configs = [load_country_config(c) for c in countries]

    save_global_report(configs)


if __name__ == "__main__":
    main()