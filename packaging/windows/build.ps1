# Build Fuseshot Windows test installer. Run from any directory; requires Windows.
param(
    [string]$HuginRoot = $env:HUGIN_ROOT,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path (Join-Path $Here "..\..")).Path
$Dist = Join-Path $Here "dist"
$Stage = Join-Path $Here "stage"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Command
        if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
            throw "Native command failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $ErrorActionPreference = $prev
    }
}

Push-Location $Root
try {
    Invoke-NativeCommand { & $Python -m pip install --quiet pyinstaller }
    if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
    Invoke-NativeCommand {
        & $Python -m PyInstaller --noconfirm --distpath $Dist --workpath (Join-Path $Here "build\pyi") `
            (Join-Path $Here "fuseshot.spec")
    }

    $App = Join-Path $Dist "Fuseshot"
    if (-not (Test-Path (Join-Path $App "Fuseshot.exe"))) {
        throw "PyInstaller did not produce Fuseshot.exe"
    }

    if ($HuginRoot -and (Test-Path $HuginRoot)) {
        $Bin = Join-Path $HuginRoot "bin"
        if (-not (Test-Path $Bin)) { $Bin = $HuginRoot }
        $Dest = Join-Path $App "tools\hugin\bin"
        New-Item -ItemType Directory -Force -Path $Dest | Out-Null
        Copy-Item -Path (Join-Path $Bin "*") -Destination $Dest -Recurse -Force
        Copy-Item -Path (Join-Path $Here "notices\HUGIN.txt") -Destination (Join-Path $App "tools\hugin\NOTICE.txt")
    }

    $Iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($Iscc) {
        Push-Location $Here
        Invoke-NativeCommand { & iscc.exe (Join-Path $Here "installer.iss") }
        Pop-Location
    } else {
        Write-Warning "Inno Setup (iscc.exe) not on PATH; skipped installer. Raw app is in $App"
    }

    $Sums = Join-Path $Dist "SHA256SUMS.txt"
    @(
        Get-ChildItem -Path $Dist -Recurse -File | Where-Object { $_.Name -ne "SHA256SUMS.txt" }
    ) | ForEach-Object {
        $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLower()
        $rel = $_.FullName.Substring($Dist.Length + 1)
        "$hash  $rel"
    } | Set-Content -Encoding ascii $Sums

    Write-Host "Built $App"
    if (Test-Path (Join-Path $Dist "Fuseshot-Setup-test.exe")) {
        Write-Host "Installer: $(Join-Path $Dist 'Fuseshot-Setup-test.exe')"
    }
    Write-Host "Checksums: $Sums"
} finally {
    Pop-Location
}
