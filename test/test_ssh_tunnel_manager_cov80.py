"""``instances.ssh_tunnel_manager`` — the ``_SshTunnel`` teardown / exit paths.

``test_instances.py`` covers the state machine's happy path, the probe loop and
the ssh error classifier. What it leaves unobserved is everything that runs when
a tunnel goes DOWN, plus the whole SSM error vocabulary:

* ``_port_reachable`` — the one-second loopback probe the readiness wait
  (``_wait_until_ready``) is built on, in both directions; the health loop
  instead checks ``_forward_alive`` (an end-to-end request through the forward);
* ``_monitor`` — the unexpected-exit path: it must drain stderr, land ERROR with a
  classified message, and notify the manager's ``on_exit`` seam, while a
  DELIBERATE stop stays silent (no ERROR, no self-heal notification);
* ``_capture_stderr`` — bounded drain, including the no-stderr-pipe case;
* ``_terminate`` — graceful terminate, the kill fallback when the child will not
  wait, and the SSM tree-reap that exists because ``proc.terminate()`` signals
  only the ``aws`` wrapper and leaves ``session-manager-plugin`` holding the port;
* ``_ssm_exit_error`` — classified separately from ssh on purpose: running SSM
  stderr through the ssh matchers would report an ``AccessDenied`` as an "ssh auth
  failure", which sends the operator to the wrong fix.

No real subprocesses, no sockets and no sleeps: the child is a fake whose
``wait()`` resolves (or raises) immediately, and the loopback probe is stubbed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.instances.ssh_tunnel_manager import (
    TunnelState,
    _sanitize_banner,
    _SshTunnel,
    _TransportParams,
)


class _FakeProc:
    """Minimal ``asyncio.subprocess.Process`` stand-in.

    ``wait_raises`` lets a test drive ``_terminate``'s two failure branches
    without a real five-second timeout.
    """

    def __init__(
        self,
        *,
        returncode: int | None = None,
        stderr: Any = None,
        wait_raises: BaseException | None = None,
        pid: int = 4242,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.pid = pid
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False

    async def wait(self) -> int | None:
        if self._wait_raises is not None:
            raise self._wait_raises
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        data, self._data = self._data, b""
        return data


def _tunnel(**kwargs: Any) -> _SshTunnel:
    return _SshTunnel("cd-1", "cd-1-alias", 53997, 7777, **kwargs)


class TestPortReachable:
    @pytest.mark.asyncio
    async def test_a_refused_connect_is_not_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _refused(*_a: Any, **_kw: Any) -> Any:
            raise OSError(111, "Connection refused")

        monkeypatch.setattr(asyncio, "open_connection", _refused)
        assert await _tunnel()._port_reachable() is False

    @pytest.mark.asyncio
    async def test_a_timeout_is_not_reachable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _hang(*_a: Any, **_kw: Any) -> Any:
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "open_connection", _hang)
        assert await _tunnel()._port_reachable() is False

    @pytest.mark.asyncio
    async def test_an_accepted_connect_is_reachable_and_closes_the_writer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[str] = []

        class _Writer:
            def close(self) -> None:
                closed.append("close")

            async def wait_closed(self) -> None:
                closed.append("wait_closed")

        async def _accepted(*_a: Any, **_kw: Any) -> Any:
            return object(), _Writer()

        monkeypatch.setattr(asyncio, "open_connection", _accepted)
        assert await _tunnel()._port_reachable() is True
        assert closed == ["close", "wait_closed"]

    @pytest.mark.asyncio
    async def test_a_writer_that_fails_to_close_is_still_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe answers "does something accept a connect", so a teardown
        wobble must not turn a healthy forward into a probe failure."""

        class _Writer:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                raise RuntimeError("transport already gone")

        async def _accepted(*_a: Any, **_kw: Any) -> Any:
            return object(), _Writer()

        monkeypatch.setattr(asyncio, "open_connection", _accepted)
        assert await _tunnel()._port_reachable() is True


class _FakeHealthSession:
    """An ``aiohttp.ClientSession`` stand-in for ``GET /api/health``.

    ``get_raises`` drives the zombie/dead-forward paths (a bytes-less stall that
    the client times out on surfaces here as an exception, exactly as it would
    in ``_forward_alive``'s except arm); ``status`` drives the answered-response
    paths. Records the requested URL so a test can assert the probe went through
    the local forward.
    """

    def __init__(self, *, status: int = 200, get_raises: BaseException | None = None) -> None:
        self._status = status
        self._get_raises = get_raises
        self.requested_url = ""

    def __call__(self, *_a: Any, **_kw: Any) -> "_FakeHealthSession":
        return self

    async def __aenter__(self) -> "_FakeHealthSession":
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    def get(self, url: str, **_kw: Any) -> Any:
        self.requested_url = url
        if self._get_raises is not None:
            raise self._get_raises
        status = self._status

        class _Resp:
            def __init__(self) -> None:
                self.status = status

            async def __aenter__(self) -> "_Resp":
                return self

            async def __aexit__(self, *_a: Any) -> bool:
                return False

        return _Resp()


class TestForwardAlive:
    """``_forward_alive`` — the steady-state end-to-end liveness probe.

    A bare TCP connect is answered by whatever holds the local listening socket,
    so a zombie SSM forward — ``session-manager-plugin`` alive but relaying
    nothing — passes ``_port_reachable`` forever and the tunnel is reported
    CONNECTED while every request through it stalls. ``_forward_alive`` requires
    a completed ``GET /api/health`` response, which the far end cannot produce
    when the forward is dead.
    """

    @pytest.mark.asyncio
    async def test_a_zombie_forward_that_never_answers_is_not_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gap this probe closes: the connect is accepted, then the far end
        sends zero bytes and the client times out. That stall must read as NOT
        alive — a connect-only probe answers True here and misses the zombie
        entirely."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        fake = _FakeHealthSession(get_raises=asyncio.TimeoutError())
        monkeypatch.setattr(stm.aiohttp, "ClientSession", fake)
        t = _tunnel()
        assert await t._forward_alive() is False
        assert fake.requested_url == "http://127.0.0.1:53997/api/health"

    @pytest.mark.asyncio
    async def test_a_healthy_forward_that_answers_200_is_alive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.instances import ssh_tunnel_manager as stm

        fake = _FakeHealthSession(status=200)
        monkeypatch.setattr(stm.aiohttp, "ClientSession", fake)
        assert await _tunnel()._forward_alive() is True

    @pytest.mark.asyncio
    async def test_a_non_2xx_answer_is_still_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A completed response of ANY status proves the far end sent bytes back,
        so a non-2xx answer still reads as a live forward and is NOT torn down."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        fake = _FakeHealthSession(status=404)
        monkeypatch.setattr(stm.aiohttp, "ClientSession", fake)
        assert await _tunnel()._forward_alive() is True

    @pytest.mark.asyncio
    async def test_a_fargate_forward_is_probed_at_the_container_liveness_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fargate tunnel (its ``turn_url`` is set) must probe the container's
        own ``/health``, not the gateway's ``/api/health`` — the container
        authorises before routing and would log a control deny for every probe
        aimed at a path it does not serve."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        fake = _FakeHealthSession(status=200)
        monkeypatch.setattr(stm.aiohttp, "ClientSession", fake)
        t = _tunnel()
        t.status.turn_url = "http://127.0.0.1:53997/v1/chat/completions"  # marks fargate
        assert await t._forward_alive() is True
        assert fake.requested_url == f"http://127.0.0.1:53997{stm.FARGATE_HEALTH_PATH}"

    @pytest.mark.asyncio
    async def test_a_connection_error_is_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.instances import ssh_tunnel_manager as stm

        fake = _FakeHealthSession(get_raises=OSError(111, "Connection refused"))
        monkeypatch.setattr(stm.aiohttp, "ClientSession", fake)
        assert await _tunnel()._forward_alive() is False

    @pytest.mark.asyncio
    async def test_an_unallocated_port_is_not_alive(self) -> None:
        """No forward end to probe — a zero port is refused before any request."""
        t = _SshTunnel("cd-1", "cd-1-alias", 0, 7777)
        assert await t._forward_alive() is False

    @pytest.mark.asyncio
    async def test_the_probe_loop_checks_the_forward_end_to_end_not_just_the_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wiring guard: the health loop must consult ``_forward_alive``, not
        ``_port_reachable``. A zombie whose local socket is bound
        (``_port_reachable`` True) but whose far end is dead (``_forward_alive``
        False) has to tear the tunnel down."""
        from kiro_crew.instances import ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_PROBE_INTERVAL", 0.01)
        called = {"port_reachable": 0, "forward_alive": 0}

        t = _tunnel(probe_failure_threshold=2)
        t._proc = _FakeProc(returncode=None)
        t.status.state = TunnelState.CONNECTED

        async def _socket_bound() -> bool:
            called["port_reachable"] += 1
            return True  # a zombie: the listener is still bound

        async def _far_end_dead() -> bool:
            called["forward_alive"] += 1
            return False  # but nothing traverses to the remote gateway

        t._port_reachable = _socket_bound  # type: ignore[assignment]
        t._forward_alive = _far_end_dead  # type: ignore[assignment]
        await asyncio.wait_for(t._probe_loop(), timeout=2)
        await asyncio.sleep(0.05)

        assert called["forward_alive"] >= 2  # the loop consulted the end-to-end check
        assert called["port_reachable"] == 0  # never the connect-only check
        assert t._probe_failed is True
        assert "health probe failed" in t._exit_error(-15)


class TestCaptureStderr:
    @pytest.mark.asyncio
    async def test_no_child_is_a_no_op(self) -> None:
        tunnel = _tunnel()
        await tunnel._capture_stderr()
        assert tunnel._stderr_buf == ""

    @pytest.mark.asyncio
    async def test_a_child_without_a_stderr_pipe_is_a_no_op(self) -> None:
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(stderr=None)  # type: ignore[assignment]
        await tunnel._capture_stderr()
        assert tunnel._stderr_buf == ""

    @pytest.mark.asyncio
    async def test_stderr_is_drained_and_decoded(self) -> None:
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(  # type: ignore[assignment]
            stderr=_FakeStderr(b"bind [127.0.0.1]:53997: Address already in use\n")
        )
        await tunnel._capture_stderr()
        assert "already in use" in tunnel._stderr_buf

    @pytest.mark.asyncio
    async def test_the_buffer_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kiro_crew.instances import ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_MAX_STDERR_CHARS", 16)
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(stderr=_FakeStderr(b"z" * 200))  # type: ignore[assignment]
        await tunnel._capture_stderr()
        assert len(tunnel._stderr_buf) == 16


# A stand-in tunnel child: it writes ``size`` bytes of the line ssh prints for
# every forwarded connection the far end refuses, then records that it got past
# the writes. The test, not the child, holds the forward's listening socket.
_CHATTY_CHILD = r"""
import os, sys, time
size, marker = int(sys.argv[1]), sys.argv[2]
line = b"channel 3: open failed: connect failed: Connection refused\r\n"
written = 0
while written < size:
    os.write(2, line)
    written += len(line)
with open(marker, "w") as f:
    f.write(str(written))
time.sleep(60)
"""

# A child that fails the way ExitOnForwardFailure does: stderr, then exit 255.
_BIND_FAILURE_CHILD = r"""
import sys
sys.stderr.write("bind [127.0.0.1]:53997: Address already in use\r\n")
sys.stderr.write("Could not request local forwarding.\r\n")
sys.exit(255)
"""


def _held_port(*, listening: bool) -> Any:
    """A 127.0.0.1 socket on a port the OS assigned, held open by the test.

    The tunnel takes a port number, so the port is picked by binding port 0 and
    kept bound until the test ends: it is never released between the pick and
    the use, and no other process can take it. A listening socket answers the
    readiness wait's connect from its backlog, with no accept needed; a socket
    that is bound but not listening refuses it.
    """
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    if listening:
        sock.listen(16)
    return sock


def _pending_tasks() -> list[asyncio.Task]:  # type: ignore[type-arg]
    current = asyncio.current_task()
    return [t for t in asyncio.all_tasks() if t is not current and not t.done()]


class TestStderrIsDrainedWhileRunning:
    """``start()`` reads the child's stderr while it runs, through the real spawn."""

    async def _run_chatty_child(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, size: int
    ) -> tuple[bool, bool, list[Any]]:
        import sys
        import time

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        forward = _held_port(listening=True)
        marker = tmp_path / "past-the-writes"
        tunnel = _SshTunnel(
            "cd-1", "cd-1-alias", forward.getsockname()[1], 7777, connect_timeout_secs=10
        )
        argv = [sys.executable, "-c", _CHATTY_CHILD, str(size), str(marker)]
        monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)
        proc = None
        try:
            started = await tunnel.start()
            proc = tunnel._proc
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not marker.exists():
                await asyncio.sleep(0.05)
            past_the_writes = marker.exists()
            await tunnel.stop()
            assert proc is not None and proc.returncode is not None, "stop() ended the child"
            return started, past_the_writes, _pending_tasks()
        finally:
            forward.close()
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()

    @pytest.mark.asyncio
    async def test_a_child_that_writes_1mb_to_stderr_keeps_running(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, past_the_writes, pending = await self._run_chatty_child(
            tmp_path, monkeypatch, 1024 * 1024
        )
        assert started
        assert past_the_writes, "the child blocked writing stderr while the tunnel ran"
        assert pending == [], "a drain task outlived stop()"

    @pytest.mark.asyncio
    async def test_a_child_that_writes_64kb_to_stderr_keeps_running(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        started, past_the_writes, pending = await self._run_chatty_child(
            tmp_path, monkeypatch, 64 * 1024
        )
        assert started and past_the_writes
        assert pending == []

    @pytest.mark.asyncio
    async def test_a_failed_start_still_reports_its_stderr(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        # Held bound but not listening, so the readiness wait's connect is refused
        # and no other process can start listening on this port mid-test.
        held = _held_port(listening=False)
        tunnel = _SshTunnel(
            "cd-1", "cd-1-alias", held.getsockname()[1], 7777, connect_timeout_secs=10
        )
        argv = [sys.executable, "-c", _BIND_FAILURE_CHILD]
        monkeypatch.setattr(tunnel, "_build_argv", lambda: argv)

        try:
            assert await tunnel.start() is False
        finally:
            held.close()
        assert tunnel.status.state is TunnelState.ERROR
        assert tunnel.status.error.startswith(
            "ssh forward bind failed (local port already in use)"
        ), tunnel.status.error
        assert "Address already in use" in tunnel.status.error
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_the_exit_wait_for_the_drain_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A grandchild that inherited the pipe can hold it open after the child
        # exits, so the drain never sees EOF. The exit path stops waiting at the
        # bound and keeps what the drain captured.
        from kiro_crew.instances import ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_STDERR_DRAIN_GRACE_SECS", 0.05)
        stream = asyncio.StreamReader()
        stream.feed_data(b"Connection closed by remote host\r\n")  # and no EOF, ever
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(returncode=255, stderr=stream)  # type: ignore[assignment]
        tunnel._stderr_task = asyncio.create_task(tunnel._drain_stderr())
        # Wait until the drain has read what is buffered and is blocked on the
        # missing EOF, on the condition itself and under a monotonic bound.
        import time

        deadline = time.monotonic() + 5.0
        while "Connection closed by remote host" not in tunnel._stderr_buf:
            assert time.monotonic() < deadline, "the drain never read the buffered line"
            await asyncio.sleep(0.01)
        assert not tunnel._stderr_task.done(), "the drain must still be waiting for EOF"

        await asyncio.wait_for(tunnel._capture_stderr(), timeout=5)

        assert tunnel._stderr_task is None
        assert "Connection closed by remote host" in tunnel._stderr_buf
        assert _pending_tasks() == []

    @pytest.mark.asyncio
    async def test_a_stop_during_the_exit_wait_is_not_an_unexpected_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The child exits on its own and the monitor waits for the stderr drain,
        # which never sees EOF. A deliberate stop() in that window must end
        # STOPPED without calling on_exit, the self-heal seam: otherwise the stop
        # itself schedules a replacement tunnel.
        from kiro_crew.instances import ssh_tunnel_manager as stm

        monkeypatch.setattr(stm, "_STDERR_DRAIN_GRACE_SECS", 5.0)
        exits: list[str] = []
        stream = asyncio.StreamReader()
        stream.feed_data(b"Connection closed by remote host\r\n")  # and no EOF, ever
        tunnel = _tunnel(on_exit=exits.append)
        tunnel._proc = _FakeProc(returncode=255, stderr=stream)  # type: ignore[assignment]
        tunnel._stderr_task = asyncio.create_task(tunnel._drain_stderr())
        tunnel.status.state = TunnelState.CONNECTED
        tunnel._monitor_task = asyncio.create_task(tunnel._monitor())
        # The monitor takes the drain task when it starts waiting for it.
        import time

        deadline = time.monotonic() + 5.0
        while tunnel._stderr_task is not None:
            assert time.monotonic() < deadline, "the monitor never reached its stderr wait"
            await asyncio.sleep(0.01)
        assert not tunnel._monitor_task.done(), "the monitor must still be waiting on stderr"

        await asyncio.wait_for(tunnel.stop(), timeout=10)

        assert exits == [], f"a deliberate stop fired the self-heal seam: {exits}"
        assert tunnel.status.state == TunnelState.STOPPED
        assert _pending_tasks() == []


class TestMonitor:
    @pytest.mark.asyncio
    async def test_no_child_is_a_no_op(self) -> None:
        tunnel = _tunnel()
        await tunnel._monitor()
        assert tunnel.status.state is not TunnelState.ERROR

    @pytest.mark.asyncio
    async def test_a_deliberate_stop_is_not_reported_as_an_error(self) -> None:
        """``stop()`` sets ``_stopping`` before the child exits; treating that exit
        as unexpected would fire self-heal against a tunnel the operator closed."""
        notified: list[str] = []
        tunnel = _tunnel(on_exit=notified.append)
        tunnel._proc = _FakeProc(returncode=0)  # type: ignore[assignment]
        tunnel._stopping = True
        await tunnel._monitor()
        assert tunnel.status.state is not TunnelState.ERROR
        assert notified == []

    @pytest.mark.asyncio
    async def test_an_unexpected_exit_lands_error_and_notifies(self) -> None:
        notified: list[str] = []
        tunnel = _tunnel(on_exit=notified.append)
        tunnel._proc = _FakeProc(  # type: ignore[assignment]
            returncode=255,
            stderr=_FakeStderr(b"host: Permission denied (publickey).\n"),
        )
        await tunnel._monitor()
        assert tunnel.status.state is TunnelState.ERROR
        assert "auth failed" in tunnel.status.error.lower()
        assert notified == ["cd-1"]

    @pytest.mark.asyncio
    async def test_an_exit_callback_that_raises_does_not_escape(self) -> None:
        def _boom(_instance_id: str) -> None:
            raise RuntimeError("manager blew up")

        tunnel = _tunnel(on_exit=_boom)
        tunnel._proc = _FakeProc(returncode=255)  # type: ignore[assignment]
        await tunnel._monitor()
        assert tunnel.status.state is TunnelState.ERROR

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self) -> None:
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(wait_raises=asyncio.CancelledError())  # type: ignore[assignment]
        with pytest.raises(asyncio.CancelledError):
            await tunnel._monitor()


class TestTerminate:
    @pytest.mark.asyncio
    async def test_an_already_exited_child_needs_no_signal(self) -> None:
        proc = _FakeProc(returncode=0)
        tunnel = _tunnel()
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert proc.terminated is False
        assert tunnel._proc is None

    @pytest.mark.asyncio
    async def test_a_live_ssh_child_is_terminated_gracefully(self) -> None:
        proc = _FakeProc()
        tunnel = _tunnel()
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert proc.terminated is True
        assert proc.killed is False

    @pytest.mark.asyncio
    async def test_a_child_that_will_not_wait_is_killed(self) -> None:
        proc = _FakeProc(wait_raises=asyncio.TimeoutError())
        tunnel = _tunnel()
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert proc.killed is True

    @pytest.mark.asyncio
    async def test_a_vanished_child_is_not_an_error(self) -> None:
        proc = _FakeProc(wait_raises=ProcessLookupError())
        tunnel = _tunnel()
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert tunnel._proc is None

    @pytest.mark.asyncio
    async def test_an_ssm_child_is_reaped_as_a_tree_not_signalled_directly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``proc.terminate()`` would signal only the ``aws`` wrapper and leave
        ``session-manager-plugin`` alive holding the forwarded port."""
        signalled: list[tuple[int, int]] = []

        def _tree_kill(pid: int, sig: int) -> bool:
            signalled.append((pid, sig))
            return True

        monkeypatch.setattr(platform_compat, "kill_process_tree", _tree_kill)
        proc = _FakeProc(pid=777)
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert signalled == [(777, platform_compat.SIGTERM)]
        assert proc.terminated is False

    @pytest.mark.asyncio
    async def test_an_ssm_tree_that_survives_sigterm_gets_sigkill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signalled: list[int] = []

        def _tree_kill(_pid: int, sig: int) -> bool:
            signalled.append(sig)
            return True

        monkeypatch.setattr(platform_compat, "kill_process_tree", _tree_kill)
        proc = _FakeProc(pid=778, wait_raises=asyncio.TimeoutError())
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert signalled == [platform_compat.SIGTERM, platform_compat.SIGKILL]
        assert proc.killed is False

    @pytest.mark.asyncio
    async def test_an_ssm_tree_kill_that_fails_falls_back_to_the_single_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(platform_compat, "kill_process_tree", lambda pid, sig: False)
        proc = _FakeProc(pid=779, wait_raises=asyncio.TimeoutError())
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._proc = proc  # type: ignore[assignment]
        await tunnel._terminate()
        assert proc.terminated is True  # no tree signal, so the wrapper was signalled
        assert proc.killed is True


class TestSignalGroup:
    def test_a_delivered_tree_kill_reports_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(platform_compat, "kill_process_tree", lambda pid, sig: True)
        assert _SshTunnel._signal_group(4242, platform_compat.SIGTERM) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ProcessLookupError(),
            PermissionError(),
            OSError("broadcast pgid refused"),
            ValueError("bad pid"),
            AttributeError("no such shim"),
        ],
    )
    def test_every_shim_failure_means_not_delivered(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException
    ) -> None:
        """All of them mean "not delivered", so the caller falls back to the
        single-process kill instead of leaving the tree alive."""

        def _boom(_pid: int, _sig: int) -> bool:
            raise exc

        monkeypatch.setattr(platform_compat, "kill_process_tree", _boom)
        assert _SshTunnel._signal_group(4242, platform_compat.SIGKILL) is False


class TestPid:
    def test_no_child_has_no_pid(self) -> None:
        assert _tunnel().pid is None

    def test_a_live_child_reports_its_pid(self) -> None:
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(pid=31337)  # type: ignore[assignment]
        assert tunnel.pid == 31337

    def test_an_exited_child_reports_no_pid(self) -> None:
        tunnel = _tunnel()
        tunnel._proc = _FakeProc(pid=31337, returncode=0)  # type: ignore[assignment]
        assert tunnel.pid is None


class TestSsmExitError:
    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            (
                "An error occurred (ExpiredTokenException) when calling StartSession",
                "credentials missing or expired",
            ),
            ("Unable to locate credentials", "credentials missing or expired"),
            (
                "An error occurred (AccessDeniedException): not authorized to perform",
                "IAM denied ssm:StartSession",
            ),
            (
                "SessionManagerPlugin is not found. Please refer to install",
                "session-manager-plugin is not installed locally",
            ),
            (
                "An error occurred (TargetNotConnected) when calling StartSession",
                "not a connected managed node",
            ),
            (
                "An error occurred (InvalidInstanceId) when calling StartSession",
                "not a connected managed node",
            ),
            (
                "bind [127.0.0.1]:53997: Address already in use",
                "SSM forward bind failed",
            ),
            ("something else entirely went wrong", "SSM session exited 1"),
        ],
    )
    def test_each_actionable_failure_mode_is_named(self, stderr: str, expected: str) -> None:
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._stderr_buf = stderr
        assert expected in tunnel._ssm_exit_error(1)

    def test_silent_exit_falls_back_to_the_bare_code(self) -> None:
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        assert tunnel._ssm_exit_error(2) == "SSM session exited with code 2"

    def test_the_ssm_transport_is_routed_to_the_ssm_classifier(self) -> None:
        """Running SSM stderr through the ssh matchers would report an
        ``AccessDenied`` as an ssh auth failure and send the operator to the
        wrong fix."""
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._stderr_buf = "An error occurred (AccessDeniedException)"
        error = tunnel._exit_error(1)
        assert "IAM denied" in error
        assert "ssh auth failed" not in error


class TestTransportParams:
    def test_ssh_target_is_the_host(self) -> None:
        params = _TransportParams(method="ssh", ssh_host="cd-1-alias")
        assert params.target == "cd-1-alias"

    def test_ssm_target_is_the_instance_id(self) -> None:
        params = _TransportParams(
            method="ssm", ssh_host="ignored", ssm_target="i-0123456789abcdef0"
        )
        assert params.target == "i-0123456789abcdef0"

    def test_tunnel_kwargs_carry_the_transport_dimensions(self) -> None:
        params = _TransportParams(
            method="ssm",
            ssm_target="i-0123456789abcdef0",
            aws_profile="zibble",
            aws_region="us-west-2",
        )
        assert params.tunnel_kwargs() == {
            "transport": "ssm",
            "ssm_target": "i-0123456789abcdef0",
            "aws_profile": "zibble",
            "aws_region": "us-west-2",
        }


class TestExitErrorDetailWindow:
    """The 200-char detail budget must not be spent on benign stderr written
    BEFORE the classified failure line: under launchd/systemd with no ``TERM``,
    arbitrary ``LocalCommand`` output (repeated ``tput`` warnings) precedes the
    real diagnostic, so a head slice surfaces the cosmetic warning while the
    classifier state is correct. The window must anchor on the matched
    phrase."""

    # 5 x 45 chars = 225 chars of benign noise, > the 183-char budget remainder.
    _NOISE = "tput: No value for $TERM and no -T specified\n" * 5

    def test_classified_reason_survives_leading_localcommand_noise(self) -> None:
        tunnel = _tunnel()
        tunnel._stderr_buf = self._NOISE + "client_loop: send disconnect: Connection reset by peer"
        error = tunnel._exit_error(255)
        assert error.startswith("ssh tunnel transport drop:")
        assert "Connection reset by peer" in error

    def test_unclassified_stderr_keeps_the_head_slice(self) -> None:
        tunnel = _tunnel()
        tunnel._stderr_buf = self._NOISE + "some entirely unclassified failure text"
        error = tunnel._exit_error(255)
        assert error.startswith("ssh exited 255: tput: No value for $TERM")
        assert "unclassified failure" not in error

    def test_ssm_detail_window_is_also_anchored(self) -> None:
        tunnel = _tunnel(transport="ssm", ssm_target="i-0123456789abcdef0")
        tunnel._stderr_buf = (
            "z" * 250 + "\nAn error occurred (TargetNotConnected) when calling StartSession"
        )
        error = tunnel._ssm_exit_error(1)
        assert "not a connected managed node" in error
        assert "TargetNotConnected" in error


class TestSanitizeBannerAnchor:
    def test_short_text_is_returned_whole(self) -> None:
        assert _sanitize_banner("short", anchor="connection reset") == "short"

    def test_banner_scrub_is_the_exfil_first_composition(self) -> None:
        """A long-query exfil URL in a proxy banner loses its WHOLE url.

        The banner buffer is proxy-controlled, and `redact_exfiltration_urls`
        classifies partly by query length before replacing the entire url — a
        hand-sequenced creds-first pair here would shorten `?token=<long>`
        first and defeat it, leaving the destination and payload parameters in
        the tunnel status detail. The scrub must stay the canonical
        `security.redact()` composition (the seam `discover.py`'s
        TestRedactExternalLayerOrder pins).
        """
        banner = (
            "refused: https://collect.attacker.example/?token=" + "aB3" * 70 + "&host=corp-laptop"
        )
        out = _sanitize_banner(banner)
        assert "corp-laptop" not in out
        assert "?token=" not in out

    def test_no_anchor_takes_the_head(self) -> None:
        assert _sanitize_banner("a" * 300) == "a" * 200

    def test_anchor_centers_the_window_on_the_matched_line(self) -> None:
        text = "n" * 250 + "\nError: Connection reset by peer"
        out = _sanitize_banner(text, anchor="connection reset")
        assert "Connection reset by peer" in out
        assert len(out) == 200

    def test_a_long_single_line_still_keeps_the_phrase_in_the_window(self) -> None:
        """The proxy controls the buffer, so LocalCommand output with no
        trailing newline can merge onto ssh's diagnostic into one arbitrarily
        long line; centering on the phrase (not its line) must still surface
        the reason."""
        text = "x" * 400 + "client_loop: send disconnect: Connection reset by peer"
        out = _sanitize_banner(text, anchor="connection reset")
        assert "Connection reset by peer" in out
        assert len(out) == 200

    def test_redaction_happens_before_the_window_is_taken(self) -> None:
        token = "ghp_" + "a1B2" * 9
        text = "m" * 250 + f"\ntoken {token} then Connection reset by peer"
        out = _sanitize_banner(text, anchor="connection reset")
        assert token not in out
        assert "Connection reset by peer" in out

    def test_a_missing_anchor_falls_back_to_the_head(self) -> None:
        assert _sanitize_banner("b" * 300, anchor="connection refused") == "b" * 200
