# Delayed gateway restart for Windows — run as a detached process via Start-Process.
# The sleep gives the calling session time to finish responding.
# Usage (from agent):
#   Start-Process -WindowStyle Hidden powershell -ArgumentList "-ExecutionPolicy", "Bypass", "-File", "<path>\do-restart.ps1", "-KirocrewBin", "<resolved-path-to-kirocrew.exe>"
param(
    [string]$KirocrewBin = "kirocrew",
    [int]$DelaySec = 10,
    [string]$LogFile = ""
)

Start-Sleep -Seconds $DelaySec

# Resolve the binary — if a path was provided, verify it exists; otherwise fall back to PATH.
if ($KirocrewBin -ne "kirocrew" -and (Test-Path $KirocrewBin)) {
    $bin = $KirocrewBin
} else {
    $found = Get-Command kirocrew -ErrorAction SilentlyContinue
    if ($found) { $bin = $found.Source } else { $bin = $KirocrewBin }
}

# Execute the restart, capturing any errors.
try {
    $output = & $bin restart 2>&1
    if ($LogFile) { "$(Get-Date -Format o) OK: $output" | Out-File -Append -Encoding utf8 $LogFile }
} catch {
    $err = $_.Exception.Message
    if ($LogFile) { "$(Get-Date -Format o) FAIL: $err" | Out-File -Append -Encoding utf8 $LogFile }
    # Last resort: try via python module
    $venvPython = Join-Path (Split-Path (Split-Path $bin)) "python.exe"
    if (Test-Path $venvPython) {
        & $venvPython -m kiro_crew.cli restart 2>&1 | Out-Null
    }
}
