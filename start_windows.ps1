$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$hackrfInfo = Join-Path $projectRoot "third_party\hackrf-tools-windows\hackrf_info.exe"
$gpsSimulator = Join-Path $projectRoot "third_party\gps-sdr-sim\gps-sdr-sim.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv was not found. Install uv and run this script again."
}
if (-not (Test-Path -LiteralPath $hackrfInfo)) {
    throw "Windows HackRF Tools were not found: $hackrfInfo"
}
if (-not (Test-Path -LiteralPath $gpsSimulator)) {
    Write-Output "gps-sdr-sim.exe is missing. Building it now."
    & (Join-Path $projectRoot "scripts\build_gps_sdr_sim_windows.ps1")
}

Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = "1"
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        throw "Python dependency installation failed."
    }
    & $hackrfInfo
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "HackRF Tools are installed, but no usable device was detected."
    }
    & uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8000
}
finally {
    Pop-Location
}
