
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

# ---------------------------------------------------------------------------
# api_sessions_restart: syncs MCP servers before restarting
# ---------------------------------------------------------------------------


def _make_restart_request():
    """Build a minimal request for api_sessions_restart."""
    state = MagicMock()
    state.sessions = MagicMock()
    state.sessions.count = 0
    state.sessions._lock = asyncio.Lock()
    state.sessions._sessions = {}
    state.sessions._pool_started = False
    state.sessions.drain_all_providers = AsyncMock(return_value=[])
    state.sessions.start_pool = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_refresh = MagicMock()
    state.push_slots_update = MagicMock()
    state._background_tasks = set()
    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    return request


class TestApiSessionsRestartMcpSync:
    """Verify MCP sync runs before session restart."""

    @pytest.mark.asyncio
    async def test_syncs_new_servers_before_restart(self):
        """The serialized sync should run and the count appear in the response."""
        from kiro_crew.dashboard.handlers.sessions import api_sessions_restart

        fake_server = MagicMock()
        request = _make_restart_request()

        with (
            patch("kiro_crew.dashboard.handlers.sessions._reset_all_sessions", new_callable=AsyncMock, return_value=2),
            patch("kiro_crew.dashboard.handlers.sessions.sync_discovered_servers", return_value=[fake_server]),
        ):
            resp = await api_sessions_restart(request)

        body = json.loads(resp.body)
        assert body["mcp_synced"] == 1
        assert body["sessions_reset"] == 2

    @pytest.mark.asyncio
    async def test_sync_failure_does_not_block_restart(self):
        """If MCP sync raises, restart must still proceed — but the reset must
        NOT be marked as applied (the on-disk config may still be stale, so
        clearing the staleness banner would acknowledge a change never applied)."""
        from kiro_crew.dashboard.handlers.sessions import api_sessions_restart

        request = _make_restart_request()

        with (
            patch("kiro_crew.dashboard.handlers.sessions._reset_all_sessions", new_callable=AsyncMock, return_value=1) as reset,
            patch("kiro_crew.dashboard.handlers.sessions.sync_discovered_servers", side_effect=RuntimeError("boom")),
        ):
            resp = await api_sessions_restart(request)

        body = json.loads(resp.body)
        assert body["sessions_reset"] == 1
        assert body["mcp_synced"] == 0
        assert body["mcp_sync_ok"] is False
        reset.assert_awaited_once_with(request)

    @pytest.mark.asyncio
    async def test_no_servers_to_sync(self):
        """When nothing needed syncing, synced count is 0 (and restart still runs)."""
        from kiro_crew.dashboard.handlers.sessions import api_sessions_restart

        request = _make_restart_request()

        with (
            patch("kiro_crew.dashboard.handlers.sessions._reset_all_sessions", new_callable=AsyncMock, return_value=0),
            patch("kiro_crew.dashboard.handlers.sessions.sync_discovered_servers", return_value=[]) as mock_sync,
        ):
            resp = await api_sessions_restart(request)

        body = json.loads(resp.body)
        assert body["mcp_synced"] == 0
        mock_sync.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_servers_synced(self):
        """Multiple discovered servers should all be counted."""
        from kiro_crew.dashboard.handlers.sessions import api_sessions_restart

        request = _make_restart_request()
        servers = [MagicMock(), MagicMock(), MagicMock()]

        with (
            patch("kiro_crew.dashboard.handlers.sessions._reset_all_sessions", new_callable=AsyncMock, return_value=1),
            patch("kiro_crew.dashboard.handlers.sessions.sync_discovered_servers", return_value=servers),
        ):
            resp = await api_sessions_restart(request)

        body = json.loads(resp.body)
        assert body["mcp_synced"] == 3


# ---------------------------------------------------------------------------
# NOTE: The former TestInjectSkillPathsPreservesFlagValues class was removed.
# It covered builder-mcp flag injection (--include-tool-tags / --exclude-tools
# for @builder-mcp) and AIM skill-path injection via the agent.py helpers
# _inject_skill_paths / _ensure_flag_values / _inject_builder_mcp_flags. That
# machinery is Amazon-internal and was removed from the public fork: the default
# agent config no longer injects @builder-mcp or AIM skill paths, so the helpers
# and their tests no longer apply.
# ---------------------------------------------------------------------------
