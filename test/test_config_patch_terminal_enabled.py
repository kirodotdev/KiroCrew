"""PATCH /api/config/kirocrew for the Terminal panel switch (``dashboard.terminal.enabled``).

A sibling of ``test_config_patch.py`` (which is at the file line cap): the owner-only
Settings → Display → Terminal switch writes the same key the config file holds, and
its teardown is the live config watcher's ``apply_terminal_enabled``.
"""

from __future__ import annotations

import json
from test.test_config_patch import _make_app_with_state, _patch, _seed_config
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(_seed_config()), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


class TestTerminalEnabled:
    """The Settings → Display → Terminal panel switch, owner-only."""

    @pytest.mark.asyncio
    async def test_owner_false_written(self, tmp_config) -> None:
        app, _ = _make_app_with_state()
        async with TestClient(TestServer(app)) as client:
            resp = await _patch(client, "dashboard.terminal.enabled", False)
            assert resp.status == 200
        data = json.loads(tmp_config.read_text())
        assert data["dashboard"]["terminal"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_non_boolean_rejected(self, tmp_config) -> None:
        app, _ = _make_app_with_state()
        async with TestClient(TestServer(app)) as client:
            resp = await _patch(client, "dashboard.terminal.enabled", "false")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_owner_refused_without_a_write(self, tmp_config) -> None:
        before = tmp_config.read_bytes()
        with patch(
            "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
            return_value=False,
        ):
            app, _ = _make_app_with_state()
            async with TestClient(TestServer(app)) as client:
                resp = await _patch(client, "dashboard.terminal.enabled", False)
                assert resp.status == 403
        assert tmp_config.read_bytes() == before

    @pytest.mark.asyncio
    async def test_overlay_pinned_key_refused(self, tmp_config) -> None:
        from kiro_crew.config.loader import config_dir

        local = config_dir() / "config.local.json"
        local.write_text(
            json.dumps({"dashboard": {"terminal": {"enabled": False}}}), encoding="utf-8"
        )
        before = tmp_config.read_bytes()
        app, _ = _make_app_with_state()
        async with TestClient(TestServer(app)) as client:
            resp = await _patch(client, "dashboard.terminal.enabled", True)
            assert resp.status == 409
            assert (await resp.json())["code"] == "config_overlay_owned"
        assert tmp_config.read_bytes() == before

    def test_spec_is_a_plain_bool(self) -> None:
        from kiro_crew.dashboard.handlers.core_config_schema import _editable_config_spec

        assert _editable_config_spec("dashboard.terminal.enabled") == {"type": "bool"}

    def test_the_live_applier_the_switch_relies_on_exists(self) -> None:
        # An off-flip from Settings ends every shell through the live config
        # watcher's applier; without it the switch would only refuse new shells
        # after the 30 s gate cache and leave running ones for hours.
        from kiro_crew.dashboard.handlers.terminal_lifecycle import apply_terminal_enabled

        assert callable(apply_terminal_enabled)
