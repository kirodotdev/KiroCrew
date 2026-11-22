"""Tests for session-keepalive endpoint + wait-tool keepalive pings.

Regression coverage for the bug where the `wait` MCP tool blocked the
ACP subprocess so long that is_responsive() went stale and the gateway
SIGTERM'd it (exit code -15).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers.sessions import api_session_keepalive


class _FakeSessions:
    def __init__(self, provider):
        self._provider = provider

    def get_provider(self, key):
        return self._provider if key == "known" else None


def _make_request(headers, state):
    req = MagicMock(spec=web.Request)
    req.headers = headers
    req.app = {"state": state}
    return req


@pytest.mark.asyncio
async def test_keepalive_missing_session_key_returns_400():
    state = MagicMock()
    state.sessions = _FakeSessions(provider=MagicMock())
    resp = await api_session_keepalive(_make_request({}, state))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_keepalive_unknown_session_returns_404():
    state = MagicMock()
    state.sessions = _FakeSessions(provider=MagicMock())
    resp = await api_session_keepalive(_make_request({"X-Session-Key": "unknown"}, state))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_keepalive_calls_touch_activity_on_provider():
    provider = MagicMock()
    state = MagicMock()
    state.sessions = _FakeSessions(provider=provider)
    resp = await api_session_keepalive(_make_request({"X-Session-Key": "known"}, state))
    assert resp.status == 200
    provider.touch_activity.assert_called_once_with()


def test_wait_tool_posts_keepalive_periodically():
    """wait() should POST /api/session-keepalive at least once while sleeping."""
    import time as _time

    from kiro_crew import mcp_core
    from kiro_crew.mcp_core import _call_tool

    # A fake clock that only `sleep` advances, so extra `monotonic()` reads on
    # the way in (identity, SEL logging) cannot spend the deadline before the
    # first ping. A call-count fake breaks on one extra read. Each sleep moves
    # toward a fixed deadline, so the loop still ends.
    #
    # Only ``mcp_core.time`` is replaced: patching ``time.monotonic`` hands the
    # fake to every thread in the worker (the pool reaper polls it every 0.5 s),
    # which would then spin on a clock only this test moves.
    clock = [0.0]

    def _fake_monotonic() -> float:
        return clock[0]

    def _fake_sleep(secs: float) -> None:
        clock[0] += secs

    class _FakeTime:
        monotonic = staticmethod(_fake_monotonic)
        sleep = staticmethod(_fake_sleep)

        def __getattr__(self, name):
            return getattr(_time, name)

    with patch("kiro_crew.mcp_core._post") as mock_post:
        mock_post.return_value = {}
        with patch.object(mcp_core, "time", _FakeTime()):
            _call_tool("wait", {"seconds": 60, "reason": "test"})

    paths = [c.args[0] for c in mock_post.call_args_list]
    assert "/api/session-keepalive" in paths
