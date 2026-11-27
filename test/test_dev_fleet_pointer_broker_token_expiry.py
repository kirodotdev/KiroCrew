"""Dev Fleet's pointer broker re-exchanges its app token before it expires.

The broker presents its app token as ``?token=``, which the gateway accepts only
until the token's ``exp`` (the 5-minute link window). A broker that kept the token
until a read was refused sent one doomed ``GET /api/apps/dev-fleet/live-target``
per window, and each one wrote a ``token expired`` denial to the security log.
The exchange now answers ``expires_in``, and the broker re-exchanges before it
runs out. The 401/403 re-exchange stays as the fallback for a gateway that sends
no ``expires_in``, or one that restarted with a new signing key.
"""

from __future__ import annotations

import json
import math
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps.builtins.dev_fleet import pointer_broker
from kiro_crew.dashboard import token_auth_tokens
from kiro_crew.dashboard.handlers import core as core_mod
from kiro_crew.testing.clock import ManualClock

_WINDOW = 300


def _fake_gateway(clock: ManualClock, *, expires_in: Any = _WINDOW):
    """A gateway that refuses a token ``_WINDOW`` wall-clock seconds after it minted it,
    as the real one judges ``exp``.

    ``expires_in`` is what the exchange answers; ``None`` leaves the field out,
    as a gateway from before this change does.
    """
    seen: dict[str, Any] = {"exchanges": 0, "refused": 0, "minted": {}}

    def _live(token: str | None) -> bool:
        minted = seen["minted"].get(token)
        return minted is not None and clock.time() < minted + _WINDOW

    async def token(request: web.Request):
        seen["exchanges"] += 1
        value = f"t{seen['exchanges']}"
        seen["minted"][value] = clock.time()
        body: dict[str, Any] = {"token": value}
        if expires_in is not None:
            body["expires_in"] = expires_in
        return web.json_response(body)

    async def state(request: web.Request):
        if not _live(request.query.get("token")):
            seen["refused"] += 1
            return web.json_response({"error": "token expired"}, status=401)
        return web.json_response({"live": "/wt/live", "staged": None})

    async def lease(request: web.Request):
        if not _live(request.query.get("token")):
            seen["refused"] += 1
            return web.json_response({"error": "token expired"}, status=401)
        return web.json_response({"granted": True, "token": "lease-1"})

    app = web.Application()
    app.router.add_post("/api/apps/dev-fleet/token", token)
    app.router.add_get("/api/apps/dev-fleet/live-target", state)
    app.router.add_post("/api/apps/dev-fleet/live-target/removal-lease", lease)
    return app, seen


@pytest.fixture
def clock(monkeypatch) -> ManualClock:
    clock = ManualClock()
    clock.install(monkeypatch, pointer_broker)
    return clock


async def _broker(app: web.Application):
    client = TestClient(TestServer(app))
    await client.start_server()
    broker = pointer_broker.GatewayPointerBroker(
        origin=f"http://127.0.0.1:{client.server.port}", app_secret="s3cret"
    )
    return client, broker


@pytest.mark.asyncio
async def test_a_read_after_the_window_never_presents_the_expired_token(clock) -> None:
    app, seen = _fake_gateway(clock)
    client, broker = await _broker(app)
    try:
        assert (await broker(True)).live == "/wt/live"
        clock.advance(_WINDOW + 1)
        assert (await broker(True)).live == "/wt/live"
        assert seen["refused"] == 0, "the broker presented a token the gateway had expired"
        assert seen["exchanges"] == 2
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_the_token_is_renewed_before_it_runs_out_not_at_the_edge(clock) -> None:
    """A token inside the refresh margin is renewed, so a read cannot expire in flight."""
    app, seen = _fake_gateway(clock)
    client, broker = await _broker(app)
    try:
        await broker(True)
        clock.advance(_WINDOW - pointer_broker._TOKEN_REFRESH_MARGIN_SECS)
        await broker(True)
        assert seen["exchanges"] == 2
        assert seen["refused"] == 0
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_a_live_token_is_reused(clock) -> None:
    app, seen = _fake_gateway(clock)
    client, broker = await _broker(app)
    try:
        await broker(True)
        clock.advance(_WINDOW - pointer_broker._TOKEN_REFRESH_MARGIN_SECS - 1)
        await broker(True)
        assert seen["exchanges"] == 1
        assert seen["refused"] == 0
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_the_deadline_is_on_the_wall_clock_the_gateway_judges_exp_by(clock) -> None:
    """Monotonic time stops while the host is suspended and wall time does not, so a
    deadline on the monotonic clock would reuse a token the gateway already expired.
    The two clocks cannot be split under one ``ManualClock``, so this pins the base the
    deadline is computed from: the wall-clock instant the exchange was sent."""
    app, _seen = _fake_gateway(clock)
    client, broker = await _broker(app)
    try:
        sent_at = clock.time()
        await broker(True)
        assert broker._token_due == sent_at + _WINDOW - pointer_broker._TOKEN_REFRESH_MARGIN_SECS
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
async def test_the_removal_lease_call_renews_the_token_too(clock) -> None:
    app, seen = _fake_gateway(clock)
    client, broker = await _broker(app)
    try:
        await broker(True)
        clock.advance(_WINDOW + 1)
        assert await broker.acquire_removal_lease("/wt/old") == "lease-1"
        assert seen["refused"] == 0
        assert seen["exchanges"] == 2
    finally:
        await broker.aclose()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("expires_in", [None, True, "300", 0, -5, math.nan, math.inf])
async def test_without_a_usable_expires_in_a_refusal_still_renews(clock, expires_in) -> None:
    """An older gateway sends no ``expires_in``; a malformed one is ignored the same way.
    The broker then keeps the token until it is refused and re-exchanges once."""
    app, seen = _fake_gateway(clock, expires_in=expires_in)
    client, broker = await _broker(app)
    try:
        await broker(True)
        clock.advance(_WINDOW + 1)
        assert (await broker(True)).live == "/wt/live"
        assert seen["refused"] == 1
        assert seen["exchanges"] == 2
    finally:
        await broker.aclose()
        await client.close()


class _Req:
    def __init__(self) -> None:
        self.match_info = {"name": "dev-fleet"}
        self.headers = {"X-App-Secret": "ok"}


@pytest.mark.asyncio
async def test_the_exchange_answers_the_window_the_query_token_is_accepted_for(
    monkeypatch,
) -> None:
    """``expires_in`` is the token's own ``exp - iat``: the shorter of its two
    windows, so a caller renewing by it never presents an expired ``?token=``."""
    monkeypatch.setattr("kiro_crew.dashboard.token_auth.validate_app_secret", lambda *_a: True)
    monkeypatch.setattr("kiro_crew.sel.sel", MagicMock)
    resp = await core_mod.api_app_token(_Req())
    assert resp.status == 200
    body = json.loads(resp.body)
    claims = json.loads(token_auth_tokens._b64url_decode(body["token"].split(".")[0]))
    assert body["expires_in"] == pytest.approx(claims["exp"] - claims["iat"], abs=1e-3)
    assert body["expires_in"] < claims["session_exp"] - claims["iat"]
