# Basafe hazard-aware evacuation routing

## Status

The routing software, APIs, preprocessing pipeline, UI, and synthetic tests are
implemented. The active routing study boundary is the full Basey municipal
polygon (official PSA geometry, `routing_study_areas` version
`basey-municipal-boundary-v1`). A narrower, reproducible composite of the
loaded official PSA polygons for Mercado, Palaypay, Baybay, Sulod, Loyo,
Buscada, and Lawa-an remains available via
`scripts/build_town_proper_boundary.py` and can be reactivated with
`scripts/import_routing_data.py study-area --replace`; that seven-barangay
grouping was defined by the project researcher and is not represented as an
independently issued town-proper boundary.

The frozen local OSM walking graph now covers the whole active study area and
is shared with street/place search; because Basey's road/path network is
sparse away from the town center, the graph has several disconnected
components, so "nearest reachable center" is still relative to whichever
component a selected point snaps onto. A photographed inventory acquired
directly from the Basey MDRRMO was transcribed into 17 coordinate rows that
normalize to nine in-scope facilities inside the town-proper area, loaded as
an operator-declared official source and enabling center search and
shortest-route generation there. The source photograph and publication date
are not yet archived, so this provenance limitation remains explicit.
Facilities matched from the municipality's 2025 MSWDO evacuation-center list
against OpenStreetMap-sourced coordinates are loaded as `supplied-reference`
(not `is_official`), so they appear in `/api/v1/evacuation-centers` for
review but are not yet usable as route destinations.

The repository does not substitute the municipal boundary, invent centers, or
download roads during a route request.

## Architecture

```text
Researcher-defined seven-barangay composite
        +
OSM/approved road source -- explicit operator sync --> local GraphML
        +                                           |
local BaSafe hazard snapshots -- offline enrichment-+
                                                    |
designated centers -- verified import --> SQLite    |
                                                    v
                                  HazardAwareRouter + A*
                                                    |
                                      stateless route API
                                                    |
                                      existing Leaflet map
```

Normal runtime route generation loads the cached graph, snaps the start and
centers, evaluates every reachable designated center with NetworkX A*, computes
metrics, and returns GeoJSON. It does not call OSM, Overpass, OSMnx downloads,
or an external routing API.

## Cost model

Shortest mode uses:

```text
C_e = D_e
```

Lower-Hazard mode uses:

```text
C_e = D_e (1 + lambda H_e)
```

where `D_e` is edge length in metres, `H_e` is precomputed mapped exposure in
`[0,1]`, and `lambda` is `risk_factor` in `config/routing.json`. `lambda` must
be non-negative, so increasing exposure can never decrease cost. Straight-line
remaining distance is the A* heuristic and remains admissible because the
generalized edge cost is never below distance.

Missing hazard data uses the configured `reject_edge` policy. It is not zero.
The hazard-aware projected graph excludes incomplete edges; the API reports
`incomplete_hazard_data` or `no_hazard_aware_route` rather than silently
falling back to shortest mode.

## Configuration and provenance

`config/routing.json` versions the algorithm, pedestrian network type, risk
factor, walking speed, elevated-exposure threshold, snap limit, completeness
threshold, missing-data policy, study-area version, graph source/date, fuzzy
model version, hazard versions, center version, and disclaimer. Startup rejects
a fuzzy-model version mismatch.

Every successful response contains graph/config/model/data/center/study-area
provenance and computation time. Public route requests are stateless and are
not written to a movement-history table.

## Data preparation

1. Rebuild the project-defined boundary from the seven loaded PSA barangays:

   ```powershell
   python scripts/build_town_proper_boundary.py --db data/geosafe.db --replace
   ```

   The script requires exactly one official, non-demo match for each named
   barangay, preserves all Polygon/MultiPolygon members without dissolving or
   simplifying them, and records every component name, PSGC code, and source.
   `scripts/import_routing_data.py study-area` remains available if an issuing
   authority later supplies an independently verified town-proper boundary.

2. Normalize the MDRRMO-sourced DMS inventory and import its town-proper scoped
   extract:

   ```powershell
   python scripts/normalize_evacuation_centers.py `
     data/routing/source/basey_town_proper_evacuation_centers_source.csv
   python scripts/import_routing_data.py centers `
     data/routing/basey_town_proper_evacuation_centers.csv `
     --db data/geosafe.db `
     --version "mdrrmo-town-proper-scoped-extract-2026-08-18-v1" `
     --source-name "Municipal Disaster Risk Reduction and Management Office of Basey (MDRRMO)" `
     --data-classification official --replace
   ```

   The authority classification is based on the researcher's statement that the
   photographed source was acquired directly from the MDRRMO. The photograph
   contained unrelated out-of-scope facilities, which were not transcribed.
   Archive the original image when available and add its document/publication
   date without changing the facility coordinates unless the issuer confirms a
   correction.

3. Explicitly acquire the pedestrian network (operator-only; requires the
   `routing-prep` extra):

   ```powershell
   uv run --extra routing-prep python scripts/sync_osm_network.py --db data/geosafe.db `
     --snapshot-version "osm-YYYY-MM-DD-v1" --allow-non-authoritative
   ```

   The explicit flag acknowledges that the project researcher, rather than an
   issuing authority, defined the seven-barangay grouping. It does not change
   the boundary's stored classification.

4. Enrich it from local BaSafe data without assessment-history writes:

   ```powershell
   python scripts/enrich_route_hazards.py --db data/geosafe.db
   ```

   For a center-only refresh after a center import:

   ```powershell
   python scripts/enrich_route_hazards.py --db data/geosafe.db --centers-only
   ```

5. Rebuild the deployment snapshot.

The enrichment samples each edge, calls the existing BaSafe point/FIS
evaluation with `persist=False` and `local_only=True`, and stores supported
hazard inputs, multi-hazard score, mean/maximum, completeness, sample count,
model version, and hazard-data version on the edge. Runtime requests never
repeat that enrichment.

## API

- `GET /api/v1/routing/status`
- `GET /api/v1/evacuation-centers`
- `POST /api/v1/route`

The public application and API expose one `shortest` walking route to the nearest
reachable designated center; `include_comparison` is disabled. The hazard-aware
comparison implementation remains available only for internal research and test
evaluation. Public errors include `invalid_coordinate`, `outside_basey`,
`outside_routing_area`, `routing_graph_missing`, `no_evacuation_centers`,
`no_reachable_center`, and `route_generation_failed`.

## Research export

`scripts/evaluate_routing_cases.py` accepts a CSV of test cases and exports
distance, walking-time estimate, mean/maximum exposure, elevated-exposure
distance, completeness, destination, computation time, and shortest-versus-
hazard-aware differences. These are measurements, not a claim of superiority.

## Scientific limitations

- `multi_hazard` is a planning/screening scenario; it does not mean all hazards
  are occurring simultaneously.
- A designated evacuation center remains an official designation separate from
  its mapped hazard screening.
- Routes do not model real-time closures, water depth, structural damage,
  traffic, crowds, passability, or official orders.
- Estimated walking time uses configurable constant speed and is approximate.
- The public interface presents a standard evacuation route and does not imply
  that any computed path is guaranteed safe.
