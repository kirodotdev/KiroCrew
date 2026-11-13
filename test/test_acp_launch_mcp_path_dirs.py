"""An agent child's PATH carries the contributed MCP launcher directories.

kiro-cli starts an unwrapped bare-name MCP server itself, against the PATH it
inherits from the gateway. A gateway started without a launcher directory on its
own PATH (a service installed from a non-login shell, whose PATH snapshot never
ran the user's shell rc files) must still hand that directory to the child, at
each spawn, LAST, and without writing it anywhere that outlives the spawn.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys

import pytest
from test_acp_launch_goldens import (
    _FakeHost,
    _launch_env,
    _launch_tools,
    _Recording,
    _request,
    _run_launch,
    _Scratch,
)

from kiro_crew import env as env_mod
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.config import loader as L

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX launcher fixture (an executable shell script)"
)

_NON_LOGIN_PATH = "/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin"


@pytest.fixture(autouse=True)
def _clean_registry(_floor_monkeypatch):
    _floor_monkeypatch.setattr(env_mod, "_registered_path_dirs", ())
    _floor_monkeypatch.setattr(env_mod, "_config_path_dirs", ())


def _launcher(directory, name: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _child_path(
    monkeypatch, tmp_path, *, gateway_path: str, tools_kwargs=None, host=None, augmented=None
) -> str:
    _launch_env(monkeypatch)
    monkeypatch.setenv("PATH", gateway_path)
    rec = _Recording()
    tools = _launch_tools(rec, scratch=_Scratch(tmp_path / "scratch"))
    for key, value in (tools_kwargs or {}).items():
        object.__setattr__(tools, key, value)
    if augmented is not None:
        object.__setattr__(tools, "augmented_path", augmented)
    _run_launch(host or _FakeHost(tmp_path), _request(), tools)
    return rec.factory_kwargs["env"]["PATH"]


def _use_config(tmp_path, monkeypatch, data: dict) -> None:
    cfgp = tmp_path / "config.json"
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(L, "config_path", lambda: cfgp)
    monkeypatch.setattr(L, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(L, "config_local_path", lambda: tmp_path / "config.local.json")


def test_a_non_login_gateway_hands_the_child_a_registered_launcher_dir(
    monkeypatch, tmp_path
) -> None:
    launcher_dir = tmp_path / ".pkg-manager" / "mcp-launchers"
    launcher = _launcher(launcher_dir, "demo-mcp")
    env_mod.register_mcp_path_dirs(str(launcher_dir))

    child = _child_path(monkeypatch, tmp_path, gateway_path=_NON_LOGIN_PATH)

    assert shutil.which("demo-mcp", path=child) == str(launcher)


def test_a_dir_removed_from_the_config_is_gone_on_the_next_spawn(monkeypatch, tmp_path) -> None:
    """Read live: each spawn reloads the config, so an edit governs the next one."""
    op_dir = tmp_path / "op-bin"
    _launcher(op_dir, "demo-mcp")
    live = {"tools_kwargs": {"agent_env_overlay": agent_env_overlay}}

    _use_config(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": [str(op_dir)]}})
    first = _child_path(monkeypatch, tmp_path, gateway_path=_NON_LOGIN_PATH, **live)
    _use_config(tmp_path, monkeypatch, {"mcp": {"extra_path_dirs": []}})
    second = _child_path(monkeypatch, tmp_path, gateway_path=_NON_LOGIN_PATH, **live)

    assert str(op_dir) in first.split(os.pathsep)
    assert str(op_dir) not in second.split(os.pathsep)
    assert shutil.which("demo-mcp", path=second) is None


def test_contributed_dirs_come_after_every_existing_entry(monkeypatch, tmp_path) -> None:
    """Never ahead of, and never a repeat of, anything the child already had."""
    mcp_dir = tmp_path / "mcp"
    env_mod.register_mcp_path_dirs(str(mcp_dir), "/usr/bin")
    env_mod.publish_config_path_dirs([str(tmp_path / "op")])

    child = _child_path(monkeypatch, tmp_path, gateway_path="/opt/tools/bin:/usr/bin").split(
        os.pathsep
    )

    # The fake augmented_path appends ``/augmented``; everything before the
    # contributed dirs is exactly what the child had without them.
    assert child == ["/opt/tools/bin", "/usr/bin", "/augmented", str(tmp_path / "op"), str(mcp_dir)]


def test_nothing_contributed_leaves_the_child_path_unchanged(monkeypatch, tmp_path) -> None:
    assert _child_path(monkeypatch, tmp_path, gateway_path="/usr/bin") == "/usr/bin:/augmented"


def test_no_contributed_dir_reaches_a_unit_file_or_an_emitted_spec(monkeypatch, tmp_path) -> None:
    from kiro_crew.service.common import service_environment

    mcp_dir = str(tmp_path / "mcp")
    op_dir = str(tmp_path / "op")
    env_mod.register_mcp_path_dirs(mcp_dir)
    env_mod.publish_config_path_dirs([op_dir])
    monkeypatch.setenv("PATH", _NON_LOGIN_PATH)

    persisted = [
        service_environment(str(tmp_path))["PATH"],
        env_mod.spec_env_path(""),
        env_mod.emit_env({"PATH": ""})["PATH"],
        env_mod.emit_env({"PATH": "/opt/pinned/bin"})["PATH"],
    ]

    for value in persisted:
        entries = value.split(os.pathsep)
        assert mcp_dir not in entries and op_dir not in entries, value
    # The launch never writes the gateway's own environment either.
    _child_path(monkeypatch, tmp_path, gateway_path=_NON_LOGIN_PATH)
    assert os.environ["PATH"] == _NON_LOGIN_PATH


def test_the_append_runs_off_the_event_loop(monkeypatch, tmp_path) -> None:
    """Validating a ``~user`` entry can block on a directory service, so the launch
    hands the append to the host's guarded off-thread hop rather than calling it."""
    env_mod.register_mcp_path_dirs(str(tmp_path / "mcp"))
    host = _FakeHost(tmp_path)

    _child_path(monkeypatch, tmp_path, gateway_path=_NON_LOGIN_PATH, host=host)

    assert "append_mcp_path_dirs" in host.guarded


def test_an_empty_current_directory_entry_survives_the_append(monkeypatch, tmp_path) -> None:
    """Adding a launcher dir must not drop an existing empty (cwd) PATH entry."""
    mcp_dir = str(tmp_path / "mcp")
    env_mod.register_mcp_path_dirs(mcp_dir)

    child = _child_path(
        monkeypatch, tmp_path, gateway_path="/usr/bin::/opt/bin", augmented=lambda p: p
    )

    assert child == f"/usr/bin::/opt/bin:{mcp_dir}"
