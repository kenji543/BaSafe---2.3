#!/usr/bin/env python3
"""Import a manually curated log of dated hazard events (rain, earthquake, etc.).

This feeds the isolated admin dashboard's hazard-event timeline only. It is
never automated and never called by the deployed public application: an
operator transcribes PAGASA/PHIVOLCS bulletins into a CSV by hand (or with
their own local tooling) and runs this command deliberately, the same way
sync_ulap_snapshot.py and import_dataset.py are run.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_SCHEMA = ROOT_DIR / "db" / "schema.sql"
DEFAULT_DB = ROOT_DIR / "data" / "admin-dev.db"
EVENT_TYPES = ("rain", "earthquake", "other")
REQUIRED_COLUMNS = {"event_type", "occurred_at", "severity_value", "severity_unit", "source"}


class ImportFailure(RuntimeError):
    """A fatal, non-row-specific import problem."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _parse_occurred_at(value: str) -> str:
    text = value.strip()
    try:
        return datetime.fromisoformat(text).isoformat()
    except ValueError:
        raise ValueError(
            f"unparseable occurred_at {value!r} (use YYYY-MM-DD or ISO 8601, "
            "optionally with time/fractional seconds/UTC offset)"
        ) from None


def _validate_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, row in enumerate(rows, start=2):
        try:
            event_type = (row.get("event_type") or "").strip().casefold()
            if event_type not in EVENT_TYPES:
                raise ValueError(
                    f"event_type must be one of {EVENT_TYPES}, got {row.get('event_type')!r}"
                )
            occurred_at = _parse_occurred_at(row.get("occurred_at") or "")
            severity_raw = (row.get("severity_value") or "").strip()
            try:
                severity_value = float(severity_raw)
            except ValueError as exc:
                raise ValueError(
                    f"severity_value must be numeric, got {severity_raw!r}"
                ) from exc
            severity_unit = (row.get("severity_unit") or "").strip()
            if not severity_unit:
                raise ValueError("severity_unit is required")
            source = (row.get("source") or "").strip()
            if not source:
                raise ValueError("source is required")
            valid.append(
                {
                    "event_type": event_type,
                    "occurred_at": occurred_at,
                    "severity_value": severity_value,
                    "severity_unit": severity_unit,
                    "source": source,
                    "raw_reference": (row.get("raw_reference") or "").strip() or None,
                    "notes": (row.get("notes") or "").strip() or None,
                }
            )
        except ValueError as exc:
            errors.append({"line": line_number, "row": row, "error": str(exc)})
    return valid, errors


def _write_error_log(path: Path, errors: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for error in errors:
            handle.write(_compact_json(error) + "\n")


def import_hazard_events(
    *,
    input_path: Path,
    database_path: Path,
    schema_path: Path,
    source_date: str | None,
    data_classification: str,
    strict: bool,
    error_log: Path | None,
) -> dict[str, Any]:
    if not input_path.is_file():
        raise ImportFailure(f"Input file does not exist: {input_path}")
    if not schema_path.is_file():
        raise ImportFailure(f"Database schema was not found: {schema_path}")

    with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            raise ImportFailure(
                "Input CSV must contain columns: " + ", ".join(sorted(REQUIRED_COLUMNS))
            )
        rows = list(reader)
    if not rows:
        raise ImportFailure("Input CSV contains no event rows.")

    valid, errors = _validate_rows(rows)
    if strict and errors:
        raise ImportFailure(
            f"{len(errors)} row(s) failed validation under --strict; no rows were "
            "imported. Re-run without --strict to import the valid rows and write "
            "an error log, or fix the input."
        )
    if errors:
        _write_error_log(
            error_log or input_path.with_suffix(input_path.suffix + ".import-errors.jsonl"),
            errors,
        )

    is_official = 1 if data_classification == "official" else 0
    is_demo = 0 if data_classification == "official" else 1
    imported_at = _utc_now()
    metadata = {
        "input_file": input_path.name,
        "input_sha256": _file_sha256(input_path),
        "imported_at": imported_at,
        "data_classification": data_classification,
    }
    metadata_json = _compact_json(metadata)

    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(schema_path.read_text(encoding="utf-8"))
        connection.execute("PRAGMA foreign_keys = ON")
        for record in valid:
            connection.execute(
                """
                INSERT INTO hazard_events (
                    event_type, occurred_at, severity_value, severity_unit,
                    source_name, source_date, raw_reference, notes,
                    is_official, is_demo, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["event_type"],
                    record["occurred_at"],
                    record["severity_value"],
                    record["severity_unit"],
                    record["source"],
                    source_date,
                    record["raw_reference"],
                    record["notes"],
                    is_official,
                    is_demo,
                    metadata_json,
                ),
            )
        connection.commit()
    finally:
        connection.close()

    return {
        "status": "success" if not errors else "partial",
        "database": str(database_path.resolve()),
        "row_count": len(rows),
        "imported_count": len(valid),
        "rejected_count": len(errors),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_path", type=Path)
    parser.add_argument("--db", dest="database_path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", dest="schema_path", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--source-date", help="Bulletin publication date, e.g. 2026-09-01.")
    parser.add_argument(
        "--data-classification",
        required=True,
        choices=("official", "demonstration"),
        help="Explicitly distinguish an official bulletin from demonstration data.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Reject the whole import if any row fails validation.",
    )
    parser.add_argument("--error-log", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = import_hazard_events(
            input_path=args.input_path,
            database_path=args.database_path,
            schema_path=args.schema_path,
            source_date=args.source_date,
            data_classification=args.data_classification,
            strict=args.strict,
            error_log=args.error_log,
        )
    except ImportFailure as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["rejected_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
