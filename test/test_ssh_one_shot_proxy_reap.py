"""A finished ssh leaves no ``ProxyCommand`` process running behind it.

On exit ssh sends its ``ProxyCommand`` only SIGHUP. A proxy that ignores it and
lets go of its stdio keeps running, re-parented to init, so every failed mint,
remote run, diagnosis probe or tunnel start during an outage left one more behind.
These tests spawn a real POSIX tree of that shape: an ssh stand-in that starts a
proxy stand-in, hangs it up the way ssh does, reports a failure and exits 255.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.instances import diagnostics
from kiro_crew.instances import token_mint as tm
from kiro_crew.instances.ssh_tunnel_manager import _SshTunnel
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups")

#: Bound on how long a proxy stand-in may outlive a test that failed to end it.
_STRAY_CAP_SECS = 120

# The proxy stand-in ignores SIGHUP, holds none of ssh's pipes, records
# "<pid> <pgid>" once it is up, and stays until the pytest worker is gone.
_PROXY = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
    "path = sys.argv[1]\n"
    "with open(path + '.tmp', 'w') as f:\n"
    "    f.write(f'{os.getpid()} {os.getpgid(0)}')\n"
    "os.replace(path + '.tmp', path)\n"
    f"owner, deadline = {os.getpid()}, time.monotonic() + {_STRAY_CAP_SECS}\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        os.kill(owner, 0)\n"
    "    except ProcessLookupError:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
)

# The ssh stand-in: start the proxy with no pipes of ours, wait for it to be up,
# send it SIGHUP as ssh does on exit, then fail the way a refused proxy makes ssh fail.
_SSH = (
    "import os, signal, subprocess, sys, time\n"
    "path = sys.argv[2]\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[1], path],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "deadline = time.monotonic() + 30\n"
    "while not os.path.exists(path) and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "os.kill(p.pid, signal.SIGHUP)\n"
    "print('proxy: sign-in expired', file=sys.stderr, flush=True)\n"
    "sys.exit(255)\n"
)


# A child that waits to be killed, and exits once the pytest worker is gone.
_IDLE = (
    "import os, time\n"
    f"owner, deadline = {os.getpid()}, time.monotonic() + {_STRAY_CAP_SECS}\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        os.kill(owner, 0)\n"
    "    except ProcessLookupError:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
)


# An ssh stand-in for a tunnel that comes up and later drops: start the proxy,
# wait for it, stay up until the test writes the drop file, then hang the proxy
# up as ssh does and exit 255.
_SSH_DROPS = (
    "import os, signal, subprocess, sys, time\n"
    "path, drop = sys.argv[2], sys.argv[3]\n"
    "p = subprocess.Popen([sys.executable, '-c', sys.argv[1], path],\n"
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    f"deadline = time.monotonic() + {_STRAY_CAP_SECS}\n"
    "while not os.path.exists(drop) and time.monotonic() < deadline:\n"
    "    time.sleep(0.02)\n"
    "os.kill(p.pid, signal.SIGHUP)\n"
    "print('Connection closed by remote host', file=sys.stderr, flush=True)\n"
    "sys.exit(255)\n"
)


def _argv(pid_file: Path) -> list[str]:
    return [sys.executable, "-c", _SSH, _PROXY, str(pid_file)]


def _proxy_identity(pid_file: Path) -> tuple[int, int]:
    pid, pgid = pid_file.read_text().split()
    return int(pid), int(pgid)


def _proxy_gone(pid: int, pgid: int) -> bool:
    """Whether the proxy has ended: its pid is gone, a zombie, or another group's."""
    return platform_compat.pgroup_of(pid) != pgid or bool(platform_compat.pid_is_zombie(pid))


def _kill_stray(pid_file: Path) -> None:
    """Kill a proxy a failed fix left, only while its pid still names that proxy."""
    if not pid_file.exists():
        return
    pid, pgid = _proxy_identity(pid_file)
    if platform_compat.pgroup_of(pid) == pgid:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


async def _mint(argv: list[str]) -> None:
    with pytest.raises(tm.TokenMintError, match="exited 255"):
        await tm.mint_remote_token("crew-host", timeout_secs=FALLBACK_TIMEOUT_SECS)


async def _remote_run(argv: list[str]) -> None:
    rc, _ = await tm.run_remote_kirocrew("crew-host", "restart", timeout_secs=FALLBACK_TIMEOUT_SECS)
    assert rc == 255


async def _probe_ok(argv: list[str]) -> None:
    assert await diagnostics._run_ok(["ssh", "crew-host", "true"], FALLBACK_TIMEOUT_SECS) is False


async def _probe_stdout(argv: list[str]) -> None:
    assert (
        await diagnostics._run_stdout(["ssh", "crew-host", "true"], FALLBACK_TIMEOUT_SECS) is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "run"),
    [
        (tm, _mint),
        (tm, _remote_run),
        (diagnostics, _probe_ok),
        (diagnostics, _probe_stdout),
    ],
    ids=["mint", "remote-run", "probe-ok", "probe-stdout"],
)
async def test_a_failed_one_shot_ssh_leaves_no_proxy_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: Any, run: Any
) -> None:
    pid_file = tmp_path / "proxy.pid"
    argv = _argv(pid_file)
    monkeypatch.setattr(module, "ssh_spawn_argv_env", lambda _argv: (argv, dict(os.environ)))
    try:
        await run(argv)
        pid, pgid = _proxy_identity(pid_file)
        assert pgid != os.getpgid(0), "the ssh must not run in the gateway's own group"
        await async_wait_until(lambda: _proxy_gone(pid, pgid), off_loop=True)
    finally:
        _kill_stray(pid_file)


@pytest.mark.asyncio
async def test_a_failed_one_shot_ssh_with_inherited_errexit_leaves_no_proxy_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.path.basename(os.path.realpath("/bin/sh")) != "bash":
        pytest.skip("/bin/sh is not bash; inherited SHELLOPTS does not enable errexit")
    pid_file = tmp_path / "proxy.pid"
    argv = _argv(pid_file)
    env = dict(os.environ, SHELLOPTS="errexit")
    monkeypatch.setattr(tm, "ssh_spawn_argv_env", lambda _argv: (argv, env))
    try:
        await _remote_run(argv)
        pid, pgid = _proxy_identity(pid_file)
        assert pgid != os.getpgid(0), "the ssh must not run in the gateway's own group"
        await async_wait_until(lambda: _proxy_gone(pid, pgid), off_loop=True)
    finally:
        _kill_stray(pid_file)


@pytest.mark.asyncio
async def test_a_tunnel_whose_ssh_exits_on_its_own_leaves_no_proxy_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pipes at EOF read as settled; a group member still alive is ended anyway."""
    pid_file = tmp_path / "proxy.pid"
    argv = _argv(pid_file)
    tunnel = _SshTunnel("cd-1", "crew-host", 53997, 7777, transport="ssh")
    monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)

    async def _never_up() -> bool:
        return False

    monkeypatch.setattr(tunnel, "_port_reachable", _never_up)
    monkeypatch.chdir(tmp_path)
    try:
        assert await tunnel.start() is False
        pid, pgid = _proxy_identity(pid_file)
        assert tunnel._child_pgid == pgid
        await async_wait_until(lambda: _proxy_gone(pid, pgid), off_loop=True)
    finally:
        _kill_stray(pid_file)
        await tunnel.stop()


@pytest.mark.asyncio
async def test_a_connected_tunnel_whose_ssh_drops_on_its_own_leaves_no_proxy_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery case: the tunnel was up, then ssh dropped by itself."""
    pid_file = tmp_path / "proxy.pid"
    drop_file = tmp_path / "drop"
    argv = [sys.executable, "-c", _SSH_DROPS, _PROXY, str(pid_file), str(drop_file)]
    tunnel = _SshTunnel("cd-1", "crew-host", 53996, 7777, transport="ssh")
    exited: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def _on_exit(instance_id: str) -> None:
        if not exited.done():
            exited.set_result(instance_id)

    tunnel._on_exit = _on_exit
    monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)

    async def _up_once_proxy_runs() -> bool:
        return pid_file.exists()

    monkeypatch.setattr(tunnel, "_port_reachable", _up_once_proxy_runs)
    monkeypatch.chdir(tmp_path)
    try:
        assert await tunnel.start() is True
        pid, pgid = _proxy_identity(pid_file)
        drop_file.touch()  # the network drops: ssh exits 255 on its own
        assert await await_until(exited, what="on_exit fires") == "cd-1"
        await async_wait_until(lambda: _proxy_gone(pid, pgid), off_loop=True)
    finally:
        _kill_stray(pid_file)
        await tunnel.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [0, 42, 127, 255])
async def test_the_leader_exits_with_ssh_s_own_status(status: int) -> None:
    """255 (ssh's failure) and 127 (remote command not found) are classified on."""
    proc = await asyncio.create_subprocess_exec(
        *tm.under_group_leader([sys.executable, "-c", f"raise SystemExit({status})"]),
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert await await_until(asyncio.ensure_future(proc.wait()), what="leader exits") == status
    finally:
        if proc.returncode is None:
            await platform_compat.kill_and_reap(proc)


@pytest.mark.asyncio
async def test_the_leader_passes_a_term_on_to_ssh_and_returns_its_status(
    tmp_path: Path,
) -> None:
    """A SIGTERM sent to the leader alone (the reclaim path) still ends ssh."""
    ready = tmp_path / "ready"
    child = f"open({str(ready)!r}, 'w').close()\n" + _IDLE
    proc = await asyncio.create_subprocess_exec(
        *tm.under_group_leader([sys.executable, "-c", child]),
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await async_wait_until(ready.exists, off_loop=True)
        os.kill(proc.pid, signal.SIGTERM)
        status = await await_until(asyncio.ensure_future(proc.wait()), what="leader exits")
        assert status == 128 + signal.SIGTERM
        assert not platform_compat.pgroup_exists(proc.pid)
    finally:
        if proc.returncode is None:
            await platform_compat.kill_and_reap(proc)


#: An ssh head :func:`~kiro_crew.instances.token_mint.resolve_ssh_bin` found
#: nowhere: bare, and on no PATH.
_MISSING_SSH = "ssh-does-not-exist-xyz"


def test_the_leader_wraps_only_an_absolute_executable_head() -> None:
    assert tm.under_group_leader(["relative", "x"]) == ["relative", "x"]
    wrapped = tm.under_group_leader([sys.executable, "x"])
    assert wrapped[0] == tm._LEADER_SHELL
    assert wrapped[-2:] == [sys.executable, "x"]


@pytest.mark.asyncio
async def test_a_missing_ssh_client_fails_the_mint_as_a_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not the leader shell's exit 127, which a remote "not found" shares."""
    argv = [_MISSING_SSH, "crew-host", "true"]
    monkeypatch.setattr(tm, "ssh_spawn_argv_env", lambda _argv: (argv, dict(os.environ)))
    with pytest.raises(tm.TokenMintError) as excinfo:
        await tm.mint_remote_token("crew-host", timeout_secs=FALLBACK_TIMEOUT_SECS)
    assert str(excinfo.value).startswith("failed to spawn ssh")


@pytest.mark.asyncio
async def test_a_missing_ssh_client_raises_from_the_diagnosis_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    argv = [_MISSING_SSH, "crew-host", "true"]
    monkeypatch.setattr(diagnostics, "ssh_spawn_argv_env", lambda _argv: (argv, dict(os.environ)))
    with pytest.raises(OSError):
        await diagnostics._run_ok(["ssh", "crew-host", "true"], FALLBACK_TIMEOUT_SECS)


def test_one_shot_ssh_never_starts_a_shared_connection() -> None:
    """A shared connection it started would keep its proxy in the group ended at exit."""
    argv = tm._build_ssh_argv("crew-host", "true")
    assert "ControlMaster=no" in argv  # wokeignore:rule=master
