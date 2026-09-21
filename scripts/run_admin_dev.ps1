$ErrorActionPreference = "Stop"

$adminProjectRoot = Split-Path -Parent $PSScriptRoot
$adminEnvironmentFile = Join-Path $adminProjectRoot ".env.admin.local"
$adminSnapshotPath = Join-Path $adminProjectRoot "data\geosafe.snapshot.db"
$adminDatabasePath = Join-Path $adminProjectRoot "data\admin-dev.db"

if (-not (Test-Path -LiteralPath $adminEnvironmentFile)) {
    throw "Missing .env.admin.local. Copy .env.admin.example and set a local password."
}

foreach ($adminEnvironmentLine in Get-Content -LiteralPath $adminEnvironmentFile) {
    $adminTrimmedLine = $adminEnvironmentLine.Trim()
    if (-not $adminTrimmedLine -or $adminTrimmedLine.StartsWith("#")) {
        continue
    }
    $adminName, $adminValue = $adminTrimmedLine -split "=", 2
    if (-not $adminName -or $null -eq $adminValue) {
        throw "Invalid admin environment entry: $adminEnvironmentLine"
    }
    Set-Item -Path "Env:$($adminName.Trim())" -Value $adminValue.Trim()
}

if (-not (Test-Path -LiteralPath $adminDatabasePath)) {
    if (-not (Test-Path -LiteralPath $adminSnapshotPath)) {
        throw "Bundled snapshot not found: $adminSnapshotPath"
    }
    Copy-Item -LiteralPath $adminSnapshotPath -Destination $adminDatabasePath
}

Set-Location -LiteralPath $adminProjectRoot
uv run python -m geosafe.server `
    --host $env:GEOSAFE_HOST `
    --port $env:GEOSAFE_PORT `
    --database $adminDatabasePath `
    --web-root (Join-Path $adminProjectRoot "web")
