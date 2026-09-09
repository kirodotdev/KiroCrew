# Install the Windows desktop artifact for real on a clean runner, boot the
# gateway it installed, then uninstall it and assert the removal.
#
# WHY A SCRIPT AND NOT INLINE YAML: the same reason
# scripts/smoke-linux-packages.sh is a script. Two callers will want the
# identical assertions (build-windows.yml today, a packaging-path PR lane later),
# and inlining is how those drift -- at which point the lane that matters is the
# one missing a check.
#
# Everything here is invisible to a unit test, because it lives in the NSIS
# metadata and the registry rather than in code we run:
#
#   * the uninstall registration, whose InstallLocation is what an in-place
#     update and the auto-updater both resolve against.
#   * the install-root OWNERSHIP boundary. The generated uninstaller removes
#     $INSTDIR recursively, so a fresh install must own a directory that did not
#     exist beforehand. installer.nsh's KiroEnsureAppInstallDir is what enforces
#     that, and nothing but a real install exercises it.
#   * the Start Menu shortcut, and specifically whether its target names the
#     bytes THIS install wrote. installer.nsh carries a whole heal path for a
#     shortcut left naming a stale sibling root, and a .lnk is a pointer no
#     build-time check can follow.
#   * the bundled CLI's reachability as its own NEW process, run from the
#     installed prefix rather than found on PATH.
#   * that the installed tree can actually BOOT. The packaged interpreter ships
#     checked-hash bytecode for the gateway import closure, so a prune that drops
#     a module produces an artifact that installs green and refuses to start.
#
# EVERY IDENTITY IS DERIVED FROM THE ARTIFACT AND THE REGISTRY, never written
# here. The nightly channel deliberately ships different ones so it can sit
# beside stable: build-desktop.sh overrides productName to "KiroCrew Nightly",
# extraMetadata.name and nsis.guid for a "-nightly." version, which changes the
# install directory, the DisplayName, the app executable's filename, the shortcut
# name and the uninstall registry key together. Hardcoding stable's spelling
# would fail this gate on every nightly build. Reading the repo's package.json
# would be just as wrong: those overrides are electron-builder CLI flags, so the
# file on disk still says "Kiro Crew" while the artifact says otherwise. The
# identity therefore comes from the registration this install CREATES, found by
# diffing the uninstall keys around it.
#
# WHAT THIS DELIBERATELY DOES NOT ASSERT: a PATH edit. The Windows installer
# makes none. `nsis` in website/electron/package.json declares no PATH handling
# and website/electron/build/installer.nsh touches only shortcuts and the
# updater cache, so there is no `kirocrew` on PATH after a desktop install and
# never has been. Asserting one would bake a false claim into a gate. The bundled
# CLI is reached at its packaged path instead, which is what
# docs/guides/windows-install.md describes and what the managed-server
# invocation actually resolves.
#
# Usage: smoke-windows-install.ps1 -DistDir <dir containing the built Setup .exe>

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$DistDir
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# `Stop-GatewayTree`, shared with `.github/scripts/test-windows-installer.ps1`.
. (Join-Path $PSScriptRoot "windows-process-tree.ps1")

# Bounded, never slept toward (flake class 2). Each ceiling is sized for a cold
# windows-latest runner with Defender scanning a fresh install.
$MaxInstallSeconds = 300
$MaxUninstallSeconds = 300
$MaxRegistrationSeconds = 30
$MaxCliSeconds = 120
$MaxHealthSeconds = 180

# The uninstall keys a per-user or per-machine NSIS install can register under.
# All three are read: /currentuser writes HKCU, and reading the machine hives too
# means a registration that lands in the wrong hive is a visible failure rather
# than a silent "found 0".
$UninstallKeys = @(
  "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall",
  "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
)

function Get-PropertyOrNull {
  <#
    StrictMode makes a direct read of an absent property a terminating error, and
    several of the values below are genuinely optional (QuietUninstallString is
    written by some NSIS configurations and not others). Ask for the property
    object rather than the value so "absent" is a return value, not a throw.
  #>
  param([Parameter(Mandatory = $true)]$Object, [Parameter(Mandatory = $true)][string]$Name)

  if ($null -eq $Object) { return $null }
  $property = $Object.PSObject.Properties[$Name]
  if ($null -eq $property) { return $null }
  return $property.Value
}

function Get-UninstallRegistrations {
  <#
    Every uninstall subkey path currently present, across all three hives.
    Returned as full key paths so a registration can be identified without
    depending on any name we chose.
  #>
  $found = @()
  foreach ($root in $UninstallKeys) {
    $children = @(Get-ChildItem -Path $root -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
      $found += "$root\$($child.PSChildName)"
    }
  }
  return @($found)
}

function Resolve-OneInstaller {
  <#
    Exactly one installer, the same rule smoke-linux-packages.sh's resolve_one
    applies and for the same reason: zero means the build silently dropped it (a
    glob that no longer matches), and more than one means an ambiguous input
    where picking arbitrarily would test bytes nobody ships.
  #>
  param([Parameter(Mandatory = $true)][string]$Directory)

  $candidates = @(Get-ChildItem -LiteralPath $Directory -File -Filter "*.exe" -ErrorAction Stop)
  if ($candidates.Count -ne 1) {
    $names = ($candidates | ForEach-Object { $_.Name }) -join ", "
    throw "Expected exactly one .exe in ${Directory}; found $($candidates.Count) [$names]."
  }
  return $candidates[0]
}

function Wait-BoundedExit {
  <#
    Run a process to completion under an explicit ceiling and return its exit
    code. Never an unbounded Wait-Process: an installer that shows a dialog on a
    headless runner would otherwise hang until the job's own timeout, which
    reports "the job timed out" instead of naming the step.
  #>
  param(
    [Parameter(Mandatory = $true)][string]$FilePath,
    [Parameter(Mandatory = $true)][string[]]$Arguments,
    [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
    [Parameter(Mandatory = $true)][string]$What,
    [string]$StdoutFile,
    [string]$StderrFile
  )

  $startArgs = @{
    FilePath = $FilePath
    ArgumentList = $Arguments
    PassThru = $true
  }
  if ($StdoutFile) {
    $startArgs["RedirectStandardOutput"] = $StdoutFile
    $startArgs["WindowStyle"] = "Hidden"
  }
  if ($StderrFile) {
    $startArgs["RedirectStandardError"] = $StderrFile
  }
  $process = Start-Process @startArgs
  if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    throw "$What exceeded its ${TimeoutSeconds}-second ceiling."
  }
  $process.Refresh()
  return $process.ExitCode
}

$distFull = (Resolve-Path -LiteralPath $DistDir).ProviderPath
$installer = Resolve-OneInstaller -Directory $distFull
Write-Host "> Installer under test: $($installer.Name)"

# The blockmap is half the shipped artifact: without it electron-updater silently
# falls back to a FULL download on every update. build-windows.yml already fails
# before upload on an orphan, so a missing one here means the artifact was
# assembled or downloaded incompletely -- worth catching before a 5-minute
# install rather than after.
$blockmap = "$($installer.FullName).blockmap"
if (-not (Test-Path -LiteralPath $blockmap -PathType Leaf)) {
  throw "Orphaned installer: $($installer.Name) has no sibling .blockmap in $distFull."
}

# A pre-existing directory handed to /D, holding a file the install must not
# touch. This is the install-root ownership boundary: the product must create and
# claim a subdirectory rather than adopt this one, because the generated
# uninstaller removes its install root recursively. Kept on the local temp volume
# deliberately -- a synced directory (OneDrive) can recreate files while NSIS
# removes them, which tests the sync client rather than the installer.
$requestedRoot = Join-Path ([IO.Path]::GetFullPath($env:TEMP)) "kirocrew-smoke-$PID"
$sentinel = Join-Path $requestedRoot "pre-existing-user-file.txt"
New-Item -ItemType Directory -Path $requestedRoot -Force | Out-Null
Set-Content -LiteralPath $sentinel -Value "must survive install and uninstall" -Encoding utf8NoBOM

$before = Get-UninstallRegistrations
Write-Host "> $($before.Count) uninstall registrations before install"

# /S silent, /currentuser so no elevation is needed on a hosted runner, and /D
# LAST -- NSIS requires it to be the final argument and treats everything after
# it as part of the path.
Write-Host "> Installing silently into an owned subdirectory of $requestedRoot"
$installTimer = [System.Diagnostics.Stopwatch]::StartNew()
$installExit = Wait-BoundedExit -FilePath $installer.FullName -Arguments @(
  "/S",
  "/currentuser",
  "/D=$requestedRoot"
) -TimeoutSeconds $MaxInstallSeconds -What "Silent install"
$installTimer.Stop()
if ($installExit -ne 0) {
  throw "Silent install exited with code $installExit."
}
$installSeconds = [Math]::Round($installTimer.Elapsed.TotalSeconds, 2)
Write-Host "> Installed in $installSeconds seconds"

# The registry write can lag the installer's own exit, so poll rather than sleep.
# A plain set difference, not Compare-Object: that cmdlet refuses an EMPTY
# -ReferenceObject on Windows PowerShell, and "no uninstall entries at all" is a
# state a hardened or freshly imaged host can genuinely be in.
$deadline = [DateTime]::UtcNow.AddSeconds($MaxRegistrationSeconds)
$added = @()
do {
  $after = Get-UninstallRegistrations
  $added = @($after | Where-Object { $before -notcontains $_ })
  if ($added.Count -eq 0) { Start-Sleep -Milliseconds 250 }
} while ($added.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)

if ($added.Count -ne 1) {
  throw "Expected exactly one NEW uninstall registration after install; found $($added.Count) [$($added -join ', ')]."
}
$registrationKey = $added[0]
$registration = Get-ItemProperty -LiteralPath $registrationKey -ErrorAction Stop

# From here on, every name comes out of this registration.
$displayName = Get-PropertyOrNull $registration "DisplayName"
$displayVersion = Get-PropertyOrNull $registration "DisplayVersion"
$installLocation = Get-PropertyOrNull $registration "InstallLocation"
if (-not $displayName) { throw "The new registration at $registrationKey has no DisplayName." }
if (-not $installLocation) { throw "$displayName registered no InstallLocation." }
Write-Host "> Registered as '$displayName' (version '$displayVersion') at $installLocation"

if (-not (Test-Path -LiteralPath $installLocation -PathType Container)) {
  throw "The registered install location does not exist: $installLocation"
}
$requestedFull = [IO.Path]::GetFullPath($requestedRoot).TrimEnd("\")
$installFull = [IO.Path]::GetFullPath($installLocation).TrimEnd("\")
if ($installFull -eq $requestedFull) {
  throw "The installer claimed the pre-existing directory instead of creating an owned product subdirectory; its uninstaller would delete $requestedFull recursively."
}
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "The install removed a file that existed before setup started: $sentinel"
}

# The app executable, derived: the one .exe in the install root that is not the
# generated uninstaller. Deriving it is what keeps this channel-agnostic --
# nightly's is "KiroCrew Nightly.exe".
$rootExes = @(Get-ChildItem -LiteralPath $installLocation -File -Filter "*.exe" |
  Where-Object { $_.Name -notlike "Uninstall*" })
if ($rootExes.Count -ne 1) {
  $names = ($rootExes | ForEach-Object { $_.Name }) -join ", "
  throw "Expected exactly one application executable in $installLocation; found $($rootExes.Count) [$names]."
}
$appExe = $rootExes[0].FullName
Write-Host "> Application executable: $appExe"

# The Start Menu shortcut, and specifically WHERE IT POINTS. Its mere existence
# proves little: installer.nsh carries a heal path for a shortcut left naming a
# stale sibling install root, because with KeepShortcuts="true" an update
# preserves whatever .lnk the previous install left behind. Reading the target is
# the only way to see that, and a .lnk target needs the shell COM object -- there
# is no pure-filesystem way to follow it.
$shortcutSearchRoots = @(
  (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
  (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Container }

$shell = New-Object -ComObject WScript.Shell
$matchingShortcuts = @()
try {
  foreach ($root in $shortcutSearchRoots) {
    foreach ($lnk in @(Get-ChildItem -LiteralPath $root -Recurse -File -Filter "*.lnk" -ErrorAction SilentlyContinue)) {
      $target = ""
      try {
        $target = $shell.CreateShortcut($lnk.FullName).TargetPath
      } catch {
        # An unreadable .lnk left by unrelated software is not this test's
        # subject; skip it rather than failing on someone else's shortcut.
        continue
      }
      if ($target -and ([IO.Path]::GetFullPath($target) -ieq [IO.Path]::GetFullPath($appExe))) {
        $matchingShortcuts += $lnk.FullName
      }
    }
  }
} finally {
  [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
}
if ($matchingShortcuts.Count -lt 1) {
  throw "No Start Menu shortcut targets the installed executable $appExe."
}
Write-Host "> Start Menu shortcut targets this install: $($matchingShortcuts[0])"

# The bundled CLI, invoked as a NEW process. Not a PATH lookup: the installer
# makes no PATH edit (see the header). What is asserted is that the packaged
# launcher runs standalone, before this script sets any KIROCREW_* or PYTHON*
# variable, so nothing this script arranged can be what made it work.
$backendRoot = Join-Path $installLocation "resources\backend-dist\kirocrew-backend"
$bundledCli = Join-Path $backendRoot "bin\kirocrew.cmd"
$bundledPython = Join-Path $backendRoot "python.exe"
foreach ($required in @($backendRoot, $bundledCli, $bundledPython)) {
  if (-not (Test-Path -LiteralPath $required)) {
    throw "The installed backend payload is incomplete; missing: $required"
  }
}

$cliOut = Join-Path $requestedRoot "cli-version.out"
$cliErr = Join-Path $requestedRoot "cli-version.err"
# The launcher itself as the process image, NOT `cmd.exe /c "<launcher>" args`:
# cmd.exe's own quote handling for /c is what makes that form fragile once the
# install path contains spaces, and it buys nothing here. Start-Process runs the
# .cmd through the command interpreter anyway, and this is a genuinely NEW
# process with no state from this shell beyond the environment.
$cliExit = Wait-BoundedExit -FilePath $bundledCli -Arguments @("--version") `
  -TimeoutSeconds $MaxCliSeconds -What "Bundled CLI --version" `
  -StdoutFile $cliOut -StderrFile $cliErr
if ($cliExit -ne 0) {
  Write-Host "CLI stdout:"; Get-Content -LiteralPath $cliOut -ErrorAction SilentlyContinue
  Write-Host "CLI stderr:"; Get-Content -LiteralPath $cliErr -ErrorAction SilentlyContinue
  throw "The bundled CLI exited with code $cliExit; the installed tree cannot run its own entry point."
}
$cliText = (Get-Content -LiteralPath $cliOut -Raw -ErrorAction SilentlyContinue)
if (-not $cliText) { $cliText = "" }
Write-Host "> Bundled CLI reports: $($cliText.Trim())"
if ($displayVersion -and ($cliText -notmatch [Regex]::Escape($displayVersion))) {
  # The installed CLI and the registration must describe ONE build. A mismatch is
  # a stamping defect: build-windows.yml stamps __init__.py and the Electron
  # package.json separately, so they can disagree, and the auto-updater's compare
  # gate reads the version the app reports.
  throw "The bundled CLI reported '$($cliText.Trim())' but the registration says '$displayVersion'; the installed backend and the installer disagree about the build."
}

# Boot the gateway from the INSTALLED prefix, using the installed interpreter,
# against the fake ACP backend that ships inside the same payload. The point is
# that these bytes start: the packaged interpreter carries checked-hash bytecode
# for the gateway import closure, so a prune that drops a module produces an
# artifact that installs green and refuses to boot.
$gatewayHome = Join-Path $requestedRoot "gateway-home"
$gatewayStdout = Join-Path $requestedRoot "gateway-stdout.log"
$gatewayStderr = Join-Path $requestedRoot "gateway-stderr.log"
New-Item -ItemType Directory -Path $gatewayHome -Force | Out-Null

# A .cmd shim, because KIROCREW_KIRO_BIN must name something CreateProcess can
# run: a .py relies on a shebang, which Windows has no equivalent for. It runs
# the fake backend out of the INSTALLED payload through the INSTALLED
# interpreter, so nothing from the repository checkout is involved.
$fakeBackendLauncher = Join-Path $requestedRoot "kiro-backend.cmd"
Set-Content -LiteralPath $fakeBackendLauncher -Encoding ascii -Value @(
  "@echo off",
  "`"$bundledPython`" -s -m kiro_crew.testing.fake_acp_backend %*"
)

$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$listener.Start()
$gatewayPort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

$savedEnv = @{}
$gatewayEnv = @{
  KIROCREW_HOME = $gatewayHome
  KIRO_HOME = (Join-Path $gatewayHome "kiro")
  KIROCREW_WORKSPACE = (Join-Path $gatewayHome "workspace")
  KIROCREW_KIRO_BIN = $fakeBackendLauncher
  KIROCREW_FAKE_ACP_TEST_MODE = "1"
  # Embeddings are default-on; never let a smoke test kick the ~610MB download.
  KIROCREW_SKIP_MODEL_DOWNLOAD = "1"
  # The launcher must not inherit a PYTHONPATH from this job, and bytecode this
  # boot compiles goes OUTSIDE the installed tree -- the same thing the shipped
  # desktop app does. Left unset, `__pycache__` lands beside the bundled sources,
  # and NSIS removes only what its install log recorded, so those post-install
  # files survive and make the uninstall assertion below fail on residue this
  # script created rather than on an uninstaller defect.
  PYTHONPYCACHEPREFIX = (Join-Path $gatewayHome "pycache")
  PYTHONPATH = $null
  PYTHONNOUSERSITE = "1"
  PYTHONUTF8 = "1"
  PYTHONIOENCODING = "utf-8:backslashreplace"
}

$gateway = $null
$healthy = $false
$healthSeconds = 0
try {
  foreach ($name in $gatewayEnv.Keys) {
    $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name)
    if ($null -eq $gatewayEnv[$name]) {
      Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path "Env:$name" -Value $gatewayEnv[$name]
    }
  }

  # -s: no user site-packages, so the installed payload is what runs.
  $gateway = Start-Process -FilePath $bundledPython -ArgumentList @(
    "-s", "-m", "kiro_crew", "gateway", "--no-open", "--port", "$gatewayPort"
  ) -WorkingDirectory $installLocation -RedirectStandardOutput $gatewayStdout `
    -RedirectStandardError $gatewayStderr -WindowStyle Hidden -PassThru

  $healthTimer = [System.Diagnostics.Stopwatch]::StartNew()
  do {
    try {
      $response = Invoke-WebRequest -UseBasicParsing `
        -Uri "http://127.0.0.1:$gatewayPort/api/health" -TimeoutSec 2
      $healthy = $response.StatusCode -eq 200
    } catch {
      $healthy = $false
    }
    if (-not $healthy) {
      Start-Sleep -Milliseconds 250
      $gateway.Refresh()
    }
  } while (
    -not $healthy -and
    -not $gateway.HasExited -and
    $healthTimer.Elapsed.TotalSeconds -lt $MaxHealthSeconds
  )
  $healthTimer.Stop()
  $healthSeconds = [Math]::Round($healthTimer.Elapsed.TotalSeconds, 2)
} finally {
  # End the TREE, not just the parent, and wait for the installed tree to be
  # released. The gateway spawns the ACP backend and the managed MCP servers, and
  # each runs the BUNDLED interpreter out of $installLocation -- Windows will not
  # delete a running executable's image, so one surviving child makes the silent
  # uninstall below leave the whole directory behind and reads as an uninstaller
  # defect for residue this script created.
  Stop-GatewayTree $gateway $installLocation
  foreach ($name in $savedEnv.Keys) {
    if ($null -eq $savedEnv[$name]) {
      Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path "Env:$name" -Value $savedEnv[$name]
    }
  }
}

if (-not $healthy) {
  Write-Host "Gateway stdout tail:"
  Get-Content -LiteralPath $gatewayStdout -Tail 40 -ErrorAction SilentlyContinue
  Write-Host "Gateway stderr tail:"
  Get-Content -LiteralPath $gatewayStderr -Tail 40 -ErrorAction SilentlyContinue
  throw "The installed gateway did not answer /api/health within $MaxHealthSeconds seconds."
}
Write-Host "> Installed gateway answered /api/health in $healthSeconds seconds"

# Silent uninstall, driven by the string the registration itself carries.
# QuietUninstallString when present (it already implies silence); otherwise
# UninstallString plus /S. Never a path composed here: only the registration
# knows what this channel's uninstaller is called.
$quiet = Get-PropertyOrNull $registration "QuietUninstallString"
$uninstallString = Get-PropertyOrNull $registration "UninstallString"
if ($quiet) {
  $uninstallPath = $quiet.Trim('"')
  $uninstallArgs = @("/currentuser")
  # A QuietUninstallString may carry its own flags after the executable. Split
  # only on the closing quote so a path with spaces survives.
  if ($quiet -match '^"([^"]+)"\s*(.*)$') {
    $uninstallPath = $Matches[1]
    $extra = $Matches[2].Trim()
    if ($extra) { $uninstallArgs = @($extra.Split(" ")) + $uninstallArgs }
  }
} elseif ($uninstallString) {
  $uninstallPath = $uninstallString.Trim('"')
  if ($uninstallString -match '^"([^"]+)"') { $uninstallPath = $Matches[1] }
  $uninstallArgs = @("/S", "/currentuser")
} else {
  throw "$displayName registered neither QuietUninstallString nor UninstallString; it cannot be uninstalled."
}
if (-not (Test-Path -LiteralPath $uninstallPath -PathType Leaf)) {
  throw "The registered uninstaller is missing: $uninstallPath"
}

Write-Host "> Uninstalling silently via $uninstallPath"
$uninstallExit = Wait-BoundedExit -FilePath $uninstallPath -Arguments $uninstallArgs `
  -TimeoutSeconds $MaxUninstallSeconds -What "Silent uninstall"
if ($uninstallExit -ne 0) {
  throw "Silent uninstall exited with code $uninstallExit."
}

# NSIS copies itself to TEMP and returns before the copy finishes removing the
# tree, so poll for the removal instead of asserting it once.
$deadline = [DateTime]::UtcNow.AddSeconds($MaxUninstallSeconds)
do {
  $registrationGone = -not (Test-Path -LiteralPath $registrationKey)
  $treeGone = -not (Test-Path -LiteralPath $installLocation)
  if (-not ($registrationGone -and $treeGone)) { Start-Sleep -Milliseconds 500 }
} while (-not ($registrationGone -and $treeGone) -and [DateTime]::UtcNow -lt $deadline)

if (-not $registrationGone) {
  throw "The uninstall left its registration behind at $registrationKey."
}
if (-not $treeGone) {
  $leftoverFiles = @(
    Get-ChildItem -LiteralPath $installLocation -Recurse -Force -File -ErrorAction SilentlyContinue |
      Select-Object -ExpandProperty FullName
  )
  throw ("The uninstall left the install tree behind at $installLocation " +
    "($($leftoverFiles.Count) file(s): $($leftoverFiles -join ', '))")
}
# The data home survives an uninstall BY DESIGN (nsis.deleteAppDataOnUninstall
# stays false, and ~/.kiro/crew holds sessions and the database), so the scratch
# home is expected to remain. What must NOT survive is the install tree, and what
# must NOT be removed is the pre-existing file the install was pointed at.
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "The uninstall removed a file that existed before setup started: $sentinel"
}

Write-Host "> Uninstalled; registration and install tree are gone and the pre-existing file survived"

if ($env:GITHUB_STEP_SUMMARY) {
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value @(
    "**Windows install-and-boot smoke** ($displayName $displayVersion)",
    "",
    "| Step | Result |",
    "| --- | --- |",
    "| Silent install | $installSeconds s (ceiling $MaxInstallSeconds s) |",
    "| Installed gateway /api/health | $healthSeconds s (ceiling $MaxHealthSeconds s) |",
    "| Start Menu shortcut target | matches this install |",
    "| Silent uninstall | registration and tree removed |"
  )
}

Write-Host "Windows install-and-boot smoke passed for '$displayName'."
