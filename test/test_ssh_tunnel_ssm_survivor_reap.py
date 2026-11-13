"""An SSM teardown reaches a ``session-manager-plugin`` that outlived its wrapper.

The SSM child is the ``aws`` wrapper; the plugin it starts holds the forwarded port
and inherits the child's stdout/stderr pipes. When the plugin survives the wrapper
(it does not finish on SIGTERM while closing a session to a target that is gone),
``proc.wait()`` cannot return -- asyncio waits for every pipe to close -- so the
tunnel's monitor never reports the exit and self-heal never starts. These tests
spawn a real POSIX tree of that shape: a wrapper that leads its own group and a
grandchild that ignores SIGTERM and holds the pipes.

The probe-forced teardown's own reporting (a raised teardown, a forward still up
afterwards) is covered with a stubbed ``_terminate``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.instances import ssh_tunnel_manager as stm
from kiro_crew.instances import ssh_tunnel_manager_child as child_mod
from kiro_crew.instances.ssh_tunnel_manager import TunnelState, _SshTunnel
from kiro_crew.testing.wait import async_wait_until, await_until

#: Bound on how long a stand-in process may outlive a test that failed to reap it.
#: Kept well above the ``await_until`` ceiling: a stand-in that ended on its own
#: before the test gave up would close its pipes and let a broken teardown pass.
_STRAY_CAP_SECS = 300

# The stand-in plugin ignores SIGTERM, so it must not depend on a test's cleanup to
# end: it also exits once the process that imported this module (the pytest
# worker) is gone, and after _STRAY_CAP_SECS regardless. That bounds a stray tree
# on every path, including a cancellation that lands before a test owns the tree.
_GRANDCHILD = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print(os.getpid(), flush=True)\n"
    f"owner, deadline = {os.getpid()}, time.monotonic() + {_STRAY_CAP_SECS}\n"
    "while time.monotonic() < deadline:\n"
    "    try:\n"
    "        os.kill(owner, 0)\n"
    "    except ProcessLookupError:\n"
    "        break\n"
    "    time.sleep(0.2)\n"
)

_WRAPPER = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdin=subprocess.DEVNULL)\n"
    "if sys.argv[2] == 'exit':\n"
    "    sys.stdin.readline()\n"
    "    sys.exit(0)\n"
    f"time.sleep({_STRAY_CAP_SECS})\n"
)


def _ssm_tunnel(**kwargs: Any) -> _SshTunnel:
    return _SshTunnel(
        "cd-1", "", 53997, 7777, transport="ssm", ssm_target="i-0123456789abcdef0", **kwargs
    )


async def _spawn_tree(cwd: Path, mode: str) -> asyncio.subprocess.Process:
    """Spawn the wrapper the way ``_SshTunnel.start`` spawns an SSM child."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _WRAPPER,
        _GRANDCHILD,
        mode,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        cwd=str(cwd),
    )
    assert proc.stdout is not None
    try:
        await asyncio.wait_for(proc.stdout.readline(), timeout=30)  # the plugin is up
    except BaseException:
        # The callers' cleanup has not started yet: reap the tree here, then re-raise.
        _kill_group(proc.pid)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=30)
        raise
    return proc


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(Exception):
        platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)


@pytest.fixture
def fast_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(child_mod, "_TERMINATE_WAIT_SECS", 1.0)
    monkeypatch.setattr(child_mod, "_EXITED_SETTLE_SECS", 0.5)


async def _connected_tunnel(proc: asyncio.subprocess.Process, exits: list[str]) -> _SshTunnel:
    """A CONNECTED tunnel supervising *proc*, its monitor already waiting."""
    tunnel = _ssm_tunnel(on_exit=exits.append)
    tunnel._proc = proc
    tunnel._ssm_pgid = _SshTunnel._spawned_group(proc)
    assert tunnel._ssm_pgid == proc.pid
    tunnel.status.state = TunnelState.CONNECTED
    tunnel._probe_failed = True
    tunnel._monitor_task = asyncio.create_task(tunnel._monitor())
    await asyncio.sleep(0)  # the monitor enters wait() while the wrapper runs
    return tunnel


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX process groups")
class TestSurvivingPluginIsReaped:
    @pytest.mark.asyncio
    async def test_a_plugin_that_ignores_sigterm_is_killed_after_the_wrapper_dies(
        self, tmp_path: Path, fast_waits: None
    ) -> None:
        proc = await _spawn_tree(tmp_path, "wait")
        exits: list[str] = []
        try:
            tunnel = await _connected_tunnel(proc, exits)
            monitor = tunnel._monitor_task
            assert monitor is not None
            await tunnel._terminate()
            await await_until(monitor, what="the monitor reports the exit")
            assert exits == ["cd-1"]
            assert tunnel.status.state == TunnelState.ERROR
        finally:
            _kill_group(proc.pid)

    @pytest.mark.asyncio
    async def test_a_plugin_left_behind_by_an_exited_wrapper_is_killed(
        self, tmp_path: Path, fast_waits: None
    ) -> None:
        proc = await _spawn_tree(tmp_path, "exit")
        exits: list[str] = []
        try:
            tunnel = await _connected_tunnel(proc, exits)
            monitor = tunnel._monitor_task
            assert monitor is not None
            assert proc.stdin is not None
            proc.stdin.write(b"go\n")  # the wrapper exits; the plugin stays
            await async_wait_until(lambda: proc.returncode is not None)
            assert not monitor.done()  # the plugin holds the pipes
            await tunnel._terminate()
            await await_until(monitor, what="the monitor reports the exit")
            assert exits == ["cd-1"]
        finally:
            _kill_group(proc.pid)

    @pytest.mark.asyncio
    async def test_a_probe_forced_teardown_ends_in_on_exit(
        self, tmp_path: Path, fast_waits: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported failure end to end: started tunnel, failed probe, recovery."""
        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.05)
        tunnel = _ssm_tunnel(on_exit=None, probe_failure_threshold=1)
        exited: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def _on_exit(instance_id: str) -> None:
            if not exited.done():
                exited.set_result(instance_id)

        tunnel._on_exit = _on_exit
        argv = [sys.executable, "-c", _WRAPPER, _GRANDCHILD, "wait"]
        monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)

        async def _plugin_listening() -> bool:
            # Stands in for the plugin's bound port: up only once it printed its pid.
            return bool(tunnel._stdout_buf.strip())

        async def _zombie() -> bool:
            return False  # the forward relays nothing

        monkeypatch.setattr(tunnel, "_port_reachable", _plugin_listening)
        monkeypatch.setattr(tunnel, "_forward_alive", _zombie)
        monkeypatch.chdir(tmp_path)
        pgid: int | None = None
        try:
            assert await tunnel.start()
            spawned = tunnel._proc
            assert spawned is not None
            pgid = spawned.pid
            assert tunnel._ssm_pgid == pgid
            assert await await_until(exited, what="on_exit fires") == "cd-1"
            assert "health probe failed" in tunnel.status.error
        finally:
            if pgid is not None:
                _kill_group(pgid)
            await tunnel.stop()

    @pytest.mark.asyncio
    async def test_a_child_whose_pipes_have_closed_signals_no_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exited child with no pipe left open needs no group signal."""
        signalled: list[int] = []
        monkeypatch.setattr(
            platform_compat, "kill_process_group", lambda pgid, sig: signalled.append(pgid)
        )

        class _Exited:
            returncode = 0
            pid = 4242

            async def wait(self) -> int:
                return 0

        tunnel = _ssm_tunnel()
        tunnel._proc = _Exited()  # type: ignore[assignment]
        tunnel._ssm_pgid = 4242
        await tunnel._terminate()
        assert signalled == []
        assert tunnel._proc is None


class TestProbeTeardownReporting:
    @pytest.mark.asyncio
    async def test_the_probe_loop_reports_a_teardown_that_did_not_take_effect(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)
        monkeypatch.setattr(child_mod, "_PROBE_TEARDOWN_CONFIRM_SECS", 0.05)
        tunnel = _ssm_tunnel(probe_failure_threshold=1)
        tunnel.status.state = TunnelState.CONNECTED

        async def _zombie() -> bool:
            return False

        async def _no_effect() -> None:
            return None

        tunnel._forward_alive = _zombie  # type: ignore[method-assign]
        tunnel._terminate = _no_effect  # type: ignore[method-assign]
        monitor = asyncio.create_task(asyncio.Event().wait())  # never reports an exit
        tunnel._monitor_task = monitor
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
                await tunnel._probe_loop()
                await async_wait_until(
                    lambda: any("is still up" in r.getMessage() for r in caplog.records),
                    timeout=10,
                )
        finally:
            monitor.cancel()

    @pytest.mark.asyncio
    async def test_a_forward_still_up_after_teardown_is_logged(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(child_mod, "_PROBE_TEARDOWN_CONFIRM_SECS", 0.05)
        tunnel = _ssm_tunnel()
        tunnel.status.state = TunnelState.CONNECTED

        async def _noop() -> None:
            return None

        tunnel._terminate = _noop  # type: ignore[method-assign]
        monitor = asyncio.create_task(asyncio.Event().wait())  # never reports an exit
        tunnel._monitor_task = monitor
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
                await tunnel._probe_teardown()
        finally:
            monitor.cancel()
        assert any("is still up" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_teardown_that_raises_is_logged_with_its_cause(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        tunnel = _ssm_tunnel()

        async def _boom() -> None:
            raise PermissionError("not permitted")

        tunnel._terminate = _boom  # type: ignore[method-assign]
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)
        tunnel._monitor_task = asyncio.ensure_future(done)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
            await tunnel._probe_teardown()
        messages = [r.getMessage() for r in caplog.records]
        assert any("PermissionError: not permitted" in m for m in messages)
        assert not any("is still up" in m for m in messages)

    @pytest.mark.asyncio
    async def test_a_monitor_that_reports_the_exit_logs_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        tunnel = _ssm_tunnel()

        async def _noop() -> None:
            return None

        tunnel._terminate = _noop  # type: ignore[method-assign]
        tunnel._monitor_task = asyncio.ensure_future(asyncio.sleep(0))
        with caplog.at_level(logging.WARNING, logger="kiro_crew.instances.ssh_tunnel_manager"):
            await tunnel._probe_teardown()
        assert caplog.records == []


@pytest.mark.asyncio
async def test_a_tunnel_that_never_spawned_has_no_group_to_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signalled: list[int] = []
    monkeypatch.setattr(
        platform_compat, "kill_process_group", lambda pgid, sig: signalled.append(pgid)
    )

    class _Exited:
        returncode = 0
        pid = 4242

    tunnel = _ssm_tunnel()
    tunnel._proc = _Exited()  # type: ignore[assignment]
    await tunnel._terminate()
    assert signalled == []
    assert tunnel._proc is None


def test_spawned_group_refuses_a_child_that_does_not_lead_its_group() -> None:
    class _Proc:
        returncode = None
        pid: Any = os.getpid()

    if platform_compat.IS_POSIX and os.getpgid(0) == os.getpid():
        pytest.skip("this test process happens to lead its own group")
    assert _SshTunnel._spawned_group(_Proc()) is None  # type: ignore[arg-type]
