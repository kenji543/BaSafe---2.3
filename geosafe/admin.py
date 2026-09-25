"""Read-only operational summaries for the isolated Basafe admin prototype."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .geometry import point_in_geometry, validate_wgs84_point
from .repository import Repository
from .service import GeoSafeService, ServiceError


REQUIRED_HAZARDS = ("flood", "liquefaction", "ground_shaking")
VISITOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
PHOTO_FILE_PATTERN = re.compile(r"^[a-f0-9]{32}\.(?:jpg|png|webp)$")
MAX_PHOTO_BYTES = 5 * 1024 * 1024
DAMAGE_TYPES = (
    "flooding", "damaged_house_partial", "damaged_house_total", "landslide",
    "road_blocked", "fallen_tree_or_power_line", "injured_or_trapped", "other",
)
SEVERITIES = ("minor", "moderate", "severe", "life_threatening")
REPORT_STATUSES = ("new", "acknowledged", "resolved")
MAX_REPORT_PHOTOS = 3
PHONE_PATTERN = re.compile(r"^\+?\d{7,15}$")


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

    # -- Evacuation center editing and publishing ---------------------------
    #
    # Edits made through the methods below are written to whichever database
    # this AdminDashboard's own repository points at (admin-dev.db in the
    # normal local workflow) -- a draft, invisible to any public app until
    # explicitly published. See docs/admin-development.md's "Exception:
    # evacuation-center editing and publishing" section for the full picture,
    # including why this is the one dataset allowed to skip the usual
    # scripts/import_dataset.py cycle, and what Publish does and does not do.

    def _preferred_municipal_boundary(self) -> dict[str, Any] | None:
        boundaries = self.repository.municipal_boundaries()
        if not boundaries:
            return None
        return next((b for b in boundaries if b["is_official"]), boundaries[0])

    def _validate_center_fields(
        self, *, name: str, latitude: Any, longitude: Any, notes: str | None
    ) -> tuple[str, float, float, str | None]:
        normalized_name = (name or "").strip()
        if not normalized_name:
            raise AdminOperationError("name_required", "Enter a facility name.")
        if len(normalized_name) > 200:
            raise AdminOperationError(
                "name_too_long", "The facility name must be 200 characters or fewer."
            )
        try:
            checked_lat, checked_lon = validate_wgs84_point(latitude, longitude)
        except (TypeError, ValueError) as exc:
            raise AdminOperationError(
                "invalid_coordinates", "Enter a valid latitude and longitude."
            ) from exc
        boundary = self._preferred_municipal_boundary()
        if boundary and not point_in_geometry(checked_lat, checked_lon, boundary["geometry"]):
            raise AdminOperationError(
                "coordinates_outside_boundary",
                "Those coordinates fall outside the Basey municipal boundary.",
            )
        normalized_notes = (notes or "").strip() or None
        if normalized_notes and len(normalized_notes) > 2000:
            raise AdminOperationError(
                "description_too_long", "The description must be 2000 characters or fewer."
            )
        return normalized_name, checked_lat, checked_lon, normalized_notes

    def evacuation_centers_admin_list(self) -> dict[str, Any]:
        """Every center (active or not), with edit/publish bookkeeping, for
        populating admin edit forms and "assign an existing center"
        pickers. Local-only; never served to the public API."""
        centers = self.repository.evacuation_centers(active_only=False)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(centers),
            "items": centers,
        }

    def create_evacuation_center(
        self,
        *,
        name: str,
        latitude: Any,
        longitude: Any,
        notes: str | None,
        actor: str,
    ) -> dict[str, Any]:
        normalized_name, checked_lat, checked_lon, normalized_notes = (
            self._validate_center_fields(
                name=name, latitude=latitude, longitude=longitude, notes=notes
            )
        )
        external_id = f"admin-{secrets.token_hex(8)}"
        center_id = self.repository.create_evacuation_center(
            external_id=external_id,
            name=normalized_name,
            latitude=checked_lat,
            longitude=checked_lon,
            notes=normalized_notes,
            barangay=None,
            designation="Admin-entered facility",
            source_name=f"Basafe admin (operator: {actor})",
            actor=actor,
        )
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'create_center', 'evacuation_center', ?, ?)
                """,
                (
                    actor,
                    str(center_id),
                    json.dumps(
                        {"name": normalized_name, "latitude": checked_lat, "longitude": checked_lon},
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()
        return self.repository.evacuation_center(center_id)

    def update_evacuation_center(
        self,
        center_id: int,
        *,
        name: str,
        latitude: Any,
        longitude: Any,
        notes: str | None,
        actor: str,
    ) -> dict[str, Any]:
        existing = self.repository.evacuation_center(center_id)
        if existing is None:
            raise AdminOperationError(
                "center_not_found", "The evacuation center was not found.", status=404
            )
        normalized_name, checked_lat, checked_lon, normalized_notes = (
            self._validate_center_fields(
                name=name, latitude=latitude, longitude=longitude, notes=notes
            )
        )
        self.repository.update_evacuation_center(
            center_id,
            name=normalized_name,
            latitude=checked_lat,
            longitude=checked_lon,
            notes=normalized_notes,
            actor=actor,
        )
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'edit_center', 'evacuation_center', ?, ?)
                """,
                (
                    actor,
                    str(center_id),
                    json.dumps(
                        {
                            "before": {
                                "name": existing["name"],
                                "latitude": existing["latitude"],
                                "longitude": existing["longitude"],
                                "notes": existing["notes"],
                            },
                            "after": {
                                "name": normalized_name,
                                "latitude": checked_lat,
                                "longitude": checked_lon,
                                "notes": normalized_notes,
                            },
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()
        return self.repository.evacuation_center(center_id)

    def barangays_admin_list(self) -> dict[str, Any]:
        """All barangays with their designated center (if assigned), for the
        51-card admin view. Local-only; never served to the public API."""
        rows = self.repository.barangays_with_designations()
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(rows),
            "assigned_count": sum(1 for row in rows if row["designated_center"]),
            "items": rows,
        }

    def designate_barangay_center(
        self, barangay_id: int, evacuation_center_id: int, *, actor: str
    ) -> dict[str, Any]:
        barangay = self.repository.barangay(barangay_id)
        if barangay is None:
            raise AdminOperationError(
                "barangay_not_found", "The barangay was not found.", status=404
            )
        center = self.repository.evacuation_center(evacuation_center_id)
        if center is None:
            raise AdminOperationError(
                "center_not_found", "The evacuation center was not found.", status=404
            )
        self.repository.set_barangay_evacuation_center(
            barangay_id, evacuation_center_id, actor=actor
        )
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'designate_center', 'barangay', ?, ?)
                """,
                (
                    actor,
                    str(barangay_id),
                    json.dumps(
                        {"evacuation_center_id": evacuation_center_id, "center_name": center["name"]},
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()
        return self.repository.barangays_with_designations()

    def _publish_target_repository(self) -> Repository:
        target_path = Path(
            os.environ.get("GEOSAFE_PUBLISH_TARGET_DB")
            or (self.repository.database_path.parent / "geosafe.db")
        ).resolve()
        if not target_path.exists():
            raise AdminOperationError(
                "publish_target_missing",
                f"The public application database was not found at {target_path}. "
                "Start the local public app at least once, or set GEOSAFE_PUBLISH_TARGET_DB.",
                status=500,
            )
        if target_path == self.repository.database_path.resolve():
            raise AdminOperationError(
                "publish_target_is_draft_database",
                "The publish target must not be this admin session's own database.",
                status=500,
            )
        target = Repository(target_path, self.repository.schema_path)
        # The target's own server process applies these additive migrations
        # at its next startup, but publishing can't wait for that -- a
        # target database that predates this feature (or just hasn't been
        # restarted yet) would otherwise fail with "no column named
        # updated_at". Applying the same idempotent, additive migration here
        # is safe on a database another process has open (WAL mode).
        with target.connection() as connection:
            connection.executescript(target.schema_path.read_text(encoding="utf-8"))
            Repository._ensure_evacuation_center_publish_columns(connection)
            Repository._ensure_barangay_designation_columns(connection)
            connection.commit()
        return target

    def publish_evacuation_center(self, center_id: int, *, actor: str) -> dict[str, Any]:
        draft = self.repository.evacuation_center(center_id)
        if draft is None:
            raise AdminOperationError(
                "center_not_found", "The evacuation center was not found.", status=404
            )
        if not draft.get("external_id"):
            raise AdminOperationError(
                "center_missing_external_id",
                "This center predates admin publishing and has no stable external_id; "
                "re-import it through scripts/import_routing_data.py first.",
            )
        # Re-validate the draft against this process's own boundary data --
        # defense in depth, since the draft may have aged since it was saved.
        self._validate_center_fields(
            name=draft["name"], latitude=draft["latitude"], longitude=draft["longitude"],
            notes=draft["notes"],
        )
        target = self._publish_target_repository()
        published_id = target.upsert_published_evacuation_center(
            external_id=draft["external_id"],
            name=draft["name"],
            latitude=draft["latitude"],
            longitude=draft["longitude"],
            notes=draft["notes"],
            barangay=draft["barangay"],
            designation=draft["designation"],
            source_name=draft["source_name"],
            actor=actor,
            dataset_version=draft["dataset_version"],
            is_official=draft["is_official"],
        )
        target.mark_evacuation_center_published(published_id)
        self.repository.mark_evacuation_center_published(center_id)
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'publish_center', 'evacuation_center', ?, ?)
                """,
                (
                    actor,
                    str(center_id),
                    json.dumps(
                        {
                            "external_id": draft["external_id"],
                            "target_database": str(target.database_path),
                        },
                        separators=(",", ":"),
                    ),
                ),
            )
            connection.commit()
        return self.repository.evacuation_center(center_id)

    def publish_barangay_designation(self, barangay_id: int, *, actor: str) -> dict[str, Any]:
        barangay = self.repository.barangay(barangay_id)
        if barangay is None:
            raise AdminOperationError(
                "barangay_not_found", "The barangay was not found.", status=404
            )
        rows = {row["barangay_id"]: row for row in self.repository.barangays_with_designations()}
        designation = rows.get(barangay_id)
        center = designation["designated_center"] if designation else None
        if not center:
            raise AdminOperationError(
                "no_designation", "Assign a center to this barangay before publishing."
            )
        if center["published_at"] is None:
            raise AdminOperationError(
                "center_not_published",
                "Publish this barangay's assigned center itself before publishing the "
                "designation, so the target database has a matching center to point to.",
            )
        target = self._publish_target_repository()
        target_center = target.evacuation_center_by_external_id(center["external_id"])
        if target_center is None:
            raise AdminOperationError(
                "center_not_published",
                "The assigned center was not found in the public database yet.",
            )
        target_barangay = target.barangay_by_psgc_or_name(
            psgc_code=barangay["psgc_code"], name=barangay["name"]
        )
        if target_barangay is None:
            raise AdminOperationError(
                "barangay_not_found_in_target",
                "This barangay could not be matched in the public database "
                "(no PSGC code and no exact name match).",
                status=500,
            )
        target.set_barangay_evacuation_center(
            target_barangay["id"], target_center["id"], actor=actor
        )
        target.mark_barangay_designation_published(target_barangay["id"])
        self.repository.mark_barangay_designation_published(barangay_id)
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'publish_designation', 'barangay', ?, ?)
                """,
                (
                    actor,
                    str(barangay_id),
                    json.dumps(
                        {"center_external_id": center["external_id"]}, separators=(",", ":")
                    ),
                ),
            )
            connection.commit()
        return self.repository.barangays_with_designations()

    # -- Citizen damage reports ---------------------------------------------
    # Written by the public app into its own database; read and triaged by
    # the admin process through _publish_target_repository(). Responders
    # only -- never exposed through the public API.

    def _store_report_photo(self, content: bytes) -> str:
        if len(content) > MAX_PHOTO_BYTES:
            raise AdminOperationError(
                "photo_too_large", "Each photo must be 5 MB or smaller.", status=413
            )
        from io import BytesIO

        from PIL import Image, ImageOps  # lazy: admin.py is also imported on Vercel

        try:
            with Image.open(BytesIO(content)) as image:
                if image.format not in {"JPEG", "PNG", "WEBP"}:
                    raise AdminOperationError(
                        "unsupported_photo_type", "Upload a JPEG, PNG, or WebP photo."
                    )
                # Re-encoding drops EXIF (GPS, device) metadata; transpose
                # first so portrait phone photos stay upright.
                picture = ImageOps.exif_transpose(image).convert("RGB")
                picture.thumbnail((1600, 1600))
                output = BytesIO()
                picture.save(output, "JPEG", quality=80)
        except (OSError, Image.DecompressionBombError) as exc:
            raise AdminOperationError("invalid_photo", "A photo could not be read.") from exc
        directory = self.uploads_root / "citizen-reports"
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{secrets.token_hex(16)}.jpg"
        with (directory / filename).open("xb") as handle:
            handle.write(output.getvalue())
        return filename

    def submit_citizen_report(
        self, fields: dict[str, str], photos: list[bytes]
    ) -> dict[str, Any]:
        def text(name: str, label: str, limit: int, *, required: bool = False) -> str | None:
            value = (fields.get(name) or "").strip()
            if required and not value:
                raise AdminOperationError(f"{name}_required", f"{label} is required.")
            if len(value) > limit:
                raise AdminOperationError(
                    f"{name}_too_long", f"{label} must be {limit} characters or fewer."
                )
            return value or None

        if fields.get("consent") != "yes":
            raise AdminOperationError(
                "consent_required",
                "Please agree to share your name and phone number with responders.",
            )
        reporter_name = text("reporter_name", "Your name", 100, required=True)
        reporter_phone = re.sub(r"[\s()-]", "", fields.get("reporter_phone") or "")
        if not PHONE_PATTERN.fullmatch(reporter_phone):
            raise AdminOperationError("invalid_phone", "Enter a valid phone number.")
        damage_type = fields.get("damage_type")
        if damage_type not in DAMAGE_TYPES:
            raise AdminOperationError("invalid_damage_type", "Choose the type of damage.")
        severity = fields.get("severity")
        if severity not in SEVERITIES:
            raise AdminOperationError("invalid_severity", "Choose how severe the damage is.")
        people_text = (fields.get("people_affected") or "").strip()
        people_affected = None
        if people_text:
            if not people_text.isdecimal() or int(people_text) > 100_000:
                raise AdminOperationError(
                    "invalid_people_affected", "People affected must be a whole number."
                )
            people_affected = int(people_text)
        street = text("street", "Street", 200)
        sitio = text("sitio", "Sitio or purok", 200)
        landmark = text("landmark", "Landmark", 200)
        description = text("description", "Description", 1000)
        if len(photos) > MAX_REPORT_PHOTOS:
            raise AdminOperationError(
                "too_many_photos", f"Attach at most {MAX_REPORT_PHOTOS} photos."
            )
        try:
            latitude, longitude = validate_wgs84_point(
                fields.get("latitude"), fields.get("longitude")
            )
            # In live runtime mode this calls ULAP; a ULAP outage would reject
            # valid reports, so keep report intake on the default snapshot mode.
            location = self.service.identify_location(latitude, longitude)
        except ServiceError as exc:
            raise AdminOperationError(exc.code, str(exc), exc.status_code) from exc
        except ValueError as exc:
            raise AdminOperationError(
                "invalid_coordinates", "Place the pin where the damage is."
            ) from exc
        if not location.get("inside_basey"):
            raise AdminOperationError(
                "outside_basey",
                "The pin is outside Basey. Move it to where the damage is.",
                status=422,
            )
        barangay = (location.get("barangay") or {}).get("name")

        stored: list[str] = []
        try:
            for content in photos:
                stored.append(self._store_report_photo(content))
            created_at = datetime.now(timezone.utc).isoformat()
            with self.repository.connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO citizen_reports (
                        created_at, latitude, longitude, barangay, street, sitio,
                        landmark, damage_type, severity, people_affected,
                        description, reporter_name, reporter_phone, photos_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        created_at, latitude, longitude, barangay, street, sitio,
                        landmark, damage_type, severity, people_affected,
                        description, reporter_name, reporter_phone, json.dumps(stored),
                    ),
                )
                connection.commit()
        except Exception:
            for filename in stored:
                (self.uploads_root / "citizen-reports" / filename).unlink(missing_ok=True)
            raise
        return {"id": cursor.lastrowid, "barangay": barangay, "received_at": created_at}

    def citizen_reports(self) -> dict[str, Any]:
        target = self._publish_target_repository()
        with target.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM citizen_reports ORDER BY status = 'resolved', id DESC"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["photos"] = json.loads(item.pop("photos_json") or "[]")
            items.append(item)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(items),
            "items": items,
        }

    def set_citizen_report_status(
        self, report_id: int, status: str, *, actor: str
    ) -> dict[str, Any]:
        if status not in REPORT_STATUSES:
            raise AdminOperationError(
                "invalid_status", "Choose new, acknowledged, or resolved."
            )
        target = self._publish_target_repository()
        with target.connection() as connection:
            cursor = connection.execute(
                "UPDATE citizen_reports SET status = ? WHERE id = ?", (status, report_id)
            )
            connection.commit()
        if cursor.rowcount == 0:
            raise AdminOperationError(
                "report_not_found", "The report was not found.", status=404
            )
        with self.repository.connection() as connection:
            connection.execute(
                """
                INSERT INTO admin_audit_log (
                    actor, action, entity_type, entity_id, details_json
                ) VALUES (?, 'report_status', 'citizen_report', ?, ?)
                """,
                (actor, str(report_id), json.dumps({"status": status}, separators=(",", ":"))),
            )
            connection.commit()
        return {"id": report_id, "status": status}

    def citizen_report_photo(self, report_id: int, filename: str) -> tuple[bytes, str]:
        if PHOTO_FILE_PATTERN.fullmatch(filename):
            target = self._publish_target_repository()
            with target.connection() as connection:
                row = connection.execute(
                    "SELECT photos_json FROM citizen_reports WHERE id = ?", (report_id,)
                ).fetchone()
            path = self.uploads_root / "citizen-reports" / filename
            if row and filename in json.loads(row["photos_json"] or "[]") and path.is_file():
                return path.read_bytes(), "image/jpeg"
        raise AdminOperationError(
            "photo_not_found", "The report photograph was not found.", status=404
        )

    def hazard_events(self) -> dict[str, Any]:
        """Return every logged row from the hazard-event log, newest first.
        The dashboard renders these on a calendar, so nothing here is
        time-windowed or capped: rain rows are a complete daily record for
        Basey's coordinates (scripts/sync_rain_events_openmeteo.py) while
        earthquake/other rows stay a curated, notable-only selection.
        Local-only; never written by the deployed public application."""
        now = datetime.now(timezone.utc)
        with self.repository.connection() as connection:
            total_logged = connection.execute(
                "SELECT COUNT(*) AS c FROM hazard_events"
            ).fetchone()["c"]
            rows = connection.execute(
                """
                SELECT id, event_type, occurred_at, severity_value, severity_unit,
                       source_name, source_date, raw_reference, notes, is_official
                FROM hazard_events
                ORDER BY occurred_at DESC
                """
            ).fetchall()

        events = [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "severity_value": row["severity_value"],
                "severity_unit": row["severity_unit"],
                "source_name": row["source_name"],
                "source_date": row["source_date"],
                "raw_reference": row["raw_reference"],
                "notes": row["notes"],
                "is_official": bool(row["is_official"]),
            }
            for row in rows
        ]
        return {
            "generated_at": now.isoformat(),
            "definition": {
                "scope": "A manually curated log of notable bulletins, not a claim of exhaustive coverage.",
            },
            "events": events,
            "total_logged": total_logged,
        }

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
                "monitoring_read_only": False,
                "photo_uploads_enabled": True,
                "allowed_admin_writes": [
                    "evacuation_center_photos",
                    "evacuation_center_details",
                    "barangay_evacuation_center_designation",
                    "citizen_report_status",
                ],
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
