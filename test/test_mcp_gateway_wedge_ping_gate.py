"""Ping-gated wedge detection + notifications/cancelled receiver tests.

Covers:
- Old request + fresh ping -> not recycled, single warning logged
- Old request + stale ping -> wedged (recycled)
- HARD_WEDGE_CEILING_SECS exceeded -> wedged even with fresh pings
- Cold-start not insta-reaped (fresh _last_ping_response_mono)
- notifications/cancelled cancels running tool, no response emitted
- Unknown notification silently ignored
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    HARD_WEDGE_CEILING_SECS,
    HEARTBEAT_PING_ID,
    HEARTBEAT_TIMEOUT_SECS,
    PING_STALE_SECS,
    _PendingRequest,
)

# --- Helpers ----------------------------------------------------------------


@dataclass
class _FakePoolKey:
    server_name: str = "kirocrew-core"
    agent_name: str = "kirocrew"

    def human_readable(self) -> str:
        return f"{self.agent_name}:{self.server_name}"


def _make_backend(
    *,
    refcount: int = 1,
    pending: Optional[dict[str, Any]] = None,
    returncode: Optional[int] = None,
    last_ping_response_mono: Optional[float] = None,
) -> Any:
    """Create a real Backend with mocked process/stdin for heartbeat testing."""
    process = MagicMock()
    process.returncode = returncode
    process.pid = 9999
    stdin = MagicMock()

    async def _drain() -> None:
        return None

    stdin.drain = _drain
    stdin.write = MagicMock()

    backend = backend_mod.Backend(
        pool_key=_FakePoolKey(),  # type: ignore[arg-type]
        process=process,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=time.monotonic(),
        last_used_at=time.monotonic(),
    )
    backend.refcount = refcount
    if pending:
        backend._pending_requests.update(pending)
    if last_ping_response_mono is not None:
        backend._last_ping_response_mono = last_ping_response_mono
    else:
        # Default: recent ping (not stale)
        backend._last_ping_response_mono = time.monotonic()

    # Attach a stub inbox so broadcast_backend_gone has a target
    inbox: asyncio.Queue[bytes] = asyncio.Queue()
    backend._stub_inboxes["stub-A"] = inbox
    backend._test_inbox = inbox  # type: ignore[attr-defined]
    return backend


# --- Fix A: Ping-gated wedge detection tests --------------------------------


@pytest.mark.asyncio
async def test_old_request_fresh_ping_not_recycled() -> None:
    """A request exceeding HEARTBEAT_TIMEOUT_SECS with fresh pings should NOT
    cause a recycle. The backend is slow, not wedged."""
    now = time.monotonic()
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 600) * 1000.0,  # 600s old
        ),
    }
    backend = _make_backend(
        refcount=1,
        pending=pending,
        last_ping_response_mono=now - 30,  # ping 30s ago -- fresh
    )
    state = await backend._heartbeat_once(now=now)
    assert state == "alive", f"Expected alive but got {state}"
    assert backend.is_alive
    assert backend._dead_reason is None
    # Should have warned about the slow request
    assert "gw-9999-1" in backend._warned_slow_ids


@pytest.mark.asyncio
async def test_old_request_fresh_ping_warns_once() -> None:
    """Warning for a slow-but-responsive request fires only once per request id."""
    now = time.monotonic()
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 600) * 1000.0,
        ),
    }
    backend = _make_backend(
        refcount=1,
        pending=pending,
        last_ping_response_mono=now - 10,
    )
    # First heartbeat: should warn
    await backend._heartbeat_once(now=now)
    assert "gw-9999-1" in backend._warned_slow_ids

    # Second heartbeat: same id already warned, no duplicate
    with patch("kiro_crew.mcp_gateway.backend.logger") as mock_logger:
        await backend._heartbeat_once(now=now + 60)
        # Should NOT have called warning with "slow in-flight" again
        for call in mock_logger.warning.call_args_list:
            assert "slow in-flight" not in str(call)


@pytest.mark.asyncio
async def test_old_request_stale_ping_is_wedged() -> None:
    """Old request + stale ping (no response in PING_STALE_SECS) -> wedged."""
    now = time.monotonic()
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 400) * 1000.0,  # 400s old > HEARTBEAT_TIMEOUT_SECS
        ),
    }
    backend = _make_backend(
        refcount=1,
        pending=pending,
        last_ping_response_mono=now - 200,  # 200s > PING_STALE_SECS (150)
    )
    state = await backend._heartbeat_once(now=now)
    assert state == "wedged"
    assert not backend.is_alive
    assert "ping stale" in backend._dead_reason


@pytest.mark.asyncio
async def test_hard_ceiling_wedged_even_with_fresh_pings() -> None:
    """HARD_WEDGE_CEILING_SECS exceeded -> wedged regardless of ping freshness."""
    now = time.monotonic()
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 2200) * 1000.0,  # 2200s > HARD_WEDGE_CEILING_SECS (2100)
        ),
    }
    backend = _make_backend(
        refcount=1,
        pending=pending,
        last_ping_response_mono=now - 5,  # ping very fresh
    )
    state = await backend._heartbeat_once(now=now)
    assert state == "wedged"
    assert not backend.is_alive
    assert "hard ceiling" in backend._dead_reason


@pytest.mark.asyncio
async def test_cold_start_not_insta_reaped() -> None:
    """A backend with an old pending request but a fresh ping response should
    NOT be recycled -- the ping-gate alone prevents recycling."""
    now = time.monotonic()
    # Request is 350s old (exceeds HEARTBEAT_TIMEOUT_SECS), but ping is fresh
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 350) * 1000.0,
        ),
    }
    backend = _make_backend(
        refcount=1,
        pending=pending,
        last_ping_response_mono=now,  # fresh ping -> not wedged
    )
    state = await backend._heartbeat_once(now=now)
    assert state == "alive"
    assert backend.is_alive


@pytest.mark.asyncio
async def test_ping_response_updates_mono() -> None:
    """_route_backend_line updates _last_ping_response_mono when a ping response arrives."""
    backend = _make_backend(refcount=1, last_ping_response_mono=0.0)
    before = time.monotonic()
    reply = (json.dumps({"jsonrpc": "2.0", "id": HEARTBEAT_PING_ID, "result": {}}) + "\n").encode()
    await backend._route_backend_line(reply)
    after = time.monotonic()
    assert backend._last_ping_response_mono >= before
    assert backend._last_ping_response_mono <= after


@pytest.mark.asyncio
async def test_warned_ids_pruned_on_completion() -> None:
    """Warned slow ids are pruned when the request completes."""
    now = time.monotonic()
    pending = {
        "gw-9999-1": _PendingRequest(
            stub_uuid="stub-A",
            original_id=1,
            method="tools/call",
            t_start_ms=(now - 600) * 1000.0,
        ),
    }
    backend = _make_backend(refcount=1, pending=pending, last_ping_response_mono=now - 10)
    await backend._heartbeat_once(now=now)
    assert "gw-9999-1" in backend._warned_slow_ids

    # Simulate request completion
    backend._pending_requests.clear()
    await backend._heartbeat_once(now=now + 60)
    assert "gw-9999-1" not in backend._warned_slow_ids


# --- Fix B: notifications/cancelled tests -----------------------------------


def test_cancel_event_check() -> None:
    """is_tool_cancelled() reads the module-level _thread_cancel_event."""
    import kiro_crew.mcp_shared as mod
    from kiro_crew.mcp_shared import is_tool_cancelled

    # No event set
    mod._thread_cancel_event = None
    assert not is_tool_cancelled()

    # Event not set
    evt = threading.Event()
    mod._thread_cancel_event = evt
    assert not is_tool_cancelled()

    # Event set
    evt.set()
    assert is_tool_cancelled()

    # Cleanup
    mod._thread_cancel_event = None


def test_tool_cancelled_exception_suppresses_response() -> None:
    """A notifications/cancelled for the in-flight request must suppress the
    JSON-RPC response: the tool cooperatively raises ToolCancelled and
    respond() is never called with that request id.

    Regression for the str/int id mismatch: the gateway sends requestId as a
    STRING ("2") while the loop stored the tools/call id as an INT (2) --
    suppression must still fire (ids are normalized to str internally)."""
    import kiro_crew.mcp_shared as mod
    from kiro_crew.mcp_shared import ToolCancelled, run_mcp_stdio_loop

    tool_started = threading.Event()

    call_idx = [0]

    def fake_read_message(stdin):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        if idx == 1:
            return {"jsonrpc": "2.0", "method": "notifications/initialized"}
        if idx == 2:
            return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cancellable_tool", "arguments": {}}}
        if idx == 3:
            # Deliver the cancel while the tool is running. requestId is a
            # STRING while the tools/call id above was an INT (regression).
            tool_started.wait(timeout=5)
            return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "2", "reason": "test"}}
        return None  # EOF

    def cancellable_tool(name, args):
        tool_started.set()
        # Cooperatively wait for the cancel event like the real wait tool
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if mod.is_tool_cancelled():
                raise ToolCancelled("cancelled by test")
            time.sleep(0.05)
        return "should-have-been-cancelled"

    def list_tools():
        return [{"name": "cancellable_tool", "description": "t", "inputSchema": {"type": "object"}}]

    respond_calls: list = []

    def capturing_respond(rid, result=None, error=None):
        respond_calls.append((rid, result, error))

    def fake_select(rlist, wlist, xlist, timeout=None):
        return (rlist, [], [])

    with patch.object(mod, "_read_message", fake_read_message), \
         patch.object(mod, "respond", capturing_respond), \
         patch("select.select", fake_select):
        loop_thread = threading.Thread(
            target=run_mcp_stdio_loop,
            args=("test-server", "0.1.0", list_tools, cancellable_tool),
            daemon=True,
        )
        loop_thread.start()
        loop_thread.join(timeout=10)
        assert not loop_thread.is_alive(), "Loop did not exit"

    # No response may be emitted for the cancelled request id (any type)
    tool_responses = [c for c in respond_calls if str(c[0]) == "2"]
    assert tool_responses == [], f"Cancelled request must get NO response, got: {tool_responses}"


def test_unknown_notification_silently_ignored() -> None:
    """An unknown notification delivered through the loop must be ignored:
    no error response is emitted and the loop keeps serving (a subsequent
    tools/call still succeeds)."""
    import kiro_crew.mcp_shared as mod
    from kiro_crew.mcp_shared import run_mcp_stdio_loop

    call_idx = [0]

    def fake_read_message(stdin):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        if idx == 1:
            return {"jsonrpc": "2.0", "method": "notifications/initialized"}
        if idx == 2:
            # Unknown notification (no id) -- must be silently ignored
            return {"jsonrpc": "2.0", "method": "notifications/some_future_thing", "params": {"x": 1}}
        if idx == 3:
            return {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "quick_tool", "arguments": {}}}
        return None  # EOF

    def quick_tool(name, args):
        return "ok"

    def list_tools():
        return [{"name": "quick_tool", "description": "t", "inputSchema": {"type": "object"}}]

    respond_calls: list = []

    def capturing_respond(rid, result=None, error=None):
        respond_calls.append((rid, result, error))

    def fake_select(rlist, wlist, xlist, timeout=None):
        return (rlist, [], [])

    with patch.object(mod, "_read_message", fake_read_message), \
         patch.object(mod, "respond", capturing_respond), \
         patch("select.select", fake_select):
        loop_thread = threading.Thread(
            target=run_mcp_stdio_loop,
            args=("test-server", "0.1.0", list_tools, quick_tool),
            daemon=True,
        )
        loop_thread.start()
        loop_thread.join(timeout=10)
        assert not loop_thread.is_alive(), "Loop did not exit"

    # The unknown notification produced no response and no error
    errors = [c for c in respond_calls if c[2] is not None]
    assert errors == [], f"Unknown notification must not produce errors: {errors}"
    # The subsequent tools/call (id=7) still got its response
    tool_responses = [c for c in respond_calls if c[0] == 7]
    assert len(tool_responses) == 1, f"tools/call after unknown notification must still work: {respond_calls}"


def test_constants_relationships() -> None:
    """Verify constant relationships: PING_STALE < HEARTBEAT_TIMEOUT < HARD_CEILING."""
    assert PING_STALE_SECS > 0
    assert HEARTBEAT_TIMEOUT_SECS > PING_STALE_SECS
    assert HARD_WEDGE_CEILING_SECS > HEARTBEAT_TIMEOUT_SECS
    # Hard ceiling should accommodate wait max (1800s) + margin
    assert HARD_WEDGE_CEILING_SECS >= 1800 + 60


# --- Fix A: ping answered while tool is in-flight ---------------------------


def test_ping_answered_while_tool_in_flight() -> None:
    """When a tool is executing, an incoming 'ping' request must still be
    answered with an empty-object response so the gateway's wedge detector
    sees the backend as responsive."""
    import kiro_crew.mcp_shared as mod
    from kiro_crew.mcp_shared import run_mcp_stdio_loop

    tool_started = threading.Event()
    tool_release = threading.Event()

    # Messages delivered to the loop via patched _read_message:
    # 1) initialize, 2) notifications/initialized, 3) tools/call, 4) ping (while tool runs), 5) None (EOF)
    call_idx = [0]

    def fake_read_message(stdin):
        idx = call_idx[0]
        call_idx[0] += 1
        if idx == 0:
            return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        if idx == 1:
            return {"jsonrpc": "2.0", "method": "notifications/initialized"}
        if idx == 2:
            return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "slow_tool", "arguments": {}}}
        if idx == 3:
            # Wait until tool starts before delivering ping
            tool_started.wait(timeout=5)
            return {"jsonrpc": "2.0", "id": 99, "method": "ping", "params": {}}
        # After ping, wait for tool to finish then EOF
        tool_release.wait(timeout=5)
        return None

    def slow_tool(name, args):
        tool_started.set()
        tool_release.wait(timeout=5)
        return "done"

    def list_tools():
        return [{"name": "slow_tool", "description": "test", "inputSchema": {"type": "object"}}]

    respond_calls: list = []
    ping_responded = threading.Event()

    def capturing_respond(rid, result=None, error=None):
        respond_calls.append((rid, result, error))
        if rid == 99:
            ping_responded.set()

    # select.select must say "readable" whenever we have a message to deliver
    def fake_select(rlist, wlist, xlist, timeout=None):
        return (rlist, [], [])

    with patch.object(mod, "_read_message", fake_read_message), \
         patch.object(mod, "respond", capturing_respond), \
         patch("select.select", fake_select):
        loop_thread = threading.Thread(
            target=run_mcp_stdio_loop,
            args=("test-server", "0.1.0", list_tools, slow_tool),
            daemon=True,
        )
        loop_thread.start()

        # Wait for tool to start
        assert tool_started.wait(timeout=5), "Tool did not start"
        # Wait until the loop actually answers the ping (no fixed sleep)
        assert ping_responded.wait(timeout=5), "Ping response not emitted in time"
        # Release tool
        tool_release.set()
        loop_thread.join(timeout=5)

    # Verify: ping (id=99) got an empty-object response
    ping_responses = [(rid, res) for rid, res, err in respond_calls if rid == 99]
    assert len(ping_responses) == 1, f"Expected 1 ping response, got {len(ping_responses)}: {respond_calls}"
    assert ping_responses[0][1] == {}, f"Ping response should be empty object, got {ping_responses[0][1]}"
