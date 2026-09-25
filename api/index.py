"""
Vercel WSGI entry point for Basafe.

Vercel calls `app(environ, start_response)` for every request routed to
this function.  Static files (everything under /web) are served directly
by Vercel's CDN via the routes defined in vercel.json – this handler only
sees /api/* requests.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import parse_qs

# ---------------------------------------------------------------------------
# Bootstrap – make the project root importable
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Lazy-initialise the GeoSafe service (cold-start once per instance)
# ---------------------------------------------------------------------------
_api = None
_api_lock = threading.Lock()
LOGGER = logging.getLogger(__name__)


def _runtime_database_path() -> Path:
    """Return writable, instance-local storage for the bundled snapshot."""

    configured_runtime = os.environ.get("GEOSAFE_RUNTIME_DIR")
    if configured_runtime:
        runtime_root = Path(configured_runtime)
    elif os.environ.get("VERCEL"):
        runtime_root = Path("/tmp")
    else:
        runtime_root = Path(tempfile.gettempdir())
    return runtime_root / "geosafe-fis" / "geosafe.db"


def _prepare_runtime_database() -> Path:
    """Copy the sanitized, read-only deployment snapshot into writable storage."""

    source = PROJECT_ROOT / "data" / "geosafe.snapshot.db"
    if not source.is_file():
        raise RuntimeError(
            "The sanitized deployment snapshot is missing: "
            "data/geosafe.snapshot.db"
        )

    destination = _runtime_database_path()
    if destination.exists():
        return destination

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".copying")
    shutil.copy2(source, temporary)
    temporary.replace(destination)
    return destination


def _get_api():
    global _api
    if _api is not None:
        return _api

    with _api_lock:
        if _api is not None:
            return _api

        from geosafe.server import create_application

        runtime_database = _prepare_runtime_database()
        _api, _, _ = create_application(
            database_path=runtime_database,
            model_path=PROJECT_ROOT / "config" / "fuzzy_model.json",
            schema_path=PROJECT_ROOT / "db" / "schema.sql",
            enable_ulap=True,
            runtime_data_mode="snapshot",
        )
        return _api


# ---------------------------------------------------------------------------
# WSGI application
# ---------------------------------------------------------------------------
def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    query_string = environ.get("QUERY_STRING", "")
    query = parse_qs(query_string, keep_blank_values=True)

    # Storage here is an ephemeral per-instance /tmp copy, so a report would
    # be silently lost; refuse it before reading the (possibly large) body.
    if path.rstrip("/") == "/api/v1/reports":
        payload = json.dumps(
            {
                "error": {
                    "code": "report_intake_unavailable",
                    "message": (
                        "Damage reports can't be received on this site yet. "
                        "If anyone is in danger, call 911 or the Basey MDRRMO."
                    ),
                }
            }
        ).encode()
        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(payload))),
            ],
        )
        return [payload]

    # Read request body for POST
    body = b""
    if method in {"POST", "PUT", "PATCH"}:
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except (ValueError, TypeError):
            length = 0
        if length > 0:
            body = environ["wsgi.input"].read(length)

    try:
        api = _get_api()
        response = api.dispatch(method, path, query, body)
    except Exception:
        LOGGER.exception("Unhandled Vercel function error")
        payload = json.dumps(
            {
                "error": {
                    "code": "internal_error",
                    "message": "The Basafe API could not process the request.",
                }
            }
        ).encode()
        start_response(
            "500 Internal Server Error",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Content-Length", str(len(payload))),
            ],
        )
        return [payload]

    status_map = {
        200: "200 OK",
        201: "201 Created",
        204: "204 No Content",
        400: "400 Bad Request",
        401: "401 Unauthorized",
        403: "403 Forbidden",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        409: "409 Conflict",
        411: "411 Length Required",
        413: "413 Payload Too Large",
        422: "422 Unprocessable Entity",
        429: "429 Too Many Requests",
        500: "500 Internal Server Error",
        503: "503 Service Unavailable",
    }
    status_line = status_map.get(response.status, f"{response.status} Unknown")

    headers = [
        # CORS – allow any origin for the public API
        ("Access-Control-Allow-Origin", "*"),
        ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
        ("Access-Control-Allow-Headers", "Content-Type"),
        # Security
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
    ]
    for k, v in response.headers.items():
        headers.append((k, v))

    start_response(status_line, headers)
    return [response.body] if response.body else [b""]
