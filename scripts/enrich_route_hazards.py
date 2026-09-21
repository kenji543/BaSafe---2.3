#!/usr/bin/env python3
"""Precompute local BaSafe mapped-hazard exposure for frozen road edges."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import networkx as nx

from geosafe.geometry import haversine_km
from geosafe.routing import HazardAwareRouter
from geosafe.server import create_application



def _edge_coordinates(graph: Any, u: Any, v: Any, edge: dict[str, Any]) -> list[list[float]]:
    coordinates = HazardAwareRouter._parse_edge_coordinates(
        edge.get("geometry_geojson", edge.get("geometry"))
    )
    if coordinates:
        return coordinates
    return [
        [float(graph.nodes[u]["x"]), float(graph.nodes[u]["y"])],
        [float(graph.nodes[v]["x"]), float(graph.nodes[v]["y"])],
    ]


def _sample_polyline(coordinates: list[list[float]], count: int) -> list[tuple[float, float]]:
    if count <= 1:
        count = 1
    segment_lengths = [
        haversine_km(a[1], a[0], b[1], b[0]) * 1000.0
        for a, b in zip(coordinates, coordinates[1:])
    ]
    total = sum(segment_lengths)
    if total <= 0:
        return [(coordinates[0][1], coordinates[0][0])]
    samples: list[tuple[float, float]] = []
    for index in range(count):
        target = total * (index + 0.5) / count
        traversed = 0.0
        for segment_index, length in enumerate(segment_lengths):
            if traversed + length >= target:
                fraction = (target - traversed) / length if length else 0.0
                start = coordinates[segment_index]
                end = coordinates[segment_index + 1]
                lon = start[0] + (end[0] - start[0]) * fraction
                lat = start[1] + (end[1] - start[1]) * fraction
                samples.append((lat, lon))
                break
            traversed += length
    return samples


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _screen_centers(
    repository: Any,
    service: Any,
    model: Any,
    screened_at: str,
) -> list[tuple[Any, ...]]:
    center_screenings: list[tuple[Any, ...]] = []
    for center in repository.evacuation_centers():
        try:
            assessment = service.evaluate_point(
                center["latitude"],
                center["longitude"],
                selection_method="routing_preprocessing",
                persist=False,
                local_only=True,
            )
            result = assessment["result"]
            center_screenings.append(
                (
                    result["status"],
                    result.get("score"),
                    result.get("category") if result["status"] == "complete" else None,
                    model.version,
                    screened_at,
                    center["id"],
                )
            )
        except Exception:
            center_screenings.append(
                ("unavailable", None, None, model.version, screened_at, center["id"])
            )
    if center_screenings:
        with repository.connection() as connection:
            connection.executemany(
                """
                UPDATE evacuation_centers
                SET hazard_screening_status = ?, hazard_score = ?,
                    hazard_category = ?, hazard_model_version = ?,
                    hazard_screened_at = ?
                WHERE id = ?
                """,
                center_screenings,
            )
            connection.commit()
    return center_screenings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--graph",
        default=str(ROOT / "data" / "routing" / "basey_walk.graphml"),
    )
    parser.add_argument("--output")
    parser.add_argument(
        "--metadata-output",
        default=str(ROOT / "data" / "routing" / "hazard_enrichment_metadata.json"),
    )
    parser.add_argument("--db", default=str(ROOT / "data" / "geosafe.db"))
    parser.add_argument("--samples-per-edge", type=int, default=3)
    parser.add_argument(
        "--centers-only",
        action="store_true",
        help="Screen loaded centers without recalculating every road edge.",
    )
    args = parser.parse_args()
    if args.samples_per_edge < 1 or args.samples_per_edge > 25:
        raise SystemExit("--samples-per-edge must be between 1 and 25.")
    graph_path = Path(args.graph)
    if not args.centers_only and not graph_path.is_file():
        raise SystemExit(f"Local graph not found: {graph_path}")
    graph = None if args.centers_only else nx.read_graphml(graph_path)
    api, repository, model = create_application(
        database_path=args.db,
        enable_ulap=False,
        runtime_data_mode="snapshot",
    )
    service = api.service
    screened_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    center_screenings = _screen_centers(repository, service, model, screened_at)
    metadata_path = Path(args.metadata_output)
    if args.centers_only:
        metadata: dict[str, Any] = {}
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata.update(
            {
                "model_version": model.version,
                "evacuation_centers_screened": len(center_screenings),
                "evacuation_centers_screened_at": screened_at,
            }
        )
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(metadata, indent=2))
        return

    assert graph is not None
    enriched = 0
    incomplete = 0
    edge_iterator = (
        graph.edges(keys=True, data=True)
        if graph.is_multigraph()
        else ((u, v, None, data) for u, v, data in graph.edges(data=True))
    )
    for u, v, _key, edge in edge_iterator:
        coordinates = _edge_coordinates(graph, u, v, edge)
        samples = _sample_polyline(coordinates, args.samples_per_edge)
        assessments: list[dict[str, Any]] = []
        for latitude, longitude in samples:
            try:
                assessments.append(
                    service.evaluate_point(
                        latitude,
                        longitude,
                        selection_method="routing_preprocessing",
                        persist=False,
                        local_only=True,
                    )
                )
            except Exception:
                assessments.append({"result": {"status": "incomplete"}, "hazards": []})
        complete = [item for item in assessments if item["result"]["status"] == "complete"]
        hazard_values: dict[str, list[float]] = {
            "flood": [],
            "liquefaction": [],
            "ground_shaking": [],
        }
        scores: list[float] = []
        for assessment in complete:
            score = assessment["result"].get("score")
            if score is not None:
                scores.append(float(score))
            for hazard in assessment.get("hazards", []):
                value = hazard.get("normalized_value")
                if hazard.get("hazard_type") in hazard_values and value is not None:
                    hazard_values[hazard["hazard_type"]].append(float(value))
        completeness = len(complete) / len(assessments) if assessments else 0.0
        hazard_complete = completeness == 1.0 and len(scores) == len(assessments)
        edge["length_m"] = float(edge.get("length_m", edge.get("length", 0.0)))
        edge["hazard_complete"] = hazard_complete
        edge["hazard_data_completeness"] = completeness
        edge["sample_count"] = len(assessments)
        edge["model_version"] = model.version
        edge["hazard_data_version"] = json.dumps(
            graph.graph.get("hazard_data_versions", "local-snapshot"),
            sort_keys=True,
        )
        if hazard_complete:
            edge["flood_score"] = _mean(hazard_values["flood"])
            edge["liquefaction_score"] = _mean(hazard_values["liquefaction"])
            edge["ground_shaking_score"] = _mean(hazard_values["ground_shaking"])
            edge["multi_hazard_score"] = _mean(scores)
            edge["hazard_mean"] = _mean(scores)
            edge["hazard_max"] = max(scores)
            enriched += 1
        else:
            for field in (
                "flood_score",
                "liquefaction_score",
                "ground_shaking_score",
                "multi_hazard_score",
                "hazard_mean",
                "hazard_max",
            ):
                edge.pop(field, None)
            incomplete += 1
    enriched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    graph.graph.update(
        {
            "hazard_enriched": True,
            "hazard_enriched_at": enriched_at,
            "model_version": model.version,
            "hazard_data_versions": json.dumps(
                {
                    item["hazard_type"]: item["slug"]
                    for item in repository.hazard_datasets()
                },
                sort_keys=True,
            ),
            "missing_hazard_policy": "reject_edge",
            "samples_per_edge": args.samples_per_edge,
        }
    )
    output = Path(args.output) if args.output else graph_path
    output.parent.mkdir(parents=True, exist_ok=True)
    nx.write_graphml(graph, output)
    metadata = {
        "output": str(output),
        "graph_snapshot_version": graph.graph.get("snapshot_version"),
        "hazard_enriched_at": enriched_at,
        "model_version": model.version,
        "samples_per_edge": args.samples_per_edge,
        "edges_with_complete_hazard_data": enriched,
        "edges_with_unknown_hazard_data": incomplete,
        "assessment_records_created": 0,
        "evacuation_centers_screened": len(center_screenings),
        "missing_hazard_policy": "reject_edge",
    }
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
