"""How the Windows installer explains a downloaded Node that will not run.

`Try-Node` answers yes or no; when the freshly bootstrapped node.exe is
rejected, the installer must report why (exit code, stderr, launch error)
and call it a wrong architecture only when Windows refused the image for its
machine type. A policy block or a quarantined file is not an architecture
problem, and telling the user it is sends them to the wrong fix.
"""

from __future__ import annotations

import json
from pathlib import Path

from installer_test_helpers import run_bounded
from playwright_cli_installer_helpers import (
    INSTALLER_PS1,
    _isolated_tool_env,
    _usable_powershell,
    posix_only,
)

# The installer runs top-level code on load, so the tests load only the probe
# functions, pulled out of the parsed script by name.
_LOAD_PROBE = (
    "$errors = $null; "
    "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
    "'{script}', [ref]$null, [ref]$errors); "
    "if ($errors) {{ $errors | ForEach-Object {{ $_.Message }}; exit 2 }}; "
    "$names = @('Test-WrongMachineType', 'Try-Node', 'Get-AbsolutePath'); "
    "$ast.FindAll({{ param($n) $n -is "
    "[System.Management.Automation.Language.FunctionDefinitionAst] -and "
    "$names -contains $n.Name }}, $true) | "
    "ForEach-Object {{ . ([scriptblock]::Create($_.Extent.Text)) }}; "
    "$MinNodeMajor = 18; "
    "$script:NodeProbeFailure = ''; $script:NodeProbeWrongArch = $false; "
)


def _run_probe(tmp_path: Path, body: str) -> dict:
    shell = _usable_powershell(tmp_path)
    script = _LOAD_PROBE.format(script=INSTALLER_PS1) + body
    result = run_bounded(
        [shell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=_isolated_tool_env(tmp_path),
        timeout=120,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _fake_node(tmp_path: Path, stdout: str, stderr: str, code: int) -> Path:
    exe = tmp_path / "fake-node"
    exe.write_text(
        "#!/bin/sh\n"
        f"printf '%s' '{stdout}'\n"
        f"printf '%s\\n' '{stderr}' >&2\n"
        f"exit {code}\n",
        encoding="utf-8",
    )
    exe.chmod(0o755)
    return exe


_REPORT = (
    "@{ ok = $ok; failure = $script:NodeProbeFailure; "
    "arch = $script:NodeProbeWrongArch } | ConvertTo-Json -Compress"
)


@posix_only
def test_failed_probe_keeps_exit_code_and_stderr(tmp_path: Path) -> None:
    node = _fake_node(tmp_path, "", "Access is denied.", 5)
    report = _run_probe(tmp_path, f"$ok = Try-Node '{node}'; {_REPORT}")
    assert report["ok"] is False
    assert "exit code 5" in report["failure"]
    assert "Access is denied." in report["failure"]
    assert report["arch"] is False


@posix_only
def test_usable_node_clears_the_failure(tmp_path: Path) -> None:
    node = _fake_node(tmp_path, "22", "", 0)
    report = _run_probe(tmp_path, f"$ok = Try-Node '{node}'; {_REPORT}")
    assert report == {"ok": True, "failure": "", "arch": False}


@posix_only
def test_only_machine_type_errors_count_as_wrong_architecture(tmp_path: Path) -> None:
    body = (
        "$w = { param($c) [System.ComponentModel.Win32Exception]::new($c) }; "
        "@{ "
        "bad_format = Test-WrongMachineType (& $w 193); "
        "mismatch = Test-WrongMachineType (& $w 216); "
        "policy = Test-WrongMachineType (& $w 1260); "
        "nested = Test-WrongMachineType ([System.Exception]::new('outer', (& $w 193))) "
        "} | ConvertTo-Json -Compress"
    )
    assert _run_probe(tmp_path, body) == {
        "bad_format": True,
        "mismatch": True,
        "policy": False,
        "nested": True,
    }


def test_bootstrap_names_architecture_only_on_a_machine_type_refusal() -> None:
    """The downloaded-Node probe site gates the architecture wording on the
    probe's own verdict and otherwise reports what Try-Node recorded."""
    body = INSTALLER_PS1.read_text(encoding="utf-8")
    site = body.index('Try-Node (Join-Path $extracted "node.exe")')
    block = body[site : body.index("$backup = ", site)]
    arch = block.index("wrong architecture")
    assert block.index("if ($script:NodeProbeWrongArch)") < arch
    fallback = block[block.index("node.exe failed to run") :]
    assert "$script:NodeProbeFailure" in fallback
    assert "AppLocker" in fallback
    assert "wrong architecture" not in fallback
    # The staged node.exe is deleted on the way out, so the message must not
    # tell the user to run it from there.
    assert "$extracted" not in fallback
