"""``dashboard.show_unread_dot``: the Settings → Display → Sidebar toggle.

On by default, so the per-row unread dot on sidebar session cards stays. A PATCH
stores a real boolean and the loader reads it back; a non-boolean is refused.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_config_patch import _make_app_with_state, _patch, tmp_config  # noqa: F401

from kiro_crew.config import KiroCrewConfig

KEY = "dashboard.show_unread_dot"


def test_defaults_to_shown(tmp_config) -> None:  # noqa: F811
    assert KiroCrewConfig().dashboard.show_unread_dot is True
    assert KiroCrewConfig.load().dashboard.show_unread_dot is True


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, True])
async def test_boolean_written_and_loaded(tmp_config, value) -> None:  # noqa: F811
    app, _ = _make_app_with_state()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, KEY, value)
        assert resp.status == 200
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["dashboard"]["show_unread_dot"] is value
    assert KiroCrewConfig.load().dashboard.show_unread_dot is value


@pytest.mark.asyncio
async def test_non_boolean_rejected(tmp_config) -> None:  # noqa: F811
    app, _ = _make_app_with_state()
    async with TestClient(TestServer(app)) as client:
        resp = await _patch(client, KEY, "false")
        assert resp.status == 400
