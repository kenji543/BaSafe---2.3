"""Development/deployment HTTP server for the unified Basafe interface."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
from collections import defaultdict, deque
from email import policy
from email.parser import BytesParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .admin import AdminDashboard, AdminOperationError
from .api import Api, Response
from .fuzzy import FuzzyModel
from .repository import Repository
from .routing import HazardAwareRouter, RoutingConfig
from .service import GeoSafeService
from .ulap.integration import UlapIntegration


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGGER = logging.getLogger("geosafe")
MAX_REQUEST_BYTES = 1_048_576
ADMIN_MAX_REQUEST_BYTES = 6 * 1_048_576
RATE_LIMIT_WINDOW_SECONDS = 60
PUBLIC_PAGE_PATHS = {
    "/",
    "/map",
    "/methodology",
    "/data-sources",
    "/limitations",
    "/about",
    "/privacy",
    "/offline",
    "/report-damage",
}


def _environment_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false.")


def _environment_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive.")
    return value


class GeoSafeServer(ThreadingHTTPServer):
    """HTTP server carrying application and static-root references."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        api: Api,
        web_root: Path,
    ):
        self.api = api
        self.web_root = web_root.resolve()
        self.api_rate_limit = _environment_positive_int(
            "GEOSAFE_API_REQUESTS_PER_MINUTE", 240
        )
        self.assessment_rate_limit = _environment_positive_int(
            "GEOSAFE_ASSESSMENTS_PER_MINUTE", 12
        )
        # Mobile carriers share one IP across many users (CGNAT), so keep this
        # generous enough for a crowded evacuation site.
        self.report_rate_limit = _environment_positive_int(
            "GEOSAFE_REPORTS_PER_MINUTE", 10
        )
        self.admin_enabled = _environment_flag(
            "GEOSAFE_ADMIN_ENABLED", default=False
        )
        self.admin_username = os.environ.get("GEOSAFE_ADMIN_USERNAME", "")
        self.admin_password = os.environ.get("GEOSAFE_ADMIN_PASSWORD", "")
        self.admin_login_rate_limit = _environment_positive_int(
            "GEOSAFE_ADMIN_LOGIN_ATTEMPTS_PER_MINUTE", 10
        )
        self.admin_session_ttl_seconds = 60 * _environment_positive_int(
            "GEOSAFE_ADMIN_SESSION_MINUTES", 480
        )
        self.visitor_analytics_enabled = _environment_flag(
            "GEOSAFE_VISITOR_ANALYTICS_ENABLED", default=False
        )
        if self.admin_enabled and not (
            self.admin_username and self.admin_password
        ):
            raise ValueError(
                "GEOSAFE_ADMIN_USERNAME and GEOSAFE_ADMIN_PASSWORD are required "
                "when GEOSAFE_ADMIN_ENABLED is true."
            )
        self.admin_dashboard = AdminDashboard(
            api.service.repository,
            api.service,
            uploads_root=os.environ.get("GEOSAFE_ADMIN_UPLOAD_ROOT"),
        )
        self._rate_limit_lock = threading.Lock()
        self._rate_limit_windows: dict[
            tuple[str, str], deque[float]
        ] = defaultdict(deque)
        self._admin_session_lock = threading.Lock()
        self._admin_sessions: dict[str, float] = {}
        super().__init__(server_address, GeoSafeRequestHandler)

    def create_admin_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._admin_session_lock:
            self._admin_sessions = {
                key: expiry
                for key, expiry in self._admin_sessions.items()
                if expiry > now
            }
            self._admin_sessions[token] = now + self.admin_session_ttl_seconds
        return token

    def validate_admin_session(self, token: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
            return False
        now = time.monotonic()
        with self._admin_session_lock:
            expiry = self._admin_sessions.get(token, 0)
            if expiry <= now:
                self._admin_sessions.pop(token, None)
                return False
            self._admin_sessions[token] = now + self.admin_session_ttl_seconds
        return True

    def revoke_admin_session(self, token: str) -> None:
        with self._admin_session_lock:
            self._admin_sessions.pop(token, None)

    def check_rate_limit(
        self, client_ip: str, bucket: str
    ) -> tuple[bool, int, int, int]:
        if bucket == "assessment":
            limit = self.assessment_rate_limit
        elif bucket == "admin_login":
            limit = self.admin_login_rate_limit
        elif bucket == "report":
            limit = self.report_rate_limit
        else:
            limit = self.api_rate_limit
        now = time.monotonic()
        cutoff = now - RATE_LIMIT_WINDOW_SECONDS
        key = (client_ip, bucket)
        with self._rate_limit_lock:
            requests = self._rate_limit_windows[key]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= limit:
                retry_after = max(1, int(RATE_LIMIT_WINDOW_SECONDS - (now - requests[0])) + 1)
                return False, limit, 0, retry_after
            requests.append(now)
            return True, limit, max(0, limit - len(requests)), 0

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            LOGGER.debug("Client disconnected before the response completed")
            return
        super().handle_error(request, client_address)


class GeoSafeRequestHandler(BaseHTTPRequestHandler):
    server: GeoSafeServer
    protocol_version = "HTTP/1.1"

    def _send(self, response: Response, head_only: bool = False) -> None:
        self.send_response(response.status)
        headers = {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Permissions-Policy": "geolocation=(self)",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "script-src 'self' https://unpkg.com; "
                "style-src 'self' 'unsafe-inline' https://unpkg.com; "
                "img-src 'self' data: blob: https://*.tile.openstreetmap.org "
                "https://server.arcgisonline.com https://services.arcgisonline.com "
                "https://ulap-hazards.georisk.gov.ph; "
                "connect-src 'self' https://*.tile.openstreetmap.org "
                "https://server.arcgisonline.com https://services.arcgisonline.com; "
                "font-src 'self'; object-src 'none'; base-uri 'self'; "
                "frame-ancestors 'none'; worker-src 'self'; manifest-src 'self'"
            ),
            **response.headers,
        }
        visitor_cookie = getattr(self, "_visitor_cookie", None)
        if visitor_cookie and "Set-Cookie" not in headers:
            headers["Set-Cookie"] = visitor_cookie
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only and response.body:
            self.wfile.write(response.body)

    def _is_admin_path(self) -> bool:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        return path == "/admin" or path.startswith("/admin/") or path.startswith(
            "/api/v1/admin/"
        )

    def _admin_authorized(self) -> bool:
        token = self._admin_session_token()
        return bool(token and self.server.validate_admin_session(token))

    def _admin_session_token(self) -> str:
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        session = cookies.get("basafe_admin_session")
        return session.value if session else ""

    def _admin_session_cookie(self, token: str = "", *, clear: bool = False) -> str:
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        max_age = 0 if clear else self.server.admin_session_ttl_seconds
        return (
            f"basafe_admin_session={token}; Path=/; Max-Age={max_age};"
            f" HttpOnly; SameSite=Strict;{secure}"
        )

    def _guard_admin(self) -> bool:
        if not self._is_admin_path():
            return True
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if not self.server.admin_enabled:
            self._send(
                Response.json(
                    {"error": {"code": "not_found", "message": "Page not found."}},
                    status=404,
                )
            )
            return False
        if path in {
            "/admin/login",
            "/admin/admin.css",
            "/admin/login.js",
            "/api/v1/admin/login",
        }:
            return True
        if self._admin_authorized():
            return True
        if path.startswith("/admin"):
            self._send(
                Response(
                    302,
                    b"",
                    {
                        "Location": "/admin/login",
                        "Content-Length": "0",
                        "Cache-Control": "no-store",
                    },
                )
            )
            return False
        self._send(
            Response.json(
                {
                    "error": {
                        "code": "admin_authentication_required",
                        "message": "Administrator authentication is required.",
                    }
                },
                status=401,
                headers={"Cache-Control": "no-store"},
            )
        )
        return False

    def _admin_login_response(self, method: str) -> Response:
        if method != "POST":
            return Response.json(
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use POST to sign in.",
                    }
                },
                status=405,
                headers={"Allow": "POST"},
            )
        allowed, limit, remaining, retry_after = self.server.check_rate_limit(
            self.client_address[0], "admin_login"
        )
        if not allowed:
            return Response.json(
                {
                    "error": {
                        "code": "too_many_login_attempts",
                        "message": "Too many sign-in attempts. Wait before trying again.",
                    }
                },
                status=429,
                headers={
                    "Retry-After": str(retry_after),
                    "RateLimit-Limit": str(limit),
                    "RateLimit-Remaining": str(remaining),
                },
            )
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "0")
        except ValueError:
            content_length = -1
        if content_length <= 0 or content_length > 8_192:
            return Response.json(
                {
                    "error": {
                        "code": "invalid_login_request",
                        "message": "Enter an administrator username and password.",
                    }
                },
                status=400,
            )
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response.json(
                {
                    "error": {
                        "code": "invalid_login_request",
                        "message": "The sign-in request could not be read.",
                    }
                },
                status=400,
            )
        username = str(payload.get("username", "")) if isinstance(payload, dict) else ""
        password = str(payload.get("password", "")) if isinstance(payload, dict) else ""
        username_matches = hmac.compare_digest(username, self.server.admin_username)
        password_matches = hmac.compare_digest(password, self.server.admin_password)
        if not (username_matches and password_matches):
            return Response.json(
                {
                    "error": {
                        "code": "invalid_credentials",
                        "message": "The username or password is incorrect.",
                    }
                },
                status=401,
            )
        token = self.server.create_admin_session()
        return Response.json(
            {"authenticated": True, "redirect": "/admin"},
            headers={"Set-Cookie": self._admin_session_cookie(token)},
        )

    def _admin_logout_response(self, method: str) -> Response:
        if method != "POST":
            return Response.json(
                {
                    "error": {
                        "code": "method_not_allowed",
                        "message": "Use POST to sign out.",
                    }
                },
                status=405,
                headers={"Allow": "POST"},
            )
        token = self._admin_session_token()
        if token:
            self.server.revoke_admin_session(token)
        return Response.json(
            {"authenticated": False, "redirect": "/admin/login"},
            headers={"Set-Cookie": self._admin_session_cookie(clear=True)},
        )

    def _record_public_page_view(self) -> None:
        if not self.server.visitor_analytics_enabled:
            return
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if path not in PUBLIC_PAGE_PATHS:
            return
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except Exception:
            cookies = SimpleCookie()
        visitor_id = cookies.get("basafe_visitor_id")
        visitor_value = visitor_id.value if visitor_id else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", visitor_value):
            visitor_value = secrets.token_urlsafe(24)
            secure = " Secure;" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self._visitor_cookie = (
                f"basafe_visitor_id={visitor_value}; Path=/; Max-Age=31536000;"
                f" HttpOnly; SameSite=Lax;{secure}"
            )
        self.server.admin_dashboard.record_visitor(visitor_value, path)

    @staticmethod
    def _multipart_fields(
        content_type: str, body: bytes
    ) -> tuple[list[bytes], dict[str, str]]:
        if not content_type.casefold().startswith("multipart/form-data"):
            raise AdminOperationError(
                "multipart_required",
                "Send the form using multipart form data.",
            )
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("utf-8")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        if not message.is_multipart():
            raise AdminOperationError(
                "invalid_multipart", "The form could not be read."
            )
        photos: list[bytes] = []
        fields: dict[str, str] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            if name == "photo":
                if payload:
                    photos.append(payload)
            else:
                try:
                    fields[name] = payload.decode("utf-8")[:1000]
                except UnicodeDecodeError as exc:
                    raise AdminOperationError(
                        "invalid_form_text", "The form details must use UTF-8 text."
                    ) from exc
        return photos, fields

    @staticmethod
    def _admin_error_response(error: AdminOperationError) -> Response:
        return Response.json(
            {"error": {"code": error.code, "message": str(error)}},
            status=error.status,
        )

    @staticmethod
    def _json_body(body: bytes) -> dict:
        if not body:
            raise AdminOperationError("body_required", "A JSON request body is required.")
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AdminOperationError(
                "invalid_json", "Request body contains invalid JSON."
            ) from exc
        if not isinstance(payload, dict):
            raise AdminOperationError("invalid_json", "Request body must be a JSON object.")
        return payload

    def _api_response(self, method: str) -> Response:
        split = urlsplit(self.path)
        normalized_path = split.path.rstrip("/") or "/"
        if normalized_path == "/api/v1/admin/login":
            return self._admin_login_response(method)
        if normalized_path == "/api/v1/admin/logout":
            return self._admin_logout_response(method)
        if normalized_path == "/api/v1/admin/overview":
            if method != "GET":
                return Response.json(
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "The admin monitoring endpoint is read-only.",
                        }
                    },
                    status=405,
                    headers={"Allow": "GET"},
                )
            return Response.json(self.server.admin_dashboard.overview())
        if normalized_path == "/api/v1/admin/analytics":
            if method != "GET":
                return Response.json(
                    {"error": {"code": "method_not_allowed", "message": "Visitor analytics is read-only."}},
                    status=405,
                    headers={"Allow": "GET"},
                )
            query = parse_qs(split.query, keep_blank_values=True)
            try:
                days = int((query.get("days") or ["7"])[-1])
            except ValueError:
                days = 7
            return Response.json(self.server.admin_dashboard.analytics(days))
        if normalized_path == "/api/v1/admin/hazard-events":
            if method != "GET":
                return Response.json(
                    {"error": {"code": "method_not_allowed", "message": "The hazard-event log is read-only here."}},
                    status=405,
                    headers={"Allow": "GET"},
                )
            return Response.json(self.server.admin_dashboard.hazard_events())
        if normalized_path == "/api/v1/admin/evacuation-centers":
            if method == "GET":
                return Response.json(self.server.admin_dashboard.evacuation_centers_admin_list())
        if normalized_path == "/api/v1/admin/barangays":
            if method != "GET":
                return Response.json(
                    {"error": {"code": "method_not_allowed", "message": "Use GET to list barangays."}},
                    status=405,
                    headers={"Allow": "GET"},
                )
            return Response.json(self.server.admin_dashboard.barangays_admin_list())
        if normalized_path == "/api/v1/admin/reports":
            if method != "GET":
                return Response.json(
                    {"error": {"code": "method_not_allowed", "message": "Use GET to list damage reports."}},
                    status=405,
                    headers={"Allow": "GET"},
                )
            try:
                return Response.json(self.server.admin_dashboard.citizen_reports())
            except AdminOperationError as error:
                return self._admin_error_response(error)
        photo_file_match = re.fullmatch(
            r"/api/v1/admin/evacuation-centers/(\d+)/photo/([^/]+)",
            normalized_path,
        )
        report_photo_match = re.fullmatch(
            r"/api/v1/admin/reports/(\d+)/photos/([^/]+)", normalized_path
        )
        if photo_file_match or report_photo_match:
            if method != "GET":
                return Response.json(
                    {"error": {"code": "method_not_allowed", "message": "The photograph resource is read-only."}},
                    status=405,
                    headers={"Allow": "GET"},
                )
            try:
                if photo_file_match:
                    content, mime_type = self.server.admin_dashboard.evacuation_center_photo(
                        int(photo_file_match.group(1)), photo_file_match.group(2)
                    )
                else:
                    content, mime_type = self.server.admin_dashboard.citizen_report_photo(
                        int(report_photo_match.group(1)), report_photo_match.group(2)
                    )
            except AdminOperationError as error:
                return self._admin_error_response(error)
            return Response(
                200,
                content,
                {
                    "Content-Type": mime_type,
                    "Content-Length": str(len(content)),
                    "Cache-Control": "private, max-age=3600",
                },
            )
        photo_upload_match = re.fullmatch(
            r"/api/v1/admin/evacuation-centers/(\d+)/photo",
            normalized_path,
        )
        if photo_upload_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to upload a facility photograph."}},
                status=405,
                headers={"Allow": "POST"},
            )
        create_center_match = normalized_path == "/api/v1/admin/evacuation-centers" and method == "POST"
        edit_center_match = re.fullmatch(
            r"/api/v1/admin/evacuation-centers/(\d+)", normalized_path
        )
        if edit_center_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to edit an evacuation center."}},
                status=405,
                headers={"Allow": "POST"},
            )
        publish_center_match = re.fullmatch(
            r"/api/v1/admin/evacuation-centers/(\d+)/publish", normalized_path
        )
        if publish_center_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to publish an evacuation center."}},
                status=405,
                headers={"Allow": "POST"},
            )
        designate_match = re.fullmatch(
            r"/api/v1/admin/barangays/(\d+)/designate", normalized_path
        )
        if designate_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to designate a barangay's center."}},
                status=405,
                headers={"Allow": "POST"},
            )
        publish_designation_match = re.fullmatch(
            r"/api/v1/admin/barangays/(\d+)/publish", normalized_path
        )
        if publish_designation_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to publish a barangay's designation."}},
                status=405,
                headers={"Allow": "POST"},
            )
        report_status_match = re.fullmatch(
            r"/api/v1/admin/reports/(\d+)/status", normalized_path
        )
        if report_status_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to change a report's status."}},
                status=405,
                headers={"Allow": "POST"},
            )
        report_match = normalized_path == "/api/v1/reports"
        if report_match and self.server.admin_enabled:
            # The admin server runs on its own database, which the triage
            # page never reads -- a report accepted here would be lost.
            return Response.json(
                {
                    "error": {
                        "code": "report_intake_unavailable",
                        "message": "Damage reports can't be received here. Use the public app.",
                    }
                },
                status=503,
            )
        if report_match and method != "POST":
            return Response.json(
                {"error": {"code": "method_not_allowed", "message": "Use POST to send a damage report."}},
                status=405,
                headers={"Allow": "POST"},
            )
        if report_match:
            bucket = "report"
        elif method == "POST" and split.path.rstrip("/") in {
            "/api/assessments",
            "/api/v1/assessments",
            "/api/route",
            "/api/v1/route",
        }:
            bucket = "assessment"
        else:
            bucket = "api"
        allowed, limit, remaining, retry_after = self.server.check_rate_limit(
            self.client_address[0], bucket
        )
        if not allowed:
            return Response.json(
                {
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": "Too many requests. Wait before trying again.",
                    }
                },
                status=429,
                headers={
                    "Retry-After": str(retry_after),
                    "RateLimit-Limit": str(limit),
                    "RateLimit-Remaining": "0",
                },
            )
        body = b""
        if method in {"POST", "PUT", "PATCH"}:
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                return Response.json(
                    {
                        "error": {
                            "code": "length_required",
                            "message": "Content-Length is required.",
                        }
                    },
                    status=411,
                )
            try:
                content_length = int(raw_length)
            except ValueError:
                return Response.json(
                    {
                        "error": {
                            "code": "invalid_content_length",
                            "message": "Content-Length must be an integer.",
                        }
                    },
                    status=400,
                )
            is_upload = bool(photo_upload_match or report_match)
            request_limit = ADMIN_MAX_REQUEST_BYTES if is_upload else MAX_REQUEST_BYTES
            if content_length < 0 or content_length > request_limit:
                return Response.json(
                    {
                        "error": {
                            "code": "request_too_large",
                            "message": (
                                "The upload exceeds the 6 MiB request limit."
                                if is_upload
                                else "Request body exceeds the 1 MiB limit."
                            ),
                        }
                    },
                    status=413,
                )
            body = self.rfile.read(content_length)
        if report_match:
            try:
                photos, fields = self._multipart_fields(
                    self.headers.get("Content-Type", ""), body
                )
                return Response.json(
                    self.server.admin_dashboard.submit_citizen_report(fields, photos),
                    status=201,
                )
            except AdminOperationError as error:
                return self._admin_error_response(error)
        if photo_upload_match:
            try:
                photos, fields = self._multipart_fields(
                    self.headers.get("Content-Type", ""), body
                )
                payload = self.server.admin_dashboard.upload_evacuation_center_photo(
                    int(photo_upload_match.group(1)),
                    photos[-1] if photos else b"",
                    alt_text=fields.get("alt_text"),
                    source=fields.get("source"),
                    source_url=fields.get("source_url"),
                    actor=self.server.admin_username,
                )
                return Response.json(payload, status=201)
            except AdminOperationError as error:
                return self._admin_error_response(error)
        if create_center_match or edit_center_match or publish_center_match or designate_match or publish_designation_match or report_status_match:
            try:
                payload = self._json_body(body) if not (publish_center_match or publish_designation_match) else {}
            except AdminOperationError as error:
                return self._admin_error_response(error)
            actor = self.server.admin_username
            try:
                if create_center_match:
                    result = self.server.admin_dashboard.create_evacuation_center(
                        name=payload.get("name"),
                        latitude=payload.get("latitude"),
                        longitude=payload.get("longitude"),
                        notes=payload.get("notes"),
                        actor=actor,
                    )
                    return Response.json(result, status=201)
                if edit_center_match:
                    result = self.server.admin_dashboard.update_evacuation_center(
                        int(edit_center_match.group(1)),
                        name=payload.get("name"),
                        latitude=payload.get("latitude"),
                        longitude=payload.get("longitude"),
                        notes=payload.get("notes"),
                        actor=actor,
                    )
                    return Response.json(result)
                if publish_center_match:
                    result = self.server.admin_dashboard.publish_evacuation_center(
                        int(publish_center_match.group(1)), actor=actor
                    )
                    return Response.json(result)
                if designate_match:
                    center_id = payload.get("evacuation_center_id")
                    if not isinstance(center_id, int):
                        return self._admin_error_response(
                            AdminOperationError(
                                "evacuation_center_id_required",
                                "Provide the evacuation_center_id to assign.",
                            )
                        )
                    result = self.server.admin_dashboard.designate_barangay_center(
                        int(designate_match.group(1)), center_id, actor=actor
                    )
                    return Response.json({"items": result})
                if publish_designation_match:
                    result = self.server.admin_dashboard.publish_barangay_designation(
                        int(publish_designation_match.group(1)), actor=actor
                    )
                    return Response.json({"items": result})
                if report_status_match:
                    return Response.json(
                        self.server.admin_dashboard.set_citizen_report_status(
                            int(report_status_match.group(1)),
                            payload.get("status"),
                            actor=actor,
                        )
                    )
            except AdminOperationError as error:
                return self._admin_error_response(error)
        return self.server.api.dispatch(
            method,
            split.path,
            parse_qs(split.query, keep_blank_values=True),
            body,
        )

    def _serve_static(self, head_only: bool = False) -> None:
        split = urlsplit(self.path)
        route = split.path
        aliases = {
            "/": "index.html",
            "/admin/login": "admin/login.html",
            "/admin/login/": "admin/login.html",
            "/admin": "admin/index.html",
            "/admin/": "admin/index.html",
            "/admin/analytics": "admin/analytics.html",
            "/admin/analytics/": "admin/analytics.html",
            "/admin/datasets": "admin/datasets.html",
            "/admin/datasets/": "admin/datasets.html",
            "/admin/evacuation-centers": "admin/evacuation-centers.html",
            "/admin/evacuation-centers/": "admin/evacuation-centers.html",
            "/admin/hazard-events": "admin/hazard-events.html",
            "/admin/hazard-events/": "admin/hazard-events.html",
            "/admin/reports": "admin/reports.html",
            "/admin/reports/": "admin/reports.html",
            "/report-damage": "report-damage.html",
            "/report-damage/": "report-damage.html",
            "/map": "map.html",
            "/map/": "map.html",
            "/methodology": "methodology.html",
            "/methodology/": "methodology.html",
            "/data-sources": "info.html",
            "/data-sources/": "info.html",
            "/limitations": "info.html",
            "/limitations/": "info.html",
            "/about": "info.html",
            "/about/": "info.html",
            "/privacy": "info.html",
            "/privacy/": "info.html",
            "/offline": "info.html",
            "/offline/": "info.html",
        }
        relative = aliases.get(route, route.lstrip("/"))
        if re.fullmatch(r"/assessment/[A-Za-z0-9_-]{20,128}/?", route):
            relative = "map.html"
        if not relative or "\x00" in relative:
            self._send(
                Response.json(
                    {"error": {"code": "not_found", "message": "Page not found."}},
                    status=404,
                ),
                head_only,
            )
            return
        candidate = (self.server.web_root / relative).resolve()
        try:
            candidate.relative_to(self.server.web_root)
        except ValueError:
            self._send(
                Response.json(
                    {"error": {"code": "not_found", "message": "Page not found."}},
                    status=404,
                ),
                head_only,
            )
            return
        if not candidate.is_file():
            self._send(
                Response.json(
                    {"error": {"code": "not_found", "message": "Page not found."}},
                    status=404,
                ),
                head_only,
            )
            return
        content = candidate.read_bytes()
        content_type, _ = mimetypes.guess_type(candidate.name)
        if candidate.suffix == ".js":
            content_type = "text/javascript"
        elif candidate.suffix == ".webmanifest":
            content_type = "application/manifest+json"
        self._send(
            Response(
                200,
                content,
                {
                    "Content-Type": f"{content_type or 'application/octet-stream'}; charset=utf-8",
                    "Content-Length": str(len(content)),
                    "Cache-Control": (
                        "no-cache"
                        if candidate.suffix in {".html", ".js", ".css"}
                        else "public, max-age=3600"
                    ),
                },
            ),
            head_only,
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guard_admin():
            return
        if urlsplit(self.path).path.startswith("/api/"):
            try:
                self._send(self._api_response("GET"))
            except Exception:
                LOGGER.exception("Unhandled API error")
                self._send(
                    Response.json(
                        {
                            "error": {
                                "code": "internal_error",
                                "message": "The request could not be completed.",
                            }
                        },
                        status=500,
                    )
                )
        else:
            self._record_public_page_view()
            self._serve_static()

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guard_admin():
            return
        if urlsplit(self.path).path.startswith("/api/"):
            self._send(
                Response.json(
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "HEAD is not supported for API resources.",
                        }
                    },
                    status=405,
                    headers={"Allow": "GET, POST, OPTIONS"},
                ),
                head_only=True,
            )
        else:
            self._serve_static(head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guard_admin():
            return
        if not urlsplit(self.path).path.startswith("/api/"):
            self._send(
                Response.json(
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "POST is allowed only for approved API resources.",
                        }
                    },
                    status=405,
                    headers={"Allow": "GET, HEAD"},
                )
            )
            return
        try:
            self._send(self._api_response("POST"))
        except Exception:
            LOGGER.exception("Unhandled API error")
            self._send(
                Response.json(
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "The request could not be completed.",
                        }
                    },
                    status=500,
                )
            )

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._guard_admin():
            return
        self._send(self._api_response("OPTIONS"))

    def log_message(self, format_string: str, *args: object) -> None:
        # Routine per-request activity is deliberately not an application feature.
        # Debug output is available for local protocol troubleshooting only.
        LOGGER.debug("HTTP %s", format_string % args)


def create_application(
    database_path: str | Path | None = None,
    model_path: str | Path | None = None,
    schema_path: str | Path | None = None,
    *,
    enable_ulap: bool = True,
    allow_test_fixtures: bool = False,
    runtime_data_mode: str | None = None,
) -> tuple[Api, Repository, FuzzyModel]:
    configured_database = database_path or os.environ.get("GEOSAFE_DB_PATH")
    resolved_database = Path(configured_database or PROJECT_ROOT / "data" / "geosafe.db")
    if configured_database is None and not resolved_database.exists():
        bundled_snapshot = PROJECT_ROOT / "data" / "geosafe.snapshot.db"
        if bundled_snapshot.is_file():
            resolved_database.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundled_snapshot, resolved_database)

    model = FuzzyModel.from_file(
        model_path
        or os.environ.get("GEOSAFE_MODEL_PATH")
        or PROJECT_ROOT / "config" / "fuzzy_model.json"
    )
    repository = Repository(
        resolved_database,
        schema_path
        or os.environ.get("GEOSAFE_SCHEMA_PATH")
        or PROJECT_ROOT / "db" / "schema.sql",
    )
    repository.initialize(model)
    configured_routing = os.environ.get("GEOSAFE_ROUTING_CONFIG")
    routing_path = Path(configured_routing or PROJECT_ROOT / "config" / "routing.json")
    if not routing_path.is_absolute():
        routing_path = PROJECT_ROOT / routing_path
    routing_config = RoutingConfig.from_file(routing_path, PROJECT_ROOT)
    if routing_config.fuzzy_model_version != model.version:
        raise ValueError(
            "Routing configuration fuzzy_model_version does not match the active "
            f"model ({routing_config.fuzzy_model_version!r} != {model.version!r})."
        )
    router = HazardAwareRouter(repository, routing_config)
    ulap = None
    if enable_ulap:
        configured_registry = os.environ.get("ULAP_SERVICES_CONFIG")
        registry_path = None
        if configured_registry:
            candidate = Path(configured_registry)
            registry_path = (
                candidate
                if candidate.is_absolute()
                else PROJECT_ROOT / candidate
            )
        ulap = UlapIntegration.from_environment(registry_path)
    service = GeoSafeService(
        repository,
        model,
        ulap,
        allow_test_fixtures=allow_test_fixtures,
        runtime_data_mode=(
            runtime_data_mode
            or os.environ.get("GEOSAFE_RUNTIME_DATA_MODE", "snapshot")
        ).strip().casefold(),
        router=router,
    )
    if ulap is not None and _environment_flag(
        "ULAP_LIVE_VALIDATION", default=False
    ):
        results = ulap.validate_services()
        LOGGER.info(
            "ULAP startup validation: %s",
            ", ".join(
                f"{item['service']}={item['status']}" for item in results
            ),
        )
    return Api(service), repository, model


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the unified Basafe Web-GIS prototype."
    )
    parser.add_argument(
        "--host", default=os.environ.get("GEOSAFE_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("GEOSAFE_PORT", "8000")),
    )
    parser.add_argument(
        "--database", default=os.environ.get("GEOSAFE_DB_PATH")
    )
    parser.add_argument(
        "--web-root",
        default=os.environ.get("GEOSAFE_WEB_ROOT", str(PROJECT_ROOT / "web")),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.environ.get("GEOSAFE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    api, _, model = create_application(database_path=args.database)
    server = GeoSafeServer((args.host, args.port), api, Path(args.web_root))
    LOGGER.info(
        "Basafe %s listening at http://%s:%s "
        "(runtime data mode: %s; admin monitoring: %s)",
        model.version,
        args.host,
        args.port,
        api.service.runtime_data_mode,
        "enabled" if server.admin_enabled else "disabled",
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Stopping Basafe")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
