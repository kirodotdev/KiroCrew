"""Tests for api_sessions_health cache TTL and lock contention."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import sessions


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset module-level cache between tests."""
    sessions._health_cache = {}
    sessions._health_cache_ts = 0.0
    sessions._health_lock = None
    yield
    sessions._health_cache = {}
    sessions._health_cache_ts = 0.0
    sessions._health_lock = None


def _make_request() -> web.Request:
    return make_mocked_request("GET", "/api/sessions/health")


@pytest.mark.asyncio
async def test_cache_deduplicates_calls_within_ttl():
    """Two calls within TTL window should only invoke compute once."""
    call_count = 0

    def fake_compute():
        nonlocal call_count
        call_count += 1
        return {"sess-1": {"reason": "subagent_timeout"}}

    with patch("kiro_crew.dashboard.session_health.compute_session_health", side_effect=fake_compute):
        resp1 = await sessions.api_sessions_health(_make_request())
        resp2 = await sessions.api_sessions_health(_make_request())

    assert call_count == 1
    assert resp1.status == 200
    assert resp2.status == 200
