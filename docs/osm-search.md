# Local OpenStreetMap search

Basafe uses one explicitly synchronized OpenStreetMap snapshot for both
place discovery and evacuation routing across the active routing study area.
Normal user requests never contact Overpass, Nominatim, OSMnx, or an external
routing service.

## Shared snapshot

Run the operator-only synchronization with the routing-preparation extra:

```powershell
uv run --extra routing-prep python scripts/sync_osm_network.py `
  --snapshot-version osm-YYYY-MM-DD-v1 --allow-non-authoritative
```

The command reads the active routing study boundary and writes:

- `data/routing/basey_walk.graphml` for NetworkX A*;
- `data/routing/basey_roads.geojson` for search geometry and QGIS;
- `data/routing/osm_snapshot_metadata.json` for acquisition and quality data;
- normalized street and allowlisted POI records in `searchable_locations`.

The active study area's stored geometry is retained as-is in SQLite (whether a
single municipal polygon or a multi-barangay composite). A MultiPolygon whose
touching members make it invalid under OGC rules is dissolved into a valid
polygonal union only for the OSMnx query; the stored component geometry and
provenance are never altered by that query-time union.

## Runtime search

`GET /api/v1/location/search?q=...` and its `/api/search` alias rank exact,
prefix, partial, and alternative-name matches from SQLite. Results can be
filtered by `type=street|poi|place|evacuation_center|barangay|coordinate`.
Evacuation centers and barangays retain their own source tables rather than
being needlessly duplicated in the OSM index.

Street selection highlights the complete stored geometry and asks the user to
choose a precise point. It does not calculate a single score for a whole road.
The chosen point remains the same selected-location state used by assessment
and routing.

## Scientific and data limitations

Road names, POIs, connectivity, and geometry reflect the OSM snapshot date and
community-contributed coverage. Unnamed roads are routable but not searchable
by invented names. House numbers are not indexed. Only configured
public-interest POI categories are indexed.

Street-level search allows users to locate mapped roads and places more
precisely, but it does not increase the spatial resolution or scientific
precision of the underlying hazard datasets.

Road/search source: © OpenStreetMap contributors.
