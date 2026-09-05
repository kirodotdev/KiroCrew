"""Tests for the dashboard-user Web Push routes: subscribe, unsubscribe, and the
VAPID public key. Verifies dashboard-user tokens are accepted and app tokens are
rejected (Web Push subscription is a browser concern, not an app producer's)."""

from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from kiro_crew.dashboard.handlers.notifications_push import (
    api_push_subscribe,
    api_push_unsubscribe,
    api_vapid_public_key,
)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _fake_subscription() -> dict:
    priv = ec.generate_private_key(ec.SECP256R1())
    raw = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "endpoint": "https://push.example.com/abc",
        "keys": {"p256dh": _b64(raw), "auth": _b64(os.urandom(16))},
    }


def _make_state(monkeypatch, tmp_path):
    from kiro_crew.dashboard.state import DashboardState

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.settings.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.push_store.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.notifications.vapid_keys.config_dir", lambda: tmp_path)
    from kiro_crew.notifications import vapid_keys

    vapid_keys.reset_cache_for_tests()
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


def _make_app(state, *, app_token: str = "") -> web.Application:
    """Build a minimal app with an auth-marker middleware that stamps identity
    like the real token-auth middleware (request['app'] / request['user'])."""

    @web.middleware
    async def _identity(request, handler):
        request["app"] = app_token
        request["user"] = "" if app_token else "fabian"
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = state
    app.router.add_get("/api/notifications/push/vapid-public-key", api_vapid_public_key)
    app.router.add_post("/api/notifications/push/subscribe", api_push_subscribe)
    app.router.add_post("/api/notifications/push/unsubscribe", api_push_unsubscribe)
    return app


@pytest.mark.asyncio
async def test_vapid_public_key_for_dashboard_user(monkeypatch, tmp_path):
    state = _make_state(monkeypatch, tmp_path)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.get("/api/notifications/push/vapid-public-key")
        body = await resp.json()
    assert resp.status == 200
    assert isinstance(body["publicKey"], str) and body["publicKey"]


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_roundtrip(monkeypatch, tmp_path):
    state = _make_state(monkeypatch, tmp_path)
    sub = _fake_subscription()
    async with TestClient(TestServer(_make_app(state))) as client:
        r1 = await client.post("/api/notifications/push/subscribe", json=sub)
        b1 = await r1.json()
        assert r1.status == 200 and b1["ok"] is True
        assert len(state.push_subscription_store.all()) == 1

        r2 = await client.post(
            "/api/notifications/push/unsubscribe", json={"endpoint": sub["endpoint"]}
        )
        b2 = await r2.json()
        assert r2.status == 200 and b2["removed"] is True
        assert state.push_subscription_store.all() == []


@pytest.mark.asyncio
async def test_subscribe_rejects_malformed(monkeypatch, tmp_path):
    state = _make_state(monkeypatch, tmp_path)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/notifications/push/subscribe", json={"endpoint": "https://x"}
        )
        body = await resp.json()
    assert resp.status == 400
    assert body["code"] == "push_subscription_invalid"


@pytest.mark.asyncio
async def test_app_token_rejected(monkeypatch, tmp_path):
    state = _make_state(monkeypatch, tmp_path)
    sub = _fake_subscription()
    async with TestClient(TestServer(_make_app(state, app_token="some-app"))) as client:
        r_sub = await client.post("/api/notifications/push/subscribe", json=sub)
        r_key = await client.get("/api/notifications/push/vapid-public-key")
    assert r_sub.status == 403
    assert r_key.status == 403
    assert state.push_subscription_store.all() == []
