# Shared Windows teardown helper for the two installer smoke scripts.
#
# ONE definition, because there are two callers that must not diverge:
# `scripts/smoke-windows-install.ps1` (nightly, real NSIS artifact) and
# `.github/scripts/test-windows-installer.ps1` (per-PR). Both boot a gateway out of
# an installed tree and then uninstall it, and both were fixing -- or in one case not
# fixing -- the same teardown bug independently. A second hand-written copy of this
# is how one of them ended up ending only the parent process while the other did not.
#
# Dot-source it:
#   . (Join-Path $PSScriptRoot 'windows-process-tree.ps1')                     # scripts/
#   . (Join-Path $PSScriptRoot '..' '..' 'scripts' 'windows-process-tree.ps1') # .github/scripts/

Set-StrictMode -Version Latest

function Stop-GatewayTree {
    <#
    .SYNOPSIS
      End a booted gateway and everything it spawned, then wait for the install
      directory to be released.

    .DESCRIPTION
      Ending the gateway is not enough to make the installed tree deletable. A
      gateway spawns the ACP backend and the managed MCP servers, and each of those
      runs the BUNDLED interpreter out of the install directory, so Windows holds
      their image files open and refuses to delete the tree. `Stop-Process` ends only
      the process it names and leaves that subtree orphaned and running, which then
      surfaces as "silent uninstall left the install directory behind" -- a failure
      attributed to the uninstaller for a residue the test rig itself created.

      So: kill the TREE (`taskkill /T`), then wait on the CONDITION the uninstall
      actually needs -- no live process whose executable lives under the install
      directory. Waiting on the condition rather than on the kill is what makes this
      honest: a tree kill can race a child that was mid-spawn, and the poll catches
      it where a one-shot kill plus a fixed sleep would not.

      The sweep is scoped by executable path to the install directory the caller
      names, which in both callers is a per-run temporary or freshly installed tree,
      so it cannot reach an unrelated process on the host.

      Best effort by design. This runs during teardown, so a failure to reap must not
      mask the verdict the run was measuring; the uninstall assertions downstream are
      what report a directory that could not be removed. It reports what it could not
      end so that failure is attributable.

    .PARAMETER Process
      The gateway process object. Ignored when already exited or $null.

    .PARAMETER InstallLocation
      The installed tree. Processes running an executable under it are ended.

    .PARAMETER TimeoutSeconds
      Bound for both the parent reap and the release poll.
    #>
    param(
        [System.Diagnostics.Process]$Process,
        [string]$InstallLocation,
        [int]$TimeoutSeconds = 30
    )

    if ($null -ne $Process -and -not $Process.HasExited) {
        $rootPid = $Process.Id
        # /T ends the whole tree, /F does not ask. Redirected because a tree whose
        # children already exited reports that on stderr, which is not a failure here.
        & taskkill.exe /PID $rootPid /T /F *> $null
        if ($LASTEXITCODE -ne 0) {
            # No taskkill, or it refused: at least end the process we started.
            Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue
        }
        # taskkill's exit code is consumed HERE and is not this script's verdict:
        # it reports 128 when a member of the tree exited on its own between the
        # listing and the kill, which is the outcome wanted. Left in place, it
        # becomes the step's exit status -- the GitHub `pwsh` step wrapper ends
        # with `exit $LASTEXITCODE` -- so a run whose every assertion passed
        # fails with no message. The wait below is what decides the tree is gone.
        $global:LASTEXITCODE = 0
        $null = $Process.WaitForExit($TimeoutSeconds * 1000)
    }
    if ([string]::IsNullOrEmpty($InstallLocation)) {
        return
    }
    $prefix = [IO.Path]::GetFullPath($InstallLocation).TrimEnd('\') + '\'
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $holders = @()
    do {
        $holders = @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                Where-Object {
                    -not [string]::IsNullOrEmpty($_.ExecutablePath) -and
                    $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
                }
        )
        if ($holders.Count -eq 0) {
            return
        }
        foreach ($holder in $holders) {
            $holderPid = $holder.ProcessId
            & taskkill.exe /PID $holderPid /T /F *> $null
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    Write-Host ("Warning: processes still running out of the install directory after " +
        "$TimeoutSeconds s: " + (($holders | ForEach-Object {
            "$($_.ProcessId) $($_.ExecutablePath)"
        }) -join '; '))
}
