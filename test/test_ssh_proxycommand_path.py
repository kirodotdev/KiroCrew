"""An ssh child's ProxyCommand must find its tools under a GUI-launched gateway.

A macOS desktop-app gateway inherits launchd's minimal ``PATH``. The ssh
transport ran every ssh child (tunnel, token mint, remote restart, diagnostics
probes) with that PATH, so a host routed through a ``~/.ssh/config``
``ProxyCommand`` that runs an SSM connect helper -- which looks
``session-manager-plugin`` up by name -- failed with
``ssh exited 255: Error: session-manager-plugin is not installed`` while the
plugin sat in ``/usr/local/bin``. Only the SSM transport's ``aws`` child got the
install dirs appended.

The spawn tests below run a REAL child with exactly the ``env=`` production
hands ``ssh``. The child is a Python stand-in for ssh + its ProxyCommand: it
looks ``session-manager-plugin`` up by name on its OWN ``PATH``, which is the
lookup the proxy does. So the verdict is whether the plugin is reachable from
the child's actual environment, not whether some kwarg was passed. The stand-in
is launched through the interpreter rather than as a shell script so the same
test runs on every platform (a ``.cmd`` shim would put the remote command
line through ``cmd.exe`` parsing). The argv head production resolved is
recorded and checked separately.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.deploy import engine
from kiro_crew.instances import diagnostics, token_mint
from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel

_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
_NOT_INSTALLED = (
    "Error: session-manager-plugin is not installed. Please run the setup command first"
)

# What OpenSSH does with `ProxyCommand /abs/path/connect-helper %h`: it runs the
# proxy under its own PATH, and the proxy looks the plugin up by name. On
# success it prints what a remote `kirocrew token` would. KC_STANDIN_RC lets a
# test choose a different outcome.
_STANDIN = f"""
import os, shutil, sys
rc = os.environ.get("KC_STANDIN_RC")
if rc:
    sys.stderr.write(os.environ.get("KC_STANDIN_ERR", "") + "\\n")
    sys.exit(int(rc))
if shutil.which("session-manager-plugin") is None:
    sys.stderr.write({_NOT_INSTALLED!r} + "\\n")
    sys.stderr.write("kex_exchange_identification: Connection closed by remote host\\n")
    sys.exit(255)
sys.stdout.write("http://localhost:7777?token={_TOKEN}\\n")
"""


def _plant(directory: Path, name: str) -> Path:
    """An executable ``name`` that ``shutil.which`` resolves on this platform."""
    if os.name == "nt":
        path = directory / f"{name}.cmd"
        path.write_text("@echo off\r\n")
    else:
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return path


@pytest.fixture
def gui_gateway(monkeypatch, tmp_path):
    """A gateway PATH holding ``ssh`` only; the plugin in an install dir.

    Mirrors a Finder/Dock launch: ``ssh`` is in the system bin dir the minimal
    PATH carries, ``session-manager-plugin`` is in ``/usr/local/bin`` (here a
    tmp stand-in patched into ``_AWS_BIN_DIRS``), which that PATH lacks.

    Every ``asyncio.create_subprocess_exec`` is recorded and re-pointed at the
    interpreter running :data:`_STANDIN`, with the production ``env=`` intact.
    """
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    _plant(sysbin, "ssh")
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    _plant(install, "session-manager-plugin")
    standin = tmp_path / "ssh_standin.py"
    standin.write_text(_STANDIN)
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(sysbin))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))

    real_exec = asyncio.create_subprocess_exec
    heads: list[str] = []

    async def exec_standin(*argv, **kw):
        heads.append(argv[0])
        return await real_exec(sys.executable, str(standin), *argv[1:], **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", exec_standin)
    return sysbin, install, heads


def _is_resolved_ssh(head: str, sysbin: Path) -> bool:
    return os.path.isabs(head) and os.path.dirname(head) == str(sysbin)


@pytest.mark.asyncio
async def test_token_mint_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    token = await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    assert token == _TOKEN
    assert _is_resolved_ssh(heads[-1], sysbin)


@pytest.mark.asyncio
async def test_remote_restart_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    rc, err = await token_mint.run_remote_kirocrew("ds-abc", "restart", timeout_secs=10)
    assert (rc, err) == (0, "")
    assert _is_resolved_ssh(heads[-1], sysbin)


@pytest.mark.asyncio
async def test_diagnostics_ssh_probe_through_a_proxycommand_finds_the_plugin(gui_gateway):
    sysbin, _install, heads = gui_gateway
    assert await diagnostics._probe_ssh("ds-abc", connect_timeout_secs=5) is True
    assert _is_resolved_ssh(heads[-1], sysbin)


def test_every_ssh_spawn_gets_the_same_env_as_the_ssm_aws_child(gui_gateway):
    """One widening rule: the ssh env is the aws env for the same resolved head."""
    sysbin, install, _heads = gui_gateway
    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "-n", "ds-abc", "true"])
    assert _is_resolved_ssh(argv[0], sysbin)
    assert argv[1:] == ["-n", "ds-abc", "true"]
    assert env == engine.aws_spawn_env(argv[0])
    parts = env["PATH"].split(os.pathsep)
    # Appended: the inherited PATH keeps first claim on every name.
    assert parts == [str(sysbin), str(install)]


def test_ssh_found_only_in_the_install_dirs_is_not_widened_into_reach(monkeypatch, tmp_path):
    """No ssh on the inherited PATH: stay bare and unwidened, fail as before.

    Widening would let execvp find an ``ssh`` planted in a user-writable
    install dir that nothing vetted -- the fail-closed rule ``aws`` already has.
    """
    empty = tmp_path / "sysbin"
    empty.mkdir()
    install = tmp_path / "usr-local-bin"
    install.mkdir()
    _plant(install, "ssh")
    if os.name == "nt":
        monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", (str(install),))
    argv, env = token_mint.ssh_spawn_argv_env(["ssh", "ds-abc"])
    assert argv[0] == "ssh"
    assert env["PATH"] == str(empty)


@pytest.mark.asyncio
async def test_mint_names_the_path_searched_when_the_plugin_is_missing(monkeypatch, gui_gateway):
    sysbin, _install, _heads = gui_gateway
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())  # plugin genuinely absent
    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    msg = str(ei.value)
    assert "ProxyCommand" in msg
    assert f"({sysbin})" in msg
    assert "session-manager-plugin is not installed" in msg  # raw cause kept


@pytest.mark.asyncio
async def test_mint_does_not_blame_the_proxycommand_for_a_remote_command_not_found(
    monkeypatch, gui_gateway
):
    """A remote shell's "command not found" exits 127 and is the remote's problem."""
    monkeypatch.setenv("KC_STANDIN_RC", "127")
    monkeypatch.setenv("KC_STANDIN_ERR", "bash: line 1: kirocrew: command not found")
    with pytest.raises(token_mint.TokenMintError) as ei:
        await token_mint.mint_remote_token("ds-abc", timeout_secs=10)
    assert "ProxyCommand" not in str(ei.value)
    assert "exited 127" in str(ei.value)


def test_tunnel_classifies_a_missing_proxy_tool_before_the_transport_drop():
    """ssh's trailing "Connection closed by remote host" must not win.

    That line is what ssh prints once any ProxyCommand exits, so reporting a
    transport drop would send the reader after the network.
    """
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
    t._stderr_buf = (
        f"{_NOT_INSTALLED}\nkex_exchange_identification: Connection closed by remote host\n"
    )
    err = t._exit_error(255)
    assert err.startswith("a program your ssh config's ProxyCommand runs was not found")
    assert f"({t._child_path})" in err
    assert "session-manager-plugin is not installed" in err
    assert "transport drop" not in err


@pytest.mark.parametrize(
    "line",
    [
        "zsh:1: command not found: art",
        "/bin/bash: line 1: exec: connect-helper: not found",
        "sh: 1: exec: aws: not found",
        "SessionManagerPlugin is not found. Please refer to SessionManager Documentation here",
    ],
)
def test_tunnel_recognises_each_shells_missing_program_wording(line):
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin"
    t._stderr_buf = f"{line}\n"
    assert "ProxyCommand runs was not found" in t._exit_error(255)


def test_tunnel_only_blames_the_proxycommand_for_an_ssh_level_failure():
    """Not-found prose on a non-255 exit is not ssh's (e.g. a LocalCommand)."""
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._child_path = "/usr/bin:/bin"
    t._stderr_buf = "sh: 1: exec: helper: not found\n"
    assert "ProxyCommand" not in t._exit_error(1)


def test_a_real_auth_failure_is_still_an_auth_failure():
    t = _SshTunnel("ds-1", "ds-abc", 1, 2)
    t._stderr_buf = "user@ds-abc: Permission denied (publickey).\n"
    assert t._exit_error(255).startswith("ssh auth failed")


@pytest.mark.asyncio
async def test_tunnel_start_reports_the_missing_plugin_with_the_child_path(
    monkeypatch, gui_gateway
):
    """End to end through ``_SshTunnel.start``: real spawn, real exit, real message."""
    sysbin, _install, heads = gui_gateway
    monkeypatch.setattr(engine, "_AWS_BIN_DIRS", ())  # plugin genuinely absent
    t = _SshTunnel("ds-1", "ds-abc", 1, 2, connect_timeout_secs=10)
    assert await t.start() is False
    assert "ProxyCommand runs was not found" in t.status.error
    assert f"({sysbin})" in t.status.error
    assert _is_resolved_ssh(heads[-1], sysbin)
