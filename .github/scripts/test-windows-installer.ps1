param(
  [switch]$SkipGatewayValidation,
  # Floor for the precompiled startup modules the installed payload must carry.
  # 1000 describes the FULL release bundle (core + voice extras). A caller that
  # bundles a narrower payload passes its own measured floor rather than having
  # this assertion silently weakened for everyone: the defect it catches is
  # bytecode filtered out of the artifact, or the launcher redirecting imports
  # into an empty user cache, and both land near zero.
  [int]$MinStartupPycs = 1000
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# A performance ceiling, deliberately tighter than the 300 s release smoke in
# scripts/smoke-windows-install.ps1. 45 green main runs (2026-10-03..04) installed
# in 39-98 s, median 60 s; 200 s is about twice the slowest, so a slow hosted
# runner alone does not fail it while a real installer slowdown still does.
$MaxInstallSeconds = 200
# Healthy boot on the runner is ~12-24 s (observed 2026-09-29). /api/ready does not await
# the Kiro CLI probe, but a tolerated probe timeout (_PROBE_TIMEOUT_SECS = 10 s in
# src/kiro_crew/kiro_prerequisite.py) still slowed one boot from 12.8 s to 30.1 s on the
# same sha (issue #11810). 24 s plus that ~17 s is ~41 s, rounded up to 50.
$MaxGatewayReadySeconds = 50

# `Stop-GatewayTree`, shared with `scripts/smoke-windows-install.ps1`. Both boot a
# gateway out of an installed tree and then uninstall it, so the teardown that makes
# the tree deletable must be ONE definition -- a second copy is how the two scripts
# came to disagree about whether ending the parent was enough.
. (Join-Path $PSScriptRoot ".." ".." "scripts" "windows-process-tree.ps1")

Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class KiroInstallerCapture {
  [StructLayout(LayoutKind.Sequential)]
  public struct Rect {
    public int Left;
    public int Top;
    public int Right;
    public int Bottom;
  }

  [DllImport("user32.dll", SetLastError = true)]
  public static extern bool GetWindowRect(IntPtr handle, out Rect rect);

  [DllImport("user32.dll")]
  public static extern bool IsWindow(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern bool IsWindowVisible(IntPtr handle);

  [DllImport("user32.dll")]
  public static extern IntPtr GetDlgItem(IntPtr dialog, int id);
}
"@

# The NSIS wizard's Cancel button (IDCANCEL), a direct child of the wizard
# window; installer.nsh reaches the same control as `GetDlgItem $HWNDPARENT 2`.
$NsisWizardCancelId = 2

# Whether a handle is the NSIS wizard rather than the short-lived window NSIS
# shows before it. When the CRC pass over the exe takes longer than a second,
# NSIS shows an unowned "Verifying installer: N%" dialog (IDD_VERIFY, a single
# static text control) and destroys it once the pass ends. Process.MainWindowHandle
# reports that dialog as the main window while it exists, so a capture that takes
# the first non-zero handle can be handed a window that is already gone.
function Test-InstallerWizardWindow {
  param([IntPtr]$Handle)

  if ($Handle -eq [IntPtr]::Zero) {
    return $false
  }
  if (-not [KiroInstallerCapture]::IsWindowVisible($Handle)) {
    return $false
  }
  return [KiroInstallerCapture]::GetDlgItem($Handle, $NsisWizardCancelId) -ne [IntPtr]::Zero
}

function Save-InstallerWindow {
  param([IntPtr]$Handle, [string]$Path)

  $rect = New-Object KiroInstallerCapture+Rect
  if (-not [KiroInstallerCapture]::GetWindowRect($Handle, [ref]$rect)) {
    $win32Error = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
    throw "Could not read the native installer bounds (Win32 error $win32Error)."
  }
  $width = $rect.Right - $rect.Left
  $height = $rect.Bottom - $rect.Top
  if ($width -le 0 -or $height -le 0) {
    throw "The native installer reported invalid bounds: ${width}x${height}."
  }

  $bitmap = New-Object System.Drawing.Bitmap $width, $height
  $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
  try {
    $graphics.CopyFromScreen($rect.Left, $rect.Top, 0, 0, $bitmap.Size)
    $bitmap.Save($Path)
  } finally {
    $graphics.Dispose()
    $bitmap.Dispose()
  }
}

function Find-InstallerRegistrations {
  param([string]$ExpectedDisplayName)

  $registrations = @()
  foreach ($location in @(
    @{
      Uninstall = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
      Install = "HKCU:\Software"
    },
    @{
      Uninstall = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*"
      Install = "HKLM:\Software"
    },
    @{
      Uninstall = "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*"
      Install = "HKLM:\Software\WOW6432Node"
    }
  )) {
    foreach ($entry in Get-ItemProperty -Path $location.Uninstall -ErrorAction SilentlyContinue) {
      $displayName = $entry.PSObject.Properties["DisplayName"]
      if ($displayName -and $displayName.Value -eq $ExpectedDisplayName) {
        $registrations += [pscustomobject]@{
          Entry = $entry
          InstallRoot = $location.Install
        }
      }
    }
  }
  return @($registrations)
}

$dist = Join-Path (Get-Location) "dist"
$evidence = Join-Path (Get-Location) "runtime-evidence"
New-Item -ItemType Directory -Path $evidence -Force | Out-Null

$installers = @(
  Get-ChildItem -Path $dist -File -Filter "*.exe" |
    Where-Object { $_.Name -match "Setup" }
)
if ($installers.Count -ne 1) {
  throw "Expected exactly one Windows setup executable in $dist; found $($installers.Count)."
}

$installer = $installers[0]
$package = Get-Content (Join-Path (Get-Location) "package.json") -Raw | ConvertFrom-Json
$productName = $package.build.productName
$productFilenameProperty = $package.build.PSObject.Properties["productFilename"]
$productFilename = if ($productFilenameProperty -and $productFilenameProperty.Value) {
  $productFilenameProperty.Value
} else {
  $productName
}
if (-not $productName -or -not $productFilename) {
  throw "package.json must define an installer product name."
}
$expectedDisplayName = "$productName $($package.version)"

# Exercise the install-root ownership boundary with an existing directory. Keep
# the install itself on the local temp volume: a synced workspace (OneDrive on a
# developer machine) can recreate or hold files while NSIS removes them, which
# tests the sync client rather than the installer. Evidence still lives under
# the workspace for artifact upload. The /D argument must remain last for NSIS.
#
# The root is kept as SHORT as the per-user default (`%LOCALAPPDATA%\Programs\
# <product>`), because the uninstaller is bound by MAX_PATH: NSIS deletes with the
# plain Win32 calls, which refuse a path of 260 characters or more, and the
# bundle's deepest files sit ~190 characters below the install root. Measured on
# the runner with a 73-character root: exactly the three bytecode files whose
# full paths came to 260, 261 and 262 characters survived a silent uninstall,
# while the 259-character neighbour was removed. A deeper root than a user's
# default would therefore fail this script on a limit the product does not
# promise to beat, and hide nothing about the default install it certifies.
$requestedInstallRoot = Join-Path ([IO.Path]::GetFullPath($env:TEMP)) "kci-$PID"
# A TREE, not one file. The boundary being tested is "the uninstaller removes the
# subdirectory it created and nothing else in the parent", and a single witness
# cannot distinguish that from a recursive delete that happens to spare one name.
# Two files at different depths plus the directory holding them can: a recursive
# sweep takes the nested one, and content is compared rather than mere existence
# so a file recreated empty is not mistaken for a survivor.
$sentinel = Join-Path $requestedInstallRoot "pre-existing-user-file.txt"
$sentinelBody = "must survive uninstall"
$sentinelDir = Join-Path $requestedInstallRoot "pre-existing-subdir"
$sentinelNested = Join-Path $sentinelDir "nested-user-file.txt"
$sentinelNestedBody = "must survive uninstall at depth"
New-Item -ItemType Directory -Path $requestedInstallRoot -Force | Out-Null
New-Item -ItemType Directory -Path $sentinelDir -Force | Out-Null
Set-Content -LiteralPath $sentinel -Value $sentinelBody -Encoding utf8NoBOM
Set-Content -LiteralPath $sentinelNested -Value $sentinelNestedBody -Encoding utf8NoBOM

function Assert-PreExistingTreeIntact {
  param([string]$Stage)

  if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
    throw "$Stage removed a file that existed before setup started: $sentinel"
  }
  if ((Get-Content -LiteralPath $sentinel -Raw).Trim() -ne $sentinelBody) {
    throw "$Stage rewrote a pre-existing file instead of leaving it alone: $sentinel"
  }
  if (-not (Test-Path -LiteralPath $sentinelDir -PathType Container)) {
    throw "$Stage removed a directory that existed before setup started: $sentinelDir"
  }
  if (-not (Test-Path -LiteralPath $sentinelNested -PathType Leaf)) {
    throw "$Stage recursed into the parent and removed nested content: $sentinelNested"
  }
  if ((Get-Content -LiteralPath $sentinelNested -Raw).Trim() -ne $sentinelNestedBody) {
    throw "$Stage rewrote nested pre-existing content: $sentinelNested"
  }
}

# Capture the reverted native install-mode page as review evidence. This preview
# is closed before installation and is outside the performance measurement.
$preview = Start-Process -FilePath $installer.FullName -PassThru
$previewDeadline = [DateTime]::UtcNow.AddSeconds(30)
$wizardHandle = [IntPtr]::Zero
do {
  Start-Sleep -Milliseconds 250
  $preview.Refresh()
  if (-not $preview.HasExited -and (Test-InstallerWizardWindow $preview.MainWindowHandle)) {
    $wizardHandle = $preview.MainWindowHandle
  }
} while (-not $preview.HasExited -and $wizardHandle -eq [IntPtr]::Zero -and [DateTime]::UtcNow -lt $previewDeadline)
if ($preview.HasExited -or $wizardHandle -eq [IntPtr]::Zero) {
  throw "The native installer did not show its install-mode page."
}
# Let the page's fade-in finish, then confirm the wizard is still the window
# being captured: it is created once and lives until the installer exits, so a
# handle that no longer names a window here is a real failure, not a race.
Start-Sleep -Milliseconds 750
if (-not [KiroInstallerCapture]::IsWindow($wizardHandle)) {
  throw "The native installer closed its install-mode page before it could be captured."
}
Save-InstallerWindow $wizardHandle (Join-Path $evidence "native-install-mode.png")
Stop-Process -Id $preview.Id -Force
$preview.WaitForExit()

# A file can never be an install root. Exercise the failure path before the
# valid install creates a registration (registered updates intentionally retain
# their existing root).
$invalidInstallTarget = Join-Path $evidence "existing-install-file"
Set-Content -LiteralPath $invalidInstallTarget -Value "not a directory" -Encoding utf8NoBOM
$invalidProcess = Start-Process -FilePath $installer.FullName -ArgumentList @(
  "/S",
  "/currentuser",
  "/D=$invalidInstallTarget"
) -PassThru
if (-not $invalidProcess.WaitForExit(30000)) {
  Stop-Process -Id $invalidProcess.Id -Force -ErrorAction SilentlyContinue
  throw "Invalid file destination did not fail within 30 seconds."
}
$invalidProcess.Refresh()
if ($invalidProcess.ExitCode -ne 2) {
  throw "Invalid file destination exited with code $($invalidProcess.ExitCode); expected 2."
}
if (-not (Test-Path -LiteralPath $invalidInstallTarget -PathType Leaf)) {
  throw "Invalid destination handling removed the pre-existing file."
}

$timer = [System.Diagnostics.Stopwatch]::StartNew()
$process = Start-Process -FilePath $installer.FullName -ArgumentList @(
  "/S",
  "/currentuser",
  "/D=$requestedInstallRoot"
) -PassThru
if (-not $process.WaitForExit($MaxInstallSeconds * 1000)) {
  Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
  throw "Windows installation exceeded the $MaxInstallSeconds-second ceiling."
}
$timer.Stop()
$process.Refresh()
if ($process.ExitCode -ne 0) {
  throw "Windows installation exited with code $($process.ExitCode)."
}

$elapsed = [Math]::Round($timer.Elapsed.TotalSeconds, 2)
$timingFile = Join-Path $evidence "install-timing.txt"
Set-Content -Path $timingFile -Value "silent-install-seconds=$elapsed" -Encoding utf8NoBOM
Write-Host "Native silent install completed in $elapsed seconds."
if ($env:GITHUB_STEP_SUMMARY) {
  Add-Content -Path $env:GITHUB_STEP_SUMMARY -Value "Windows silent install: **$elapsed seconds** (ceiling: $MaxInstallSeconds seconds)."
}

$deadline = [DateTime]::UtcNow.AddSeconds(10)
$registrations = @()
do {
  $registrations = @(Find-InstallerRegistrations $expectedDisplayName)
  if ($registrations.Count -eq 0) {
    Start-Sleep -Milliseconds 250
  }
} while ($registrations.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)

if ($registrations.Count -ne 1) {
  throw "Expected one $productName registration after install; found $($registrations.Count)."
}
$registration = $registrations[0]
$installKey = Join-Path $registration.InstallRoot $registration.Entry.PSChildName
$installEntry = Get-ItemProperty -LiteralPath $installKey -ErrorAction Stop
$installLocationProperty = $installEntry.PSObject.Properties["InstallLocation"]
$installLocation = if ($installLocationProperty) { $installLocationProperty.Value } else { $null }
if (-not $installLocation -or -not (Test-Path -LiteralPath $installLocation -PathType Container)) {
  throw "The registered install location is missing."
}
$requestedInstallRootFull = [IO.Path]::GetFullPath($requestedInstallRoot).TrimEnd("\")
$installLocationFull = [IO.Path]::GetFullPath($installLocation).TrimEnd("\")
if ($installLocationFull -eq $requestedInstallRootFull) {
  throw "The installer claimed an existing directory instead of creating an owned product subdirectory."
}
Assert-PreExistingTreeIntact "The install"

$installedExecutable = Join-Path $installLocation "$productFilename.exe"
if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
  throw "The installed application executable is missing: $installedExecutable"
}

if ($SkipGatewayValidation) {
  Write-Host "Skipping bundled gateway validation for the synthetic CI backend payload."
} else {
# Exercise the same bundled interpreter Electron launches, immediately after
# installation while Defender's post-install scanning is still active. The
# Windows package ships hash-based (unchecked) bytecode for the measured gateway
# import closure; this catches either those files being filtered out of the
# artifact or the launcher accidentally redirecting imports into an empty user
# cache again.
$backendRoot = Join-Path $installLocation "resources\backend-dist\kirocrew-backend"
$bundledPython = Join-Path $backendRoot "python.exe"
if (-not (Test-Path -LiteralPath $bundledPython -PathType Leaf)) {
  throw "The installed bundled Python is missing: $bundledPython"
}
$startupPycCount = @(
  Get-ChildItem -LiteralPath $backendRoot -Recurse -File -Filter "*.pyc"
).Count
if ($startupPycCount -lt $MinStartupPycs) {
  throw "Expected at least $MinStartupPycs precompiled startup modules; found $startupPycCount."
}

$portProbe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$portProbe.Start()
$gatewayPort = ([Net.IPEndPoint]$portProbe.LocalEndpoint).Port
$portProbe.Stop()
$gatewayHome = Join-Path $evidence "gateway-home-$PID"
$gatewayStdout = Join-Path $evidence "gateway-stdout.log"
$gatewayStderr = Join-Path $evidence "gateway-stderr.log"
New-Item -ItemType Directory -Path $gatewayHome -Force | Out-Null

# Point the ACP backend at the fake one that ships INSIDE the payload under test,
# so readiness needs no real model, no network and no sign-in. It must be a .cmd:
# KIROCREW_KIRO_BIN names something CreateProcess can run, and a .py would rely
# on a shebang, which Windows has no equivalent for. The shim runs the INSTALLED
# interpreter, so nothing outside the artifact is involved. `-B`: the gateway
# spawns its agent backend with the Python bytecode variables stripped
# (`sandbox._PYTHON_ENV_PREFIXES`, by design, so a foreign interpreter never
# inherits our cache settings), so the PYTHONPYCACHEPREFIX set below does not
# reach this shim; without `-B` its interpreter writes `__pycache__` beside the
# `kiro_crew.testing` sources, which ship without precompiled bytecode, and
# those files survive the uninstall as residue this script created.
$fakeBackend = Join-Path $evidence "kiro-backend.cmd"
Set-Content -Encoding ascii -LiteralPath $fakeBackend -Value @(
  '@echo off',
  "`"$bundledPython`" -s -B -m kiro_crew.testing.fake_acp_backend %*"
)

$savedGatewayEnv = @{
  KIROCREW_HOME = $env:KIROCREW_HOME
  KIROCREW_PROJECT_DIR = $env:KIROCREW_PROJECT_DIR
  KIRO_HOME = $env:KIRO_HOME
  KIROCREW_KIRO_BIN = $env:KIROCREW_KIRO_BIN
  KIROCREW_FAKE_ACP_TEST_MODE = $env:KIROCREW_FAKE_ACP_TEST_MODE
  KIROCREW_SKIP_MODEL_DOWNLOAD = $env:KIROCREW_SKIP_MODEL_DOWNLOAD
  PYTHONPYCACHEPREFIX = $env:PYTHONPYCACHEPREFIX
  PYTHONPATH = $env:PYTHONPATH
  PYTHONNOUSERSITE = $env:PYTHONNOUSERSITE
  PYTHONUTF8 = $env:PYTHONUTF8
  PYTHONIOENCODING = $env:PYTHONIOENCODING
}
$gatewayProcess = $null
$gatewayReady = $false
$gatewayTimer = [System.Diagnostics.Stopwatch]::StartNew()
try {
  $env:KIROCREW_HOME = $gatewayHome
  $env:KIROCREW_PROJECT_DIR = $installLocation
  $env:KIRO_HOME = Join-Path $gatewayHome "kiro"
  $env:KIROCREW_KIRO_BIN = $fakeBackend
  # Marks this as a test rig. It grants no launch privilege: the shim above is
  # exec'd by the ordinary in-place path like any other runnable file.
  $env:KIROCREW_FAKE_ACP_TEST_MODE = "1"
  # Embeddings are default-on; a ~610MB download inside a 50-second readiness
  # ceiling would fail the gate for a reason unrelated to the artifact.
  $env:KIROCREW_SKIP_MODEL_DOWNLOAD = "1"
  # Bytecode this boot compiles goes OUTSIDE the installed tree, which is what the
  # shipped desktop app does (it exports the same variable at
  # `<data home>/cache/pycache` to keep bytecode out of the codesigned bundle).
  # Leaving it unset writes `__pycache__` beside the bundled sources instead, and
  # NSIS removes only what its install log recorded -- so those post-install files
  # survive the uninstall, keep the directory non-empty, and the "silent uninstall
  # left the install directory behind" assertion below then fails on residue this
  # script created rather than on anything the uninstaller got wrong. Measured: 3
  # such files, and the startup-pyc floor is unaffected because it counts the
  # SHIPPED payload before the gateway ever runs.
  $env:PYTHONPYCACHEPREFIX = Join-Path $gatewayHome "pycache"
  $env:PYTHONPATH = $null
  $env:PYTHONNOUSERSITE = "1"
  $env:PYTHONUTF8 = "1"
  $env:PYTHONIOENCODING = "utf-8:backslashreplace"
  $gatewayProcess = Start-Process -FilePath $bundledPython -ArgumentList @(
    "-s", "-P", "-m", "kiro_crew", "gateway", "--no-open", "--port", "$gatewayPort"
  ) -WorkingDirectory $installLocation -RedirectStandardOutput $gatewayStdout `
    -RedirectStandardError $gatewayStderr -WindowStyle Hidden -PassThru

  do {
    try {
      $readyResponse = Invoke-WebRequest -UseBasicParsing `
        -Uri "http://127.0.0.1:$gatewayPort/api/ready" -TimeoutSec 1
      $gatewayReady = $readyResponse.StatusCode -eq 200
    } catch {
      $gatewayReady = $false
    }
    if (-not $gatewayReady) {
      Start-Sleep -Milliseconds 100
      $gatewayProcess.Refresh()
    }
  } while (
    -not $gatewayReady -and
    -not $gatewayProcess.HasExited -and
    $gatewayTimer.Elapsed.TotalSeconds -lt $MaxGatewayReadySeconds
  )
} finally {
  $gatewayTimer.Stop()
  # The gateway's CHILDREN are what block the uninstall, so end the TREE and then
  # wait for the install directory to be released -- not just for the parent to go.
  # A gateway spawns the ACP backend and the managed MCP servers, and every one of
  # them runs the BUNDLED interpreter out of $installLocation. Windows will not
  # delete a running executable's image, so a single surviving child makes the
  # silent uninstall leave the whole directory behind. `Stop-Process` alone ends
  # only the process named -- orphaning that tree -- which is why this failed on
  # roughly every other run with "Silent uninstall left the install directory
  # behind" while the parent had exited perfectly cleanly.
  Stop-GatewayTree $gatewayProcess $installLocation
  foreach ($name in $savedGatewayEnv.Keys) {
    if ($null -eq $savedGatewayEnv[$name]) {
      Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path "Env:$name" -Value $savedGatewayEnv[$name]
    }
  }
}

$gatewayElapsed = [Math]::Round($gatewayTimer.Elapsed.TotalSeconds, 2)
Set-Content -Path (Join-Path $evidence "gateway-startup-timing.txt") `
  -Value "gateway-ready-seconds=$gatewayElapsed" -Encoding utf8NoBOM
if (-not $gatewayReady) {
  Write-Host "Gateway stdout tail:"
  Get-Content -LiteralPath $gatewayStdout -Tail 30 -ErrorAction SilentlyContinue
  Write-Host "Gateway stderr tail:"
  Get-Content -LiteralPath $gatewayStderr -Tail 30 -ErrorAction SilentlyContinue
  $probeTimedOut = [bool](Select-String -LiteralPath $gatewayStderr -Pattern 'probe timed out' `
    -SimpleMatch -Quiet -ErrorAction SilentlyContinue)
  throw "Installed gateway was not ready within $MaxGatewayReadySeconds seconds (Kiro CLI probe timed out: $probeTimedOut)."
}
Write-Host "Installed gateway became ready in $gatewayElapsed seconds ($startupPycCount pycs)."
if ($env:GITHUB_STEP_SUMMARY) {
  Add-Content -Path $env:GITHUB_STEP_SUMMARY `
    -Value "Windows installed gateway ready: **$gatewayElapsed seconds** (ceiling: $MaxGatewayReadySeconds seconds)."
}

# The commands Settings shows for a file-only setting must run AS PASTED in each shell
# a Windows user has, against the INSTALLED launcher. Three things are proved here, none
# of them by a unit test: the gateway's own resolver names the bundle's `.cmd` shim, the
# production formatter turns that launcher into prefixes with the right quoting, and
# Windows PowerShell 5.1, PowerShell 7 and cmd.exe each deliver a padded Unicode token to
# the CLI as ONE argument (5.1 drops a separate empty argument before a `.cmd` shim, which
# is why the value rides attached as `--value-base64=<token>`).
$expectedLauncher = Join-Path $installLocation "resources\backend-dist\kirocrew-backend\bin\kirocrew.cmd"
if (-not (Test-Path -LiteralPath $expectedLauncher -PathType Leaf)) {
  throw "The installed kirocrew launcher is missing: $expectedLauncher"
}
foreach ($shellName in @("powershell.exe", "pwsh.exe")) {
  if (-not (Get-Command $shellName -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "$shellName is not available; the config-set proof needs Windows PowerShell 5.1 and PowerShell 7."
  }
}
$windowsPowerShellMajor = (& powershell.exe -NoProfile -Command '$PSVersionTable.PSVersion.Major' | Select-Object -Last 1).Trim()
if ($windowsPowerShellMajor -ne "5") {
  throw "powershell.exe reported major version $windowsPowerShellMajor; the proof needs Windows PowerShell 5.1."
}

$savedConfigProofEnv = @{
  KIROCREW_HOME = $env:KIROCREW_HOME
  PYTHONNOUSERSITE = $env:PYTHONNOUSERSITE
  PYTHONUTF8 = $env:PYTHONUTF8
  PYTHONPYCACHEPREFIX = $env:PYTHONPYCACHEPREFIX
}
$cmdDriver = Join-Path $gatewayHome "config-set-proof.cmd"
try {
  $env:KIROCREW_HOME = $gatewayHome
  $env:PYTHONNOUSERSITE = "1"
  $env:PYTHONUTF8 = "1"
  $env:PYTHONPYCACHEPREFIX = Join-Path $gatewayHome "pycache"

  # Ask the gateway's resolver, from the bundled interpreter, which launcher it hands out.
  $resolverCode = "from kiro_crew.agent import _resolve_kirocrew_bin; print(_resolve_kirocrew_bin())"
  $resolvedLauncher = (& $bundledPython -s -P -c $resolverCode | Select-Object -Last 1).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "The bundled launcher resolver exited with code $LASTEXITCODE."
  }
  if ($resolvedLauncher -ine $expectedLauncher) {
    throw "The gateway resolves the launcher '$resolvedLauncher'; expected the bundled shim '$expectedLauncher'."
  }

  # Feed that same resolved value to the production formatter.
  $helperCode = "import json, sys; from kiro_crew.kiro_prerequisite_status import config_set_hints_for_launcher as hints; print(json.dumps(hints(sys.argv[1], sys.argv[2])))"
  $hintJson = (& $bundledPython -s -P -c $helperCode $resolvedLauncher win32 | Select-Object -Last 1)
  if ($LASTEXITCODE -ne 0) {
    throw "The config-set hint formatter exited with code $LASTEXITCODE."
  }
  $hints = $hintJson | ConvertFrom-Json
  if ($hints.version -ne 1 -or $hints.encoding -ne "utf-8-base64") {
    throw "Unexpected config-set hint envelope: $hintJson"
  }
  $shellRows = @($hints.shells)
  if ($shellRows.Count -ne 2 -or $shellRows[0].id -ne "powershell" -or $shellRows[1].id -ne "cmd") {
    throw "Expected the powershell and cmd rows in order; got: $hintJson"
  }
  $powershellPrefix = $shellRows[0].prefix
  $cmdPrefix = $shellRows[1].prefix
  $expectedPowershellPrefix = "& '" + $expectedLauncher.Replace("'", "''") + "'"
  $expectedCmdPrefix = '"' + $expectedLauncher + '"'
  if ($powershellPrefix -cne $expectedPowershellPrefix) {
    throw "PowerShell prefix was '$powershellPrefix'; expected '$expectedPowershellPrefix'."
  }
  if ($cmdPrefix -cne $expectedCmdPrefix) {
    throw "cmd prefix was '$cmdPrefix'; expected '$expectedCmdPrefix'."
  }

  # `["C:/my skills/a&b's/é"]`: a space, an ampersand, an apostrophe and a non-ASCII letter,
  # padded with `==`. The accent is built from its code point so this file's encoding
  # cannot change what is being proved.
  $token = "WyJDOi9teSBza2lsbHMvYSZiJ3Mvw6kiXQ=="
  $expectedPath = "C:/my skills/a&b's/" + [char]0x00E9
  $configFile = Join-Path $gatewayHome "config.json"

  function Reset-ExtraPaths {
    # Directly through the bundled CLI, before EACH shell: a value left by an earlier
    # shell must not let a later, broken shell look green.
    & $bundledPython -s -P -m kiro_crew config set skills.extra_paths --value-base64=W10= | Out-Null
    if ($LASTEXITCODE -ne 0) {
      throw "Resetting skills.extra_paths exited with code $LASTEXITCODE."
    }
    $afterReset = @((Get-Content -LiteralPath $configFile -Raw -Encoding utf8 | ConvertFrom-Json).skills.extra_paths)
    if ($afterReset.Count -ne 0) {
      throw "skills.extra_paths was not empty after the reset: $($afterReset -join ', ')"
    }
  }

  function Assert-ExtraPathsPersisted {
    param([string]$Shell, [int]$ExitCode)

    if ($ExitCode -ne 0) {
      throw "The $Shell command exited with code $ExitCode."
    }
    $stored = @((Get-Content -LiteralPath $configFile -Raw -Encoding utf8 | ConvertFrom-Json).skills.extra_paths)
    if ($stored.Count -ne 1 -or $stored[0] -cne $expectedPath) {
      throw "$Shell persisted skills.extra_paths = [$($stored -join ' | ')]; expected exactly [$expectedPath]."
    }
  }

  $setCommand = "config set skills.extra_paths --value-base64=$token"

  Reset-ExtraPaths
  & powershell.exe -NoProfile -Command "$powershellPrefix $setCommand" | Out-Null
  Assert-ExtraPathsPersisted "Windows PowerShell 5.1" $LASTEXITCODE

  Reset-ExtraPaths
  & pwsh.exe -NoProfile -Command "$powershellPrefix $setCommand" | Out-Null
  Assert-ExtraPathsPersisted "PowerShell 7" $LASTEXITCODE

  Reset-ExtraPaths
  # `call`: a batch file that runs another one without it never comes back.
  Set-Content -Encoding ascii -LiteralPath $cmdDriver -Value @(
    "@echo off",
    "call $cmdPrefix $setCommand",
    "exit /b %ERRORLEVEL%"
  )
  $cmdRun = Start-Process -FilePath "cmd.exe" -ArgumentList "/s /c `"`"$cmdDriver`"`"" `
    -Wait -PassThru -NoNewWindow
  Assert-ExtraPathsPersisted "cmd.exe" $cmdRun.ExitCode
  Write-Host "Config-set commands persisted the padded Unicode list from Windows PowerShell 5.1, PowerShell 7 and cmd.exe."
} finally {
  Remove-Item -LiteralPath $cmdDriver -Force -ErrorAction SilentlyContinue
  foreach ($name in $savedConfigProofEnv.Keys) {
    if ($null -eq $savedConfigProofEnv[$name]) {
      Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
    } else {
      Set-Item -Path "Env:$name" -Value $savedConfigProofEnv[$name]
    }
  }
}
}

$uninstaller = Get-ChildItem -LiteralPath $installLocation -File -Filter "Uninstall*.exe" |
  Select-Object -First 1
if (-not $uninstaller) {
  throw "The installed application does not contain an uninstaller."
}

$uninstallProcess = Start-Process -FilePath $uninstaller.FullName -ArgumentList @("/S", "/currentuser") -PassThru
if (-not $uninstallProcess.WaitForExit(60000)) {
  Stop-Process -Id $uninstallProcess.Id -Force -ErrorAction SilentlyContinue
  throw "Silent uninstall exceeded 60 seconds."
}
$uninstallProcess.Refresh()
if ($uninstallProcess.ExitCode -ne 0) {
  throw "Silent uninstall exited with code $($uninstallProcess.ExitCode)."
}

# An NSIS uninstaller started without `_?=` copies itself to %TEMP% (Au_.exe /
# Un_A.exe) and relaunches, so the process waited on above can exit while the
# real uninstall is still running. Wait for that relaunched copy too (bounded at
# 120 s), so the 30 s registration poll starts when the uninstall is done. A copy
# that never exits still ends in the named failure below.
$relaunchDeadline = [DateTime]::UtcNow.AddSeconds(120)
while ([DateTime]::UtcNow -lt $relaunchDeadline -and @(Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(Au_|Un_A)$' }).Count -ne 0) {
  Start-Sleep -Milliseconds 250
}
$uninstallDeadline = [DateTime]::UtcNow.AddSeconds(30)
do {
  $remainingRegistrations = @(Find-InstallerRegistrations $expectedDisplayName)
  if ($remainingRegistrations.Count -ne 0 -or (Test-Path -LiteralPath $installLocation)) {
    Start-Sleep -Milliseconds 250
  }
} while (
  ($remainingRegistrations.Count -ne 0 -or (Test-Path -LiteralPath $installLocation)) -and
  [DateTime]::UtcNow -lt $uninstallDeadline
)
if ($remainingRegistrations.Count -ne 0) {
  throw "Silent uninstall left a $productName registration behind: $(($remainingRegistrations | ForEach-Object { $_.PSPath }) -join '; ')"
}
if (Test-Path -LiteralPath $installLocation) {
  # NAME the residue. "The directory is still there" is not actionable: the
  # uninstaller deletes what its install log recorded, so anything CREATED after
  # install -- the `__pycache__` trees the measured gateway boot writes beside the
  # bundled sources are the obvious candidate -- survives and keeps the directory
  # non-empty. Which of those it is decides whether the fix belongs in the
  # uninstaller, in this script's teardown, or in what the gateway is allowed to
  # write into its own install tree, and the answer is one listing away.
  $residue = @(
    Get-ChildItem -LiteralPath $installLocation -Recurse -Force -ErrorAction SilentlyContinue
  )
  $files = @($residue | Where-Object { -not $_.PSIsContainer })
  $summary = ($files | Group-Object {
    if ($_.FullName -match '__pycache__') { '__pycache__' } else { 'other' }
  } | ForEach-Object { "$($_.Name)=$($_.Count)" }) -join ' '
  Write-Host "Residue under ${installLocation}: $($files.Count) file(s) [$summary]"
  $files |
    Where-Object { $_.FullName -notmatch '__pycache__' } |
    Select-Object -First 40 -ExpandProperty FullName |
    ForEach-Object { Write-Host "  left: $_" }
  # The bytecode residue too, by module: which interpreter wrote it is what the
  # names tell (a `kiro_crew\testing` pyc is the fake backend shim's).
  $files |
    Where-Object { $_.FullName -match '__pycache__' } |
    Select-Object -First 20 -ExpandProperty FullName |
    ForEach-Object { Write-Host "  left (bytecode): $_" }
  throw ("Silent uninstall left the install directory behind: $installLocation " +
    "($($files.Count) file(s): $summary)")
}
Assert-PreExistingTreeIntact "Silent uninstall"

# The tree has proved ownership boundaries; leave only reusable evidence,
# not a temp directory on local developer runs. Recursive because the witness is
# a tree now -- a non-recursive remove would fail on the surviving subdirectory
# and turn a PASSING run into a red one.
Remove-Item -LiteralPath $requestedInstallRoot -Recurse -Force

if ($elapsed -gt $MaxInstallSeconds) {
  throw "Windows installation took $elapsed seconds, above the $MaxInstallSeconds-second ceiling."
}
