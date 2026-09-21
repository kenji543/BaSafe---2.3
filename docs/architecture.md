# Basafe Architecture

## 1. Purpose and scope

Basafe is a focused Web-GIS decision-support prototype for screening locations in Basey, Samar. It combines available flood, liquefaction, and ground-shaking information with verified municipal and barangay sources, historical incident context, and relevant Comprehensive Land Use Plan (CLUP) references. It then runs a documented fuzzy-inference model and explains the resulting vulnerability screening score.

This document describes application package version `0.5.0` and demonstration model version `0.5.1-demo`.

Every feature must directly support at least one of the following:

- data integration;
- location selection;
- barangay identification;
- hazard visualization;
- fuzzy assessment;
- explainability;
- historical incident context;
- CLUP context;
- data-quality communication;
- planning-oriented recommendations; or
- report generation.
- pedestrian evacuation-route comparison, within the loaded routing study area, using frozen local data.

Anything that fails this scope gate is not part of the application.

## 2. Explicit exclusions

These exclusions describe the **public-facing product**: the unified
interface reachable by ordinary users at the deployed origin. The prototype's
public product has one unified interface and does not implement:

- administrators, analysts, technical staff, planning viewers, public viewers, or any other user type;
- user registration, multiple accounts, users, roles, permissions, or role-based access control;
- account approval, dataset approval, or publication workflows;
- staff or office-specific dashboards for the public product;
- user activity monitoring, permission matrices, or application audit-log screens exposed to end users;
- browser-based dataset or fuzzy-model management reachable from the public product;
- a general-purpose content-management system; or
- separate interfaces for municipal offices.

Authentication is not a core requirement of the public product and the initial prototype is unauthenticated there. If a later deployment requires basic perimeter protection for the public product, a single shared password may be read from an environment variable. That optional protection is not implemented in the initial prototype and must not introduce registration, multiple accounts, roles, permissions, user tables, or management pages in the public product.

Section 3.1 documents a separate, isolated, local-only operator dashboard used
for development visibility. It is not part of the public product, is not
reachable from the public origin or the Vercel deployment, and does not
relax any exclusion above for the public product itself.

## 3. System context

The target implementation intentionally uses a small number of components:

| Component | Responsibility |
| --- | --- |
| Static browser application | Public education landing page at `/`, unified Leaflet assessment interface at `/map`, device-local recent history, and public methodology/source/limitations pages |
| Python standard-library JSON API | Spatial lookup orchestration, dataset retrieval, assessment execution, explanation payloads, report generation, and supporting-information endpoints |
| Backend ULAP integration | Allowlisted ArcGIS REST client, live metadata validation, PSA Basey/barangay identification, hazard point queries, typed source results, retries, and TTL caching |
| SQLite database | Approved spatial records, provenance, fuzzy configuration records where seeded, assessments, explanations, and generated-report metadata |
| Version-controlled fuzzy configuration | `config/fuzzy_model.json`: model variables, membership functions, rules, weights, outputs, thresholds, defuzzification method, version, and validation notes |
| Command-line import utilities | Validate, transform, and load deployment datasets while recording provenance and errors |
| Local routing service | Load a frozen walking graph for the active routing study area, compare shortest and mapped-hazard-aware A* routes to every reachable designated center, and return stateless GeoJSON plus reproducibility metadata |
| Local OSM search service | Rank normalized street/POI records stored from the same frozen walking-network synchronization and return inspectable GeoJSON without runtime geocoding |
| PDF report renderer | Produce a repeatable assessment document containing the result, explanation, sources, quality notices, limitations, and disclaimer |
| Local admin dashboard *(isolated, not part of the public product; see 3.1)* | Read-mostly operational visibility for a local developer: dataset/routing/context inventory, visitor analytics, evacuation-center photo upload |

The browser is a client of the JSON API. It does not call ULAP directly, contain authoritative hazard values, expose an ArcGIS token, or independently calculate the final score. The API validates live source metadata, resolves the selected point, obtains available source records, applies the exact configured model only when all required inputs are valid, and persists enough detail to reproduce the explanation.

### 3.1 Local admin dashboard (isolated developer tool)

A local, read-mostly operational dashboard gives a developer visibility into
the running system without becoming part of the public product described
elsewhere in this document. It is implemented by `geosafe/admin.py` and
`web/admin/` and documented operationally in
[admin-development.md](admin-development.md). It is deliberately excluded
from the scope gate in Section 1 and from the public interface in Section 4:
it exists to inspect the local worktree, not to extend what end users can
reach.

Isolation boundaries:

- Runs in the same local development worktree, on its own port
  (`http://127.0.0.1:8001/admin` by default), started only by
  `scripts/run_admin_dev.ps1`.
- Reads and writes its own private, gitignored SQLite database
  (`data/admin-dev.db`), copied once from the bundled snapshot. It never
  connects to the public local application on port 8000, to the public
  Vercel deployment, or to `data/geosafe.db`.
- Uses server-side session cookies (random session id, HTTP-only, SameSite,
  eight-hour inactivity expiry, invalidated on restart, rate-limited sign-in)
  gated behind `/admin/login`. This authentication model is specific to the
  admin dashboard; it does not apply to, and is not required by, the public
  product's unauthenticated design in Section 2.
- Enabled only in the isolated environment; visitor analytics additionally
  require `GEOSAFE_VISITOR_ANALYTICS_ENABLED=true`.

Pages, each a dedicated authenticated route: `/admin` (overview/action
queue), `/admin/analytics` (anonymous visitor counts and a seven-day
activity graph, keyed by a random first-party browser identifier — no
accounts, no raw IP persistence), `/admin/datasets` (hazard dataset
inventory), `/admin/evacuation-centers` (facility inventory and photo
upload), `/admin/routing` (routing dependency and study-area status),
`/admin/context` (historical incident, CLUP, barangay, and boundary counts),
and `/admin/activity` (recent scoring activity).

The dashboard deliberately does not implement dataset activation, record
deletion, or production synchronization; those remain command-line
operator actions (Section 3, "Command-line import utilities") pending
managed authentication, roles, versioned staging, validation, backup, and
rollback design suitable for a networked deployment. A remotely hosted
administration system would additionally need HTTPS, managed accounts,
role-based authorization, password recovery, persistent session storage,
CSRF protection, and an auditable identity provider — none of which this
local tool provides or claims to.

## 4. Approved pages

Sections 4–10 below describe the public product only. The local admin
dashboard (Section 3.1) has its own pages, API surface, and database, and is
out of scope for the approved-pages list, the API boundaries in Section 8,
and the database table list in Section 7.

The static application exposes only these project pages or panels:

1. **Main Web-GIS Assessment**: interactive map, location search, coordinate input, pin/map-click selection, Basey containment check, barangay identification, hazard layers, layer controls, legends, and assessment action.
2. **Assessment Results**: selected point, barangay, raw and normalized hazard values, memberships, activated rules and strengths, score/category or incomplete state, incident and CLUP context, provenance, quality and missing-data notices, cautious recommendations, and disclaimer.
3. **Assessment Report Preview**: the exact substantive content intended for the PDF and a report-generation action.
4. **Methodology and Limitations**: model version, variables, membership functions, rules, source catalogue, limitations, validation status, and disclaimer.
5. **Assessment History panel**: private-token references retained in the current browser for review and report download. The backend does not publish a shared history listing.

The implementation uses four HTML documents: `index.html` is the public landing page, `map.html` contains the map/results/report workflow, `methodology.html` documents the model, and `info.html` renders the source, limitations, about, privacy, and offline routes. No page is varied according to user type.

## 5. Primary workflow

1. The user opens the single Web-GIS interface.
2. The user searches, enters latitude/longitude, or clicks the map.
3. The API validates the coordinate and verifies it against the live PSA Basey municipal result.
4. The API identifies the containing barangay and codes through the verified PSA barangay service.
5. The backend queries the verified MGB flood and PHIVOLCS liquefaction layers. Ground shaking is queried only when an authorized, metadata-verified source exists.
6. Relevant historical incidents and CLUP references are resolved and clearly described as point, nearby, barangay-wide, or broader context.
7. Source labels, dates, official/demonstration status, availability, and quality notes are attached to every resolved input.
8. Each exact live source code is transformed through the versioned model lookup while its official label remains unchanged. Authorized imported layers may instead use their separately documented `0–1` fraction-to-`0–100` contract.
9. If any required hazard input is unavailable, the assessment is marked
   `incomplete`; absence is never converted to zero or “low.”
10. Only for a complete valid input set, the fuzzy engine calculates
    memberships, activates documented rules, applies weights, and defuzzifies
    across the 1–100 output universe.
11. The result includes the descriptive category when complete—or explicit
    missing reasons when incomplete—plus contributing explanations, cautious
    planning recommendations, limitations, and disclaimer.
12. The user previews and downloads a PDF containing the same assessment facts and explanation.

## 6. Spatial and data architecture

### 6.1 Coordinate conventions

- API point coordinates use WGS 84 (`EPSG:4326`) and GeoJSON order `[longitude, latitude]`.
- The interface labels latitude and longitude fields separately and rejects reversed or out-of-range values.
- Live ArcGIS point queries explicitly use `inSR=4326`, `outSR=4326`, and `esriSpatialRelIntersects`.
- Importers detect or require a source coordinate reference system (CRS), then reproject to WGS 84.
- Original CRS, original filename, import time, transformation details, and source metadata are retained.
- Live point identification queries the municipal boundary first, then barangays. The local-import path retains deterministic point-in-polygon behavior for authorized static datasets and tests.

SQLite stores normalized WGS 84 GeoJSON text without a spatial extension or persisted bounding-box columns/indexes. Runtime point-in-geometry checks scan the applicable loaded feature set. The geometry module can calculate a bounding box in memory, for example to obtain a representative search point, but the database does not use it for candidate filtering. GeoJSON and coordinate CSV imports work with the Python standard library; Shapefile and GeoPackage support uses optional Fiona/GDAL. GeoTIFF must be inspected and vectorized outside the application before its classified polygons are imported.

### 6.2 Dataset provenance

Every dataset and context response carries, as applicable:

- title and hazard/context type;
- source organization and source reference;
- source date, publication date, and retrieval/import date when known;
- coverage and scale/resolution;
- CRS and transformation information;
- license or use restriction;
- `data_status`: `official` or `demonstration`;
- quality summary, known limitations, and validation messages;
- availability for the selected location; and
- the dataset/model version used by the assessment.

Live ULAP results additionally retain the ArcGIS layer ID, classification field, raw code, unchanged official label, complete returned attributes, retrieval/cache timestamps, source URL, and runtime schema/domain warnings. Endpoint verification, source authority, point availability, and model usability are separate statuses.

“Official” is an explicit designation backed by issuing-agency/source metadata. The application never infers official status from a filename, URL hostname, or geographic plausibility. Hazard quality is separately labelled `verified`, `provisional`, `limited`, or `unknown`. Demonstration records are visibly labelled in the map, result, methodology/source display, preview, and PDF.

### 6.3 Missing and uncertain information

Availability and vulnerability are separate concepts. Live-source statuses include `available`, `no_intersection`, `outside_coverage`, `unavailable`, `authentication_required`, `service_error`, `timeout`, `invalid_response`, and `changed_schema`; stored assessments normalize those facts into available/missing input snapshots. A missing required input has no numeric substitute. If the configured model requires that input:

- the saved assessment and fuzzy result `status` are `incomplete`;
- the missing input and reason are listed;
- no final numeric score or descriptive category is asserted;
- rules are not presented as a complete evaluation;
- available hazards and context may still be displayed; and
- the report carries the incomplete notice and disclaimer.

The current live registry has verified flood and liquefaction endpoints but no verified ground-shaking source. Therefore a live three-hazard assessment remains incomplete even when the other two values are available. The application does not silently encode lower confidence as lower vulnerability.

## 7. Database

Only tables directly required by approved functions are allowed:

| Table | Purpose |
| --- | --- |
| `municipal_boundary` | Basey boundary geometry and source metadata reference |
| `barangays` | Barangay names/codes, WGS 84 GeoJSON geometries, and provenance |
| `hazard_datasets` | Flood, liquefaction, and ground-shaking dataset catalogue, version, CRS, dates, status, and quality |
| `hazard_features` | Authorized imported vector features, classifications, stored 0–1 fractions, WGS 84 GeoJSON, and dataset link; live ULAP evidence is snapshotted through assessment inputs |
| `historical_incidents` | Available dated incident records, location/extent, type, description, and provenance |
| `clup_references` | Relevant CLUP zones, policies, map references, location/extent, dates, and provenance |
| `fuzzy_models` | Model version, status, defuzzification method, validation notes, and configuration checksum |
| `fuzzy_variables` | Model inputs and output definitions, units, required flags, and normalization mappings |
| `membership_functions` | Linguistic labels, function types, parameters, and variable links |
| `fuzzy_rules` | Human-readable and machine-readable antecedents/consequents, weights, and ordering |
| `assessments` | Selected point, barangay, timestamps, status, model version, and dataset-version snapshot |
| `assessment_inputs` | Source classifications, 0–100 model values, stored 0–1 fractions, availability, provenance, and quality at assessment time |
| `assessment_memberships` | Membership degree per input and linguistic category |
| `assessment_rule_activations` | Rule snapshot, unweighted firing strength, weight, and effective activation |
| `assessment_results` | Score/category when complete, completeness reasons, recommendations, and disclaimer version |
| `generated_reports` | Assessment link, PDF checksum, generation time, and the assessment snapshot used for that generation |
| `routing_study_areas` | Versioned routing-boundary polygon(s) and source authority metadata; the active row (currently the full Basey municipal boundary) sets the routing scope |
| `searchable_locations` | Normalized OSM street/POI search records, geometry, snapshot date, and provenance |
| `evacuation_centers` | Researcher/LGU-supplied designated centers, coordinates, designation, provenance, version, and optional capacity |
| `visitor_sessions`, `visitor_events` | Aggregate, non-identifying visitor counts (random first-party id, path, timestamps) read only by the isolated admin dashboard's analytics page (Section 3.1); not read by any public-product API |
| `admin_audit_log` | Local admin-dashboard action log (actor, action, entity, timestamp); not read by any public-product API |

Foreign keys are enabled. Imports and assessments use transactions. Provenance and model snapshots prevent a later data or configuration update from silently changing an existing assessment explanation.

`visitor_sessions`, `visitor_events`, and `admin_audit_log` are defined in
the same shared `db/schema.sql` as every table above, so they exist in every
database this schema initializes, including `data/geosafe.db`. They exist
solely to support the isolated admin dashboard (Section 3.1); no public API
endpoint or public page reads or writes them, and they carry no user
identity, role, or permission semantics — they are the dashboard's own
operational log, not a reintroduction of the excluded audit/permission
concepts in Section 2.

There are deliberately no `users`, `roles`, `user_roles`, `permissions`, `staff_profiles`, `user_sessions`, or `administrative_approvals` tables. A lightweight server error log may exist only for technical diagnosis and is not an application feature.

## 8. API boundaries

This section describes the public product's API. The isolated admin
dashboard (Section 3.1) exposes its own separate, session-authenticated
routes under `/admin/*`, documented in
[admin-development.md](admin-development.md), not under `/api/v1` and not
reachable from the public origin/deployment; they are not part of this
boundary.

The public API is versioned under `/api/v1` and limited to:

- live-source status: ULAP service summaries and validated metadata;
- map and spatial data: Basey boundary, barangays, hazard layers/features, search, identify, and hazards at a point;
- assessment: create/list/read, explanation, and PDF report;
- supporting information: incidents, nearby incidents, CLUP references/by-location, methodology, and data sources.

Detailed application contracts are in [api.md](api.md). Live-source architecture,
field mappings, failures, and gaps are documented in
[ulap-integration.md](ulap-integration.md),
[ulap-field-mappings.md](ulap-field-mappings.md), and
[known-data-gaps.md](known-data-gaps.md). Within this public boundary there
are no authentication, account, role, permission, staff, administration,
approval, audit, upload, or model-editor API groups.

## 9. Fuzzy engine boundary

The engine loads a versioned configuration and validates it before use. The configuration, not interface code, defines:

- input variables and required flags;
- linguistic categories;
- normalization mappings;
- membership-function types and parameters;
- rule statements and weights;
- output membership functions and category thresholds;
- defuzzification and score-normalization method;
- model version; and
- validation notes.

The current configuration is `config/fuzzy_model.json`, version `0.5.1-demo`. It defines exact demonstration mappings from verified live `fscode` and `lccode` values into separate `0–100` model inputs, retains the imported-fraction compatibility path, and leaves ground-shaking source/mappings empty. It defines three required inputs, low/moderate/high input memberships, a generated complete 27-rule monotonic Mamdani grid, centroid sampling across output points 0–100, entropy-weight configuration with an explicitly labelled equal-weight research fallback, and the configured Very Low/Low/Moderate/High/Very High score bands. It forbids inferring a missing numeric value from a label, unknown code, empty query, or substitute hazard. Historical incidents and CLUP references remain non-numeric context.

The assessment persists all normalized inputs, membership values, rule activations, and the exact model/configuration version. This provides explainability without a model-management interface. The demonstration model must state that its thresholds, rules, and recommendations require validation by qualified domain experts.

## 10. Reports

The server regenerates a PDF from the immutable saved assessment snapshot whenever the report endpoint is requested. It does not rerun spatial lookup or fuzzy inference. Each generation records its SHA-256 digest and content snapshot. The PDF contains:

- selected location, coordinates, Basey containment, and barangay;
- input classifications and normalized values;
- memberships, activated rules, weights, and activation strengths;
- score/category, or an explicit incomplete result;
- incident and CLUP context with the spatial relationship stated;
- source names, dates, versions, official/demonstration labels, and quality/missing-data notices;
- planning-oriented recommendations;
- model version, saved assessment time, and assessment identifier; and
- the required disclaimer.

Report preview and PDF content are contract-tested so material warnings cannot disappear in one representation.

## 11. Security, privacy, and operational constraints

- The prototype binds according to deployment configuration and should be placed behind HTTPS when exposed beyond a trusted network.
- No identity data is required. Assessment history stores a point selected by the user, so deployments must define an appropriate retention policy.
- Inputs are validated and SQL statements are parameterized.
- Outbound ArcGIS calls use HTTPS and an explicit two-host allowlist; redirects are revalidated.
- Optional ArcGIS credentials are read from `ULAP_TOKEN` on the server and removed from logs, cache keys, response URLs, and reports.
- Location search uses the local OSM street/POI index, designated centers,
  loaded Basey barangays/PSGC codes, and coordinate pairs. It does not call an
  external geocoder or Overpass at runtime.
- Report filenames are server controlled and cannot be supplied as arbitrary filesystem paths.
- Import utilities, not the browser, are the trusted data-management boundary.
- The local admin dashboard (Section 3.1) is a separate trust boundary: it is
  the one part of the codebase with accounts and sessions, and that
  authentication model applies only there. It must remain unreachable from
  the public origin/deployment and must not read or write `data/geosafe.db`.

## 12. Architecture tests and scope control

The current standard-library `unittest` suite covers:

- schema initialization produces exactly the approved application tables;
- endpoint scope checks reject identity and administrative API families from the public product;
- no approved public page, public table, or tested public API path introduces roles, users, permissions, admin portals, approvals, or browser uploads (`tests/test_scope.py`);
- the local admin dashboard's own isolation: its routes, session cookies, and rate limiting behave as documented, and it stays off the public API/page allowlists above (`tests/test_admin_dashboard.py`);
- point containment and barangay identification;
- the presence of all three selection controls and approved frontend API references;
- hazard/context retrieval and provenance;
- ULAP registry/URL validation, ArcGIS error parsing, TTL cache/retry behavior, domain decoding, point-query edge cases, and provider completeness gating;
- complete and incomplete fuzzy assessments;
- persistence of explanation values and scores in assessment snapshots;
- demonstration versus official labels; and
- PDF generation, report metadata, and visible disclaimer/missing-data source contracts.

The suite includes static HTML/JavaScript contract checks and real HTTP workflow tests, but it is not browser automation. Manual browser acceptance still verifies map rendering, layer toggling, search/coordinate/map-click interaction, responsive behavior, and report-preview presentation.

Code review uses the scope gate in section 1. A technically attractive feature is still rejected when it does not directly support an approved function.
