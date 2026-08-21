[CmdletBinding()]
param(
    [ValidateRange(1, 864000)]
    [int]$UserMotionSize = 864000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $projectRoot "third_party\gps-sdr-sim"
$gpsSource = "gpssim.c"
$getoptSource = "getopt.c"
$gpsSourcePath = Join-Path $sourceDir $gpsSource
$getoptSourcePath = Join-Path $sourceDir $getoptSource
$output = Join-Path $sourceDir "gps-sdr-sim.exe"
$outputName = "gps-sdr-sim.exe"

if (-not (Test-Path -LiteralPath $gpsSourcePath) -or
    -not (Test-Path -LiteralPath $getoptSourcePath)) {
    throw "gps-sdr-sim sources are missing from third_party/gps-sdr-sim."
}

$defines = @(
    "-D_FILE_OFFSET_BITS=64",
    "-DUSER_MOTION_SIZE=$UserMotionSize"
)

$zig = Get-Command zig -ErrorAction SilentlyContinue
$gcc = Get-Command gcc -ErrorAction SilentlyContinue
$cl = Get-Command cl -ErrorAction SilentlyContinue
$uv = Get-Command uv -ErrorAction SilentlyContinue

Push-Location $sourceDir
try {
    if ($zig) {
        & $zig.Source cc -O3 @defines $gpsSource $getoptSource -o $outputName
    }
    elseif ($gcc) {
        & $gcc.Source -O3 -Wall @defines $gpsSource $getoptSource -lm -o $outputName
    }
    elseif ($cl) {
        & $cl.Source /nologo /O2 /W3 "/DUSER_MOTION_SIZE=$UserMotionSize" `
            $gpsSource $getoptSource "/Fe:$outputName"
    }
    elseif ($uv) {
        Write-Output "No local C compiler found. Building with Zig through uv."
        & $uv.Source tool run --from ziglang python-zig cc -O3 @defines `
            $gpsSource $getoptSource -o $outputName
    }
    else {
        throw "No C compiler or uv installation was found. Install uv and retry."
    }

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $output)) {
        throw "gps-sdr-sim build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}

Write-Output "gps-sdr-sim Windows executable created: $output"
