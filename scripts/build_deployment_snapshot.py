"""Build a privacy-safe SQLite snapshot for local clones and Vercel.

The source database may contain device-local assessment history. This command
uses SQLite's backup API, removes every assessment-owned record through foreign
key cascades, compacts the copy, and verifies its integrity before publishing.
It never changes the source database.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


PRIVATE_TABLES = (
    "assessments",
    "assessment_inputs",
    "assessment_memberships",
    "assessment_rule_activations",
    "assessment_results",
    "generated_reports",
    "citizen_reports",
)


def row_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def build_snapshot(source: Path, destination: Path, *, force: bool = False) -> str:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Source and destination databases must be different files.")
    if not source.is_file():
        raise FileNotFoundError(f"Source database not found: {source}")
    if destination.exists() and not force:
        raise FileExistsError(
            f"Destination already exists: {destination}. Pass --force to replace it."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".building")
    temporary.unlink(missing_ok=True)

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
        destination_connection.execute("PRAGMA foreign_keys = ON")
        existing = {
            name
            for (name,) in destination_connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        private_tables = [table for table in PRIVATE_TABLES if table in existing]
        for table in private_tables:
            destination_connection.execute(f"DELETE FROM {table}")
        destination_connection.execute(
            "DELETE FROM sqlite_sequence WHERE name IN ("
            + ",".join("?" for _ in PRIVATE_TABLES)
            + ")",
            PRIVATE_TABLES,
        )
        destination_connection.commit()

        remaining = {
            table: row_count(destination_connection, table) for table in private_tables
        }
        if any(remaining.values()):
            raise RuntimeError(f"Private rows remain in deployment snapshot: {remaining}")

        destination_connection.execute("VACUUM")
        integrity = destination_connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    except Exception:
        destination_connection.close()
        source_connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        destination_connection.close()
        source_connection.close()

    temporary.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a sanitized Basafe deployment database."
    )
    parser.add_argument("--source", type=Path, default=Path("data/geosafe.db"))
    parser.add_argument(
        "--output", type=Path, default=Path("data/geosafe.snapshot.db")
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    digest = build_snapshot(args.source, args.output, force=args.force)
    size_mb = args.output.stat().st_size / (1024 * 1024)
    print(f"Created {args.output} ({size_mb:.2f} MiB)")
    print(f"SHA256 {digest}")


if __name__ == "__main__":
    main()
