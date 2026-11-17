# Windows-only installer smoke (needs built Fuseshot-Setup-test.exe and a clean temp profile).
# Steps marked [Windows] are not runnable on Linux CI.
param(
    [string]$Installer = (Join-Path $PSScriptRoot "dist\Fuseshot-Setup-test.exe")
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path $Installer)) {
    Write-Error "Build the installer first: .\packaging\windows\build.ps1"
}

$TempRoot = Join-Path $env:TEMP ("fuseshot-smoke-" + [guid]::NewGuid().ToString("n"))
$AppDir = Join-Path $TempRoot "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$DataHome = Join-Path $TempRoot "data"
New-Item -ItemType Directory -Force -Path $DataHome | Out-Null

Write-Host "[Windows] Silent install to $AppDir"
& $Installer "/DIR=`"$AppDir`"" /SILENT | Out-Null
$Exe = Join-Path $AppDir "Fuseshot.exe"
if (-not (Test-Path $Exe)) {
    throw "Fuseshot.exe not found after install"
}

$env:FUSESHOT_HOME = $DataHome
Write-Host "[Windows] Launch frozen review (loopback)"
$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 4
try {
    $session = Get-Content (Join-Path $DataHome "desktop-session.json") -Raw | ConvertFrom-Json
    $url = "http://127.0.0.1:$($session.port)/settings"
    Invoke-WebRequest -Uri $url -UseBasicParsing | Out-Null
    Write-Host "Settings page reachable at $url"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

Write-Host "[Windows] Run sample scan via frozen CLI"
& $Exe --home $DataHome run --no-loops --no-groups --no-stitch 2>&1 | Out-Host

Write-Host "installer smoke finished (manual: confirm uninstall keeps $DataHome)"