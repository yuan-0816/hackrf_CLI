$ErrorActionPreference = "Stop"
$projectRoot = $PSScriptRoot
$hackrfInfo = Join-Path $projectRoot "third_party\hackrf-tools-windows\hackrf_info.exe"
$gpsSimulator = Join-Path $projectRoot "third_party\gps-sdr-sim\gps-sdr-sim.exe"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "找不到 uv。請先安裝 uv，再重新執行此腳本。"
}
if (-not (Test-Path -LiteralPath $hackrfInfo)) {
    throw "找不到 Windows HackRF Tools: $hackrfInfo"
}
if (-not (Test-Path -LiteralPath $gpsSimulator)) {
    Write-Output "尚未建立 gps-sdr-sim.exe，現在開始建置。"
    & (Join-Path $projectRoot "scripts\build_gps_sdr_sim_windows.ps1")
}

Push-Location $projectRoot
try {
    $env:PYTHONUTF8 = "1"
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        throw "Python 相依套件安裝失敗。"
    }
    & $hackrfInfo
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "HackRF Tools 已找到，但目前未偵測到可用裝置。"
    }
    & uv run uvicorn app.backend.app:app --host 127.0.0.1 --port 8000
}
finally {
    Pop-Location
}
