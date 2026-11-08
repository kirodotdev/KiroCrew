"""A cron child gets the PATH an agent session child gets.

A gateway launched from the Dock (launchd) or a service unit carries a bare
system PATH. ``_clean_cron_env`` is the base env of every cron spawn (the script
child, the command child, the brace probe, ``ctx.call_tool``'s MCP servers), so it
must add the well-known user tool dirs and the contributed MCP dirs the way
``acp.launch`` does for an agent child, while still stripping every denied key.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys

import pytest

from kiro_crew import env as env_mod
from kiro_crew.cron_script import _clean_cron_env

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX fixture (an executable shell script)"
)

_LAUNCHD_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture(autouse=True)
def _clean_registry(_floor_monkeypatch):
    _floor_monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    _floor_monkeypatch.setattr(env_mod, "_config_path_dirs", ())


def _tool(directory, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_a_bare_launchd_path_still_resolves_a_user_tool(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", _LAUNCHD_PATH)
    _tool(tmp_path / ".toolbox" / "bin", "mytool")

    path = _clean_cron_env()["PATH"]

    assert shutil.which("mytool", path=path) == str(tmp_path / ".toolbox" / "bin" / "mytool")
    # The gateway's own entries are all kept, in order.
    kept = [p for p in path.split(os.pathsep) if p in _LAUNCHD_PATH.split(os.pathsep)]
    assert kept == _LAUNCHD_PATH.split(os.pathsep)


def test_a_contributed_mcp_dir_trails_the_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", _LAUNCHD_PATH)
    contributed = tmp_path / "mcp-launchers"
    _tool(contributed, "launcher")
    env_mod.register_mcp_path_dirs(str(contributed))

    path = _clean_cron_env()["PATH"]

    assert path.split(os.pathsep)[-1] == str(contributed)
    assert shutil.which("launcher", path=path) == str(contributed / "launcher")


def test_denied_keys_are_still_stripped_and_an_absent_path_stays_absent(monkeypatch) -> None:
    monkeypatch.setenv("KIROCREW_INTERNAL_SECRET", "s3cret")
    monkeypatch.delenv("PATH", raising=False)

    env = _clean_cron_env()

    assert "KIROCREW_INTERNAL_SECRET" not in env
    assert "PATH" not in env
