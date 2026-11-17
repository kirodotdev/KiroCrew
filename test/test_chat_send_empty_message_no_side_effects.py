"""An empty POST /api/chat is refused before it creates or changes any slot.

A refused send must leave no new empty chat in the sidebar (one per retry when
no slot is named) and must not write the request's agent onto an agentless slot.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

pytestmark = pytest.mark.asyncio


async def test_empty_send_creates_no_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    before = set(state._slots)
    async with TestClient(TestServer(_make_app(state))) as client:
        named = await client.post("/api/chat?ws=1", json={"message": "   ", "slot": "fresh-x"})
        unnamed = await client.post("/api/chat?ws=1", json={"message": ""})
        assert named.status == 400
        assert unnamed.status == 400
        assert (await named.json()).get("code") == "message_required"
    assert set(state._slots) == before


async def test_empty_send_does_not_bind_agent(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("s1")
    slot.agent = ""
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat?ws=1", json={"message": "", "slot": "s1", "agent": "kirocrew"}
        )
        assert resp.status == 400
    assert slot.agent == ""
