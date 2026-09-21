"""Read-only operational summaries for the isolated Basafe admin prototype."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .repository import Repository
from .service import GeoSafeService


REQUIRED_HAZARDS = ("flood", "liquefaction", "ground_shaking")
VISITOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
PHOTO_FILE_PATTERN = re.compile(r"^[a-f0-9]{32}\.(?:jpg|png|webp)$")
MAX_PHOTO_BYTES = 5 * 1024 * 1024


class AdminOperationError(ValueError):
    """A safe local administration request could not be completed."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _integer(value: Any) -> int:
    return int(value or 0)


class AdminDashboard:
    """Build authenticated, aggregate-only monitoring payloads."""

    def __init__(
        self,
        repository: Repository,
        service: GeoSafeService,
        uploads_root: str | Path | None = None,
    ):
        self.repository = repository
        self.service = service
        self.uploads_root = Path(
            uploads_root or repository.database_path.parent / "admin-uploads"
        ).resolve()

    def record_visitor(self, visitor_id: str, path: str) -> None:
        """Record an anonymous page view without retaining an IP address."""
        if not VISITOR_ID_PATTERN.fullmatch(visitor_id):
            return
        normalized_path = path[:160] if path.startswith("/") else "/"
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO visitor_sessions (
                    visitor_id, first_seen, last_seen, last_path, page_views
                ) VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, 1)
                ON CONFLICT(visitor_id) DO UPDATE SET
                    last_seen = CURRENT_TIMESTAMP,
                    last_path = excluded.last_path,
                    page_views = visitor_sessions.page_views + 1
                """,
                (visitor_id, normalized_path),
            )
            connection.execute(
                """
                INSERT INTO visitor_events (visitor_id, event_type, path)
                VALUES (?, 'page_view', ?)
                """,
                (visitor_id, normalized_path),
            )
            connection.commit()

    def analytics(self, days: int = 7) -> dict[str, Any]:
        """Return privacy-preserving visitor totals and a daily time series."""
        days = min(30, max(7, int(days)))
        today = datetime.now(timezone.utc).date()
        first_day = today - timedelta(days=days - 1)
        with self.repository.connection() as connection:
            summary = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM visitor_sessions) AS unique_visitors,
                    (SELECT COUNT(*) FROM visitor_sessions
                     WHERE last_seen >= datetime('now', '-5 minutes')) AS online_now,
                    (SELECT COUNT(*) FROM visitor_sessions
                     WHERE date(first_seen) = date('now')) AS new_today,
                    (SELECT COUNT(*) FROM visitor_events
                     WHERE date(occurred_at) = date('now')) AS page_views_today
                """
            ).fetchone()
            daily_rows = connection.execute(
                """
                SELECT date(occurred_at) AS day,
                       COUNT(*) AS page_views,
                       COUNT(DISTINCT visitor_id) AS unique_visitors
                FROM visitor_events
                WHERE date(occurred_at) >= date(?)
                GROUP BY date(occurred_at)
                ORDER BY day
                """,
                (first_day.isoformat(),),
            ).fetchall()
            top_paths = connection.execute(
                """
                SELECT path, COUNT(*) AS page_views,
                       COUNT(DISTINCT visitor_id) AS unique_visitors
                FROM visitor_events
                WHERE date(occurred_at) >= date(?)
                GROUP BY path
                ORDER BY page_views DESC, path
                LIMIT 8
                """,
                (first_day.isoformat(),),
            ).fetchall()

        values_by_day = {row["day"]: row for row in daily_rows}
        series = []
        for offset in range(days):
            day = first_day + timedelta(days=offset)
            row = values_by_day.get(day.isoformat())
            series.append(
                {
                    "date": day.isoformat(),
                    "page_views": _integer(row["page_views"]) if row else 0,
                    "unique_visitors": (
                        _integer(row["unique_visitors"]) if row else 0
                    ),
                }
            )
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "definition": {
                "unique_visitors": "Anonymous browser identifiers; not registered accounts.",
                "online_now": "Anonymous visitors active within the last five minutes.",
                "privacy": "Raw IP addresses are not stored by this feature.",
            },
            "summary": {
                "unique_visitors": _integer(summary["unique_visitors"]),
                "online_now": _integer(summary["online_now"]),
                "new_today": _integer(summary["new_today"]),
                "page_views_today": _integer(summary["page_views_today"]),
            },
            "days": days,
            "series": series,
            "top_paths": [dict(row) for row in top_paths],
        }

    @staticmethod
    def _photo_format(content: bytes) -> tuple[str, str]:
        if content.startswith(b"\xff\xd8\xff"):
            return "jpg", "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png", "image/png"
        if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "webp", "image/webp"
        raise AdminOperationError(
            "unsupported_photo_type",
            "Upload a JPEG, PNG, or WebP image.",
        )

    def upload_evacuation_center_photo(
        self,
        center_id: int,
        content: bytes,
        *,
        alt_text: str | None,
        source: str | None,
        source_url: str | None,
        actor: str,
    ) -> dict[str, Any]:
        if not content:
            raise AdminOperationError("photo_required", "Choose an image to upload.")
        if len(content) > MAX_PHOTO_BYTES:
            raise AdminOperationError(
                "photo_too_large", "The image must be 5 MB or smaller.", status=413
            )
        extension, mime_type = self._photo_format(content)
        normalized_source_url = (source_url or "").strip() or None
        if normalized_source_url:
            parsed = urlparse(normalized_source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise AdminOperationError(
                    "invalid_photo_source_url",
                    "The photo source URL must use HTTP or HTTPS.",
                )
        with self.repository.connection() as connection:
            center = connection.execute(
                "SELECT id, name, active FROM evacuation_centers WHERE id = ?",
                (center_id,),
            ).fetchone()
        if center is None:
            raise AdminOperationError(
                "center_not_found", "The evacuation center was not found.", status=404
            )
        if not bool(center["active"]):
            raise AdminOperationError(
                "center_inactive",
                "Photographs can be uploaded only for active evacuation centers.",
            )

        filename = f"{secrets.token_hex(16)}.{extension}"
        center_directory = (self.uploads_root / "evacuation-centers" / str(center_id)).resolve()
        try:
            center_directory.relative_to(self.uploads_root)
        except ValueError as exc:
            raise AdminOperationError(
                "invalid_upload_path", "The upload location is invalid.", status=500
            ) from exc
        center_directory.mkdir(parents=True, exist_ok=True)
        photo_path = center_directory / filename
        with photo_path.open("xb") as handle:
            handle.write(content)

        photo_url = f"/api/v1/admin/evacuation-centers/{center_id}/photo/{filename}"
        normalized_alt = (alt_text or "").strip() or f"Photograph of {center['name']}"
        normalized_source = (source or "").strip() or "Source not reported"
        try:
            with self.repository.connection() as connection:
                connection.execute(
                    """
                    UPDATE evacuation_centers
                    SET photo_url = ?, photo_alt = ?, photo_source = ?,
                        photo_source_url = ?
                    WHERE id = ?
                    """,
                    (
                        photo_url,
                        normalized_alt,
                        normalized_source,
                        normalized_source_url,
                        center_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO admin_audit_log (
                        actor, action, entity_type, entity_id, details_json
                    ) VALUES (?, 'upload_photo', 'evacuation_center', ?, ?)
                    """,
                    (
                        actor,
                        str(center_id),
                        json.dumps(
                            {
                                "file_name": filename,
                                "mime_type": mime_type,
                                "size_bytes": len(content),
                                "source": normalized_source,
                            },
                            separators=(",", ":"),
                        ),
                    ),
                )
                connection.commit()
        except Exception:
            photo_path.unlink(missing_ok=True)
            raise
        return {
            "status": "uploaded",
            "center_id": center_id,
            "center_name": center["name"],
            "photo_url": photo_url,
            "photo_alt": normalized_alt,
            "photo_source": normalized_source,
            "photo_source_url": normalized_source_url,
            "mime_type": mime_type,
            "size_bytes": len(content),
        }

    def evacuation_center_photo(
        self, center_id: int, filename: str
    ) -> tuple[bytes, str]:
        if not PHOTO_FILE_PATTERN.fullmatch(filename):
            raise AdminOperationError(
                "photo_not_found", "The facility photograph was not found.", status=404
            )
        expected_url = f"/api/v1/admin/evacuation-centers/{center_id}/photo/{filename}"
        with self.repository.connection() as connection:
            row = connection.execute(
                "SELECT photo_url FROM evacuation_centers WHERE id = ? AND active = 1",
                (center_id,),
            ).fetchone()
        if row is None or row["photo_url"] != expected_url:
            raise AdminOperationError(
                "photo_not_found", "The facility photograph was not found.", status=404
            )
        path = (self.uploads_root / "evacuation-centers" / str(center_id) / filename).resolve()
        try:
            path.relative_to(self.uploads_root)
        except ValueError as exc:
            raise AdminOperationError(
                "photo_not_found", "The facility photograph was not found.", status=404
            ) from exc
        if not path.is_file():
            raise AdminOperationError(
                "photo_not_found", "The facility photograph was not found.", status=404
            )
        mime_types = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
        return path.read_bytes(), mime_types[path.suffix.lstrip(".")]

    def overview(self) -> dict[str, Any]:
        with self.repository.connection() as connection:
            datasets = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT d.id, d.slug, d.name, d.hazard_type, d.source_name,
                           d.source_date, d.quality_status, d.is_official,
                           d.imported_at, COUNT(f.id) AS feature_count
                    FROM hazard_datasets d
                    LEFT JOIN hazard_features f ON f.dataset_id = d.id
                    GROUP BY d.id
                    ORDER BY CASE d.hazard_type
                        WHEN 'flood' THEN 1
                        WHEN 'liquefaction' THEN 2
                        WHEN 'ground_shaking' THEN 3
                        ELSE 4 END,
                        d.imported_at DESC
                    """
                ).fetchall()
            ]
            scoring = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'complete' THEN 1 ELSE 0 END) AS complete,
                       SUM(CASE WHEN status = 'incomplete' THEN 1 ELSE 0 END) AS incomplete,
                       MAX(created_at) AS latest_created_at
                FROM assessments
                """
            ).fetchone()
            context = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM historical_incidents) AS incidents,
                    (SELECT COUNT(*) FROM historical_incidents WHERE is_official = 1)
                        AS official_incidents,
                    (SELECT COUNT(*) FROM clup_references) AS clup_references,
                    (SELECT COUNT(*) FROM clup_references WHERE is_official = 1)
                        AS official_clup_references,
                    (SELECT COUNT(*) FROM barangays) AS barangays,
                    (SELECT COUNT(*) FROM barangays WHERE is_official = 1)
                        AS official_barangays,
                    (SELECT COUNT(*) FROM municipal_boundary) AS boundaries,
                    (SELECT COUNT(*) FROM municipal_boundary WHERE is_official = 1)
                        AS official_boundaries
                """
            ).fetchone()

        centers = self.repository.evacuation_centers(active_only=False)
        active_centers = [center for center in centers if center["active"]]
        center_summary = {
            "total": len(centers),
            "active": len(active_centers),
            "inactive": len(centers) - len(active_centers),
            "official": sum(
                1 for center in active_centers if center["is_official"]
            ),
            "with_capacity": sum(
                1 for center in active_centers if center.get("capacity") is not None
            ),
            "with_photos": sum(
                1 for center in active_centers if center.get("photo_url")
            ),
            "items": [
                {
                    "id": center["id"],
                    "name": center["name"],
                    "barangay": center.get("barangay"),
                    "active": center["active"],
                    "is_official": center["is_official"],
                    "capacity": center.get("capacity"),
                    "has_photo": bool(center.get("photo_url")),
                    "photo_url": center.get("photo_url"),
                    "photo_alt": center.get("photo_alt"),
                    "photo_source": center.get("photo_source"),
                    "photo_source_url": center.get("photo_source_url"),
                    "dataset_version": center.get("dataset_version"),
                }
                for center in active_centers
            ],
        }

        routing = self.service.routing_status()
        recent_scores = self.repository.assessments(limit=8, offset=0)
        dataset_items = [
            {
                "id": row["id"],
                "slug": row["slug"],
                "name": row["name"],
                "hazard_type": row["hazard_type"],
                "source_name": row["source_name"],
                "source_date": row["source_date"],
                "quality_status": row["quality_status"],
                "is_official": bool(row["is_official"]),
                "imported_at": row["imported_at"],
                "feature_count": _integer(row["feature_count"]),
            }
            for row in datasets
        ]

        present_hazards = {item["hazard_type"] for item in dataset_items}
        missing_hazards = [
            hazard for hazard in REQUIRED_HAZARDS if hazard not in present_hazards
        ]
        alerts: list[dict[str, str]] = []
        if missing_hazards:
            alerts.append(
                {
                    "severity": "critical",
                    "title": "Required hazard data is missing",
                    "message": ", ".join(
                        hazard.replace("_", " ").title() for hazard in missing_hazards
                    ),
                    "action": "Load and validate the missing dataset before public scoring.",
                }
            )
        limited_datasets = [
            item for item in dataset_items if item["quality_status"] != "verified"
        ]
        if limited_datasets:
            alerts.append(
                {
                    "severity": "warning",
                    "title": "Dataset documentation needs review",
                    "message": (
                        f"{len(limited_datasets)} active hazard dataset(s) are not marked "
                        "verified in the local inventory."
                    ),
                    "action": "Review source dates, coverage, and validation records.",
                }
            )
        if not routing.get("routing_available"):
            alerts.append(
                {
                    "severity": "critical",
                    "title": "Evacuation routing is unavailable",
                    "message": "One or more local routing dependencies are unavailable.",
                    "action": "Inspect the routing configuration, graph, and center inventory.",
                }
            )
        study_area = routing.get("study_area") or {}
        if study_area and not study_area.get("is_official"):
            alerts.append(
                {
                    "severity": "warning",
                    "title": "Routing boundary is non-authoritative",
                    "message": "The town-proper routing extent is a researcher-defined composite.",
                    "action": "Replace it with an independently issued boundary before operational use.",
                }
            )
        if active_centers and center_summary["with_photos"] < len(active_centers):
            alerts.append(
                {
                    "severity": "info",
                    "title": "Evacuation-center photographs are incomplete",
                    "message": (
                        f"{len(active_centers) - center_summary['with_photos']} active center(s) "
                        "do not yet have a verified facility photograph."
                    ),
                    "action": "Add permission-cleared photographs and attribution when available.",
                }
            )
        incomplete_scores = _integer(scoring["incomplete"])
        if incomplete_scores:
            alerts.append(
                {
                    "severity": "info",
                    "title": "Some scores are incomplete",
                    "message": f"{incomplete_scores} saved score(s) lack all required hazard inputs.",
                    "action": "Use the score history to identify recurring data-coverage gaps.",
                }
            )
        if not alerts:
            alerts.append(
                {
                    "severity": "healthy",
                    "title": "No immediate monitoring issues",
                    "message": "All required local monitoring checks currently pass.",
                    "action": "Continue routine source and backup review.",
                }
            )

        critical_count = sum(
            1 for item in alerts if item["severity"] == "critical"
        )
        warning_count = sum(
            1 for item in alerts if item["severity"] == "warning"
        )
        status = "critical" if critical_count else "attention" if warning_count else "healthy"
        database_stat = self.repository.database_path.stat()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "name": "Isolated admin development",
                "database_file": self.repository.database_path.name,
                "database_size_bytes": database_stat.st_size,
                "database_modified_at": datetime.fromtimestamp(
                    database_stat.st_mtime, tz=timezone.utc
                ).isoformat(),
                "runtime_data_mode": self.service.runtime_data_mode,
                "model_version": self.service.model.version,
                "monitoring_read_only": True,
                "photo_uploads_enabled": True,
                "allowed_admin_writes": ["evacuation_center_photos"],
                "production_connected": False,
            },
            "system": {
                "status": status,
                "critical_alerts": critical_count,
                "warnings": warning_count,
                "required_hazards_loaded": len(REQUIRED_HAZARDS) - len(missing_hazards),
                "required_hazards_total": len(REQUIRED_HAZARDS),
                "routing_available": bool(routing.get("routing_available")),
            },
            "datasets": dataset_items,
            "routing": routing,
            "evacuation_centers": center_summary,
            "context": {
                "historical_incidents": _integer(context["incidents"]),
                "official_historical_incidents": _integer(
                    context["official_incidents"]
                ),
                "clup_references": _integer(context["clup_references"]),
                "official_clup_references": _integer(
                    context["official_clup_references"]
                ),
                "barangays": _integer(context["barangays"]),
                "official_barangays": _integer(context["official_barangays"]),
                "municipal_boundaries": _integer(context["boundaries"]),
                "official_municipal_boundaries": _integer(
                    context["official_boundaries"]
                ),
            },
            "scoring": {
                "total": _integer(scoring["total"]),
                "complete": _integer(scoring["complete"]),
                "incomplete": incomplete_scores,
                "latest_created_at": scoring["latest_created_at"],
                "recent": recent_scores["items"],
            },
            "alerts": alerts,
            "analytics": self.analytics()["summary"],
        }
