"""Supervision of ``playwright-cli show``: loopback bind, health, lifecycle."""

from __future__ import annotations

import contextlib
import http.server
import inspect
import io
import os
import socket
import threading
import time
from collections.abc import Iterator

import pytest

from kiro_crew import platform_compat
from kiro_crew.browser_cli import view as mod

_REAL_STRUCTURAL_BLINDNESS_PROBE = mod._structurally_blind_listener_attribution
_REAL_PROCESS_OWNS_LOOPBACK_LISTENER = platform_compat.process_owns_loopback_listener


class FakeProc:
    """Stand-in for the supervised child; never touches a real process."""

    def __init__(self, alive: bool = True, pid: int = 424242, stdout: bytes = b"") -> None:
        self.pid = pid
        self._alive = alive
        self.returncode: int | None = None if alive else 1
        self.killed = False
        self.stdout = io.BytesIO(stdout)
        proof = mod._BindingProof(port=0, reported=threading.Event())
        proof.root_identity = platform_compat.ProcessDescendantIdentity(
            pid,
            0,
            str(pid),
            platform_compat.ProcessIdentitySource.ATOMIC,
        )
        setattr(proof, "cli_version", "0.1.99")
        setattr(self, "_kirocrew_browser_view_binding", proof)

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
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        lambda: False,
        raising=False,
    )
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: [cli] if cli else None)
    monkeypatch.setattr(mod, "installed_cli_version", lambda command=None: "0.1.99")
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )
    # Ownership lookups are undecidable by default, so no test shells out to
    # lsof/netstat by accident. `_port_owner` then answers UNPROVEN, which is the
    # pre-identity behaviour every existing test was written against; the tests
    # that exercise ownership opt in with `_stub_port_owner`.
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
    # A bare FakeProc stands for a real child that owns its listener. Tests for
    # blind hosts override these two seams independently.
    monkeypatch.setattr(mod, "_child_reported_binding", lambda proc, port: True, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: True,
        raising=False,
    )
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


def _windows_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)


def _linux_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)


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
    listeners = [
        platform_compat.PortListener(pid=p, address="127.0.0.1", family="4") for p in listener_pids
    ]
    monkeypatch.setattr(platform_compat, "find_port_listeners", lambda port: list(listeners))
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: (list(listeners), True),
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: list(descendants))
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda root_pid, candidate_pids=None: [
            platform_compat.ProcessDescendantIdentity(
                descendant_pid,
                root_pid,
                str(descendant_pid),
            )
            for descendant_pid in descendants
        ],
        raising=False,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: str(pid),
    )


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
    argv = mod._show_argv(["/n/playwright-cli"], 45613)

    assert "--host" in argv
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "45613"
    assert argv[:2] == ["/n/playwright-cli", "show"]


def test_show_argv_never_binds_a_routable_address() -> None:
    """A non-loopback bind would publish remote browser input to the network."""
    argv = mod._show_argv(["/n/playwright-cli"], 45613)

    assert "0.0.0.0" not in argv
    assert "::" not in argv
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


def test_show_argv_preserves_the_direct_node_and_javascript_prefix() -> None:
    command = ["/sealed/gateway-node", "/sealed/playwright-cli.js"]

    argv = mod._show_argv(command, 45613)

    assert argv[:3] == [*command, "show"]
    assert argv[argv.index("--host") + 1] == mod.LOOPBACK_HOST


@pytest.mark.parametrize(
    ("line", "requested_port", "reported_port"),
    [
        pytest.param(
            b"Listening on http://127.0.0.1:45613\n",
            45613,
            45613,
            id="captured-fixed-port",
        ),
        pytest.param(
            b"Listening on http://127.0.0.1:42963\n",
            0,
            42963,
            id="captured-port-zero",
        ),
        pytest.param(
            b"[playwright] Listening at: http://127.0.0.1:45613/dashboard\n",
            45613,
            45613,
            id="compatible-prefix-and-path",
        ),
        pytest.param(
            b"Browser view is listening on http://127.0.0.1:45613/\n",
            45613,
            45613,
            id="compatible-wording",
        ),
    ],
)
def test_binding_report_extracts_the_spawned_loopback_address(
    line: bytes,
    requested_port: int,
    reported_port: int,
) -> None:
    assert mod._binding_reported_port_on_line(line, requested_port) == reported_port


@pytest.mark.parametrize(
    "line",
    [
        b"Listening on http://127.0.0.1:45614\n",
        b"Listening on http://localhost:45613\n",
        b"Listening on ftp://127.0.0.1:45613\n",
        b"Dashboard link http://127.0.0.1:45613\n",
    ],
)
def test_binding_report_rejects_the_wrong_endpoint_or_non_listener_line(line: bytes) -> None:
    assert mod._binding_reported_port_on_line(line, 45613) is None


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

    def fake_spawn(command: list[str], port: int) -> FakeProc:
        recorded.append(mod._show_argv(command, port))
        return FakeProc()

    monkeypatch.setattr(mod, "_spawn", fake_spawn)

    info = mod.ensure_running()

    assert info is not None
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert info.url == f"http://127.0.0.1:{info.port}"
    assert argv[argv.index("--port") + 1] == str(info.port)


def test_ensure_running_spawns_the_direct_node_and_javascript_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    direct = ["/sealed/gateway-node", "/sealed/playwright-cli.js"]
    recorded: list[list[str]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/sealed/playwright-cli")
    monkeypatch.setattr(mod, "cli_command", lambda cli=None: direct)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_spawn",
        lambda command, port: recorded.append(list(command)) or FakeProc(),
    )

    info = mod.ensure_running()

    assert info is not None
    assert recorded == [direct]


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

    def fake_spawn(command: list[str], port: int) -> FakeProc:
        recorded.append(mod._show_argv(command, port))
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
    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": None,
    }


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


@pytest.mark.parametrize("failure", ["health", "ownership"])
def test_definitive_live_child_failure_is_reaped_and_respawned(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
    failure: str,
) -> None:
    _linux_shape(monkeypatch)
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    ports = iter((45613, 45614))
    failed_health_ports: set[int] = set()
    ownership = {4242: True, 4343: True}
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_free_port", lambda: next(ports))
    monkeypatch.setattr(mod, "_healthy", lambda port: port not in failed_health_ports)
    monkeypatch.setattr(
        mod, "_spawn", lambda cli, port: spawned.append(children.pop(0)) or spawned[-1]
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: ownership[pid],
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)

    first = mod.ensure_running()
    assert first is not None
    if failure == "health":
        failed_health_ports.add(first.port)
    else:
        ownership[4242] = False

    second = mod.ensure_running()

    assert second is not None
    assert second.port == 45614
    assert [child.pid for child in spawned] == [4242, 4343]
    assert 4242 in reset_state


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
    assert "cli_version" not in st


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
    assert "cli_version" not in st


def test_status_does_not_start_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    spawns: list[int] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawns.append(port) or FakeProc())
    monkeypatch.setattr(
        mod,
        "installed_cli_version",
        lambda command=None: pytest.fail("status must not read the CLI version"),
    )

    result = mod.status()

    assert result["status"] == "stopped"
    assert "cli_version" not in result
    assert spawns == []


def test_structurally_blind_status_without_start_allowance_names_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    mod._proc = proc
    mod._info = mod.ShowInfo("http://127.0.0.1:45613", 45613)
    mod._child_port = 45613
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda child, port, allow_report: (None, False),
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)

    reasons: list[str | None] = []
    for is_windows in (False, True):
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", is_windows)
        reasons.append(mod.status()["reason"])

    assert reasons == [
        "listener ownership cannot be re-proved on this host: "
        "lsof is absent or cannot attribute processes",
        "listener ownership cannot be re-proved on this host: "
        "GetExtendedTcpTable is absent or cannot attribute processes",
    ]
    assert mod._proc is proc


def test_capable_host_rechecks_owner_on_every_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    mod._proc = proc
    mod._info = mod.ShowInfo("http://127.0.0.1:45613", 45613)
    mod._child_port = 45613
    checks: list[tuple[FakeProc, int, bool]] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)
    monkeypatch.setattr(
        mod,
        "_verify_child_listener",
        lambda child, port, allow_report: (
            checks.append((child, port, allow_report)) or False,
            False,
        ),
    )
    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": "Couldn't confirm the browser view still owns its port",
    }
    assert checks == [(proc, 45613, False)]


# ── port ownership: reachability is not identity ────────────────────────────
#
# `_free_port` releases its probe socket before the child binds it, so a local
# process can take the number in between. `_healthy` then answers True for the
# squatter exactly as it would for our child, and the panel frames whatever the
# squatter serves — with input forwarding attached.


def test_port_owner_proves_the_direct_child(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


def test_port_owner_proves_a_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI spawns Node and helpers, so the listener is often not the child."""
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(9931,), descendants=(9931,))
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == 9931,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


@pytest.mark.parametrize(
    ("lstart_recheck", "expected"),
    [
        pytest.param(
            "Mon Jan  1 00:00:01 2024",
            mod._OWNER_CHILD,
            id="same-lstart-source",
        ),
        pytest.param(None, mod._OWNER_UNPROVEN, id="atomic-source-only"),
    ],
)
def test_posix_fallback_identity_rechecks_with_its_capture_source(
    monkeypatch: pytest.MonkeyPatch,
    lstart_recheck: str | None,
    expected: str,
) -> None:
    proc = FakeProc(pid=10)
    root_lstart = "Mon Jan  1 00:00:00 2024"
    child_lstart = "Mon Jan  1 00:00:01 2024"
    proof = getattr(proc, "_kirocrew_browser_view_binding")
    proof.root_identity = platform_compat.ProcessDescendantIdentity(
        proc.pid,
        0,
        root_lstart,
        platform_compat.ProcessIdentitySource.LSTART,
    )
    listener = platform_compat.PortListener(11, "127.0.0.1", "4")
    snapshot = {
        10: platform_compat._PosixProcessSnapshotRow(1, root_lstart),
        11: platform_compat._PosixProcessSnapshotRow(10, child_lstart),
    }
    sources: list[object] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(platform_compat, "_posix_process_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else "atomic-child-start",
    )

    def _source_recheck(pid: int, source: object) -> str | None:
        sources.append(source)
        return root_lstart if pid == proc.pid else lstart_recheck

    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        _source_recheck,
        raising=False,
    )

    assert mod._port_owner(45613, proc) == expected
    assert sources
    assert {getattr(source, "value", None) for source in sources} == {"lstart"}


def test_owner_disappearing_during_identity_walk_is_inconclusive_and_not_reaped(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    owner_pid = 9931
    listener = platform_compat.PortListener(owner_pid, "127.0.0.1", "4")
    owner_starts = iter((str(owner_pid), None))
    verdicts: list[str] = []
    _linux_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else next(owner_starts, None),
    )
    monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: None)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "cli_path", lambda: None)
    real_port_owner = mod._port_owner

    def _record_port_owner(port: int, child: FakeProc) -> str:
        verdict = real_port_owner(port, child)
        verdicts.append(verdict)
        return verdict

    monkeypatch.setattr(mod, "_port_owner", _record_port_owner)
    mod._proc = proc
    mod._info = mod.ShowInfo("http://127.0.0.1:45613", 45613)
    mod._child_port = 45613

    assert mod.ensure_running() is None
    assert verdicts == [mod._OWNER_UNPROVEN]
    assert mod._proc is proc
    assert reset_state == []


def test_windows_recycled_root_cannot_authorize_its_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    listener = platform_compat.PortListener(4242, "127.0.0.1", "4")
    _windows_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: "recycled")

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_windows_descendant_recycled_before_confirming_probe_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    identity = platform_compat.ProcessDescendantIdentity(9931, 4242, "old")
    starts = iter(("old", "new"))
    listener = platform_compat.PortListener(9931, "127.0.0.1", "4")
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([listener], True),
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid: [9931])
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [identity],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else next(starts, "new"),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (None, False)


@pytest.mark.parametrize(
    ("second_map", "expected"),
    [
        ({10: 1, 11: 10, 13: 10}, mod._OWNER_CHILD),
        ({10: 1, 11: 12, 12: 10}, mod._OWNER_UNPROVEN),
    ],
    ids=["sibling-churn", "listener-chain-changed"],
)
def test_windows_owner_stability_is_scoped_to_the_listener_chain(
    monkeypatch: pytest.MonkeyPatch,
    second_map: dict[int, int],
    expected: str,
) -> None:
    proc = FakeProc(pid=10)
    first_map = {10: 1, 11: 10, 12: 10}
    maps = iter((first_map, first_map, second_map))
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == 11,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )
    monkeypatch.setattr(platform_compat, "_windows_process_parent_map", lambda: next(maps))
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )

    assert mod._port_owner(45613, proc) == expected


def test_owner_child_proof_is_rechecked_before_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    proof = platform_compat.ProcessDescendantIdentity(9931, 4242, "old")
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_CHILD)
    monkeypatch.setattr(
        mod,
        "_take_port_owner_identity_proof",
        lambda child, port: proof,
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid) if pid == proc.pid else "new",
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (False, False)


def test_port_owner_names_a_squatter(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(777,), descendants=(9931,))
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_FOREIGN


def test_port_owner_is_unproven_without_the_lookup_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one fail-open branch: a host without lsof must keep its panel.

    Static per host rather than per start, and not attacker-controllable --
    removing the tool needs the access that makes this panel moot.
    """
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    assert mod._port_owner(45613, proc) == mod._OWNER_UNPROVEN


def test_port_owner_treats_one_failed_probe_as_transient_when_control_is_functional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    probes: list[tuple[int, int]] = []
    _windows_shape(monkeypatch)

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owns_listener(pid: int, port: int) -> bool | None:
        probes.append((pid, port))
        if (pid, port) == (proc.pid, target_port):
            return None
        if (pid, port) == (os.getpid(), control_port):
            return True
        pytest.fail(f"unexpected Windows listener probe: pid={pid}, port={port}")

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert probes == [(proc.pid, target_port), (os.getpid(), control_port)]


def test_windows_port_owner_never_invokes_netstat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: True,
    )

    assert mod._port_owner(45613, proc) == mod._OWNER_CHILD


@pytest.mark.parametrize(
    "target_result",
    [None, False],
    ids=["target-incomplete", "target-empty"],
)
def test_inconclusive_control_probe_never_claims_foreign(
    monkeypatch: pytest.MonkeyPatch,
    target_result: bool | None,
) -> None:
    target_port = 45613
    control_port = 45614
    proc = FakeProc(pid=4242)
    probes: list[tuple[int, int]] = []
    _windows_shape(monkeypatch)

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owns_listener(pid: int, port: int) -> bool | None:
        probes.append((pid, port))
        if (pid, port) == (proc.pid, target_port):
            return target_result
        if (pid, port) == (os.getpid(), control_port):
            return None
        pytest.fail(f"unexpected Windows listener probe: pid={pid}, port={port}")

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )
    monkeypatch.setattr(
        platform_compat,
        "listening_pid_tool_available",
        lambda: pytest.fail("Windows ownership must not resolve netstat"),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert probes == [(proc.pid, target_port), (os.getpid(), control_port)]


def test_transient_probe_failure_cannot_promote_startup_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_UNPROVEN)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: None,
    )
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: False)

    assert mod._verify_child_listener(proc, 45613, allow_report=True) == (None, False)


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        pytest.param(True, True, id="own-control-pid"),
        pytest.param(False, False, id="completed-without-own-pid"),
        pytest.param(None, None, id="table-failure"),
    ],
)
def test_windows_listener_self_test_requires_own_control_pid(
    monkeypatch: pytest.MonkeyPatch,
    observed: bool | None,
    expected: bool | None,
) -> None:
    control_port = 45614
    probes: list[tuple[int, int]] = []

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    _windows_shape(monkeypatch)
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: probes.append((pid, port)) or observed,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows self-test must not invoke netstat"),
    )

    assert mod._listener_lookup_functional() is expected
    assert probes == [(os.getpid(), control_port)]


def test_windows_inconclusive_self_test_is_not_blind_or_adoptable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        _REAL_STRUCTURAL_BLINDNESS_PROBE,
    )
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: None)
    monkeypatch.setattr(mod, "_port_owner", lambda port, child: mod._OWNER_UNPROVEN)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: None,
    )

    structurally_blind = mod._structurally_blind_listener_attribution()
    verification = mod._verify_child_listener(proc, 45613, allow_report=True)

    assert structurally_blind is False
    assert verification == (None, False)


def test_two_status_calls_run_one_self_test_after_the_target_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_port = 45613
    control_port = 45614
    claims: list[int] = []
    probes: list[int] = []

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "_proc", proc)
    monkeypatch.setattr(mod, "_info", mod.ShowInfo(f"http://127.0.0.1:{child_port}", child_port))
    monkeypatch.setattr(mod, "_child_port", child_port)
    monkeypatch.setattr(
        mod,
        "_structurally_blind_listener_attribution",
        _REAL_STRUCTURAL_BLINDNESS_PROBE,
    )
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: "/usr/bin/lsof" if name == "lsof" else None,
    )
    monkeypatch.setattr(
        mod,
        "_claim_listener",
        lambda port: claims.append(port) or _ControlListener(),
    )

    def _probe(port: int) -> tuple[list[platform_compat.PortListener], bool]:
        probes.append(port)
        return [], True

    monkeypatch.setattr(platform_compat, "probe_port_listeners", _probe)
    monkeypatch.setattr(
        platform_compat, "process_descendant_identities", lambda pid, candidate_pids=None: []
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    mod.status()
    mod.status()

    assert claims == [0]
    assert probes == [child_port, control_port, child_port]


def test_listener_self_test_cache_invalidates_when_the_tool_path_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_path = ["/usr/bin/lsof"]
    control_ports = iter((45614, 45615))
    claims: list[int] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)

    class _ControlListener:
        def __init__(self, port: int) -> None:
            self.port = port

        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, self.port)

        def close(self) -> None:
            pass

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: tool_path[0] if name == "lsof" else None,
    )
    monkeypatch.setattr(
        mod,
        "_claim_listener",
        lambda port: claims.append(port) or _ControlListener(next(control_ports)),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: (
            [platform_compat.PortListener(os.getpid(), "127.0.0.1", "4")],
            True,
        ),
    )

    assert mod._listener_lookup_functional() is True
    assert mod._listener_lookup_functional() is True
    tool_path[0] = "/opt/tools/lsof"
    assert mod._listener_lookup_functional() is True

    assert claims == [0, 0]


def test_blind_lookup_squatter_is_not_adopted_without_child_binding_proof(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    """A squatter cannot receive the host-scoped browser cookie through the view URL."""
    proc = FakeProc(pid=4242)
    clock = _FakeClock()
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "time", clock)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(platform_compat, "find_port_listeners", lambda port: [])
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([], True),
        raising=False,
    )

    assert mod.ensure_running() is None
    assert mod._info is None
    assert proc.pid in reset_state
    assert mod.status()["url"] is None


def test_blind_lookup_adopts_real_child_after_recognized_binding_report(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recognized URL from the trusted child's stdout is positive proof."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True, raising=False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(platform_compat, "find_port_listeners", lambda port: [])
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: ([], True),
        raising=False,
    )

    with caplog.at_level("WARNING"):
        info = mod.ensure_running()

    assert info is not None
    assert proc.pid not in reset_state
    assert any("reported that it bound" in r.message for r in caplog.records)


def test_port_owner_refuses_when_lookup_attributes_control_but_not_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A functional Windows table seeing no target owner still means foreign."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owners(port: int) -> set[int]:
        looked_up.append(port)
        return set() if port == target_port else {os.getpid()}

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        _owners,
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_FOREIGN
    assert looked_up == [target_port, control_port]


def test_port_owner_preserves_when_control_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Windows control-table read is not completed non-ownership."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    control_port = 45614
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    def _owners(port: int) -> set[int] | None:
        looked_up.append(port)
        return set() if port == target_port else None

    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        _owners,
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert looked_up == [target_port, control_port]


def test_port_owner_preserves_when_control_listener_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Windows control listener means table capability stays unproven."""
    proc = FakeProc(pid=4242)
    target_port = 45613
    looked_up: list[int] = []
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_claim_listener", lambda port: None)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: looked_up.append(port) or set(),
    )

    assert mod._port_owner(target_port, proc) == mod._OWNER_UNPROVEN
    assert looked_up == [target_port]


def test_port_owner_refuses_without_a_child_to_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recorded child means nothing can be proved ours."""
    _stub_port_owner(monkeypatch, listener_pids=(4242,))

    assert mod._port_owner(45613, None) == mod._OWNER_FOREIGN


def test_ensure_running_refuses_a_squatter_on_the_child_port(
    monkeypatch: pytest.MonkeyPatch, reset_state: list[int]
) -> None:
    """A Windows owner-table row for a non-descendant must remain foreign."""
    proc = FakeProc(pid=4242)
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: {777},
    )

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


def test_ensure_running_adopts_when_tool_absent_but_child_reports_binding(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing PID lookup is safe when the trusted child proves its bind."""
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: FakeProc(pid=4242))
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(777,), tool=False)

    with caplog.at_level("WARNING"):
        assert mod.ensure_running() is not None

    assert any("reported that it bound" in r.message for r in caplog.records)


def test_blind_global_lsof_demotes_pid_scoped_negative_to_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    _stub_port_owner(monkeypatch, listener_pids=(), tool=True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: False)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: False,
    )

    assert mod._verify_child_listener(proc, 45613, allow_report=False) == (None, False)


@pytest.mark.parametrize(
    ("target_owners", "expected"),
    [
        pytest.param({9931}, (True, False), id="descendant-owner"),
        pytest.param({7777}, (False, False), id="foreign-owner"),
        pytest.param(None, (None, False), id="table-failure"),
    ],
)
def test_windows_owner_pid_table_decides_listener_ownership(
    monkeypatch: pytest.MonkeyPatch,
    target_owners: set[int] | None,
    expected: tuple[bool | None, bool],
) -> None:
    target_port = 45613
    control_port = 45614
    proc = FakeProc(pid=4242)
    identity = platform_compat.ProcessDescendantIdentity(9931, proc.pid, "9931")

    class _ControlListener:
        def getsockname(self) -> tuple[str, int]:
            return (mod.LOOPBACK_HOST, control_port)

        def close(self) -> None:
            pass

    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: False)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda *args, **kwargs: pytest.fail("netstat must not be used"),
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: (
            None
            if target_owners is None
            else ({os.getpid()} if port == control_port else target_owners)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [identity],
    )
    monkeypatch.setattr(mod, "_claim_listener", lambda port: _ControlListener())
    monkeypatch.setattr(mod, "_listener_lookup_self_test_cache", None, raising=False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)

    assert mod._verify_child_listener(proc, target_port, allow_report=False) == expected


def _blind_host_child(
    monkeypatch: pytest.MonkeyPatch,
    owns_listener: list[bool | None],
    spawned: list[FakeProc],
) -> FakeProc:
    """Start one child where the global listener lookup cannot attribute owners."""
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: spawned.append(proc) or proc)
    reports = iter((True,))
    monkeypatch.setattr(
        mod,
        "_child_reported_binding",
        lambda child, port: next(reports, False),
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: owns_listener[0],
        raising=False,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)
    return proc


def test_blind_host_accepts_a_listener_owned_by_the_spawned_descendant(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    checked: list[int] = []
    _linux_shape(monkeypatch)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)

    def _owns_listener(pid: int, port: int) -> bool:
        checked.append(pid)
        return pid == 9931

    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _owns_listener,
    )

    assert mod.ensure_running() is not None
    assert checked == [4242, 9931]
    assert reset_state == []


@pytest.mark.parametrize(
    "descendant_reads",
    [("new",), ("old", "new")],
    ids=["before-probe", "after-probe"],
)
def test_recycled_descendant_identity_is_inconclusive_around_listener_probe(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reset_state: list[int],
    descendant_reads: tuple[str, ...],
) -> None:
    proc = FakeProc(pid=4242)
    reads = iter(descendant_reads)
    probed: list[int] = []
    _linux_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: [
            platform_compat.ProcessDescendantIdentity(
                9931,
                4242,
                "old",
                platform_compat.ProcessIdentitySource.LSTART,
            )
        ],
        raising=False,
    )
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: str(pid) if pid == proc.pid else pytest.fail("root-only fallback"),
    )

    def _source_recheck(pid: int, source: platform_compat.ProcessIdentitySource) -> str:
        if pid == proc.pid:
            assert source is platform_compat.ProcessIdentitySource.ATOMIC
            return str(pid)
        assert pid == 9931
        assert source is platform_compat.ProcessIdentitySource.LSTART
        return next(reads)

    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        _source_recheck,
    )

    def _owns_listener(pid: int, port: int) -> bool:
        probed.append(pid)
        return pid == 9931

    monkeypatch.setattr(platform_compat, "process_owns_loopback_listener", _owns_listener)
    mod._proc = proc
    mod._info = mod.ShowInfo("http://127.0.0.1:45613", 45613)
    mod._child_port = 45613
    monkeypatch.setattr(mod, "cli_path", lambda: None)

    with caplog.at_level("DEBUG"):
        assert mod.ensure_running() is None

    assert mod._proc is proc
    assert reset_state == []
    assert any(
        "enumerated=(pid=9931, ppid=4242, start='old'), "
        "current=(pid=9931, start='new')" in record.message
        for record in caplog.records
    )
    if len(descendant_reads) == 1:
        assert probed == [4242]
    else:
        assert probed == [4242, 9931]


def test_same_second_descendant_reuse_with_a_different_start_id_is_inconclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    probed: list[int] = []
    captured_start = "1700000000.000001"
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=(9931,), tool=False)
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: False)
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid: [platform_compat.ProcessDescendantIdentity(9931, 4242, captured_start)],
    )
    monkeypatch.setattr(
        platform_compat,
        "process_start_time",
        lambda pid: captured_start,
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: "1700000000.000002" if pid == 9931 else str(pid),
    )

    def _owns_listener(pid: int, port: int) -> bool:
        probed.append(pid)
        return pid == 9931

    monkeypatch.setattr(platform_compat, "process_owns_loopback_listener", _owns_listener)

    result = mod._verify_child_listener(proc, 45613, allow_report=False)

    assert result == (None, False)
    assert probed == [4242]


def test_root_identity_inconclusive_preserves_but_mismatch_reaps(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    starts: dict[int, str | None] = {4242: "4242", 4343: "4343"}
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_process_start_identity", lambda pid: starts[pid])
    monkeypatch.setattr(
        platform_compat,
        "process_start_id_for_source",
        lambda pid, source: starts[pid],
    )

    def _spawn(command: list[str], port: int) -> FakeProc:
        child = children.pop(0)
        spawned.append(child)
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn)

    first = mod.ensure_running()
    assert first is not None
    starts[4242] = None

    assert mod.status() == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": mod._OWNERSHIP_REASON,
    }
    assert mod.ensure_running() is None
    assert mod._proc is spawned[0]
    assert mod._info == first
    assert reset_state == []

    starts[4242] = "recycled"

    assert mod.ensure_running() is not None
    assert [child.pid for child in spawned] == [4242, 4343]
    assert mod._proc is spawned[1]
    assert reset_state == [4242]


def test_blind_port_zero_banner_authorizes_the_resolved_startup_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(pid=4242)
    proof = getattr(proc, "_kirocrew_browser_view_binding")
    proof.record(42963)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_spawn", lambda command, port: proc)
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_child_reported_binding",
        lambda child, port: mod._child_reported_port(child, port) is not None,
    )
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)
    monkeypatch.setattr(mod, "time", _FakeClock())

    info = mod.ensure_running()

    assert proof.port == 0
    assert proof.reported_port() == 42963
    assert info == mod.ShowInfo("http://127.0.0.1:42963", 42963)
    assert mod._proc is proc


def test_structurally_blind_start_publishes_once_then_withholds_a_squatter(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    spawn_ports: list[int] = []
    listener_owner = {"pid": proc.pid}
    reason = (
        "listener ownership cannot be re-proved on this host: "
        "lsof is absent or cannot attribute processes"
    )
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(
        mod,
        "_spawn",
        lambda command, port: spawn_ports.append(port) or proc,
    )
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: 45613,
        raising=False,
    )

    def _listener_verdict(
        child: FakeProc, port: int, *, allow_report: bool
    ) -> tuple[bool | None, bool]:
        if allow_report and listener_owner["pid"] == child.pid:
            return True, True
        return None, False

    monkeypatch.setattr(mod, "_verify_child_listener", _listener_verdict)

    first = mod.ensure_running()
    listener_owner["pid"] = 777
    later = mod.status()

    assert first == mod.ShowInfo("http://127.0.0.1:45613", 45613)
    assert later == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": reason,
    }
    assert mod.ensure_running() is None
    assert spawn_ports == [0]
    assert mod._proc is proc
    assert mod._info == first
    assert reset_state == []


def test_structurally_blind_dead_child_respawns_from_a_fresh_report(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    reported_by_pid = {4242: 45613, 4343: 45614}
    spawned: list[FakeProc] = []
    spawn_ports: list[int] = []
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)

    def _spawn(command: list[str], port: int) -> FakeProc:
        spawn_ports.append(port)
        child = children.pop(0)
        spawned.append(child)
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn)
    monkeypatch.setattr(
        mod,
        "_child_reported_port",
        lambda child, requested_port: reported_by_pid.pop(child.pid, None),
        raising=False,
    )
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: None,
    )
    _stub_port_owner(monkeypatch, listener_pids=(), tool=False)

    first = mod.ensure_running()
    assert first == mod.ShowInfo("http://127.0.0.1:45613", 45613)
    spawned[0]._alive = False
    spawned[0].returncode = 1

    second = mod.ensure_running()
    status = mod.status()

    assert second == mod.ShowInfo("http://127.0.0.1:45614", 45614)
    assert status == {
        "status": "stopped",
        "url": None,
        "port": None,
        "reason": (
            "listener ownership cannot be re-proved on this host: "
            "GetExtendedTcpTable is absent or cannot attribute processes"
        ),
    }
    assert spawn_ports == [0, 0]
    assert [child.pid for child in spawned] == [4242, 4343]
    assert mod._proc is spawned[1]
    assert mod._info == second
    assert reset_state == [4242]


def test_replacement_spawn_invalidates_listener_self_test_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    invalidations: list[None] = []
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda command, port: children.pop(0))
    monkeypatch.setattr(
        mod,
        "_invalidate_listener_lookup_self_test_cache",
        lambda: invalidations.append(None),
        raising=False,
    )

    assert mod.ensure_running() is not None
    assert mod._proc is not None
    mod._proc._alive = False
    mod._proc.returncode = 1
    assert mod.ensure_running() is not None

    assert invalidations == [None, None]


def test_transient_listener_probe_failure_still_degrades_without_reaping(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(platform_compat, "IS_LINUX", True)
    monkeypatch.setattr(platform_compat, "IS_POSIX", True)
    owns_listener: list[bool | None] = [True]
    spawned: list[FakeProc] = []
    proc = _blind_host_child(monkeypatch, owns_listener, spawned)
    reports = iter((True, False, False))
    monkeypatch.setattr(mod, "_child_reported_binding", lambda child, port: next(reports))

    assert mod.ensure_running() is not None
    monkeypatch.setattr(platform_compat, "listening_pid_tool_available", lambda: True)
    monkeypatch.setattr(platform_compat, "listening_pid_tool", lambda: "lsof")
    monkeypatch.setattr(
        platform_compat,
        "trusted_system_bin",
        lambda name: "/usr/sbin/lsof" if name == "lsof" else None,
    )
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: None)
    owns_listener[0] = None
    assert mod.ensure_running() is None
    status = mod.status()
    assert status["url"] is None
    assert status["reason"] == (
        "lsof at /usr/sbin/lsof did not attribute the gateway's own control "
        "listener; check its permissions/namespace"
    )
    assert mod._proc is proc
    assert spawned == [proc]
    assert reset_state == []


def test_every_view_thread_start_uses_one_guard() -> None:
    source = inspect.getsource(mod)
    guard = inspect.getsource(mod._start_daemon_thread)

    assert source.count(".start()") == guard.count(".start()") == 1


def test_relay_thread_start_failure_reaps_spawned_child(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    public_port = mod._free_port()
    real_start = threading.Thread.start
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_structurally_blind_listener_attribution", lambda: True)
    monkeypatch.setattr(mod, "_spawn", lambda command, port: proc)
    monkeypatch.setattr(mod, "_child_reported_port", lambda child, port: 45613)
    monkeypatch.setattr(
        mod,
        "_healthy",
        lambda port: pytest.fail("relay failure must stop before health probing"),
    )

    def _fail_relay_start(thread: threading.Thread) -> None:
        if thread.name == "browser-view-relay":
            raise RuntimeError("thread capacity exhausted")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", _fail_relay_start)

    assert mod.ensure_running(public_port) is None
    assert mod.status()["reason"] == "Couldn't start the browser view's connection relay"
    assert proc.stdout.closed
    assert proc.pid in reset_state
    assert not proc._alive


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param("linux-proc", id="linux-proc"),
        pytest.param("windows-table", id="windows-table"),
        pytest.param("ps-fallback", id="ps-fallback"),
    ],
)
def test_spawn_captures_root_start_identity(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    proc = FakeProc(pid=4242)
    source_reads: list[str] = []
    expected_start = {
        "linux-proc": "linux-start",
        "windows-table": "windows-start",
        "ps-fallback": "ps-start",
    }[shape]
    expected_source = {
        "linux-proc": platform_compat.ProcessIdentitySource.ATOMIC,
        "windows-table": platform_compat.ProcessIdentitySource.WINDOWS,
        "ps-fallback": platform_compat.ProcessIdentitySource.LSTART,
    }[shape]
    expected_capture_reads = {
        "linux-proc": ["atomic"],
        "windows-table": ["windows"],
        "ps-fallback": ["atomic", "lstart"],
    }[shape]
    expected_recheck_reads = {
        "linux-proc": ["atomic"],
        "windows-table": ["windows"],
        "ps-fallback": ["lstart"],
    }[shape]
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", shape == "windows-table")
    monkeypatch.setattr(platform_compat, "IS_POSIX", shape != "windows-table")
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod, "ui_socket_env", lambda env: {})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: proc)

    def _atomic(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("atomic")
        return "linux-start" if shape == "linux-proc" else None

    def _windows(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("windows")
        return "windows-start" if shape == "windows-table" else None

    def _lstart(pid: int) -> str | None:
        assert pid == proc.pid
        source_reads.append("lstart")
        return "ps-start" if shape == "ps-fallback" else None

    monkeypatch.setattr(platform_compat, "get_process_start_id", _atomic)
    monkeypatch.setattr(platform_compat, "process_start_time", _windows)
    monkeypatch.setattr(platform_compat, "_process_lstart", _lstart)
    monkeypatch.setattr(mod, "_start_daemon_thread", lambda thread: True)

    assert mod._spawn(["/n/pw"], 45613) is proc
    proof = getattr(proc, "_kirocrew_browser_view_binding")
    assert proof.root_identity == platform_compat.ProcessDescendantIdentity(
        proc.pid,
        0,
        expected_start,
        expected_source,
    )
    assert source_reads == expected_capture_reads

    source_reads.clear()

    assert mod._root_process_identity_matches(proc, "test") is True
    assert source_reads == expected_recheck_reads


def test_spawn_reaps_child_when_proof_reader_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    proc = FakeProc(pid=4242)
    monkeypatch.setattr(mod, "cli_env", lambda: {})
    monkeypatch.setattr(mod, "ui_socket_env", lambda env: {})
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *args, **kwargs: proc)

    def _refuse_start(thread: threading.Thread) -> None:
        raise RuntimeError("thread capacity exhausted")

    monkeypatch.setattr(threading.Thread, "start", _refuse_start)

    assert mod._spawn(["/n/pw"], 45613) is None
    assert proc.stdout.closed
    assert proc.pid in reset_state
    assert not proc._alive


def test_binding_reader_discards_after_proof_budget_until_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chatty child cannot block after the bounded proof window closes."""

    class NeverEof(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.stop = threading.Event()
            self.budget_reached = threading.Event()
            self.reads = 0

        def read(self, size: int = -1) -> bytes:
            self.reads += 1
            if self.reads >= 4:
                self.budget_reached.set()
            return b"" if self.stop.is_set() else b"not the binding report\n"

    stream = NeverEof()
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_LINES", 4)
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 4096)
    monkeypatch.setattr(mod, "_BINDING_PROOF_TIMEOUT_S", 1.0)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    reader.start()
    try:
        assert stream.budget_reached.wait(timeout=1)
        reader.join(timeout=0.05)
        discarding_after_budget = reader.is_alive()
        assert discarding_after_budget
    finally:
        stream.stop.set()
        reader.join(timeout=1)

    assert not reader.is_alive()
    assert not proof.reported.is_set()


def test_binding_reader_drops_proof_at_the_byte_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"Listening on http://127.0.0.1:45613\n")
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 8)

    mod._drain_child_output(stream, 45613, proof)

    assert not proof.reported.is_set()


def test_binding_reader_retains_a_matched_banner_after_later_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ChunkedBytesIO(io.BytesIO):
        def __init__(self, chunks: tuple[bytes, ...]) -> None:
            super().__init__()
            self._chunks = iter(chunks)

        def read(self, size: int = -1) -> bytes:
            return next(self._chunks, b"")

    banner = b"Listening on http://127.0.0.1:45613\n"
    stream = ChunkedBytesIO((banner, b"overflow", b""))
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", len(banner) + 4)

    mod._drain_child_output(stream, 45613, proof)

    assert proof.reported.is_set()


def _assert_binding_reader_drains_real_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    payload = b"x" * (1024 * 1024)
    errors: list[OSError] = []
    monkeypatch.setattr(mod, "_BINDING_PROOF_MAX_BYTES", 32)

    def _write_payload() -> None:
        remaining = memoryview(payload)
        try:
            while remaining:
                written = os.write(write_fd, remaining)
                remaining = remaining[written:]
        except OSError as exc:
            errors.append(exc)
        finally:
            os.close(write_fd)

    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    writer = threading.Thread(target=_write_payload, daemon=True)
    try:
        reader.start()
        writer.start()
        writer.join(timeout=2)
        write_completed = not writer.is_alive()
    finally:
        stream.close()
        writer.join(timeout=1)
        reader.join(timeout=1)

    assert write_completed, "the child writer blocked after the proof reader exited"
    assert errors == []
    assert not reader.is_alive()
    assert not proof.reported.is_set()


def test_binding_reader_drains_a_real_pipe_after_the_proof_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_binding_reader_drains_real_pipe(monkeypatch)


def test_binding_reader_owns_a_duplicate_across_original_fd_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read, original_write = os.pipe()
    stream = os.fdopen(original_read, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    read_started = threading.Event()
    allow_read = threading.Event()
    read_fds: list[int] = []
    closed_fds: list[int] = []
    real_read = os.read
    real_write = os.write
    real_close = os.close

    def _tracked_read(fd: int, size: int) -> bytes:
        read_fds.append(fd)
        read_started.set()
        assert allow_read.wait(timeout=1)
        return real_read(fd, size)

    def _tracked_close(fd: int) -> None:
        closed_fds.append(fd)
        real_close(fd)

    monkeypatch.setattr(mod.os, "read", _tracked_read)
    monkeypatch.setattr(mod.os, "close", _tracked_close)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    foreign_read = -1
    foreign_write = -1
    try:
        reader.start()
        assert read_started.wait(timeout=1)
        stream.close()
        foreign_read, foreign_write = os.pipe()
        if foreign_read != original_read:
            os.dup2(foreign_read, original_read)
            real_close(foreign_read)
            foreign_read = original_read
        real_write(original_write, b"Listening on http://127.0.0.1:45613\n")
        real_close(original_write)
        original_write = -1
        real_write(foreign_write, b"Listening on http://127.0.0.1:49999\n")
        real_close(foreign_write)
        foreign_write = -1
        allow_read.set()
        reader.join(timeout=1)
    finally:
        allow_read.set()
        reader.join(timeout=1)
        for fd in (original_write, foreign_write, foreign_read):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    real_close(fd)

    assert not reader.is_alive()
    assert proof.reported_port() == 45613
    assert read_fds
    assert set(read_fds) != {original_read}
    assert closed_fds == [read_fds[0]]


def test_binding_reader_falls_back_to_blocking_drain_when_nonblocking_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_nonblocking(fd: int, blocking: bool) -> None:
        raise OSError("nonblocking pipe reads unavailable")

    monkeypatch.setattr(mod.os, "set_blocking", _refuse_nonblocking)

    _assert_binding_reader_drains_real_pipe(monkeypatch)


def test_binding_reader_accepts_a_tolerant_listener_banner() -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"[playwright] Browser view is listening at http://127.0.0.1:45613/\n")

    mod._drain_child_output(stream, 45613, proof)

    assert proof.reported.is_set()


def test_binding_reader_logs_when_stdout_has_no_listener_banner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    stream = io.BytesIO(b"Dashboard ready; no listener URL was reported\n")

    with caplog.at_level("WARNING"):
        mod._drain_child_output(stream, 45613, proof, cli_version="0.1.99")

    assert not proof.reported.is_set()
    assert any(
        "playwright-cli 0.1.99" in r.message and "Listening on http://127.0.0.1:45613" in r.message
        for r in caplog.records
    )


def test_binding_reader_switches_to_drain_at_the_time_budget_with_a_silent_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb", buffering=0)
    proof = mod._BindingProof(port=45613, reported=threading.Event())
    monkeypatch.setattr(mod, "_BINDING_PROOF_TIMEOUT_S", 0.05)
    reader = threading.Thread(
        target=mod._drain_child_output,
        args=(stream, 45613, proof),
        daemon=True,
    )
    try:
        reader.start()
        reader.join(timeout=0.2)
        assert reader.is_alive(), "the reader exited instead of draining after the proof timeout"
        assert not proof.reported.is_set()
    finally:
        os.close(write_fd)
        reader.join(timeout=1)
        stream.close()
    assert not reader.is_alive()


def test_reuse_replaces_a_live_child_after_global_squatter_proof(
    monkeypatch: pytest.MonkeyPatch,
    reset_state: list[int],
) -> None:
    children = [FakeProc(pid=4242), FakeProc(pid=4343)]
    spawned: list[FakeProc] = []
    listener_owner = {"pid": 0}
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        lambda pid, port: pid == listener_owner["pid"],
    )
    monkeypatch.setattr(platform_compat, "process_descendants", lambda pid, candidate_pids=None: [])
    monkeypatch.setattr(
        platform_compat,
        "process_descendant_identities",
        lambda pid, candidate_pids=None: [],
    )
    monkeypatch.setattr(
        platform_compat,
        "get_process_start_id",
        lambda pid: str(pid),
    )
    monkeypatch.setattr(
        platform_compat,
        "probe_port_listeners",
        lambda port: pytest.fail("Windows ownership must not invoke netstat"),
    )

    def _spawn_fresh(cli: str, port: int) -> FakeProc:
        child = children.pop(0)
        spawned.append(child)
        listener_owner["pid"] = child.pid
        return child

    monkeypatch.setattr(mod, "_spawn", _spawn_fresh)
    first = mod.ensure_running()
    assert first is not None

    listener_owner["pid"] = 777

    assert mod.ensure_running() is not None
    assert [child.pid for child in spawned] == [4242, 4343]
    assert 4242 in reset_state


def test_status_does_not_report_a_squatter_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`status` withholds a URL after Windows ownership moves to a squatter."""
    proc = FakeProc(pid=4242)
    listener_owner = {4242}
    _windows_shape(monkeypatch)
    _stub_port_owner(monkeypatch, listener_pids=(), descendants=())
    monkeypatch.setattr(mod, "cli_path", lambda: "/n/pw")
    monkeypatch.setattr(mod, "_healthy", lambda port: True)
    monkeypatch.setattr(mod, "_spawn", lambda cli, port: proc)
    monkeypatch.setattr(mod, "_listener_lookup_functional", lambda: True)
    monkeypatch.setattr(
        platform_compat,
        "process_owns_loopback_listener",
        _REAL_PROCESS_OWNS_LOOPBACK_LISTENER,
    )
    monkeypatch.setattr(
        platform_compat,
        "_windows_loopback_listener_owner_pids",
        lambda port: set(listener_owner),
    )
    assert mod.ensure_running() is not None
    assert mod.status()["status"] == "running"

    listener_owner.clear()
    listener_owner.add(777)

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


@pytest.mark.parametrize(
    ("shape", "expected_source"),
    [
        pytest.param(
            "posix",
            platform_compat.ProcessIdentitySource.ATOMIC,
            id="posix",
        ),
        pytest.param(
            "windows",
            platform_compat.ProcessIdentitySource.WINDOWS,
            id="windows",
        ),
    ],
)
def test_show_child_registers_in_the_gateway_owned_session_registry(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected_source: platform_compat.ProcessIdentitySource,
) -> None:
    """FP item 6: the ``show`` grid still lists the panel sessions.

    The grid (the documented CAPTCHA/2FA takeover surface) lists whatever is in
    the CLI session registry the ``show`` child runs against. The launcher
    registers each ``panel-`` session under the gateway-owned registry that
    :func:`ui_socket_env` pins (``<root>/ui/d``); this test proves ``_spawn``
    hands the ``show`` child that SAME env, so the grid and the launched
    sessions share one registry and the grid lists what a launch created.
    """
    if shape == "windows":
        _windows_shape(monkeypatch)
    else:
        _linux_shape(monkeypatch)
    monkeypatch.setattr(platform_compat, "process_start_time", lambda pid: str(pid))
    captured: dict[str, dict[str, str]] = {}

    def fake_popen(argv, **kwargs):  # noqa: ANN001, ANN003
        captured["env"] = dict(kwargs["env"])
        assert kwargs["stdout"] is mod.subprocess.PIPE
        return FakeProc(stdout=b"Listening on http://127.0.0.1:7777\n")

    monkeypatch.setattr(mod, "cli_env", lambda: {"PATH": "/n"})
    monkeypatch.setattr(
        mod,
        "ui_socket_env",
        lambda env: {"PWTEST_SOCKETS_DIR": "/root/ui/s", "PWTEST_DAEMON_SESSION_DIR": "/root/ui/d"},
    )
    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

    proc = mod._spawn(["/n/pw"], 7777)

    assert proc is not None
    proof = getattr(proc, "_kirocrew_browser_view_binding")
    assert proof.reported.wait(timeout=1)
    assert proof.port == 7777
    assert proof.root_identity.source is expected_source
    assert captured["env"]["PWTEST_SOCKETS_DIR"] == "/root/ui/s"
    assert captured["env"]["PWTEST_DAEMON_SESSION_DIR"] == "/root/ui/d"
