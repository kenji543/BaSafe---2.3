param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "The local Python environment was not found at $pythonPath."
}

Set-Location $projectRoot
& $pythonPath -m geosafe.server --host 127.0.0.1 --port $Port
