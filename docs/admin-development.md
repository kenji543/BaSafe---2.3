# Basafe local administration environment

The administration module is part of the local development worktree. It
monitors a private copy of the bundled Basafe database and does not connect to
the public Vercel deployment or write to the application on port 8000.

## Isolation boundaries

- Worktree: `C:\Users\Windows 11\Downloads\Basafe001-main\Basafe001-main`
- Branch: `development`
- Admin URL: `http://127.0.0.1:8001/admin`
- Private database: `data/admin-dev.db`
- Public local application: remains on `http://127.0.0.1:8000`
- The private database, local credentials, and future upload directory are
  excluded by `.gitignore`.

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
- `/admin/evacuation-centers` — facility inventory and photograph upload
- `/admin/routing` — routing dependencies and study-area status
- `/admin/context` — historical incident, CLUP, barangay, and boundary counts
- `/admin/activity` — recent scoring activity

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

Dataset activation, record deletion, and production synchronization remain
unavailable. Those functions should be added only after managed authentication,
roles, versioned staging, validation, backup, and rollback are designed.
