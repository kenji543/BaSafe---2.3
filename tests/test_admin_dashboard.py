from __future__ import annotations

import base64
import csv
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from geosafe.admin import AdminDashboard, AdminOperationError
from geosafe.server import GeoSafeServer
from tests.helpers import DemoApplication


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = PROJECT_ROOT / "web"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AdminDashboardPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()

    def tearDown(self) -> None:
        self.application.close()

    def test_overview_is_aggregate_only_and_identifies_isolation(self) -> None:
        payload = AdminDashboard(
            self.application.repository, self.application.service
        ).overview()
        self.assertEqual(payload["environment"]["name"], "Isolated admin development")
        self.assertFalse(payload["environment"]["monitoring_read_only"])
        self.assertTrue(payload["environment"]["photo_uploads_enabled"])
        self.assertFalse(payload["environment"]["production_connected"])
        self.assertIn(
            "evacuation_center_details", payload["environment"]["allowed_admin_writes"]
        )
        self.assertEqual(payload["system"]["required_hazards_loaded"], 3)
        self.assertEqual(len(payload["datasets"]), 3)
        self.assertEqual(payload["context"]["historical_incidents"], 1)
        self.assertEqual(payload["context"]["clup_references"], 1)
        self.assertEqual(payload["scoring"]["total"], 0)
        self.assertTrue(
            all(center["active"] for center in payload["evacuation_centers"]["items"])
        )
        self.assertTrue(
            any(
                alert["title"] == "Evacuation routing is unavailable"
                for alert in payload["alerts"]
            )
        )

    def test_visitor_analytics_use_anonymous_identifiers(self) -> None:
        dashboard = AdminDashboard(
            self.application.repository, self.application.service
        )
        visitor_id = "anonymousVisitorIdentifier123"
        dashboard.record_visitor(visitor_id, "/map")
        dashboard.record_visitor(visitor_id, "/methodology")
        payload = dashboard.analytics()
        self.assertEqual(payload["summary"]["unique_visitors"], 1)
        self.assertEqual(payload["summary"]["online_now"], 1)
        self.assertEqual(payload["summary"]["page_views_today"], 2)
        self.assertEqual(payload["definition"]["privacy"], "Raw IP addresses are not stored by this feature.")

    def test_hazard_events_returns_all_rows_newest_first(self) -> None:
        with self.application.repository.connection() as connection:
            connection.executemany(
                """
                INSERT INTO hazard_events (
                    event_type, occurred_at, severity_value, severity_unit,
                    source_name, is_official, is_demo
                ) VALUES (?, ?, ?, ?, ?, 1, 0)
                """,
                [
                    ("rain", "2026-09-01T00:00:00+00:00", 40.0, "mm", "PAGASA Bulletin"),
                    ("rain", "2026-09-02T00:00:00+00:00", 15.5, "mm", "PAGASA Bulletin"),
                    ("earthquake", "2026-09-03T00:00:00+00:00", 3.2, "magnitude", "PHIVOLCS Bulletin"),
                    ("earthquake", "2026-09-04T00:00:00+00:00", 4.7, "magnitude", "PHIVOLCS Bulletin"),
                ],
            )
            connection.commit()
        payload = AdminDashboard(
            self.application.repository, self.application.service
        ).hazard_events()
        self.assertEqual(payload["total_logged"], 4)
        self.assertEqual(len(payload["events"]), 4)
        # Newest first.
        self.assertEqual(payload["events"][0]["occurred_at"], "2026-09-04T00:00:00+00:00")

    def test_events_are_never_windowed_or_capped(self) -> None:
        with self.application.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO hazard_events (
                    event_type, occurred_at, severity_value, severity_unit,
                    source_name, is_official, is_demo
                ) VALUES ('earthquake', '2020-01-01T00:00:00+00:00', 5.0, 'magnitude', 'Old Bulletin', 1, 0)
                """
            )
            connection.commit()
        payload = AdminDashboard(
            self.application.repository, self.application.service
        ).hazard_events()
        self.assertEqual(payload["total_logged"], 1)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["occurred_at"], "2020-01-01T00:00:00+00:00")


class EvacuationCenterAdminTests(unittest.TestCase):
    """Edit + publish flow: draft writes stay in one database until an
    explicit Publish call copies them into a separate target database,
    matched by external_id (centers) / psgc_code (barangays), never by
    raw autoincrement id."""

    def setUp(self) -> None:
        self.draft = DemoApplication()
        self.dashboard = AdminDashboard(self.draft.repository, self.draft.service)

    def tearDown(self) -> None:
        self.draft.close()

    def test_create_center_requires_coordinates_inside_the_boundary(self) -> None:
        with self.assertRaises(AdminOperationError) as caught:
            self.dashboard.create_evacuation_center(
                name="Outside Center", latitude=1.0, longitude=1.0,
                notes=None, actor="tester",
            )
        self.assertEqual(caught.exception.code, "coordinates_outside_boundary")

    def test_create_and_edit_center_writes_draft_with_audit_log(self) -> None:
        created = self.dashboard.create_evacuation_center(
            name="  New Facility  ", latitude=11.5, longitude=125.5,
            notes="A hall.", actor="tester",
        )
        self.assertEqual(created["name"], "New Facility")
        self.assertTrue(created["external_id"].startswith("admin-"))
        self.assertIsNone(created["published_at"])

        edited = self.dashboard.update_evacuation_center(
            created["id"], name="Renamed Facility", latitude=11.6,
            longitude=125.6, notes="Updated notes.", actor="tester2",
        )
        self.assertEqual(edited["name"], "Renamed Facility")
        self.assertEqual(edited["updated_by"], "tester2")

        with self.draft.repository.connection() as connection:
            actions = [
                row["action"]
                for row in connection.execute(
                    "SELECT action FROM admin_audit_log ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(actions, ["create_center", "edit_center"])

    def test_update_unknown_center_is_not_found(self) -> None:
        with self.assertRaises(AdminOperationError) as caught:
            self.dashboard.update_evacuation_center(
                999, name="X", latitude=11.5, longitude=125.5, notes=None, actor="tester",
            )
        self.assertEqual(caught.exception.code, "center_not_found")
        self.assertEqual(caught.exception.status, 404)

    def test_publish_center_upserts_into_target_database_by_external_id(self) -> None:
        target = DemoApplication()
        self.addCleanup(target.close)
        created = self.dashboard.create_evacuation_center(
            name="Published Facility", latitude=11.5, longitude=125.5,
            notes="Notes.", actor="tester",
        )
        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(target.database_path)},
            clear=False,
        ):
            published = self.dashboard.publish_evacuation_center(created["id"], actor="tester")
        self.assertIsNotNone(published["published_at"])
        target_row = target.repository.evacuation_center_by_external_id(created["external_id"])
        self.assertIsNotNone(target_row)
        self.assertEqual(target_row["name"], "Published Facility")
        self.assertIsNotNone(target_row["published_at"])

        # Re-publishing after another edit updates the same target row
        # rather than inserting a duplicate.
        self.dashboard.update_evacuation_center(
            created["id"], name="Published Facility (renamed)", latitude=11.5,
            longitude=125.5, notes="Notes.", actor="tester",
        )
        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(target.database_path)},
            clear=False,
        ):
            self.dashboard.publish_evacuation_center(created["id"], actor="tester")
        with target.repository.connection() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS c FROM evacuation_centers WHERE external_id = ?",
                (created["external_id"],),
            ).fetchone()["c"]
        self.assertEqual(count, 1)
        refreshed = target.repository.evacuation_center_by_external_id(created["external_id"])
        self.assertEqual(refreshed["name"], "Published Facility (renamed)")

    def test_publish_self_heals_a_target_database_missing_publish_columns(self) -> None:
        """A target database whose own server process hasn't restarted since
        this feature shipped won't have updated_at/published_at yet --
        publish must not depend on that restart having happened."""
        target = DemoApplication()
        self.addCleanup(target.close)
        with target.repository.connection() as connection:
            connection.execute("ALTER TABLE evacuation_centers DROP COLUMN updated_at")
            connection.execute("ALTER TABLE evacuation_centers DROP COLUMN updated_by")
            connection.execute("ALTER TABLE evacuation_centers DROP COLUMN published_at")
            connection.execute("ALTER TABLE barangays DROP COLUMN evacuation_center_id")
            connection.commit()

        created = self.dashboard.create_evacuation_center(
            name="Pre-migration Target Facility", latitude=11.5, longitude=125.5,
            notes=None, actor="tester",
        )
        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(target.database_path)},
            clear=False,
        ):
            published = self.dashboard.publish_evacuation_center(created["id"], actor="tester")
        self.assertIsNotNone(published["published_at"])
        target_row = target.repository.evacuation_center_by_external_id(created["external_id"])
        self.assertEqual(target_row["name"], "Pre-migration Target Facility")

    def test_publish_target_missing_is_a_clear_error(self) -> None:
        created = self.dashboard.create_evacuation_center(
            name="Facility", latitude=11.5, longitude=125.5, notes=None, actor="tester",
        )
        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": "/nonexistent/path/geosafe.db"},
            clear=False,
        ):
            with self.assertRaises(AdminOperationError) as caught:
                self.dashboard.publish_evacuation_center(created["id"], actor="tester")
        self.assertEqual(caught.exception.code, "publish_target_missing")

    def test_designate_and_publish_barangay_matches_target_by_psgc_code(self) -> None:
        target = DemoApplication()
        self.addCleanup(target.close)
        barangay = next(
            b for b in self.draft.repository.barangays() if b["psgc_code"] == "TEST-WEST"
        )
        created = self.dashboard.create_evacuation_center(
            name="Shared Center", latitude=11.5, longitude=125.5, notes=None, actor="tester",
        )
        self.dashboard.designate_barangay_center(barangay["id"], created["id"], actor="tester")

        listing = self.dashboard.barangays_admin_list()
        row = next(r for r in listing["items"] if r["barangay_id"] == barangay["id"])
        self.assertEqual(row["designated_center"]["name"], "Shared Center")
        self.assertIsNone(row["designation_published_at"])

        # Publishing the designation before the center itself is published
        # is refused -- the target database would have nothing to point to.
        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(target.database_path)},
            clear=False,
        ):
            with self.assertRaises(AdminOperationError) as caught:
                self.dashboard.publish_barangay_designation(barangay["id"], actor="tester")
        self.assertEqual(caught.exception.code, "center_not_published")

        with patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(target.database_path)},
            clear=False,
        ):
            self.dashboard.publish_evacuation_center(created["id"], actor="tester")
            self.dashboard.publish_barangay_designation(barangay["id"], actor="tester")

        target_barangay = target.repository.barangay_by_psgc_or_name(
            psgc_code="TEST-WEST", name="Test West"
        )
        target_designations = {
            row["barangay_id"]: row
            for row in target.repository.barangays_with_designations()
        }
        target_row = target_designations[target_barangay["id"]]
        self.assertIsNotNone(target_row["designated_center"])
        self.assertEqual(target_row["designated_center"]["name"], "Shared Center")
        self.assertIsNotNone(target_row["designation_published_at"])

    def test_designate_unknown_barangay_or_center_is_not_found(self) -> None:
        with self.assertRaises(AdminOperationError) as caught:
            self.dashboard.designate_barangay_center(999, 1, actor="tester")
        self.assertEqual(caught.exception.code, "barangay_not_found")
        barangay = self.draft.repository.barangays()[0]
        with self.assertRaises(AdminOperationError) as caught:
            self.dashboard.designate_barangay_center(barangay["id"], 999, actor="tester")
        self.assertEqual(caught.exception.code, "center_not_found")


class CitizenReportAdminTests(unittest.TestCase):
    """The public app writes reports into its own database; the admin
    process reads and triages them from that database, sharing one uploads
    folder, while its audit log stays in the admin's own database."""

    def setUp(self) -> None:
        self.public = DemoApplication()
        self.admin = DemoApplication()
        uploads = Path(self.public.temporary_directory.name) / "uploads"
        self.public_dashboard = AdminDashboard(
            self.public.repository, self.public.service, uploads_root=uploads
        )
        self.admin_dashboard = AdminDashboard(
            self.admin.repository, self.admin.service, uploads_root=uploads
        )
        target = patch.dict(
            "os.environ",
            {"GEOSAFE_PUBLISH_TARGET_DB": str(self.public.database_path)},
            clear=False,
        )
        target.start()
        self.addCleanup(target.stop)

    def tearDown(self) -> None:
        self.public.close()
        self.admin.close()

    def _submit(self) -> dict:
        from io import BytesIO

        from PIL import Image

        photo = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(photo, "JPEG")
        return self.public_dashboard.submit_citizen_report(
            {
                "latitude": "11.5",
                "longitude": "125.25",
                "damage_type": "road_blocked",
                "severity": "moderate",
                "reporter_name": "Maria",
                "reporter_phone": "+639171234567",
                "consent": "yes",
            },
            [photo.getvalue()],
        )

    def test_admin_lists_triages_and_serves_photo(self) -> None:
        submitted = self._submit()
        listing = self.admin_dashboard.citizen_reports()
        self.assertEqual(listing["count"], 1)
        report = listing["items"][0]
        self.assertEqual(report["id"], submitted["id"])
        self.assertEqual(report["status"], "new")
        [filename] = report["photos"]

        content, mime_type = self.admin_dashboard.citizen_report_photo(report["id"], filename)
        self.assertEqual(mime_type, "image/jpeg")
        self.assertTrue(content.startswith(b"\xff\xd8\xff"))
        with self.assertRaises(AdminOperationError) as caught:
            self.admin_dashboard.citizen_report_photo(report["id"], "0" * 32 + ".jpg")
        self.assertEqual(caught.exception.status, 404)

        self.admin_dashboard.set_citizen_report_status(report["id"], "acknowledged", actor="mdrrmo")
        self.assertEqual(self.admin_dashboard.citizen_reports()["items"][0]["status"], "acknowledged")
        with self.admin.repository.connection() as connection:
            action = connection.execute(
                "SELECT action FROM admin_audit_log WHERE entity_type = 'citizen_report'"
            ).fetchone()["action"]
        self.assertEqual(action, "report_status")

        with self.assertRaises(AdminOperationError) as caught:
            self.admin_dashboard.set_citizen_report_status(report["id"], "deleted", actor="mdrrmo")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(AdminOperationError) as caught:
            self.admin_dashboard.set_citizen_report_status(999, "resolved", actor="mdrrmo")
        self.assertEqual(caught.exception.status, 404)

    def test_target_database_without_the_table_lists_empty(self) -> None:
        with self.public.repository.connection() as connection:
            connection.execute("DROP TABLE citizen_reports")
            connection.commit()
        self.assertEqual(self.admin_dashboard.citizen_reports()["count"], 0)

    def test_overview_declares_the_status_write(self) -> None:
        self.assertIn(
            "citizen_report_status",
            self.admin_dashboard.overview()["environment"]["allowed_admin_writes"],
        )


class AdminDashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()
        environment = {
            "GEOSAFE_ADMIN_ENABLED": "true",
            "GEOSAFE_ADMIN_USERNAME": "admin-test",
            "GEOSAFE_ADMIN_PASSWORD": "admin-test-password",
            "GEOSAFE_VISITOR_ANALYTICS_ENABLED": "true",
        }
        with patch.dict("os.environ", environment, clear=False):
            self.server = GeoSafeServer(
                ("127.0.0.1", 0), self.application.api, WEB_ROOT
            )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.admin_session = self.server.create_admin_session()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.application.close()

    def request(
        self,
        path: str,
        *,
        authorized: bool = False,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        request_headers = dict(headers or {})
        if authorized:
            request_headers["Cookie"] = (
                f"basafe_admin_session={self.admin_session}"
            )
        return urlopen(
            Request(
                self.base_url + path,
                headers=request_headers,
                method=method,
                data=data,
            ),
            timeout=3,
        )

    def test_admin_page_requires_authentication(self) -> None:
        with self.request("/admin") as response:
            page = response.read().decode("utf-8")
            self.assertTrue(response.geturl().endswith("/admin/login"))
        self.assertIn("Sign in to continue", page)
        self.assertIn("login-form", page)

        with self.request("/admin", authorized=True) as response:
            page = response.read().decode("utf-8")
        self.assertIn("System overview", page)
        self.assertIn("Visitor analytics", page)

    def test_admin_login_creates_a_session_and_logout_revokes_it(self) -> None:
        login_body = json.dumps(
            {
                "username": "admin-test",
                "password": "admin-test-password",
            }
        ).encode("utf-8")
        with self.request(
            "/api/v1/admin/login",
            method="POST",
            data=login_body,
            headers={"Content-Type": "application/json"},
        ) as response:
            payload = json.loads(response.read())
            session_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(payload["authenticated"])
        self.assertIn("basafe_admin_session=", session_cookie)

        with self.request("/admin", headers={"Cookie": session_cookie}) as response:
            self.assertIn("System overview", response.read().decode("utf-8"))
        with self.request(
            "/api/v1/admin/logout",
            method="POST",
            data=b"",
            headers={"Cookie": session_cookie},
        ) as response:
            self.assertFalse(json.loads(response.read())["authenticated"])
            self.assertIn("Max-Age=0", response.headers["Set-Cookie"])
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "/api/v1/admin/overview",
                headers={"Cookie": session_cookie},
            )
        self.assertEqual(caught.exception.code, 401)

    def test_admin_login_rejects_invalid_credentials_without_basic_prompt(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "/api/v1/admin/login",
                method="POST",
                data=json.dumps(
                    {"username": "admin-test", "password": "incorrect"}
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(caught.exception.code, 401)
        self.assertIsNone(caught.exception.headers.get("WWW-Authenticate"))
        self.assertEqual(
            json.loads(caught.exception.read())["error"]["code"],
            "invalid_credentials",
        )

    def test_admin_overview_requires_authentication_and_is_read_only(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v1/admin/overview")
        self.assertEqual(caught.exception.code, 401)

        with self.request(
            "/api/v1/admin/overview", authorized=True
        ) as response:
            payload = json.loads(response.read())
        self.assertFalse(payload["environment"]["monitoring_read_only"])
        self.assertFalse(payload["environment"]["production_connected"])
        self.assertEqual(payload["system"]["required_hazards_total"], 3)

    def test_public_page_views_feed_anonymous_analytics(self) -> None:
        with self.request("/map") as response:
            visitor_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        with self.request("/methodology", headers={"Cookie": visitor_cookie}):
            pass
        with self.request(
            "/api/v1/admin/analytics", authorized=True
        ) as response:
            payload = json.loads(response.read())
        self.assertEqual(payload["summary"]["unique_visitors"], 1)
        self.assertEqual(payload["summary"]["online_now"], 1)
        self.assertEqual(payload["summary"]["page_views_today"], 2)

    def test_admin_can_upload_and_retrieve_a_valid_center_photo(self) -> None:
        with self.application.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO evacuation_centers (
                    id, external_id, name, latitude, longitude, barangay,
                    designation, source_name, dataset_version,
                    is_official, active
                ) VALUES (
                    901, 'ADMIN-PHOTO-TEST', 'Photo Test Center',
                    11.28, 125.07, 'Test West', 'Test designation',
                    'Test source', 'admin-test-v1', 1, 1
                )
                """
            )
            connection.commit()
        boundary = "----BasafeAdminPhotoBoundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="photo"; filename="center.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode() + ONE_PIXEL_PNG + (
            f"\r\n--{boundary}\r\n"
            'Content-Disposition: form-data; name="alt_text"\r\n\r\n'
            "Front of Photo Test Center\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="source"\r\n\r\n'
            "Basey MDRRMO\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        with self.request(
            "/api/v1/admin/evacuation-centers/901/photo",
            authorized=True,
            method="POST",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        ) as response:
            self.assertEqual(response.status, 201)
            payload = json.loads(response.read())
        self.assertEqual(payload["photo_alt"], "Front of Photo Test Center")
        self.assertEqual(payload["photo_source"], "Basey MDRRMO")
        with self.request(payload["photo_url"], authorized=True) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), ONE_PIXEL_PNG)
        with self.application.repository.connection() as connection:
            audit_count = connection.execute(
                "SELECT COUNT(*) AS count FROM admin_audit_log"
            ).fetchone()["count"]
        self.assertEqual(audit_count, 1)

    def test_evacuation_center_create_edit_and_list_require_authentication(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "/api/v1/admin/evacuation-centers",
                method="POST",
                data=json.dumps({"name": "X", "latitude": 11.5, "longitude": 125.5}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v1/admin/evacuation-centers")
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v1/admin/barangays")
        self.assertEqual(caught.exception.code, 401)

    def test_reports_require_login_and_admin_server_refuses_intake(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/v1/admin/reports")
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "/api/v1/reports",
                method="POST",
                data=b"x",
                headers={"Content-Type": "multipart/form-data; boundary=x"},
            )
        self.assertEqual(caught.exception.code, 503)
        self.assertEqual(
            json.loads(caught.exception.read())["error"]["code"], "report_intake_unavailable"
        )

    def test_create_edit_and_publish_center_over_http(self) -> None:
        with self.request(
            "/api/v1/admin/evacuation-centers",
            authorized=True,
            method="POST",
            data=json.dumps(
                {"name": "HTTP Center", "latitude": 11.5, "longitude": 125.5, "notes": "Notes."}
            ).encode(),
            headers={"Content-Type": "application/json"},
        ) as response:
            self.assertEqual(response.status, 201)
            created = json.loads(response.read())
        self.assertEqual(created["name"], "HTTP Center")
        center_id = created["id"]

        with self.request(
            f"/api/v1/admin/evacuation-centers/{center_id}",
            authorized=True,
            method="POST",
            data=json.dumps(
                {"name": "HTTP Center Renamed", "latitude": 11.5, "longitude": 125.5, "notes": None}
            ).encode(),
            headers={"Content-Type": "application/json"},
        ) as response:
            edited = json.loads(response.read())
        self.assertEqual(edited["name"], "HTTP Center Renamed")

        with tempfile.TemporaryDirectory() as target_directory:
            target_path = Path(target_directory) / "target-geosafe.db"
            from geosafe.repository import Repository as _Repository

            _Repository(target_path, PROJECT_ROOT / "db" / "schema.sql").initialize(
                self.application.model
            )
            with patch.dict(
                "os.environ", {"GEOSAFE_PUBLISH_TARGET_DB": str(target_path)}, clear=False
            ):
                with self.request(
                    f"/api/v1/admin/evacuation-centers/{center_id}/publish",
                    authorized=True,
                    method="POST",
                    data=b"",
                ) as response:
                    published = json.loads(response.read())
            self.assertIsNotNone(published["published_at"])

    def test_designate_barangay_over_http_requires_a_valid_center_id(self) -> None:
        barangay = self.application.repository.barangays()[0]
        with self.assertRaises(HTTPError) as caught:
            self.request(
                f"/api/v1/admin/barangays/{barangay['id']}/designate",
                authorized=True,
                method="POST",
                data=json.dumps({"evacuation_center_id": "not-an-int"}).encode(),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(
            json.loads(caught.exception.read())["error"]["code"],
            "evacuation_center_id_required",
        )


class AdminDashboardFrontendContractTests(unittest.TestCase):
    def test_dashboard_uses_dedicated_feature_pages_and_is_mobile_ready(self) -> None:
        pages = {
            name: (WEB_ROOT / "admin" / name).read_text(encoding="utf-8")
            for name in (
                "index.html",
                "analytics.html",
                "datasets.html",
                "evacuation-centers.html",
                "hazard-events.html",
                "reports.html",
                "login.html",
            )
        }
        script = (WEB_ROOT / "admin" / "admin.js").read_text(encoding="utf-8")
        login_script = (WEB_ROOT / "admin" / "login.js").read_text(encoding="utf-8")
        styles = (WEB_ROOT / "admin" / "admin.css").read_text(encoding="utf-8")
        self.assertIn("System overview", pages["index.html"])
        self.assertIn("Visitors and page views", pages["analytics.html"])
        self.assertIn("Hazard datasets", pages["datasets.html"])
        self.assertIn("photo-upload-form", pages["evacuation-centers.html"])
        self.assertIn("Logged hazard events", pages["hazard-events.html"])
        self.assertIn("Sign in to continue", pages["login.html"])
        self.assertIn("/api/v1/admin/login", login_script)
        self.assertIn("/api/v1/admin/logout", script)
        self.assertIn('apiFetch("/api/v1/admin/overview"', script)
        self.assertIn('apiFetch("/api/v1/admin/analytics?days=7"', script)
        self.assertIn('apiFetch("/api/v1/admin/hazard-events"', script)
        self.assertIn('apiFetch("/api/v1/admin/reports"', script)
        self.assertIn("Citizen reports", pages["reports.html"])
        self.assertIn("new FormData()", script)
        self.assertIn("@media (max-width: 760px)", styles)
        self.assertNotIn("Upload dataset", pages["datasets.html"])
        self.assertNotIn("Delete center", pages["evacuation-centers.html"])
        for name, markup in pages.items():
            if name == "login.html":
                continue
            self.assertNotIn("/admin/routing", markup, f"{name} still has a removed routing nav link")
            self.assertNotIn("/admin/context", markup, f"{name} still has a removed planning-context nav link")
            self.assertNotIn("/admin/activity", markup, f"{name} still has a removed scoring-activity nav link")
            self.assertNotIn("/admin/sync-history", markup, f"{name} still has a removed sync-history nav link")
            self.assertIn("/admin/hazard-events", markup, f"{name} is missing the hazard-events nav link")
            self.assertIn("/admin/reports", markup, f"{name} is missing the citizen-reports nav link")


SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from sync_ulap_snapshot import _finish_sync_run, _start_sync_run  # noqa: E402
from import_hazard_events import ImportFailure, import_hazard_events  # noqa: E402
import sync_earthquake_events  # noqa: E402
import sync_rain_events  # noqa: E402
import sync_rain_events_openmeteo  # noqa: E402


class SyncRunPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()

    def tearDown(self) -> None:
        self.application.close()

    def test_start_and_finish_are_recorded(self) -> None:
        run_id = _start_sync_run(self.application.database_path, "flood")
        self.assertIsNotNone(run_id)
        _finish_sync_run(
            self.application.database_path, run_id, status="success", error_message=None
        )
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT target, status, finished_at FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
        self.assertEqual(row["target"], "flood")
        self.assertEqual(row["status"], "success")
        self.assertIsNotNone(row["finished_at"])

    def test_failure_records_the_error_message(self) -> None:
        run_id = _start_sync_run(self.application.database_path, "liquefaction")
        _finish_sync_run(
            self.application.database_path,
            run_id,
            status="failed",
            error_message="liquefaction: outside_coverage: no features returned",
        )
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT status, error_message FROM sync_runs WHERE id = ?", (run_id,)
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("outside_coverage", row["error_message"])

    def test_start_against_a_schema_without_sync_runs_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bare_db = Path(directory) / "bare.sqlite3"
            sqlite3.connect(bare_db).close()
            run_id = _start_sync_run(bare_db, "flood")
        self.assertIsNone(run_id)


class HazardEventImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)

    def tearDown(self) -> None:
        self.application.close()

    def _write_csv(self, name: str, rows: list[dict[str, str]]) -> Path:
        path = Path(self.temp_directory.name) / name
        fieldnames = ["event_type", "occurred_at", "severity_value", "severity_unit", "source", "raw_reference", "notes"]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def test_valid_rows_import_with_official_classification(self) -> None:
        csv_path = self._write_csv(
            "events.csv",
            [
                {
                    "event_type": "rain",
                    "occurred_at": "2026-09-01",
                    "severity_value": "42.5",
                    "severity_unit": "mm",
                    "source": "PAGASA Regional Bulletin",
                    "raw_reference": "https://pagasa.example/bulletin/1",
                    "notes": "",
                }
            ],
        )
        result = import_hazard_events(
            input_path=csv_path,
            database_path=self.application.database_path,
            schema_path=PROJECT_ROOT / "db" / "schema.sql",
            source_date="2026-09-01",
            data_classification="official",
            strict=True,
            error_log=None,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["imported_count"], 1)
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT event_type, severity_value, is_official, is_demo FROM hazard_events"
            ).fetchone()
        self.assertEqual(row["event_type"], "rain")
        self.assertEqual(row["is_official"], 1)
        self.assertEqual(row["is_demo"], 0)

    def test_strict_import_rejects_the_whole_batch_on_a_bad_row(self) -> None:
        csv_path = self._write_csv(
            "events.csv",
            [
                {
                    "event_type": "not_a_real_type",
                    "occurred_at": "2026-09-01",
                    "severity_value": "1",
                    "severity_unit": "mm",
                    "source": "Test",
                    "raw_reference": "",
                    "notes": "",
                }
            ],
        )
        with self.assertRaises(ImportFailure):
            import_hazard_events(
                input_path=csv_path,
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                source_date=None,
                data_classification="official",
                strict=True,
                error_log=None,
            )
        with self.application.repository.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM hazard_events").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_non_strict_import_writes_an_error_log_and_keeps_valid_rows(self) -> None:
        csv_path = self._write_csv(
            "events.csv",
            [
                {
                    "event_type": "earthquake",
                    "occurred_at": "2026-09-03",
                    "severity_value": "4.1",
                    "severity_unit": "magnitude",
                    "source": "PHIVOLCS Bulletin",
                    "raw_reference": "",
                    "notes": "",
                },
                {
                    "event_type": "earthquake",
                    "occurred_at": "not-a-date",
                    "severity_value": "4.1",
                    "severity_unit": "magnitude",
                    "source": "PHIVOLCS Bulletin",
                    "raw_reference": "",
                    "notes": "",
                },
            ],
        )
        error_log = Path(self.temp_directory.name) / "errors.jsonl"
        result = import_hazard_events(
            input_path=csv_path,
            database_path=self.application.database_path,
            schema_path=PROJECT_ROOT / "db" / "schema.sql",
            source_date=None,
            data_classification="demonstration",
            strict=False,
            error_log=error_log,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["imported_count"], 1)
        self.assertEqual(result["rejected_count"], 1)
        self.assertTrue(error_log.is_file())
        logged = json.loads(error_log.read_text(encoding="utf-8").splitlines()[0])
        self.assertIn("occurred_at", logged["error"])
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT is_official, is_demo FROM hazard_events"
            ).fetchone()
        self.assertEqual(row["is_official"], 0)
        self.assertEqual(row["is_demo"], 1)


class UsgsEarthquakeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()

    def tearDown(self) -> None:
        self.application.close()

    @staticmethod
    def _feature(url: str, mag: float, place: str, time_ms: int) -> dict:
        return {
            "properties": {"mag": mag, "place": place, "time": time_ms, "url": url}
        }

    def test_new_events_are_imported_as_official(self) -> None:
        features = [
            self._feature(
                "https://earthquake.usgs.gov/earthquakes/eventpage/us1",
                5.3,
                "32 km NE of Hernani, Philippines",
                1739566200000,
            )
        ]
        with patch.object(sync_earthquake_events, "_fetch_usgs_events", return_value=features):
            result = sync_earthquake_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=90,
                min_magnitude=5.0,
                latitude=11.28,
                longitude=125.07,
                radius_km=150.0,
                data_classification="official",
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["imported_count"], 1)
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT event_type, severity_value, source_name, is_official FROM hazard_events"
            ).fetchone()
        self.assertEqual(row["event_type"], "earthquake")
        self.assertEqual(row["severity_value"], 5.3)
        self.assertIn("USGS", row["source_name"])
        self.assertEqual(row["is_official"], 1)

    def test_already_logged_events_are_not_reimported(self) -> None:
        url = "https://earthquake.usgs.gov/earthquakes/eventpage/us2"
        features = [self._feature(url, 5.5, "Eastern Samar, Philippines", 1739566200000)]
        with patch.object(sync_earthquake_events, "_fetch_usgs_events", return_value=features):
            sync_earthquake_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=90,
                min_magnitude=5.0,
                latitude=11.28,
                longitude=125.07,
                radius_km=150.0,
                data_classification="official",
            )
            second = sync_earthquake_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=90,
                min_magnitude=5.0,
                latitude=11.28,
                longitude=125.07,
                radius_km=150.0,
                data_classification="official",
            )
        self.assertEqual(second["status"], "no_new_events")
        with self.application.repository.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM hazard_events").fetchone()["c"]
        self.assertEqual(count, 1)


class PagasaRainAdvisorySyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()

    def tearDown(self) -> None:
        self.application.close()

    SAMPLE_TEXT = (
        "WEATHER ADVISORY NO. 12\n"
        "For: Tropical Cyclone WILMA\n"
        "Issued at: 5:00 AM, 6 December 2025\n"
        "Sorsogon, Masbate, Northern Samar, Eastern Samar, and Samar would have "
        "100 to 200 mm of rainfall due to Wilma.\n"
    )
    NO_MENTION_TEXT = (
        "WEATHER ADVISORY NO. 80 FINAL\n"
        "For: Southwest Monsoon\n"
        "Issued at: 5:00 PM, 11 September 2026\n"
        "Scattered rains may still be experienced over Metro Manila.\n"
    )

    def test_advisory_matching_target_province_is_logged(self) -> None:
        with patch.object(sync_rain_events, "_fetch_advisory_pdf", return_value=b"fake-pdf"), \
             patch.object(sync_rain_events, "_extract_text", return_value=self.SAMPLE_TEXT):
            result = sync_rain_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                target_province="Samar",
                data_classification="official",
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["advisory_number"], "12")
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT event_type, severity_value, severity_unit, occurred_at, is_official "
                "FROM hazard_events"
            ).fetchone()
        self.assertEqual(row["event_type"], "rain")
        self.assertEqual(row["severity_value"], 150.0)
        self.assertEqual(row["severity_unit"], "mm")
        self.assertEqual(row["occurred_at"], "2025-12-06T05:00:00+00:00")
        self.assertEqual(row["is_official"], 1)

    def test_advisory_without_target_province_mention_is_not_logged(self) -> None:
        with patch.object(sync_rain_events, "_fetch_advisory_pdf", return_value=b"fake-pdf"), \
             patch.object(sync_rain_events, "_extract_text", return_value=self.NO_MENTION_TEXT):
            result = sync_rain_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                target_province="Samar",
                data_classification="official",
            )
        self.assertEqual(result["status"], "no_rainfall_mention")
        with self.application.repository.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM hazard_events").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_same_advisory_number_is_not_relogged(self) -> None:
        with patch.object(sync_rain_events, "_fetch_advisory_pdf", return_value=b"fake-pdf"), \
             patch.object(sync_rain_events, "_extract_text", return_value=self.SAMPLE_TEXT):
            sync_rain_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                target_province="Samar",
                data_classification="official",
            )
            second = sync_rain_events.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                target_province="Samar",
                data_classification="official",
            )
        self.assertEqual(second["status"], "already_logged")
        with self.application.repository.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM hazard_events").fetchone()["c"]
        self.assertEqual(count, 1)


class OpenMeteoRainSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = DemoApplication()

    def tearDown(self) -> None:
        self.application.close()

    SAMPLE_PAYLOAD = {
        "latitude": 11.35,
        "longitude": 125.10,
        "daily": {
            "time": ["2025-12-04", "2025-12-05", "2025-12-06", "2025-12-07"],
            "precipitation_sum": [5.0, 12.3, 68.4, 30.1],
        },
    }

    def test_days_above_threshold_are_logged_as_basey_specific(self) -> None:
        with patch.object(
            sync_rain_events_openmeteo, "_fetch_daily_precipitation", return_value=self.SAMPLE_PAYLOAD
        ):
            result = sync_rain_events_openmeteo.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=10,
                min_daily_mm=50.0,
                latitude=11.28,
                longitude=125.07,
                data_classification="official",
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["days_above_threshold"], 1)
        self.assertEqual(result["imported_count"], 1)
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT event_type, occurred_at, severity_value, source_name FROM hazard_events"
            ).fetchone()
        self.assertEqual(row["event_type"], "rain")
        self.assertEqual(row["occurred_at"], "2025-12-06T00:00:00+00:00")
        self.assertEqual(row["severity_value"], 68.4)
        self.assertIn("Open-Meteo", row["source_name"])

    def test_days_already_logged_are_not_reimported(self) -> None:
        with patch.object(
            sync_rain_events_openmeteo, "_fetch_daily_precipitation", return_value=self.SAMPLE_PAYLOAD
        ):
            sync_rain_events_openmeteo.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=10,
                min_daily_mm=50.0,
                latitude=11.28,
                longitude=125.07,
                data_classification="official",
            )
            second = sync_rain_events_openmeteo.sync(
                database_path=self.application.database_path,
                schema_path=PROJECT_ROOT / "db" / "schema.sql",
                days_back=10,
                min_daily_mm=50.0,
                latitude=11.28,
                longitude=125.07,
                data_classification="official",
            )
        self.assertEqual(second["new_events"], 0)
        self.assertEqual(second["already_logged_count"], 1)
        with self.application.repository.connection() as connection:
            count = connection.execute("SELECT COUNT(*) AS c FROM hazard_events").fetchone()["c"]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
