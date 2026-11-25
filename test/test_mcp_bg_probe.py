"""Regression: the background MCP probe must use the bounded probe_all() path,
and must tell the open dashboard tabs when it has filled the cache.

Ported focused test (upstream appended it to the poolable-API
suite, which is not present in this fork).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo, _probe_cache

# ── background probe must use the bounded probe_all() path ──


class TestBgMcpProbeBoundedFanout:
    """``_bg_mcp_probe`` must respect the probe concurrency bound.

    Its own unbounded ``asyncio.gather`` over every configured server would
    bypass the ``PROBE_MAX_CONCURRENCY`` semaphore that ``probe_all()`` carries.
    Under a network blip that floods the loop's default executor and can starve
    the heartbeat into a watchdog ``_exit`` (full gateway restart). It MUST route
    through the bounded
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

        await mcp_mod._bg_mcp_probe(None)

        # The bounded path was used exactly once. Fails if an unbounded gather
        # over probe_server is ever reintroduced in _bg_mcp_probe.
        probe_all_mock.assert_awaited_once()
        assert [d["name"] for d in mcp_mod._mcp_probe_cache] == ["builder-mcp"]
        assert mcp_mod._mcp_probe_cache[0]["enabled"] is True
        assert mcp_mod._mcp_probe_in_progress is False


# ── a finished background probe tells the open tabs ──


class _State:
    """The dashboard state the probe and ``GET /api/mcp`` read, recording each push."""

    def __init__(self) -> None:
        self._background_tasks: set = set()
        self.sessions = SimpleNamespace(final_drain_started=False)
        self.pushed: list[tuple[str, ...]] = []

    def push_refresh(self, *kinds: str) -> None:
        self.pushed.append(kinds)


def _request(state: _State) -> SimpleNamespace:
    return SimpleNamespace(app={"state": state})


def _status(resp, name: str) -> str:
    return {r["name"]: r for r in json.loads(resp.text or "[]")}[name]["status"]


class TestFinishedProbeTellsTheOpenTab:
    """The MCP Servers tab reads ``GET /api/mcp`` once and never on a timer.

    That read arms the background probe for a server it has no verdict for, so
    the probe must tell the tab when it is done; otherwise the row says
    "Unknown" until a reload.
    """

    @pytest.fixture
    def probes(self, monkeypatch, tmp_path):
        import kiro_crew.mcp_discovery as disc

        calls: list[int] = []

        async def _probe_all():
            calls.append(1)
            return [McpServerInfo(name="qsrv", command="/bin/true", status="ok", tools=["hello"])]

        monkeypatch.setattr(
            disc, "list_servers", lambda *a, **k: [McpServerInfo(name="qsrv", command="/bin/true")]
        )
        monkeypatch.setattr(disc, "probe_all", _probe_all)
        monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
        monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
        monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
        monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", 0.0)
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        monkeypatch.setattr(mcp_mod, "_mcp_probe_task", None)
        monkeypatch.setattr(mcp_mod, "_mcp_reprobe_armed_for", {})
        _probe_cache.clear()
        yield calls
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_the_probe_a_listing_arms_pushes_once_and_the_re_read_arms_none(
        self, probes: list[int]
    ) -> None:
        state = _State()
        resp = await mcp_mod.api_mcp_servers(_request(state))
        assert _status(resp, "qsrv") == "unknown"
        assert state._background_tasks, "a server with no verdict must arm the probe"
        for task in list(state._background_tasks):
            await task

        assert state.pushed == [("mcp_servers",)]

        # The re-read that frame makes the tab do: the verdict, and no second probe.
        resp = await mcp_mod.api_mcp_servers(_request(state))
        assert _status(resp, "qsrv") == "ok"
        assert not state._background_tasks
        assert probes == [1]
        assert state.pushed == [("mcp_servers",)]

    @pytest.mark.asyncio
    async def test_a_probe_that_fails_pushes_nothing(self, probes, monkeypatch) -> None:
        """Its cache did not change, and a re-read would only arm it again."""
        import kiro_crew.mcp_discovery as disc

        monkeypatch.setattr(disc, "probe_all", AsyncMock(side_effect=RuntimeError("boom")))
        state = _State()

        await mcp_mod._bg_mcp_probe(state)

        assert state.pushed == []
        assert mcp_mod._mcp_probe_cache == []
