"""``instances.ssh_tunnel_manager`` — the ``_SshTunnel`` teardown / exit paths.

``test_instances.py`` covers the state machine's happy path, the probe loop and
the ssh error classifier. What it leaves unobserved is everything that runs when
a tunnel goes DOWN, plus the whole SSM error vocabulary:

* ``_port_reachable`` — the one-second loopback probe both the readiness wait and
  the health loop are built on, in both directions;
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
