"""Regression: the handler's own probe cache must not re-serve an edited server.

``GET /api/mcp`` keeps a second probe cache, ``_mcp_probe_cache``, and prefers it
whenever the discovery cache reports ``outdated`` or ``unknown`` — that is how a
tool list survives the discovery TTL.

Both caches are keyed on the server NAME. So gating the discovery cache on a
config fingerprint accomplishes nothing on its own: a refused entry reports
``unknown``, which is precisely the status this overlay fires on, and the
previous target's tools go straight back onto the row one layer above the check
that just refused them. The row then renders ``ok`` with no ``probedAt``, an
undated Online, because the overlay copies status, tools and error but not the
timestamp.

The overlay has to apply the same identity check.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo, _cache_probe, _probe_cache


def _request(state) -> object:
    """Minimal stand-in for the aiohttp request the handler reads.

    Mirrors ``test_mcp_probe_treadmill``: the owner gate reads the request as a
    mapping as well as calling ``.get``, so the double carries the same reads
    holding the standalone-local owner claims.
    """

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


def _arrange(monkeypatch, tmp_path, servers, cache):
    """Point the handler at a synthetic server list and a warm probe cache."""
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: list(servers))
    monkeypatch.setattr(mcp_mod, "_GLOBAL_MCP_JSON", tmp_path / "absent.json")
    monkeypatch.setattr(mcp_mod, "_kirocrew_mcp_json", lambda: tmp_path / "absent-kc.json")
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", list(cache))
    # Warm: the TTL branch must not be what decides this test.
    monkeypatch.setattr(mcp_mod, "_mcp_probe_ts", time.time())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
    monkeypatch.setattr(mcp_mod, "_bg_mcp_probe", AsyncMock(return_value=None))


def _row(resp, name: str) -> dict:
    import json

    body = resp.body
    assert isinstance(body, (bytes, bytearray))
    rows = {r["name"]: r for r in json.loads(body)}
    return rows[name]


class TestHandlerOverlayRespectsProbeIdentity:
    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_edited_command_is_not_re_served_by_the_handler_cache(
        self, monkeypatch, tmp_path
    ) -> None:
        """Reverted (the overlay without ``cached_probe_is_current``), the row
        comes back ``ok`` carrying the previous command's tools."""
        # Probed under the OLD command, so the discovery entry's identity is the
        # old one. The handler cache holds that probe's serialized row.
        _cache_probe(
            McpServerInfo(name="srv", command="old-bin", status="ok", tools=["old_tool"])
        )
        cache = [{"name": "srv", "status": "ok", "tools": ["old_tool"], "error": ""}]
        # Current config runs a DIFFERENT command under the same name.
        servers = [McpServerInfo(name="srv", command="new-bin")]
        _arrange(monkeypatch, tmp_path, servers, cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))

        row = _row(resp, "srv")
        assert row["status"] == "unknown"
        assert row["tools"] == []

    @pytest.mark.asyncio
    async def test_matching_config_still_gets_the_overlay(self, monkeypatch, tmp_path) -> None:
        """The feature the overlay exists for has to survive the gate: an entry
        probed under the CURRENT config still outlives the discovery TTL."""
        _cache_probe(McpServerInfo(name="srv", command="bin", status="ok", tools=["t"]))
        # Age the discovery entry past its TTL so the row arrives "outdated"
        # and the overlay is the only thing that can report "ok".
        _probe_cache["srv"].probed_at = time.monotonic() - 10_000
        cache = [{"name": "srv", "status": "ok", "tools": ["t"], "error": ""}]
        servers = [McpServerInfo(name="srv", command="bin")]
        _arrange(monkeypatch, tmp_path, servers, cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))

        row = _row(resp, "srv")
        assert row["status"] == "ok"
        assert row["tools"] == ["t"]

    @pytest.mark.asyncio
    async def test_a_server_the_discovery_cache_never_saw_keeps_the_overlay(
        self, monkeypatch, tmp_path
    ) -> None:
        """No discovery entry is no claim to contradict. A handler-cached row
        for a server this module never recorded must not be dropped — that
        would trade the stale-tools bug for a disappearing row."""
        cache = [{"name": "srv", "status": "ok", "tools": ["t"], "error": ""}]
        servers = [McpServerInfo(name="srv", command="bin")]
        _arrange(monkeypatch, tmp_path, servers, cache)

        resp = await mcp_mod.api_mcp_servers(_request(_State()))

        assert _row(resp, "srv")["status"] == "ok"


def _count_arms(monkeypatch) -> list[int]:
    armed: list[int] = []
    monkeypatch.setattr(mcp_mod, "_arm_reprobe", lambda request: armed.append(1))
    monkeypatch.setattr(mcp_mod, "_mcp_reprobe_armed_for", {})
    return armed


class TestAnEditArmsOneReprobe:
    """Refusing the entry leaves the row empty; something has to refill it.

    The name is still in the handler cache, so the unseen-name check never
    fires, and the row would otherwise stay empty until the handler TTL lapses.
    """

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    def _edited(self, monkeypatch, tmp_path) -> None:
        _cache_probe(McpServerInfo(name="srv", command="old-bin", status="ok", tools=["t"]))
        cache = [{"name": "srv", "status": "ok", "tools": ["t"], "error": ""}]
        _arrange(monkeypatch, tmp_path, [McpServerInfo(name="srv", command="new-bin")], cache)

    @pytest.mark.asyncio
    async def test_an_edit_arms_once_and_a_probe_that_never_lands_does_not_re_arm(
        self, monkeypatch, tmp_path
    ) -> None:
        """A quarantined server is left out of the spawn set, so its entry
        stays mismatched. Re-arming on every request would run a full fan-out
        per page load for as long as that lasts."""
        self._edited(monkeypatch, tmp_path)
        armed = _count_arms(monkeypatch)

        await mcp_mod.api_mcp_servers(_request(_State()))
        assert armed == [1]

        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        await mcp_mod.api_mcp_servers(_request(_State()))
        assert armed == [1]

    @pytest.mark.asyncio
    async def test_a_second_edit_arms_again(self, monkeypatch, tmp_path) -> None:
        self._edited(monkeypatch, tmp_path)
        armed = _count_arms(monkeypatch)
        await mcp_mod.api_mcp_servers(_request(_State()))

        # The armed probe lands under new-bin, then the config moves again.
        _cache_probe(McpServerInfo(name="srv", command="new-bin", status="ok", tools=["t2"]))
        import kiro_crew.mcp_discovery as disc

        monkeypatch.setattr(
            disc, "list_servers", lambda *a, **k: [McpServerInfo(name="srv", command="third-bin")]
        )
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        await mcp_mod.api_mcp_servers(_request(_State()))

        assert armed == [1, 1]

    @pytest.mark.asyncio
    async def test_a_probe_already_in_flight_does_not_mark_the_edit_handled(
        self, monkeypatch, tmp_path
    ) -> None:
        """That probe may have read the config before the edit landed."""
        self._edited(monkeypatch, tmp_path)
        armed = _count_arms(monkeypatch)
        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", True)
        await mcp_mod.api_mcp_servers(_request(_State()))
        assert armed == []

        monkeypatch.setattr(mcp_mod, "_mcp_probe_in_progress", False)
        await mcp_mod.api_mcp_servers(_request(_State()))
        assert armed == [1]


class TestProbeEndpointRespectsProbeIdentity:
    """``GET /api/mcp/probe`` serves the same name-keyed handler cache whole."""

    def setup_method(self) -> None:
        _probe_cache.clear()

    def teardown_method(self) -> None:
        _probe_cache.clear()

    @pytest.mark.asyncio
    async def test_an_edited_server_is_left_out_and_re_probed(self, monkeypatch, tmp_path) -> None:
        import json

        _cache_probe(McpServerInfo(name="srv", command="old-bin", status="ok", tools=["old"]))
        _cache_probe(McpServerInfo(name="other", command="bin", status="ok", tools=["t"]))
        cache = [
            {"name": "srv", "status": "ok", "tools": ["old"], "error": ""},
            {"name": "other", "status": "ok", "tools": ["t"], "error": ""},
        ]
        servers = [
            McpServerInfo(name="srv", command="new-bin"),
            McpServerInfo(name="other", command="bin"),
        ]
        _arrange(monkeypatch, tmp_path, servers, cache)
        monkeypatch.setattr(mcp_mod, "_annotate_quarantine", lambda rows: None)
        armed = _count_arms(monkeypatch)

        resp = await mcp_mod.api_mcp_probe_cached(_request(_State()))

        body = resp.body
        assert isinstance(body, (bytes, bytearray))
        assert [r["name"] for r in json.loads(body)] == ["other"]
        assert armed == [1]
