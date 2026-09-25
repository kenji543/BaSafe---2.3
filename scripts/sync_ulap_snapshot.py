#!/usr/bin/env python3
"""Synchronize validated Basey ULAP layers into the local GeoSafe database.

This is an operator/deployment command, never part of an assessment request.
An unavailable or invalid upstream response is reported without replacing the
last successfully imported local snapshot.
"""

from __future__ import annotations

import argparse
import colorsys
import io
import json
import math
import sqlite3
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from geosafe.fuzzy import FuzzyModel  # noqa: E402
from geosafe.geometry import geometry_bbox, point_in_geometry  # noqa: E402
from geosafe.ulap.integration import UlapIntegration  # noqa: E402
from scripts.import_dataset import (  # noqa: E402
    ImportFailure,
    ImportOptions,
    import_dataset,
)


TARGETS = (
    "municipal_boundary",
    "barangay_boundary",
    "flood",
    "liquefaction",
    "ground_shaking",
)
HAZARDS = {"flood", "liquefaction", "ground_shaking"}
GROUND_SHAKING_SCENARIO_URLS = (
    "https://gisweb.phivolcs.dost.gov.ph/gisweb/storage/hazard-maps/"
    "region-viii-(eastern-visayas)/regional/earthquake/ground-shaking/"
    "gsh_2014_080000000_01.kmz",
    "https://gisweb.phivolcs.dost.gov.ph/gisweb/storage/hazard-maps/"
    "region-viii-(eastern-visayas)/regional/earthquake/ground-shaking/"
    "gsh_2014_080000000_02.kmz",
    "https://gisweb.phivolcs.dost.gov.ph/gisweb/storage/hazard-maps/"
    "region-viii-(eastern-visayas)/regional/earthquake/ground-shaking/"
    "gsh_2014_080000000_03.kmz",
    "https://gisweb.phivolcs.dost.gov.ph/gisweb/storage/hazard-maps/"
    "region-viii-(eastern-visayas)/regional/earthquake/ground-shaking/"
    "gsh_2014_080000000_04.kmz",
)
GROUND_SHAKING_GRID_STEP = 0.005
PEIS_ROMAN = {
    3: "III",
    4: "IV",
    5: "V",
    6: "VI",
    7: "VII",
    8: "VIII",
    9: "IX",
}


class SnapshotSyncError(RuntimeError):
    """A source response was unsafe to activate as a local snapshot."""


def _start_sync_run(database_path: Path, target: str) -> int | None:
    """Record the start of a sync attempt; never blocks the sync on failure."""
    try:
        connection = sqlite3.connect(database_path)
    except sqlite3.Error:
        return None
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        cursor = connection.execute(
            "INSERT INTO sync_runs (target, status) VALUES (?, 'in_progress')",
            (target,),
        )
        connection.commit()
        return cursor.lastrowid
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _finish_sync_run(
    database_path: Path, run_id: int | None, *, status: str, error_message: str | None
) -> None:
    if run_id is None:
        return
    try:
        connection = sqlite3.connect(database_path)
    except sqlite3.Error:
        return
    try:
        connection.execute(
            """
            UPDATE sync_runs
            SET finished_at = CURRENT_TIMESTAMP, status = ?, error_message = ?
            WHERE id = ?
            """,
            (status, error_message, run_id),
        )
        connection.commit()
    except sqlite3.Error:
        pass
    finally:
        connection.close()


def _download_official_kmz(url: str) -> bytes:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gisweb.phivolcs.dost.gov.ph"
        or not parsed.path.startswith("/gisweb/storage/hazard-maps/")
        or not parsed.path.endswith(".kmz")
    ):
        raise SnapshotSyncError("Ground-shaking source URL is not allowlisted.")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Basafe-Snapshot-Sync/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = response.read(50_000_001)
    except OSError as exc:
        raise SnapshotSyncError(
            f"Ground-shaking scenario download failed: {exc}"
        ) from exc
    if len(payload) > 50_000_000 or not zipfile.is_zipfile(io.BytesIO(payload)):
        raise SnapshotSyncError(
            "Ground-shaking scenario response is not a valid bounded KMZ file."
        )
    return payload


def _kmz_overlays(
    payload: bytes, basey_bbox: list[float]
) -> list[dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
        document = ElementTree.fromstring(archive.read("doc.kml"))
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise SnapshotSyncError("Ground-shaking KMZ structure is invalid.") from exc
    namespace = {"kml": "http://www.opengis.net/kml/2.2"}
    overlays: list[dict[str, Any]] = []
    for element in document.findall(".//kml:GroundOverlay", namespace):
        href = element.findtext("kml:Icon/kml:href", namespaces=namespace)
        box = element.find("kml:LatLonBox", namespace)
        if not href or box is None or "_L4_" not in href:
            continue
        try:
            west = float(box.findtext("kml:west", namespaces=namespace) or "")
            south = float(box.findtext("kml:south", namespaces=namespace) or "")
            east = float(box.findtext("kml:east", namespaces=namespace) or "")
            north = float(box.findtext("kml:north", namespaces=namespace) or "")
        except ValueError as exc:
            raise SnapshotSyncError(
                "Ground-shaking KMZ contains an invalid overlay extent."
            ) from exc
        if (
            east < basey_bbox[0]
            or west > basey_bbox[2]
            or north < basey_bbox[1]
            or south > basey_bbox[3]
        ):
            continue
        try:
            image = Image.open(io.BytesIO(archive.read(href))).convert("RGBA")
        except (KeyError, OSError) as exc:
            raise SnapshotSyncError(
                "Ground-shaking KMZ contains an unreadable overlay image."
            ) from exc
        overlays.append(
            {
                "href": href,
                "west": west,
                "south": south,
                "east": east,
                "north": north,
                "image": image,
            }
        )
    if not overlays:
        raise SnapshotSyncError(
            "Ground-shaking KMZ has no highest-resolution overlay covering Basey."
        )
    return overlays


def _pixel_peis(red: int, green: int, blue: int, alpha: int) -> float | None:
    if alpha < 220:
        return None
    hue, saturation, value = colorsys.rgb_to_hsv(
        red / 255.0, green / 255.0, blue / 255.0
    )
    # The official 2014 map legend is a continuous hue ramp from PEIS 3
    # (magenta, 300 degrees) to PEIS 9 (red, 0 degrees).
    if saturation < 0.55 or value < 0.65:
        return None
    intensity = 9.0 - (hue * 360.0 / 50.0)
    return min(9.0, max(3.0, intensity))


def _sample_scenario(overlays: list[dict[str, Any]], lon: float, lat: float) -> float:
    overlay = next(
        (
            item
            for item in overlays
            if item["west"] <= lon <= item["east"]
            and item["south"] <= lat <= item["north"]
        ),
        None,
    )
    if overlay is None:
        raise SnapshotSyncError(
            "Ground-shaking scenario raster does not cover a Basey sample point."
        )
    image: Image.Image = overlay["image"]
    x = round(
        (lon - overlay["west"])
        / (overlay["east"] - overlay["west"])
        * (image.width - 1)
    )
    y = round(
        (overlay["north"] - lat)
        / (overlay["north"] - overlay["south"])
        * (image.height - 1)
    )
    values: list[float] = []
    for delta_y in range(-10, 11):
        for delta_x in range(-10, 11):
            pixel = image.getpixel(
                (
                    max(0, min(image.width - 1, x + delta_x)),
                    max(0, min(image.height - 1, y + delta_y)),
                )
            )
            intensity = _pixel_peis(*pixel)
            if intensity is not None:
                values.append(intensity)
    if not values:
        raise SnapshotSyncError(
            "Ground-shaking raster sample contains no classifiable PEIS pixels."
        )
    # Narrow cartographic lines (rivers, faults and isoseismals) use unrelated
    # colours. Select the dominant quarter-PEIS bin in the 21x21 neighbourhood
    # so the underlying scenario surface, rather than a line symbol, wins.
    bins: dict[int, list[float]] = {}
    for value in values:
        bins.setdefault(round(value * 4), []).append(value)
    dominant = max(bins.values(), key=lambda items: (len(items), sum(items) / len(items)))
    dominant.sort()
    return dominant[len(dominant) // 2]


def _ground_shaking_collection(
    integration: UlapIntegration,
) -> tuple[dict[str, Any], dict[str, Any]]:
    municipal = integration.boundary_geojson("municipal_boundary")
    municipal_features = _require_available("municipal_boundary", municipal)
    geometries = [
        feature.get("geometry")
        for feature in municipal_features
        if isinstance(feature, dict) and isinstance(feature.get("geometry"), dict)
    ]
    if not geometries:
        raise SnapshotSyncError(
            "Ground-shaking synchronization needs the verified Basey geometry."
        )
    extents = [geometry_bbox(geometry) for geometry in geometries]
    bbox = [
        min(item[0] for item in extents),
        min(item[1] for item in extents),
        max(item[2] for item in extents),
        max(item[3] for item in extents),
    ]
    scenario_overlays = [
        _kmz_overlays(_download_official_kmz(url), bbox)
        for url in GROUND_SHAKING_SCENARIO_URLS
    ]
    x_start = math.floor(bbox[0] / GROUND_SHAKING_GRID_STEP) * GROUND_SHAKING_GRID_STEP
    y_start = math.floor(bbox[1] / GROUND_SHAKING_GRID_STEP) * GROUND_SHAKING_GRID_STEP
    columns = math.ceil((bbox[2] - x_start) / GROUND_SHAKING_GRID_STEP)
    rows = math.ceil((bbox[3] - y_start) / GROUND_SHAKING_GRID_STEP)
    features: list[dict[str, Any]] = []
    for column in range(columns):
        west = x_start + column * GROUND_SHAKING_GRID_STEP
        east = west + GROUND_SHAKING_GRID_STEP
        for row in range(rows):
            south = y_start + row * GROUND_SHAKING_GRID_STEP
            north = south + GROUND_SHAKING_GRID_STEP
            candidates = (
                ((south + north) / 2, (west + east) / 2),
                (south, west),
                (south, east),
                (north, west),
                (north, east),
            )
            inside = next(
                (
                    (latitude, longitude)
                    for latitude, longitude in candidates
                    if any(
                        point_in_geometry(latitude, longitude, geometry)
                        for geometry in geometries
                    )
                ),
                None,
            )
            if inside is None:
                continue
            latitude, longitude = inside
            scenario_values = [
                _sample_scenario(overlays, longitude, latitude)
                for overlays in scenario_overlays
            ]
            maximum = max(scenario_values)
            peis = max(3, min(9, math.floor(maximum + 0.5)))
            roman = PEIS_ROMAN[peis]
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [west, south],
                            [east, south],
                            [east, north],
                            [west, north],
                            [west, south],
                        ]],
                    },
                    "properties": {
                        "classification": f"PEIS {roman}",
                        "normalized_value": peis / 10.0,
                        "source_code": f"{peis:02d}",
                        "source_classification_field": "PEIS",
                        "scenario_peis_values": [
                            round(value, 3) for value in scenario_values
                        ],
                        "aggregation": "maximum_of_four_official_scenarios",
                        "grid_step_degrees": GROUND_SHAKING_GRID_STEP,
                    },
                }
            )
    if not features:
        raise SnapshotSyncError(
            "Ground-shaking scenario conversion produced no Basey grid cells."
        )
    retrieved_at = municipal.get("dataset", {}).get("retrievedAt")
    dataset = {
        "key": "ground_shaking",
        "agency": "Philippine Institute of Volcanology and Seismology",
        "sourceUrl": GROUND_SHAKING_SCENARIO_URLS[0].rsplit("/", 1)[0] + "/",
        "sourceDate": "2014",
        "retrievedAt": retrieved_at,
        "classificationField": "PEIS",
        "retrievalMethod": "PHIVOLCS regional scenario KMZ conversion",
        "scenarioUrls": list(GROUND_SHAKING_SCENARIO_URLS),
        "derivation": (
            "Each Basey grid cell stores the rounded maximum PEIS intensity "
            "sampled from four official Region VIII scenario rasters."
        ),
    }
    return {"type": "FeatureCollection", "features": features}, dataset


def _write_collection(path: Path, collection: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(collection, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _require_available(
    target: str, collection: dict[str, Any]
) -> list[dict[str, Any]]:
    status = str(collection.get("status") or "invalid_response")
    features = collection.get("features")
    if status != "available" or not isinstance(features, list) or not features:
        notices = collection.get("notices") or []
        detail = "; ".join(str(item) for item in notices) or "no features returned"
        raise SnapshotSyncError(f"{target}: {status}: {detail}")
    return features


def _hazard_collection(
    integration: UlapIntegration,
    model: FuzzyModel,
    hazard: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    collection = integration.hazard_geojson(hazard)
    features = _require_available(hazard, collection)
    dataset = dict(collection.get("dataset") or {})
    field_name = str(dataset.get("classificationField") or "").strip()
    if not field_name:
        raise SnapshotSyncError(
            f"{hazard}: the verified classification field is missing"
        )
    normalization = dict(
        (model.inputs.get(hazard) or {}).get("normalization") or {}
    )
    mappings = normalization.get("classification_mappings")
    if not isinstance(mappings, dict) or not mappings:
        raise SnapshotSyncError(
            f"{hazard}: the model has no exact-code normalization mapping"
        )

    converted: list[dict[str, Any]] = []
    for index, feature in enumerate(features, start=1):
        properties = dict(feature.get("properties") or {})
        raw_code = properties.get(field_name)
        mapping = mappings.get(str(raw_code)) if raw_code is not None else None
        if not isinstance(mapping, dict):
            raise SnapshotSyncError(
                f"{hazard}: feature {index} contains unmapped code {raw_code!r}"
            )
        official_label = properties.get("officialLabel")
        expected_label = mapping.get("official_label")
        if str(official_label) != str(expected_label):
            raise SnapshotSyncError(
                f"{hazard}: feature {index} label {official_label!r} does not "
                f"match the configured exact-code mapping {expected_label!r}"
            )
        try:
            normalized_fraction = float(mapping["normalized_value"]) / 100.0
        except (KeyError, TypeError, ValueError) as exc:
            raise SnapshotSyncError(
                f"{hazard}: code {raw_code!r} has an invalid model value"
            ) from exc
        properties.update(
            {
                "classification": str(official_label),
                "normalized_value": normalized_fraction,
                "source_code": str(raw_code),
                "source_classification_field": field_name,
            }
        )
        converted.append({**feature, "properties": properties})

    return ({"type": "FeatureCollection", "features": converted}, dataset)


def _import_options(
    *,
    target: str,
    input_path: Path,
    database_path: Path,
    integration: UlapIntegration,
    dataset: dict[str, Any],
) -> ImportOptions:
    definition = integration.registry.get(target)
    retrieved_at = dataset.get("retrievedAt")
    common = dict(
        input_path=input_path,
        database_path=database_path,
        source_name=dataset.get("agency") or definition.agency,
        source_date=dataset.get("sourceDate"),
        source_url=dataset.get("sourceUrl") or definition.layer_url,
        data_classification="official",
        quality_status=("limited" if target == "ground_shaking" else "provisional"),
        quality_notes=[
            (
                "Converted from official PHIVOLCS Region VIII scenario KMZ maps."
                if target == "ground_shaking"
                else "Synchronized from the configured GeoRisk Philippines ULAP service."
            ),
            "Activation followed source, schema, geometry, and classification validation.",
            "The source publication date and fitness for operational use require agency confirmation.",
            *(
                [
                    "Ground shaking is a derived 0.005-degree grid from four "
                    "official PHIVOLCS Region VIII 2014 scenario rasters.",
                    "The stored PEIS class is the rounded maximum of the four "
                    "scenario values and is not a new PHIVOLCS-issued vector layer.",
                ]
                if target == "ground_shaking"
                else []
            ),
            *(
                [f"ULAP retrieval timestamp: {retrieved_at}"]
                if retrieved_at
                else []
            ),
        ],
        source_crs="EPSG:4326",
        replace=True,
        strict=True,
    )
    if target == "municipal_boundary":
        return ImportOptions(
            target="municipal-boundary",
            boundary_name="Basey, Samar",
            **common,
        )
    if target == "barangay_boundary":
        return ImportOptions(
            target="barangays",
            name_field="brgy_name",
            psgc_field="psgc_10d",
            **common,
        )
    return ImportOptions(
        target="hazard",
        slug=f"{target}-georisk-snapshot",
        dataset_name=(
            definition.expected_layer_name
            or target.replace("_", " ").title()
        ),
        hazard_type=target,
        classification_field="classification",
        normalized_field="normalized_value",
        **common,
    )


def synchronize(
    *,
    database_path: Path,
    model_path: Path,
    registry_path: Path | None,
    targets: list[str],
) -> dict[str, Any]:
    model = FuzzyModel.from_file(model_path)
    integration = UlapIntegration.from_environment(registry_path)
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="geosafe-ulap-sync-") as temporary:
        temporary_path = Path(temporary)
        for target in targets:
            run_id = _start_sync_run(database_path, target)
            try:
                if target == "ground_shaking":
                    collection, dataset = _ground_shaking_collection(integration)
                elif target in HAZARDS:
                    collection, dataset = _hazard_collection(
                        integration, model, target
                    )
                else:
                    collection = integration.boundary_geojson(target)
                    _require_available(target, collection)
                    dataset = dict(collection.get("dataset") or {})
                snapshot_path = temporary_path / f"{target}.geojson"
                _write_collection(snapshot_path, collection)
                imported = import_dataset(
                    _import_options(
                        target=target,
                        input_path=snapshot_path,
                        database_path=database_path,
                        integration=integration,
                        dataset=dataset,
                    )
                )
            except (ImportFailure, OSError, SnapshotSyncError) as exc:
                _finish_sync_run(
                    database_path, run_id, status="failed", error_message=str(exc)
                )
                results.append(
                    {
                        "target": target,
                        "status": "not_activated",
                        "message": str(exc),
                        "preserved_previous_snapshot": True,
                    }
                )
            else:
                _finish_sync_run(
                    database_path, run_id, status="success", error_message=None
                )
                results.append(
                    {
                        "target": target,
                        "status": "activated",
                        "import": asdict(imported),
                    }
                )

    return {
        "status": (
            "complete"
            if all(item["status"] == "activated" for item in results)
            else "partial"
        ),
        "database": str(database_path.resolve()),
        "results": results,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download, validate, and atomically activate Basey ULAP snapshots. "
            "Ordinary assessments never invoke this command."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT_DIR / "data" / "geosafe.db",
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=ROOT_DIR / "config" / "fuzzy_model.json",
    )
    parser.add_argument("--registry", type=Path)
    parser.add_argument(
        "--target",
        action="append",
        choices=TARGETS,
        dest="targets",
        help="Repeat to synchronize selected layers; defaults to all layers.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    result = synchronize(
        database_path=arguments.db,
        model_path=arguments.model,
        registry_path=arguments.registry,
        targets=arguments.targets or list(TARGETS),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
