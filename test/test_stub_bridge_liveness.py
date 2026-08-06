"""Tests for the stub bridge liveness monitor (ping-while-outstanding).

Covers:
* A silent peer (accepts, never answers) causes the stub to emit a JSON-RPC
  error and return a BridgeLivenessFailure — not park forever.
* A legitimately slow call (peer still answers pings) is NOT killed.
* Normal bridge teardown (stdin EOF) returns None (no liveness failure).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kiro_crew.mcp_gateway.stub import (
    _BRIDGE_PING_TYPE,
    _BRIDGE_PONG_TYPE,
    BridgeLivenessFailure,
    run_bridge,
)

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
    requests must trigger a BridgeLivenessFailure within a bounded time,
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
    result = await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
        ),
        timeout=10,
    )

    assert result is not None
    assert isinstance(result, BridgeLivenessFailure)
    assert "call-42" in result.outstanding_ids

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
            except (json.JSONDecodeError, ValueError):
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

    result = await asyncio.wait_for(
        run_bridge(
            gw_reader,
            fake_writer,  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
        ),
        timeout=10,
    )
    await stop_task

    # No liveness failure: the peer answered pings.
    assert result is None

    # Verify pings were sent AND pongs were received (at least 2 cycles).
    ping_frames = [
        b for b in gw_written
        if b.strip() and json.loads(b).get("type") == _BRIDGE_PING_TYPE
    ]
    assert len(ping_frames) >= 2


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
    result = await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
        ),
        timeout=10,
    )

    assert result is None


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

    result = await asyncio.wait_for(
        run_bridge(
            gw_reader,
            _FakeWriter(),  # type: ignore[arg-type]
            stop_event,
            stdin=stdin_reader,
            stdout_writer=stdout_writer,
            ping_interval=0.05,
            ping_max_misses=3,
            peer_supports_ping=True,
        ),
        timeout=10,
    )
    await stop_task

    assert result is None
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
    except (json.JSONDecodeError, ValueError):
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
        """The stub's gate is only reachable if the daemon actually offers it."""
        from pathlib import Path

        src = Path("src/kiro_crew/mcp_gateway/gatewayd.py").read_text(encoding="utf-8")
        assert '"capabilities": ["ensure_backend", "bridge_ping"]' in src

    def test_liveness_path_does_not_exec(self) -> None:
        """The degrade path must fail fast, not exec a fresh server.

        Pre-flight fallbacks work because kiro-cli's `initialize` is still unread
        in fd0. Mid-bridge it has already been consumed and kiro-cli never
        re-sends it, so an exec'd server would reject every later call — the
        session's tools are lost either way, and exec would additionally discard
        the socket a future reconnect could reuse.
        """
        from pathlib import Path

        src = Path("src/kiro_crew/mcp_gateway/stub.py").read_text(encoding="utf-8")
        marker = "if liveness_failure is not None:"
        assert marker in src
        tail = src[src.index(marker):]
        assert "fallback_exec" not in tail
