"""PATCH /api/config/kirocrew write gate for ``dashboard.remote_editor.*``.

Split from ``test_config_patch.py`` (line cap); the app, fixture and PATCH
helper are the same minimal ones that file uses.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    state = request.app.get("state")
    if state is not None:
        state.owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auto_update": False}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


async def _patch(client, path, value):
    return await client.patch("/api/config/kirocrew", json={"path": path, "value": value})


class TestRemoteEditor:
    """Write gate for the remote-editor link config.

    ``editor`` is an enum, not a free string, because the value is interpolated
    into a clickable custom-scheme URL in the browser; ``host`` is charset-
    restricted so it cannot inject URL structure. Both are three segments deep,
    so this PATCH gate — not the load-time schema — is where a UI/CLI write is
    actually enforced.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("editor", ["", "vscode", "kiro"])
    async def test_valid_editor_persists_nested(self, tmp_config, editor) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.remote_editor.editor", editor)
            assert resp.status == 200
        data = json.loads(tmp_config.read_text())
        assert data["dashboard"]["remote_editor"]["editor"] == editor

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["emacs", "vim", "code", "VS Code", "vscode-insiders"])
    async def test_invalid_editor_rejected(self, tmp_config, bad) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.remote_editor.editor", bad)
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_valid_host_persists_nested(self, tmp_config) -> None:
        host = "dev-host.example.com"
        async with TestClient(TestServer(_make_app())) as c:
            resp = await _patch(c, "dashboard.remote_editor.host", host)
            assert resp.status == 200
        data = json.loads(tmp_config.read_text())
        assert data["dashboard"]["remote_editor"]["host"] == host

    @pytest.mark.asyncio
    async def test_empty_host_clears(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.remote_editor.host", "")).status == 200

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad",
        [
            "bad/host",  # path separator
            "host:2222",  # port colon
            "a host",  # whitespace
            "http://evil",  # scheme structure
            "host#frag",  # fragment
            "host?q=1",  # query
        ],
    )
    async def test_host_charset_rejected(self, tmp_config, bad) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.remote_editor.host", bad)).status == 400
        # Nothing persisted for a refused host.
        data = json.loads(tmp_config.read_text())
        assert "host" not in data.get("dashboard", {}).get("remote_editor", {})

    @pytest.mark.asyncio
    async def test_host_overlong_rejected(self, tmp_config) -> None:
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.remote_editor.host", "a" * 257)).status == 400

    @pytest.mark.asyncio
    async def test_editor_and_host_nest_together(self, tmp_config) -> None:
        # Two writes to the same sub-object must not clobber each other's key —
        # the same nesting guarantee the terminal completion test pins.
        async with TestClient(TestServer(_make_app())) as c:
            assert (await _patch(c, "dashboard.remote_editor.editor", "vscode")).status == 200
            assert (
                await _patch(c, "dashboard.remote_editor.host", "host.example.com")
            ).status == 200
        data = json.loads(tmp_config.read_text())
        assert data["dashboard"]["remote_editor"] == {
            "editor": "vscode",
            "host": "host.example.com",
        }
