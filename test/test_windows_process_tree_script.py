"""``Stop-GatewayTree`` must not leave taskkill's exit code behind for its caller.

``scripts/windows-process-tree.ps1`` ends a booted gateway's tree with
``taskkill /T /F``. taskkill answers 128 when a member it was asked to end has
already gone -- often with an earlier member's ``/T`` tree -- which is the outcome
the teardown wants. The GitHub ``pwsh`` step wrapper ends with
``exit $LASTEXITCODE``, so a code left in ``$LASTEXITCODE`` becomes the verdict of
a run whose every assertion passed, with no message.

These run the real script under PowerShell inside that same wrapper, with two
command shims defined before it is dot-sourced: ``taskkill.exe`` runs a real native
child that exits 128, so ``$LASTEXITCODE`` is set the way the real tool sets it,
and ``Get-CimInstance`` answers the process listing. The listed holder carries a
pid no process can have, and the harness refuses to run unless both shims are the
commands the script will reach, so no process on the host is listed or ended
except the one sleeper the gateway-path test starts for itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_windows_smoke_evidence import _native_powershell

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "windows-process-tree.ps1"

#: Lost-run ceiling for one PowerShell run, not a race to tune: a run starts one
#: interpreter and a few Python children and finishes in seconds. Half the suite's
#: 120 s ``--timeout``, so a wedge ends here -- ``run_bounded`` reaps the whole tree
#: and raises ``TimeoutExpired`` naming this bound -- instead of killing the worker.
_LOST_RUN_SECS = 60.0

#: The interpreter the shims and the gateway stand-in run. A venv's ``python.exe``
#: on Windows is a launcher that runs the real interpreter as its child, so ending
#: it would leave that child running; the base interpreter is one process.
_PYTHON = getattr(sys, "_base_executable", sys.executable)

#: The pid the holder shim reports. No process can have it, so even a shim that
#: failed to bind could not end a real process.
_HOLDER_PID = 99_999_999_999

# What GitHub runs around a `shell: pwsh` step body: `$ErrorActionPreference =
# 'stop'` first, and last the line that makes $LASTEXITCODE the step's verdict.
_STEP_HEAD = "$ErrorActionPreference = 'stop'\n"
_STEP_TAIL = "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n"

# A function shadows a native command or a cmdlet of the same name, so the
# script's `& taskkill.exe` and `Get-CimInstance` reach these instead of the host.
_SHIMS = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
function taskkill.exe {
    & $env:KC_PYTHON -I -S -B -c 'raise SystemExit(128)'
    Add-Content -LiteralPath $env:KC_TASKKILL_LOG -Value (
        ($args -join ' ') + " -> $global:LASTEXITCODE")
}
$script:listings = 0
function Get-CimInstance {
    $script:listings++
    Add-Content -LiteralPath $env:KC_LISTING_LOG -Value $script:listings
    if ($script:listings -le [int]$env:KC_HOLDER_LISTINGS) {
        $prefix = [IO.Path]::GetFullPath($env:KC_INSTALL).TrimEnd('\') + '\'
        [pscustomobject]@{
            ProcessId = [long]$env:KC_HOLDER_PID
            ExecutablePath = $prefix + 'bin\python.exe'
        }
    }
}
if ((Get-Command taskkill.exe).CommandType -ne 'Function' -or
    (Get-Command Get-CimInstance).CommandType -ne 'Function') {
    throw 'the command shims are not what the script would reach'
}
. $env:KC_SCRIPT
"""


def _run_step(tmp_path: Path, body: str, *, holder_listings: int) -> tuple[int, str, list[str]]:
    """Run *body* as a GitHub ``pwsh`` step after the shims and the script.

    Returns the step's exit code, its output, and one line per taskkill the
    script ran: its arguments and the exit code it left.
    """
    shell = _native_powershell("pwsh") or _native_powershell("powershell")
    if shell is None:
        pytest.skip("no native PowerShell on PATH (a version-manager shim does not count)")
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    install = tmp_path / "install"
    install.mkdir()
    taskkill_log = tmp_path / "taskkill.log"
    listing_log = tmp_path / "listing.log"
    step = tmp_path / "step.ps1"
    step.write_text(_STEP_HEAD + _SHIMS + body + _STEP_TAIL, encoding="utf-8")
    env = dict(os.environ)
    # PowerShell writes its startup state under HOME/XDG and its pipes under the
    # temp directory, which keeps both inside tmp_path on Linux and macOS. On
    # Windows it keeps its own startup cache under the user's LocalAppData, which
    # .NET resolves without reading the environment, as every pwsh step does.
    env.update(
        HOME=str(home),
        USERPROFILE=str(home),
        TMPDIR=str(temp),
        TMP=str(temp),
        TEMP=str(temp),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_DATA_HOME=str(home / "data"),
        POWERSHELL_TELEMETRY_OPTOUT="1",
        POWERSHELL_UPDATECHECK="Off",
        KC_SCRIPT=str(SCRIPT),
        KC_PYTHON=_PYTHON,
        KC_INSTALL=str(install),
        KC_HOLDER_PID=str(_HOLDER_PID),
        KC_HOLDER_LISTINGS=str(holder_listings),
        KC_TASKKILL_LOG=str(taskkill_log),
        KC_LISTING_LOG=str(listing_log),
    )
    quoted = str(step).replace("'", "''")
    result = run_bounded(
        # GitHub's own invocation of a step file. The policy only matters on
        # Windows, where dot-sourcing a script file is subject to it.
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        + ["-Command", f". '{quoted}'"],
        env=env,
        timeout=_LOST_RUN_SECS,
        cwd=str(tmp_path),
    )
    calls = taskkill_log.read_text(encoding="utf-8").splitlines() if taskkill_log.exists() else []
    return result.returncode, result.stdout + result.stderr, calls


def test_a_holder_that_exits_on_its_own_leaves_the_step_green(tmp_path):
    """The 128 a holder's taskkill answers is not the step's verdict."""
    code, output, calls = _run_step(
        tmp_path,
        "Stop-GatewayTree -Process $null -InstallLocation $env:KC_INSTALL -TimeoutSeconds 30",
        holder_listings=1,
    )
    # Precondition: the script reached the holder and taskkill left 128 behind.
    assert calls == [f"/PID {_HOLDER_PID} /T /F -> 128"], output
    assert "processes still running out of the install directory" not in output
    assert code == 0, f"the step exited {code}: {output}"


def test_a_holder_that_outlives_the_wait_leaves_the_step_green(tmp_path):
    """Giving up on a survivor is reported as a warning, never through the exit code."""
    code, output, calls = _run_step(
        tmp_path,
        "Stop-GatewayTree -Process $null -InstallLocation $env:KC_INSTALL -TimeoutSeconds 1",
        holder_listings=1_000_000,
    )
    assert calls, output
    assert set(calls) == {f"/PID {_HOLDER_PID} /T /F -> 128"}, output
    assert "processes still running out of the install directory" in output
    assert code == 0, f"the step exited {code}: {output}"


def test_a_refused_gateway_tree_kill_leaves_the_step_green(tmp_path):
    """The gateway path: a refused tree kill falls back to ending the gateway itself."""
    body = r"""
$gateway = Start-Process -FilePath $env:KC_PYTHON -PassThru -ArgumentList @(
    '-I', '-S', '-B', '-c', '"import time; time.sleep(60)"')
try {
    Stop-GatewayTree -Process $gateway -InstallLocation $env:KC_INSTALL -TimeoutSeconds 30
    if (-not $gateway.HasExited) { throw 'the gateway was left running' }
} finally {
    if (-not $gateway.HasExited) { Stop-Process -Id $gateway.Id -Force }
}
"""
    code, output, calls = _run_step(tmp_path, body, holder_listings=0)
    assert len(calls) == 1 and calls[0].endswith(" /T /F -> 128"), output
    assert code == 0, f"the step exited {code}: {output}"
