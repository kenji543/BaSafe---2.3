from __future__ import annotations

import base64
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from geosafe.admin import AdminDashboard
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
        self.assertTrue(payload["environment"]["monitoring_read_only"])
        self.assertTrue(payload["environment"]["photo_uploads_enabled"])
        self.assertFalse(payload["environment"]["production_connected"])
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
        self.assertTrue(payload["environment"]["monitoring_read_only"])
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


class AdminDashboardFrontendContractTests(unittest.TestCase):
    def test_dashboard_uses_dedicated_feature_pages_and_is_mobile_ready(self) -> None:
        pages = {
            name: (WEB_ROOT / "admin" / name).read_text(encoding="utf-8")
            for name in (
                "index.html",
                "analytics.html",
                "datasets.html",
                "evacuation-centers.html",
                "routing.html",
                "context.html",
                "activity.html",
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
        self.assertIn("Routing dependencies", pages["routing.html"])
        self.assertIn("Planning context", pages["context.html"])
        self.assertIn("Scoring activity", pages["activity.html"])
        self.assertIn("Sign in to continue", pages["login.html"])
        self.assertIn("/api/v1/admin/login", login_script)
        self.assertIn("/api/v1/admin/logout", script)
        self.assertIn('apiFetch("/api/v1/admin/overview"', script)
        self.assertIn('apiFetch("/api/v1/admin/analytics?days=7"', script)
        self.assertIn("new FormData()", script)
        self.assertIn("@media (max-width: 760px)", styles)
        self.assertNotIn("Upload dataset", pages["datasets.html"])
        self.assertNotIn("Delete center", pages["evacuation-centers.html"])


if __name__ == "__main__":
    unittest.main()
