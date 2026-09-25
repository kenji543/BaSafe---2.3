from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = PROJECT_ROOT / "data" / "geosafe.snapshot.db"
ROUTING_CONFIG_PATH = PROJECT_ROOT / "config" / "routing.json"


class DeploymentSnapshotStalenessTests(unittest.TestCase):
    def setUp(self) -> None:
        if not SNAPSHOT_PATH.is_file():
            self.skipTest(f"{SNAPSHOT_PATH} is not present in this checkout.")

    def test_active_official_study_area_matches_routing_config(self) -> None:
        expected_version = json.loads(
            ROUTING_CONFIG_PATH.read_text(encoding="utf-8")
        )["study_area_version"]
        connection = sqlite3.connect(f"file:{SNAPSHOT_PATH.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT version, name FROM routing_study_areas "
                "WHERE is_active = 1 AND is_official = 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, "No active, official routing_study_areas row in the snapshot.")
        version, name = row
        self.assertEqual(
            version,
            expected_version,
            f"Snapshot's active study area is {version!r} ({name!r}) but "
            f"config/routing.json expects {expected_version!r}. Rebuild: "
            "python scripts/build_deployment_snapshot.py --force",
        )

    def test_official_evacuation_centers_are_present(self) -> None:
        connection = sqlite3.connect(f"file:{SNAPSHOT_PATH.as_posix()}?mode=ro", uri=True)
        try:
            count = connection.execute(
                "SELECT COUNT(*) FROM evacuation_centers WHERE active = 1 AND is_official = 1"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(count, 0)

    def test_committed_snapshot_has_no_citizen_reports(self) -> None:
        connection = sqlite3.connect(f"file:{SNAPSHOT_PATH.as_posix()}?mode=ro", uri=True)
        try:
            tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master")}
            if "citizen_reports" in tables:
                count = connection.execute("SELECT COUNT(*) FROM citizen_reports").fetchone()[0]
                self.assertEqual(count, 0)
        finally:
            connection.close()


class SnapshotBuilderPrivacyTests(unittest.TestCase):
    def test_builder_strips_citizen_reports(self) -> None:
        import sys
        import tempfile

        sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
        from build_deployment_snapshot import build_snapshot
        from tests.helpers import DemoApplication

        application = DemoApplication()
        self.addCleanup(application.close)
        with application.repository.connection() as connection:
            connection.execute(
                "INSERT INTO citizen_reports (created_at, latitude, longitude, damage_type, "
                "severity, reporter_name, reporter_phone) VALUES "
                "('2026-09-25T00:00:00+00:00', 11.5, 125.25, 'flooding', 'minor', 'A', '09170000000')"
            )
            connection.commit()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "snapshot.db"
            build_snapshot(application.database_path, output)
            connection = sqlite3.connect(output)
            try:
                count = connection.execute("SELECT COUNT(*) FROM citizen_reports").fetchone()[0]
            finally:
                connection.close()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
