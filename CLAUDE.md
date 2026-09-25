# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Basafe is a Web-GIS decision-support prototype for Basey, Samar. Given a
selected point, it identifies the containing barangay, retrieves locally
stored flood/liquefaction/ground-shaking hazard evidence, runs a versioned
fuzzy-inference model, and generates an explainable PDF vulnerability
screening report. It also includes a local OpenStreetMap search index and an
A* pedestrian router that finds one walking route to the nearest reachable
designated evacuation center within a fixed Basey town-proper study area.

The project deliberately has **no accounts, roles, permissions, or
general admin/CMS surface** in its public-facing scope (see "Scope gate"
below) — the public product is one unified map/assessment interface, not a
multi-role platform. A separate, isolated, dev-only admin dashboard
(`geosafe/admin.py`, `web/admin/`) exists for local operational visibility
only; see "Admin dashboard (local dev only)" below before touching it.

## Commands

Requires Python 3.11+ (`.python-version` pins 3.12). Dependencies are managed
with `uv` (`uv.lock` present) or plain `pip`; scripts under `scripts/` invoke
`uv run`.

```powershell
# Install
python -m pip install -e .
python -m pip install -e ".[gis]"           # adds pyproj/shapely for non-4326/3857 imports

# Run the public app (copies data/geosafe.snapshot.db -> data/geosafe.db on first run)
python -m geosafe.server
# or, from an existing .venv:
.\scripts\run_local_dev.ps1 [-Port 8000]

# Run the full test suite
python -m unittest discover -s tests -v

# Run a single test module / class / test
python -m unittest tests.test_fuzzy -v
python -m unittest tests.test_fuzzy.FuzzyModelTests.test_specific_case -v

# Opt-in live ULAP network integration tests (normally skipped)
$env:LIVE_ULAP_TESTS = "true"
python -m unittest tests.test_ulap_live -v

# Metadata-only smoke check against the live ArcGIS services
python scripts/verify_ulap_services.py --boundary-file "C:\path\to\boundary.json"

# Deliberately refresh local hazard/boundary snapshots from ULAP (ordinary requests never call ULAP)
python scripts/sync_ulap_snapshot.py --target municipal_boundary --target barangay_boundary --target flood --target liquefaction --target ground_shaking

# Import a dataset (see scripts/README.md and docs/data-import.md for all flags)
python scripts/import_dataset.py barangays path/to/file.geojson --db data/geosafe.db --source-name "..." --source-date "YYYY-MM-DD" --data-classification official --quality-status verified --source-crs EPSG:4326 --name-field barangay_name --psgc-field psgc_code --replace --strict

# Rebuild the privacy-safe deployment snapshot (strips assessment history) before deploying
python scripts/build_deployment_snapshot.py --force

# Deploy
vercel build
vercel --prod
```

Local admin dashboard (isolated dev environment, port 8001 by default):

```powershell
# One-time: copy .env.admin.example -> .env.admin.local and set a local password
.\scripts\run_admin_dev.ps1
# then open http://127.0.0.1:8001/admin
```

## Architecture

Framework-free: the whole backend is Python standard library
(`http.server`) plus `networkx`, `pydantic`, `Pillow`, and `reportlab`. There
is no web framework, ORM, or build step for the backend. The frontend is
static HTML/CSS/vanilla JS served directly from `web/` — no bundler, no
frontend framework.

Request flow is a straight layered pipeline, each layer only calling the one
below it:

```
geosafe/server.py   HTTP server: routing, static files, cookies/rate limiting,
                     dispatches /api/* to Api, /admin/* to AdminDashboard
        v
geosafe/api.py       Endpoint allowlist -> validates/parses query params,
                     calls GeoSafeService, wraps results as JSON Response
        v
geosafe/service.py   Core orchestration (largest module, ~2100 lines):
                     point validation, barangay identification, hazard
                     retrieval, fuzzy assessment execution, incident/CLUP
                     context, report generation orchestration
        v
geosafe/repository.py  All SQLite access (parameterized), schema init,
                     transactions, provenance-preserving reads/writes
        v
data/geosafe.db      SQLite (schema in db/schema.sql)
```

Supporting modules used by `service.py`:
- `geosafe/fuzzy.py` — loads and validates `config/fuzzy_model.json` (versioned
  Mamdani fuzzy model: variables, membership functions, rules, weights,
  centroid defuzzification, score bands); the model config drives behavior,
  not the code.
- `geosafe/entropy_weight.py` — entropy-based rule/variable weighting used by
  the fuzzy model.
- `geosafe/geometry.py` — WGS84 point/polygon validation, point-in-geometry,
  bounding boxes; no spatial database extension is used, containment checks
  scan loaded GeoJSON in memory.
- `geosafe/study_area.py` — the fixed town-proper routing study-area polygon.
- `geosafe/routing.py` — loads a frozen local pedestrian graph (`networkx`)
  and runs A* to the nearest reachable evacuation center; stateless, no
  runtime OSM calls.
- `geosafe/search.py` — text normalization for the local OSM street/POI index.
- `geosafe/pdf.py` — renders the immutable saved assessment snapshot into a
  PDF (`reportlab`); never reruns spatial lookup or fuzzy inference.
- `geosafe/ulap/` — the ArcGIS/GeoRisk ULAP live-service client: allowlisted
  HTTPS calls (two hosts only), metadata validation, TTL caching, retries,
  response parsing per hazard/boundary provider. Only used by the explicit
  `scripts/sync_*.py` synchronization commands and by `GEOSAFE_RUNTIME_DATA_MODE=live`
  diagnostics — **not** by ordinary assessment requests.

### Snapshot vs. live data mode

`GEOSAFE_RUNTIME_DATA_MODE=snapshot` (default) means all hazard/boundary data
served to users comes from `data/geosafe.db`, populated ahead of time by the
`sync_ulap_snapshot.py` / `import_dataset.py` scripts. `live` mode queries
ULAP directly per request and exists only for diagnostics/legacy operation.
A clean clone ships `data/geosafe.snapshot.db` (sanitized, no assessment
history); the server copies it to the gitignored `data/geosafe.db` on first
run. `scripts/build_deployment_snapshot.py` produces a fresh
`geosafe.snapshot.db` from `geosafe.db` with assessment history stripped —
run it before deploying if source data changed.

### Database

SQLite, schema in `db/schema.sql`, applied by `Repository.initialize()`.
Foreign keys are enabled; imports and assessments use transactions. Tables
split into: spatial/reference data (`municipal_boundary`, `barangays`,
`hazard_datasets`, `hazard_features`, `historical_incidents`,
`clup_references`, `evacuation_centers`, `searchable_locations`,
`routing_study_areas`), fuzzy model config (`fuzzy_models`,
`fuzzy_variables`, `membership_functions`, `fuzzy_rules`), and per-assessment
snapshots (`assessments`, `assessment_inputs`, `assessment_memberships`,
`assessment_rule_activations`, `assessment_results`, `generated_reports`).
Assessment snapshots freeze the model version and inputs used, so later data
or config changes never retroactively alter a saved explanation. There are
intentionally no user/role/permission/session tables in this schema.

### Scope gate

`docs/architecture.md` §1–2 defines an explicit scope gate: every feature
must support one of a fixed list of approved functions (data integration,
location/barangay selection, hazard visualization, fuzzy assessment,
explainability, incident/CLUP context, data-quality communication,
recommendations, report generation, evacuation-route comparison, citizen
damage reporting). The public product explicitly excludes user accounts,
roles/permissions, approval workflows, staff dashboards, and a general
admin/CMS. Citizen damage reporting (`/report-damage`, `POST /api/v1/reports`)
is a deliberate, documented exception: the one public write/upload path. It
is submit-only, needs no account, stores the reporter's name and phone with
consent, is visible only to responders in the admin dashboard, and is refused
on Vercel and on the admin server. Report logic lives in `geosafe/admin.py`
(`submit_citizen_report` and related methods) and is routed in
`geosafe/server.py`. When adding a
public-facing feature, check it against this gate before assuming it belongs
in `web/`, `geosafe/api.py`, or `geosafe/service.py`.

### Admin dashboard (local dev only)

`geosafe/admin.py` + `web/admin/` implement a **separate, isolated,
mostly-read** operational dashboard (dataset inventory, evacuation-center
editing/publishing, hazard-event log, visitor analytics, citizen-report
triage) documented in
`docs/admin-development.md`. It runs against its own gitignored database
(`data/admin-dev.db`), its own port (8001 via `scripts/run_admin_dev.ps1`),
and never touches the Vercel deployment. It uses server-side session cookies
(not the "no accounts" public app) and is explicitly scoped to not include
dataset activation or deletion — see the last section of
`docs/admin-development.md` for what is deliberately unimplemented and for
the two exceptions to "never touches the public app on port 8000". Both
work through `_publish_target_repository()` on `data/geosafe.db`: the
explicit, validated, audit-logged **Publish** action for evacuation centers,
and reading or triaging citizen damage reports (status changes only). The
Publish action never reaches the live
Vercel deployment, which reads an ephemeral per-cold-start copy of
`data/geosafe.snapshot.db`, not `data/geosafe.db` directly — going live still
requires the existing manual `scripts/build_deployment_snapshot.py` +
`vercel --prod` steps. Do not conflate this dashboard with the public scope
gate above; it is a developer tool, not part of the approved public
interface.

### Vercel deployment

`api/index.py` is the WSGI entry point; `vercel.json` maps `/api/*` there and
static/page routes to files under `web/`. The function copies
`data/geosafe.snapshot.db` into its writable `/tmp` at cold start — instance
storage is ephemeral, so anything written at runtime (new assessments) does
not persist across invocations/regions.

### Testing conventions

`tests/helpers.py` provides `DemoApplication`, which builds a fresh temp
SQLite DB from `db/schema.sql` + `config/fuzzy_model.json` and seeds minimal
fixtures — most tests build one of these rather than touching
`data/geosafe.db`. `tests/test_scope.py` enforces the scope gate at the
schema/API/frontend level (asserts no user/role/admin tables or endpoints
leak into the public surface). ULAP tests use sanitized fixture payloads
except `tests/test_ulap_live.py`, which is opt-in via `LIVE_ULAP_TESTS=true`
and makes real network calls.

## Key docs

- [docs/architecture.md](docs/architecture.md) — scope, workflow, DB schema, API boundaries, fuzzy engine contract
- [docs/api.md](docs/api.md) — full API contract
- [docs/routing.md](docs/routing.md) — routing graph/data requirements
- [docs/osm-search.md](docs/osm-search.md) — local OSM search/snapshot
- [docs/ulap-integration.md](docs/ulap-integration.md), [docs/ulap-field-mappings.md](docs/ulap-field-mappings.md), [docs/known-data-gaps.md](docs/known-data-gaps.md) — live-source integration details
- [docs/data-import.md](docs/data-import.md), [scripts/README.md](scripts/README.md) — dataset import procedures
- [docs/admin-development.md](docs/admin-development.md) — local admin dashboard setup and isolation boundaries
