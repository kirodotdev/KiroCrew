"""Windows fleet setup preserves native-shell checks and bootstraps Node parity."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/setup-windows-tests/action.yml"


def _steps():
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]


def _bash_probe():
    body = _steps()[0]["run"]
    probe = body[body.index("$system = ") : body.index("& $pwsh.Source")].rstrip()
    match = re.match(r"\$system = @\(& \$bash (--noprofile --norc -c uname)\)", probe)
    assert match, "Pass a single command token, not quoted code or an encoded stdin script"
    return probe, match[1].split()


# The exact previous payload reproduced a U+FEFF before `case` in the job log.
_OLD_STDIN_PROBE = (
    'case "$(uname -s)" in MINGW*|MSYS*) exit 0;; *) exit 1;; esac # native Windows only'
)
_NATIVE_CASES = [
    ("MINGW64_NT-10.0", 0, True),
    ("MSYS_NT-10.0", 0, True),
    ("Linux", 0, False),
    ("Darwin", 0, False),
    ("", 0, False),
    ("not-MINGW64_NT-10.0", 0, False),
    ("mingw64_NT-10.0", 0, False),
    ("MINGW64_NT-10.0\nLinux", 0, False),
    ("MINGW64_NT-10.0", 7, False),
]


def _uname_fixture(tmp_path):
    # Override only uname; both Bash and the action's PowerShell guard stay real.
    startup = tmp_path / "uname fixture.sh"
    startup.write_text(
        'uname() { printf "%s\\n" "$PROBE_SYSTEM"; return "$PROBE_EXIT"; }\n',
        encoding="ascii",
        newline="\n",
    )
    return startup.as_posix()


def _native_bash():
    if sys.platform == "win32":
        git = shutil.which("git")
        candidates = [Path(git).parent.parent / "bin/bash.exe"] if git else []
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(key):
                candidates.append(Path(os.environ[key]) / "Git/bin/bash.exe")
        return next((str(path) for path in candidates if path.is_file()), None)
    return shutil.which("bash")


def _native_powershell(name):
    """The first *name* on PATH that IS that interpreter, or None.

    ``shutil.which`` answers by NAME, and on a developer host the first ``pwsh`` on
    PATH is often a version-manager shim (mise, asdf): a symlink to the manager's
    own binary, which then refuses to run because no toolchain is pinned -- so the
    shell boundary test ran a shim and failed on the manager's error, while a host
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


def test_setup_node_uses_repo_pin_and_version_without_changing_hosted():
    steps = _steps()
    assert len(steps) == 2
    assert all(
        step["if"] == "runner.os == 'Windows' && runner.environment == 'self-hosted'"
        for step in steps
    )
    assert steps[0]["shell"] == "powershell"
    node = steps[1]
    assert re.fullmatch(r"actions/setup-node@[0-9a-f]{40}", node["uses"])
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert any(
        step.get("uses") == node["uses"]
        and step.get("with", {}).get("node-version") == node["with"]["node-version"]
        for job in ci["jobs"].values()
        for step in job.get("steps", [])
    )
    assert str(node["with"]["node-version"]) == (ROOT / ".nvmrc").read_text().strip() == "24"
    assert node["with"]["package-manager-cache"] is False
    assert all("continue-on-error" not in step for step in steps)


@pytest.mark.parametrize("ending", ["\n", "\r\n"], ids=["lf", "powershell-crlf"])
def test_bash_probe_ignores_measured_bom_stdin(tmp_path, ending):
    bash = _native_bash()
    assert bash, "Native Bash is required for the Windows bootstrap regression tests"
    _, argv = _bash_probe()
    env = {**os.environ, "BASH_ENV": _uname_fixture(tmp_path)}
    for system, exit_code, _ in _NATIVE_CASES:
        env.update(PROBE_SYSTEM=system, PROBE_EXIT=str(exit_code))
        for bom in (b"", b"\xef\xbb\xbf"):
            payload = bom + (_OLD_STDIN_PROBE + ending).encode("ascii")
            if bom:
                # Negative control: reproduce the measured failure with the old boundary.
                old = subprocess.run(
                    [bash, "--noprofile", "--norc", "-s"],
                    input=payload,
                    env=env,
                    capture_output=True,
                    cwd=tmp_path,
                    timeout=15,
                )
                assert old.returncode != 0
                assert b"syntax error near unexpected token" in old.stderr
            result = subprocess.run(
                [bash, *argv],
                input=payload,
                env=env,
                capture_output=True,
                cwd=tmp_path,
                timeout=15,
            )
            # -c uname never parses stdin, regardless of its BOM or line endings.
            assert result.returncode == exit_code, result.stderr
            assert result.stdout.decode().replace("\r\n", "\n") == system + "\n"
            assert not result.stderr, result.stderr


def _ps_quote(value):
    return "'" + value.replace("'", "''") + "'"


def _checked_probe(probe, accepted):
    # Catch only the real native-environment verdict, never a syntax/launch failure.
    return (
        "$accepted = $true\ntry {\n"
        + probe
        + "\n} catch {\n"
        + "  if ($_.Exception.Message -ne "
        + "'Git Bash did not report a native Windows environment') { throw }\n"
        + "  $accepted = $false\n}\n"
        + f"if ($accepted -ne ${str(accepted).lower()}) {{ throw 'Wrong native verdict' }}\n"
    )


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_powershell_executes_actual_native_bash_probe(tmp_path, shell):
    powershell = _native_powershell(shell)
    bash = _native_bash()
    if sys.platform == "win32":
        assert powershell and bash, "Windows must exercise PowerShell and native Git Bash"
    elif not powershell or not bash:
        pytest.skip(f"{shell} and native Bash are required for the actual shell boundary test")
    probe, _ = _bash_probe()
    startup = _uname_fixture(tmp_path)
    script = tmp_path / "probe with spaces.ps1"
    source = (
        "$ErrorActionPreference = 'Stop'\n"
        # Force the BOM-emitting encoding which broke the previous pipeline.
        + "$OutputEncoding = [Text.UTF8Encoding]::new($true)\n"
        + "$env:BASH_ENV = ''\n"
        + "$bash = "
        + _ps_quote(bash)
        + "\n"
    )
    if sys.platform == "win32" and shell == "powershell":
        source += (
            "if ($PSVersionTable.PSVersion.Major -ne 5 -or "
            "$PSVersionTable.PSVersion.Minor -ne 1) { throw 'Expected PowerShell 5.1' }\n"
        )
    # First exercise the installed uname, rejecting real Linux Bash/WSL.
    source += _checked_probe(probe, sys.platform == "win32")
    source += "$env:BASH_ENV = " + _ps_quote(startup) + "\n"
    for system, exit_code, accepted in _NATIVE_CASES:
        source += "$env:PROBE_SYSTEM = " + _ps_quote(system) + "\n"
        source += "$env:PROBE_EXIT = " + _ps_quote(str(exit_code)) + "\n"
        source += _checked_probe(probe, accepted)
    # A rejected native exit is expected test data, not the harness's exit code.
    source += "exit 0\n"
    for ending in ("\n", "\r\n"):
        script.write_bytes(source.replace("\n", ending).encode("utf-8-sig"))
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-File", str(script)],
            capture_output=True,
            cwd=tmp_path,
            timeout=60,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert not result.stderr, result.stderr
