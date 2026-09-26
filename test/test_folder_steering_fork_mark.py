"""A fork carries the parent's channel mark to the child.

``chat_folder_steering_set`` refuses a conversation that channel words have
entered (``_ChatSlot._channel_turn_seen``, or ``channel_origin`` folded in by the
slot projection). A fork copies the parent's transcript -- rows a linked thread
may have authored -- into a fresh slot with no links, so if the child were born
unmarked a tainted-then-unlinked tab could fork itself and pass the refusal from
the copy. These pins drive the real fork endpoint.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _seeded_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("forkable")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return state


async def _fork(state, slot: str, payload) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot}/fork", json=payload)
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["head", "tail"])
async def test_a_fork_of_a_marked_conversation_is_marked(tmp_path, monkeypatch, direction) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    state._slots["forkable"]._channel_turn_seen = True
    payload = {} if direction == "head" else {"direction": "tail", "at_index": 0}
    status, body = await _fork(state, "forkable", payload)
    assert status == 200, body
    child = state._slots[body["key"]]
    assert child._channel_turn_seen is True
    assert child.to_dict()["channel_turn_seen"] is True
    assert not child.to_dict().get("links")


@pytest.mark.asyncio
async def test_a_fork_of_a_channel_origin_tab_is_marked(tmp_path, monkeypatch) -> None:
    """A tab born to display a channel transcript taints its fork the same way."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    state._slots["forkable"].channel_origin = True
    status, body = await _fork(state, "forkable", {})
    assert status == 200, body
    assert state._slots[body["key"]].to_dict()["channel_turn_seen"] is True


@pytest.mark.asyncio
async def test_a_fork_of_a_clean_conversation_stays_clean(tmp_path, monkeypatch) -> None:
    """Contrast: the mark is inherited, never invented."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    status, body = await _fork(state, "forkable", {})
    assert status == 200, body
    assert state._slots[body["key"]].to_dict()["channel_turn_seen"] is False
