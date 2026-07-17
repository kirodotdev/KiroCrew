# ======================================================================
#  KiroCrew — Windows setup (cloud mode)
# ======================================================================
#  KiroCrew's backend (kiro-cli) runs on macOS/Linux only, so on Windows
#  KiroCrew runs on an EC2 Linux instance in YOUR OWN AWS account and this
#  machine is the thin client. This script only ensures the client
#  prerequisites — Python, the AWS CLI, and the SSM Session Manager plugin —
#  then hands off to the Python launcher wizard (`kirocrew cloud launch`).
#
#  It NEVER stores AWS credentials: the aws CLI resolves them from your
#  configured profile/SSO.
#
#  Usage (from a clone):   powershell -ExecutionPolicy Bypass -File install.ps1
#  Options:
#     -NonInteractive   Do not launch the wizard; just install prerequisites.
#     -Size <tier>      Pass a size tier straight through (light|balanced|power).
# ======================================================================
[CmdletBinding()]
param(
    [switch]$NonInteractive,
    [string]$Size = ""
)

$ErrorActionPreference = "Stop"

function Write-Step($n, $total, $msg) { Write-Host ""; Write-Host ("  [{0}/{1}] {2}" -f $n, $total, $msg) -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  [ok] $msg"   -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  [!]  $msg"   -ForegroundColor Yellow }
function Write-Info($msg) { Write-Host "  ->  $msg"    -ForegroundColor Gray }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# Emulation-invariant architecture detection: %PROCESSOR_ARCHITECTURE% lies
# under WOW64 / Prism emulation, so read the CIM processor architecture.
function Get-RealArch {
    try {
        $arch = (Get-CimInstance Win32_Processor | Select-Object -First 1).Architecture
        switch ($arch) { 12 { return "arm64" } 9 { return "x86_64" } default { return "x86_64" } }
    } catch { return "x86_64" }
}

function Ensure-WingetOrChoco {
    if (Have "winget") { return "winget" }
    if (Have "choco")  { return "choco" }
    return ""
}

Write-Host ""
Write-Host "  KiroCrew — Windows setup (cloud mode)" -ForegroundColor Magenta
Write-Host "  Runs KiroCrew on your own AWS EC2; this PC is the client." -ForegroundColor DarkGray
$arch = Get-RealArch
Write-Info "Detected architecture: $arch"

$pm = Ensure-WingetOrChoco

# --- 1. Python -------------------------------------------------------------
Write-Step 1 4 "Python"
if (Have "python") {
    Write-Ok ("python found ({0})" -f (python --version 2>&1))
} elseif ($pm -eq "winget") {
    Write-Info "Installing Python via winget..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Write-Ok "Python installed (restart the shell if 'python' isn't found yet)"
} elseif ($pm -eq "choco") {
    choco install -y python
    Write-Ok "Python installed"
} else {
    Write-Warn "No package manager found. Install Python 3.10+ from https://python.org and re-run."
    exit 1
}

# --- 2. AWS CLI ------------------------------------------------------------
Write-Step 2 4 "AWS CLI"
if (Have "aws") {
    Write-Ok ("aws found ({0})" -f (aws --version 2>&1))
} else {
    Write-Info "Installing the AWS CLI v2 (MSI)..."
    $msi = Join-Path $env:TEMP "AWSCLIV2.msi"
    Invoke-WebRequest -Uri "https://awscli.amazonaws.com/AWSCLIV2.msi" -OutFile $msi
    Start-Process msiexec.exe -Wait -ArgumentList "/i `"$msi`" /qn"
    Remove-Item $msi -ErrorAction SilentlyContinue
    if (Have "aws") { Write-Ok "aws CLI installed" } else { Write-Warn "aws CLI installed; restart the shell so it lands on PATH" }
}

# --- 3. SSM Session Manager plugin ----------------------------------------
Write-Step 3 4 "SSM Session Manager plugin"
if (Have "session-manager-plugin") {
    Write-Ok "session-manager-plugin found"
} else {
    Write-Info "Installing the SSM Session Manager plugin..."
    $ssmMsi = Join-Path $env:TEMP "SessionManagerPluginSetup.exe"
    Invoke-WebRequest -Uri "https://session-manager-downloads.s3.amazonaws.com/plugin/latest/windows/SessionManagerPluginSetup.exe" -OutFile $ssmMsi
    Start-Process $ssmMsi -Wait -ArgumentList "/quiet"
    Remove-Item $ssmMsi -ErrorAction SilentlyContinue
    Write-Ok "session-manager-plugin installed (restart the shell if not yet on PATH)"
}

# --- 4. KiroCrew client + launch ------------------------------------------
Write-Step 4 4 "KiroCrew client"
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $repoRoot
try {
    Write-Info "Installing the KiroCrew client (pip)..."
    python -m pip install --upgrade pip *> $null
    $env:KIROCREW_SKIP_FRONTEND = "1"
    python -m pip install -e . 2>&1 | Select-Object -Last 5
    # PowerShell has no `set -e`: a failed pip install would otherwise fall
    # through to a misleading "installed" + a launch that dies opaquely. Check
    # the exit code and fail closed, matching cloud-install.sh's behavior.
    if ($LASTEXITCODE -ne 0) {
        # `finally` below runs Pop-Location before the process exits.
        Write-Warn "pip install failed (exit $LASTEXITCODE) - fix the error above, then re-run this installer."
        exit 1
    }
    Write-Ok "KiroCrew client installed"
} finally {
    Pop-Location
}

if (-not (Have "aws")) {
    Write-Warn "Restart your terminal so aws / session-manager-plugin are on PATH, then run: kirocrew cloud launch"
    exit 0
}
if (-not (Have "session-manager-plugin")) {
    # Freshly installed above, but not yet on this shell's PATH — launching now
    # would get through provisioning and then fail at the tunnel step.
    Write-Warn "session-manager-plugin is not on PATH yet. Restart your terminal, then run: kirocrew cloud launch"
    exit 0
}

# Check AWS is configured before launching.
$identity = ""
try { $identity = (aws sts get-caller-identity --output text 2>$null) } catch {}
if (-not $identity) {
    Write-Warn "AWS credentials aren't configured yet."
    Write-Info "Run one of:"
    Write-Info "   aws configure sso        (recommended)"
    Write-Info "   aws configure            (access keys)"
    Write-Info "Then run:  kirocrew cloud launch"
    exit 0
}
Write-Ok "AWS credentials resolve."

if ($NonInteractive) {
    Write-Info "Prerequisites ready. Launch when you're set:  kirocrew cloud launch"
    exit 0
}

Write-Host ""
Write-Host "  Launching KiroCrew on AWS..." -ForegroundColor Cyan
if ($Size -ne "") {
    python -m kiro_crew cloud launch --size $Size
} else {
    python -m kiro_crew cloud launch
}
