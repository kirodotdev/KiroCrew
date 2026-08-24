$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$MaxInstallSeconds = 300

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

  [DllImport("user32.dll")]
  public static extern bool GetWindowRect(IntPtr handle, out Rect rect);
}
"@

function Save-InstallerWindow {
  param([IntPtr]$Handle, [string]$Path)

  $rect = New-Object KiroInstallerCapture+Rect
  if (-not [KiroInstallerCapture]::GetWindowRect($Handle, [ref]$rect)) {
    throw "Could not read the native installer bounds."
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
  param([string]$ProductName)

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
      if ($displayName -and $displayName.Value -like "$ProductName*") {
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

# Exercise the install-root ownership boundary with an existing directory. The
# /D argument must remain last for NSIS to parse it as an install destination.
$requestedInstallRoot = Join-Path $evidence "existing-install-parent"
$sentinel = Join-Path $requestedInstallRoot "pre-existing-user-file.txt"
New-Item -ItemType Directory -Path $requestedInstallRoot -Force | Out-Null
Set-Content -LiteralPath $sentinel -Value "must survive uninstall" -Encoding utf8NoBOM

# Capture the reverted native install-mode page as review evidence. This preview
# is closed before installation and is outside the performance measurement.
$preview = Start-Process -FilePath $installer.FullName -PassThru
$previewDeadline = [DateTime]::UtcNow.AddSeconds(30)
do {
  Start-Sleep -Milliseconds 250
  $preview.Refresh()
} while (-not $preview.HasExited -and $preview.MainWindowHandle -eq 0 -and [DateTime]::UtcNow -lt $previewDeadline)
if ($preview.HasExited -or $preview.MainWindowHandle -eq 0) {
  throw "The native installer did not show its install-mode page."
}
Start-Sleep -Milliseconds 750
Save-InstallerWindow $preview.MainWindowHandle (Join-Path $evidence "native-install-mode.png")
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
  $registrations = @(Find-InstallerRegistrations $productName)
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
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "The install removed a file that existed before setup started."
}

$installedExecutable = Join-Path $installLocation "$productFilename.exe"
if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
  throw "The installed application executable is missing: $installedExecutable"
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

$uninstallDeadline = [DateTime]::UtcNow.AddSeconds(30)
do {
  $remainingRegistrations = @(Find-InstallerRegistrations $productName)
  if ($remainingRegistrations.Count -ne 0 -or (Test-Path -LiteralPath $installLocation)) {
    Start-Sleep -Milliseconds 250
  }
} while (
  ($remainingRegistrations.Count -ne 0 -or (Test-Path -LiteralPath $installLocation)) -and
  [DateTime]::UtcNow -lt $uninstallDeadline
)
if ($remainingRegistrations.Count -ne 0) {
  throw "Silent uninstall left a $productName registration behind."
}
if (Test-Path -LiteralPath $installLocation) {
  throw "Silent uninstall left the install directory behind: $installLocation"
}
if (-not (Test-Path -LiteralPath $sentinel -PathType Leaf)) {
  throw "Silent uninstall removed content from the pre-existing parent directory."
}

if ($elapsed -gt $MaxInstallSeconds) {
  throw "Windows installation took $elapsed seconds, above the $MaxInstallSeconds-second ceiling."
}
