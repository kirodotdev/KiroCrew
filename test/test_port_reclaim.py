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
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture(autouse=True)
def _fabricated_holders_are_old(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The pids these cases fabricate may name a real young process on the host;
    read every holder as long-lived unless a case says otherwise."""
    _floor_monkeypatch.setattr(pr, "_holder_age_secs", lambda _pid: 3600.0)
    # No real lsof: a fabricated pid holds no socket the ::1 probe could reach.
    _floor_monkeypatch.setattr(
        pr.platform_compat, "probe_port_listeners", lambda _port: ([], True)
    )

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
async def test_default_identity_gate_is_the_argv_check(monkeypatch) -> None:
    """argv is reclaim's gate: with no injected checker, a stale
    holder the argv check declines is never terminated."""
    import kiro_crew.cli_server as cli_server

    seen: list[int] = []

    def _declines(pid: int) -> bool:
        seen.append(pid)
        return False

    monkeypatch.setattr(cli_server, "_is_kirocrew_process", _declines)
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [4321],
        probe_healthy=_unhealthy,
        terminate=_terminate,
    )
    assert outcome == pr.FOREIGN_HOLDER
    assert seen == [4321]
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
async def test_a_healthy_peer_bound_to_the_v6_wildcard_is_never_killed() -> None:
    """A gateway bound to ``::`` (IPv6 only, no IPv4 secondary) answers on
    ``::1`` alone. Its own socket takes that probe, so it is a healthy peer."""
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,  # nothing on 127.0.0.1
        probe_v6_healthy=_healthy,
        holder_reaches_v6=lambda _port, pids: pids == [1234],
        terminate=_terminate,
    )
    assert outcome == pr.HEALTHY_PEER
    assert terminate_called is False


@pytest.mark.asyncio
async def test_another_program_on_v6_loopback_cannot_vouch_for_a_stale_holder() -> None:
    """When the holder's own sockets do not take ``::1``, whatever answers there
    is someone else, and the stale holder is still reclaimed."""
    v6_probed = False

    async def _v6(_port: int, _timeout: float) -> bool:
        nonlocal v6_probed
        v6_probed = True
        return True

    async def _terminate(_pids: list[int]) -> bool:
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        probe_v6_healthy=_v6,
        holder_reaches_v6=lambda _port, _pids: False,
        terminate=_terminate,
    )
    assert outcome == pr.RECLAIMED
    assert v6_probed is False


@pytest.mark.parametrize(
    ("listeners", "completed", "expected"),
    [
        ([pr.platform_compat.PortListener(1234, "::", "6")], True, True),
        ([pr.platform_compat.PortListener(1234, "::1", "6")], True, True),
        ([pr.platform_compat.PortListener(1234, "*", "6")], True, True),
        ([pr.platform_compat.PortListener(1234, "127.0.0.1", "4")], True, False),
        ([pr.platform_compat.PortListener(9999, "::1", "6")], True, False),  # not the holder
        ([], True, False),
        ([], False, None),  # lookup failed: unknown, never "not on ::1"
    ],
    ids=["v6-wildcard", "v6-loopback", "lsof-v6-star", "v4-only", "other-pid", "none", "failed"],
)
def test_holder_reaches_v6_loopback_only_through_its_own_socket(
    _floor_monkeypatch: pytest.MonkeyPatch, listeners: list, completed: bool, expected: object
) -> None:
    _floor_monkeypatch.setattr(
        pr.platform_compat, "probe_port_listeners", lambda _port: (listeners, completed)
    )
    assert pr._holder_reaches_v6_loopback(5476, [1234]) is expected


@pytest.mark.asyncio
async def test_an_unreadable_listener_lookup_never_kills_the_holder() -> None:
    """When the holder's addresses cannot be read, a healthy v6-only holder
    cannot be told from a stale one, so the reclaim falls back to wait/retry."""
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        holder_reaches_v6=lambda _port, _pids: None,
        terminate=_terminate,
    )
    assert outcome == pr.UNAVAILABLE
    assert terminate_called is False


@pytest.mark.asyncio
async def test_a_booting_peer_holding_its_reserved_port_is_never_killed() -> None:
    """A gateway in its boot pass holds the port reserved (bound and
    listening, accept deferred), so it does not answer the probe. Its young age
    marks it as booting, and no holder is signalled."""
    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234, 5678],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,  # silent: reserved, not yet accepting
        terminate=_terminate,
        holder_age=lambda pid: 3.0 if pid == 5678 else 3600.0,
        thread_count=lambda _pid: 40,
    )
    assert outcome == pr.BOOTING_PEER
    assert terminate_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("age", "threads"),
    [
        (pr.BOOTING_PEER_MAX_AGE_SECS, 40),  # old enough to be wedged
        (3.0, 1),  # young, but a single-threaded fork is no gateway
        (None, 40),  # age unreadable: not taken as young
    ],
)
async def test_a_silent_holder_that_is_not_booting_is_still_reclaimed(
    age: float | None, threads: int
) -> None:
    killed: list[int] = []

    async def _terminate(pids: list[int]) -> bool:
        killed.extend(pids)
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        terminate=_terminate,
        holder_age=lambda _pid: age,
        thread_count=lambda _pid: threads,
    )
    assert outcome == pr.RECLAIMED
    assert killed == [1234]


@pytest.mark.asyncio
async def test_a_young_holder_with_unknown_thread_count_is_spared() -> None:
    """Off Linux the thread count is unreadable, and age alone decides."""
    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        terminate=lambda _pids: pytest.fail("a booting peer was signalled"),
        holder_age=lambda _pid: 10.0,
        thread_count=lambda _pid: None,
    )
    assert outcome == pr.BOOTING_PEER


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
    """A holder that fails identity at the pre-kill re-check is dropped."""
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
@pytest.mark.parametrize(
    ("windows", "hint"), [(True, "taskkill /PID 1234 /T /F"), (False, "kill -9 1234")]
)
async def test_reclaim_failed_names_the_platform_s_own_force_kill(
    monkeypatch: pytest.MonkeyPatch, windows: bool, hint: str
) -> None:
    """Windows reaches this path too (netstat lookup, taskkill terminate), so
    the manual remedy must not be a POSIX-only ``kill -9`` there."""
    from unittest.mock import MagicMock

    async def _terminate(_pids: list[int]) -> bool:
        return False

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", windows)
    log = MagicMock()
    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
        terminate=_terminate,
        log=log,
    )
    assert outcome == pr.RECLAIM_FAILED
    fmt, *args = log.error.call_args.args
    assert hint in fmt % tuple(args)


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
async def test_probe_healthy_true_for_ipv6_only_responsive_server() -> None:
    """A live gateway bound to ``::`` (IPv6 only, as ``_bind_once`` binds it)
    answers on ``::1`` alone, and must not read as stale."""
    if not socket.has_ipv6:
        pytest.skip("no IPv6")

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    try:
        server = await asyncio.start_server(_handle, "::1", 0)
    except OSError:
        pytest.skip("cannot bind ::1")
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await pr._probe_v6_loopback_healthy(port, timeout=FALLBACK_TIMEOUT_SECS) is True


@pytest.mark.asyncio
async def test_reclaim_leaves_a_live_v6_only_gateway_alone(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the public entry point with its default health probe: a real
    HTTP server listening on ``::1`` only (as a ``::``-bound gateway does, with
    no IPv4 secondary) is a healthy peer, never terminated. The listener lookup
    reports that holder's own ``::1`` socket, as lsof does."""
    if not socket.has_ipv6:
        pytest.skip("no IPv6")

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(1024)
        writer.write(b"HTTP/1.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    try:
        server = await asyncio.start_server(_handle, "::1", 0)
    except OSError:
        pytest.skip("cannot bind ::1")
    port = server.sockets[0].getsockname()[1]
    holder = 1234
    _floor_monkeypatch.setattr(
        pr.platform_compat,
        "probe_port_listeners",
        lambda _port: ([pr.platform_compat.PortListener(holder, "::1", "6")], True),
    )
    terminated: list[list[int]] = []

    async def _terminate(pids: list[int]) -> bool:
        terminated.append(pids)
        return True

    async with server:
        outcome = await pr.reclaim_stale_gateway_port(
            port,
            list_listeners=lambda _p: [holder],
            is_kirocrew=lambda _pid: True,
            terminate=_terminate,
        )
    assert outcome == pr.HEALTHY_PEER
    assert terminated == []


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


def test_listeners_posix_resolves_lsof_from_trusted_dirs(monkeypatch) -> None:
    """The POSIX branch spawns the trusted-directory lsof, never a PATH-resolved
    one: PATH can lead with a same-uid-writable dir, and this probe's output
    decides which PIDs get signalled."""
    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.platform_compat, "trusted_system_bin", lambda name: "/usr/bin/lsof")
    argvs: list[list[str]] = []

    def _check_output(argv, **_kwargs):
        argvs.append(list(argv))
        return "4242\n"

    monkeypatch.setattr(pr.subprocess, "check_output", _check_output)
    assert pr._listeners_on_port(5476) == [4242]
    assert argvs and argvs[0][0] == "/usr/bin/lsof"


def test_listeners_posix_tool_absent_returns_none(monkeypatch) -> None:
    """No trusted lsof must read as "cannot determine" (wait/retry), never as
    "no holder" -- and nothing may be spawned in that case."""
    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.platform_compat, "trusted_system_bin", lambda name: None)

    def _never(*_a, **_k):  # pragma: no cover - guards the refusal
        raise AssertionError("spawned despite no trusted lsof")

    monkeypatch.setattr(pr.subprocess, "check_output", _never)
    assert pr._listeners_on_port(5476) is None
