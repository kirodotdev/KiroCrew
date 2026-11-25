"""``dashboard.expand_linked_items``: the Settings → Display → Session Cards toggle.

Off by default, so every session card keeps its linked-items strip folded. A
PATCH stores a real boolean and the loader reads it back; a non-boolean is
refused.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_config_patch import _make_app_with_state, _patch, tmp_config  # noqa: F401

from kiro_crew.config import KiroCrewConfig

KEY = "dashboard.expand_linked_items"


def test_defaults_to_folded(tmp_config) -> None:  # noqa: F811
    assert KiroCrewConfig().dashboard.expand_linked_items is False
    assert KiroCrewConfig.load().dashboard.expand_linked_items is False


@pytest.mark.asyncio
async def test_patch_stores_a_boolean_and_refuses_a_string(tmp_config) -> None:  # noqa: F811
    # One server for the whole round trip: the write, the read-back, the refusal.
    app, _ = _make_app_with_state()
    async with TestClient(TestServer(app)) as client:
        assert (await _patch(client, KEY, True)).status == 200
        assert (await _patch(client, KEY, "false")).status == 400
    data = json.loads(tmp_config.read_text(encoding="utf-8"))
    assert data["dashboard"]["expand_linked_items"] is True
    assert KiroCrewConfig.load().dashboard.expand_linked_items is True
