# Kiro Crew - Windows build targets (pip + npm/vite + pytest).
#
# The Windows counterpart of the Makefile: same target names, same artifacts.
# It exists because `make` is not part of a Windows install (Git Bash ships no
# make, and the Windows CI runner image has none either), and the Makefile's
# recipes are POSIX-shaped throughout -- `.venv/bin/pip`, `rm -rf`, `cp -R`,
# `find -exec`, and a `bash ensure-*.sh` bootstrap whose installer paths are
# curl|sh. Teaching those recipes to branch would add GNU make as a prerequisite
# for every Windows contributor without removing a single one of the POSIX-isms.
#
# Usage:  .\make.ps1 <target> [-Py <python>]
#         .\make.ps1            # default target: test (build, then pytest)
#
# The target set is kept identical to the Makefile's .PHONY line by
# test/test_build_target_parity.py, which parses both files and fails when one
# grows a target the other lacks. Add a target to BOTH or neither.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Target = "all",

    # Interpreter used to create .venv, mirroring the Makefile's `PY ?= python3`
    # override (`make PY=python3.12 build`). Windows has no `python3` on a
    # stock install -- the name resolves to the Microsoft Store alias stub -- so
    # the default is resolved by Ensure-Python instead of being hardcoded here.
    [string]$Py = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Run from the script's own directory so relative paths hold however it was
# invoked, matching make's behavior of running recipes at the Makefile root.
$RepoRoot = $PSScriptRoot
Push-Location $RepoRoot

$VenvDir = Join-Path $RepoRoot ".venv"
# The one structural difference from the Makefile: a Windows venv puts its
# executables in Scripts\, not bin/.
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$VenvPytest = Join-Path $VenvDir "Scripts\pytest.exe"

# Package floor, kept in step with pyproject's requires-python. Duplicated from
# ensure-python.sh's MIN_MAJOR/MIN_MINOR rather than parsed: this script must
# work before any interpreter is known to exist.
$MinPyMajor = 3
$MinPyMinor = 10

function Write-Step($msg) { Write-Host "  -> $msg" -ForegroundColor Cyan }
function Write-Ok($msg) { Write-Host "  [ok] $msg" -ForegroundColor Green }
function Write-Err($msg) { Write-Host "  [ERROR] $msg" -ForegroundColor Red }

function Get-DataHome {
    if ($env:KIROCREW_HOME) { return $env:KIROCREW_HOME }
    # Never the legacy ~/.kirocrew: the one-time data-home migration deletes it,
    # so writing there would resurrect it on every build.
    return (Join-Path $env:USERPROFILE ".kiro\crew")
}

# Fail the target the way make does -- stop at the first failing command rather
# than pressing on with a half-built tree. $LASTEXITCODE is only meaningful for
# native commands, so every external invocation is followed by this.
function Assert-LastExitCode($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "$what failed (exit $LASTEXITCODE)"
    }
}

# Run a build step (npm, pip, bash, ...) and judge it by its EXIT CODE alone.
#
# The exit code is the only correct success signal for these tools: npm, pip and
# electron-builder all report ordinary progress and warnings on stderr while
# succeeding. `$ErrorActionPreference = "Stop"` promotes native stderr to a
# terminating error whenever the stream is captured or redirected, so a step is
# run with the preference relaxed and restored right after -- narrowly, so
# `Stop` still governs the cmdlet logic around it.
function Invoke-Step($exe, [string[]]$stepArgs, $what) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $exe @stepArgs
    } finally {
        $ErrorActionPreference = $prev
    }
    Assert-LastExitCode $what
}

# --- toolchain bootstrap -----------------------------------------------------

# First line of a <data-home> toolchain marker, or "" when it is absent, empty,
# or unreadable. The cast to [string] before .Trim() is what makes an empty
# marker survivable: `Get-Content` on a zero-byte file returns $null, and under
# StrictMode calling .Trim() on it raises "You cannot call a method on a
# null-valued expression" -- so a truncated marker (an interrupted
# ensure-python.sh write, a full disk) would abort the build instead of falling
# through to ordinary toolchain discovery.
function Read-Marker($path) {
    if (-not (Test-Path $path)) { return "" }
    try {
        return ([string](Get-Content $path -First 1)).Trim()
    } catch {
        return ""
    }
}

# The Windows stand-in for `bash ensure-python.sh`. That script's install path
# is `curl https://mise.run | sh`, which needs a POSIX shell; here the search is
# resolve-only and the remedy is printed rather than executed, because silently
# installing an interpreter is a bigger side effect than a build target should
# have on a developer machine.
#
# Candidate order matches ensure-python.sh: newest SUPPORTED minor first (3.12
# is the CI/build target), with 3.13 after it -- numpy 1.x ships no 3.13 Windows
# wheel, so a 3.12 that is present must win.
function Ensure-Python {
    if ($Py) {
        if (Test-PythonUsable $Py) { return (Resolve-PythonPath $Py) }
        throw "'$Py' is not a usable Python >= $MinPyMajor.$MinPyMinor"
    }

    # A recorded interpreter from a previous bootstrap, same contract as the
    # Makefile reading <data-home>/python-bin.
    $cand = Read-Marker (Join-Path (Get-DataHome) "python-bin")
    if ($cand -and (Test-PythonUsable $cand)) { return (Resolve-PythonPath $cand) }

    # `py -3.12` before a bare `python`: the launcher reports real CPython
    # installs only, so it never hands back the Store alias stub.
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("3.12", "3.11", "3.10", "3.13")) {
            $probe = Invoke-Probe "py" @("-$v", "-c", "import sys; print(sys.executable)")
            if ($probe -and (Test-PythonUsable $probe)) {
                $resolved = Resolve-PythonPath $probe
                Set-RecordedPython $resolved
                return $resolved
            }
        }
    }

    foreach ($name in @("python3.12", "python3.11", "python3.10", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $path = $cmd.Source
        if (Test-WindowsStorePythonStub $path) { continue }
        if (Test-PythonUsable $path) {
            $resolved = Resolve-PythonPath $path
            Set-RecordedPython $resolved
            return $resolved
        }
    }

    Write-Err "No Python >= $MinPyMajor.$MinPyMinor found."
    Write-Host "         Install one, then re-run:" -ForegroundColor Yellow
    Write-Host "           winget install Python.Python.3.12" -ForegroundColor Yellow
    Write-Host "         or point this target at an existing interpreter:" -ForegroundColor Yellow
    Write-Host "           .\make.ps1 backend -Py C:\path\to\python.exe" -ForegroundColor Yellow
    throw "python >= $MinPyMajor.$MinPyMinor not found"
}

# The Microsoft Store ships a zero-byte `python.exe` reparse point under
# WindowsApps whose only behavior is to open the Store page. It is on PATH by
# default, so a bare `python` lookup finds it first on a machine with no real
# CPython; running it emits the "Python was not found" nag and exits non-zero.
# Mirrors platform_compat._is_windows_store_python_stub.
function Test-WindowsStorePythonStub($path) {
    return $path -and ($path -like "*\WindowsApps\*")
}

# Run a candidate tool purely to interrogate it, returning trimmed stdout or ""
# on any failure. Every probe goes through this because $ErrorActionPreference
# = "Stop" makes native stderr a TERMINATING error in PowerShell 5.1: `py -3.12`
# on a machine without 3.12 prints "No suitable Python runtime found" to stderr,
# which would abort the whole target instead of falling through to the next
# candidate. Redirecting stderr into the success stream and discarding it keeps
# a failed probe a probe, not a build failure.
#
# Callers passing Python one-liners must quote string literals with SINGLE
# quotes: PowerShell strips inner double quotes when it builds a native
# command line, so `print("ok")` reaches python as `print(ok)` and dies with a
# NameError that this function then reports as a plain empty result.
function Invoke-Probe($exe, [string[]]$probeArgs) {
    try {
        $out = (& $exe @probeArgs 2>&1 | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
        if ($LASTEXITCODE -ne 0) { return "" }
        if (-not $out) { return "" }
        return ([string]($out | Select-Object -First 1)).Trim()
    } catch {
        return ""
    }
}

function Test-PythonUsable($path) {
    if (-not $path) { return $false }
    if (Test-WindowsStorePythonStub $path) { return $false }
    # Print rather than exit-code the comparison: Invoke-Probe already treats a
    # non-zero exit as "unusable", so a version below the floor is reported as
    # output instead of being indistinguishable from a failure to run at all.
    $code = "import sys; print('ok' if sys.version_info >= ($MinPyMajor, $MinPyMinor) else 'old')"
    return ((Invoke-Probe $path @("-c", $code)) -eq "ok")
}

# Resolve through symlinks/shims before recording. A python-build-standalone
# interpreter (what uv and mise install) locates its stdlib relative to the
# INVOKED path's dirname, so creating a venv from a shim writes the shim's
# directory into pyvenv.cfg and the venv then fails to find `encodings`.
# Same reasoning as ensure-python.sh's _resolve.
function Resolve-PythonPath($path) {
    $real = Invoke-Probe $path @("-c", "import os, sys; print(os.path.realpath(sys.executable))")
    if ($real -and (Test-Path $real)) { return $real }
    return $path
}

function Set-RecordedPython($path) {
    # Not $home: that is a read-only PowerShell automatic variable and assigning
    # to it throws.
    $dataHome = Get-DataHome
    try {
        New-Item -ItemType Directory -Force -Path $dataHome | Out-Null
        # BOM-less UTF-8, written through .NET rather than `Set-Content -Encoding
        # utf8`: PowerShell 5.1 -- still the Windows-shipped default -- emits a
        # UTF-8 BOM there, and this marker is SHARED with the Makefile, whose
        # `PY="$(cat <data-home>/python-bin)"` would then hold a path prefixed
        # with EF BB BF and fail as "No such file or directory".
        [IO.File]::WriteAllText(
            (Join-Path $dataHome "python-bin"),
            $path + "`n",
            (New-Object System.Text.UTF8Encoding($false))
        )
    } catch {
        # Advisory cache only -- a read-only or absent data home must not fail
        # the build, matching ensure-python.sh's `|| true` on the same write.
    }
}

# The Windows stand-in for `bash ensure-node.sh`, resolve-only for the same
# reason as Ensure-Python. Returns nothing: node either satisfies the floor (and
# the caller proceeds) or this throws with the remedy.
#
# Floor 22.12 is the 22.x leg of the frontend bundler's own declared range
# (vite/rolldown engines.node "^20.19.0 || >=22.12.0"); the 20.x leg is excluded
# because Node 20 is EOL. Keep in step with ensure-node.sh's MIN_VERSION.
function Ensure-Node {
    $MinNodeMajor = 22
    $MinNodeMinor = 12

    # A node-bin-dir recorded by a previous ensure-node.sh run (e.g. under WSL
    # or Git Bash) is honored the same way the Makefile honors it.
    $dir = Read-Marker (Join-Path (Get-DataHome) "node-bin-dir")
    if ($dir -and (Test-Path $dir)) { $env:PATH = "$dir;$env:PATH" }

    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        Write-Err "node not found. Install Node >= $MinNodeMajor.$MinNodeMinor and re-run:"
        Write-Host "           winget install OpenJS.NodeJS.LTS" -ForegroundColor Yellow
        throw "node not found"
    }

    $ver = Invoke-Probe $node.Source @("-v")
    if (-not $ver) { throw "node is on PATH but does not run" }
    $parts = $ver.TrimStart("v").Split(".")
    $maj = [int]$parts[0]
    $min = [int]$parts[1]
    $ok = ($maj -gt $MinNodeMajor) -or ($maj -eq $MinNodeMajor -and $min -ge $MinNodeMinor)
    if (-not $ok) {
        Write-Err "node $ver is below the frontend bundler's floor ($MinNodeMajor.$MinNodeMinor)."
        Write-Host "         Upgrade: winget install OpenJS.NodeJS.LTS" -ForegroundColor Yellow
        throw "node $ver too old"
    }
    Write-Ok "node $ver"
}

# --- targets -----------------------------------------------------------------

function Invoke-Frontend {
    Ensure-Node
    Write-Step "building the dashboard (npm + vite)"

    # npm ships as npm.cmd on Windows; PowerShell will not run a .cmd found via
    # Get-Command unless invoked through its resolved Source.
    $npm = (Get-Command npm -ErrorAction Stop).Source
    Push-Location (Join-Path $RepoRoot "website")
    try {
        if (Test-Path "package-lock.json") {
            Invoke-Step $npm @("ci", "--no-audit", "--no-fund") "npm ci"
        } else {
            Invoke-Step $npm @("install", "--no-audit", "--no-fund") "npm install"
        }
        Invoke-Step $npm @("run", "build") "npm run build"
    } finally {
        Pop-Location
    }

    # Stage into the package. The destination is CLEARED first: vite emits
    # content-hashed filenames, so copying over an existing bundle accumulates
    # stale assets that can then be served or packaged.
    $staged = Join-Path $RepoRoot "src\kiro_crew\static\dist"
    Remove-PathForce $staged
    New-Item -ItemType Directory -Force -Path (Join-Path $RepoRoot "src\kiro_crew\static") | Out-Null
    Copy-Item -Recurse -Path (Join-Path $RepoRoot "website\dist") -Destination $staged
    Write-Ok "dashboard staged into src\kiro_crew\static\dist"
}

function Invoke-Backend {
    $python = Ensure-Python
    # sys.version.split() rather than a "."-join: the join needs a double-quoted
    # separator literal, which PowerShell strips on the way to a native command.
    $ver = Invoke-Probe $python @("-c", 'import sys; print(sys.version.split()[0])')
    Write-Ok "python $ver ($python)"

    # Recreate a venv built from a too-old interpreter rather than letting the
    # editable install backtrack forever or crash at import. A venv whose
    # interpreter does not run at all (a deleted base python leaves exactly
    # that) is equally unusable, and Test-PythonUsable already reports both as
    # false.
    if ((Test-Path $VenvPython) -and -not (Test-PythonUsable $VenvPython)) {
        Write-Step "recreating .venv (existing interpreter unusable or < $MinPyMajor.$MinPyMinor)"
        Remove-PathForce $VenvDir
    }
    if (-not (Test-Path $VenvPython)) {
        Write-Step "creating .venv"
        Invoke-Step $python @("-m", "venv", $VenvDir) "python -m venv"
    }

    Write-Step "installing the backend into .venv"
    Invoke-Step $VenvPython @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel") `
        "pip install --upgrade pip setuptools wheel"

    # KIROCREW_SKIP_FRONTEND: the frontend target already staged the dist, and
    # the editable install must not try to rebuild it.
    $prev = $env:KIROCREW_SKIP_FRONTEND
    $env:KIROCREW_SKIP_FRONTEND = "1"
    try {
        Invoke-Step $VenvPython @("-m", "pip", "install", "--prefer-binary", "-e", ".[dev]") `
            "pip install -e .[dev]"
    } finally {
        $env:KIROCREW_SKIP_FRONTEND = $prev
    }
    # CI parity: also install the PEP 735 dev dependency-group (pins jsonschema
    # so the config-validation guard tests actually run). Needs pip >= 25.1 for
    # --group, which the upgrade above guarantees.
    Invoke-Step $VenvPython @("-m", "pip", "install", "--group", "dev") "pip install --group dev"
    # No resign-macos-libs step: that works around a macOS code-signing
    # validation flake on ad-hoc-signed .so files and is a no-op off Darwin.
    Write-Ok "backend installed into .venv"
}

function Invoke-Test {
    Invoke-Build
    Write-Step "running the test suite"
    Invoke-Step $VenvPytest @("-q") "pytest"
}

function Invoke-Build {
    Invoke-Frontend
    Invoke-Backend
}

function Invoke-Wheel {
    Invoke-Frontend
    Invoke-Backend
    Write-Step "building the wheel"
    Invoke-Step $VenvPython @("-m", "pip", "install", "--upgrade", "build") "pip install build"
    Invoke-Step $VenvPython @("-m", "build", "--wheel") "python -m build --wheel"
    Write-Ok "wheel written to dist\"
}

# packaging/build-desktop.sh is bash, but it already handles Windows: it
# normalizes MINGW/MSYS uname output to OS=windows and has a
# build_backend_windows path for the PBS layout (python.exe at the tree root
# rather than bin/python3.12). CI's own Windows lane invokes it through Git
# Bash for exactly this reason, so both desktop targets shell into it the same
# way rather than reimplementing ~540 lines of PBS provisioning in PowerShell.
function Get-BashPath {
    # Prefer an explicitly installed Git Bash over whatever `bash` PATH resolves
    # first. Two stubs must never win: the WindowsApps Store alias, and
    # System32\bash.exe, which is the WSL launcher present whenever WSL is
    # installed. The WSL one is the dangerous case -- it RUNS, so it is not
    # caught by an "does it execute" probe, but it drops the script into a Linux
    # distro where uname reports Linux, the repo sits under /mnt/c, and the
    # Windows PBS/electron-builder branches never execute.
    foreach ($c in @("$env:ProgramFiles\Git\bin\bash.exe",
                     "${env:ProgramFiles(x86)}\Git\bin\bash.exe",
                     "$env:LOCALAPPDATA\Programs\Git\bin\bash.exe")) {
        if (Test-Path $c) { return $c }
    }
    foreach ($cand in @(Get-Command bash -All -ErrorAction SilentlyContinue)) {
        $src = $cand.Source
        if (-not $src) { continue }
        if ($src -like "*\WindowsApps\*") { continue }
        if ($src -like "*\System32\bash.exe" -or $src -like "*\Sysnative\bash.exe") { continue }
        return $src
    }
    Write-Err "bash not found. The desktop targets run packaging/build-desktop.sh,"
    Write-Host "         which needs the Git for Windows bash:" -ForegroundColor Yellow
    Write-Host "           winget install Git.Git" -ForegroundColor Yellow
    throw "bash not found"
}

function Invoke-BackendBin {
    Invoke-Frontend
    $bash = Get-BashPath
    Write-Step "freezing the standalone backend"
    # Host-arch only (UNIVERSAL=0): a frozen backend is a local-machine
    # artifact, not a distributable app.
    $env:UNIVERSAL = "0"; $env:SKIP_FRONTEND = "1"; $env:SKIP_ELECTRON = "1"
    try {
        Invoke-Step $bash @("packaging/build-desktop.sh") "build-desktop.sh"
    } finally {
        Remove-Item Env:UNIVERSAL, Env:SKIP_FRONTEND, Env:SKIP_ELECTRON -ErrorAction SilentlyContinue
    }
}

function Invoke-Desktop {
    Ensure-Node
    $bash = Get-BashPath
    Write-Step "building the desktop app (NSIS installer)"
    Invoke-Step $bash @("packaging/build-desktop.sh") "build-desktop.sh"
    Write-Host ""
    Write-Host "  Note: the Windows installer is unsigned outside CI's signing lane," -ForegroundColor Yellow
    Write-Host "  so SmartScreen shows an 'unrecognized app' interstitial." -ForegroundColor Yellow
}

# Remove a file, directory, or directory link. Plain Remove-Item -Recurse
# follows a junction and deletes the TARGET's contents, so a staged dist that is
# a junction (how the dev-mode dist link is created on Windows -- os.symlink
# needs SeCreateSymbolicLinkPrivilege) must be unlinked, not recursed into.
function Remove-PathForce($path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($item.PSIsContainer) {
            # .Delete() on a DirectoryInfo removes the reparse point itself.
            $item.Delete()
        } else {
            Remove-Item -LiteralPath $path -Force
        }
        return
    }
    Remove-Item -LiteralPath $path -Recurse -Force
}

function Invoke-Clean {
    Write-Step "removing build artifacts and caches"
    foreach ($p in @("build", "dist", "src\kiro_crew\static\dist", "website\dist",
                     "website\electron\backend-dist", "website\electron\dist",
                     ".pytest_cache", ".mypy_cache")) {
        Remove-PathForce (Join-Path $RepoRoot $p)
    }
    foreach ($egg in @(Get-ChildItem -Path $RepoRoot, (Join-Path $RepoRoot "src") `
                       -Filter "*.egg-info" -Directory -ErrorAction SilentlyContinue)) {
        Remove-PathForce $egg.FullName
    }
    Get-ChildItem -Path $RepoRoot -Filter "__pycache__" -Directory -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-PathForce $_.FullName }
    Write-Ok "clean"
}

try {
    switch ($Target.ToLowerInvariant()) {
        "all"         { Invoke-Test }
        "build"       { Invoke-Build }
        "frontend"    { Invoke-Frontend }
        "backend"     { Invoke-Backend }
        "test"        { Invoke-Test }
        "wheel"       { Invoke-Wheel }
        "backend-bin" { Invoke-BackendBin }
        "desktop"     { Invoke-Desktop }
        "clean"       { Invoke-Clean }
        default {
            Write-Err "unknown target '$Target'"
            Write-Host "  Targets: build frontend backend test wheel backend-bin desktop clean"
            exit 2
        }
    }
} catch {
    Write-Err $_.Exception.Message
    exit 1
} finally {
    Pop-Location
}
