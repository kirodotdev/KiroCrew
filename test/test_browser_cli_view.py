"""Supervision of ``playwright-cli show``: loopback bind, health, lifecycle."""

from __future__ import annotations

import contextlib
import http.server
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod


class FakeProc:
    """Stand-in for the supervised child; never touches a real process."""

    def __init__(self, alive: bool = True, pid: int = 424242) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 1
        self.killed = False

    def poll(self) -> int | None:
        return None if self._alive else self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._alive = False
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._alive = False


class _FakeClock:
    """Stand-in for the module's ``time``, advanced only by the code under test.

    ``ensure_running``'s readiness gate is a ``time.monotonic`` deadline polled
    at ``time.sleep(_POLL_INTERVAL_S)``, so driving the clock from the sleeps
    turns "wait 30 real seconds" into a fixed, instant tick count.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def reset_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Clear the module singleton and neutralize real process signalling."""
    signalled: list[int] = []
    monkeypatch.setattr(mod, "_proc", None)
    monkeypatch.setattr(mod, "_info", None)
    monkeypatch.setattr(mod, "_relay", None)
    monkeypatch.setattr(mod, "_child_port", None)
    monkeypatch.setattr(mod, "_last_reason", None)
    # Ownership lookups are undecidable by default, so no test shells out to
    # lsof/netstat by accident. `_port_owner` then answers UNPROVEN, which is the
    # pre-identity behaviour every existing test was written against; the tests
    # that exercise ownership opt in with `_stub_port_owner`.
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
    monkeypatch.setattr(
        platform_compat,
        "kill_process_tree",
        lambda pid, sig=platform_compat.SIGTERM: signalled.append(pid) or True,
    )
    yield signalled
    mod._proc = None
    mod._info = None
    mod._relay = None
    mod._child_port = None
    mod._last_reason = None


def _stub_port_owner(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listener_pids: tuple[int, ...],
    descendants: tuple[int, ...] = (),
    tool: bool = True,
) -> None:
    """Make the port->PID lookup answer with *listener_pids*.

    Stubs the ``platform_compat`` primitives rather than ``_port_owner`` itself,
    so the module's own tier logic (tool absent, empty lookup, descendant match)
    is what the tests exercise.
    """
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: tool)
    monkeypatch.setattr(
        platform_compat,
        "find_port_listeners",
        lambda port: [
            platform_compat.PortListener(pid=p, address="127.0.0.1", family="4")
            for p in listener_pids
        ],
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: list(descendants))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Answers ``/`` with 302, exactly as the real dashboard does."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        self.send_response(302)
        self.send_header("Location", "/dashboard")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        pass


@pytest.fixture
def redirecting_server() -> Iterator[int]:
    """A concurrent loopback HTTP server whose root answers 302.

    Relay tests hold more than one connection open at a time.  A serial
    ``HTTPServer`` leaves one upstream leg queued behind another, so closing
    that queued connection cannot finish both relay pumps until the unrelated
    active connection also closes.  Which relay worker reaches the server
    first is scheduler-dependent.  Match the real dashboard's concurrent
    connection handling so each test connection owns an independent handler.
    """
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    # Make server_close() join every request thread after the clients are torn
    # down; no daemon handler may leak into the next test.
    srv.daemon_threads = False
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_show_argv_binds_explicit_loopback_host() -> None:
    """The default listener is IPv6-only, so ``--host 127.0.0.1`` must be passed."""
    argv = mod._show_argv("/n/playwright-cli", 45613)

    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "45613"
    assert argv[:2] == ["/n/playwright-cli", "show"]


def test_show_argv_never_binds_a_routable_address() -> None:
    """A non-loopback bind would publish remote browser input to the network."""
    argv = mod._show_argv("/n/playwright-cli", 45613)

    assert "0.0.0.0" not in argv
    assert "::" not in argv
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


def test_health_accepts_a_302(redirecting_server: int) -> None:
    """``/`` answers 302; a health check that demanded 200 would report dead."""
    assert mod._healthy(redirecting_server) is True


def test_health_false_when_nothing_is_listening() -> None:
    assert mod._healthy(_free_port()) is False


def test_free_port_is_bindable_loopback() -> None:
    port = mod._free_port()

    assert 1 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_free_port_is_not_hardcoded() -> None:
    """A fixed port would collide with whatever else the operator runs."""
    assert mod._free_port() != mod._free_port()


def test_ensure_running_returns_none_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawned.append([cli]) or FakeProc())

    assert mod.ensure_running() is None
    assert spawned == []


def test_ensure_running_spawns_with_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real argv reaching the child carries the explicit loopback bind."""
    recorded: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)

    def fake_spawn(cli: str, port: int) -> FakeProc:
        recorded.append(mod._show_argv(cli, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running()

    assert info is not None
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert info.url == f"http://127.0.0.1:{info.port}"
    assert argv[argv.index("--port") + 1] == str(info.port)


def test_ensure_running_pins_the_configured_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pin is served by a held relay listener; the child stays ephemeral.

    The pinned port must never appear in the child argv — handing it to the
    child would reopen the probe-to-bind window a local squatter can win. The
    module claims the pin itself and the child binds its own ephemeral port.
    """
    recorded: list[list[str]] = []
    opened: list[tuple[object, int]] = []
    claimed: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)

    sentinel_listener = object()
    monkeypatch.setattr(
        mod, "_claim_listener", lambda port: claimed.append(port) or sentinel_listener
    )

    class FakeRelay:
        def __init__(self) -> None:
            self.closed = False

        @classmethod
        def from_listener(cls, listener: object, target_port: int) -> "FakeRelay":
            opened.append((listener, target_port))
            return cls()

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(mod, "_Relay", FakeRelay)

    def fake_spawn(cli: str, port: int) -> FakeProc:
        recorded.append(mod._show_argv(cli, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running(port=45613)

    assert info is not None
    assert info.port == 45613
    assert info.url == "http://127.0.0.1:45613"
    assert claimed == [45613]
    assert opened == [(sentinel_listener, 51515)]
    argv = recorded[0]
    # The child binds its own ephemeral port; the pin never reaches its argv.
    assert argv[argv.index("--port") + 1] == "51515"
    # Pinning the port must not loosen the loopback bind.
    assert argv[argv.index("--host") + 1] == "127.0.0.1"


def test_pinned_start_claims_the_pin_before_choosing_the_child_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pin is bound before _free_port() runs, so an ephemeral-range pin can
    never be handed back as the child's port (bind collision by construction)."""
    order: list[str] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())

    pin = _free_port()
    real_claim = mod._claim_listener

    def tracking_claim(port: int) -> object:
        order.append("claim")
        return real_claim(port)

    real_free = mod._free_port

    def tracking_free() -> int:
        order.append("free_port")
        got = real_free()
        assert got != pin, "kernel handed out a port that is supposed to be bound"
        return got

    monkeypatch.setattr(mod, "_claim_listener", tracking_claim)
    monkeypatch.setattr(mod, "_free_port", tracking_free)

    try:
        info = mod.ensure_running(port=pin)
        assert info is not None and info.port == pin
        assert order == ["claim", "free_port"]
    finally:
        mod.stop()


def test_relay_forwards_http_to_the_target_port(redirecting_server: int) -> None:
    """The held listener relays real HTTP byte-for-byte to the child's port."""
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    try:
        assert mod._healthy(public)  # the 302 travels through the relay
    finally:
        relay.close()


def test_relay_close_joins_the_accept_thread(redirecting_server: int) -> None:
    """close() must not leave the accept thread running (test-visible side
    effect otherwise: a daemon thread outliving the test that spawned it)."""
    relay = mod._Relay.open(_free_port(), redirecting_server)
    assert relay is not None
    try:
        assert relay._thread.is_alive()
    finally:
        relay.close()
    assert not relay._thread.is_alive()


def test_relay_caps_concurrent_connections(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """Connections beyond the cap are refused; finished ones free their slot."""
    monkeypatch.setattr(mod, "_RELAY_MAX_CONNS", 2)
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    held: list[socket.socket] = []
    try:
        held = [socket.create_connection(("127.0.0.1", public), timeout=2) for _ in range(2)]
        # Give the accept loop a moment to register both.
        deadline = time.time() + 5
        while time.time() < deadline and len(relay._conns) < 2:
            time.sleep(0.05)
        assert len(relay._conns) == 2
        # The third is accepted at the OS level then closed by the cap: reads EOF.
        extra = socket.create_connection(("127.0.0.1", public), timeout=2)
        try:
            extra.settimeout(5)
            assert extra.recv(1) == b""
        finally:
            extra.close()
        # Closing a held connection frees its slot.
        held[0].close()
        deadline = time.time() + 5
        while time.time() < deadline and len(relay._conns) > 1:
            time.sleep(0.05)
        assert len(relay._conns) == 1
    finally:
        for sock in held:
            with contextlib.suppress(OSError):
                sock.close()
        relay.close()


def test_relay_close_tears_down_live_connections(redirecting_server: int) -> None:
    """close() closes tracked sockets instead of leaving pumps to linger."""
    public = _free_port()
    relay = mod._Relay.open(public, redirecting_server)
    assert relay is not None
    client: socket.socket | None = None
    try:
        client = socket.create_connection(("127.0.0.1", public), timeout=2)
        deadline = time.time() + 5
        while time.time() < deadline and not relay._conns:
            time.sleep(0.05)
        assert relay._conns
    finally:
        relay.close()
    assert not relay._conns
    try:
        client.settimeout(5)
        assert client.recv(1) == b""  # our side was closed
    finally:
        client.close()


def test_relay_slot_held_until_both_pumps_finish() -> None:
    """A half-closed connection keeps its cap slot: one pump exiting while the
    other stays parked must not free the accounting bound."""
    # A silent upstream that accepts and then neither sends nor closes, so the
    # upstream->client pump stays parked after the client half-closes. (The
    # redirecting HTTP fixture would close on EOF and end BOTH pumps.)
    silent = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent.bind(("127.0.0.1", 0))
    silent.listen(4)
    accepted: list[socket.socket] = []

    def _hold() -> None:
        with contextlib.suppress(OSError):
            conn, _ = silent.accept()
            accepted.append(conn)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()

    public = _free_port()
    relay = mod._Relay.open(public, int(silent.getsockname()[1]))
    assert relay is not None
    client: socket.socket | None = None
    try:
        client = socket.create_connection(("127.0.0.1", public), timeout=2)
        deadline = time.time() + 5
        while time.time() < deadline and not relay._conns:
            time.sleep(0.05)
        assert relay._conns
        # Half-close: our write side closes, so the client->upstream pump sees
        # EOF and exits, while the upstream->client pump stays parked on the
        # silent server.
        client.shutdown(socket.SHUT_WR)
        time.sleep(0.5)  # give the exited pump time to run on_done
        assert relay._conns, "slot was released while one pump was still live"
    finally:
        relay.close()
        for sock in [client, silent, *accepted]:
            if sock is not None:
                with contextlib.suppress(OSError):
                    sock.close()
        holder.join(timeout=5)
    assert not relay._conns


def test_claim_listener_sets_the_platform_exclusivity_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POSIX gets SO_REUSEADDR (TIME_WAIT rebind); Windows gets
    SO_EXCLUSIVEADDRUSE (without it another local process can rebind our held
    port by setting SO_REUSEADDR on ITS socket, defeating the ownership proof)."""
    calls: list[tuple[int, int, int]] = []
    real_setsockopt = socket.socket.setsockopt

    def spy(self: socket.socket, level: int, opt: int, value: int) -> None:
        calls.append((level, opt, value))
        real_setsockopt(self, level, opt, value)

    monkeypatch.setattr(socket.socket, "setsockopt", spy)

    listener = mod._claim_listener(_free_port())
    assert listener is not None
    listener.close()

    if platform_compat.IS_POSIX:
        assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls
    elif hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert (socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) in calls


def test_relay_open_returns_none_when_port_is_taken(redirecting_server: int) -> None:
    """bind() is the atomic ownership proof: an occupied pin is refused."""
    assert mod._Relay.open(redirecting_server, 51515) is None


def test_ensure_running_falls_back_to_ephemeral_when_unpinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``None`` and ``0`` both mean "unset": today's OS-assigned behavior."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)
    spawns: list[int] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    for unset in (None, 0):
        mod._proc = None
        mod._info = None
        info = mod.ensure_running(port=unset)
        assert info is not None and info.port == 51515, unset

    assert spawns == [51515, 51515]


def test_ensure_running_refuses_an_occupied_pinned_port(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """An occupied pin must fail BEFORE spawning: the doomed child would lose
    the bind and exit while ``_healthy`` accepts the unrelated occupant's
    response, recording a corpse as running and iframing a stranger."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    spawns: list[int] = []
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    info = mod.ensure_running(port=redirecting_server)

    assert info is None
    assert spawns == []
    st = mod.status()
    assert st["status"] == "stopped"
    assert st["reason"] is not None and str(redirecting_server) in st["reason"]


def test_status_reason_cleared_after_a_successful_start(
    monkeypatch: pytest.MonkeyPatch, redirecting_server: int
) -> None:
    """A stale failure reason must not outlive a later successful start."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())
    # First attempt fails on the occupied pin and records a reason.
    assert mod.ensure_running(port=redirecting_server) is None
    assert mod.status()["reason"] is not None

    # Second attempt (unpinned, healthy) succeeds; then stop() clears state.
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_free_port", lambda: 51515)
    assert mod.ensure_running() is not None
    assert mod.status()["reason"] is None
    mod.stop()
    assert mod.status() == {"status": "stopped", "url": None, "port": None, "reason": None}


def test_ensure_running_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy server is reused, not duplicated by a second panel mount."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    first = mod.ensure_running()
    second = mod.ensure_running()

    assert first == second
    assert len(spawns) == 1


def test_ensure_running_replaces_a_dead_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reusing a corpse would leave the panel permanently blank."""
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    mod.ensure_running()
    mod._proc = FakeProc(alive=False)

    assert mod.ensure_running() is not None
    assert len(spawns) == 2


def test_ensure_running_respawns_when_process_stops_answering(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """Alive but unresponsive is still unusable, and the stale child is reaped."""
    spawns: list[int] = []
    probes: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    healthy = {"value": True}

    def probe(port: int) -> bool:
        probes.append(port)
        return healthy["value"]

    monkeypatch.setattr(mod, "_healthy", probe)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc(pid=len(spawns))
    )
    # This test deliberately never satisfies the startup gate, so it would
    # otherwise wait out the real 30s budget one 0.25s tick at a time. The fake
    # clock only advances when the gate sleeps, which costs no wall time and
    # makes the tick count exact rather than timing-dependent.
    monkeypatch.setattr(mod, "time", _FakeClock())

    mod.ensure_running()
    healthy["value"] = False
    probes.clear()
    # Startup gate cannot pass while unhealthy, so this reports failure...
    assert mod.ensure_running() is None
    # ...and BOTH children were signalled rather than left holding a port: the
    # incumbent it declined to reuse (pid 1) and the replacement that never
    # answered (pid 2). Only checking pid 1 would leave the give-up path's reap
    # untested, since the incumbent is reaped before the replacement is spawned.
    assert reset_state == [1, 2]
    # The gate polled for its whole documented budget before giving up, one probe
    # per tick, plus the single probe of the incumbent it declined to reuse. A
    # budget that expires before its first poll would report the same failure
    # while proving nothing about an unhealthy child.
    assert len(probes) == 1 + int(mod._STARTUP_TIMEOUT_S / mod._POLL_INTERVAL_S)


def test_ensure_running_gives_up_when_child_exits_during_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: False)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(alive=False))

    assert mod.ensure_running() is None
    assert mod.status()["status"] == "stopped"


def test_ensure_running_returns_none_when_spawn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: None)

    assert mod.ensure_running() is None


def test_stop_reaps_child_without_a_global_kill(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """``stop()`` reaps only the child it spawned.

    A global ``show --kill`` would stop the daemon, but it stops EVERY session
    with it, including one the operator launched independently — and unsaved work
    goes with it. The child is spawned into its own session, so reaping its
    process group covers the server and the browser it started.
    """
    runs: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=777))
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda argv, **kw: runs.append(list(argv)) or None,
    )

    mod.ensure_running()
    mod.stop()

    assert runs == [], runs
    assert 777 in reset_state
    assert mod.status()["status"] == "stopped"


def test_stop_reaps_child_even_when_kill_command_fails(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """A failed daemon kill must not leave the child holding the port."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=888))

    def boom(argv: list[str], **kw: object) -> None:
        raise OSError("no such binary")

    monkeypatch.setattr(mod.subprocess, "run", boom)

    mod.ensure_running()
    mod.stop()

    assert 888 in reset_state
    assert mod._proc is None


def test_status_unavailable_without_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unavailable is distinct from stopped: starting the server cannot fix it."""
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    st = mod.status()

    assert st["status"] == "unavailable"
    assert st["url"] is None
    assert st["port"] is None
    assert st["reason"]


def test_status_running_reports_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc())

    info = mod.ensure_running()
    st = mod.status()

    assert info is not None
    assert st["status"] == "running"
    assert st["url"] == info.url
    assert st["port"] == info.port


def test_status_does_not_start_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())

    assert mod.status()["status"] == "stopped"
    assert spawns == []


# ── port ownership: reachability is not identity ────────────────────────────
#
# `_free_port` releases its probe socket before the child binds it, so a local
# process can take the number in between. `_healthy` then answers True for the
# squatter exactly as it would for our child, and the panel frames whatever the
# squatter serves — with input forwarding attached.


def test_port_owner_proves_the_direct_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


def test_port_owner_proves_a_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI spawns Node and helpers, so the listener is often not the child."""
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(9931,), descendants=(9931,))

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


def test_port_owner_names_a_squatter(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(777,), descendants=(9931,))

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_port_owner_is_unproven_without_the_lookup_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one fail-open branch: a host without lsof must keep its panel.

    Static per host rather than per start, and not attacker-controllable --
    removing the tool needs the access that makes this panel moot.
    """
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    assert mod._port_owner(45613, proc) == mod._OWNER_UNPROVEN


def test_port_owner_refuses_when_a_working_lookup_sees_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The port just answered, so a lookup that shows no owner is not ours.

    Covers a squatter owned by another user (invisible to our lsof) and a lookup
    that timed out under load. A refused start is recoverable; adopting an
    unverified responder is not.
    """
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=())

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_port_owner_refuses_without_a_child_to_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recorded child means nothing can be proved ours."""
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, None) == mod._OWNER_FOREIGN


def test_ensure_running_refuses_a_squatter_on_the_child_port(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """The whole point: a foreign responder must not become the panel."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    _stub_port_owner(monkeypatch, listener_pids=(777,))

    assert mod.ensure_running() is None
    assert mod._info is None
    assert mod._child_port is None
    # The child we spawned is reaped rather than left holding nothing.
    assert proc.pid in reset_state
    status = mod.status()
    assert status["status"] == "stopped"
    assert "took port" in (status["reason"] or "")


def test_ensure_running_adopts_a_proven_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    info = mod.ensure_running()

    assert info is not None
    assert mod._child_port == info.port


def test_ensure_running_adopts_when_ownership_cannot_be_proved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No regression on a host where the lookup tool is unavailable.

    Adopting on reachability alone is the pre-identity behaviour, so it is warned
    about rather than done silently.
    """
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=4242))
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    with caplog.at_level("WARNING"):
        assert mod.ensure_running() is not None

    assert any("cannot verify which process holds port" in r.message for r in caplog.records)


def test_reuse_refuses_a_squatter_that_took_a_live_childs_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child can stay alive after losing its listener; the port is then free.

    Without re-proving ownership on reuse, the next panel mount hands back the
    squatter that took it.
    """
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    # A FRESH child per spawn. Reusing one fake would let the reaped corpse end
    # the second attempt at the "exited during startup" check, so the test would
    # pass without the reuse gate having done anything.
    spawned: list[FakeProc] = []

    def _spawn_fresh(cli: str, port: int) -> FakeProc:
        child = FakeProc(pid=4242)
        spawned.append(child)
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn_fresh)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))
    first = mod.ensure_running()
    assert first is not None

    # The child is still alive, but a foreign process now answers its port.
    _stub_port_owner(monkeypatch, listener_pids=(777,))

    assert mod.ensure_running() is None
    assert len(spawned) == 2, "the reuse gate did not reject the foreign responder"


def test_status_does_not_report_a_squatter_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` is what hands the panel its URL, so it obeys the same rule."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))
    assert mod.ensure_running() is not None
    assert mod.status()["status"] == "running"

    _stub_port_owner(monkeypatch, listener_pids=(777,))

    assert mod.status()["status"] == "stopped"


def test_stop_clears_the_recorded_child_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale child port would be proved against a reaped tree."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=4242))
    _stub_port_owner(monkeypatch, listener_pids=(4242,))
    assert mod.ensure_running() is not None

    mod.stop()

    assert mod._child_port is None
