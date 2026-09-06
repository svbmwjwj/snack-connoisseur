[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$bashPath = (Get-Command bash.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)

if (-not $bashPath) {
    $candidates = @(
        "C:\Program Files\Git\bin\bash.exe",
        "C:\Program Files (x86)\Git\bin\bash.exe",
        "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe"
    )
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            $bashPath = $cand
            break
        }
    }
}

if (-not $bashPath) {
    Write-Error "Git Bash (bash.exe) was not found. Please verify Git for Windows installation."
    exit 1
}

$scriptPath = Join-Path $PSScriptRoot "cnsr.sh"
& $bashPath $scriptPath @ScriptArgs
exit $LASTEXITCODE
