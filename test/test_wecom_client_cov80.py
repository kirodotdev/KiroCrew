"""Coverage for the WeCom client's connection-liveness machinery.

Companion to ``test_wecom_client.py``, which owns this module's main behaviour; this file
only closes the coverage gaps left at its edges. New behaviour cases belong
in the sibling, not here.

The keepalive and reconnect paths are what keep the bot's settings badge honest
and stop a bad-credential connect from hot-looping: the ping loop's missed-pong
kill, the reconnect loop's "lived long enough to be healthy" reset, and the
lifecycle (start / close) that owns the WS task and session. Also pinned: the
handler wiring that must never let an app exception escape into the receive loop.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import aiohttp
import pytest

from kiro_crew.wecom import client as client_mod
from kiro_crew.wecom.client import WeComClient, WeComInbound, _resolve_proxy, new_stream_id


@dataclass
class _Msg:
    type: Any
    data: str = ""


class _FakeWS:
    """Fake aiohttp WebSocket recording sent frames; closes after ``close_after`` sends."""

    def __init__(self, close_after: int | None = None, send_exc: Exception | None = None) -> None:
        self.sent: list[dict] = []
        self.closed = False
        self._close_after = close_after
        self._send_exc = send_exc

    async def send_json(self, data: dict) -> None:
        if self._send_exc is not None:
            raise self._send_exc
        self.sent.append(data)
        if self._close_after is not None and len(self.sent) >= self._close_after:
            self.closed = True

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def client() -> WeComClient:
    return WeComClient(bot_id="bot-x", secret="sec-x", ws_url="wss://example.invalid/ws")


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace the module's sleeps with a yield, recording requested delays."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(secs: float) -> None:
        delays.append(secs)
        await real_sleep(0)

    monkeypatch.setattr(client_mod.asyncio, "sleep", _sleep)
    return delays


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_launches_the_loop_and_close_tears_it_down(self, client) -> None:
        entered = asyncio.Event()

        async def _loop() -> None:
            entered.set()
            await asyncio.sleep(60)

        client._run_loop = _loop  # type: ignore[method-assign]
        await client.start()
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert client._closed is False
        assert client._kicked is False

        ws = _FakeWS()
        session = _FakeSession()
        client._ws = ws  # type: ignore[assignment]
        client._session = session  # type: ignore[assignment]
        await client.close()
        assert client._closed is True
        assert client._task is None
        assert ws.closed is True
        assert session.closed is True

    @pytest.mark.asyncio
    async def test_close_is_safe_without_a_live_connection(self, client) -> None:
        await client.close()
        assert client._closed is True
        assert client._task is None


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestPingLoop:
    @pytest.mark.asyncio
    async def test_sends_a_ping_and_tracks_the_pending_pong(self, client, no_sleep) -> None:
        ws = _FakeWS(close_after=1)
        await client._ping_loop(ws)
        assert no_sleep == [30]
        assert [f["cmd"] for f in ws.sent] == ["ping"]
        assert client._pending_pongs == 1
        assert ws.sent[0]["headers"]["req_id"] in client._ping_reqs

    @pytest.mark.asyncio
    async def test_three_missed_pongs_closes_the_connection(self, client, no_sleep) -> None:
        ws = _FakeWS()
        client._pending_pongs = 3
        await client._ping_loop(ws)
        assert ws.closed is True
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_tracking_set_is_bounded(self, client, no_sleep) -> None:
        ws = _FakeWS(close_after=1)
        client._ping_reqs = {f"stale-{i}" for i in range(101)}
        await client._ping_loop(ws)
        assert len(client._ping_reqs) == 1

    @pytest.mark.asyncio
    async def test_send_failure_ends_the_loop(self, client, no_sleep) -> None:
        ws = _FakeWS(send_exc=ConnectionError("socket gone"))
        await client._ping_loop(ws)
        assert client._pending_pongs == 0

    @pytest.mark.asyncio
    async def test_closed_socket_never_pings(self, client, no_sleep) -> None:
        ws = _FakeWS()
        ws.closed = True
        await client._ping_loop(ws)
        assert no_sleep == []
        assert ws.sent == []


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_long_lived_connection_resets_the_backoff(
        self, client, no_sleep, monkeypatch
    ) -> None:
        # A fixed-length iterator standing in for a monotonic clock is a trap:
        # the loop reads it an implementation-defined number of times, and once
        # the values run out `next()` raises StopIteration inside a coroutine,
        # which PEP 479 surfaces as `RuntimeError: generator raised
        # StopIteration` -- an error that names neither the clock nor this test.
        # Hold the final value instead, so the scripted timeline is unchanged
        # but the clock can be read any number of times.
        scripted = [0.0, 100.0, 200.0, 200.0]

        def _monotonic() -> float:
            return scripted.pop(0) if len(scripted) > 1 else scripted[0]

        monkeypatch.setattr(client_mod.time, "monotonic", _monotonic)
        calls = {"n": 0}

        async def _serve() -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                client._closed = True

        client._connect_and_serve = _serve  # type: ignore[method-assign]
        await client._run_loop()
        assert calls["n"] == 2
        assert no_sleep == []  # a healthy connection never backs off

    @pytest.mark.asyncio
    async def test_immediate_close_backs_off_and_reports_a_reason(
        self, client, no_sleep, monkeypatch
    ) -> None:
        monkeypatch.setattr(client_mod.time, "monotonic", lambda: 1.0)
        statuses: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: statuses.append((healthy, reason))

        async def _serve() -> None:
            client._closed = len(no_sleep) >= 1

        client._connect_and_serve = _serve  # type: ignore[method-assign]
        await client._run_loop()
        assert no_sleep == [1.0]
        assert statuses and statuses[0][0] is False
        assert "check bot ID" in statuses[0][1]

    @pytest.mark.asyncio
    async def test_transport_error_backs_off_with_the_error_reason(self, client, no_sleep) -> None:
        statuses: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: statuses.append((healthy, reason))

        async def _serve() -> None:
            client._closed = len(no_sleep) >= 1
            raise aiohttp.ClientError("handshake refused")

        client._connect_and_serve = _serve  # type: ignore[method-assign]
        await client._run_loop()
        assert no_sleep == [1.0]
        assert statuses[0] == (False, "handshake refused")

    @pytest.mark.asyncio
    async def test_cancellation_breaks_out_without_backing_off(self, client, no_sleep) -> None:
        async def _serve() -> None:
            raise asyncio.CancelledError

        client._connect_and_serve = _serve  # type: ignore[method-assign]
        await client._run_loop()
        assert no_sleep == []

    @pytest.mark.asyncio
    async def test_kick_stops_reconnecting_and_reports_it(self, client, no_sleep) -> None:
        statuses: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: statuses.append((healthy, reason))

        async def _serve() -> None:
            client._kicked = True

        client._connect_and_serve = _serve  # type: ignore[method-assign]
        await client._run_loop()
        assert no_sleep == []
        assert statuses[-1][0] is False
        assert "kicked" in statuses[-1][1]


class TestStatusNotifications:
    def test_repeated_state_is_deduped(self, client) -> None:
        seen: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: seen.append((healthy, reason))
        client._notify_status(False, "first")
        client._notify_status(False, "second")
        client._notify_status(True, "")
        assert seen == [(False, "first"), (True, "")]

    def test_observer_exception_never_escapes(self, client) -> None:
        def _boom(healthy: bool, reason: str) -> None:
            raise RuntimeError("observer broke")

        client.on_status = _boom
        client._notify_status(False, "reason")
        assert client._last_status is False


class TestAckFrames:
    @pytest.mark.asyncio
    async def test_malformed_ack_headers_are_dropped(self, client) -> None:
        client._pending_pongs = 2
        await client._handle_message(json.dumps({"errcode": 0, "headers": ["not", "a", "dict"]}))
        assert client._pending_pongs == 2

    @pytest.mark.asyncio
    async def test_pong_ack_clears_the_pending_pong(self, client) -> None:
        client._pending_pongs = 2
        client._ping_reqs = {"ping-1"}
        await client._handle_message(json.dumps({"errcode": 0, "headers": {"req_id": "ping-1"}}))
        assert client._pending_pongs == 1
        assert client._ping_reqs == set()


class TestHandlerWiring:
    @pytest.mark.asyncio
    async def test_unset_handler_is_a_noop(self, client) -> None:
        assert await client._invoke_handler(WeComInbound(userid="u1", text="hi")) is None

    @pytest.mark.asyncio
    async def test_handler_set_post_construction_receives_the_inbound(self, client) -> None:
        seen: list[WeComInbound] = []

        async def _handler(inbound: WeComInbound) -> None:
            seen.append(inbound)

        client.set_message_handler(_handler)
        await client._invoke_handler(WeComInbound(userid="u1", text="hi"))
        assert [i.userid for i in seen] == ["u1"]

    @pytest.mark.asyncio
    async def test_handler_exception_is_contained(self, client) -> None:
        async def _boom(inbound: WeComInbound) -> None:
            raise RuntimeError("turn blew up")

        client.set_message_handler(_boom)
        await client._invoke_handler(WeComInbound(userid="u1", text="hi"))


class TestProxyResolution:
    def test_first_set_proxy_variable_wins(self, monkeypatch) -> None:
        for var in (
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
        assert _resolve_proxy() == "http://proxy.invalid:3128"

    def test_no_proxy_variables_resolve_to_none(self, monkeypatch) -> None:
        for var in (
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "https_proxy",
            "http_proxy",
            "all_proxy",
        ):
            monkeypatch.delenv(var, raising=False)
        assert _resolve_proxy() is None


def test_stream_ids_are_unique_and_prefixed() -> None:
    first, second = new_stream_id(), new_stream_id()
    assert first.startswith("stream_") and second.startswith("stream_")
    assert first != second
