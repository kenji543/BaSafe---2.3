# Known ULAP and Deployment Data Gaps

## 1. Release interpretation

The application can produce a complete three-hazard demonstration result from
local snapshots. It is not yet a defensible operational assessment because the
ground-shaking layer is a limited-quality derivative of regional deterministic
scenario maps and the fuzzy model has not been validated by domain experts.

The gaps below are data and validation dependencies, not reasons to add an
administrative portal, accounts, roles, approval screens, or browser upload
workflows. The one deliberate exception, added from client feedback, is the
submit-only citizen damage report form (`/report-damage`). Its reports go
only to responders, are never public, and do not feed any hazard dataset or
the fuzzy model: they are responder information, not source data.

### Citizen damage report gaps

- **Not available on the live site.** Vercel has no persistent storage, so
  it refuses reports. Accepting them there needs a persistent database,
  private photo storage, and a rate limit that isn't held in process memory.
- **MDRRMO hotline number.** The form's emergency notice gives 911 and "the
  Basey MDRRMO" without a number; the client must supply the official one.
- **Retention period.** Basey MDRRMO must set how long reports (with names
  and phone numbers) are kept. Until then, purge them manually as described
  in [admin-development.md](admin-development.md).
- **No offline sending.** A report sent without signal fails, and the form
  keeps its answers for a retry, but there is no background queue.

## 2. Blocking gaps

### Municipality-wide evacuation routing data

The active routing study area is the full official Basey municipal PSA
polygon (`routing_study_areas` version `basey-municipal-boundary-v1`), not
the seven-barangay Mercado/Palaypay/Baybay/Sulod/Loyo/Buscada/Lawa-an
composite this section used to describe as active — that composite remains
available only as a legacy, explicitly non-official alternative, reactivatable
via `scripts/build_town_proper_boundary.py` +
`scripts/import_routing_data.py study-area --replace`. The frozen pedestrian
graph covers this wider area but is genuinely sparse away from the town
center: 9 disconnected components, 17.04% named road segments
(`data/routing/osm_snapshot_metadata.json`). Run
`scripts/report_routing_connectivity.py` for a current count; as of the last
run, 41 of 51 barangays have a working route (`maximum_snap_distance_m` is
1000m, raised from 500m after that script showed 16 barangays' representative
points clustered under 900m from the network with no working route at the old
cutoff), 2 barangays (Baloog, Mabini) sit in a graph component with no
official evacuation center at all and cannot be fixed by snap-distance
tuning, and the rest remain snap-distance-limited. Both the original 9-facility
MDRRMO town-proper extract and two later MSWDO-derived imports
(`mswdo-cy2025-osm-matched-*`, `mswdo-cy2025-user-verified-coords-*`,
45 rows across 42 barangays) are loaded `is_official` and usable as route
destinations; the MSWDO imports have no audit trail for when/why they were
promoted to official (`updated_at` is `NULL` on every such row), and their
stored `import_notice` metadata still incorrectly says they are "not enabled
as an operational route destination" — a stale field, not current behavior.
Capacities and activation conditions remain unknown for all sources. The
public workflow uses the nearest reachable designated center and does not
expose the experimental hazard-aware comparison. See
[routing.md](routing.md) for the exact provenance and preprocessing contract.

| Gap | Current evidence | Required resolution | Effect until resolved |
| --- | --- | --- | --- |
| Flood query capability | On 2026-08-05 the layer advertised `Query`, but the operation returned ArcGIS error 400 | The synchronization client uses the same official service's working `identify` operation and stores the validated result locally; monitor metadata and classifications on every refresh | Runtime is no longer blocked by the rejected query; genuine no-intersection areas remain missing, never Low |
| Ground-shaking coverage | The public PHIVOLCS ground-shaking feature layer has no Basey point coverage; four official Region VIII 2014 deterministic-scenario KMZ rasters do cover Basey | Replace the derived grid when PHIVOLCS supplies a current, authoritative Basey ground-shaking product; preserve scenario/date/resolution metadata | Current ground shaking is usable only as a `limited` demonstration input |
| Ground-shaking transformation | `peiscode` I-X maps explicitly to model indices 10-100 | Qualified PHIVOLCS/seismology and model reviewers approve the scenario aggregation, PEIS extraction, source-to-index mapping, and membership implications | The mapping remains a transparent capstone assumption, not an official rating |
| Fuzzy-model domain validation | Version `0.5.5-demo` mappings, functions, rules, weights, thresholds, and recommendations are capstone assumptions | Documented review and approval by qualified MGB, PHIVOLCS, geotechnical, DRRM, and planning specialists | Results remain demonstration screening only, even when all source values are available |

Active Fault, distance to a fault, liquefaction, epicenters, an invented
intensity, zero, and the last successful result remain explicitly prohibited
substitutes.

## 3. Supplied boundary gaps

The file `basey-barangay-boundary-final.json` has:

- no declared CRS;
- no issuing agency, license, source date, version, or authority statement;
- 58 features but only 52 populated barangay names;
- six unnamed features and seven features lacking several candidate
  identifiers;
- one property-level EPSG:3125 clue, which is not a GeoJSON CRS declaration;
- no reliable PSGC join field; and
- a different feature count and material boundary difference from the live PSA
  service.

EPSG:3125 is a high-confidence spatial inference, supported by a plausible
WGS 84 transformation and geometry overlay, but must be confirmed by the
issuer. All 58 interpreted geometries are topologically valid; that does not
resolve provenance or version differences.

The live PSA query returned 51 Basey barangays. Before the supplied file can be
used operationally, the project needs issuer confirmation, CRS confirmation,
PSGC reconciliation, a decision on unnamed/difference polygons, source date
and version, license/use terms, and a documented authority preference.

## 4. Hazard coverage gaps

On 2026-07-23:

- the rectangular live Basey municipal extent intersected 329 flood features
  and four liquefaction features;
- longitude `125.068`, latitude `11.282` was inside Basey and Buscada (Pob.);
  that point returned zero flood and zero liquefaction features; and
- longitude `125.0336740411251`, latitude `11.290798389750039` separately
  returned flood `fscode = 03` and liquefaction `lccode = 01`.

These observations show that service availability, municipal-envelope
intersection, and point availability are different. The current audit has not
established:

- complete flood coverage of the municipal polygon;
- complete liquefaction coverage of the municipal polygon;
- whether every apparent gap means intentionally unmapped area, true absence
  of a classified polygon, scale/generalization, or another source limitation;
- the preferred response when overlapping polygons carry conflicting classes;
  or
- fitness of the July 2018-attributed layers for a current planning decision.

Until a coverage study is completed, zero features must be reported as
`no_intersection` or `outside_coverage` when that distinction can be
established. It must never be Low or Safe.

## 5. Source date, terms, and authority gaps

Live metadata identified MGB/PHIVOLCS attribution as of July 2018. Runtime
reachability does not establish:

- whether a newer authorized dataset exists;
- the exact acquisition/mapping date for each polygon;
- suitability at parcel or site-investigation scale;
- completeness for Basey;
- permission to cache, redistribute, or embed full geometries; or
- whether additional agency/LGU attribution language is required.

Deployment owners must obtain and retain the applicable data-use terms and
document any caching, report reproduction, and retention constraints.

## 6. Historical-incident gap

No authorized historical-incident dataset is configured. Synthetic repository
records are not operational evidence.

Required work:

1. identify the municipal/provincial/national custodian and authorized
   extract;
2. define event, date, spatial, duplicate, and verification semantics;
3. document temporal and reporting completeness;
4. import through the CLI with provenance and quality notices; and
5. state that no matching record does not prove no incident occurred.

Incident context remains non-numeric in model `0.5.1-demo`.

## 7. CLUP gap

No adopted, authorized Basey CLUP reference dataset is configured. Synthetic
repository references are not operational evidence.

Required work:

1. identify the adopted edition and approval status;
2. obtain authorized plan text/maps and use terms;
3. define stable section/map/zone references;
4. document spatial precision and whether a match is point, barangay, or
   municipality context; and
5. import with source date, edition, limitations, and provenance.

CLUP context remains non-numeric in model `0.5.1-demo`.

## 8. Runtime reliability gaps

The four configured ArcGIS services responded without a token during the
2026-07-23 checks. That does not guarantee future anonymous access or uptime.
Runtime must still handle:

- DNS/TLS/network failure;
- timeouts and rate limiting;
- HTTP and ArcGIS errors;
- token requirements and ArcGIS codes 498/499;
- layer removal or ID changes;
- field/domain/schema changes;
- malformed or partial JSON;
- pagination and transfer limits; and
- expired or stale cache entries.

An expired value must not be reused indefinitely. Any explicitly allowed stale
fallback must retain its original retrieval time, expiry, and prominent stale
label. No such fallback may fabricate a missing ground-shaking value.

## 9. Testing and acceptance gaps

Isolated unit tests may use sanitized fixtures. They do not prove that a live
service is currently working. Live integration tests are opt-in through:

```text
LIVE_ULAP_TESTS=true
```

All five opt-in live tests passed on 2026-07-23. The ordinary suite ran 78
tests: 73 passed and those five network tests were skipped by default. These
dated results establish one observed service state, not future uptime or
complete coverage.

Before deployment acceptance, record successful results for:

- service and layer metadata validation;
- the Basey municipal/barangay query;
- representative flood and liquefaction point queries, including a
  zero-feature point;
- a ground-shaking query only after an authorized endpoint exists;
- timeouts, ArcGIS error objects, unknown codes, changed domains, and multiple
  intersections;
- backend normalized responses and incomplete-score blocking; and
- source/quality/disclaimer parity among interface, saved assessment, preview,
  and PDF.

The verification date and endpoint metadata must accompany every claimed live
test result.

## 10. Non-gaps by design

The following are intentionally excluded rather than missing:

- users, registration, multiple accounts, roles, and permissions (the damage
  report form collects a name and phone per report and needs no account);
- administrator/staff/office-specific interfaces;
- account or dataset approval workflows;
- browser-based data/model management;
- user activity monitoring and audit-log management; and
- an administrator-only fuzzy-model editor.

Data preparation, source changes, and model revisions remain version-controlled
development/deployment processes.
