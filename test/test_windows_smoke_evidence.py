"""Windows smoke logs must be discoverable by the workflow that uploads them."""

from __future__ import annotations

import base64
import glob
import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from installer_test_helpers import run_bounded

ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = ROOT / "scripts" / "smoke-windows-install.ps1"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build-windows.yml"
LOG_NAMES = {"gateway-stdout.log", "gateway-stderr.log", "cli-version.out", "cli-version.err"}


def _log_paths(tmp_path: Path, runner_temp: Path | None) -> dict[str, Path]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        pytest.skip("PowerShell is not installed")

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
