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
    throw "找不到 gps-sdr-sim 原始碼。請先初始化 third_party/gps-sdr-sim。"
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
        Write-Output "找不到本機 C 編譯器，改由 uv 暫時取得 Zig 編譯器。"
        & $uv.Source tool run --from ziglang python-zig cc -O3 @defines `
            $gpsSource $getoptSource -o $outputName
    }
    else {
        throw "找不到 C 編譯器或 uv。請先安裝 uv 後重試。"
    }

    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $output)) {
        throw "gps-sdr-sim Windows 編譯失敗，結束代碼: $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

Write-Output "gps-sdr-sim Windows 執行檔已建立: $output"
