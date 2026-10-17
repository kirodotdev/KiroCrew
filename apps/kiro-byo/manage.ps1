param(
    [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$IntegrationState = Join-Path $Root "data\integration.json"
$python = $null
if (Test-Path $IntegrationState) {
    try {
        $state = Get-Content $IntegrationState -Raw | ConvertFrom-Json
        if ($state.kiro_byo_bin -and (Test-Path ([string]$state.kiro_byo_bin))) { $python = [string]$state.kiro_byo_bin }
    } catch {}
}
if (-not $python -and $env:KIRO_BYO_BIN -and (Test-Path $env:KIRO_BYO_BIN)) { $python = $env:KIRO_BYO_BIN }
if (-not $python) { throw "Kiro BYO runtime was not found. Open Kiro BYO -> Integration, run Install / Update integration, then fully restart KiroCrew." }
$previousHome = $env:KIRO_BYO_HOME
$env:KIRO_BYO_HOME = $Root
try {
    & $python -s -P -m kiro_byo ui --port $Port
    exit $LASTEXITCODE
} finally {
    if ($null -eq $previousHome) { Remove-Item Env:KIRO_BYO_HOME -ErrorAction SilentlyContinue } else { $env:KIRO_BYO_HOME = $previousHome }
}
