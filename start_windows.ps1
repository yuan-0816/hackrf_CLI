[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$hackrfInfo = Join-Path $projectRoot "third_party\hackrf-tools-windows\hackrf_info.exe"
$gpsSimulator = Join-Path $projectRoot "third_party\gps-sdr-sim\gps-sdr-sim.exe"

function Test-LocalPortAvailable {
    param(
        [Parameter(Mandatory = $true)]
        [int]$CandidatePort
    )

    $listener = New-Object System.Net.Sockets.TcpListener(
        [System.Net.IPAddress]::Loopback,
        $CandidatePort
    )
    try {
        $listener.Start()
        return $true
    }
    catch {
        return $false
    }
    finally {
        $listener.Stop()
    }
}

$lastPort = [Math]::Min($Port + 100, 65535)
$selectedPort = $null
for ($candidatePort = $Port; $candidatePort -le $lastPort; $candidatePort++) {
    if (Test-LocalPortAvailable -CandidatePort $candidatePort) {
        $selectedPort = $candidatePort
        break
    }
}

if ($null -eq $selectedPort) {
    throw "No available local TCP port was found from $Port through $lastPort."
}

if ($selectedPort -ne $Port) {
    Write-Warning "Port $Port is unavailable. Using port $selectedPort instead."
}

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
    Write-Output "Web interface: http://127.0.0.1:$selectedPort"
    & uv run uvicorn app.backend.app:app --host 127.0.0.1 --port $selectedPort
}
finally {
    Pop-Location
}
