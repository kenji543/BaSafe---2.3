#!/usr/bin/env python3
"""Pull recent regional earthquakes from the USGS Earthquake Catalog and log
them into the isolated admin dashboard's hazard_events table.

Local/operator-run only, like every other sync script in this project --
never called by the deployed public application. USGS is an independent
global seismological network, not PHIVOLCS; magnitude and location for a
given event can differ from PHIVOLCS's own (sometimes later-revised)
figures. That distinction is recorded in every logged row's source/notes,
never silently presented as a PHIVOLCS bulletin.

Deduplicates against already-logged rows by the USGS event page URL, so
re-running this on a schedule only adds genuinely new events.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT_DIR / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from import_hazard_events import ImportFailure, import_hazard_events  # noqa: E402

USGS_ENDPOINT = "https://earthquake.usgs.gov/fdsnws/event/1/query"
DEFAULT_DB = ROOT_DIR / "data" / "admin-dev.db"
DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"
# Centered on Basey, Samar; wide enough to cover the Eastern Visayas region
# (e.g. the Hernani/Llorente, Eastern Samar epicenters logged manually earlier).
DEFAULT_LATITUDE = 11.28
DEFAULT_LONGITUDE = 125.07
DEFAULT_RADIUS_KM = 150.0
DEFAULT_MIN_MAGNITUDE = 5.0


class SyncFailure(RuntimeError):
    """A fatal problem fetching or interpreting the USGS feed."""


def _fetch_usgs_events(
    *,
    start: datetime,
    end: datetime,
    min_magnitude: float,
    latitude: float,
    longitude: float,
    radius_km: float,
) -> list[dict[str, Any]]:
    params = {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "latitude": latitude,
        "longitude": longitude,
        "maxradiuskm": radius_km,
        "minmagnitude": min_magnitude,
        "orderby": "time",
    }
    url = f"{USGS_ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Basafe-HazardEventsSync/1.0 (local admin tool)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncFailure(f"USGS request failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SyncFailure(f"USGS response was not valid JSON: {exc}") from exc
    features = payload.get("features")
    if not isinstance(features, list):
        raise SyncFailure("USGS response did not contain a features list.")
    return features


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


def _to_rows(
    features: list[dict[str, Any]], already_logged: set[str]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for feature in features:
        properties = feature.get("properties") or {}
        event_url = properties.get("url") or ""
        if not event_url or event_url in already_logged:
            continue
        magnitude = properties.get("mag")
        occurred_ms = properties.get("time")
        if magnitude is None or occurred_ms is None:
            continue
        occurred_at = datetime.fromtimestamp(
            occurred_ms / 1000, tz=timezone.utc
        ).isoformat()
        place = properties.get("place") or "location not reported by USGS"
        rows.append(
            {
                "event_type": "earthquake",
                "occurred_at": occurred_at,
                "severity_value": str(magnitude),
                "severity_unit": "magnitude",
                "source": "USGS Earthquake Catalog (independent of PHIVOLCS)",
                "raw_reference": event_url,
                "notes": (
                    f"USGS location: {place}. USGS is a separate global seismological "
                    "network, not PHIVOLCS; magnitude/location may differ from any "
                    "official PHIVOLCS bulletin for the same event."
                ),
            }
        )
    return rows


def sync(
    *,
    database_path: Path,
    schema_path: Path,
    days_back: int,
    min_magnitude: float,
    latitude: float,
    longitude: float,
    radius_km: float,
    data_classification: str,
) -> dict[str, Any]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days_back)
    features = _fetch_usgs_events(
        start=start,
        end=end,
        min_magnitude=min_magnitude,
        latitude=latitude,
        longitude=longitude,
        radius_km=radius_km,
    )
    already_logged = _existing_raw_references(database_path)
    rows = _to_rows(features, already_logged)
    if not rows:
        return {
            "status": "no_new_events",
            "fetched": len(features),
            "already_logged_count": len(already_logged),
            "imported_count": 0,
        }
    with tempfile.TemporaryDirectory(prefix="basafe-usgs-sync-") as temp_dir:
        csv_path = Path(temp_dir) / "usgs_earthquakes.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "event_type",
                    "occurred_at",
                    "severity_value",
                    "severity_unit",
                    "source",
                    "raw_reference",
                    "notes",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
        result = import_hazard_events(
            input_path=csv_path,
            database_path=database_path,
            schema_path=schema_path,
            source_date=end.date().isoformat(),
            data_classification=data_classification,
            strict=True,
            error_log=None,
        )
    return {"status": "success", "fetched": len(features), "new_events": len(rows), **result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", dest="database_path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", dest="schema_path", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--days-back", type=int, default=90)
    parser.add_argument("--min-magnitude", type=float, default=DEFAULT_MIN_MAGNITUDE)
    parser.add_argument("--latitude", type=float, default=DEFAULT_LATITUDE)
    parser.add_argument("--longitude", type=float, default=DEFAULT_LONGITUDE)
    parser.add_argument("--radius-km", type=float, default=DEFAULT_RADIUS_KM)
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
            min_magnitude=args.min_magnitude,
            latitude=args.latitude,
            longitude=args.longitude,
            radius_km=args.radius_km,
            data_classification=args.data_classification,
        )
    except (SyncFailure, ImportFailure) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
