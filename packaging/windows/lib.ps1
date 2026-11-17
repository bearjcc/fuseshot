# Shared helpers for Windows packaging scripts.

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