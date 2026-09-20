"""Regression tests for the gateway listener guard.

The bug: on Windows, CPython's proactor loop closes the LISTEN socket after a
single failed ``accept()`` (``[WinError 64]`` from an aborted tunnelled peer)
and never re-arms it. The gateway process stays alive, its accepted
connections stay ESTABLISHED, and every new connection is refused. These tests
prove the guard (a) rebinds the same port after that exact event, (b) treats a
failed loopback HTTP self-probe as unhealthy even when the process is alive,
(c) exits non-zero when it cannot rebind, and (d) leaves the crash-guard
exception handler in the chain.

Platform split: the recovery mechanics run on every platform against a real
aiohttp server (the dead-listener state is simulated); the proactor accept
injection and the SO_LINGER=0 RST case need Windows' proactor loop.
"""

from __future__ import annotations

import asyncio
import socket
import sys
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from kiro_crew.dashboard import listener_guard as lg
from kiro_crew.dashboard.listener_guard import (
    ACCEPT_FAILED_MESSAGE,
    LISTENER_LOST_EXIT_CODE,
    ListenerGuard,
    http_probe,
    listener_guard_exit_code,
    probe_target,
)

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _live(_request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


class _Served:
    """A real aiohttp server on an ephemeral loopback port plus its guard."""

    def __init__(self, **guard_kwargs: Any) -> None:
        self.app = web.Application()
        self.app.router.add_get("/api/live", _live)
        self.runner = web.AppRunner(self.app)
        self.shutdown = asyncio.Event()
        self._guard_kwargs = guard_kwargs
        self.guard: ListenerGuard | None = None
        self.site: web.TCPSite | None = None

    async def __aenter__(self) -> "_Served":
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.guard = ListenerGuard(self.runner, self.site, self.shutdown, **self._guard_kwargs)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.guard is not None:
            self.guard.stop()
        await self.runner.cleanup()

    @property
    def port(self) -> int:
        assert self.guard is not None
        return self.guard.port


async def _get_live(port: int) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}/api/live") as resp:
            return resp.status


def _listening_socket(site: web.TCPSite) -> socket.socket:
    server = site._server
    assert server is not None and server.sockets
    return server.sockets[0]._sock  # type: ignore[attr-defined]


async def _wait_until(pred: Any, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sockname", "expected"),
    [
        (("127.0.0.1", 5476), ("127.0.0.1", 5476)),
        (("0.0.0.0", 5476), ("127.0.0.1", 5476)),
        (("", 5476), ("127.0.0.1", 5476)),
        (("::", 5476, 0, 0), ("::1", 5476)),
        (("10.0.0.7", 5476), ("10.0.0.7", 5476)),
        ("/tmp/gateway.sock", None),
        (("127.0.0.1",), None),
    ],
)
def test_probe_target_maps_wildcards_to_loopback(sockname: Any, expected: Any) -> None:
    assert probe_target(sockname) == expected


def test_exit_code_helper_is_zero_without_a_guard() -> None:
    assert listener_guard_exit_code(None) == 0


@pytest.mark.asyncio
async def test_http_probe_true_for_serving_and_false_for_dead_port() -> None:
    async with _Served() as served:
        assert await http_probe("127.0.0.1", served.port, timeout=2.0) is True
    # The runner is cleaned up: the port refuses connections now.
    assert await http_probe("127.0.0.1", served.port, timeout=2.0) is False


@pytest.mark.asyncio
async def test_http_probe_false_for_non_http_listener() -> None:
    """A TCP listener that never speaks HTTP is not a serving gateway."""

    async def _silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await http_probe("127.0.0.1", port, timeout=0.1) is False
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# Recovery mechanics (every platform, real aiohttp server)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebinds_same_port_when_listener_reported_closed(monkeypatch: Any) -> None:
    """A dead listener is replaced by a fresh site on the SAME port; serving resumes."""
    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        old_site = guard.site
        port = served.port
        assert await _get_live(port) == 200

        # The state CPython leaves behind: the Server object exists, its socket
        # reports fileno() == -1. Simulated via the guard's own evidence reader
        # so the selector loop's fd bookkeeping is not disturbed.
        monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)

        assert await guard.check_now("test") is True
        assert guard.site is not old_site
        assert guard.port == port
        assert guard.recoveries == 1
        assert guard.exit_code == 0
        assert not served.shutdown.is_set()
        # The old site is unregistered and its server closed; the new one serves.
        assert old_site not in served.runner.sites
        assert guard.site in served.runner.sites
        assert await _get_live(port) == 200


@pytest.mark.asyncio
async def test_check_is_a_no_op_while_listener_is_open() -> None:
    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        site = guard.site
        assert guard.listener_open() is True
        assert await guard.check_now("test") is True
        assert guard.site is site
        assert guard.recoveries == 0


@pytest.mark.asyncio
async def test_accept_failed_report_triggers_recovery_and_chains_previous_handler(
    monkeypatch: Any,
) -> None:
    """The loop-exception hook reacts only to the proactor's message and keeps crash_guard."""
    seen: list[dict[str, Any]] = []

    def _previous(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        seen.append(context)

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_previous)
    try:
        async with _Served(interval=3600) as served:
            guard = served.guard
            assert guard is not None
            guard.arm()
            old_site = guard.site
            monkeypatch.setattr(guard, "listener_open", lambda: guard.site is not old_site)

            # An unrelated report: forwarded, no recovery.
            loop.call_exception_handler({"message": "Task exception was never retrieved"})
            await asyncio.sleep(0.05)
            assert guard.site is old_site
            assert len(seen) == 1

            # The proactor's report: forwarded AND recovery scheduled one tick later.
            exc = OSError(64, "The specified network name is no longer available")
            loop.call_exception_handler({"message": ACCEPT_FAILED_MESSAGE, "exception": exc})
            await _wait_until(lambda: guard.site is not old_site)
            assert len(seen) == 2
            assert seen[1]["exception"] is exc
            assert await _get_live(served.port) == 200

            # stop() hands the loop back to the previous handler.
            guard.stop()
            assert loop.get_exception_handler() is _previous
    finally:
        loop.set_exception_handler(None)


@pytest.mark.asyncio
async def test_periodic_probe_failure_is_confirmed_then_recovers(monkeypatch: Any) -> None:
    """A twice-failed loopback HTTP probe counts as dead even with an open socket."""
    calls: list[tuple[str, int]] = []

    async def _failing_probe(host: str, port: int, *, timeout: float) -> bool:
        calls.append((host, port))
        return False

    async with _Served(interval=0.05, confirm_delay=0.02, probe=_failing_probe) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        old_site = guard.site
        await _wait_until(lambda: guard.site is not old_site)
        # Probe, confirmation probe, then the rebind -- never a rebind on one miss.
        assert len(calls) >= 2
        assert calls[0] == ("127.0.0.1", served.port)
        assert guard.recoveries >= 1
        assert await _get_live(served.port) == 200


@pytest.mark.asyncio
async def test_transient_probe_miss_does_not_rebind() -> None:
    """One failed probe followed by a passing confirmation leaves the listener alone."""
    outcomes = iter([False, True, True, True])

    async def _flaky_probe(host: str, port: int, *, timeout: float) -> bool:
        return next(outcomes, True)

    async with _Served(interval=0.02, confirm_delay=0.01, probe=_flaky_probe) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        site = guard.site
        await asyncio.sleep(0.2)
        assert guard.site is site
        assert guard.recoveries == 0


@pytest.mark.asyncio
async def test_gives_up_with_nonzero_exit_and_shutdown_when_rebind_keeps_failing(
    monkeypatch: Any,
) -> None:
    """Unrecoverable listener: bounded attempts, then exit code + shutdown event."""
    attempts = 0

    async def _refuse_bind(self: web.TCPSite) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError(98, "Address already in use")

    async with _Served(
        interval=3600, max_attempts=3, backoff_base=0.001, max_backoff=0.002
    ) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        monkeypatch.setattr(guard, "listener_open", lambda: False)
        monkeypatch.setattr(web.TCPSite, "start", _refuse_bind)

        assert await guard.check_now("test") is False
        assert attempts == 3
        assert guard.exit_code == LISTENER_LOST_EXIT_CODE
        assert listener_guard_exit_code(guard) == LISTENER_LOST_EXIT_CODE
        assert served.shutdown.is_set()
        # Nothing half-started is left registered in the runner.
        assert not served.runner.sites


@pytest.mark.asyncio
async def test_recovery_declines_once_shutdown_requested(monkeypatch: Any) -> None:
    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        site = guard.site
        monkeypatch.setattr(guard, "listener_open", lambda: False)
        served.shutdown.set()
        assert await guard.check_now("test") is True
        assert guard.site is site
        assert guard.exit_code == 0


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_cancels_probe() -> None:
    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        task = guard._probe_task
        assert task is not None
        guard.stop()
        guard.stop()
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Windows proactor: the real CPython path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not IS_WINDOWS, reason="proactor accept path is Windows-only")
@pytest.mark.asyncio
async def test_windows_proactor_accept_oserror_does_not_kill_the_listener(
    monkeypatch: Any,
) -> None:
    """Inject ONE failing AcceptEx completion; the gateway must keep accepting.

    Without the guard, ``BaseProactorEventLoop._start_serving.loop`` closes the
    LISTEN socket on the ``OSError`` and never re-arms accept: every later
    connect is refused while the process lives on.
    """
    loop = asyncio.get_running_loop()
    assert isinstance(loop, asyncio.ProactorEventLoop)

    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        old_site = guard.site
        old_sock = _listening_socket(old_site)
        port = served.port
        assert await _get_live(port) == 200

        proactor = loop._proactor  # type: ignore[attr-defined]
        real_accept = proactor.accept
        injected = {"done": False}

        def _accept_once_failing(sock: socket.socket) -> Any:
            if not injected["done"] and sock is old_sock:
                injected["done"] = True
                fut = loop.create_future()
                loop.call_soon(
                    fut.set_exception,
                    OSError(64, "The specified network name is no longer available"),
                )
                return fut
            return real_accept(sock)

        monkeypatch.setattr(proactor, "accept", _accept_once_failing)

        # Re-arm accept on the old socket through the proactor's own loop()
        # closure: the pending real accept completes with a connection, and the
        # NEXT accept it arms is our failing future -- exactly the CPython path.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await _wait_until(lambda: injected["done"])
        # CPython closes the listener right after reporting the failure.
        await _wait_until(lambda: old_sock.fileno() == -1)
        # The guard saw the report and rebound the same port.
        await _wait_until(lambda: guard.site is not old_site)
        assert guard.port == port
        assert guard.recoveries == 1
        assert guard.exit_code == 0
        assert await _get_live(port) == 200


@pytest.mark.skipif(not IS_WINDOWS, reason="RST-on-accept race is a Windows proactor concern")
@pytest.mark.asyncio
async def test_windows_peer_rst_before_accept_keeps_serving() -> None:
    """Real peers that connect and immediately RST (SO_LINGER=0) never take the listener down."""
    async with _Served(interval=3600) as served:
        guard = served.guard
        assert guard is not None
        guard.arm()
        port = served.port

        def _connect_and_reset() -> None:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, __import__("struct").pack("ii", 1, 0))
            s.settimeout(2.0)
            s.connect(("127.0.0.1", port))
            s.close()  # linger 0 -> RST, possibly before the server-side accept completes

        for _ in range(25):
            await asyncio.to_thread(_connect_and_reset)
        await asyncio.sleep(0.2)

        assert guard.exit_code == 0
        assert not served.shutdown.is_set()
        assert guard.listener_open() is True
        assert await _get_live(port) == 200


# ---------------------------------------------------------------------------
# Wiring: the gateway's exit path consults the guard
# ---------------------------------------------------------------------------


def test_gateway_shutdown_consults_listener_guard_exit_code() -> None:
    """The gateway's exit status falls through to the guard's code.

    Exercises the real ``shutdown_exit_code`` alongside
    :func:`listener_guard_exit_code`: a shutdown the stale-asset watchdog did
    not cause contributes 0, so a guard that gave up must still produce a
    non-zero status. Without this the "alive but not listening" state would
    exit 0 and no supervisor would relaunch the gateway.
    """
    from kiro_crew.dashboard.stale_asset_watchdog import (
        STALE_ASSET_EXIT_CODE,
        shutdown_exit_code,
    )

    class _Guard:
        exit_code = LISTENER_LOST_EXIT_CODE

    guard: Any = _Guard()

    # No watchdog fired -> the guard's code is what the gateway exits with.
    assert shutdown_exit_code(None) == 0
    assert (shutdown_exit_code(None) or listener_guard_exit_code(guard)) == (
        LISTENER_LOST_EXIT_CODE
    )
    # A stale-asset shutdown keeps its own code; the guard never masks it.
    assert (STALE_ASSET_EXIT_CODE or listener_guard_exit_code(guard)) == STALE_ASSET_EXIT_CODE
    # A healthy guard contributes nothing, so a clean shutdown still exits 0.
    assert (shutdown_exit_code(None) or listener_guard_exit_code(None)) == 0
    # The two self-initiated shutdowns stay distinguishable in a post-mortem.
    assert LISTENER_LOST_EXIT_CODE not in (0, STALE_ASSET_EXIT_CODE)


def test_gateway_exit_path_reads_the_listener_guard() -> None:
    """The orchestrator's shutdown really consults the guard (pins the wiring)."""
    import inspect

    from kiro_crew.slack import gateway as gw

    source = inspect.getsource(gw.GatewayOrchestrator._shutdown_and_exit)
    assert "listener_guard_exit_code" in source


def test_both_gateway_entrypoints_arm_the_listener_guard() -> None:
    """``start_dashboard`` and the headless ``start_api_server`` both arm the guard.

    A listener that only the full dashboard protects would leave
    ``--slack-only`` gateways with the original defect.
    """
    import inspect

    from kiro_crew.dashboard import server as srv

    for entrypoint in (srv.start_dashboard, srv.start_api_server):
        source = inspect.getsource(entrypoint)
        assert "_arm_listener_guard" in source, entrypoint.__name__
        assert "_register_listener_guard_shutdown" in source, entrypoint.__name__


@pytest.mark.asyncio
async def test_cleanup_detaches_the_guard_before_the_sites_close() -> None:
    """A deliberate shutdown must not read as a lost listener.

    ``runner.cleanup()`` stops every site, leaving exactly the state the defect
    produces: a ``Server`` whose socket is closed. The ``on_cleanup`` hook from
    ``_register_listener_guard_shutdown`` runs before that, so the guard is
    already detached and never rebinds a site that is shutting down on purpose.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import server as srv

    app = web.Application()
    app.router.add_get("/api/live", _live)
    state: Any = SimpleNamespace()
    # MUST precede runner.setup(), which freezes the app's signal lists.
    srv._register_listener_guard_shutdown(app, state)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    guard = ListenerGuard(runner, site, asyncio.Event())
    guard.arm()
    state._listener_guard = guard
    port = guard.port
    rebinds: list[str] = []
    monkey_reason: Any = guard.check_now

    async def _recording_check(reason: str) -> bool:
        rebinds.append(reason)
        return await monkey_reason(reason)

    guard.check_now = _recording_check  # type: ignore[method-assign]

    await runner.cleanup()

    # Detached: no probe task left to observe the now-closed listener.
    assert guard._probe_task is None
    assert guard.listener_open() is False
    # And nothing treated the shutdown as an incident.
    assert rebinds == []
    assert guard.exit_code == 0
    assert guard.port == port


def test_module_documents_why_selector_loop_is_not_the_fix() -> None:
    """The rejected alternative stays on record next to the code."""
    doc = lg.__doc__ or ""
    assert "WindowsSelectorEventLoopPolicy" in doc
    assert "subprocess" in doc


@pytest.mark.asyncio
async def test_guard_is_armed_on_windows_only(monkeypatch: Any) -> None:
    """POSIX sites get no guard at all, not an idle one.

    The defect is the proactor loop's, and the selector loop keeps its listener
    registered across a failed accept. Arming everywhere would leave macOS and
    Linux running a periodic self-probe against a failure mode they cannot
    reach, so the wiring gates on the platform rather than on the probe finding
    nothing.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import server as srv

    async with _Served() as served:
        assert served.site is not None

        monkeypatch.setattr(srv.platform_compat, "IS_WINDOWS", False)
        posix_state: Any = SimpleNamespace()
        srv._arm_listener_guard(posix_state, served.runner, served.site)
        assert getattr(posix_state, "_listener_guard", None) is None

        monkeypatch.setattr(srv.platform_compat, "IS_WINDOWS", True)
        windows_state: Any = SimpleNamespace()
        srv._arm_listener_guard(windows_state, served.runner, served.site)
        guard = windows_state._listener_guard
        try:
            assert isinstance(guard, ListenerGuard)
            assert guard.port == served.port
        finally:
            guard.stop()
