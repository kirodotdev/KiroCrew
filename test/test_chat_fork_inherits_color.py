"""A fork keeps its parent's sidebar color, and the person can still recolor it.

Color is how a person tells groups of sessions apart in the sidebar, so a fork
inherits the palette index or the custom hex, whichever the parent has, and a
later recolor of the fork leaves the parent alone.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _seeded_state(tmp_path, *, color_index, color_hex):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("parent")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    slot.color_index = color_index
    slot.color_hex = color_hex
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "color_index, color_hex",
    [(3, None), (None, "#12ab34"), (None, None)],
    ids=["palette", "custom", "uncolored"],
)
async def test_the_fork_takes_the_parents_color(
    tmp_path, monkeypatch, color_index, color_hex
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, color_index=color_index, color_hex=color_hex)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        body = await resp.json()

    assert resp.status == 200
    child = state._slots[body["key"]]
    assert (child.color_index, child.color_hex) == (color_index, color_hex)


@pytest.mark.asyncio
async def test_the_color_is_in_the_birth_save(tmp_path, monkeypatch) -> None:
    """A restart that rehydrates the fork from disk still finds it colored."""
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, color_index=None, color_hex="#12ab34")

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        body = await resp.json()
    assert resp.status == 200
    del state._slots[body["key"]]

    child = _rehydrate_slot_from_history(state, body["key"])

    assert child is not None
    assert child.color_hex == "#12ab34"


@pytest.mark.asyncio
async def test_recoloring_the_fork_leaves_the_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, color_index=3, color_hex=None)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json={})
        key = (await resp.json())["key"]
        assert state._slots[key].color_index == 3
        recolor = await client.patch(f"/api/chat/slots/{key}/color", json={"color_index": 5})

    assert recolor.status == 200
    assert state._slots[key].color_index == 5
    assert state._slots["parent"].color_index == 3
