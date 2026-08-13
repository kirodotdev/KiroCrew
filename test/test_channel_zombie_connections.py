"""Regression tests for the silent zombie-connection defect class.

A NAT-evicted half-open TCP connection must never leave a channel's inbound
path permanently dead while looking healthy: transports need a transport-level
keepalive (or a per-request deadline), liveness-task death must recycle the
connection loudly, and connection ends must be visible at WARNING+.

Offline only: no network. The Discord WS and iLink HTTP surfaces are exercised
through fakes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

import pytest

from kiro_crew.discord.client import _WS_HEARTBEAT_SECS, DiscordClient
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.weixin.client import ContextTokenStore
from kiro_crew.weixin.transport import WeixinTransport

# ── Discord: transport keepalive ──────────────────────────────────────────────


def test_ws_connect_requests_aiohttp_level_keepalive():
    """The Gateway WS must be opened with a transport-level ping.

    ``heartbeat=None`` leaves a NAT-evicted half-open socket blocking the
    dispatch loop forever (the Aug 2026 zombie). The interval must also stay
    clear of Discord's ~41s op-1 heartbeat so a healthy connection never flaps.
    """
    src = inspect.getsource(DiscordClient._run_connection)
    assert "heartbeat=_WS_HEARTBEAT_SECS" in src
    assert "heartbeat=None" not in src
    assert _WS_HEARTBEAT_SECS > 41.25  # aiohttp ping must not race op-1


# ── Discord: heartbeat-task death recycles the connection ─────────────────────


class _FailingWs:
    """WS stub whose send always raises, driving the heartbeat error path."""

    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0

    async def send_json(self, payload) -> None:  # noqa: ANN001
        raise ConnectionResetError("boom")

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


@pytest.mark.asyncio
async def test_heartbeat_loop_death_closes_ws_and_warns(caplog):
    client = DiscordClient(token="test")
    ws = _FailingWs()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
        # interval=0 removes the pre-send jitter sleep from the timing budget.
        await client._heartbeat_loop(ws, 0.0)
    assert ws.close_calls == 1, "a dead heartbeat task must recycle the connection"
    assert any("heartbeat loop failed" in r.message for r in caplog.records)
    assert all(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_loop_cancellation_stays_quiet_and_leaves_ws_open():
    """Shutdown cancellation is not a failure: no close, no warning."""
    client = DiscordClient(token="test")

    class _IdleWs(_FailingWs):
        async def send_json(self, payload) -> None:  # noqa: ANN001
            await asyncio.sleep(3600)

    ws = _IdleWs()
    task = asyncio.create_task(client._heartbeat_loop(ws, 3600.0))
    await asyncio.sleep(0)
    task.cancel()
    await task
    assert ws.close_calls == 0


# ── Discord: connection end clears READY/badge on EVERY exit path ─────────────


class _ExplodingWs:
    """WS stub whose read loop raises mid-dispatch (socket reset shape)."""

    close_code = None
    closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionResetError("reset mid-dispatch")


class _CleanCloseWs(_ExplodingWs):
    close_code = 1000

    async def __anext__(self):
        raise StopAsyncIteration


class _FakeWsCtx:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc) -> bool:
        return False


class _FakeSession:
    """Just enough of aiohttp.ClientSession for _run_connection."""

    closed = False

    def __init__(self, ws) -> None:
        self._ws = ws

    def ws_connect(self, *args, **kwargs):
        return _FakeWsCtx(self._ws)


@pytest.mark.asyncio
async def test_exception_escape_clears_ready_and_flips_the_badge():
    """A socket reset mid-dispatch exits _run_connection via exception; READY
    and the dashboard badge must still be cleared (they previously survived
    only the clean-close path)."""
    client = DiscordClient(token="test")
    client._session = _FakeSession(_ExplodingWs())
    client.ready.set()
    states: list[tuple[bool, str]] = []
    client.on_state_change = lambda connected, error: states.append((connected, error))
    with pytest.raises(ConnectionResetError):
        await client._run_connection()
    assert not client.ready.is_set()
    assert states and states[-1][0] is False


@pytest.mark.asyncio
async def test_clean_close_warns_and_flips_the_badge(caplog):
    client = DiscordClient(token="test")
    client._session = _FakeSession(_CleanCloseWs())
    client.ready.set()
    states: list[tuple[bool, str]] = []
    client.on_state_change = lambda connected, error: states.append((connected, error))
    with caplog.at_level(logging.WARNING, logger="kiro_crew.discord.client"):
        await client._run_connection()
    assert not client.ready.is_set()
    assert any("connection ended" in r.message for r in caplog.records)
    assert states == [(False, "reconnecting (close 1000)")]


@pytest.mark.asyncio
async def test_deliberate_close_does_not_report_reconnecting():
    """close() sets _closed before tearing down; the finally must not flip the
    badge to a 'reconnecting' reason on an intentional shutdown."""
    client = DiscordClient(token="test")
    client._session = _FakeSession(_CleanCloseWs())
    client._closed = True
    states: list[tuple[bool, str]] = []
    client.on_state_change = lambda connected, error: states.append((connected, error))
    await client._run_connection()
    assert states == []


# ── Weixin: timeouts are counted, not laundered ────────────────────────────────


class _TimeoutClient:
    """iLink client stub returning N timed-out polls, then parking forever."""

    def __init__(self, timeouts: int) -> None:
        self._remaining = timeouts

    async def get_updates(self, sync_buf: str) -> dict:
        if self._remaining > 0:
            self._remaining -= 1
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf, "_timed_out": True}
        await asyncio.sleep(3600)
        return {}

    async def close(self) -> None:
        pass


def _transport(client, tmp_path) -> WeixinTransport:
    async def dispatch(msg: InboundMessage) -> None:
        pass

    return WeixinTransport(
        client,
        account_id="acct1",
        ctx_store=ContextTokenStore(str(tmp_path)),
        dm_policy="open",
        dispatch=dispatch,
    )


async def _run_poll_until_parked(t: WeixinTransport) -> None:
    """Drive the poll loop until the stub client parks, then cancel-shutdown."""
    t._running = True
    task = asyncio.create_task(t._poll_loop())
    for _ in range(400):  # poll until the stub parks (all scripted responses consumed)
        await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a_sustained_timeout_streak_warns_exactly_once(tmp_path, caplog):
    """45 consecutive timeouts must produce ONE warning (at the 20 threshold),
    not one every N polls — a long-idle channel must not spam the log even if
    the server holds every poll to the client deadline."""
    t = _transport(_TimeoutClient(timeouts=45), tmp_path)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.weixin.transport"):
        await _run_poll_until_parked(t)
    hits = [r for r in caplog.records if "consecutive long-poll timeouts" in r.message]
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_a_successful_poll_resets_the_timeout_streak(tmp_path, caplog):
    class _NineteenThenOk(_TimeoutClient):
        def __init__(self) -> None:
            super().__init__(timeouts=19)
            self._served_ok = False

        async def get_updates(self, sync_buf: str) -> dict:
            if self._remaining > 0:
                return await super().get_updates(sync_buf)
            if not self._served_ok:
                self._served_ok = True
                return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}
            await asyncio.sleep(3600)
            return {}

    t = _transport(_NineteenThenOk(), tmp_path)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.weixin.transport"):
        await _run_poll_until_parked(t)
    assert not any("consecutive long-poll timeouts" in r.message for r in caplog.records)


# ── Weixin: unexpected loop death is loud and flips the badge ─────────────────


@pytest.mark.asyncio
async def test_unexpected_poll_loop_death_warns_and_notifies(tmp_path, caplog):
    class _ExplodingClient:
        async def get_updates(self, sync_buf: str) -> dict:
            raise SystemExit("simulated non-Exception escape")  # bypasses except Exception

        async def close(self) -> None:
            pass

    t = _transport(_ExplodingClient(), tmp_path)
    states: list[tuple[bool, str]] = []
    t.on_state_change = lambda connected, error: states.append((connected, error))
    t._running = True
    with caplog.at_level(logging.WARNING, logger="kiro_crew.weixin.transport"):
        with pytest.raises(SystemExit):
            await t._poll_loop()
    assert any("ended unexpectedly" in r.message for r in caplog.records)
    assert states == [(False, "poll loop ended unexpectedly")]


@pytest.mark.asyncio
async def test_production_shutdown_cancellation_is_not_reported_as_death(tmp_path, caplog):
    """Gateway teardown cancels the poll task WITHOUT calling disconnect()
    (so ``_running`` is still True). That is a shutdown, not a death: no
    warning, no badge flip, and the cancellation propagates."""
    t = _transport(_TimeoutClient(timeouts=0), tmp_path)
    states: list[tuple[bool, str]] = []
    t.on_state_change = lambda connected, error: states.append((connected, error))
    t._running = True  # deliberately NOT cleared — mirrors production teardown
    task = asyncio.create_task(t._poll_loop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert states == []
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
