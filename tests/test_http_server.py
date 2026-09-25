from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from geosafe.server import GeoSafeServer
from tests.helpers import DemoApplication


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


class HttpServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = DemoApplication()
        cls.server = GeoSafeServer(
            ("127.0.0.1", 0), cls.application.api, WEB_ROOT
        )
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.application.close()

    def test_public_landing_and_map_pages_load(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=5) as response:
            content = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Understand the hazards affecting a location.", content)
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertIn("https://server.arcgisonline.com", response.headers["Content-Security-Policy"])
            self.assertIn("https://ulap-hazards.georisk.gov.ph", response.headers["Content-Security-Policy"])
        with urlopen(f"{self.base_url}/map", timeout=5) as response:
            content = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn("Interactive Basey hazard map", content)

    def test_public_routes_and_pwa_assets_load(self) -> None:
        for route in (
            "/methodology",
            "/data-sources",
            "/limitations",
            "/about",
            "/privacy",
            "/offline",
            "/report-damage",
            "/manifest.webmanifest",
            "/service-worker.js",
        ):
            with self.subTest(route=route):
                with urlopen(f"{self.base_url}{route}", timeout=5) as response:
                    self.assertEqual(response.status, 200)

    def test_health_and_compatibility_endpoints(self) -> None:
        for route in ("/api/health", "/api/layers", "/api/model/current"):
            with self.subTest(route=route):
                with urlopen(f"{self.base_url}{route}", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(json.load(response))
        try:
            response = urlopen(f"{self.base_url}/api/source-status", timeout=5)
        except HTTPError as error:
            self.assertEqual(error.code, 503)
            self.assertTrue(json.load(error))
        else:
            with response:
                self.assertEqual(response.status, 200)
                self.assertTrue(json.load(response))

    def test_api_workflow_over_http(self) -> None:
        request = Request(
            f"{self.base_url}/api/v1/assessments",
            data=json.dumps({"latitude": 11.5, "longitude": 125.25}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as response:
            assessment = json.load(response)
            self.assertEqual(response.status, 201)
            self.assertEqual(assessment["status"], "complete")
            report_url = assessment["links"]["report"]
        with urlopen(f"{self.base_url}{report_url}", timeout=5) as report:
            self.assertEqual(report.headers["Content-Type"], "application/pdf")
            self.assertTrue(report.read(8).startswith(b"%PDF-1.4"))

    def test_out_of_scope_http_endpoint_is_absent(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base_url}/api/v1/admin", timeout=5)
        self.assertEqual(caught.exception.code, 404)

    def test_assessment_rate_limit_is_bounded_per_client(self) -> None:
        client = "203.0.113.77"
        for _ in range(self.server.assessment_rate_limit):
            allowed, _, _, _ = self.server.check_rate_limit(client, "assessment")
            self.assertTrue(allowed)
        allowed, limit, remaining, retry_after = self.server.check_rate_limit(
            client, "assessment"
        )
        self.assertFalse(allowed)
        self.assertEqual(limit, self.server.assessment_rate_limit)
        self.assertEqual(remaining, 0)
        self.assertGreaterEqual(retry_after, 1)

    def _post_report(self, fields: dict[str, str], photos: list[bytes] = ()):
        boundary = "----BasafeReportBoundary"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            for name, value in fields.items()
        ]
        for photo in photos:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="p.jpg"\r\n'
                "Content-Type: image/jpeg\r\n\r\n".encode()
                + photo
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        request = Request(
            f"{self.base_url}/api/v1/reports",
            data=b"".join(parts),
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    REPORT = {
        "latitude": "11.5",
        "longitude": "125.25",
        "damage_type": "flooding",
        "severity": "severe",
        "people_affected": "4",
        "street": "Rizal St.",
        "reporter_name": "Juan Dela Cruz",
        "reporter_phone": "0917 123 4567",
        "consent": "yes",
    }

    def test_citizen_report_is_stored_with_barangay_and_clean_photo(self) -> None:
        from io import BytesIO

        from PIL import Image

        exif = Image.Exif()
        exif[0x010F] = "TestCamera"  # Make
        exif[0x0112] = 6  # Orientation: rotate 90 -- a portrait phone photo
        source = BytesIO()
        Image.new("RGB", (40, 20), "red").save(source, "JPEG", exif=exif)

        status, payload = self._post_report(self.REPORT, [source.getvalue()])
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["barangay"], "Test West")
        with self.application.repository.connection() as connection:
            row = connection.execute(
                "SELECT * FROM citizen_reports WHERE id = ?", (payload["id"],)
            ).fetchone()
        self.assertEqual(row["reporter_phone"], "09171234567")
        self.assertEqual(row["status"], "new")
        self.assertTrue(row["created_at"].endswith("+00:00"))
        [filename] = json.loads(row["photos_json"])
        stored = self.server.admin_dashboard.uploads_root / "citizen-reports" / filename
        with Image.open(stored) as image:
            self.assertEqual(len(image.getexif()), 0)
            self.assertEqual(image.size, (20, 40))

    def test_citizen_report_rejections(self) -> None:
        for fields, status, code in (
            ({**self.REPORT, "latitude": "13.5"}, 422, "outside_basey"),
            ({**self.REPORT, "reporter_phone": "call me"}, 400, "invalid_phone"),
            ({**self.REPORT, "consent": ""}, 400, "consent_required"),
            ({**self.REPORT, "severity": "apocalyptic"}, 400, "invalid_severity"),
        ):
            with self.subTest(code=code):
                actual_status, payload = self._post_report(fields)
                self.assertEqual(actual_status, status)
                self.assertEqual(payload["error"]["code"], code)
        status, payload = self._post_report(self.REPORT, [b"not an image"])
        self.assertEqual((status, payload["error"]["code"]), (400, "invalid_photo"))
        with self.assertRaises(HTTPError) as caught:
            urlopen(f"{self.base_url}/api/v1/reports", timeout=5)
        self.assertEqual(caught.exception.code, 405)

    def test_report_rate_limit_has_its_own_bucket(self) -> None:
        client = "203.0.113.78"
        for _ in range(self.server.report_rate_limit):
            self.assertTrue(self.server.check_rate_limit(client, "report")[0])
        self.assertFalse(self.server.check_rate_limit(client, "report")[0])
        self.assertTrue(self.server.check_rate_limit(client, "api")[0])

    def test_vercel_entry_point_refuses_reports(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "vercel_index", WEB_ROOT.parent / "api" / "index.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        statuses: list[str] = []
        body = module.app(
            {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/v1/reports"},
            lambda status, headers: statuses.append(status),
        )
        self.assertEqual(statuses, ["503 Service Unavailable"])
        self.assertEqual(json.loads(b"".join(body))["error"]["code"], "report_intake_unavailable")


if __name__ == "__main__":
    unittest.main()
