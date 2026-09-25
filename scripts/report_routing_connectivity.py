#!/usr/bin/env python3
"""Report per-barangay and per-evacuation-center connected-component
reachability against the frozen routing graph. Diagnostic only -- makes
no database, config, or graph file changes.

Reaches into HazardAwareRouter._load_graph()/_nearest_node() (internal,
underscore-prefixed) to reuse the exact snap logic the live API uses,
rather than reimplementing the nearest-node scan and risking drift.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import math  # noqa: E402

import networkx as nx  # noqa: E402

from geosafe.geometry import haversine_km, point_in_geometry, representative_point  # noqa: E402
from geosafe.server import create_application  # noqa: E402


def _snap(router, latitude: float, longitude: float) -> tuple[object, float]:
    """Nearest graph node and its distance, unconstrained by
    maximum_snap_distance_m. Mirrors HazardAwareRouter._nearest_node's own
    scan (reusing its _node_coordinates helper) instead of calling it
    directly: _nearest_node enforces the cutoff as a hard raise that drops
    the node id, and RoutingConfig is a frozen dataclass so the cutoff
    can't be temporarily widened to get it back."""
    graph = router._load_graph()
    nearest, nearest_distance = None, math.inf
    for node in graph.nodes:
        longitude_, latitude_ = router._node_coordinates(graph, node)
        distance = haversine_km(latitude, longitude, latitude_, longitude_) * 1000.0
        if distance < nearest_distance:
            nearest, nearest_distance = node, distance
    return nearest, nearest_distance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "data" / "geosafe.db"))
    parser.add_argument("--snap-thresholds", default="500,750,1000,1500,2000")
    parser.add_argument("--json-output")
    args = parser.parse_args()

    api, _, _ = create_application(
        database_path=args.db, enable_ulap=False, runtime_data_mode="snapshot"
    )
    router = api.service.router
    if router is None:
        raise SystemExit("Routing is not configured for this database.")
    repository = router.repository
    graph = router._load_graph()

    components = list(nx.weakly_connected_components(graph))
    node_component = {node: index for index, nodes in enumerate(components) for node in nodes}

    area = repository.routing_study_area()
    centers = [
        center
        for center in repository.evacuation_centers()
        if center["is_official"]
        and point_in_geometry(center["latitude"], center["longitude"], area["geometry"])
    ]

    served_components: set[int] = set()
    center_rows = []
    for center in centers:
        node, distance = _snap(router, center["latitude"], center["longitude"])
        component_id = node_component[node]
        served_components.add(component_id)
        center_rows.append(
            {
                "name": center["name"],
                "barangay": center["barangay"],
                "snap_distance_m": round(distance, 1),
                "component_id": component_id,
            }
        )

    cutoff = router.config.maximum_snap_distance_m
    barangay_rows = []
    for barangay in repository.barangays():
        point = representative_point(barangay["geometry"])
        node, distance = _snap(router, point["latitude"], point["longitude"])
        component_id = node_component[node]
        component_has_center = component_id in served_components
        barangay_rows.append(
            {
                "name": barangay["name"],
                "snap_distance_m": round(distance, 1),
                "component_id": component_id,
                "component_has_center": component_has_center,
                "reachable_at_current_cutoff": component_has_center and distance <= cutoff,
            }
        )

    sensitivity = []
    for threshold in (float(value) for value in args.snap_thresholds.split(",")):
        newly_reachable = sum(
            1
            for row in barangay_rows
            if row["component_has_center"]
            and not row["reachable_at_current_cutoff"]
            and row["snap_distance_m"] <= threshold
        )
        sensitivity.append({"threshold_m": threshold, "newly_reachable_vs_current": newly_reachable})

    orphan_barangays = sum(1 for row in barangay_rows if not row["component_has_center"])
    reachable_now = sum(1 for row in barangay_rows if row["reachable_at_current_cutoff"])

    print(f"Connected components: {len(components)} ({len(served_components)} contain an official center)")
    print(f"Barangays reachable at current {cutoff:.0f}m cutoff: {reachable_now}/{len(barangay_rows)}")
    print(f"Barangays in a component with no official center at all: {orphan_barangays}")
    print("Additional barangays reachable if the cutoff were raised (same-component near-misses only):")
    for row in sensitivity:
        print(f"  {row['threshold_m']:.0f}m: +{row['newly_reachable_vs_current']}")

    if args.json_output:
        report = {
            "component_count": len(components),
            "served_component_count": len(served_components),
            "current_maximum_snap_distance_m": cutoff,
            "centers": center_rows,
            "barangays": barangay_rows,
            "sensitivity": sensitivity,
        }
        Path(args.json_output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote full report to {args.json_output}")


if __name__ == "__main__":
    main()
