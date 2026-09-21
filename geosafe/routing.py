"""Local-snapshot pedestrian routing with transparent mapped-hazard costs."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .geometry import haversine_km, point_in_geometry, validate_wgs84_point
from .repository import Repository

try:
    import networkx as nx
except ImportError:  # pragma: no cover - exercised through status/error handling
    nx = None  # type: ignore[assignment]


ROUTING_DISCLAIMER = (
    "The displayed route is a research-based planning aid generated from the "
    "available road network and mapped hazard information. It does not represent "
    "real-time road closures, flood depth, structural damage, traffic, crowds, "
    "or an official evacuation order."
)


class RoutingError(RuntimeError):
    """Structured routing failure safe for the public API."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
        status_code: int = 422,
    ):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.status_code = status_code


@dataclass(frozen=True)
class RoutingConfig:
    config_version: str
    algorithm: str
    network_type: str
    risk_factor: float
    walking_speed_kph: float
    elevated_hazard_threshold: float
    maximum_snap_distance_m: float
    minimum_hazard_completeness: float
    missing_hazard_policy: str
    study_area_version: str
    road_network_source: str
    road_network_date: str | None
    road_graph_path: Path
    fuzzy_model_version: str
    hazard_dataset_versions: Mapping[str, str]
    evacuation_center_dataset_version: str
    planning_disclaimer: str

    @classmethod
    def from_file(cls, path: str | Path, project_root: str | Path) -> "RoutingConfig":
        source = Path(path)
        payload = json.loads(source.read_text(encoding="utf-8"))
        graph_path = Path(payload["road_graph_path"])
        if not graph_path.is_absolute():
            graph_path = Path(project_root) / graph_path
        config = cls(
            config_version=str(payload["config_version"]),
            algorithm=str(payload["algorithm"]),
            network_type=str(payload["network_type"]),
            risk_factor=float(payload["risk_factor"]),
            walking_speed_kph=float(payload["walking_speed_kph"]),
            elevated_hazard_threshold=float(payload["elevated_hazard_threshold"]),
            maximum_snap_distance_m=float(payload["maximum_snap_distance_m"]),
            minimum_hazard_completeness=float(payload["minimum_hazard_completeness"]),
            missing_hazard_policy=str(payload["missing_hazard_policy"]),
            study_area_version=str(payload["study_area_version"]),
            road_network_source=str(payload["road_network_source"]),
            road_network_date=payload.get("road_network_date"),
            road_graph_path=graph_path,
            fuzzy_model_version=str(payload["fuzzy_model_version"]),
            hazard_dataset_versions=dict(payload["hazard_dataset_versions"]),
            evacuation_center_dataset_version=str(
                payload["evacuation_center_dataset_version"]
            ),
            planning_disclaimer=str(
                payload.get("planning_disclaimer") or ROUTING_DISCLAIMER
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.algorithm != "astar":
            raise ValueError("routing.algorithm must be 'astar'.")
        if self.network_type != "walk":
            raise ValueError("routing.network_type must be 'walk'.")
        if self.risk_factor < 0:
            raise ValueError("routing.risk_factor must not be negative.")
        if self.walking_speed_kph <= 0:
            raise ValueError("routing.walking_speed_kph must be positive.")
        if not 0 <= self.elevated_hazard_threshold <= 1:
            raise ValueError("routing.elevated_hazard_threshold must be in [0, 1].")
        if self.maximum_snap_distance_m <= 0:
            raise ValueError("routing.maximum_snap_distance_m must be positive.")
        if not 0 <= self.minimum_hazard_completeness <= 1:
            raise ValueError("routing.minimum_hazard_completeness must be in [0, 1].")
        if self.missing_hazard_policy != "reject_edge":
            raise ValueError("routing.missing_hazard_policy must be 'reject_edge'.")


def generalized_edge_cost(length_m: float, hazard: float, risk_factor: float) -> float:
    """Return D * (1 + lambda * H), validating every scientific input."""
    distance = float(length_m)
    exposure = float(hazard)
    factor = float(risk_factor)
    if not math.isfinite(distance) or distance < 0:
        raise ValueError("Edge length must be finite and non-negative.")
    if not math.isfinite(exposure) or not 0 <= exposure <= 1:
        raise ValueError("Mapped hazard exposure must be in [0, 1].")
    if not math.isfinite(factor) or factor < 0:
        raise ValueError("Risk factor must be finite and non-negative.")
    return distance * (1.0 + factor * exposure)


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().casefold() in {"1", "true", "yes", "complete"}


class HazardAwareRouter:
    """Route on a frozen local graph; hazard evaluation happens in preprocessing."""

    def __init__(
        self,
        repository: Repository,
        config: RoutingConfig,
        *,
        graph: Any | None = None,
    ):
        self.repository = repository
        self.config = config
        self._graph = graph

    def status(self) -> dict[str, Any]:
        area = self.repository.routing_study_area()
        loaded_centers = self.repository.evacuation_centers()
        centers = [
            center for center in loaded_centers
            if center["is_official"]
        ]
        reference_centers = [
            center for center in loaded_centers if not center["is_official"]
        ]
        graph_exists = self._graph is not None or self.config.road_graph_path.is_file()
        dependencies = {
            "networkx": nx is not None,
            "routing_study_area": area is not None,
            "local_road_graph": graph_exists,
            "designated_evacuation_centers": bool(centers),
        }
        ready = all(dependencies.values())
        notices: list[str] = []
        if not centers:
            if reference_centers:
                notices.append(
                    f"{len(reference_centers)} supplied evacuation-center reference "
                    "records are loaded, but their issuing authority and publication "
                    "date have not been verified; they are not enabled as route "
                    "destinations."
                )
            else:
                notices.append("No designated evacuation-center records are loaded.")
        if area is None:
            notices.append(
                "A verified Basey routing study-area polygon has not been loaded."
            )
        elif not area["is_official"]:
            notices.append(
                "The loaded routing boundary is non-authoritative and must not be "
                "used for operational planning."
            )
        if not graph_exists:
            notices.append("The frozen local pedestrian road graph is not available.")
        if nx is None:
            notices.append("The NetworkX runtime dependency is not installed.")
        return {
            "status": "available" if ready else "unavailable",
            "routing_available": ready,
            "scope": (
                f"Limited to the loaded routing study area: {area['name']}"
                if area is not None
                else "No routing study area is loaded"
            ),
            "dependencies": dependencies,
            "evacuation_centers": {
                "loaded_count": len(loaded_centers),
                "verified_count": len(centers),
                "reference_count": len(reference_centers),
            },
            "study_area": (
                {
                    "name": area["name"],
                    "version": area["version"],
                    "source": area["source_name"],
                    "source_date": area["source_date"],
                    "is_official": area["is_official"],
                }
                if area
                else None
            ),
            "road_graph": {
                "path": str(self.config.road_graph_path),
                "source": self.config.road_network_source,
                "date": self.config.road_network_date,
                "network_type": self.config.network_type,
            },
            "center_count": len(centers),
            "notices": notices,
            "disclaimer": self.config.planning_disclaimer,
        }

    def _load_graph(self) -> Any:
        if self._graph is not None:
            return self._graph
        if nx is None:
            raise RoutingError(
                "routing_graph_missing",
                "The local routing engine dependency is unavailable.",
                details={"dependency": "networkx"},
                status_code=503,
            )
        if not self.config.road_graph_path.is_file():
            raise RoutingError(
                "routing_graph_missing",
                "The frozen Basey pedestrian road graph is not loaded.",
                details={"expected_path": str(self.config.road_graph_path)},
                status_code=503,
            )
        try:
            self._graph = nx.read_graphml(self.config.road_graph_path)
        except Exception as exc:
            raise RoutingError(
                "routing_graph_missing",
                "The local routing graph could not be loaded.",
                details={"reason": str(exc)},
                status_code=503,
            ) from exc
        if self._graph.number_of_nodes() == 0 or self._graph.number_of_edges() == 0:
            raise RoutingError(
                "routing_graph_missing",
                "The local routing graph is empty.",
                status_code=503,
            )
        return self._graph

    @staticmethod
    def _node_coordinates(graph: Any, node: Any) -> tuple[float, float]:
        data = graph.nodes[node]
        lon = _as_float(data.get("x", data.get("longitude")))
        lat = _as_float(data.get("y", data.get("latitude")))
        if lon is None or lat is None:
            raise RoutingError(
                "routing_graph_missing",
                "A routing graph node has no valid WGS84 coordinates.",
                details={"node": str(node)},
                status_code=503,
            )
        validate_wgs84_point(lat, lon)
        return lon, lat

    def _nearest_node(
        self, graph: Any, latitude: float, longitude: float
    ) -> tuple[Any, float]:
        nearest = None
        nearest_distance = math.inf
        for node in graph.nodes:
            lon, lat = self._node_coordinates(graph, node)
            distance = haversine_km(latitude, longitude, lat, lon) * 1000.0
            if distance < nearest_distance:
                nearest = node
                nearest_distance = distance
        if nearest is None or nearest_distance > self.config.maximum_snap_distance_m:
            raise RoutingError(
                "no_reachable_center",
                "The point is too far from the frozen pedestrian road network.",
                details={
                    "snap_distance_m": round(nearest_distance, 2),
                    "maximum_snap_distance_m": self.config.maximum_snap_distance_m,
                },
            )
        return nearest, nearest_distance

    @staticmethod
    def _edge_options(graph: Any, u: Any, v: Any) -> Iterable[tuple[Any, Mapping[str, Any]]]:
        data = graph.get_edge_data(u, v)
        if data is None:
            return []
        if graph.is_multigraph():
            return list(data.items())
        return [(None, data)]

    @staticmethod
    def _length(edge: Mapping[str, Any]) -> float:
        length = _as_float(edge.get("length_m", edge.get("length")))
        if length is None or length <= 0:
            raise RoutingError(
                "routing_graph_missing",
                "A road segment has no valid positive length.",
                status_code=503,
            )
        return length

    def _hazard(self, edge: Mapping[str, Any], scenario: str) -> float | None:
        if not _as_bool(edge.get("hazard_complete")):
            return None
        completeness = _as_float(edge.get("hazard_data_completeness"), 1.0)
        if completeness is None or completeness < self.config.minimum_hazard_completeness:
            return None
        field = {
            "multi_hazard": "multi_hazard_score",
            "flood": "flood_score",
            "earthquake": "earthquake_score",
        }.get(scenario)
        if field is None:
            return None
        raw = _as_float(edge.get(field))
        if raw is None:
            return None
        exposure = raw / 100.0 if raw > 1 else raw
        return exposure if 0 <= exposure <= 1 else None

    def edge_cost(
        self,
        edge: Mapping[str, Any],
        *,
        mode: str,
        scenario: str,
    ) -> float | None:
        length = self._length(edge)
        if mode == "shortest":
            return length
        hazard = self._hazard(edge, scenario)
        if hazard is None:
            return None
        return generalized_edge_cost(length, hazard, self.config.risk_factor)

    def _project_graph(self, graph: Any, *, mode: str, scenario: str) -> Any:
        projected = nx.DiGraph() if graph.is_directed() else nx.Graph()
        for node, data in graph.nodes(data=True):
            projected.add_node(node, **dict(data))
        for u, v in graph.edges():
            best: tuple[float, Any, Mapping[str, Any]] | None = None
            for key, edge in self._edge_options(graph, u, v):
                cost = self.edge_cost(edge, mode=mode, scenario=scenario)
                if cost is not None and (best is None or cost < best[0]):
                    best = (cost, key, edge)
            if best is not None:
                projected.add_edge(
                    u,
                    v,
                    routing_cost=best[0],
                    source_key=best[1],
                    source_attributes=dict(best[2]),
                )
        return projected

    def _heuristic(self, graph: Any, target: Any):
        target_lon, target_lat = self._node_coordinates(graph, target)

        def remaining(node: Any, _target: Any) -> float:
            lon, lat = self._node_coordinates(graph, node)
            return haversine_km(lat, lon, target_lat, target_lon) * 1000.0

        return remaining

    @staticmethod
    def _parse_edge_coordinates(value: Any) -> list[list[float]] | None:
        if not value:
            return None
        if isinstance(value, list):
            return [[float(p[0]), float(p[1])] for p in value]
        text = str(value).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "LineString":
            return [[float(p[0]), float(p[1])] for p in parsed["coordinates"]]
        if isinstance(parsed, list):
            return [[float(p[0]), float(p[1])] for p in parsed]
        if text.upper().startswith("LINESTRING"):
            body = text[text.find("(") + 1 : text.rfind(")")]
            try:
                return [
                    [float(part.split()[0]), float(part.split()[1])]
                    for part in body.split(",")
                ]
            except (ValueError, IndexError):
                return None
        return None

    def _route_coordinates(self, graph: Any, projected: Any, nodes: list[Any]) -> list[list[float]]:
        coordinates: list[list[float]] = []
        for u, v in zip(nodes, nodes[1:]):
            chosen = projected[u][v]["source_attributes"]
            segment = self._parse_edge_coordinates(
                chosen.get("geometry_geojson", chosen.get("geometry"))
            )
            if not segment:
                u_lon, u_lat = self._node_coordinates(graph, u)
                v_lon, v_lat = self._node_coordinates(graph, v)
                segment = [[u_lon, u_lat], [v_lon, v_lat]]
            u_lon, u_lat = self._node_coordinates(graph, u)
            first_distance = (segment[0][0] - u_lon) ** 2 + (segment[0][1] - u_lat) ** 2
            last_distance = (segment[-1][0] - u_lon) ** 2 + (segment[-1][1] - u_lat) ** 2
            if last_distance < first_distance:
                segment.reverse()
            coordinates.extend(segment if not coordinates else segment[1:])
        return coordinates

    def _metrics(
        self,
        graph: Any,
        projected: Any,
        nodes: list[Any],
        *,
        scenario: str,
    ) -> dict[str, Any]:
        distance = 0.0
        generalized = 0.0
        exposure_distance = 0.0
        elevated = 0.0
        maximum: float | None = None
        complete_edges = 0
        for u, v in zip(nodes, nodes[1:]):
            link = projected[u][v]
            edge = link["source_attributes"]
            length = self._length(edge)
            hazard = self._hazard(edge, scenario)
            distance += length
            generalized += float(link["routing_cost"])
            if hazard is not None:
                complete_edges += 1
                exposure_distance += hazard * length
                maximum = hazard if maximum is None else max(maximum, hazard)
                if hazard >= self.config.elevated_hazard_threshold:
                    elevated += length
        edge_count = max(0, len(nodes) - 1)
        completeness = complete_edges / edge_count if edge_count else 0.0
        mean = exposure_distance / distance if distance and complete_edges == edge_count else None
        return {
            "distance_m": round(distance, 2),
            "estimated_walk_minutes": round(
                distance / (self.config.walking_speed_kph * 1000.0 / 60.0), 1
            ),
            "generalized_cost": round(generalized, 3),
            "mean_hazard_exposure": round(mean * 100.0, 2) if mean is not None else None,
            "maximum_hazard_exposure": (
                round(maximum * 100.0, 2) if maximum is not None else None
            ),
            "elevated_hazard_distance_m": round(elevated, 2) if complete_edges == edge_count else None,
            "number_of_edges": edge_count,
            "hazard_data_completeness": round(completeness, 4),
            "hazard_data_complete": complete_edges == edge_count,
        }

    def _candidate_route(
        self,
        graph: Any,
        projected: Any,
        start_node: Any,
        center: Mapping[str, Any],
        *,
        mode: str,
        scenario: str,
    ) -> dict[str, Any] | None:
        try:
            center_node, center_snap = self._nearest_node(
                graph, center["latitude"], center["longitude"]
            )
            nodes = nx.astar_path(
                projected,
                start_node,
                center_node,
                heuristic=self._heuristic(projected, center_node),
                weight="routing_cost",
            )
        except RoutingError:
            return None
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return None
        metrics = self._metrics(graph, projected, nodes, scenario=scenario)
        return {
            "destination": dict(center),
            "destination_snap_distance_m": round(center_snap, 2),
            "routing": {
                "algorithm": self.config.algorithm,
                "mode": mode,
                "scenario": scenario,
                **metrics,
            },
            "route": {
                "type": "LineString",
                "coordinates": self._route_coordinates(graph, projected, nodes),
            },
        }

    def _best_route(
        self,
        graph: Any,
        start_node: Any,
        centers: list[dict[str, Any]],
        *,
        mode: str,
        scenario: str,
    ) -> tuple[dict[str, Any], int]:
        projected = self._project_graph(graph, mode=mode, scenario=scenario)
        if projected.number_of_edges() == 0:
            code = "incomplete_hazard_data" if mode != "shortest" else "no_reachable_center"
            raise RoutingError(
                code,
                (
                    "No road segments have complete mapped hazard data for this scenario."
                    if mode != "shortest"
                    else "The local road graph contains no routable segments."
                ),
                details={"mode": mode, "scenario": scenario},
            )
        candidates = [
            candidate
            for center in centers
            if (
                candidate := self._candidate_route(
                    graph,
                    projected,
                    start_node,
                    center,
                    mode=mode,
                    scenario=scenario,
                )
            )
            is not None
        ]
        if not candidates:
            raise RoutingError(
                "no_hazard_aware_route" if mode != "shortest" else "no_reachable_center",
                (
                    "No designated evacuation center is reachable using only road "
                    "segments with complete mapped hazard data."
                    if mode != "shortest"
                    else "No designated evacuation center is reachable on the local graph."
                ),
                details={"mode": mode, "scenario": scenario},
            )
        candidates.sort(
            key=lambda item: (
                item["routing"]["generalized_cost"],
                item["routing"]["distance_m"],
                str(item["destination"]["id"]),
            )
        )
        return candidates[0], len(candidates)

    @staticmethod
    def _comparison(shortest: Mapping[str, Any], lower: Mapping[str, Any]) -> dict[str, Any]:
        shortest_metrics = shortest["routing"]
        lower_metrics = lower["routing"]
        difference = lower_metrics["distance_m"] - shortest_metrics["distance_m"]
        percentage = (
            difference / shortest_metrics["distance_m"] * 100.0
            if shortest_metrics["distance_m"]
            else 0.0
        )
        same_route = shortest["route"]["coordinates"] == lower["route"]["coordinates"]
        if same_route:
            explanation = (
                "The shortest route is also the lowest-cost route under the current "
                "hazard-routing parameters."
            )
        else:
            explanation = (
                f"The lower-hazard route is approximately {abs(round(difference))} "
                f"meters {'longer' if difference >= 0 else 'shorter'} and changes "
                "travel through road segments based on mapped hazard exposure."
            )
        return {
            "same_route": same_route,
            "distance_difference_m": round(difference, 2),
            "distance_increase_percent": round(percentage, 2),
            "explanation": explanation,
        }

    def route(
        self,
        latitude: float,
        longitude: float,
        *,
        mode: str = "hazard_aware",
        scenario: str = "multi_hazard",
        include_comparison: bool = True,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            lat, lon = validate_wgs84_point(latitude, longitude)
        except Exception as exc:
            raise RoutingError("invalid_coordinate", str(exc)) from exc
        aliases = {
            "lower_hazard": "hazard_aware",
            "multi_hazard": "hazard_aware",
        }
        mode = aliases.get(mode, mode)
        if mode not in {"shortest", "hazard_aware"}:
            raise RoutingError(
                "route_generation_failed",
                "mode must be 'shortest' or 'hazard_aware'.",
            )
        if scenario not in {"multi_hazard"}:
            raise RoutingError(
                "route_generation_failed",
                "Only the precomputed multi_hazard planning scenario is available.",
            )
        area = self.repository.routing_study_area()
        if area is None:
            raise RoutingError(
                "outside_routing_area",
                "Detailed evacuation routing requires a loaded Basey routing "
                "study-area boundary, which is not currently loaded.",
                details={"study_area_loaded": False},
                status_code=503,
            )
        if not point_in_geometry(lat, lon, area["geometry"]):
            raise RoutingError(
                "outside_routing_area",
                "Detailed evacuation routing is currently limited to the loaded "
                f"{area['name']} study area.",
                details={"study_area": area["name"], "version": area["version"]},
            )
        loaded_centers = self.repository.evacuation_centers()
        centers = [
            center
            for center in loaded_centers
            if center["is_official"] and point_in_geometry(
                center["latitude"], center["longitude"], area["geometry"]
            )
        ]
        if not centers:
            raise RoutingError(
                "no_evacuation_centers",
                (
                    "No verified active designated evacuation centers are loaded "
                    f"within the {area['name']} routing study area."
                ),
                details={"loaded_center_count": len(loaded_centers)},
                status_code=503,
            )
        graph = self._load_graph()
        start_node, start_snap = self._nearest_node(graph, lat, lon)
        selected, evaluated = self._best_route(
            graph,
            start_node,
            centers,
            mode=mode,
            scenario=scenario,
        )
        routes: dict[str, Any] = {
            "lower_hazard" if mode == "hazard_aware" else "shortest": selected
        }
        warnings: list[str] = []
        if not area["is_official"]:
            warnings.append(
                "The routing study-area boundary is non-authoritative development "
                "data and must be replaced before operational use."
            )
        if not selected["destination"].get("is_official"):
            warnings.append(
                "The destination record is non-authoritative development data and "
                "must be replaced before operational use."
            )
        comparison = None
        if include_comparison:
            other_mode = "shortest" if mode == "hazard_aware" else "hazard_aware"
            other_key = "shortest" if other_mode == "shortest" else "lower_hazard"
            try:
                other, _ = self._best_route(
                    graph,
                    start_node,
                    centers,
                    mode=other_mode,
                    scenario=scenario,
                )
                routes[other_key] = other
                comparison = self._comparison(routes["shortest"], routes["lower_hazard"])
            except RoutingError as exc:
                warnings.append(
                    f"The comparison route is unavailable: {exc}. No fallback was applied."
                )
        selected_key = "lower_hazard" if mode == "hazard_aware" else "shortest"
        selected_route = routes[selected_key]
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        provenance = {
            "routing_config_version": self.config.config_version,
            "road_graph_version": graph.graph.get("snapshot_version", "not_reported"),
            "road_network_source": graph.graph.get(
                "source", self.config.road_network_source
            ),
            "road_network_date": graph.graph.get(
                "acquisition_date", self.config.road_network_date
            ),
            "algorithm": self.config.algorithm,
            "risk_factor": self.config.risk_factor,
            "scenario": scenario,
            "hazard_model_version": graph.graph.get(
                "model_version", self.config.fuzzy_model_version
            ),
            "hazard_data_versions": graph.graph.get(
                "hazard_data_versions",
                json.dumps(dict(self.config.hazard_dataset_versions), sort_keys=True),
            ),
            "study_area_version": area["version"],
            "evacuation_center_dataset_version": self.config.evacuation_center_dataset_version,
        }
        research_metrics = {
            "start": {"latitude": lat, "longitude": lon},
            "selected_destination_id": selected_route["destination"]["id"],
            "selected_destination_name": selected_route["destination"]["name"],
            "route_computation_time_ms": elapsed_ms,
            "shortest": routes.get("shortest", {}).get("routing"),
            "lower_hazard": routes.get("lower_hazard", {}).get("routing"),
            **(comparison or {}),
        }
        return {
            "status": "ok",
            "start": {
                "latitude": lat,
                "longitude": lon,
                "snap_distance_m": round(start_snap, 2),
            },
            "routing_area": {
                "name": area["name"],
                "version": area["version"],
                "source": area["source_name"],
                "is_official": area["is_official"],
            },
            "requested_mode": mode,
            "scenario": scenario,
            "selected_route": selected_key,
            "destination": selected_route["destination"],
            "routing": selected_route["routing"],
            "hazard_exposure": {
                "mean": selected_route["routing"]["mean_hazard_exposure"],
                "maximum": selected_route["routing"]["maximum_hazard_exposure"],
                "elevated_hazard_distance_m": selected_route["routing"][
                    "elevated_hazard_distance_m"
                ],
                "data_complete": selected_route["routing"]["hazard_data_complete"],
            },
            "route": selected_route["route"],
            "routes": routes,
            "comparison": comparison,
            "candidate_centers_evaluated": evaluated,
            "provenance": provenance,
            "research_metrics": research_metrics,
            "warnings": warnings,
            "disclaimer": self.config.planning_disclaimer,
        }
