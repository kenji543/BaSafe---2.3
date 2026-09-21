#!/usr/bin/env python3
"""Synchronize one local OSM snapshot for both search and evacuation routing."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from geosafe.geometry import point_in_geometry
from geosafe.search import normalize_search_text


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return str(value)


def _text_values(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item).strip() for item in values if item is not None and str(item).strip()]


def _first_text(value: Any) -> str | None:
    values = _text_values(value)
    return values[0] if values else None


def _source_id(prefix: str, values: Iterable[Any]) -> str:
    digest = hashlib.sha256(
        "|".join(sorted({str(value) for value in values})).encode("utf-8")
    ).hexdigest()[:20]
    return f"osm-{prefix}-{digest}"


def _barangay_name(latitude: float, longitude: float, barangays: list[dict[str, Any]]) -> str | None:
    for barangay in barangays:
        try:
            if point_in_geometry(latitude, longitude, barangay["geometry"]):
                return barangay["name"]
        except Exception:
            continue
    return None


def _load_area(connection: sqlite3.Connection, allow_non_authoritative: bool) -> sqlite3.Row:
    connection.row_factory = sqlite3.Row
    area = connection.execute(
        """SELECT * FROM routing_study_areas WHERE is_active = 1
           ORDER BY is_official DESC, created_at DESC, id DESC LIMIT 1"""
    ).fetchone()
    if area is None:
        raise SystemExit("No active town-proper routing polygon is loaded.")
    if not area["is_official"] and not allow_non_authoritative:
        raise SystemExit(
            "The active study boundary is researcher-defined. Review its provenance "
            "and pass --allow-non-authoritative to acknowledge that classification."
        )
    return area


def _roads_feature_collection(edges: Any, mapping: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    features: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for (u, v, key), row in edges.iterrows():
        geometry = row.get("geometry")
        if geometry is None or geometry.is_empty:
            continue
        properties = {
            "u": str(u), "v": str(v), "key": str(key),
            "osmid": _jsonable(row.get("osmid")),
            "name": _jsonable(row.get("name")),
            "ref": _jsonable(row.get("ref")),
            "highway": _jsonable(row.get("highway")),
            "length": _jsonable(row.get("length")),
            "oneway": _jsonable(row.get("oneway")),
        }
        geometry_json = mapping(geometry)
        features.append({"type": "Feature", "properties": properties, "geometry": geometry_json})
        records.append({**properties, "geometry_shape": geometry, "geometry": geometry_json})
    return {"type": "FeatureCollection", "features": features}, records


def _street_records(
    edges: list[dict[str, Any]],
    unary_union: Any,
    mapping: Any,
    barangays: list[dict[str, Any]],
    snapshot_date: str,
    study_area_version: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        names = _text_values(edge.get("name"))
        for name in names:
            grouped[normalize_search_text(name)].append({**edge, "display_name": name})
    results: list[dict[str, Any]] = []
    for normalized_name, members in sorted(grouped.items()):
        if not normalized_name:
            continue
        geometry = unary_union([member["geometry_shape"] for member in members])
        midpoint = geometry.interpolate(0.5, normalized=True)
        alternate_names: list[str] = []
        osmids: list[Any] = []
        for member in members:
            osmids.extend(_text_values(member.get("osmid")))
            for key in ("ref",):
                alternate_names.extend(_text_values(member.get(key)))
        alternate_names = sorted(set(alternate_names), key=str.casefold)
        name = members[0]["display_name"]
        results.append(
            {
                "source_id": _source_id("street", [normalized_name, *osmids]),
                "source_type": "openstreetmap",
                "result_type": "street",
                "name": name,
                "normalized_name": normalized_name,
                "alternate_name": "; ".join(alternate_names) or None,
                "normalized_alternate_name": normalize_search_text(" ".join(alternate_names)) or None,
                "barangay": _barangay_name(midpoint.y, midpoint.x, barangays),
                "latitude": midpoint.y,
                "longitude": midpoint.x,
                "geometry_geojson": json.dumps(mapping(geometry), separators=(",", ":")),
                "category": "road",
                "subtype": _first_text(members[0].get("highway")),
                "source_name": "© OpenStreetMap contributors",
                "snapshot_date": snapshot_date,
                "metadata_json": json.dumps({"osmid": sorted(set(osmids)), "edge_count": len(members)}, separators=(",", ":")),
                "study_area_version": study_area_version,
            }
        )
    return results


def _poi_records(
    pois: Any,
    mapping: Any,
    barangays: list[dict[str, Any]],
    snapshot_date: str,
    study_area_version: str,
    allowlist: dict[str, list[str]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, row in pois.iterrows():
        name = _first_text(row.get("name"))
        geometry = row.get("geometry")
        if not name or geometry is None or geometry.is_empty:
            continue
        matched = [
            (key, str(row.get(key)))
            for key, allowed in allowlist.items()
            if row.get(key) is not None and str(row.get(key)) in allowed
        ]
        if not matched:
            continue
        point = geometry.representative_point()
        element_type, osmid = index if isinstance(index, tuple) else ("feature", index)
        alternative = _first_text(row.get("alt_name")) or _first_text(row.get("short_name"))
        category, subtype = matched[0]
        results.append(
            {
                "source_id": f"osm-{element_type}-{osmid}",
                "source_type": "openstreetmap",
                "result_type": "poi",
                "name": name,
                "normalized_name": normalize_search_text(name),
                "alternate_name": alternative,
                "normalized_alternate_name": normalize_search_text(alternative or "") or None,
                "barangay": _barangay_name(point.y, point.x, barangays),
                "latitude": point.y,
                "longitude": point.x,
                "geometry_geojson": json.dumps(mapping(geometry), separators=(",", ":")),
                "category": category,
                "subtype": subtype,
                "source_name": "© OpenStreetMap contributors",
                "snapshot_date": snapshot_date,
                "metadata_json": json.dumps({"element_type": str(element_type), "osmid": str(osmid)}, separators=(",", ":")),
                "study_area_version": study_area_version,
            }
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "geosafe.db"))
    parser.add_argument("--graph-output", default=str(ROOT / "data" / "routing" / "basey_walk.graphml"))
    parser.add_argument("--roads-output", default=str(ROOT / "data" / "routing" / "basey_roads.geojson"))
    parser.add_argument("--metadata-output", default=str(ROOT / "data" / "routing" / "osm_snapshot_metadata.json"))
    parser.add_argument("--snapshot-version", required=True)
    parser.add_argument("--allow-non-authoritative", action="store_true")
    args = parser.parse_args()

    try:
        import networkx as nx
        import osmnx as ox
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union
    except ImportError as exc:
        raise SystemExit("Install preprocessing dependencies with: uv sync --extra routing-prep") from exc

    config = json.loads((ROOT / "config" / "osm_search.json").read_text(encoding="utf-8"))
    database = Path(args.db)
    with sqlite3.connect(database) as connection:
        connection.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        area = _load_area(connection, args.allow_non_authoritative)
        barangays = [
            {"name": row[0], "geometry": json.loads(row[1])}
            for row in connection.execute("SELECT name, geometry_geojson FROM barangays ORDER BY name")
        ]

    area_geometry = json.loads(area["geometry_geojson"])
    polygon = shape(area_geometry)
    source_geometry_valid = bool(not polygon.is_empty and polygon.is_valid)
    if not source_geometry_valid and area_geometry.get("type") == "MultiPolygon":
        # Adjacent/overlapping official barangay members can make an undissolved
        # MultiPolygon invalid under OGC rules. Union the query copy only; the
        # stored component geometry and provenance remain unchanged.
        polygon = unary_union(
            [shape({"type": "Polygon", "coordinates": coordinates})
             for coordinates in area_geometry.get("coordinates", [])]
        )
    if polygon.is_empty or not polygon.is_valid or polygon.geom_type not in {"Polygon", "MultiPolygon"}:
        raise SystemExit("The routing study geometry could not be converted to a valid polygonal union.")
    acquired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshot_date = date.today().isoformat()
    graph = ox.graph.graph_from_polygon(
        polygon, network_type="walk", simplify=True, retain_all=True
    )
    nodes, edges = ox.convert.graph_to_gdfs(graph, nodes=True, edges=True, fill_edge_geometry=True)
    missing_lengths = int((~edges["length"].apply(lambda value: isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0)).sum())
    if missing_lengths:
        raise SystemExit(f"{missing_lengths} road edges lack a valid positive length.")

    road_collection, edge_records = _roads_feature_collection(edges, mapping)
    tags = {key: values for key, values in config["poi_allowlist"].items()}
    try:
        pois = ox.features.features_from_polygon(polygon, tags)
    except Exception as exc:
        print(f"Warning: POI synchronization failed; continuing with roads only: {exc}", file=sys.stderr)
        import geopandas as gpd
        pois = gpd.GeoDataFrame(geometry=[])

    streets = _street_records(edge_records, unary_union, mapping, barangays, snapshot_date, area["version"])
    poi_records = _poi_records(pois, mapping, barangays, snapshot_date, area["version"], config["poi_allowlist"])
    components = list(nx.weakly_connected_components(graph))
    segment_keys: dict[tuple[str, str, str], bool] = {}
    for edge in edge_records:
        key = tuple(sorted((edge["u"], edge["v"]))) + (json.dumps(edge["osmid"], sort_keys=True),)
        segment_keys[key] = bool(_text_values(edge.get("name")))
    named_segments = sum(segment_keys.values())
    unnamed_segments = len(segment_keys) - named_segments
    roads_without_geometry = len(edges) - len(edge_records)

    graph.graph.update({
        "snapshot_version": args.snapshot_version,
        "source": "© OpenStreetMap contributors via OSMnx explicit sync",
        "acquisition_date": snapshot_date,
        "acquired_at": acquired_at,
        "network_type": "walk",
        "study_area_version": area["version"],
        "study_area_source": area["source_name"],
        "study_area_official": bool(area["is_official"]),
        "hazard_enriched": False,
    })
    graph_output = Path(args.graph_output)
    roads_output = Path(args.roads_output)
    metadata_output = Path(args.metadata_output)
    for path in (graph_output, roads_output, metadata_output):
        path.parent.mkdir(parents=True, exist_ok=True)
    ox.io.save_graphml(graph, filepath=graph_output)
    roads_output.write_text(json.dumps(road_collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    columns = (
        "source_id", "source_type", "result_type", "name", "normalized_name",
        "alternate_name", "normalized_alternate_name", "barangay", "latitude",
        "longitude", "geometry_geojson", "category", "subtype", "source_name",
        "snapshot_date", "metadata_json", "study_area_version",
    )
    placeholders = ",".join("?" for _ in columns)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM searchable_locations WHERE source_type = 'openstreetmap' AND study_area_version = ?",
            (area["version"],),
        )
        connection.execute("UPDATE searchable_locations SET active = 0 WHERE source_type = 'openstreetmap'")
        connection.executemany(
            f"INSERT INTO searchable_locations ({','.join(columns)}, active) VALUES ({placeholders}, 1)",
            [tuple(record[column] for column in columns) for record in [*streets, *poi_records]],
        )
        connection.commit()

    metadata = {
        "snapshot_version": args.snapshot_version,
        "snapshot_date": snapshot_date,
        "acquired_at": acquired_at,
        "source": "© OpenStreetMap contributors",
        "network_type": "walk",
        "study_area": {"name": area["name"], "version": area["version"], "source": area["source_name"], "is_official": bool(area["is_official"])},
        "study_geometry_query_union_applied": not source_geometry_valid,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "road_segment_count": len(segment_keys),
        "named_road_segment_count": named_segments,
        "unnamed_road_segment_count": unnamed_segments,
        "named_edge_percentage": round(100 * named_segments / len(segment_keys), 2) if segment_keys else 0,
        "unique_street_names": len(streets),
        "searchable_poi_count": len(poi_records),
        "disconnected_components": len(components),
        "roads_without_usable_geometry": roads_without_geometry,
        "outputs": {"graphml": str(graph_output), "roads_geojson": str(roads_output), "database": str(database)},
        "runtime_network_requests": False,
        "precision_notice": config["precision_notice"],
    }
    metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
