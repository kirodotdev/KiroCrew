"""Tests for the agent->Electron browser command channel (Python half).

Bus-level tests exercise :class:`BrowserCommandBus` with no aiohttp and an
injected clock (TTL is testable without sleeping). A small set of handler-level
tests confirm the HTTP status mapping (503 fast-path, 204 idle drain, 404
unknown-id) through the real aiohttp handlers.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.browser.command_bus import (
    BrowserCommandBus,
    NoPanelError,
    QueueFullError,
)
from kiro_crew.dashboard.handlers import messaging as msg


async def _wait_registered(bus: BrowserCommandBus, key: str, timeout: float = 2.0) -> None:
    """Poll until ``key`` is registered as a live panel (bounded)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await bus.is_registered(key):
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"panel {key!r} never registered")


# ── bus: enqueue + complete round trip ────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_complete_round_trip() -> None:
    bus = BrowserCommandBus()
    drain_task = asyncio.create_task(bus.drain(["s1"], wait_ms=2000))
    await _wait_registered(bus, "s1")
    submit_task = asyncio.create_task(
        bus.submit("s1", "navigate", {"url": "http://x"}, timeout_ms=2000)
    )
    cmd = await drain_task
    assert cmd is not None
    assert cmd["session_key"] == "s1"
    assert cmd["op"] == "navigate"
    assert cmd["args"] == {"url": "http://x"}

    matched = await bus.complete(cmd["id"], True, result={"title": "hi"})
    assert matched is True

    outcome = await submit_task
    assert outcome["ok"] is True
    assert outcome["result"] == {"title": "hi"}
    assert outcome["id"] == cmd["id"]
    # No leaked state.
    assert bus._inflight == {}
    assert bus._queues == {}


@pytest.mark.asyncio
async def test_complete_with_failure_returns_error_outcome() -> None:
    bus = BrowserCommandBus()
    drain_task = asyncio.create_task(bus.drain(["s1"], wait_ms=2000))
    await _wait_registered(bus, "s1")
    submit_task = asyncio.create_task(bus.submit("s1", "click", {}, timeout_ms=2000))
    cmd = await drain_task
    assert cmd is not None
    await bus.complete(cmd["id"], False, error="element not found")
    outcome = await submit_task
    assert outcome["ok"] is False
    assert outcome["error"] == "element not found"


# ── bus: 503 fast-path when no panel registered ───────────────────────────


@pytest.mark.asyncio
async def test_submit_no_panel_raises_fast() -> None:
    bus = BrowserCommandBus()
    assert await bus.is_registered("s1") is False
    with pytest.raises(NoPanelError):
        await bus.submit("s1", "navigate", {}, timeout_ms=5000)


# ── bus: drain registers a session, then submit no longer 503s ────────────


@pytest.mark.asyncio
async def test_drain_registers_then_submit_ok() -> None:
    bus = BrowserCommandBus()
    with pytest.raises(NoPanelError):
        await bus.submit("s1", "op", {}, timeout_ms=100)

    drain_task = asyncio.create_task(bus.drain(["s1"], wait_ms=2000))
    await _wait_registered(bus, "s1")

    submit_task = asyncio.create_task(bus.submit("s1", "op", {}, timeout_ms=2000))
    cmd = await drain_task
    assert cmd is not None
    assert cmd["op"] == "op"
    await bus.complete(cmd["id"], True, result=None)
    await submit_task


# ── bus: TTL expiry de-registers (no sleeping; injected clock) ────────────


@pytest.mark.asyncio
async def test_ttl_expiry_deregisters() -> None:
    clock = {"t": 1000.0}
    bus = BrowserCommandBus(now=lambda: clock["t"])
    # wait_ms small -> TTL floors at 1.0s; registered at t=1000, expiry=1001.
    res = await bus.drain(["s1"], wait_ms=20)
    assert res is None
    assert await bus.is_registered("s1") is True
    # Advance the injected clock past the TTL without any real sleep.
    clock["t"] = 1002.0
    assert await bus.is_registered("s1") is False
    with pytest.raises(NoPanelError):
        await bus.submit("s1", "op", {}, timeout_ms=100)


# ── bus: timeout path cleans up ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_timeout_cleans_up() -> None:
    bus = BrowserCommandBus()
    # Register via an idle drain (returns None but leaves the panel live).
    assert await bus.drain(["s1"], wait_ms=20) is None
    assert await bus.is_registered("s1") is True
    with pytest.raises(asyncio.TimeoutError):
        await bus.submit("s1", "op", {}, timeout_ms=30)
    # The timed-out command must not linger in memory.
    assert bus._inflight == {}
    assert "s1" not in bus._queues


# ── bus: oversized queue rejected ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_oversized_queue_rejected() -> None:
    # Freeze the clock so the panel never expires during the test.
    bus = BrowserCommandBus(now=lambda: 1000.0, max_queue_per_session=2)
    assert await bus.drain(["s1"], wait_ms=10) is None
    assert await bus.is_registered("s1") is True

    # Two long-lived submits that stay queued (nothing drains them).
    t1 = asyncio.create_task(bus.submit("s1", "op", {}, timeout_ms=5000))
    t2 = asyncio.create_task(bus.submit("s1", "op", {}, timeout_ms=5000))
    for _ in range(400):
        if len(bus._queues.get("s1", [])) == 2:
            break
        await asyncio.sleep(0.005)
    assert len(bus._queues.get("s1", [])) == 2

    with pytest.raises(QueueFullError):
        await bus.submit("s1", "op", {}, timeout_ms=5000)

    for t in (t1, t2):
        t.cancel()
    await asyncio.gather(t1, t2, return_exceptions=True)


# ── bus: unknown-id completion returns False ──────────────────────────────


@pytest.mark.asyncio
async def test_complete_unknown_id_returns_false() -> None:
    bus = BrowserCommandBus()
    assert await bus.complete("does-not-exist", True, result=1) is False


# ── bus: drain returns None when idle ─────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_idle_returns_none() -> None:
    bus = BrowserCommandBus()
    assert await bus.drain(["s1"], wait_ms=30) is None


# ── handlers: HTTP status mapping ─────────────────────────────────────────


def _req(payload, remote: str = "127.0.0.1", internal_auth: bool = True) -> MagicMock:
    req = MagicMock()
    req.remote = remote
    req.headers = {}
    req.app = {"state": MagicMock()}

    # These are MACHINE endpoints: loopback is necessary but NOT sufficient, so
    # the handlers require ``request["internal_auth"] is True`` (set only on the
    # validated X-Internal-Secret path). A plain MagicMock would return a truthy
    # mock for any key, which would silently defeat that check in tests.
    def _get(key, default=None):
        if key == "internal_auth":
            return True if internal_auth else None
        return default

    req.get = _get

    async def _json():
        return payload

    req.json = _json
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


@pytest.fixture
def fresh_bus(monkeypatch):
    bus = BrowserCommandBus()
    monkeypatch.setattr(msg, "get_command_bus", lambda: bus)
    monkeypatch.setattr(msg, "_sel", lambda: MagicMock())
    return bus


@pytest.mark.asyncio
async def test_handler_command_503_no_panel(fresh_bus) -> None:
    resp = await msg.api_browser_command(_req({"session_key": "s1", "op": "navigate", "args": {}}))
    assert resp.status == 503
    assert _body(resp)["error"] == "no-native-panel"


@pytest.mark.asyncio
async def test_handler_command_non_loopback_403(fresh_bus) -> None:
    resp = await msg.api_browser_command(
        _req({"session_key": "s1", "op": "navigate", "args": {}}, remote="10.0.0.9")
    )
    assert resp.status == 403


@pytest.mark.asyncio
async def test_handlers_reject_loopback_cookie_caller(fresh_bus) -> None:
    """Loopback alone must NOT admit a browser-credentialed caller.

    Membership in ``_STRICT_INTERNAL_API_PATHS`` still lets the token_auth
    middleware admit a loopback dashboard-COOKIE caller, so these machine
    endpoints additionally require ``request["internal_auth"] is True``. Without
    that, a browser-credentialed page could drive, intercept or forge
    native-browser operations (drive/read someone's embedded browser).
    """
    payloads = (
        (msg.api_browser_command, {"session_key": "s1", "op": "navigate", "args": {}}),
        (msg.api_browser_command_drain, {"session_keys": ["s1"], "wait_ms": 1}),
        (msg.api_browser_command_result, {"id": "x", "ok": True}),
    )
    for handler, payload in payloads:
        resp = await handler(_req(payload, internal_auth=False))
        assert resp.status == 403, f"{handler.__name__} admitted a cookie-only caller"


@pytest.mark.asyncio
async def test_handler_command_missing_fields_400(fresh_bus) -> None:
    # ``op`` is the only hard-required field now; a missing session identity is a
    # graceful 503 (below), not a 400.
    resp = await msg.api_browser_command(_req({"session_key": "s1"}))
    assert resp.status == 400
    assert _body(resp)["code"] == "op_required"


@pytest.mark.asyncio
async def test_handler_command_no_session_identity_is_503_not_400(fresh_bus) -> None:
    """op present but no resolvable session (no host_pid, empty fallback) ->
    answer like no-panel (503) so the proxy falls back to Playwright, rather than
    surfacing a hard 400 MCP error to the agent."""
    resp = await msg.api_browser_command(_req({"op": "navigate", "args": {}}))
    assert resp.status == 503
    assert _body(resp)["error"] == "no-native-panel"


@pytest.mark.asyncio
async def test_handler_command_resolves_session_from_host_pid(fresh_bus, monkeypatch) -> None:
    """The gateway resolves the authoritative key from host_pid, OVERRIDING the
    proxy's empty frozen-env fallback, and strips the "dashboard:" prefix to
    match the bare slot key the Electron panel registers via command-drain."""
    monkeypatch.setattr(msg, "_resolve_browse_session_key", lambda _pid: "dashboard:chat-9")
    # Panel registers under the BARE slot key (what Electron's listPanelIds yields).
    drain_task = asyncio.create_task(
        msg.api_browser_command_drain(_req({"session_keys": ["chat-9"], "wait_ms": 2000}))
    )
    await _wait_registered(fresh_bus, "chat-9")
    # Warm-pool proxy: empty session_key, but host_pid resolves to chat-9.
    submit_task = asyncio.create_task(
        msg.api_browser_command(
            _req({"session_key": "", "host_pid": 4242, "op": "navigate", "args": {"url": "http://x"}})
        )
    )
    drain_resp = await drain_task
    assert drain_resp.status == 200
    assert _body(drain_resp)["session_key"] == "chat-9", "resolved+stripped key drives the panel"
    await msg.api_browser_command_result(_req({"id": _body(drain_resp)["id"], "ok": True, "result": "ok"}))
    submit_resp = await submit_task
    assert submit_resp.status == 200
    assert _body(submit_resp)["ok"] is True


@pytest.mark.asyncio
async def test_handler_drain_idle_204(fresh_bus) -> None:
    resp = await msg.api_browser_command_drain(_req({"session_keys": ["s1"], "wait_ms": 30}))
    assert resp.status == 204


@pytest.mark.asyncio
async def test_handler_result_unknown_404(fresh_bus) -> None:
    resp = await msg.api_browser_command_result(_req({"id": "nope", "ok": True}))
    assert resp.status == 404
    assert _body(resp)["error"] == "unknown-command"


@pytest.mark.asyncio
async def test_handler_full_round_trip(fresh_bus) -> None:
    # Electron long-polls; the proxy submits; Electron posts the result.
    drain_task = asyncio.create_task(
        msg.api_browser_command_drain(_req({"session_keys": ["s1"], "wait_ms": 2000}))
    )
    await _wait_registered(fresh_bus, "s1")
    submit_task = asyncio.create_task(
        msg.api_browser_command(
            _req({"session_key": "s1", "op": "navigate", "args": {"url": "http://x"}})
        )
    )
    drain_resp = await drain_task
    assert drain_resp.status == 200
    drained = _body(drain_resp)
    assert drained["op"] == "navigate"

    result_resp = await msg.api_browser_command_result(
        _req({"id": drained["id"], "ok": True, "result": {"title": "ok"}})
    )
    assert result_resp.status == 200

    submit_resp = await submit_task
    assert submit_resp.status == 200
    outcome = _body(submit_resp)
    assert outcome["ok"] is True
    assert outcome["result"] == {"title": "ok"}
