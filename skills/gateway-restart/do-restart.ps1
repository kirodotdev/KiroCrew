# Delayed gateway restart for Windows — run as a detached process via Start-Process.
# The sleep gives the calling session time to finish responding.
# Usage (from agent):
#   Start-Process -WindowStyle Hidden powershell -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "<path>\do-restart.ps1", "-KirocrewBin", "<resolved-path-to-kirocrew.exe>"
#
# The restart's exit status is recorded to <crew home>\logs\restart-status —
# the same file the POSIX do-restart.sh writes — so the calling agent can
# verify the outcome on its next turn instead of assuming success (see
# SKILL.md "Verify the outcome"). While the file is absent an attempt is
# pending; once present it names the exit status of the most recent attempt.
param(
    [string]$KirocrewBin = "kirocrew",
    [int]$DelaySec = 10,
    [string]$StatusFile = ""
)

$crewHome = if ($env:KIROCREW_HOME) { $env:KIROCREW_HOME } else { Join-Path $env:USERPROFILE ".kiro\crew" }
$logDir = Join-Path $crewHome "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
# Attempt-specific when the scheduler passes one (SKILL.md step 3 generates a
# per-attempt path), so overlapping restart attempts cannot overwrite each
# other's verdict; the shared default serves a lone attempt.
# CONFINEMENT INVARIANT: every attempt artifact lives inside $logDir under the
# restart-status. prefix, with no nested path and no traversal. This script
# runs detached from any agent sandbox, so an unvalidated caller-supplied path
# would let it delete and overwrite arbitrary user files; a non-conforming
# path falls back to the shared default instead.
$attemptSpecific = $false
$defaultStatus = Join-Path $logDir "restart-status"
if ($StatusFile) {
    $prefix = "$defaultStatus."
    if ($StatusFile.StartsWith($prefix)) {
        $suffix = $StatusFile.Substring($prefix.Length)
        if ($suffix -and $suffix -notmatch '[\\/]' -and $suffix -notmatch '\.\.') {
            $attemptSpecific = $true
        }
    }
}
if (-not $attemptSpecific) { $StatusFile = $defaultStatus }
# Artifact writes never follow links: each artifact is removed and re-created
# with FileMode.CreateNew, which fails on anything left or re-planted at the
# path instead of writing through it, and content goes through the held
# stream. A failed exclusive create drops the artifact rather than writing
# through a link.
function Write-ArtifactExclusive([string]$Path, [string]$Content) {
    Remove-Item -Force -ErrorAction SilentlyContinue $Path
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::CreateNew)
        try {
            $writer = New-Object System.IO.StreamWriter($fs, [System.Text.Encoding]::UTF8)
            $writer.WriteLine($Content)
            $writer.Flush()
            $writer.Dispose()
        } finally { if (-not $fs.SafeFileHandle.IsClosed) { $fs.Dispose() } }
        return $true
    } catch { return $false }
}

Remove-Item -Force -ErrorAction SilentlyContinue $StatusFile
# The diagnostic log is correlated with the attempt the same way: derived from
# the (validated) attempt status file, so a failed attempt's verifier never
# quotes another attempt's output or remedy out of a shared log. A lone
# unscheduled run keeps the shared restart.log.
$LogFile = if ($attemptSpecific) { "$StatusFile.log" } else { Join-Path $logDir "restart.log" }

Start-Sleep -Seconds $DelaySec

# Resolve the binary — if a path was provided, verify it exists; otherwise fall back to PATH.
if ($KirocrewBin -ne "kirocrew" -and (Test-Path $KirocrewBin)) {
    $bin = $KirocrewBin
} else {
    $found = Get-Command kirocrew -ErrorAction SilentlyContinue
    if ($found) { $bin = $found.Source } else { $bin = $KirocrewBin }
}

# Execute the restart, capturing any errors. $status must reflect what the
# restart verb reported: it exits non-zero when the replacement gateway never
# became ready, and that verdict is the whole point of the status file.
# The log write happens OUTSIDE the try: a failed write must not land in the
# catch and trigger the fallback, which would restart a second time.
$status = 1
$logLine = ""
try {
    $output = & $bin restart 2>&1
    $status = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }
    # The restart verb prints a fresh dashboard token URL on success; the
    # skill tells the resumed agent to quote this log, so redact the bearer.
    $redacted = "$output" -replace '([?&]token=)\S+', '$1REDACTED'
    $logLine = "$(Get-Date -Format o) exit=${status}: $redacted"
} catch {
    $err = $_.Exception.Message
    $logLine = "$(Get-Date -Format o) FAIL: $err"
    # Last resort: try via python module
    $venvPython = Join-Path (Split-Path (Split-Path $bin)) "python.exe"
    if (Test-Path $venvPython) {
        & $venvPython -m kiro_crew.cli restart 2>&1 | Out-Null
        $status = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 1 }
    }
}
if ($LogFile -and $logLine) {
    Write-ArtifactExclusive $LogFile $logLine | Out-Null
}
Write-ArtifactExclusive $StatusFile $status | Out-Null
exit $status
