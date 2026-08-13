"""Tests for POST /api/shutdown endpoint."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.core import api_shutdown


@pytest.fixture()
def shutdown_event():
    return asyncio.Event()


@pytest.fixture()
def app(shutdown_event):
    a = web.Application()
    a.router.add_post("/api/shutdown", api_shutdown)
    a["local_secret"] = "test-secret-abc123"
    return a


@pytest.mark.asyncio
async def test_shutdown_rejects_non_loopback(app):
    # FORK-DIVERGENCE: api_shutdown mirrors api_logout's local-import style —
    # it calls _h.is_loopback (where _h is the kiro_crew.dashboard.handlers
    # package), so patch the name there, not on core.
    with patch("kiro_crew.dashboard.handlers.core._sel") as mock_sel, \
         patch("kiro_crew.dashboard.handlers.is_loopback", return_value=False):
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/shutdown",
                headers={"X-Local-Secret": "test-secret-abc123"},
            )
            assert resp.status == 403
            body = await resp.json()
            assert body["error"] == "loopback only"


@pytest.mark.asyncio
async def test_shutdown_rejects_invalid_secret(app):
    with patch("kiro_crew.dashboard.handlers.core._sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/shutdown",
                headers={"X-Local-Secret": "wrong-secret"},
            )
            assert resp.status == 403
            body = await resp.json()
            assert body["error"] == "invalid secret"


@pytest.mark.asyncio
async def test_shutdown_success_sets_event(app, shutdown_event):
    # FORK-DIVERGENCE: the handler does a function-local
    # `from kiro_crew import shutdown_event`, so patch the source attribute.
    with patch("kiro_crew.dashboard.handlers.core._sel") as mock_sel, \
         patch("kiro_crew.shutdown_event", shutdown_event):
        mock_sel.return_value = MagicMock()
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/shutdown",
                headers={"X-Local-Secret": "test-secret-abc123"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["ok"] is True
            assert body["shutting_down"] is True
            # The event is set after a 250ms delay via call_later.
            await asyncio.sleep(0.35)
            assert shutdown_event.is_set()
