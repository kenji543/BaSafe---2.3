#!/usr/bin/env python3
"""Pull daily precipitation for Basey's own coordinates from Open-Meteo and
log every day with recorded rainfall into hazard_events, not just the heavy
ones -- the calendar this feeds is meant to show the complete rain picture
for Basey, with earthquakes remaining a curated, notable-only selection.
`--min-daily-mm` still lets an operator narrow this back down to notable/
heavy days only, if that's what a particular run needs.

Local/operator-run only, like every other sync script in this project --
never called by the deployed public application.

Why this exists alongside sync_rain_events.py (PAGASA): PAGASA's Weather
Advisory file is always a province-wide outlook ("Samar would have 100 to
200mm") -- it structurally cannot say what happened in Basey specifically,
only the wider province. This queries Open-Meteo's archive at Basey's own
coordinates (11.28, 125.07), so what gets logged is actually about Basey,
not the province it sits in. Confirmed in practice: for TD Wilma's landfall
day (2025-12-06), the province-wide PAGASA figure was 100-200mm, while
Open-Meteo's Basey-coordinate estimate for that day was ~11mm -- a real,
meaningful difference this tool exists to surface, not obscure.

**Real, disclosed limitation**: this is a reanalysis/forecast model estimate
(ERA5-based) snapped to the nearest ~9km model grid cell, not a physical
rain gauge reading in Basey. Every logged row says so. Keep
sync_rain_events.py's PAGASA advisories too, where available, for their
independent value: they name the actual storm/system and carry PAGASA's own
authority, which a model estimate cannot.

No API key required (Open-Meteo is free, keyless, non-commercial use):
https://open-meteo.com/en/docs/historical-weather-api
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT_DIR / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from import_hazard_events import ImportFailure, import_hazard_events  # noqa: E402

ARCHIVE_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_DB = ROOT_DIR / "data" / "admin-dev.db"
DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"
DEFAULT_LATITUDE = 11.28
DEFAULT_LONGITUDE = 125.07
DEFAULT_MIN_DAILY_MM = 0.1
DEFAULT_DAYS_BACK = 90


class SyncFailure(RuntimeError):
    """A fatal problem fetching or interpreting the Open-Meteo archive."""


def _fetch_daily_precipitation(
    *, start: date, end: date, latitude: float, longitude: float
) -> dict[str, Any]:
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "precipitation_sum",
        "timezone": "Asia/Manila",
    }
    url = f"{ARCHIVE_ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Basafe-HazardEventsSync/1.0 (local admin tool)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncFailure(f"Open-Meteo request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SyncFailure(f"Open-Meteo response was not valid JSON: {exc}") from exc


def _existing_raw_references(database_path: Path) -> set[str]:
    if not database_path.is_file():
        return set()
    connection = sqlite3.connect(database_path)
    try:
        try:
            rows = connection.execute(
                "SELECT raw_reference FROM hazard_events WHERE raw_reference IS NOT NULL"
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        return {row[0] for row in rows}
    finally:
        connection.close()


def sync(
    *,
    database_path: Path,
    schema_path: Path,
    days_back: int,
    min_daily_mm: float,
    latitude: float,
    longitude: float,
    data_classification: str,
) -> dict[str, Any]:
    # The archive API's reanalysis data lags a few days behind real time;
    # end a week back so the requested range is reliably available.
    end = date.today() - timedelta(days=7)
    start = end - timedelta(days=days_back)
    payload = _fetch_daily_precipitation(
        start=start, end=end, latitude=latitude, longitude=longitude
    )
    daily = payload.get("daily") or {}
    dates = daily.get("time") or []
    sums = daily.get("precipitation_sum") or []
    if len(dates) != len(sums):
        raise SyncFailure("Open-Meteo response had mismatched date/precipitation arrays.")
    actual_lat = payload.get("latitude", latitude)
    actual_lon = payload.get("longitude", longitude)

    already_logged = _existing_raw_references(database_path)
    rows: list[dict[str, str]] = []
    considered = 0
    above_threshold = 0
    already_logged_count = 0
    for day_str, value in zip(dates, sums):
        if value is None:
            continue
        considered += 1
        if value < min_daily_mm:
            continue
        above_threshold += 1
        raw_reference = f"{ARCHIVE_ENDPOINT}?latitude={latitude}&longitude={longitude}#day-{day_str}"
        if raw_reference in already_logged:
            already_logged_count += 1
            continue
        occurred_at = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
        rows.append(
            {
                "event_type": "rain",
                "occurred_at": occurred_at,
                "severity_value": f"{value:.1f}",
                "severity_unit": "mm",
                "source": "Open-Meteo Historical Weather (ERA5 reanalysis, Basey coordinates)",
                "raw_reference": raw_reference,
                "notes": (
                    f"Modeled daily precipitation total for {latitude},{longitude} "
                    f"(Basey), snapped to the nearest ~9km reanalysis grid cell "
                    f"(returned as {actual_lat},{actual_lon}). This is a reanalysis "
                    "model estimate, not a physical rain gauge reading in Basey. "
                    f"Basey recorded {value:.1f}mm that day, at or above this run's "
                    f"{min_daily_mm:g}mm/day inclusion threshold."
                ),
            }
        )

    result: dict[str, Any] = {
        "status": "success",
        "days_considered": considered,
        "days_above_threshold": above_threshold,
        "already_logged_count": already_logged_count,
        "new_events": len(rows),
    }
    if not rows:
        result["imported_count"] = 0
        return result
    with tempfile.TemporaryDirectory(prefix="basafe-openmeteo-rain-sync-") as temp_dir:
        csv_path = Path(temp_dir) / "openmeteo_rain_events.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        import_result = import_hazard_events(
            input_path=csv_path,
            database_path=database_path,
            schema_path=schema_path,
            source_date=end.isoformat(),
            data_classification=data_classification,
            strict=True,
            error_log=None,
        )
    result.update(import_result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", dest="database_path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", dest="schema_path", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK)
    parser.add_argument(
        "--min-daily-mm",
        type=float,
        default=DEFAULT_MIN_DAILY_MM,
        help=(
            "Log any day with a modeled total at or above this many mm "
            f"(default {DEFAULT_MIN_DAILY_MM:g}mm -- effectively every day Basey "
            "recorded measurable rain). Raise this to restrict the log to "
            "notable/heavy rain days only."
        ),
    )
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument(
        "--data-classification", choices=("official", "demonstration"), default="official"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = sync(
            database_path=args.database_path,
            schema_path=args.schema_path,
            days_back=args.days_back,
            min_daily_mm=args.min_daily_mm,
            latitude=args.latitude,
            longitude=args.longitude,
            data_classification=args.data_classification,
        )
    except (SyncFailure, ImportFailure) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
