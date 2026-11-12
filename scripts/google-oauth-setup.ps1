# google-oauth-setup.ps1 -- Windows twin of scripts/google-oauth-setup.sh.
# Sets up ONE Google Cloud project for every Kiro Crew Google card (Gmail, Drive,
# Calendar, Sheets, Docs, Slides), for private use: picks or creates the project,
# enables every API and MCP API, then prints the Project number and the three
# console links Google offers no API for. Safe to run again; writes nothing to disk.
# Guide: docs/guides/oauth-app-registration/google.md
#
# Usage: powershell -ExecutionPolicy Bypass -File scripts\google-oauth-setup.ps1 [-ProjectId <id>]
param([string]$ProjectId = '')
# No global 'Stop': Windows PowerShell 5.1 turns a native command's stderr into a
# terminating error under it, and gcloud prints notes on stderr. Invoke-Gcloud throws.

# Keep in step with src/kiro_crew/connections/registry.json (a test checks it).
$Apis = @('gmail.googleapis.com', 'drive.googleapis.com', 'calendar-json.googleapis.com', 'sheets.googleapis.com', 'docs.googleapis.com', 'slides.googleapis.com')
$McpApis = @('gmailmcp.googleapis.com', 'drivemcp.googleapis.com', 'calendarmcp.googleapis.com', 'sheetsmcp.googleapis.com', 'docsmcp.googleapis.com', 'slidesmcp.googleapis.com')
$RedirectUris = @('http://127.0.0.1:48103/callback', 'http://127.0.0.1:48104/callback', 'http://127.0.0.1:48105/callback', 'http://127.0.0.1:48109/callback', 'http://127.0.0.1:48110/callback', 'http://127.0.0.1:48111/callback')
$Scopes = 'https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/drive.readonly,https://www.googleapis.com/auth/calendar.calendarlist.readonly,https://www.googleapis.com/auth/calendar.events.readonly,https://www.googleapis.com/auth/calendar.events.freebusy,https://www.googleapis.com/auth/spreadsheets.readonly,https://www.googleapis.com/auth/documents.readonly,https://www.googleapis.com/auth/presentations.readonly'

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
  Write-Host 'gcloud is not installed. Install it from https://cloud.google.com/sdk/docs/install and run this again.'
  exit 1
}

# Native commands do not throw on a non-zero exit; check $LASTEXITCODE.
function Invoke-Gcloud { & gcloud @args; if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed" } }

$account = (& gcloud config get-value account 2>$null) -as [string]
if (-not $account) {
  Invoke-Gcloud auth login
  $account = (& gcloud config get-value account 2>$null) -as [string]
}

$project = $ProjectId
if (-not $project) {
  $project = (& gcloud projects list '--filter=name="Kiro Crew"' '--format=value(projectId)' --limit=1) -as [string]
}
if (-not $project) {
  $project = 'kiro-crew-' + (-join ((1..6) | ForEach-Object { '0123456789abcdef'[(Get-Random -Maximum 16)] }))
  Write-Host "Creating project $project ..."
  Invoke-Gcloud projects create $project '--name=Kiro Crew'
} else {
  & gcloud projects describe $project *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating project $project ..."
    Invoke-Gcloud projects create $project '--name=Kiro Crew'
  }
}
Write-Host "Using project $project (signed in as $account)."

Invoke-Gcloud services enable @Apis "--project=$project"
$mcpNote = ''
& gcloud services enable @McpApis "--project=$project"
if ($LASTEXITCODE -ne 0) {
  $mcpNote = '  The MCP APIs did not enable yet. Run this script again after Google accepts step 1.'
}

$number = (& gcloud projects describe $project '--format=value(projectNumber)') -as [string]
$parent = (& gcloud projects describe $project '--format=value(parent.type)') -as [string]
$console = 'https://console.cloud.google.com'

Write-Host ''
Write-Host "Done. Project ID: $project   Project number: $number"
if ($mcpNote) { Write-Host $mcpNote }
Write-Host ''
Write-Host 'Three clicks left (Google has no API for these):'
Write-Host ''
Write-Host '1. Join the Developer Preview (once per project; Google answers in a few days):'
Write-Host '   https://developers.google.com/workspace/preview'
Write-Host "   Paste the Project number: $number"
Write-Host ''
Write-Host "2. Consent screen: $console/auth/branding?project=$project"
Write-Host '   Click Get Started. App name: Kiro Crew. Pick your own email.'
# Any parent (an organization, or a folder inside one) means a Workspace org.
if ($parent) {
  Write-Host '   Audience: Internal.'
} else {
  Write-Host '   Audience: External (Internal needs a Workspace org). Then add yourself as a test user:'
  Write-Host "   $console/auth/audience?project=$project  -> Test users -> Add users -> $account"
}
Write-Host '   Then Data Access -> Add or Remove Scopes -> Manually add scopes, paste:'
Write-Host "   $console/auth/scopes?project=$project"
Write-Host "   $Scopes"
Write-Host ''
Write-Host "3. OAuth client: $console/auth/clients/create?project=$project"
Write-Host "   Type: Web application. Tick 'Use this client for an AI-powered agent'."
Write-Host '   Authorized redirect URIs (one per Google card):'
foreach ($uri in $RedirectUris) { Write-Host "     $uri" }
Write-Host '   Click Create and copy the Client ID and Client secret.'
Write-Host ''
Write-Host 'Last: Kiro Crew -> Settings -> OAuth Apps -> any Google card. Paste both once and Save;'
Write-Host "'Also save on ...' (on by default) fills the other five."
