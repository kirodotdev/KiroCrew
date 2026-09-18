"""Tests for the stub bridge liveness monitor (ping-while-outstanding).

Covers:
* A silent peer (accepts, never answers) causes the stub to emit a JSON-RPC
  error and report ``peer_dead`` on the session -- not park forever.
* A legitimately slow call (peer still answers pings) is NOT killed.
* The ping RATE stays one per interval against a peer that answers instantly,
  rather than one per round-trip.
* A missed pong still declares the peer dead inside the advertised grace, so
  bounding the rate did not lengthen detection.
* Normal bridge teardown (stdin EOF, or a stop) is reported as itself, so a
  reconnect is never attempted on a shutdown.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiro_crew.mcp_gateway.stub import (
    _BRIDGE_PING_TYPE,
    _BRIDGE_PONG_TYPE,
    StubSession,
    run_bridge,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _strip_comments(source: str) -> str:
    """``source`` with every comment removed, for source-text ratchets.

    A ratchet that greps raw text cannot tell a call from a comment explaining
    why that call is absent, so a well-documented invariant trips the very
    assertion documenting it. Tokenizing is exact where a ``#`` split would also
    cut string literals.
    """
    import io
    import tokenize

    kept: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type != tokenize.COMMENT:
                kept.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        # A partial tail need not be valid Python on its own; fall back to a
        # line-wise cut, which is coarser but never blind.
        return "\n".join(ln.split("#", 1)[0] for ln in source.splitlines())
    return "\n".join(kept)


# Pin to one xdist worker (requires --dist loadgroup) alongside the other
# mcp_gateway suites.
pytestmark = pytest.mark.xdist_group("mcp_gateway")


def _jsonrpc_request(method: str = "tools/call", req_id: str = "req-1") -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method})
        + "\n"
    ).encode()


def _jsonrpc_response(req_id: str = "req-1") -> bytes:
    return (
        json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {}})
        + "\n"
    ).encode()


@pytest.mark.asyncio
async def test_silent_peer_triggers_liveness_failure() -> None:
    """A gateway that accepts connections but never replies to pings or
    requests must be reported as ``peer_dead`` within a bounded time,
    not park the stub forever."""
    # Socket-side reader: gateway never sends anything.
    gw_reader = asyncio.StreamReader()
    # Stdin: kiro-cli sends one JSON-RPC request, then goes silent.
    stdin_reader = asyncio.StreamReader()
    stdin_reader.feed_data(_jsonrpc_request("tools/call", "call-42"))
    # Do NOT feed EOF — kiro-cli holds stdin open.

    # Capture what the stub would write to the gateway socket.
    gw_written: list[bytes] = []

    class _FakeWriter:
        """Minimal asyncio.StreamWriter stand-in for the gateway socket."""

        _mc_write_lock = asyncio.Lock()

        def write(self, data: bytes) -> None:
            gw_written.append(data)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    # Stdout capture (what would go to kiro-cli).
    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_transport = _FakeTransport()
    stdout_writer = asyncio.StreamWriter(
        stdout_transport, stdout_proto, stdout_writer_transport, loop
    )

    stop_event = asyncio.Event()

    # Use very short intervals so the test completes quickly.
    # 3 misses × 0.05s interval = 0.15s wait per ping + response wait.
    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=10,
    )

    assert session.reason == "peer_dead"
    assert "call-42" in session.outstanding_ids

    # Verify pings were sent to the gateway.
    ping_frames = [
        json.loads(b)
        for b in gw_written
        if b.strip() and json.loads(b).get("type") == _BRIDGE_PING_TYPE
    ]
    assert len(ping_frames) >= 3


@pytest.mark.asyncio
async def test_slow_call_not_killed_when_pongs_arrive() -> None:
    """A legitimately slow tool call must NOT be killed as long as the
    gateway keeps answering pings (proving it is alive)."""
    # Gateway reader that echoes pong for every ping received.
    gw_reader = asyncio.StreamReader()
    stdin_reader = asyncio.StreamReader()
    stdin_reader.feed_data(_jsonrpc_request("tools/call", "slow-1"))

    gw_written: list[bytes] = []

    class _PongWriter:
        """Fake writer that captures writes and feeds pongs back."""

        _mc_write_lock = asyncio.Lock()

        def __init__(self, feed_reader: asyncio.StreamReader) -> None:
            self._feed = feed_reader

        def write(self, data: bytes) -> None:
            gw_written.append(data)
            # If this is a ping frame, schedule a pong response on gw_reader.
            try:
                msg = json.loads(data)
                if isinstance(msg, dict) and msg.get("type") == _BRIDGE_PING_TYPE:
                    pong = json.dumps({"type": _BRIDGE_PONG_TYPE}) + "\n"
                    self._feed.feed_data(pong.encode())
            except ValueError:
                pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    fake_writer = _PongWriter(gw_reader)

    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_transport = _FakeTransport()
    stdout_writer = asyncio.StreamWriter(
        stdout_transport, stdout_proto, stdout_writer_transport, loop
    )

    stop_event = asyncio.Event()

    # Run the bridge for several ping intervals, then tear down gracefully
    # via stop_event. The bridge must NOT declare the peer dead.
    async def _stop_after_pings() -> None:
        # Wait long enough for multiple ping cycles to have fired.
        await asyncio.sleep(0.5)
        stop_event.set()

    stop_task = asyncio.create_task(_stop_after_pings())

    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            fake_writer,  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=10,
    )
    await stop_task

    # No liveness failure: the peer answered pings, so the bridge ended on the
    # stop event rather than on a dead peer.
    assert session.reason == "stop"

    # Verify pings were sent AND pongs were received (at least 2 cycles).
    ping_frames = [
        b for b in gw_written
        if b.strip() and json.loads(b).get("type") == _BRIDGE_PING_TYPE
    ]
    assert len(ping_frames) >= 2


@pytest.mark.asyncio
async def test_ping_rate_is_bounded_by_the_interval() -> None:
    """A peer that answers instantly must NOT be pinged at socket speed.

    The pong wait is not the cycle's interval when the peer is healthy: the
    gateway answers a ping inline in its connection handler, so the reply is
    back in microseconds. If the monitor returns straight to the next ping, the
    ping count scales with socket round-trip time instead of with elapsed time,
    and one stub with a request outstanding pegs both itself and the
    single-event-loop daemon that has to answer every ping.

    So the assertion is on the RATE, derived from the elapsed time this run
    actually took rather than from a hardcoded count: at most one ping per
    interval, plus the one sent at cycle zero. A lower bound comes with it,
    because a monitor that stopped pinging altogether would satisfy any ceiling.
    """
    gw_reader = asyncio.StreamReader()
    stdin_reader = asyncio.StreamReader()
    stdin_reader.feed_data(_jsonrpc_request("tools/call", "busy-1"))

    gw_written: list[bytes] = []

    class _InstantPongWriter:
        """Answers every ping synchronously, as a healthy daemon does."""

        _mc_write_lock = asyncio.Lock()

        def __init__(self, feed_reader: asyncio.StreamReader) -> None:
            self._feed = feed_reader

        def write(self, data: bytes) -> None:
            gw_written.append(data)
            if _safe_json_get_type(data) == _BRIDGE_PING_TYPE:
                pong = json.dumps({"type": _BRIDGE_PONG_TYPE}) + "\n"
                self._feed.feed_data(pong.encode())

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_writer = asyncio.StreamWriter(
        _FakeTransport(), stdout_proto, stdout_writer_transport, loop
    )

    stop_event = asyncio.Event()
    ping_interval = 0.05
    window = 0.6

    async def _stop_after_window() -> None:
        await asyncio.sleep(window)
        stop_event.set()

    stop_task = asyncio.create_task(_stop_after_window())
    started = loop.time()

    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _InstantPongWriter(gw_reader),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=ping_interval,
            ping_max_misses=3,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=30,
    )
    elapsed = loop.time() - started
    await stop_task

    assert session.reason == "stop"
    pings = [b for b in gw_written if _safe_json_get_type(b) == _BRIDGE_PING_TYPE]
    ceiling = elapsed / ping_interval + 1
    assert len(pings) <= ceiling, (
        f"{len(pings)} pings in {elapsed:.3f}s at interval {ping_interval}s "
        f"exceeds the one-per-interval ceiling of {ceiling:.1f}: the monitor is "
        "pinging per round-trip, not per interval"
    )
    # The monitor really ran: a socket-speed loop would be in the thousands
    # here, and a broken one would be at zero.
    assert len(pings) >= 2


@pytest.mark.asyncio
async def test_missed_pong_still_declares_peer_dead_within_the_grace() -> None:
    """Bounding the ping rate must not lengthen dead-peer detection.

    The remainder sleep belongs to the ANSWERED path only. Adding it to the
    missed path as well would make each miss cycle cost two intervals and
    silently double the advertised ``ping_interval × ping_max_misses`` grace, so
    this pins the wall-clock ceiling, not just the verdict.
    """
    gw_reader = asyncio.StreamReader()
    stdin_reader = asyncio.StreamReader()
    stdin_reader.feed_data(_jsonrpc_request("tools/call", "silent-1"))

    class _SilentWriter:
        """Accepts frames and never answers -- a wedged daemon."""

        _mc_write_lock = asyncio.Lock()

        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_writer = asyncio.StreamWriter(
        _FakeTransport(), stdout_proto, stdout_writer_transport, loop
    )

    ping_interval = 0.5
    ping_max_misses = 3
    grace = ping_interval * ping_max_misses
    started = loop.time()

    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _SilentWriter(),  # type: ignore[arg-type]
            asyncio.Event(),
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=ping_interval,
            ping_max_misses=ping_max_misses,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=30,
    )
    elapsed = loop.time() - started

    assert session.reason == "peer_dead"
    # Generous absolute slack for a loaded host, but far below the 2x that a
    # remainder sleep on the missed path would cost.
    assert elapsed < grace + 1.0, (
        f"peer declared dead after {elapsed:.3f}s, well past the advertised "
        f"{grace:.3f}s grace: a miss cycle is costing more than one interval"
    )


@pytest.mark.asyncio
async def test_normal_teardown_returns_none() -> None:
    """A clean stdin EOF must return None (no liveness failure) — the
    bridge tears down normally and never enters the degrade path."""
    gw_reader = asyncio.StreamReader()
    stdin_reader = asyncio.StreamReader()
    # Feed a request, its response, then EOF.
    stdin_reader.feed_data(_jsonrpc_request("initialize", "init-1"))
    # After a short delay, feed the response from the gateway and close stdin.
    gw_reader.feed_data(_jsonrpc_response("init-1"))
    stdin_reader.feed_eof()

    gw_written: list[bytes] = []

    class _FakeWriter:
        _mc_write_lock = asyncio.Lock()

        def write(self, data: bytes) -> None:
            gw_written.append(data)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_transport = _FakeTransport()
    stdout_writer = asyncio.StreamWriter(
        stdout_transport, stdout_proto, stdout_writer_transport, loop
    )

    stop_event = asyncio.Event()
    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=10,
    )

    assert session.reason != "peer_dead"


@pytest.mark.asyncio
async def test_no_pings_when_idle() -> None:
    """When no requests are outstanding, no pings should be sent — even
    after several intervals pass."""
    gw_reader = asyncio.StreamReader()
    stdin_reader = asyncio.StreamReader()
    # No requests sent — idle bridge.

    gw_written: list[bytes] = []

    class _FakeWriter:
        _mc_write_lock = asyncio.Lock()

        def write(self, data: bytes) -> None:
            gw_written.append(data)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    stdout_writer_transport = asyncio.StreamReader()
    stdout_proto = asyncio.StreamReaderProtocol(stdout_writer_transport)
    loop = asyncio.get_running_loop()
    stdout_transport = _FakeTransport()
    stdout_writer = asyncio.StreamWriter(
        stdout_transport, stdout_proto, stdout_writer_transport, loop
    )

    stop_event = asyncio.Event()

    async def _stop_after() -> None:
        await asyncio.sleep(0.3)
        stop_event.set()

    stop_task = asyncio.create_task(_stop_after())

    session = StubSession()
    await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
            session=session,
        ),
        timeout=10,
    )
    await stop_task

    assert session.reason != "peer_dead"
    # No pings should have been sent (only an unregister on clean close, if any).
    ping_frames = [
        b for b in gw_written
        if b.strip() and _safe_json_get_type(b) == _BRIDGE_PING_TYPE
    ]
    assert len(ping_frames) == 0


# --- Helpers ----------------------------------------------------------------


class _FakeTransport:
    """Minimal transport for asyncio.StreamWriter construction."""

    def get_extra_info(self, name: str, default=None):  # noqa: D102
        return default

    def is_closing(self) -> bool:  # noqa: D102
        return False

    def write(self, data: bytes) -> None:  # noqa: D102
        pass

    def write_eof(self) -> None:  # noqa: D102
        pass

    def can_write_eof(self) -> bool:  # noqa: D102
        return False

    def close(self) -> None:  # noqa: D102
        pass


def _safe_json_get_type(data: bytes) -> str:
    try:
        msg = json.loads(data)
        if isinstance(msg, dict):
            return msg.get("type", "")
    except ValueError:
        pass
    return ""


class TestLivenessIsNegotiated:
    """An un-negotiated peer must never be pinged.

    A gatewayd that outlived a package upgrade has no ``{"type": "ping"}``
    handler: the frame falls through to its forward path and no pong ever
    returns. Pinging it unconditionally would guarantee a full miss streak and
    force-degrade a perfectly healthy pooled session — the monitor would become a
    false-positive killer on exactly the version skew it is meant to survive.
    """

    def test_default_is_off(self) -> None:
        """`peer_supports_ping` defaults to False, so the gate fails closed."""
        import inspect

        from kiro_crew.mcp_gateway.stub import run_bridge

        assert inspect.signature(run_bridge).parameters[
            "peer_supports_ping"
        ].default is False

    def test_gatewayd_advertises_the_capability(self) -> None:
        """The stub's gate is only reachable if the daemon actually offers it.

        Asserted against the advertised set rather than gatewayd's source text:
        a grep for one literal breaks whenever an unrelated capability is added,
        and passes if the list is built but never sent.
        """
        from kiro_crew.mcp_gateway.gatewayd import REGISTERED_CAPABILITIES

        assert "bridge_ping" in REGISTERED_CAPABILITIES

    def test_liveness_path_does_not_exec(self) -> None:
        """The degrade path must reconnect or fail fast, never exec a server.

        Pre-flight fallbacks work because kiro-cli's `initialize` is still unread
        in fd0. Mid-bridge it has already been consumed and kiro-cli never
        re-sends it, so an exec'd server would reject every later call. What
        recovers the session instead is re-attaching to the restarted gateway and
        replaying the captured handshake, so this pins both halves: from the
        point the stub owns state that outlives one connection there is no
        ``fallback_exec``, and a reconnect is actually attempted rather than the
        outage being merely reported.
        """
        src = (_REPO_ROOT / "src/kiro_crew/mcp_gateway/stub.py").read_text(encoding="utf-8")
        # Anchor where the stub starts owning cross-connection state: every
        # pre-flight fallback site sits above it.
        marker = "    session = StubSession()"
        assert marker in src
        tail = src[src.index(marker):]
        # Judge CODE, not prose. The tail deliberately explains at length why it
        # does not exec, and a text search would read those very words as the
        # violation -- so comments are tokenized away before the check.
        assert "fallback_exec" not in _strip_comments(tail)
        assert "_reconnect_while_draining(" in tail, (
            "the mid-bridge degrade path no longer attempts a reconnect, so a "
            "broker restart again strips an attached session's servers for good"
        )
        # Through the PAIRED entry point specifically: reconnecting with nothing
        # reading kiro-cli's stream leaves a request issued in that window unread
        # for the whole budget (see test_stub_reconnect_queued_calls.py).
        assert "_reconnect(" not in _strip_comments(tail), (
            "the reconnect must be reached through _reconnect_while_draining so "
            "queued calls are answered rather than held"
        )
