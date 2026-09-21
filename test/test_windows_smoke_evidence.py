"""Windows smoke logs must be discoverable by the workflow that uploads them."""

from __future__ import annotations

import base64
import glob
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from installer_test_helpers import run_bounded

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "scripts" / "smoke-windows-install.ps1"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"
LOG_NAMES = {"gateway-stdout.log", "gateway-stderr.log", "cli-version.out", "cli-version.err"}


def _native_powershell(name):
    """The first *name* on PATH that IS that interpreter, or None.

    ``shutil.which`` answers by NAME, and on a developer host the first ``pwsh`` on
    PATH is often a version-manager shim (mise, asdf): a symlink to the manager's
    own binary, which then refuses to run because no toolchain is pinned -- so these
    tests ran a shim and failed on the manager's error, while a host
    with no ``pwsh`` at all skipped. Judging the candidate by the file it RESOLVES
    to gives the same verdict on every run of one host: a real ``pwsh`` resolves to
    a file named ``pwsh``, a shim to ``mise``. Same rule as
    ``test_playwright_cli_installer._native_tool_on_path``. Windows executables
    carry PATHEXT and have no shim problem, so the ordinary lookup is right there.
    """
    if sys.platform == "win32":
        return shutil.which(name)
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        try:
            resolved = (Path(entry) / name).resolve(strict=True)
        except OSError:
            continue
        if resolved.name == name and os.access(resolved, os.X_OK):
            return str(resolved)
    return None


def _log_paths(tmp_path: Path, runner_temp: Path | None) -> dict[str, Path]:
    powershell = _native_powershell("pwsh") or _native_powershell("powershell")
    if powershell is None:
        pytest.skip("no native PowerShell on PATH (a version-manager shim does not count)")

    user_temp = tmp_path / "user temp"
    user_temp.mkdir()
    env = dict(os.environ)
    env.update(TEMP=str(user_temp), TMP=str(user_temp), SMOKE_SCRIPT=str(SMOKE_SCRIPT))
    if runner_temp is None:
        env.pop("RUNNER_TEMP", None)
    else:
        runner_temp.mkdir()
        env["RUNNER_TEMP"] = str(runner_temp)

    # Parse the real script, but execute ONLY its path assignments: no installer,
    # registry access, gateway process, or uninstall runs on the developer's host.
    command = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$tokens = $null
$errors = $null
$source = [IO.File]::ReadAllText($env:SMOKE_SCRIPT)
$ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$names = @('tempRoot', 'requestedRoot', 'cliOut', 'cliErr', 'gatewayStdout', 'gatewayStderr')
foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -is [Management.Automation.Language.AssignmentStatementAst] -and
        $statement.Left.VariablePath.UserPath -in $names) {
        . ([scriptblock]::Create($statement.Extent.Text))
    }
}
@{root=$requestedRoot; cliOut=$cliOut; cliErr=$cliErr;
  gatewayStdout=$gatewayStdout; gatewayStderr=$gatewayStderr} | ConvertTo-Json -Compress
"""
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    result = run_bounded(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env=env,
        timeout=30,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return {name: Path(value) for name, value in json.loads(result.stdout).items()}


class TestWindowsSmokeEvidence:
    def test_runner_temp_matches_upload_globs(self, tmp_path: Path) -> None:
        """Reproduce the hosted runner's distinct TEMP and RUNNER_TEMP roots."""
        runner_temp = tmp_path / "runner temp"
        paths = _log_paths(tmp_path, runner_temp)
        assert paths["root"].parent == runner_temp
        assert paths["root"].name.startswith("kirocrew-smoke-")
        paths["root"].mkdir()
        logs = {path for name, path in paths.items() if name != "root"}
        assert {path.name for path in logs} == LOG_NAMES
        for path in logs:
            assert path.parent == paths["root"]
            path.write_text("smoke evidence\n", encoding="utf-8")

        workflow = yaml.safe_load(BUILD_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["smoke-install-windows"]["steps"]
        upload = next(step for step in steps if step.get("name") == "Upload smoke evidence")
        assert upload["if"] == "always()", "failure logs must also be collected"
        patterns = upload["with"]["path"].replace("${{ runner.temp }}", runner_temp.as_posix())
        uploaded = {Path(path) for pattern in patterns.splitlines() for path in glob.glob(pattern)}
        assert uploaded == logs

    def test_local_run_falls_back_to_temp(self, tmp_path: Path) -> None:
        paths = _log_paths(tmp_path, runner_temp=None)
        assert paths["root"].parent == tmp_path / "user temp"
        assert all(path.parent == paths["root"] for name, path in paths.items() if name != "root")
