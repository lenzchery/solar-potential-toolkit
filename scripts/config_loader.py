"""
config_loader.py
==================
Loads and validates a per-country JSON configuration file (see config/*.json).

Paths follow the same layout as the original single-country scripts:
    data/raster/<raster_filename>
    data/vectors/<admin_vector_filename>
    outputs/<country_code>/maps/
    outputs/<country_code>/tables/
    outputs/<country_code>/reports/

Usage:
    from config_loader import load_country_config
    cfg = load_country_config("reunion")   # looks up config/reunion.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_RASTER_DIR = PROJECT_ROOT / "data" / "raster"
DATA_VECTORS_DIR = PROJECT_ROOT / "data" / "vectors"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

REQUIRED_KEYS = [
    "country", "country_code", "crs_target",
    "raster_filename", "admin_vector_filename", "admin_name_field",
    "ghi_threshold_kwh_m2_day", "plot",
]
REQUIRED_PLOT_KEYS = ["tick_interval_m", "figsize_in", "vmin", "vmax"]

# Tokens used across config/*.json to mark a value that is intentionally
# not yet finalized (e.g. a vector layer's provenance details not yet
# confirmed for a given country). Reporting uses this list to surface
# incomplete metadata explicitly rather than silently including a
# placeholder string in a report.
PLACEHOLDER_TOKENS = {"TO_DEFINE", "TO_VERIFY"}


@dataclass
class CountryConfig:
    country: str
    country_code: str
    crs_target: str
    raster_path: Path
    admin_vector_path: Path
    admin_name_field: str
    ghi_threshold_kwh_m2_day: float
    tick_interval_m: float
    figsize_in: tuple
    vmin: float
    vmax: float
    maps_dir: Path
    tables_dir: Path
    reports_dir: Path
    administrative_unit: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)  # untouched parsed JSON, in case a script needs an extra field


def list_available_countries() -> list[str]:
    """Return the country keys (filenames without .json) found in config/."""
    return sorted(p.stem for p in CONFIG_DIR.glob("*.json"))


def load_country_config(country: str) -> CountryConfig:
    """
    Load config/<country>.json, validate it, and return a CountryConfig
    with every path resolved relative to the project root - so scripts
    behave the same whether run from the project root or from scripts/.
    """
    config_path = CONFIG_DIR / f"{country}.json"
    if not config_path.exists():
        available = ", ".join(list_available_countries()) or "(none found)"
        raise FileNotFoundError(
            f"No config file for '{country}' at {config_path}. "
            f"Available countries: {available}"
        )

    with config_path.open(encoding="utf-8") as f:
        data = json.load(f)

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise ValueError(f"{config_path} is missing required key(s): {missing}")

    missing_plot = [k for k in REQUIRED_PLOT_KEYS if k not in data["plot"]]
    if missing_plot:
        raise ValueError(f"{config_path}['plot'] is missing key(s): {missing_plot}")

    country_code = data["country_code"]
    output_root = OUTPUTS_DIR / country_code.lower()

    return CountryConfig(
        country=data["country"],
        country_code=country_code,
        crs_target=data["crs_target"],
        raster_path=DATA_RASTER_DIR / data["raster_filename"],
        admin_vector_path=DATA_VECTORS_DIR / data["admin_vector_filename"],
        admin_name_field=data["admin_name_field"],
        ghi_threshold_kwh_m2_day=float(data["ghi_threshold_kwh_m2_day"]),
        tick_interval_m=float(data["plot"]["tick_interval_m"]),
        figsize_in=tuple(data["plot"]["figsize_in"]),
        vmin=float(data["plot"]["vmin"]),
        vmax=float(data["plot"]["vmax"]),
        maps_dir=output_root / "maps",
        tables_dir=output_root / "tables",
        reports_dir=output_root / "reports",
        administrative_unit=data.get("administrative_unit", {}),
        provenance=data.get("provenance", {}),
        raw=data,
    )


def find_placeholder_fields(cfg: CountryConfig) -> list[str]:
    """
    Recursively scan cfg.administrative_unit and cfg.provenance for any
    string value equal to a known placeholder token (see PLACEHOLDER_TOKENS).

    Returns a list of dotted paths (e.g. "provenance.vector.source_url")
    pointing to fields that are not yet finalized. Used by reporting.py to
    surface incomplete metadata explicitly in qc_report.json / final_report.html,
    rather than silently embedding "TO_DEFINE" in a report as if it were a
    real value.
    """
    warnings: list[str] = []

    def _walk(node, path_prefix: str):
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path_prefix}.{key}" if path_prefix else key)
        elif isinstance(node, str) and node in PLACEHOLDER_TOKENS:
            warnings.append(path_prefix)

    _walk({"administrative_unit": cfg.administrative_unit, "provenance": cfg.provenance}, "")
    return warnings


if __name__ == "__main__":
    for country in list_available_countries():
        cfg = load_country_config(country)
        placeholders = find_placeholder_fields(cfg)
        flag = f"  [!] {len(placeholders)} placeholder field(s)" if placeholders else ""
        print(f"{cfg.country_code:>4}  {cfg.country:<15}  target CRS: {cfg.crs_target:<12}  "
              f"raster: {cfg.raster_path.name:<20}  admin field: {cfg.admin_name_field}{flag}")
