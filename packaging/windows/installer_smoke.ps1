# Windows-only installer smoke (needs built Fuseshot-Setup-0.2.0rc1.exe and a clean temp profile).
# Steps marked [Windows] are not runnable on Linux CI.
param(
    [string]$Installer = (Join-Path $PSScriptRoot "dist\Fuseshot-Setup-0.2.0rc1.exe")
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib.ps1")

if (-not (Test-Path $Installer)) {
    Write-Error "Build the installer first: .\packaging\windows\build.ps1"
}

$TempRoot = Join-Path $env:TEMP ("fuseshot-smoke-" + [guid]::NewGuid().ToString("n"))
$AppDir = Join-Path $TempRoot "app"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
$DataHome = Join-Path $TempRoot "data"
New-Item -ItemType Directory -Force -Path $DataHome | Out-Null

Write-Host "[Windows] Silent install to $AppDir"
Invoke-NativeCommand {
    & $Installer "/DIR=`"$AppDir`"" /SILENT /NOCLOSEAPPLICATIONS /TASKS= |
        Out-Null
}
$Exe = Join-Path $AppDir "Fuseshot.exe"
if (-not (Test-Path $Exe)) {
    throw "Fuseshot.exe not found after install"
}

$env:FUSESHOT_HOME = $DataHome
Write-Host "[Windows] Launch frozen review (loopback)"
$proc = Start-Process -FilePath $Exe -PassThru
Start-Sleep -Seconds 4
try {
    $sessionPath = Join-Path $DataHome "desktop-session.json"
    if (-not (Test-Path $sessionPath)) {
        throw "desktop-session.json was not written"
    }
    $session = Get-Content $sessionPath -Raw | ConvertFrom-Json
    if ($session.pid -ne $proc.Id) {
        throw "desktop-session.json pid $($session.pid) is not the installed process $($proc.Id)"
    }
    $port = [int]$session.port
    $url = "http://127.0.0.1:$port/settings"
    Invoke-WebRequest -Uri $url -UseBasicParsing | Out-Null
    Write-Host "Settings page reachable at $url (pid $($proc.Id))"
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}

$PhotoLib = Join-Path $TempRoot "photos"
$Fixtures = Join-Path $PSScriptRoot "fixtures\panorama_sample"
if (-not (Test-Path $Fixtures)) {
    throw "missing packaged smoke photos at $Fixtures"
}
New-Item -ItemType Directory -Force -Path $PhotoLib | Out-Null
Copy-Item -Path (Join-Path $Fixtures "*") -Destination $PhotoLib -Force
$OutDir = Join-Path $TempRoot "output"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[Windows] Run sample scan via frozen CLI (folder source, no Immich)"
Invoke-NativeCommand {
    & $Exe --home $DataHome --source "folder:$PhotoLib" --output-folder $OutDir run --no-loops --no-groups --no-stitch 2>&1 | Out-Host
}
$candidates = Join-Path $DataHome "output\candidates.json"
if (-not (Test-Path $candidates)) {
    throw "run did not write $candidates"
}

Write-Host "installer smoke finished (manual: confirm uninstall keeps $DataHome)"
