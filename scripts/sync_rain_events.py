#!/usr/bin/env python3
"""Check PAGASA's live Weather Advisory file for a rainfall-outlook mention
of a target province and log it into hazard_events.

Local/operator-run only, like every other sync script in this project --
never called by the deployed public application, and never on the public
request path.

Design note / real limitation: PAGASA publishes each new Weather Advisory by
overwriting one fixed file (pubfiles.pagasa.dost.gov.ph/tamss/weather/
advisory.pdf) rather than an incrementing archive -- confirmed live: fetching
it returns whichever advisory is currently in effect, identified by its own
"WEATHER ADVISORY NO. <n>" heading. There is no historical index at this
URL. That means this script can only ever see "whichever advisory happens to
be live when it runs" -- if two advisories are issued between two runs, the
earlier one is silently unreachable, not just skipped. Running this
frequently during active weather narrows that gap; it cannot close it. This
is an inherent property of PAGASA's own file layout, not a bug in this
script. It never guesses at content it can't see, only what is downloaded.

Same PAGASA/PHIVOLCS PDF-parsing caveat as the earlier (superseded)
archive-based design: rainfall-outlook wording is free text and varies, so
extraction only recognizes a "<province mention> ... <N> to <M> mm" /
"up to <N> mm" phrasing. No match found is reported plainly, never
fabricated. Requires pdfplumber:

    uv run --extra hazard-events-prep python scripts/sync_rain_events.py
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT_DIR / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from import_hazard_events import ImportFailure, import_hazard_events  # noqa: E402

ADVISORY_URL = "https://pubfiles.pagasa.dost.gov.ph/tamss/weather/advisory.pdf"
ALLOWLISTED_HOST = "pubfiles.pagasa.dost.gov.ph"
ALLOWLISTED_PATH = "/tamss/weather/advisory.pdf"
DEFAULT_DB = ROOT_DIR / "data" / "admin-dev.db"
DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"
DEFAULT_TARGET_PROVINCE = "Samar"
CONTEXT_WINDOW_CHARS = 400

ADVISORY_NUMBER_PATTERN = re.compile(r"WEATHER ADVISORY NO\.\s*(\d+)", re.IGNORECASE)
ISSUED_AT_PATTERN = re.compile(
    r"Issued at:\s*([\d:]+\s*[AP]M),?\s*(\d{1,2}\s+\w+\s+\d{4})", re.IGNORECASE
)
FOR_HAZARD_PATTERN = re.compile(r"^For:\s*(.+)$", re.MULTILINE)
RANGE_PATTERN = re.compile(r"(\d+)\s*(?:to|-|–)\s*(\d+)\s*mm", re.IGNORECASE)
UP_TO_PATTERN = re.compile(r"up\s+to\s+(\d+)\s*mm", re.IGNORECASE)


class SyncFailure(RuntimeError):
    """A fatal problem fetching or interpreting the advisory."""


def _fetch_advisory_pdf() -> bytes:
    parsed = urllib.parse.urlsplit(ADVISORY_URL)
    if parsed.scheme != "https" or parsed.hostname != ALLOWLISTED_HOST or parsed.path != ALLOWLISTED_PATH:
        raise SyncFailure("Advisory URL is not the allowlisted PAGASA file.")
    request = urllib.request.Request(
        ADVISORY_URL, headers={"User-Agent": "Basafe-HazardEventsSync/1.0 (local admin tool)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read(20_000_001)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SyncFailure(f"PAGASA advisory request failed: {exc}") from exc


def _extract_text(pdf_bytes: bytes) -> str:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
        return "\n".join(page.extract_text() or "" for page in document.pages)


def _parse_issued_at(text: str) -> str | None:
    match = ISSUED_AT_PATTERN.search(text)
    if not match:
        return None
    time_part, date_part = match.group(1), match.group(2)
    try:
        parsed = datetime.strptime(f"{date_part} {time_part}", "%d %B %Y %I:%M %p")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _find_rainfall_mention(text: str, target_province: str) -> dict[str, Any] | None:
    target = target_province.casefold()
    for pattern, is_range in ((RANGE_PATTERN, True), (UP_TO_PATTERN, False)):
        for match in pattern.finditer(text):
            window_start = max(0, match.start() - CONTEXT_WINDOW_CHARS)
            context = text[window_start : match.end()]
            if target not in context.casefold():
                continue
            if is_range:
                low, high = int(match.group(1)), int(match.group(2))
                severity_value = (low + high) / 2
                range_note = f"reported range {low}-{high}mm, midpoint logged"
            else:
                value = int(match.group(1))
                severity_value = float(value)
                range_note = f"reported as 'up to {value}mm'"
            return {
                "severity_value": severity_value,
                "range_note": range_note,
                "context": " ".join(context.split())[-300:],
            }
    return None


def sync(
    *,
    database_path: Path,
    schema_path: Path,
    target_province: str,
    data_classification: str,
) -> dict[str, Any]:
    pdf_bytes = _fetch_advisory_pdf()
    text = _extract_text(pdf_bytes)
    number_match = ADVISORY_NUMBER_PATTERN.search(text)
    if not number_match:
        return {
            "status": "unrecognized_format",
            "message": "Could not find a 'WEATHER ADVISORY NO. <n>' heading in the fetched PDF.",
        }
    advisory_number = number_match.group(1)
    raw_reference = f"{ADVISORY_URL}#no{advisory_number}"

    connection = sqlite3.connect(database_path) if database_path.is_file() else None
    already_logged = False
    if connection is not None:
        try:
            row = connection.execute(
                "SELECT 1 FROM hazard_events WHERE raw_reference = ?", (raw_reference,)
            ).fetchone()
            already_logged = row is not None
        except sqlite3.OperationalError:
            already_logged = False
        finally:
            connection.close()
    if already_logged:
        return {
            "status": "already_logged",
            "advisory_number": advisory_number,
            "raw_reference": raw_reference,
        }

    hazard_match = FOR_HAZARD_PATTERN.search(text)
    hazard_for = hazard_match.group(1).strip() if hazard_match else "unspecified hazard"
    occurred_at = _parse_issued_at(text) or datetime.now(timezone.utc).isoformat()
    mention = _find_rainfall_mention(text, target_province)
    if mention is None:
        return {
            "status": "no_rainfall_mention",
            "advisory_number": advisory_number,
            "for": hazard_for,
            "message": (
                f"Advisory No. {advisory_number} (for {hazard_for}) does not mention "
                f"'{target_province}' near a rainfall figure. Not logged."
            ),
        }

    row = {
        "event_type": "rain",
        "occurred_at": occurred_at,
        "severity_value": f"{mention['severity_value']:.1f}",
        "severity_unit": "mm",
        "source": f"PAGASA Weather Advisory No. {advisory_number} (for {hazard_for})",
        "raw_reference": raw_reference,
        "notes": (
            f"{mention['range_note']} for a phrase mentioning '{target_province}'. "
            f"Matched context: “...{mention['context']}”. Fetched from PAGASA's "
            "live advisory file, which is overwritten per-advisory (no historical "
            "index at this URL) -- an advisory issued and superseded between two "
            "runs of this script would not be seen."
        ),
    }
    with tempfile.TemporaryDirectory(prefix="basafe-pagasa-rain-sync-") as temp_dir:
        csv_path = Path(temp_dir) / "pagasa_rain_event.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(row))
            writer.writeheader()
            writer.writerow(row)
        import_result = import_hazard_events(
            input_path=csv_path,
            database_path=database_path,
            schema_path=schema_path,
            source_date=date.today().isoformat(),
            data_classification=data_classification,
            strict=True,
            error_log=None,
        )
    return {"status": "success", "advisory_number": advisory_number, "for": hazard_for, **import_result}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", dest="database_path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", dest="schema_path", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--target-province", default=DEFAULT_TARGET_PROVINCE)
    parser.add_argument(
        "--data-classification", choices=("official", "demonstration"), default="official"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        print(
            "Error: pdfplumber is not installed. Run with: "
            "uv run --extra hazard-events-prep python scripts/sync_rain_events.py",
            file=sys.stderr,
        )
        return 1
    try:
        result = sync(
            database_path=args.database_path,
            schema_path=args.schema_path,
            target_province=args.target_province,
            data_classification=args.data_classification,
        )
    except (SyncFailure, ImportFailure) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
