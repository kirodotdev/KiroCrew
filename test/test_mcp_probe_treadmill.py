"""Regression: a disabled MCP server must not defeat the probe cache.

``GET /api/mcp`` re-probes when a configured server is absent from
``_mcp_probe_cache``, so a freshly installed server flips from "Unknown" to a
real status on the next page load instead of waiting out the TTL.

The cache is filled from ``probe_all()``, which deliberately excludes
consent-disabled rows — probing spawns the server process, and that is exactly
what consent gates. ``list_servers()`` deliberately INCLUDES them so the UI can
render the row. Comparing the unfiltered list against the cache therefore
classified every disabled server as "new" on every single request, so
``should_reprobe`` was permanently true and a full spawn fan-out was always in
flight for anyone with one disabled server. The freshness check must apply
probe_all's own filter.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo


def _request(state) -> object:
    """Minimal stand-in for the aiohttp request the handler reads."""

    class _App(dict):
        pass

    class _Req:
        def __init__(self, app):
            self.app = app

    app = _App()
    app["state"] = state
    return _Req(app)


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()


def _arrange(monkeypatch, tmp_path, servers, cache):
    """Point the handler at a synthetic server list and a warm probe cache."""
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    # No mcp.json on disk: the disabled/kirocrewManaged overlay is a no-op, so
    # `disabled` comes purely from the McpServerInfo rows above.
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
    # Warm cache: the TTL branch must NOT be the thing that triggers a reprobe.
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)

    probe = AsyncMock(return_value=None)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", probe)
    return probe


class TestDisabledServerDoesNotDefeatProbeCache:
    @pytest.mark.asyncio
    async def test_disabled_server_does_not_force_a_reprobe(
        self, monkeypatch, tmp_path
    ) -> None:
        """A disabled row absent from the cache must not re-arm the probe.

        Reverted (the check iterating every server rather than only probeable
        ones), this schedules a background probe on a warm cache.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="turned-off", command="/bin/true", disabled=True),
        ]
        # probe_all() would only ever have returned the enabled row.
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        probe = _arrange(monkeypatch, tmp_path, servers, cache)

        state = _State()
        await mcp_mod.api_mcp_servers(_request(state))

        probe.assert_not_awaited()
        assert not state._background_tasks
        assert mcp_mod._mcp_probe_in_progress is False

    @pytest.mark.asyncio
    async def test_new_enabled_server_still_forces_a_reprobe(
        self, monkeypatch, tmp_path
    ) -> None:
        """The feature the check exists for must survive the fix.

        An ENABLED server missing from the cache is genuinely new and still has
        to trigger a probe — the fix narrows the check, it does not remove it.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="just-installed", command="/bin/true"),
        ]
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        _arrange(monkeypatch, tmp_path, servers, cache)

        state = _State()
        await mcp_mod.api_mcp_servers(_request(state))

        # The handler creates a real asyncio task around the patched coroutine.
        assert state._background_tasks, "a genuinely new server must re-arm the probe"
        assert mcp_mod._mcp_probe_in_progress is True
        for task in list(state._background_tasks):
            await task

    @pytest.mark.asyncio
    async def test_disabled_row_is_still_returned_to_the_ui(
        self, monkeypatch, tmp_path
    ) -> None:
        """Skipping disabled rows in the freshness check must not hide them.

        The row still has to reach the response, otherwise the fix would trade
        the spawn treadmill for a missing table entry.
        """
        servers = [
            McpServerInfo(name="enabled-one", command="/bin/true", status="ok"),
            McpServerInfo(name="turned-off", command="/bin/true", disabled=True),
        ]
        cache = [{"name": "enabled-one", "status": "ok", "tools": [], "error": ""}]
        _arrange(monkeypatch, tmp_path, servers, cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))

        import json as _json

        rows = _json.loads(resp.text or "[]")
        names = [r["name"] for r in rows]
        assert "turned-off" in names, "a disabled server must still render a row"
        assert "enabled-one" in names
