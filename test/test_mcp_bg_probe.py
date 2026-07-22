"""Regression: the background MCP probe must use the bounded probe_all() path.

Ported focused test (upstream appended it to the poolable-API
suite, which is not present in this fork).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod

# ── background probe must use the bounded probe_all() path ──


class TestBgMcpProbeBoundedFanout:
    """Regression guard.

    ``_bg_mcp_probe`` previously ran its own unbounded ``asyncio.gather`` over
    every configured server, bypassing the ``_PROBE_MAX_CONCURRENCY`` semaphore
    that ``probe_all()`` carries (the fix). Under a network blip that
    floods the loop's default executor and can starve the heartbeat into a
    watchdog ``_exit`` (full gateway restart). It MUST route through the bounded
    ``probe_all()`` path.
    """

    @pytest.mark.asyncio
    async def test_routes_through_probe_all(self, monkeypatch, tmp_path) -> None:
        import kiro_crew.mcp_discovery as disc

        srv = MagicMock()
        srv.name = "builder-mcp"
        srv.to_dict.return_value = {"name": "builder-mcp", "status": "ok"}
        probe_all_mock = AsyncMock(return_value=[srv])
        # _bg_mcp_probe does `from kiro_crew.mcp_discovery import probe_all`
        # at call time, so patching the attribute on the module is picked up.
        monkeypatch.setattr(disc, "probe_all", probe_all_mock)

        # No global mcp.json -> enabled/disabledTools overlay is a no-op.
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
        # Patch module probe state so the test doesn't leak into other tests.
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)

        await mcp_mod._bg_mcp_probe()

        # The bounded path was used exactly once. Fails if an unbounded gather
        # over probe_server is ever reintroduced in _bg_mcp_probe.
        probe_all_mock.assert_awaited_once()
        assert [d["name"] for d in mcp_mod._mcp_probe_cache] == ["builder-mcp"]
        assert mcp_mod._mcp_probe_cache[0]["enabled"] is True
        assert mcp_mod._mcp_probe_in_progress is False
