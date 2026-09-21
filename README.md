# Basafe

Basafe is a focused Web-GIS decision-support prototype for Basey, Samar.
Its backend identifies a selected location and barangay from validated local
snapshots, retrieves locally stored MGB/PHIVOLCS hazard evidence, explains
source and model transformations, and generates a PDF assessment report.
GeoRisk/ULAP is used by a separate operator-run synchronization command, not
by ordinary assessment requests.

The codebase also contains a shared local OpenStreetMap snapshot for street/POI
search and an A* routing subsystem. The public interface presents one standard
walking route to the nearest reachable designated evacuation center inside the
loaded Basey routing study area; hazard-aware comparison remains internal
research code. The active study area is the full Basey municipal boundary
(official PSA geometry, already used for containment checks elsewhere in the
app); a reproducible seven-barangay town-proper composite
(`scripts/build_town_proper_boundary.py`, covering Mercado, Palaypay, Baybay,
Sulod, Loyo, Buscada, and Lawa-an) remains available as a narrower,
researcher-defined alternative. Operational routing uses the locally imported
Basey MDRRMO evacuation-center register, the frozen walking graph, and the
local search index. The source register and its ingestion metadata are
retained so developers can audit the configured centers; the application does
not invent destinations.

The application has a public information site and a unified assessment map. It intentionally contains no user
accounts, roles, permissions, staff dashboards, administrative portal,
approval workflow, browser upload manager, or model editor.

## Prototype status

The repository has no operational synthetic-data seed. Normal server mode is
`snapshot`: runtime boundary and hazard evidence comes from validated records
in `data/geosafe.db`. Sanitized responses and synthetic geometry exist only as
isolated automated-test fixtures and are rejected by normal server mode.

Live service/layer metadata for flood, liquefaction, ground shaking, municipal
boundary, and barangay boundary was independently checked in 2026. The runtime
database contains Basey-local hazard snapshots: MGB flood polygons, PHIVOLCS
liquefaction polygons, and a limited-quality ground-shaking grid derived from
four official 2014 Region VIII deterministic-scenario maps. Historical
incidents and adopted CLUP references remain unavailable until authorized
local records are imported.

Fuzzy model `0.5.5-demo`, including its source-code transformations, is not
domain validated.

## Run locally

Use Python 3.11 or newer and install the declared runtime dependencies:

```powershell
python -m pip install -e .
python -m geosafe.server
```

A clean clone contains `data/geosafe.snapshot.db`, a sanitized, read-only
developer snapshot with no assessment or report history. On the first local
run, the server copies it to the ignored writable file `data/geosafe.db`.
Operators can then refresh that writable database with the synchronization and
import commands below without changing the published snapshot.

Open <http://127.0.0.1:8000>. The browser loads Leaflet, OpenStreetMap street
tiles, and optional Esri World Imagery/World Topographic basemaps from their
public services with visible attribution. Basemaps are visual context only and
do not supply hazard classifications. If those external resources are unavailable,
coordinate-based assessment remains available and the interface displays a
map availability notice. Hazard overlays remain independent of the selected
basemap and are rendered from activated local snapshot features.

Before the first snapshot-mode run, deliberately synchronize each currently
supported source:

```powershell
python scripts/sync_ulap_snapshot.py `
  --target municipal_boundary `
  --target barangay_boundary `
  --target flood `
  --target liquefaction `
  --target ground_shaking
```

The command validates the complete response before atomically replacing the
matching local snapshot. A failed synchronization preserves the previous
working data. When an ArcGIS layer rejects its advertised `Query` operation,
the synchronizer can use the same official MapServer's `identify` operation.
Ground shaking is synchronized from a fixed allowlist of official PHIVOLCS
Region VIII scenario KMZs and is labelled limited-quality derived data.

The server initializes the approved schema and loads the versioned fuzzy and
ULAP registries. Configure `ULAP_LIVE_VALIDATION=true` only when deliberate
startup metadata validation is wanted.

## Deploy to Vercel

The repository includes a Python WSGI function and static routing configuration
for Vercel. The function copies `data/geosafe.snapshot.db` into its writable
`/tmp` directory before initializing SQLite. This supports the public map and
assessment workflow, but Vercel instance-local assessment records are
ephemeral and must not be treated as durable storage.

Rebuild the privacy-safe deployment snapshot after updating local source data:

```powershell
python scripts/build_deployment_snapshot.py --force
```

Then validate and deploy:

```powershell
python -m unittest discover -s tests -v
vercel build
vercel --prod
```

For durable server-side assessment history, replace the SQLite write path with
a managed relational database before operational deployment.

## Test

```powershell
python -m unittest discover -s tests -v
```

Deliberate live metadata smoke check:

```powershell
python scripts/verify_ulap_services.py `
  --boundary-file "C:\path\to\basey-barangay-boundary-final.json"
```

The metadata smoke check verifies source schemas; Basey coverage and snapshot
quality are reported separately. Network integration tests are opt-in:

```powershell
$env:LIVE_ULAP_TESTS = "true"
python -m unittest tests.test_ulap_live -v
```

The test suite covers:

- ULAP registry, allowlisted URLs, ArcGIS errors, metadata/domain decoding,
  cache/retry behavior, and provider contracts with sanitized fixtures;
- spatial containment and live-provider barangay identification;
- complete and incomplete assessment workflows;
- membership functions, rule activations, score bounds, and categories;
- source status, fixture isolation, and missing-data behavior;
- PDF report generation and stored snapshots;
- GeoJSON/CSV import behavior and reprojection;
- the HTTP and frontend contracts; and
- A* shortest/lower-hazard routing, cost monotonicity, MultiDiGraph edges,
  missing-hazard rejection, center reachability/ranking, GeoJSON order, and
  zero-download runtime isolation; and
- negative scope checks proving that user, role, permission, admin, audit, and
  registration APIs/tables are absent.

## Data import

GeoJSON and coordinate CSV import require only Python. EPSG:4326 and EPSG:3857
are supported natively. Shapefile/GeoPackage and other coordinate systems can
be processed when Fiona/GDAL and PyProj are installed. Classified GeoTIFF data
must be inspected/vectorized with a GIS tool or Rasterio and imported with its
original raster metadata preserved.

See [data import](docs/data-import.md) and the
[command reference](scripts/README.md). Example:

```powershell
python scripts/import_dataset.py barangays path/to/basey_barangays.geojson `
  --db data/geosafe.db `
  --source-name "Issuing organization and dataset title" `
  --source-date "YYYY-MM-DD" `
  --data-classification official `
  --quality-status verified `
  --source-crs EPSG:4326 `
  --name-field barangay_name `
  --psgc-field psgc_code `
  --replace --strict
```

Declaring a dataset `official` records the deployer's explicit provenance
classification; the software cannot independently authenticate its issuer.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEOSAFE_HOST` | `127.0.0.1` | HTTP bind address |
| `GEOSAFE_PORT` | `8000` | HTTP port |
| `GEOSAFE_DB_PATH` | `data/geosafe.db` | SQLite database |
| `GEOSAFE_MODEL_PATH` | `config/fuzzy_model.json` | Versioned fuzzy model |
| `GEOSAFE_SCHEMA_PATH` | `db/schema.sql` | Approved database schema |
| `GEOSAFE_ROUTING_CONFIG` | `config/routing.json` | Versioned local routing configuration |
| `GEOSAFE_WEB_ROOT` | `web` | Unified static interface |
| `GEOSAFE_RUNTIME_DATA_MODE` | `snapshot` | `snapshot` for network-independent assessments; `live` only for diagnostics/legacy operation |
| `GEOSAFE_LOG_LEVEL` | `INFO` | Lightweight technical logging |
| `GEOSAFE_API_REQUESTS_PER_MINUTE` | `240` | Per-client general API request limit |
| `GEOSAFE_ASSESSMENTS_PER_MINUTE` | `12` | Per-client assessment creation limit |
| `ULAP_HAZARDS_BASE_URL` | GeoRisk hazards REST root | Allowlisted hazard service base |
| `ULAP_NGA_BASE_URL` | GeoRisk NGA REST root | Allowlisted boundary service base |
| `ULAP_TOKEN` | empty | Optional server-only ArcGIS token |
| `ULAP_REQUEST_TIMEOUT_SECONDS` | `15` | Per-request timeout |
| `ULAP_MAX_RETRIES` | `2` | Retries after the initial request |
| `ULAP_METADATA_CACHE_SECONDS` | `86400` | In-process metadata TTL |
| `ULAP_QUERY_CACHE_SECONDS` | `3600` | In-process query TTL |
| `ULAP_LIVE_VALIDATION` | `false` | Validate configured metadata at startup |
| `ULAP_SERVICES_CONFIG` | `config/ulap-services.generated.json` | Version-controlled source registry |

Authentication is not implemented in this prototype. If deployment later
requires basic perimeter protection, that should be a separately approved
single shared application password—not accounts, roles, or permissions.

Assessment history is device-local: saved records are addressed by unguessable
tokens retained in browser storage, and the API does not expose a shared
assessment listing.

## Documentation

- [Architecture and scope](docs/architecture.md)
- [API contract](docs/api.md)
- [Fuzzy methodology](docs/fuzzy-methodology.md)
- [Implementation plan](docs/implementation-plan.md)
- [Acceptance criteria](docs/acceptance-criteria.md)
- [Dataset import procedure](docs/data-import.md)
- [ULAP integration](docs/ulap-integration.md)
- [Supplied JSON and live-service audit](docs/ulap-json-audit.md)
- [Live field mappings](docs/ulap-field-mappings.md)
- [Source register](docs/data-sources.md)
- [Fuzzy source transformations](docs/fuzzy-data-transformations.md)
- [Error and availability handling](docs/ulap-error-handling.md)
- [Known data gaps](docs/known-data-gaps.md)
- [Hazard-aware routing architecture and data requirements](docs/routing.md)
- [Local OpenStreetMap search and shared snapshot](docs/osm-search.md)

## Disclaimer

This report is a preliminary multi-hazard screening output based on selected
available data. It does not certify that a location is safe or unsafe and does
not replace official hazard, planning, engineering, geological, geotechnical,
or regulatory assessment.
