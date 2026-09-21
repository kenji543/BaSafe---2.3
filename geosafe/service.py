"""Application services that enforce the approved Basafe workflow."""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)

from .fuzzy import FuzzyModel
from .geometry import (
    GeometryError,
    as_feature,
    geometry_bbox,
    haversine_km,
    point_in_geometry,
    representative_point,
    validate_wgs84_point,
)
from .pdf import assessment_report_lines, generate_pdf
from .repository import Repository
from .search import normalize_search_text
from .routing import HazardAwareRouter, RoutingError
from .ulap.integration import REQUIRED_HAZARDS, UlapIntegration


HAZARD_ORDER = ("flood", "liquefaction", "ground_shaking")
RUNTIME_DATA_MODES = ("snapshot", "live")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ServiceError(RuntimeError):
    """Base class for errors that have a safe API representation."""

    status_code = 400
    code = "service_error"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class DataUnavailableError(ServiceError):
    status_code = 503
    code = "data_unavailable"


class OutsideBaseyError(ServiceError):
    status_code = 422
    code = "outside_basey"


class ValidationError(ServiceError):
    status_code = 422
    code = "validation_error"


class RoutingRequestError(ServiceError):
    """Expose a routing-specific code without weakening existing API errors."""

    def __init__(self, error: RoutingError):
        super().__init__(str(error), error.details or None)
        self.code = error.code
        self.status_code = error.status_code


class GeoSafeService:
    """Coordinates spatial lookup, fuzzy evaluation, persistence, and reports."""

    def __init__(
        self,
        repository: Repository,
        model: FuzzyModel,
        ulap: UlapIntegration | None = None,
        *,
        allow_test_fixtures: bool = False,
        runtime_data_mode: str = "live",
        router: HazardAwareRouter | None = None,
    ):
        if runtime_data_mode not in RUNTIME_DATA_MODES:
            raise ValueError(
                "runtime_data_mode must be one of: "
                + ", ".join(RUNTIME_DATA_MODES)
            )
        self.repository = repository
        self.model = model
        self.ulap = ulap
        self.allow_test_fixtures = allow_test_fixtures
        self.runtime_data_mode = runtime_data_mode
        self.router = router

    @property
    def uses_live_runtime_data(self) -> bool:
        """Whether ordinary map and assessment requests may query ULAP."""
        return self.runtime_data_mode == "live" and self.ulap is not None

    # ------------------------------------------------------------------
    # Live GeoRisk fallback (snapshot mode only)
    # Used when the local snapshot has no polygon covering the point.
    # The map layer continues to use the live ULAP server; only the
    # hazard score falls back here.
    # ------------------------------------------------------------------

    _LIVE_LAYER_URLS: dict[str, str] = {
        "flood": (
            "https://ulap-hazards.georisk.gov.ph/arcgis/rest/services"
            "/MGBPublic/Flood/MapServer/0"
        ),
        "liquefaction": (
            "https://ulap-hazards.georisk.gov.ph/arcgis/rest/services"
            "/PHIVOLCSPublic/Liquefaction/MapServer/0"
        ),
        "ground_shaking": (
            "https://gisweb.phivolcs.dost.gov.ph/arcgis/rest/services"
            "/PHIVOLCSPublic/GroundShaking/MapServer/0"
        ),
    }
    _LIVE_CLASS_FIELDS: dict[str, str] = {
        "flood": "fscode",
        "liquefaction": "lccode",
        "ground_shaking": "peiscode",
    }
    _LIVE_AGENCIES: dict[str, str] = {
        "flood": "Mines and Geosciences Bureau (live fallback)",
        "liquefaction": "PHIVOLCS (live fallback)",
        "ground_shaking": "PHIVOLCS (live fallback)",
    }

    def _live_hazard_fallback(
        self,
        hazard_type: str,
        latitude: float,
        longitude: float,
    ) -> dict[str, Any] | None:
        """
        Query the public GeoRisk ArcGIS REST layer for a single point.
        Returns a hazard dict compatible with _hazard_at_location output,
        or None when the live API is unreachable or returns no feature.
        """
        layer_url = self._LIVE_LAYER_URLS.get(hazard_type)
        field = self._LIVE_CLASS_FIELDS.get(hazard_type)
        if not layer_url or not field:
            return None

        # Build ArcGIS point-query request (public, no token needed)
        params = urllib.parse.urlencode({
            "geometry": f"{longitude},{latitude}",
            "geometryType": "esriGeometryPoint",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": field,
            "returnGeometry": "false",
            "f": "json",
        })
        url = f"{layer_url}/query?{params}"
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Basafe/0.5-snapshot-fallback"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.warning("Live hazard fallback failed for %s: %s", hazard_type, exc)
            return None

        features = data.get("features") or []
        if not features:
            # A zero-feature response is an absence of classification, not proof
            # of low susceptibility. Preserve it as missing evidence.
            return None

        attrs = features[0].get("attributes") or {}
        raw = attrs.get(field)
        raw_code = str(int(raw)).zfill(2) if raw is not None else None

        # Map through the fuzzy model's classification_mappings
        input_cfg = self.model.inputs.get(hazard_type, {})
        mappings: dict[str, Any] = (
            (input_cfg.get("normalization") or {}).get("classification_mappings") or {}
        )
        mapping = mappings.get(str(raw_code)) if raw_code else None

        if not isinstance(mapping, dict):
            _log.warning(
                "Live fallback for %s returned unknown code %r",
                hazard_type, raw_code,
            )
            return None  # Unknown code — don't guess

        official_label = mapping.get("official_label", raw_code)
        normalized_value = float(mapping["normalized_value"])
        normalized_fraction = normalized_value / 100.0

        return {
            "status": "available",
            "availability_status": "available",
            "classification": official_label,
            "official_label": official_label,
            "raw_code": raw_code,
            "normalized_value": normalized_value,
            "normalized_fraction": normalized_fraction,
            "source_tag": "live_fallback",
            "agency": self._LIVE_AGENCIES.get(hazard_type, "GeoRisk ULAP"),
        }

    def _prefer_official(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return authorized records; test fixtures require an explicit opt-in."""
        official = [record for record in records if record.get("is_official")]
        if official:
            return official
        if self.allow_test_fixtures:
            return records
        return [record for record in records if not record.get("is_demo")]

    @staticmethod
    def _public_boundary(boundary: Mapping[str, Any]) -> dict[str, Any]:
        return as_feature(
            boundary["geometry"],
            {
                "name": boundary["name"],
                "is_official": boundary["is_official"],
                "is_demo": boundary["is_demo"],
                "data_status": boundary["data_status"],
                "source_metadata": boundary["source_metadata"],
                "created_at": boundary["created_at"],
            },
            boundary["id"],
        )

    @staticmethod
    def _public_barangay(barangay: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": barangay["id"],
            "name": barangay["name"],
            "psgc_code": barangay["psgc_code"],
            "is_official": barangay["is_official"],
            "is_demo": barangay["is_demo"],
            "data_status": barangay["data_status"],
            "source_metadata": barangay["source_metadata"],
        }

    def boundary_geojson(self) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            return self.ulap.boundary_geojson("municipal_boundary")
        all_boundaries = self.repository.municipal_boundaries()
        boundaries = self._prefer_official(all_boundaries)
        notices = []
        if not boundaries:
            notices.append(
                "No Basey municipal boundary is loaded. Import an official boundary "
                "before creating assessments."
            )
        elif any(boundary["is_demo"] for boundary in boundaries):
            notices.append(
                "At least one displayed municipal boundary is DEMONSTRATION DATA — "
                "NOT OFFICIAL."
            )
        if len(boundaries) != len(all_boundaries):
            notices.append(
                "Loaded demonstration municipal boundaries are suppressed because "
                "an explicitly official boundary collection is available."
            )
        return {
            "type": "FeatureCollection",
            "features": [self._public_boundary(item) for item in boundaries],
            "notices": notices,
        }

    def barangays_geojson(self) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            return self.ulap.boundary_geojson("barangay_boundary")
        all_barangays = self.repository.barangays()
        barangays = self._prefer_official(all_barangays)
        notices = []
        if not barangays:
            notices.append("No Basey barangay boundary dataset is loaded.")
        if any(barangay["is_demo"] for barangay in barangays):
            notices.append(
                "Displayed barangay boundaries include DEMONSTRATION DATA — NOT OFFICIAL."
            )
        if len(barangays) != len(all_barangays):
            notices.append(
                "Loaded demonstration barangay cells are suppressed because an "
                "explicitly official barangay collection is available."
            )
        return {
            "type": "FeatureCollection",
            "features": [
                as_feature(
                    barangay["geometry"],
                    self._public_barangay(barangay),
                    barangay["id"],
                )
                for barangay in barangays
            ],
            "notices": notices,
        }

    def barangay_geojson(self, barangay_id: int) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            collection = self.ulap.boundary_geojson("barangay_boundary")
            for feature in collection.get("features", []):
                properties = feature.get("properties") or {}
                identifiers = {
                    str(feature.get("id")),
                    str(properties.get("objectid")),
                    str(properties.get("brgy_code")),
                    str(properties.get("psgc_10d")),
                }
                if str(barangay_id) in identifiers:
                    return feature
            raise NotFoundError(f"Barangay {barangay_id} was not found.")
        barangay = self.repository.barangay(barangay_id)
        if not barangay:
            raise NotFoundError(f"Barangay {barangay_id} was not found.")
        return as_feature(
            barangay["geometry"],
            self._public_barangay(barangay),
            barangay["id"],
        )

    def _view_only_hazard_overlays(self) -> list[dict[str, Any]]:
        """Return verified optional map overlays without adding model inputs."""
        if self.ulap is None:
            return []
        overlays: list[dict[str, Any]] = []
        for item in self.ulap.services()["items"]:
            verification = item.get("verification") or {}
            if (
                item.get("dataset_type") != "hazard"
                or item.get("required")
                or not verification.get("view_only")
            ):
                continue
            configured = bool(item.get("configured"))
            verified = verification.get("status") == "verified"
            key = item["key"]
            overlays.append(
                {
                    "id": key,
                    "key": key,
                    "slug": key,
                    "name": item.get("name")
                    or key.replace("_", " ").title(),
                    "hazard_type": key,
                    "dataset_type": "hazard_overlay",
                    "required": False,
                    "view_only": True,
                    "source_name": item.get("agency"),
                    "source_date": verification.get("source_data_date"),
                    "source_url": item.get("layer_url"),
                    "layer_url": item.get("layer_url"),
                    "layer_id": item.get("layer_id"),
                    "classification_field": item.get("classification_field"),
                    "expected_domain": item.get("expected_domain")
                    or item.get("classification_domain")
                    or {},
                    "legend_colors": verification.get("legend_colors") or {},
                    "attribution": item.get("attribution"),
                    "configured": configured,
                    "is_official": verified,
                    "is_demo": False,
                    "quality_status": "available" if configured and verified else "unavailable",
                    "data_status": "available" if configured and verified else "unavailable",
                    "status": "available" if configured and verified else "unavailable",
                    "map_render_mode": verification.get("map_render_mode", "arcgis_export"),
                    "metadata": item,
                }
            )
        return overlays

    def hazard_layers(self) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            services = self.ulap.services()["items"]
            datasets = []
            for item in services:
                if item.get("dataset_type") != "hazard" or not item.get("required"):
                    continue
                key = item["key"]
                datasets.append(
                    {
                        "id": key,
                        "slug": key,
                        "name": key.replace("_", " ").title(),
                        "hazard_type": key,
                        "source_name": item.get("agency"),
                        "source_date": item.get("verification", {}).get(
                            "verified_on"
                        ),
                        "source_url": item.get("layer_url"),
                        "quality_status": item.get("runtime_validation", {}).get(
                            "status", "pending_verification"
                        ),
                        "is_official": bool(item.get("configured")),
                        "is_demo": False,
                        "data_status": (
                            "available"
                            if item.get("configured")
                            else "unavailable"
                        ),
                        "metadata": item,
                    }
                )
            datasets.extend(self._view_only_hazard_overlays())
            return {
                "items": datasets,
                "required_hazard_types": list(REQUIRED_HAZARDS),
                "notices": [
                    "Layers are fetched from the configured ULAP services at "
                    "runtime through the Basafe backend.",
                    "Ground shaking remains unavailable until a genuine, verified "
                    "ground-shaking endpoint is configured.",
                ],
            }
        datasets = self._prefer_official(self.repository.hazard_datasets())
        rendered_datasets = [
            {
                **dataset,
                "key": dataset["hazard_type"],
                "dataset_type": "hazard",
                "configured": True,
                "status": "available",
                "layer_url": (dataset.get("metadata") or {}).get("source_url"),
                "display_url": (
                    f"/hazard-layers/{dataset['hazard_type']}/features"
                ),
            }
            for dataset in datasets
        ]
        rendered_datasets.extend(self._view_only_hazard_overlays())
        return {
            "items": rendered_datasets,
            "required_hazard_types": list(HAZARD_ORDER),
            "runtime_data_mode": self.runtime_data_mode,
            "notices": (
                [
                    "One or more hazard layers are DEMONSTRATION DATA — NOT OFFICIAL."
                ]
                if any(dataset["is_demo"] for dataset in datasets)
                else []
            ),
        }

    def hazard_layer_features(self, dataset_id_or_slug: int | str) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            key = str(dataset_id_or_slug).replace("-", "_")
            if key not in REQUIRED_HAZARDS:
                raise NotFoundError(
                    f"Hazard layer {dataset_id_or_slug!r} was not found."
                )
            return self.ulap.hazard_geojson(key)
        dataset = self.repository.hazard_dataset(dataset_id_or_slug)
        if not dataset and str(dataset_id_or_slug) in HAZARD_ORDER:
            candidates = self._prefer_official(
                self.repository.hazard_datasets()
            )
            dataset = next(
                (
                    item
                    for item in candidates
                    if item["hazard_type"] == str(dataset_id_or_slug)
                ),
                None,
            )
        if not dataset:
            raise NotFoundError(f"Hazard layer {dataset_id_or_slug!r} was not found.")
        if dataset.get("is_demo") and not self.allow_test_fixtures:
            raise NotFoundError(f"Hazard layer {dataset_id_or_slug!r} was not found.")
        features = self.repository.hazard_features(dataset["id"])
        return {
            "type": "FeatureCollection",
            "features": [
                as_feature(
                    feature["geometry"],
                    {
                        **feature["properties"],
                        "classification": feature["classification"],
                        "normalized_fraction": feature["normalized_fraction"],
                        "normalized_value": (
                            round(feature["normalized_fraction"] * 100, 4)
                            if feature["normalized_fraction"] is not None
                            else None
                        ),
                        "dataset_id": dataset["id"],
                        "hazard_type": dataset["hazard_type"],
                    },
                    feature["id"],
                )
                for feature in features
            ],
            "dataset": dataset,
            "notices": (
                ["This layer is DEMONSTRATION DATA — NOT OFFICIAL."]
                if dataset["is_demo"]
                else []
            ),
        }

    def identify_location(self, latitude: float, longitude: float) -> dict[str, Any]:
        try:
            lat, lon = validate_wgs84_point(latitude, longitude)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc
        if self.uses_live_runtime_data:
            identified = self.ulap.identify(lat, lon)
            status = str(identified.get("status") or "invalid_response")
            inside = bool(identified.get("inside_basey"))
            municipality_source = self.ulap.registry.get("municipal_boundary")
            barangay_source = self.ulap.registry.get("barangay_boundary")
            source_metadata = {
                "source_name": municipality_source.agency,
                "source_url": municipality_source.layer_url,
                "attribution": municipality_source.attribution,
                "retrieved_at": identified.get("retrieved_at"),
                "data_classification": "official_reference",
            }
            barangay_metadata = {
                "source_name": barangay_source.agency,
                "source_url": barangay_source.layer_url,
                "attribution": barangay_source.attribution,
                "retrieved_at": identified.get("retrieved_at"),
                "data_classification": "official_reference",
            }
            return {
                "latitude": lat,
                "longitude": lon,
                "inside_basey": inside,
                "insideBasey": inside,
                "status": status,
                "municipality": identified.get("municipality"),
                "province": identified.get("province"),
                "municipal_boundary": (
                    {
                        "id": identified.get("municipality_code"),
                        "name": "Basey, Samar",
                        "municipality": identified.get("municipality"),
                        "province": identified.get("province"),
                        "municipality_code": identified.get(
                            "municipality_code"
                        ),
                        "province_code": identified.get("province_code"),
                        "psgc_code": (
                            identified.get("municipality_code")
                            or identified.get("psgc")
                        ),
                        "is_official": True,
                        "is_demo": False,
                        "data_status": status,
                        "source_metadata": source_metadata,
                    }
                    if inside
                    else None
                ),
                "barangay": (
                    {
                        "id": None,
                        "name": identified.get("barangay"),
                        "code": identified.get("barangay_code"),
                        "barangay_code": identified.get("barangay_code"),
                        "psgc_code": identified.get("psgc"),
                        "municipality": identified.get("municipality"),
                        "province": identified.get("province"),
                        "is_official": True,
                        "is_demo": False,
                        "data_status": status,
                        "source_metadata": barangay_metadata,
                    }
                    if identified.get("barangay")
                    else None
                ),
                "source_urls": identified.get("source_urls", []),
                "retrieved_at": identified.get("retrieved_at"),
                "notices": list(identified.get("warnings") or []),
            }
        boundaries = self._prefer_official(
            self.repository.municipal_boundaries()
        )
        if not boundaries:
            raise DataUnavailableError(
                "The Basey municipal boundary has not been loaded."
            )
        matching_boundaries = []
        geometry_errors = []
        for boundary in boundaries:
            try:
                if point_in_geometry(lat, lon, boundary["geometry"]):
                    matching_boundaries.append(boundary)
            except GeometryError:
                geometry_errors.append(boundary["id"])
        matching_boundaries.sort(
            key=lambda item: (not item["is_official"], item["id"])
        )
        inside = bool(matching_boundaries)
        selected_boundary = matching_boundaries[0] if inside else None
        notices: list[str] = []
        if geometry_errors:
            notices.append(
                "One or more municipal boundary geometries could not be evaluated."
            )
        if selected_boundary and selected_boundary["is_demo"]:
            notices.append(
                "The inside-Basey check uses a DEMONSTRATION boundary — NOT OFFICIAL."
            )

        matching_barangays: list[dict[str, Any]] = []
        if inside:
            for barangay in self._prefer_official(self.repository.barangays()):
                try:
                    if point_in_geometry(lat, lon, barangay["geometry"]):
                        matching_barangays.append(barangay)
                except GeometryError:
                    continue
            matching_barangays.sort(
                key=lambda item: (not item["is_official"], item["id"])
            )
            if not matching_barangays:
                notices.append(
                    "No loaded barangay polygon contains this point. Barangay "
                    "information is unavailable; this does not imply low vulnerability."
                )
            if len(matching_barangays) > 1:
                notices.append(
                    "Multiple barangay polygons contain the point. The preferred "
                    "official record is shown; boundary topology should be reviewed."
                )
            if matching_barangays and matching_barangays[0]["is_demo"]:
                notices.append(
                    "Barangay identification uses DEMONSTRATION DATA — NOT OFFICIAL."
                )
        return {
            "latitude": lat,
            "longitude": lon,
            "inside_basey": inside,
            "municipal_boundary": (
                {
                    "id": selected_boundary["id"],
                    "name": selected_boundary["name"],
                    "is_official": selected_boundary["is_official"],
                    "is_demo": selected_boundary["is_demo"],
                    "data_status": selected_boundary["data_status"],
                    "source_metadata": selected_boundary["source_metadata"],
                }
                if selected_boundary
                else None
            ),
            "barangay": (
                self._public_barangay(matching_barangays[0])
                if matching_barangays
                else None
            ),
            "notices": notices,
        }

    def search_locations(
        self,
        query: str,
        limit: int = 10,
        result_type: str | None = None,
    ) -> dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 2:
            raise ValidationError("Search text must contain at least two characters.")
        if not 1 <= limit <= 50:
            raise ValidationError("limit must be between 1 and 50.")
        allowed_types = {"street", "poi", "place", "evacuation_center", "barangay", "coordinate"}
        if result_type is not None and result_type not in allowed_types:
            raise ValidationError(
                "type must be street, poi, place, evacuation_center, barangay, or coordinate."
            )
        results: list[dict[str, Any]] = []
        wanted = normalize_search_text(query)
        coordinate_match = re.fullmatch(
            r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", query
        )
        if coordinate_match and result_type in {None, "coordinate"}:
            location = self.identify_location(
                float(coordinate_match.group(1)),
                float(coordinate_match.group(2)),
            )
            results.append(
                {
                    "kind": "coordinate",
                    "type": "coordinate",
                    "name": f"{location['latitude']:.6f}, {location['longitude']:.6f}",
                    "label": f"{location['latitude']:.6f}, {location['longitude']:.6f}",
                    **location,
                }
            )

        if result_type in {None, "street", "poi", "place"}:
            local_type = result_type if result_type in {"street", "poi", "place"} else None
            local_results = self.repository.search_local_locations(
                query,
                result_type=local_type,
                limit=max(limit, 50),
            )
            for item in local_results:
                item["subtitle"] = item.get("barangay") or "Basey"
                item["is_official"] = False
                item["is_demo"] = False
                results.append(item)

        if result_type in {None, "evacuation_center"}:
            for center in self.repository.evacuation_centers():
                if not center["is_official"]:
                    continue
                normalized_name = normalize_search_text(center["name"])
                if wanted not in normalized_name:
                    continue
                rank = 0 if wanted == normalized_name else 1 if normalized_name.startswith(wanted) else 2
                results.append(
                    {
                        "kind": "evacuation_center",
                        "type": "evacuation_center",
                        "id": center["id"],
                        "name": center["name"],
                        "label": center["name"],
                        "subtitle": center.get("barangay") or "Basey",
                        "latitude": center["latitude"],
                        "longitude": center["longitude"],
                        "barangay": center.get("barangay"),
                        "designation": center["designation"],
                        "source": center["source_name"],
                        "source_date": center["source_date"],
                        "is_official": center["is_official"],
                        "relevance": rank,
                    }
                )

        if self.uses_live_runtime_data:
            collection = self.ulap.boundary_geojson("barangay_boundary")
            if result_type in {None, "barangay"}:
                for feature in collection.get("features", []):
                    properties = feature.get("properties") or {}
                    name = str(properties.get("brgy_name") or properties.get("BARANGAY") or "").strip()
                    normalized_name = normalize_search_text(name)
                    if not name or wanted not in normalized_name:
                        continue
                    point = representative_point(feature["geometry"])
                    results.append(
                        {
                            "kind": "barangay", "type": "barangay", "name": name,
                            "label": f"Barangay {name}, Basey, Samar", **point,
                            "geometry": feature["geometry"],
                            "barangay": {"id": None, "name": name,
                                "barangay_code": properties.get("brgy_code"),
                                "psgc_code": properties.get("psgc_10d"),
                                "municipality": properties.get("city_name", "Basey"),
                                "province": properties.get("prov_name", "Samar"),
                                "is_official": True, "is_demo": False},
                            "is_demo": False, "is_official": True,
                            "relevance": 0 if wanted == normalized_name else 1 if normalized_name.startswith(wanted) else 2,
                        }
                    )
        elif result_type in {None, "barangay"}:
            search_candidates = self.repository.search_barangays(query, limit=max(limit, 100))
            for barangay in self._prefer_official(search_candidates):
                point = representative_point(barangay["geometry"])
                normalized_name = normalize_search_text(barangay["name"])
                results.append(
                    {
                        "kind": "barangay", "type": "barangay", "name": barangay["name"],
                        "label": f"Barangay {barangay['name']}, Basey, Samar", **point,
                        "geometry": barangay["geometry"],
                        "barangay": self._public_barangay(barangay),
                        "is_demo": barangay["is_demo"], "is_official": barangay["is_official"],
                        "relevance": 0 if wanted == normalized_name else 1 if normalized_name.startswith(wanted) else 2,
                    }
                )

        type_order = {"coordinate": 0, "street": 1, "poi": 2, "place": 3, "evacuation_center": 4, "barangay": 5}
        results.sort(key=lambda item: (
            int(item.get("relevance", 0)),
            type_order.get(str(item.get("kind")), 9),
            str(item.get("name") or item.get("label") or "").casefold(),
        ))
        return {
            "query": query,
            "items": results[:limit],
            "scope": "Local OSM streets/places, designated centers, Basey barangays, and WGS84 coordinates",
            "notice": (
                "Search uses only locally synchronized data and does not call Nominatim "
                "or Overpass at runtime. Street search improves location discovery, not "
                "the resolution of the underlying hazard datasets."
            ),
        }

    def _hazard_at_location(
        self,
        hazard_type: str,
        latitude: float,
        longitude: float,
        datasets: list[dict[str, Any]],
        *,
        allow_live_fallback: bool = True,
    ) -> tuple[dict[str, Any], list[str]]:
        relevant = [
            dataset
            for dataset in datasets
            if dataset["hazard_type"] == hazard_type
        ]
        notices: list[str] = []
        if not relevant:
            return (
                {
                    "hazard_type": hazard_type,
                    "hazard": hazard_type,
                    "name": hazard_type.replace("_", " ").title(),
                    "status": "unavailable",
                    "availability_status": "missing",
                    "classification": None,
                    "official_label": None,
                    "raw_code": None,
                    "normalized_value": None,
                    "normalized_fraction": None,
                    "source": None,
                    "warnings": ["No validated local snapshot is loaded."],
                    "quality_notice": (
                        "No dataset is loaded. Missing information is not low vulnerability."
                    ),
                },
                notices,
            )

        # Repository order deliberately prefers official and more recent data.
        dataset = relevant[0]
        matches: list[dict[str, Any]] = []
        for feature in self.repository.hazard_features(dataset["id"]):
            try:
                if point_in_geometry(latitude, longitude, feature["geometry"]):
                    matches.append(feature)
            except GeometryError:
                notices.append(
                    f"A feature in {dataset['name']} has invalid geometry and was skipped."
                )
        feature = matches[0] if matches else None
        quality_parts: list[str] = []
        if dataset["is_demo"]:
            quality_parts.append("DEMONSTRATION DATA — NOT OFFICIAL")
        if dataset["quality_status"] != "verified":
            quality_parts.append(f"quality status: {dataset['quality_status']}")
        if not dataset["source_date"]:
            quality_parts.append("source date unavailable")
        if len(matches) > 1:
            quality_parts.append(
                "overlapping features found; the first matching feature was used"
            )
        dataset_metadata = dict(dataset.get("metadata") or {})
        source_url = dataset_metadata.get("source_url")
        retrieved_at = (
            dataset_metadata.get("retrieved_at")
            or dataset_metadata.get("imported_at")
            or dataset.get("imported_at")
        )
        source = {
            "dataset_id": dataset["id"],
            "dataset_slug": dataset["slug"],
            "dataset_name": dataset["name"],
            "source_name": dataset["source_name"],
            "source_date": dataset["source_date"],
            "quality_status": dataset["quality_status"],
            "is_official": dataset["is_official"],
            "is_demo": dataset["is_demo"],
            "data_status": dataset["data_status"],
            "metadata": dataset["metadata"],
            "imported_at": dataset["imported_at"],
            "source_url": source_url,
            "retrieved_at": retrieved_at,
        }
        normalization = {
            "source_field": "hazard_features.normalized_value",
            "stored_domain": [0, 1],
            "model_domain": [0, 100],
            "transformation": "model input = stored normalized fraction × 100",
            "model_version": self.model.version,
            "notice": (
                "The importing deployment must document and validate how the "
                "source classification or measurement was normalized."
            ),
        }
        if feature is None:
            # --- Hybrid fallback: try live GeoRisk API before giving up ---
            live = (
                self._live_hazard_fallback(hazard_type, latitude, longitude)
                if allow_live_fallback
                else None
            )
            if live is not None:
                live.pop("source_tag", None)
                fallback_agency = live.pop("agency", dataset.get("source_name") or "GeoRisk ULAP")
                no_local_warning = (
                    "Local snapshot has no polygon at this point. "
                    "Score retrieved from live GeoRisk ArcGIS service."
                )
                quality_parts.append(no_local_warning)
                return (
                    {
                        "hazard_type": hazard_type,
                        "hazard": hazard_type,
                        "name": dataset["name"],
                        "status": live["status"],
                        "availability_status": live["availability_status"],
                        "classification": live["classification"],
                        "official_label": live["official_label"],
                        "raw_code": live["raw_code"],
                        "normalized_value": live["normalized_value"],
                        "normalized_fraction": live["normalized_fraction"],
                        "source": {
                            **source,
                            "source_name": fallback_agency,
                            "data_status": "live_fallback",
                        },
                        "normalization": normalization,
                        "source_name": fallback_agency,
                        "source_date": dataset["source_date"],
                        "quality_status": "live_fallback",
                        "is_official": True,
                        "is_demo": False,
                        "data_status": "live_fallback",
                        "source_url": self._LIVE_LAYER_URLS.get(hazard_type, source_url),
                        "retrieved_at": retrieved_at,
                        "spatial_reference": 4326,
                        "warnings": [no_local_warning],
                        "quality_notice": "; ".join(quality_parts) or None,
                    },
                    notices,
                )
            reason = (
                "No feature in the local snapshot or live GeoRisk response covers "
                "this point. Missing information is not low vulnerability."
            )
            quality_parts.append(reason)
            return (
                {
                    "hazard_type": hazard_type,
                    "hazard": hazard_type,
                    "name": dataset["name"],
                    "status": "no_intersection",
                    "availability_status": "missing",
                    "classification": None,
                    "official_label": None,
                    "raw_code": None,
                    "normalized_value": None,
                    "normalized_fraction": None,
                    "source": source,
                    "normalization": normalization,
                    "source_name": dataset["source_name"],
                    "source_date": dataset["source_date"],
                    "quality_status": dataset["quality_status"],
                    "is_official": dataset["is_official"],
                    "is_demo": dataset["is_demo"],
                    "data_status": dataset["data_status"],
                    "source_url": source_url,
                    "retrieved_at": retrieved_at,
                    "spatial_reference": 4326,
                    "warnings": [reason],
                    "quality_notice": "; ".join(quality_parts) or None,
                },
                notices,
            )
            
        if feature["normalized_fraction"] is None:
            reason = "The covering feature has no normalized hazard value."
            quality_parts.append(
                f"{reason} Missing information is not low vulnerability."
            )
            return (
                {
                    "hazard_type": hazard_type,
                    "hazard": hazard_type,
                    "name": dataset["name"],
                    "status": "missing_value",
                    "availability_status": "missing",
                    "classification": feature["classification"],
                    "official_label": feature["classification"],
                    "raw_code": feature["properties"].get("source_code"),
                    "normalized_value": None,
                    "normalized_fraction": None,
                    "source": source,
                    "normalization": normalization,
                    "source_name": dataset["source_name"],
                    "source_date": dataset["source_date"],
                    "quality_status": dataset["quality_status"],
                    "is_official": dataset["is_official"],
                    "is_demo": dataset["is_demo"],
                    "data_status": dataset["data_status"],
                    "source_url": source_url,
                    "retrieved_at": retrieved_at,
                    "spatial_reference": 4326,
                    "warnings": [reason],
                    "quality_notice": "; ".join(quality_parts),
                },
                notices,
            )
        normalized_fraction = float(feature["normalized_fraction"])
        normalized_value = round(normalized_fraction * 100, 4)
        return (
            {
                "hazard_type": hazard_type,
                "hazard": hazard_type,
                "name": dataset["name"],
                "status": "available",
                "availability_status": "available",
                "classification": feature["classification"],
                "official_label": feature["classification"],
                "raw_code": feature["properties"].get("source_code"),
                "normalized_value": normalized_value,
                "normalized_fraction": normalized_fraction,
                "source": source,
                "normalization": normalization,
                "source_name": dataset["source_name"],
                "source_date": dataset["source_date"],
                "quality_status": dataset["quality_status"],
                "is_official": dataset["is_official"],
                "is_demo": dataset["is_demo"],
                "data_status": dataset["data_status"],
                "feature_properties": feature["properties"],
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "spatial_reference": 4326,
                "warnings": [],
                "quality_notice": "; ".join(quality_parts) or None,
            },
            notices,
        )

    def _runtime_assessment_hazards(
        self,
        latitude: float,
        longitude: float,
        *,
        local_only: bool = False,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve model inputs from the configured runtime data source.

        Snapshot mode never falls through to the network. An absent local
        dataset therefore remains an explicit missing input instead of making
        an opportunistic ULAP request during an assessment.
        """
        if self.uses_live_runtime_data and not local_only:
            assert self.ulap is not None
            return self.ulap.assessment_hazards(
                latitude, longitude, self.model
            )

        datasets = self._prefer_official(self.repository.hazard_datasets())
        hazards: list[dict[str, Any]] = []
        notices: list[str] = []
        for hazard_type in HAZARD_ORDER:
            hazard, hazard_notices = self._hazard_at_location(
                hazard_type,
                latitude,
                longitude,
                datasets,
                allow_live_fallback=not local_only,
            )
            hazards.append(hazard)
            notices.extend(hazard_notices)
        return hazards, notices

    def nearby_incidents(
        self,
        latitude: float,
        longitude: float,
        radius_km: float = 10,
        barangay_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        try:
            lat, lon = validate_wgs84_point(latitude, longitude)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc
        if radius_km <= 0 or radius_km > 100:
            raise ValidationError("radius_km must be greater than 0 and no more than 100.")
        matches: list[dict[str, Any]] = []
        incident_records = self._prefer_official(
            self.repository.incidents(limit=1000)
        )
        for incident in incident_records:
            reason = None
            distance = None
            if barangay_id is not None and incident["barangay_id"] == barangay_id:
                reason = "same_barangay"
            if incident["geometry"]:
                try:
                    if point_in_geometry(lat, lon, incident["geometry"]):
                        reason = "covering_geometry"
                except GeometryError:
                    pass
            if incident["latitude"] is not None and incident["longitude"] is not None:
                distance = haversine_km(
                    lat,
                    lon,
                    incident["latitude"],
                    incident["longitude"],
                )
                if distance <= radius_km:
                    reason = reason or "within_radius"
            if reason:
                item = dict(incident)
                item["match_reason"] = reason
                item["distance_km"] = round(distance, 3) if distance is not None else None
                matches.append(item)
        matches.sort(
            key=lambda item: (
                item["distance_km"] is None,
                item["distance_km"] if item["distance_km"] is not None else 0,
                item["incident_date"] or "",
            )
        )
        return matches[:limit]

    def clup_by_location(
        self,
        latitude: float,
        longitude: float,
        barangay_id: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        try:
            lat, lon = validate_wgs84_point(latitude, longitude)
        except GeometryError as exc:
            raise ValidationError(str(exc)) from exc
        matches: list[dict[str, Any]] = []
        clup_records = self._prefer_official(
            self.repository.clup_references(limit=1000)
        )
        for reference in clup_records:
            reason = None
            if barangay_id is not None and reference["barangay_id"] == barangay_id:
                reason = "same_barangay"
            if reference["geometry"]:
                try:
                    if point_in_geometry(lat, lon, reference["geometry"]):
                        reason = "covering_geometry"
                except GeometryError:
                    pass
            if reference["barangay_id"] is None and reference["geometry"] is None:
                reason = "municipality_wide"
            if reason:
                item = dict(reference)
                item["match_reason"] = reason
                matches.append(item)
        return matches[:limit]

    def _recommendations(
        self,
        result: Mapping[str, Any],
        contains_demonstration_data: bool,
    ) -> list[str]:
        category = (
            result["category"] if result["status"] == "complete" else "Incomplete"
        )
        recommendations = list(
            self.model.configuration.get("recommendation_templates", {}).get(
                category, []
            )
        )
        if "DEMONSTRATION" in str(
            self.model.configuration.get("status", "")
        ).upper():
            recommendations.insert(
                0,
                "Treat this score as a demonstration-model output until qualified "
                "domain experts validate the variables, thresholds, rules, and weights.",
            )
        if contains_demonstration_data:
            recommendations.insert(
                0,
                "Replace demonstration layers with current, authorized official "
                "datasets before operational planning use.",
            )
        return recommendations

    def evaluate_point(
        self,
        latitude: float,
        longitude: float,
        location_label: str | None = None,
        selection_method: str = "coordinates",
        *,
        persist: bool = False,
        local_only: bool = False,
    ) -> dict[str, Any]:
        """Evaluate one point, optionally persisting a normal user assessment.

        Routing enrichment uses ``persist=False`` so preprocessing cannot fill
        public assessment history with road-sample records.
        """
        location = self.identify_location(latitude, longitude)
        if not location["inside_basey"]:
            if location.get("status") not in {
                None,
                "outside_coverage",
                "no_intersection",
            }:
                raise DataUnavailableError(
                    "The live Basey boundary service could not confirm the "
                    "selected location.",
                    {"location": location},
                )
            raise OutsideBaseyError(
                "The selected point is outside the verified Basey municipal boundary.",
                {"location": location},
            )
        location["label"] = location_label
        location["selection_method"] = selection_method
        hazards, lookup_notices = self._runtime_assessment_hazards(
            location["latitude"],
            location["longitude"],
            local_only=local_only,
        )
        model_inputs = {
            hazard["hazard_type"]: hazard["normalized_value"]
            for hazard in hazards
        }
        result = self.model.evaluate(model_inputs)
        barangay_id = (
            location["barangay"].get("id") if location.get("barangay") else None
        )
        incidents = self.nearby_incidents(
            location["latitude"],
            location["longitude"],
            radius_km=10,
            barangay_id=barangay_id,
        )
        clup_references = self.clup_by_location(
            location["latitude"],
            location["longitude"],
            barangay_id=barangay_id,
        )
        data_quality_notices = list(location["notices"]) + lookup_notices
        for hazard in hazards:
            if hazard.get("quality_notice"):
                data_quality_notices.append(
                    f"{hazard['hazard_type'].replace('_', ' ').title()}: "
                    f"{hazard['quality_notice']}"
                )
        if not incidents:
            data_quality_notices.append(
                "No matching historical incident record is available in the loaded "
                "dataset. This does not establish that no incident occurred."
            )
        elif any(incident["is_demo"] for incident in incidents):
            data_quality_notices.append(
                "Historical incident context includes DEMONSTRATION DATA — NOT OFFICIAL."
            )
        if not clup_references:
            data_quality_notices.append(
                "No location-matched CLUP reference is available in the loaded data. "
                "Confirm against the current adopted CLUP."
            )
        elif any(reference["is_demo"] for reference in clup_references):
            data_quality_notices.append(
                "CLUP context includes DEMONSTRATION DATA — NOT OFFICIAL."
            )
        # Preserve order while removing repeated messages.
        data_quality_notices = list(dict.fromkeys(data_quality_notices))
        contains_demonstration_data = any(
            [
                bool(
                    location.get("municipal_boundary", {}).get("is_demo")
                    if location.get("municipal_boundary")
                    else False
                ),
                bool(
                    location.get("barangay", {}).get("is_demo")
                    if location.get("barangay")
                    else False
                ),
                any(hazard.get("is_demo") for hazard in hazards),
                any(incident.get("is_demo") for incident in incidents),
                any(reference.get("is_demo") for reference in clup_references),
            ]
        )
        recommendations = self._recommendations(
            result, contains_demonstration_data
        )
        source_information = {
            "municipal_boundary": location["municipal_boundary"],
            "barangay_boundary": location["barangay"],
            "hazard_datasets": [
                hazard.get("source") for hazard in hazards if hazard.get("source")
            ],
            "historical_incident_sources": [
                {
                    "incident_id": incident["id"],
                    "source_metadata": incident["source_metadata"],
                    "is_official": incident["is_official"],
                    "is_demo": incident["is_demo"],
                }
                for incident in incidents
            ],
            "clup_sources": [
                {
                    "reference_id": reference["id"],
                    "source_metadata": reference["source_metadata"],
                    "is_official": reference["is_official"],
                    "is_demo": reference["is_demo"],
                }
                for reference in clup_references
            ],
            "fuzzy_model": {
                "version": self.model.version,
                "checksum": self.model.checksum,
                "status": self.model.configuration.get("status"),
                "effective_date": self.model.configuration.get("effective_date"),
            },
        }
        assessment_sources: list[dict[str, Any]] = []
        for subject, spatial_source in (
            ("Municipal boundary", location["municipal_boundary"]),
            ("Barangay boundary", location["barangay"]),
        ):
            if not spatial_source:
                continue
            metadata = spatial_source.get("source_metadata", {})
            assessment_sources.append(
                {
                    **metadata,
                    "subject": subject,
                    "name": metadata.get("source_name", subject),
                    "data_status": spatial_source.get("data_status"),
                    "is_official": spatial_source.get("is_official"),
                    "is_demo": spatial_source.get("is_demo"),
                }
            )
        for hazard in hazards:
            if not hazard.get("source"):
                continue
            source = hazard["source"]
            assessment_sources.append(
                {
                    **source.get("metadata", {}),
                    "subject": hazard["hazard_type"].replace("_", " ").title(),
                    "name": source["dataset_name"],
                    "provider": source["source_name"],
                    "source_date": source["source_date"],
                    "quality_notice": source["quality_status"],
                    "data_status": source["data_status"],
                    "is_official": source["is_official"],
                    "is_demo": source["is_demo"],
                }
            )
        for subject, records in (
            ("Historical incident context", incidents),
            ("CLUP reference context", clup_references),
        ):
            seen_metadata: set[str] = set()
            for record in records:
                metadata = record["source_metadata"]
                metadata_key = str(sorted(metadata.items()))
                if metadata_key in seen_metadata:
                    continue
                seen_metadata.add(metadata_key)
                assessment_sources.append(
                    {
                        **metadata,
                        "subject": subject,
                        "name": metadata.get("source_name", subject),
                        "data_status": record["data_status"],
                        "is_official": record["is_official"],
                        "is_demo": record["is_demo"],
                    }
                )
        assessment_sources.append(
            {
                "subject": "Fuzzy inference model",
                "name": self.model.configuration.get("model_name"),
                "version": self.model.version,
                "checksum": self.model.checksum,
                "data_status": "demonstration",
                "is_official": False,
                "is_demo": True,
                "quality_notice": self.model.configuration.get("status"),
            }
        )
        snapshot = {
            "location": location,
            "hazards": hazards,
            "result": result,
            "assessment": {
                "status": result["status"],
                "assessmentStatus": result["status"],
                "score": result["score"],
                "category": (
                    result["category"]
                    if result["status"] == "complete"
                    else None
                ),
                "memberships": result["memberships"],
                "indicatorWeights": result.get("indicator_weights", {}),
                "activatedRules": result["activated_rules"],
                "missingInputs": result["missing_inputs"],
                "message": result["message"],
                "recommendations": recommendations,
            },
            "historical_incidents": incidents,
            "clup_references": clup_references,
            "source_information": source_information,
            "data_sources": assessment_sources,
            "data_quality_notices": data_quality_notices,
            "recommendations": recommendations,
            "disclaimer": self.model.configuration["disclaimer"],
            "report_preview": {
                "sections": [
                    "Selected location and barangay",
                    "Hazard classifications and normalized values",
                    "Membership values and activated fuzzy rules",
                    "Score/category or incomplete-data statement",
                    "Historical incident and CLUP context",
                    "Sources, dates, data-quality notices, recommendations, and disclaimer",
                ],
                "format": "PDF",
            },
        }
        if not persist:
            return snapshot
        saved = self.repository.save_assessment(snapshot, location_label)
        return self._with_links(saved)

    def create_assessment(
        self,
        latitude: float,
        longitude: float,
        location_label: str | None = None,
        selection_method: str = "coordinates",
    ) -> dict[str, Any]:
        """Create and persist the existing user-facing assessment record."""
        return self.evaluate_point(
            latitude,
            longitude,
            location_label,
            selection_method,
            persist=True,
        )

    def routing_status(self) -> dict[str, Any]:
        if self.router is None:
            return {
                "status": "unavailable",
                "routing_available": False,
                "scope": "No routing study area is loaded",
                "dependencies": {"routing_configuration": False},
                "notices": ["Routing has not been configured for this deployment."],
            }
        return self.router.status()

    def evacuation_centers(self) -> dict[str, Any]:
        centers = self.repository.evacuation_centers()
        official_count = sum(1 for center in centers if center["is_official"])
        reference_count = len(centers) - official_count
        return {
            "status": (
                "available"
                if official_count
                else "reference_available"
                if reference_count
                else "unavailable"
            ),
            "count": len(centers),
            "official_count": official_count,
            "reference_count": reference_count,
            "items": centers,
            "notice": (
                "The loaded center inventory is a user-supplied reference. Its "
                "issuing authority and publication date still need verification, so "
                "these records are not enabled as operational route destinations."
                if reference_count and not official_count
                else None
                if official_count
                else "No designated evacuation-center records are loaded."
            ),
            "routing": self.routing_status(),
        }

    def calculate_route(
        self,
        latitude: float,
        longitude: float,
        *,
        mode: str = "shortest",
        scenario: str = "multi_hazard",
        include_comparison: bool = False,
    ) -> dict[str, Any]:
        if self.router is None:
            raise RoutingRequestError(
                RoutingError(
                    "routing_graph_missing",
                    "Routing has not been configured for this deployment.",
                    status_code=503,
                )
            )
        location = self.identify_location(latitude, longitude)
        if not location["inside_basey"]:
            raise RoutingRequestError(
                RoutingError(
                    "outside_basey",
                    "The selected point is outside the verified Basey municipal boundary.",
                    details={"location": location},
                )
            )
        try:
            result = self.router.route(
                latitude,
                longitude,
                mode=mode,
                scenario=scenario,
                include_comparison=include_comparison,
            )
        except RoutingError as error:
            raise RoutingRequestError(error) from error
        result["location"] = {
            "inside_basey": True,
            "barangay": location.get("barangay"),
        }
        return result

    @staticmethod
    def _with_links(snapshot: dict[str, Any]) -> dict[str, Any]:
        linked = deepcopy(snapshot)
        assessment_id = linked.get("id")
        if assessment_id:
            linked["links"] = {
                "self": f"/api/v1/assessments/{assessment_id}",
                "explanation": f"/api/v1/assessments/{assessment_id}/explanation",
                "report": f"/api/v1/assessments/{assessment_id}/report",
            }
            linked.setdefault("report_preview", {})["download_url"] = linked["links"][
                "report"
            ]
        return linked

    def _assessment_record(
        self, public_token: str
    ) -> tuple[int, dict[str, Any]]:
        record = self.repository.assessment_by_token(public_token)
        if not record:
            raise NotFoundError("Assessment was not found.")
        internal_id, snapshot = record
        return internal_id, self._with_links(snapshot)

    def assessment(self, public_token: str) -> dict[str, Any]:
        return self._assessment_record(public_token)[1]

    def assessments(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100.")
        if offset < 0:
            raise ValidationError("offset must not be negative.")
        return self.repository.assessments(limit, offset)

    def explanation(self, public_token: str) -> dict[str, Any]:
        assessment = self.assessment(public_token)
        result = assessment["result"]
        return {
            "assessment_id": public_token,
            "status": result["status"],
            "model_version": result["model_version"],
            "normalized_inputs": result["normalized_inputs"],
            "memberships": result["memberships"],
            "indicator_weights": result.get("indicator_weights", {}),
            "evaluated_rules": result["evaluated_rules"],
            "activated_rules": result["activated_rules"],
            "inference": result["inference"],
            "centroid": result.get("centroid"),
            "score": result.get("score"),
            "category": result.get("category"),
            "missing_inputs": result["missing_inputs"],
            "message": result["message"],
            "validation_notes": result["validation_notes"],
        }

    def assessment_report(self, public_token: str) -> tuple[bytes, dict[str, Any]]:
        assessment_id, assessment = self._assessment_record(public_token)
        pdf_content = generate_pdf(
            assessment_report_lines(
                assessment,
                assessment.get(
                    "disclaimer", self.model.configuration["disclaimer"]
                ),
            ),
            title=f"Basafe Assessment {public_token[:12]}",
            assessment=assessment,
        )
        metadata = self.repository.save_report_record(
            assessment_id, pdf_content, assessment
        )
        return pdf_content, metadata

    def methodology(self) -> dict[str, Any]:
        configured = self.model.describe()
        public_model = {
            **configured,
            "version": self.model.version,
            "checksum": self.model.checksum,
            "defuzzification_method": configured["inference"]["defuzzification"],
            "input_variables": [
                {
                    **variable,
                    "domain": variable["universe"],
                    "membership_functions": variable["memberships"],
                }
                for variable in configured["inputs"]
            ],
            "output_categories": [
                {
                    **threshold,
                    **next(
                        (
                            membership
                            for membership in configured["output"]["memberships"]
                            if membership["term"].replace("_", " ").lower()
                            == threshold["category"].replace("_", " ").lower()
                        ),
                        {},
                    ),
                }
                for threshold in configured["output"]["category_thresholds"]
            ],
        }
        return {
            "model": public_model,
            "model_checksum": self.model.checksum,
            "scope": {
                "purpose": "Planning-oriented multi-hazard vulnerability screening",
                "numerical_inputs": list(HAZARD_ORDER),
                "context_only": ["historical_incidents", "clup_references"],
                "access": "One unified interface; no user roles or administrative portal",
            },
        }

    def ulap_status(self, *, refresh: bool = False) -> dict[str, Any]:
        if self.ulap is None:
            raise DataUnavailableError(
                "Live ULAP integration is not enabled for this application instance."
            )
        return self.ulap.status(refresh=refresh)

    def ulap_services(self) -> dict[str, Any]:
        if self.ulap is None:
            raise DataUnavailableError(
                "Live ULAP integration is not enabled for this application instance."
            )
        return self.ulap.services()

    def ulap_service_metadata(self, key: str) -> dict[str, Any]:
        if self.ulap is None:
            raise DataUnavailableError(
                "Live ULAP integration is not enabled for this application instance."
            )
        try:
            return self.ulap.service_metadata(key.replace("-", "_"))
        except ValueError as exc:
            raise NotFoundError(str(exc)) from exc

    def live_hazard_at_location(
        self, hazard: str, latitude: float, longitude: float
    ) -> dict[str, Any]:
        if self.ulap is None:
            raise DataUnavailableError(
                "Live ULAP integration is not enabled for this application instance."
            )
        location = self.identify_location(latitude, longitude)
        if not location["inside_basey"]:
            raise OutsideBaseyError(
                "The selected point is outside the verified Basey municipal boundary.",
                {"location": location},
            )
        try:
            result = self.ulap.hazard_at_location(
                hazard.replace("-", "_"),
                location["latitude"],
                location["longitude"],
            )
        except ValueError as exc:
            raise NotFoundError(str(exc)) from exc
        return {"location": location, **result}

    def live_hazards_at_location(
        self, latitude: float, longitude: float
    ) -> dict[str, Any]:
        if self.ulap is None:
            raise DataUnavailableError(
                "Live ULAP integration is not enabled for this application instance."
            )
        location = self.identify_location(latitude, longitude)
        if not location["inside_basey"]:
            raise OutsideBaseyError(
                "The selected point is outside the verified Basey municipal boundary.",
                {"location": location},
            )
        hazards, lookup_notices = self.ulap.assessment_hazards(
            location["latitude"], location["longitude"], self.model
        )
        return self._hazard_lookup_response(
            location, hazards, lookup_notices
        )

    def hazard_at_location(
        self, hazard: str, latitude: float, longitude: float
    ) -> dict[str, Any]:
        """Resolve one hazard using the configured runtime source."""
        normalized = hazard.replace("-", "_")
        if normalized not in HAZARD_ORDER:
            raise NotFoundError(f"Hazard {hazard!r} was not found.")
        payload = self.hazards_at_location(latitude, longitude)
        key = "groundShaking" if normalized == "ground_shaking" else normalized
        return {"location": payload["location"], **payload["hazards"][key]}

    def hazards_at_location(
        self, latitude: float, longitude: float
    ) -> dict[str, Any]:
        """Resolve all hazards without network access in snapshot mode."""
        if self.uses_live_runtime_data:
            return self.live_hazards_at_location(latitude, longitude)
        location = self.identify_location(latitude, longitude)
        if not location["inside_basey"]:
            raise OutsideBaseyError(
                "The selected point is outside the verified Basey municipal boundary.",
                {"location": location},
            )
        hazards, lookup_notices = self._runtime_assessment_hazards(
            location["latitude"], location["longitude"]
        )
        return self._hazard_lookup_response(
            location, hazards, lookup_notices
        )

    def _hazard_lookup_response(
        self,
        location: dict[str, Any],
        hazards: list[dict[str, Any]],
        lookup_notices: list[str],
    ) -> dict[str, Any]:
        result = self.model.evaluate(
            {
                hazard["hazard_type"]: hazard["normalized_value"]
                for hazard in hazards
            }
        )
        sources = []
        for hazard in hazards:
            source = hazard.get("source")
            if not source:
                continue
            metadata = dict(source.get("metadata") or {})
            sources.append(
                {
                    "hazard": hazard["hazard_type"],
                    "dataset": source.get("dataset_name"),
                    "agency": hazard.get("source_name"),
                    "source_url": hazard.get("source_url"),
                    "data_date": hazard.get("source_date"),
                    "data_status": hazard.get("status"),
                    "classification_field": hazard.get(
                        "classification_field"
                    ),
                    "raw_code": hazard.get("raw_code"),
                    "official_label": hazard.get("official_label"),
                    "retrieved_at": hazard.get("retrieved_at"),
                    "spatial_reference": hazard.get("spatial_reference"),
                    "attribution": hazard.get("attribution"),
                    "cache": hazard.get("cache"),
                    "is_official": source.get("is_official"),
                    "is_demo": source.get("is_demo"),
                    "metadata": metadata,
                }
            )
        notices = list(location["notices"]) + lookup_notices
        notices.extend(
            f"{hazard['hazard_type'].replace('_', ' ').title()}: "
            f"{hazard['quality_notice']}"
            for hazard in hazards
            if hazard.get("quality_notice")
        )
        return {
            "location": {
                **location,
                "insideBasey": location["inside_basey"],
            },
            "hazards": {
                (
                    "groundShaking"
                    if hazard["hazard_type"] == "ground_shaking"
                    else hazard["hazard_type"]
                ): hazard
                for hazard in hazards
            },
            "assessment": {
                "status": result["status"],
                "assessmentStatus": result["status"],
                "score": result["score"],
                "category": (
                    result["category"]
                    if result["status"] == "complete"
                    else None
                ),
                "memberships": result["memberships"],
                "activatedRules": result["activated_rules"],
                "missingInputs": result["missing_inputs"],
                "message": result["message"],
            },
            "sources": sources,
            "dataQuality": list(dict.fromkeys(notices)),
            "retrievedAt": _now_iso(),
            "runtimeDataMode": self.runtime_data_mode,
        }

    def data_sources(self) -> dict[str, Any]:
        if self.uses_live_runtime_data:
            registered = self.ulap.services()
            services = registered["items"]
            incidents = self._prefer_official(self.repository.incidents())
            clup = self._prefer_official(self.repository.clup_references())
            items = [
                {
                    "subject": service["key"].replace("_", " ").title(),
                    "name": service.get("expected_layer_name")
                    or service["key"].replace("_", " ").title(),
                    "provider": service.get("agency"),
                    "source_url": service.get("layer_url"),
                    "attribution": service.get("attribution"),
                    "classification_field": service.get(
                        "classification_field"
                    ),
                    "classification_domain": service.get("expected_domain", {}),
                    "data_status": (
                        service.get("runtime_validation", {}).get("status")
                        or (
                            "pending_verification"
                            if service.get("configured")
                            else "unavailable"
                        )
                    ),
                    "is_official": bool(service.get("configured")),
                    "is_demo": False,
                    "verification": service.get("verification", {}),
                }
                for service in services
            ]
            items.append(
                {
                    "subject": "Supplied barangay boundary reference",
                    "name": registered["manifest_audit"].get("file_name"),
                    **registered["manifest_audit"],
                    "data_status": "pending_verification",
                    "is_official": False,
                    "is_demo": False,
                }
            )
            items.append(
                {
                    "subject": "Fuzzy inference model",
                    "name": self.model.configuration.get("model_name"),
                    "version": self.model.version,
                    "checksum": self.model.checksum,
                    "data_status": "demonstration_model",
                    "is_official": False,
                    "is_demo": True,
                    "quality_notice": self.model.configuration.get("status"),
                }
            )
            for subject, records in (
                ("Historical incidents", incidents),
                ("CLUP references", clup),
            ):
                seen: set[str] = set()
                for record in records:
                    metadata = record["source_metadata"]
                    key = str(sorted(metadata.items()))
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(
                        {
                            "subject": subject,
                            "name": metadata.get("source_name", subject),
                            **metadata,
                            "data_status": record["data_status"],
                            "is_official": record["is_official"],
                            "is_demo": False,
                        }
                    )
            availability = [
                {
                    "target": service["key"],
                    "status": (
                        service.get("runtime_validation", {}).get("status")
                        or (
                            "pending_verification"
                            if service.get("configured")
                            else "unavailable"
                        )
                    ),
                }
                for service in services
            ]
            availability.extend(
                [
                    {
                        "target": "historical_incidents",
                        "status": "available" if incidents else "not_configured",
                    },
                    {
                        "target": "clup_references",
                        "status": "available" if clup else "not_configured",
                    },
                ]
            )
            return {
                "items": items,
                "official_data_availability": availability,
                "ulap_services": services,
                "supplied_manifest_audit": registered["manifest_audit"],
                "historical_incidents": {
                    "record_count": len(incidents),
                    "contains_demo": False,
                },
                "clup_references": {
                    "record_count": len(clup),
                    "contains_demo": False,
                },
                "fuzzy_model": {
                    "version": self.model.version,
                    "checksum": self.model.checksum,
                    "status": self.model.configuration.get("status"),
                    "validation_notes": self.model.configuration.get(
                        "validation_notes", []
                    ),
                },
                "notice": (
                    "Operational hazards and boundaries are requested from the "
                    "configured ULAP services. No synthetic runtime hazard source "
                    "is permitted."
                ),
            }
        boundaries = self._prefer_official(
            self.repository.municipal_boundaries()
        )
        barangays = self._prefer_official(self.repository.barangays())
        hazard_datasets = self._prefer_official(
            self.repository.hazard_datasets()
        )
        incidents = self._prefer_official(self.repository.incidents())
        clup = self._prefer_official(self.repository.clup_references())
        flattened_sources: list[dict[str, Any]] = []
        for item in boundaries:
            flattened_sources.append(
                {
                    "subject": "Municipal boundary",
                    "name": item["name"],
                    **item["source_metadata"],
                    "data_status": item["data_status"],
                    "is_official": item["is_official"],
                    "is_demo": item["is_demo"],
                }
            )
        seen_barangay_sources: set[str] = set()
        for item in barangays:
            source_key = str(sorted(item["source_metadata"].items()))
            if source_key in seen_barangay_sources:
                continue
            seen_barangay_sources.add(source_key)
            flattened_sources.append(
                {
                    "subject": "Barangay boundaries",
                    "name": item["source_metadata"].get(
                        "source_name", "Loaded barangay boundary dataset"
                    ),
                    **item["source_metadata"],
                    "data_status": item["data_status"],
                    "is_official": item["is_official"],
                    "is_demo": item["is_demo"],
                }
            )
        for dataset in hazard_datasets:
            flattened_sources.append(
                {
                    **dataset["metadata"],
                    "subject": dataset["hazard_type"].replace("_", " ").title(),
                    "name": dataset["name"],
                    "provider": dataset["source_name"],
                    "source_date": dataset["source_date"],
                    "quality_notice": dataset["quality_status"],
                    "data_status": dataset["data_status"],
                    "is_official": dataset["is_official"],
                    "is_demo": dataset["is_demo"],
                }
            )
        seen_context_sources: set[str] = set()
        for subject, records in (
            ("Historical incidents", incidents),
            ("CLUP references", clup),
        ):
            for item in records:
                source_key = f"{subject}:{sorted(item['source_metadata'].items())}"
                if source_key in seen_context_sources:
                    continue
                seen_context_sources.add(source_key)
                flattened_sources.append(
                    {
                        "subject": subject,
                        "name": item["source_metadata"].get("source_name", subject),
                        **item["source_metadata"],
                        "data_status": item["data_status"],
                        "is_official": item["is_official"],
                        "is_demo": item["is_demo"],
                    }
                )
        flattened_sources.append(
            {
                "subject": "Fuzzy inference model",
                "name": self.model.configuration.get("model_name"),
                "version": self.model.version,
                "checksum": self.model.checksum,
                "data_status": "demonstration",
                "is_official": False,
                "is_demo": True,
                "quality_notice": self.model.configuration.get("status"),
            }
        )
        official_data_availability = [
            {
                "target": "municipal_boundary",
                "status": (
                    "available"
                    if any(item["is_official"] for item in boundaries)
                    else "not_configured"
                ),
            },
            {
                "target": "barangay_boundaries",
                "status": (
                    "available"
                    if any(item["is_official"] for item in barangays)
                    else "not_configured"
                ),
            },
            *[
                {
                    "target": hazard_type,
                    "status": (
                        "available"
                        if any(
                            item["is_official"]
                            and item["hazard_type"] == hazard_type
                            for item in hazard_datasets
                        )
                        else "not_configured"
                    ),
                }
                for hazard_type in HAZARD_ORDER
            ],
            {
                "target": "historical_incidents",
                "status": (
                    "available"
                    if any(item["is_official"] for item in incidents)
                    else "not_configured"
                ),
            },
            {
                "target": "clup_references",
                "status": (
                    "available"
                    if any(item["is_official"] for item in clup)
                    else "not_configured"
                ),
            },
        ]
        return {
            "items": flattened_sources,
            "official_data_availability": official_data_availability,
            "municipal_boundaries": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    "source_metadata": item["source_metadata"],
                    "is_official": item["is_official"],
                    "is_demo": item["is_demo"],
                }
                for item in boundaries
            ],
            "barangay_boundaries": {
                "record_count": len(barangays),
                "sources": [
                    {
                        "source_metadata": item["source_metadata"],
                        "is_official": item["is_official"],
                        "is_demo": item["is_demo"],
                    }
                    for item in barangays
                ],
            },
            "hazard_datasets": hazard_datasets,
            "historical_incidents": {
                "record_count": len(incidents),
                "contains_demo": any(item["is_demo"] for item in incidents),
            },
            "clup_references": {
                "record_count": len(clup),
                "contains_demo": any(item["is_demo"] for item in clup),
            },
            "fuzzy_model": {
                "version": self.model.version,
                "checksum": self.model.checksum,
                "status": self.model.configuration.get("status"),
                "validation_notes": self.model.configuration.get(
                    "validation_notes", []
                ),
            },
            "notice": (
                "Runtime maps and assessments use these activated local snapshots "
                "without querying ULAP. Every record exposes official/demonstration "
                "status; demonstration content is unsuitable for operational decisions."
            ),
            "runtime_data_mode": self.runtime_data_mode,
        }

    def all_incidents(self, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValidationError("limit must be between 1 and 1000.")
        items = self._prefer_official(self.repository.incidents(limit))
        return {"items": items, "count": len(items)}

    def all_clup_references(self, limit: int = 200) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise ValidationError("limit must be between 1 and 1000.")
        items = self._prefer_official(self.repository.clup_references(limit))
        return {"items": items, "count": len(items)}
