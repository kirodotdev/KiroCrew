"""Pins scripts/check_playwright_cli_banner.py and its scheduled workflow.

The script must accept exactly what the Browser view's real banner parser
accepts, report the CLI version and the offending stdout line on drift, and
always kill the child it launched. Fake CLIs stand in for ``playwright-cli`` so
the test runs without Node; the workflow test pins that CI installs the same
spec the installer does and runs this script.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from kiro_crew.browser_cli import install

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "check_playwright_cli_banner.py"
_WORKFLOW = ROOT / ".github" / "workflows" / "playwright-cli-banner.yml"
_spec = importlib.util.spec_from_file_location("check_playwright_cli_banner", _SCRIPT)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _fake_cli(tmp_path: Path, show_body: str) -> list[str]:
    """A CLI that answers ``--version`` with 9.8.7 and runs *show_body* for ``show``.

    ``show`` records its argv and pid so tests can assert what was launched and
    that it was killed.
    """
    script = tmp_path / "fake_cli.py"
    script.write_text(
        textwrap.dedent(f"""
            import os, sys, time
            if sys.argv[1:] == ["--version"]:
                print("9.8.7")
                sys.exit(0)
            with open({str(tmp_path / "argv.txt")!r}, "w", encoding="utf-8") as fh:
                fh.write(" ".join(sys.argv[1:]))
            with open({str(tmp_path / "pid.txt")!r}, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            """) + textwrap.dedent(show_body),
        encoding="utf-8",
    )
    return [sys.executable, str(script)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="os.kill(pid, 0) terminates the process on Windows"
)


_SERVE_FOREVER = """
while True:
    time.sleep(1)
"""


@_POSIX_ONLY
def test_current_banner_parses_and_child_is_killed(tmp_path, capsys):
    command = _fake_cli(
        tmp_path,
        "print('starting dashboard', flush=True)\n"
        "print('Listening on http://127.0.0.1:45613', flush=True)\n" + _SERVE_FOREVER,
    )
    assert gate.main(["--timeout", "20", *command]) == 0
    out = capsys.readouterr().out
    assert "OK: playwright-cli 9.8.7" in out
    assert "port 45613" in out
    assert (tmp_path / "argv.txt").read_text(encoding="utf-8") == "show --port 0 --host 127.0.0.1"
    pid = int((tmp_path / "pid.txt").read_text(encoding="utf-8"))
    assert not _pid_alive(pid)


@pytest.mark.parametrize(
    "drifted",
    [
        "Dashboard ready at http://127.0.0.1:45613",
        "Listening on http://localhost:45613",
        "Listening on 127.0.0.1:45613",
    ],
)
def test_drifted_banner_fails_naming_version_and_line(tmp_path, capsys, drifted):
    command = _fake_cli(tmp_path, f"print({drifted!r}, flush=True)\n")
    assert gate.main(["--timeout", "20", *command]) == 1
    out = capsys.readouterr().out
    assert "FAIL: playwright-cli 9.8.7" in out
    assert repr(drifted) in out
    assert "stdout closed" in out


@_POSIX_ONLY
def test_silent_cli_times_out_and_is_killed(tmp_path, capsys):
    command = _fake_cli(tmp_path, _SERVE_FOREVER)
    assert gate.main(["--timeout", "2", *command]) == 1
    out = capsys.readouterr().out
    assert "no parsable banner within 2s" in out
    assert "stdout lines read: none" in out
    pid = int((tmp_path / "pid.txt").read_text(encoding="utf-8"))
    assert not _pid_alive(pid)


def test_workflow_installs_the_installer_spec_and_runs_the_check():
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    triggers = document.get("on") or document.get(True)
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    runs = [step.get("run", "") for step in document["jobs"]["check"]["steps"]]
    assert f"npm install -g {install.NPM_SPEC}" in runs
    assert "python3 scripts/check_playwright_cli_banner.py" in runs
