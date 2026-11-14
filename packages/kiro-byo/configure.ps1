$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Version = (Get-Content (Join-Path $Root "VERSION") -Raw).Trim()
$IntegrationState = Join-Path $Root "data\integration.json"

Write-Host "Kiro BYO provider setup" -ForegroundColor Cyan
Write-Host "$Version stores provider credentials in the operating system credential store." -ForegroundColor DarkGray
Write-Host "providers.json contains routing/model metadata only; the API key is not written there." -ForegroundColor DarkGray

$baseUrl = Read-Host "Base URL (e.g. https://api.deepseek.com or https://api.openai.com/v1)"
if (-not $baseUrl) { throw "Base URL is required." }
$baseUrl = $baseUrl.TrimEnd('/')

$secureKey = Read-Host "API Key (input is hidden)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
if (-not $apiKey) { throw "API Key is required." }

$python = $null
if (Test-Path $IntegrationState) {
    try {
        $state = Get-Content $IntegrationState -Raw | ConvertFrom-Json
        if ($state.kiro_byo_bin -and (Test-Path ([string]$state.kiro_byo_bin))) {
            $python = [string]$state.kiro_byo_bin
        }
    } catch {}
}
if (-not $python -and $env:KIRO_BYO_BIN -and (Test-Path $env:KIRO_BYO_BIN)) {
    $python = $env:KIRO_BYO_BIN
}
if (-not $python) {
    throw "Kiro BYO runtime was not found. Open Kiro BYO -> Integration, run Install / Update integration, then fully restart KiroCrew."
}

Write-Host "`nDiscovering models and saving provider..." -ForegroundColor Cyan
$previousSetupKey = $env:KIRO_BYO_SETUP_KEY
$previousHome = $env:KIRO_BYO_HOME
$env:KIRO_BYO_SETUP_KEY = $apiKey
$env:KIRO_BYO_HOME = $Root
try {
    $raw = (& $python -s -P -m kiro_byo configure-provider --base-url $baseUrl --api-key-env KIRO_BYO_SETUP_KEY --json 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Provider setup failed.`n$raw"
    }
} finally {
    if ($null -eq $previousSetupKey) { Remove-Item Env:KIRO_BYO_SETUP_KEY -ErrorAction SilentlyContinue } else { $env:KIRO_BYO_SETUP_KEY = $previousSetupKey }
    if ($null -eq $previousHome) { Remove-Item Env:KIRO_BYO_HOME -ErrorAction SilentlyContinue } else { $env:KIRO_BYO_HOME = $previousHome }
    $apiKey = $null
}

try {
    $result = $raw | ConvertFrom-Json
} catch {
    throw "Provider setup returned invalid data: $raw"
}

Write-Host "Saved provider: $($result.name) ($($result.id))" -ForegroundColor Green
Write-Host "Endpoint: $($result.base_url)" -ForegroundColor Green
Write-Host "Models: $($result.model_count)" -ForegroundColor Green
Write-Host "Credential: stored outside providers.json" -ForegroundColor Green
Write-Host "Fully restart Kiro Crew, then create a NEW chat session." -ForegroundColor Green
Write-Host "Optional UI: run .\manage.ps1" -ForegroundColor DarkGray
