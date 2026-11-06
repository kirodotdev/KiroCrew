"""An SSH teardown reaches the processes a ``ProxyCommand`` left holding the pipes.

When the host's ssh config names a ``ProxyCommand`` or ``ProxyJump``, ssh starts
processes of its own, and they inherit its stderr pipe. On SIGTERM ssh exits, but a
proxy process that was not signalled keeps that pipe open. asyncio resolves
``proc.wait()`` only once every pipe has closed, so the tunnel's monitor never
reports the exit, self-heal never starts, and the tunnel sits CONNECTED with
nothing listening (#31610). These tests spawn a real POSIX tree of that shape: an
ssh stand-in that exits on SIGTERM, and a proxy stand-in that holds the inherited
stderr until it is signalled itself.
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
from kiro_crew.instances import ssh_tunnel_manager as stm
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel
from kiro_crew.testing.wait import await_until

#: Bound on how long a stand-in process may outlive a test that failed to reap it.
#: Kept well above the ``await_until`` ceiling: a stand-in that ended on its own
#: before the test gave up would close the pipe and let a broken teardown pass.
_STRAY_CAP_SECS = 300

# The proxy stand-in keeps the default SIGTERM action, as a real proxy does: it
# survives only because nothing signals it. It prints its pid to the inherited
# stderr, which is both the readiness signal and how cleanup finds it. It also
# exits once the pytest worker is gone, and after _STRAY_CAP_SECS regardless.
_PROXY = (
    "import os, sys, time\n"
    "print(os.getpid(), file=sys.stderr, flush=True)\n"
    f"owner, deadline = {os.getpid()}, time.monotonic() + {_STRAY_CAP_SECS}\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        os.kill(owner, 0)\n"
    "    except ProcessLookupError:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
)

# The ssh stand-in starts the proxy with its own stderr, then waits to be killed.
_SSH = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdin=subprocess.DEVNULL)\n"
    f"time.sleep({_STRAY_CAP_SECS})\n"
)


def _ssh_tunnel(**kwargs: Any) -> _SshTunnel:
    return _SshTunnel("cd-1", "crew-host", 53998, 7777, transport="ssh", **kwargs)


def _kill_strays(tunnel: _SshTunnel, pgid: int | None) -> None:
    """Kill what a failed teardown left, and only what this test still owns.

    A teardown that worked has already reaped both ids, and a reaped id can be
    handed to an unrelated process, so each signal is sent only while the id
    still names a member of this test's tree: the spawned group, or this
    process's own group for a proxy that never left it.
    """
    owned = {os.getpgid(0)}
    if pgid is not None and platform_compat.pgroup_exists(pgid):
        owned.add(pgid)
        with contextlib.suppress(Exception):
            platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)
    head = tunnel._stderr_buf.split()[:1]
    if head and head[0].isdigit():
        proxy = int(head[0])
        if platform_compat.pgroup_of(proxy) in owned:
            with contextlib.suppress(OSError):
                os.kill(proxy, signal.SIGKILL)


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups")
@pytest.mark.asyncio
async def test_a_probe_forced_ssh_teardown_ends_in_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure end to end: started tunnel, failed probe, recovery."""
    monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.05)
    tunnel = _ssh_tunnel(probe_failure_threshold=1)
    exited: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def _on_exit(instance_id: str) -> None:
        if not exited.done():
            exited.set_result(instance_id)

    tunnel._on_exit = _on_exit
    argv = [sys.executable, "-c", _SSH, _PROXY]
    monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)

    async def _proxy_up() -> bool:
        # Stands in for ssh's bound port: up once the proxy printed its pid.
        return bool(tunnel._stderr_buf.strip())

    async def _dead_forward() -> bool:
        return False  # the network dropped, so nothing answers through it

    monkeypatch.setattr(tunnel, "_port_reachable", _proxy_up)
    monkeypatch.setattr(tunnel, "_forward_alive", _dead_forward)
    monkeypatch.chdir(tmp_path)
    pgid: int | None = None
    try:
        assert await tunnel.start()
        spawned = tunnel._proc
        assert spawned is not None
        pgid = spawned.pid
        assert await await_until(exited, what="on_exit fires") == "cd-1"
        assert tunnel.status.state == TunnelState.ERROR
        assert "health probe failed" in tunnel.status.error
        assert tunnel._child_pgid == pgid
    finally:
        _kill_strays(tunnel, pgid)
        await tunnel.stop()


@pytest.mark.asyncio
async def test_an_ssh_child_that_does_not_lead_its_group_gets_no_group_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a recorded group, a group signal would reach the gateway's own."""
    grouped: list[int] = []

    async def _record(pid: int, sig: int) -> bool:
        grouped.append(pid)
        return True

    monkeypatch.setattr(_SshTunnel, "_signal_group_async", staticmethod(_record))
    monkeypatch.setattr(
        platform_compat, "kill_process_group", lambda pgid, sig: grouped.append(pgid)
    )

    class _Running:
        returncode: int | None = None
        pid = 4242
        terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        async def wait(self) -> int:
            return -15

    proc = _Running()
    tunnel = _ssh_tunnel()
    tunnel._proc = proc  # type: ignore[assignment]
    assert tunnel._child_pgid is None
    await tunnel._terminate()
    assert grouped == []
    assert proc.terminated
    assert tunnel._proc is None
