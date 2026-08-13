"""Tests for kiro_crew.dashboard.port_reclaim.

The decision logic in :func:`reclaim_stale_gateway_port` is exercised through
injected collaborators so no test ever shells out to ``lsof``/``ps`` or sends a
real signal. Two real-socket tests cover the HTTP liveness probe end to end.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from kiro_crew.dashboard import port_reclaim as pr

# ---------------------------------------------------------------------------
# reclaim_stale_gateway_port — outcome matrix (all collaborators injected)
# ---------------------------------------------------------------------------


async def _healthy(_port: int, _timeout: float) -> bool:
    return True


async def _unhealthy(_port: int, _timeout: float) -> bool:
    return False


@pytest.mark.asyncio
async def test_lsof_unavailable_returns_unavailable() -> None:
    outcome = await pr.reclaim_stale_gateway_port(
        5476, list_listeners=lambda _p: None, probe_healthy=_unhealthy
    )
    assert outcome == pr.UNAVAILABLE


@pytest.mark.asyncio
async def test_no_listeners_returns_no_holder() -> None:
    outcome = await pr.reclaim_stale_gateway_port(
        5476, list_listeners=lambda _p: [], probe_healthy=_unhealthy
    )
    assert outcome == pr.NO_HOLDER


@pytest.mark.asyncio
async def test_foreign_holder_is_never_touched() -> None:
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [4321],
        is_kirocrew=lambda _pid: False,  # not a KiroCrew process
        probe_healthy=_unhealthy,
        terminate=_terminate,
    )
    assert outcome == pr.FOREIGN_HOLDER
    assert terminate_called is False


@pytest.mark.asyncio
async def test_healthy_peer_is_never_killed() -> None:
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_healthy,  # responds → legitimate owner
        terminate=_terminate,
    )
    assert outcome == pr.HEALTHY_PEER
    assert terminate_called is False


@pytest.mark.asyncio
async def test_stale_kirocrew_gateway_is_reclaimed() -> None:
    killed: list[int] = []

    async def _terminate(pids: list[int]) -> bool:
        killed.extend(pids)
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234, 1234, 5678],  # dupes tolerated
        is_kirocrew=lambda pid: pid in (1234, 5678),
        probe_healthy=_unhealthy,  # no HTTP reply → wedged
        terminate=_terminate,
    )
    assert outcome == pr.RECLAIMED
    assert killed == [1234, 5678]


@pytest.mark.asyncio
async def test_pid_reuse_recheck_drops_stranger() -> None:
    """A holder that no longer passes identity at the pre-kill re-check is dropped."""
    calls = {"n": 0}

    def _checker(_pid: int) -> bool:
        calls["n"] += 1
        # True on the initial filter, False on the pre-kill re-check → recycled.
        return calls["n"] <= 1

    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=_checker,
        probe_healthy=_unhealthy,
        terminate=_terminate,
    )
    assert outcome == pr.NO_HOLDER
    assert terminate_called is False


@pytest.mark.asyncio
async def test_reclaim_failed_when_terminate_returns_false() -> None:
    async def _terminate(_pids: list[int]) -> bool:
        return False  # e.g. PermissionError

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        terminate=_terminate,
    )
    assert outcome == pr.RECLAIM_FAILED


@pytest.mark.asyncio
async def test_slow_identity_check_times_out_to_unavailable() -> None:
    """A wedged ps (slow identity check) degrades to UNAVAILABLE, never hangs."""
    import time as _time

    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    def _slow(_pid: int) -> bool:
        _time.sleep(0.5)  # simulate a wedged/stale-NFS ps
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=_slow,
        probe_healthy=_unhealthy,
        terminate=_terminate,
        identity_timeout=0.05,  # trips well before _slow returns
    )
    assert outcome == pr.UNAVAILABLE
    assert terminate_called is False


@pytest.mark.asyncio
async def test_ps_unavailable_during_identity_check_is_unavailable() -> None:
    def _boom(_pid: int) -> bool:
        raise FileNotFoundError("ps not found")

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=_boom,
        probe_healthy=_unhealthy,
    )
    assert outcome == pr.UNAVAILABLE


# ---------------------------------------------------------------------------
# _probe_gateway_healthy — real sockets
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_probe_healthy_true_for_responsive_server() -> None:
    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await pr._probe_gateway_healthy(port, timeout=2.0) is True


@pytest.mark.asyncio
async def test_probe_healthy_false_when_nothing_listening() -> None:
    # Nothing is listening on a just-freed ephemeral port → connect refused.
    assert await pr._probe_gateway_healthy(_free_port(), timeout=0.5) is False


@pytest.mark.asyncio
async def test_probe_healthy_false_for_wedged_socket() -> None:
    """A socket that accepts but never replies must read as unhealthy."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        # Never accept()/reply → the read times out → unhealthy.
        assert await pr._probe_gateway_healthy(port, timeout=0.3) is False
    finally:
        listener.close()


# ---------------------------------------------------------------------------
# _terminate_pids — escalation logic with injected clock/liveness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_returns_true_when_already_gone(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    # Patch BOTH delivery primitives so the test is platform agnostic: POSIX
    # _signal calls os.kill, Windows _signal calls kill_process_tree.
    monkeypatch.setattr(pr.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        pr.platform_compat, "kill_process_tree",
        lambda pid, sig: signals.append((pid, sig)),
    )
    # Patch the liveness check used inside _terminate_pids.
    import kiro_crew.cli_server as cli_server

    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)

    ok = await pr._terminate_pids([1234], term_wait=0.1, kill_wait=0.1, poll=0.01)
    assert ok is True
    # Only SIGTERM sent; no escalation needed.
    assert signals == [(1234, pr.platform_compat.SIGTERM)]


@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    import kiro_crew.cli_server as cli_server

    # Survives SIGTERM, dies after SIGKILL is issued.
    state = {"alive": True}

    def _exited(_pid: int) -> bool:
        return not state["alive"]

    def _kill(pid: int, sig: int) -> None:
        signals.append((pid, sig))
        if sig == pr.platform_compat.SIGKILL:
            state["alive"] = False

    monkeypatch.setattr(pr.os, "kill", _kill)
    monkeypatch.setattr(pr.platform_compat, "kill_process_tree", _kill)
    monkeypatch.setattr(cli_server, "_pid_exited", _exited)

    ok = await pr._terminate_pids([1234], term_wait=0.05, kill_wait=0.2, poll=0.01)
    assert ok is True
    assert (1234, pr.platform_compat.SIGTERM) in signals
    assert (1234, pr.platform_compat.SIGKILL) in signals


@pytest.mark.asyncio
async def test_terminate_returns_false_on_permission_error(monkeypatch) -> None:
    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError("not allowed")

    monkeypatch.setattr(pr.os, "kill", _kill)
    monkeypatch.setattr(pr.platform_compat, "kill_process_tree", _kill)
    ok = await pr._terminate_pids([1], term_wait=0.05, kill_wait=0.05, poll=0.01)
    assert ok is False


# ---------------------------------------------------------------------------
# _listeners_on_port - cross-platform lookup (POSIX lsof / Windows netstat)
# ---------------------------------------------------------------------------


def test_listeners_windows_uses_find_listening_pids(monkeypatch) -> None:
    """On Windows the listener lookup routes through
    platform_compat.find_listening_pids (netstat -ano), not lsof."""
    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pr.platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(pr.platform_compat, "find_listening_pids", lambda port: [4242])
    assert pr._listeners_on_port(5476) == [4242]


def test_listeners_windows_tool_absent_returns_none(monkeypatch) -> None:
    """When netstat is unavailable on Windows the lookup returns None so the
    caller degrades to wait/retry rather than mis-reporting no holder."""
    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pr.platform_compat, "listening_pid_tool_available", lambda: False)
    assert pr._listeners_on_port(5476) is None
