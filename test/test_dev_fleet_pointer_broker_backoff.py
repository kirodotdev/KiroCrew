"""Dev Fleet's pointer broker holds its secret exchange after a refusal.

A broker with a stale secret must not exchange on every pointer read, because each
refusal is one more row in the gateway's security event log. It waits the refusal's
``Retry-After``, and at least 1 s doubling to 30 s, before it asks again.
"""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.dev_fleet import live, pointer_broker
from kiro_crew.testing.clock import ManualClock


def _gateway(answers: list[tuple[int, dict[str, str]]]) -> tuple[web.Application, dict[str, Any]]:
    """Answer each exchange with the next ``(status, headers)``; 200 grants ``t1``."""
    seen: dict[str, Any] = {"exchanges": 0}

    async def token(_request: web.Request) -> web.Response:
        status, headers = answers[min(seen["exchanges"], len(answers) - 1)]
        seen["exchanges"] += 1
        if status == 200:
            return web.json_response({"token": "t1"})
        return web.json_response({"error": "invalid secret"}, status=status, headers=headers)

    async def state(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "live": "/wt/live", "staged": None})

    app = web.Application()
    app.router.add_post("/api/apps/dev-fleet/token", token)
    app.router.add_get("/api/apps/dev-fleet/live-target", state)
    return app, seen


@pytest.fixture
def clock(monkeypatch) -> ManualClock:
    c = ManualClock()
    c.install(monkeypatch, pointer_broker, time=True)
    return c


async def _read(broker: pointer_broker.GatewayPointerBroker) -> str:
    try:
        return (await broker(True)).live or ""
    except live.PointerUnavailable as exc:
        return f"unavailable: {exc}"


@pytest.mark.asyncio
async def test_retry_after_holds_the_next_exchange(clock) -> None:
    app, seen = _gateway([(429, {"Retry-After": "60"}), (200, {})])
    client = TestClient(TestServer(app))
    await client.start_server()
    broker = pointer_broker.GatewayPointerBroker(
        origin=f"http://127.0.0.1:{client.server.port}", app_secret="s"
    )
    try:
        assert (await _read(broker)).endswith("HTTP 429")
        clock.advance(59)
        assert "next attempt in 1s" in await _read(broker)
        assert seen["exchanges"] == 1, "inside the hold no request is sent"
        clock.advance(1)
        assert await _read(broker) == "/wt/live"
        assert seen["exchanges"] == 2
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_refusals_without_a_header_back_off_and_a_grant_resets(clock) -> None:
    app, seen = _gateway([(403, {}), (403, {}), (403, {}), (200, {}), (403, {})])
    client = TestClient(TestServer(app))
    await client.start_server()
    broker = pointer_broker.GatewayPointerBroker(
        origin=f"http://127.0.0.1:{client.server.port}", app_secret="s"
    )
    try:
        for hold in (1, 2, 4):
            assert (await _read(broker)).endswith("HTTP 403")
            clock.advance(hold - 0.5)
            assert "next attempt in" in await _read(broker)
            clock.advance(0.5)
        assert await _read(broker) == "/wt/live"
        assert seen["exchanges"] == 4

        broker._token = None
        assert (await _read(broker)).endswith("HTTP 403")
        assert broker._exchange_not_before - clock.monotonic() == 1, "a grant resets the backoff"
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_a_server_error_holds_nothing(clock) -> None:
    app, seen = _gateway([(503, {}), (200, {})])
    client = TestClient(TestServer(app))
    await client.start_server()
    broker = pointer_broker.GatewayPointerBroker(
        origin=f"http://127.0.0.1:{client.server.port}", app_secret="s"
    )
    try:
        assert (await _read(broker)).endswith("HTTP 503")
        assert await _read(broker) == "/wt/live", "a 5xx says nothing about the secret"
        assert seen["exchanges"] == 2
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.parametrize(
    ("header", "refusals", "expected"),
    [
        (None, 0, 1.0),
        (None, 10, 30.0),
        ("60", 0, 60.0),
        ("5", 4, 16.0),
        ("86400", 0, 300.0),
        ("inf", 0, 1.0),
        ("nan", 0, 1.0),
        ("-5", 0, 1.0),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0, 1.0),
    ],
)
def test_hold_is_the_header_or_the_backoff_whichever_is_longer(header, refusals, expected) -> None:
    assert pointer_broker._exchange_hold_secs(header, refusals) == expected
