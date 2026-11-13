"""A ``routed`` row on ``GET /api/mcp`` tracks the current gateway routing.

A ``secret://`` server the running gateway serves reports ``routed`` from the
probe, and the MCP tab reads it through the handler's own probe cache, which
holds for ``_MCP_PROBE_CACHE_SECS``. Routing can be turned off inside that
window, and nothing re-probes on it, so the handler asks again on every read
that carries a ``routed`` row.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache

_ROUTED_NAMES = "kiro_crew.agent_materialization.mcp_sources._gateway_routed_names"


def _request(state) -> object:
    class _App(dict):
        pass

    class _Req(dict):
        def __init__(self, app):
            super().__init__(user="local-app", app="")
            self.app = app

    app = _App()
    app["state"] = state
    return _Req(app)


class _State:
    def __init__(self) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=False)


def _arrange(monkeypatch, tmp_path, servers, cache):
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))


def _row(resp, name: str) -> dict:
    import json

    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    return {r["name"]: r for r in json.loads(body)}[name]


_SERVER = McpServerInfo(name="srv", command="srv-mcp", env={"API_TOKEN": "secret://NAME"})
_ROUTED_ROW = {"name": "srv", "status": "routed", "tools": [], "error": ""}


class TestRoutedRowOnTheListEndpoint:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_a_routed_probe_result_reaches_the_row(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(_ROUTED_NAMES, lambda: frozenset({"srv"}))
        _arrange(monkeypatch, tmp_path, [_SERVER], [_ROUTED_ROW])

        row = _row(await mcp_mod.api_mcp_servers(_request(_State())), "srv")

        assert row["status"] == "routed"
        assert row["error"] == ""

    @pytest.mark.asyncio
    async def test_routing_turned_off_returns_the_not_probed_row(
        self, monkeypatch, tmp_path
    ) -> None:
        """The cached ``routed`` answer is not served once the gateway stops
        routing the server: the row reads as the probe would now report it."""
        monkeypatch.setattr(_ROUTED_NAMES, lambda: frozenset())
        _arrange(monkeypatch, tmp_path, [_SERVER], [_ROUTED_ROW])

        row = _row(await mcp_mod.api_mcp_servers(_request(_State())), "srv")

        assert row["status"] == "outdated"
        assert row["error"].startswith("not probed: env API_TOKEN")
        assert "NAME" not in row["error"].replace("API_TOKEN", "")

    @pytest.mark.asyncio
    async def test_unreadable_routing_is_not_routed(self, monkeypatch, tmp_path) -> None:
        def _boom() -> frozenset[str]:
            raise RuntimeError("overlay unreadable")

        monkeypatch.setattr(_ROUTED_NAMES, _boom)
        _arrange(monkeypatch, tmp_path, [_SERVER], [_ROUTED_ROW])

        row = _row(await mcp_mod.api_mcp_servers(_request(_State())), "srv")

        assert row["status"] == "outdated"

    @pytest.mark.asyncio
    async def test_no_routed_row_reads_no_routing(self, monkeypatch, tmp_path) -> None:
        """The routing read is paid only when a row reads ``routed``."""

        def _unexpected() -> frozenset[str]:
            raise AssertionError("routing read with no routed row")

        monkeypatch.setattr(_ROUTED_NAMES, _unexpected)
        cache = [{"name": "srv", "status": "ok", "tools": ["t"], "error": ""}]
        _arrange(monkeypatch, tmp_path, [McpServerInfo(name="srv", command="bin")], cache)

        row = _row(await mcp_mod.api_mcp_servers(_request(_State())), "srv")

        assert row["status"] == "ok"
