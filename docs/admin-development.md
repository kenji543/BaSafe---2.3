# Basafe local administration environment

The administration module is part of the local development worktree. It reads
and edits a private copy of the bundled Basafe database and never connects to
the public Vercel deployment. It also cannot write to the application on port
8000 as a side effect of ordinary use. There are two deliberate exceptions,
both described below, and each touches `data/geosafe.db` (the file the local
port-8000 app reads) only when an administrator explicitly acts: the
evacuation-center **Publish** action, and changing a citizen damage report's
triage status.

## Isolation boundaries

- Worktree: `C:\Users\Windows 11\Downloads\Basafe001-main\Basafe001-main`
- Branch: `development`
- Admin URL: `http://127.0.0.1:8001/admin`
- Private database: `data/admin-dev.db`
- Public local application: remains on `http://127.0.0.1:8000`, reading
  `data/geosafe.db`
- The private database, local credentials, and future upload directory are
  excluded by `.gitignore`.
- Publishing an evacuation center and changing a citizen report's status are
  the only paths by which the admin environment writes to `data/geosafe.db`.
  Both are per-record, explicit, validated, and audit-logged — never a bulk
  or automatic sync. The reports page also reads `citizen_reports` from that
  file, because reports are written there by the public app.

The existing snapshot is copied only when `data/admin-dev.db` does not exist.
Restarting the admin server does not overwrite that development database.

## Start the dashboard

1. Copy `.env.admin.example` to `.env.admin.local` if the local file is absent.
2. Replace the example password with a strong local-only password.
3. From PowerShell, run:

   ```powershell
   .\scripts\run_admin_dev.ps1
   ```

4. Open `http://127.0.0.1:8001/admin`. Unauthenticated visits are redirected to
   `/admin/login`, where the local administrator credentials can be entered.

Successful sign-in creates a random server-side session with an HTTP-only,
SameSite cookie. Sessions expire after eight hours of inactivity by default and
are invalidated when the local server restarts. Sign-in attempts are rate
limited. The password is not stored in browser storage. Basic authentication is
not enabled; every dashboard request requires a valid server-side session.

A remotely hosted administration system must additionally use HTTPS, managed
accounts, role-based authorization, password recovery, persistent session
storage, CSRF protection, and an auditable identity provider.

## Administration pages

Each feature has a dedicated authenticated route:

- `/admin/login` — administrator sign-in
- `/admin` — system overview and action queue
- `/admin/analytics` — anonymous visitors, online activity, and seven-day graph
- `/admin/datasets` — hazard dataset inventory
- `/admin/evacuation-centers` — one card per barangay; edit a center's name,
  coordinates, and description, assign a barangay's designated center, upload
  a facility photograph, and publish a draft to the local public app
- `/admin/hazard-events` — a hazard-event log with a calendar view and a
  toggleable weekly trend chart
- `/admin/reports` — citizen damage reports sent from the public
  `/report-damage` form, with contact details, a map link, photos, and a
  new / acknowledged / resolved status

Visitor analytics use a random first-party browser identifier. They do not
represent registered user accounts, and the feature does not persist raw IP
addresses. "Online now" means a visitor loaded a public page within the last
five minutes. Analytics are enabled only in the isolated environment by
`GEOSAFE_VISITOR_ANALYTICS_ENABLED=true`.

Authenticated administrators can upload a JPEG, PNG, or WebP facility photo of
up to 5 MB. Files are validated by signature and written below
`data/admin-uploads/`, outside the public web root. The active photo is available
only through an authenticated admin endpoint. Its attribution fields and an
audit record are stored in `admin-dev.db`.

Dataset activation and record deletion remain unavailable. Those functions
should be added only after managed authentication, roles, versioned staging,
validation, backup, and rollback are designed.

**Exception: evacuation-center editing and publishing.** Unlike every other
dataset, evacuation centers may need real-world corrections often enough
(wrong coordinates, a closed facility, a barangay newly assigned a center)
that requiring a full `scripts/import_dataset.py` cycle for every fix was
judged worse than building a narrow, validated publish path. An
authenticated administrator can edit a center's name, coordinates, and
description, and assign a barangay's designated center, directly in
`admin-dev.db` (a draft — this alone changes nothing the public app serves).
A separate, explicit **Publish** action validates that draft (required
fields, coordinates inside the Basey municipal boundary) and writes it into
`data/geosafe.db`, the file the local public app on port 8000 actually
reads, with an `admin_audit_log` entry recording who published what and
when. Publishing is immediate for the **local** public app only.

**What Publish does not do: put a change on the live internet.** The
deployed Vercel site does not read `data/geosafe.db` directly and cannot be
made to — `api/index.py` copies `data/geosafe.snapshot.db` into an ephemeral
`/tmp` at each cold start (see the root `CLAUDE.md`), so there is no
persistent, network-writable production database for an admin click to
reach. Getting a published change onto the real, public internet still
requires the two existing manual steps: `python
scripts/build_deployment_snapshot.py --force` (rebuild the snapshot from
`data/geosafe.db`) and `vercel --prod` (deploy it). This project does not
run those two commands automatically from an admin action; they stay
operator-run, same as every other command in this document.

## Citizen damage reports

Residents send reports from `http://127.0.0.1:8000/report-damage` (the public
app). Reports are stored in `data/geosafe.db` (`citizen_reports`), and photos
are stored in `data/admin-uploads/citizen-reports/`, a folder that both
servers resolve to and that is gitignored. `/admin/reports` reads both,
refreshes every minute, and lists new and acknowledged reports first.

- **Restart the public app after pulling.** Its startup creates the
  `citizen_reports` table. The admin page also creates it, empty, if the
  public app hasn't restarted yet.
- **The admin server refuses reports.** `/report-damage` on port 8001 shows
  a "can't be sent from this site" notice, because port 8001 runs on
  `admin-dev.db` and a report stored there would never appear in triage. The
  Vercel deployment refuses reports the same way, because it has no
  persistent storage.
- **Nobody may be watching.** The form tells residents it is not an emergency
  hotline. The MDRRMO hotline number still has to be supplied by the client
  and added to the form's notices.
- **Personal data.** Reporter names and phone numbers fall under the
  Philippine Data Privacy Act. Use them only to respond to the report.
  `scripts/build_deployment_snapshot.py` deletes every report, so none ship
  in the committed snapshot.
- **Retention and purge.** Basey MDRRMO must set a retention period. There is
  no purge button. To delete resolved reports older than a cutoff date,
  delete their photo files first and then the rows. Stop the public app
  first, or run this while it is idle:

  ```powershell
  python -c "import json,sqlite3,pathlib; c=sqlite3.connect('data/geosafe.db'); cutoff='2026-12-31'; rows=c.execute(\"SELECT photos_json FROM citizen_reports WHERE status='resolved' AND created_at < ?\", (cutoff,)).fetchall(); [pathlib.Path('data/admin-uploads/citizen-reports', n).unlink(missing_ok=True) for (p,) in rows for n in json.loads(p)]; c.execute(\"DELETE FROM citizen_reports WHERE status='resolved' AND created_at < ?\", (cutoff,)); c.commit()"
  ```

## Sync history

`scripts/sync_ulap_snapshot.py` still records a `sync_runs` row per target
(start/finish time, status, error message) in whichever local database it is
pointed at with `--db`. This is written by the same operator-run script that
already writes everything else — never by the deployed public application —
so it needs no external durable store. The recording is best-effort: a
missing `sync_runs` table (an older, un-migrated database) or a write failure
never blocks or fails the sync itself. There is no longer a dashboard page
for this — query `sync_runs` directly (`sqlite3 data/admin-dev.db "SELECT *
FROM sync_runs ORDER BY started_at DESC"`) if you need to check recent runs.

## Hazard-event log

`/admin/hazard-events` shows a manually curated log of dated hazard bulletins
(rainfall, earthquake, other) on a calendar: days with logged events are
marked with a colored dot per event type, and clicking a day opens the full
detail for everything logged that day (severity, source, verification
status, and notes) — the table below the calendar lists every logged row for
linear/keyboard browsing. The API itself (`GET /api/v1/admin/hazard-events`)
returns every row unwindowed and uncapped; a curated log is low-volume by
design, so there's nothing to page or bucket. Rows are imported with:

```powershell
python scripts/import_hazard_events.py path/to/events.csv `
  --db data/admin-dev.db `
  --source-date "YYYY-MM-DD" `
  --data-classification official `
  --strict
```

CSV columns: `event_type` (`rain`/`earthquake`/`other`), `occurred_at`,
`severity_value`, `severity_unit`, `source`, and optional `raw_reference`/
`notes`. Rejected rows write to `<input>.import-errors.jsonl` unless
`--strict` is passed, in which case any invalid row fails the whole import.
The dashboard panel is explicitly labeled a curated log, not a claim of
exhaustive coverage.

Two operator-run scripts can populate this log semi-automatically, in
addition to manual CSV transcription. Both are local-only (never called by
the deployed application), deduplicate against what's already logged, and
write through the same validated `import_hazard_events()` path above.

### Earthquakes — USGS Earthquake Catalog

```powershell
python scripts/sync_earthquake_events.py --db data/admin-dev.db
```

Queries USGS's free, keyless, structured GeoJSON feed (150km radius around
Basey, magnitude 5.0+ by default — tuned against real PHIVOLCS-reported
events so it lands roughly one notable quake every few weeks, not a flood of
minor ones) and logs any event not already present, deduplicated by the
USGS event page URL. **USGS is an independent global network, not PHIVOLCS**
— magnitude and location for the same physical earthquake can differ
slightly between the two (confirmed in practice: USGS logged the May 2026
Eastern Samar quake at M6.0, PHIVOLCS's own bulletin reported M6.1). Every
row's source/notes says which agency it came from; never presented as a
PHIVOLCS bulletin.

### Rain — PAGASA's live Weather Advisory file

```powershell
uv run --extra hazard-events-prep python scripts/sync_rain_events.py --db data/admin-dev.db
```

Fetches `pubfiles.pagasa.dost.gov.ph/tamss/weather/advisory.pdf` — PAGASA's
own file server, the same domain family this project already trusts for
PHIVOLCS ground-shaking data — extracts its text (requires `pdfplumber`, the
`hazard-events-prep` extra), and looks for a rainfall-outlook figure
mentioning the target province (`--target-province`, default `Samar`) within
a nearby stretch of text. Deduplicates by advisory number, so re-running it
only logs a genuinely new advisory.

**Real, structural limitation, not a bug**: PAGASA publishes each Weather
Advisory by overwriting this one fixed file rather than keeping an
incrementing archive. This script can only ever see whichever advisory is
live at the moment it runs — if two advisories are issued between two runs,
the earlier one is silently unreachable, not just skipped. Running it more
often during active weather narrows that gap; nothing can close it, since
PAGASA's own file layout doesn't expose the history. An earlier design
pulled PAGASA's Tropical Cyclone Bulletin PDFs from the
`pagasa-parser/bulletin-archive` GitHub mirror instead (which is a genuine
historical archive) — abandoned because those bulletins never contain the
rainfall figures directly, only a reference to a separate Weather Advisory
number, which that archive doesn't include.

Also real: rainfall-outlook wording is free text and varies between
advisories, so this recognizes a `<province> ... <N> to <M> mm` / `up to <N>
mm` phrasing near a target-province mention and nothing more exotic. An
advisory that doesn't match is reported plainly (`no_rainfall_mention`),
never guessed at.

### Rain — Open-Meteo Historical Weather (Basey-coordinate estimate)

```powershell
python scripts/sync_rain_events_openmeteo.py --db data/admin-dev.db
```

The script above is inherently province-wide — PAGASA's advisory text says
what Samar province should expect, never what actually happened at Basey's
own coordinates. This script queries Open-Meteo's free, keyless historical
archive (`archive-api.open-meteo.com`, ERA5 reanalysis) directly at Basey's
coordinates (11.28, 125.07) and logs every day over the last `--days-back`
(default 90) whose modeled daily total is at or above `--min-daily-mm`
(default 0.1mm — effectively every day Basey recorded measurable rain, not
just heavy/notable days), deduplicated by date. Raise `--min-daily-mm` to
narrow a given run back down to notable/heavy rain only. Confirmed in
practice: for TD Wilma's Dec 6 2025 landfall, PAGASA's province-wide outlook
said 100–200mm for Samar while Open-Meteo's Basey-coordinate estimate for
that day was ~11mm — logging both sources side by side surfaces that gap
instead of hiding it.

Because rain is now a complete daily record rather than a curated selection,
it dominates the row count in `hazard_events` — earthquakes and other
bulletins remain the curated, notable-only side of the same log. The
dashboard calendar and sidebar note both say this explicitly so the
difference in coverage is never implied to be uniform.

**Real, disclosed limitation**: this is a reanalysis-model estimate snapped
to the nearest ~9km grid cell, not a physical rain gauge reading in Basey.
Every logged row's notes say so explicitly and the source name identifies it
as "ERA5 reanalysis, Basey coordinates" rather than an official bulletin.
Keep running `sync_rain_events.py` (PAGASA) too — its rows carry PAGASA's own
authority and name the actual storm/system, which a model estimate cannot.

## Request/error/latency metrics

Not implemented. Before building a custom `admin_metrics` table and the
external durable store it would require, check whether Vercel's own
request/error/duration observability already covers what's needed — see
`admin-dashboard-plan-v3.md` (kept outside this repository) for the reasoning
and the narrowed, hot-path-safe design if it turns out to be necessary.
