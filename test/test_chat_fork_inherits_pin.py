"""A person's fork of a pinned session is pinned too.

The fork inherits the parent's folder and tags so it lands beside the parent in
the sidebar. A pinned parent sits in the pinned group, so the fork takes the pin
as well and stays in that group with it. The human route
(``POST /api/chat/slots/{slot}/fork``) carries the pin over; the agent
``session_fork`` path does not (see ``test_session_control_fork.py``).
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _seeded_state(tmp_path, *, pinned: bool):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("parent")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    slot.pinned = pinned
    return state


async def _fork(state, payload=None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json=payload or {})
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False])
async def test_the_fork_takes_the_parents_pin(tmp_path, monkeypatch, pinned) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, pinned=pinned)

    status, body = await _fork(state)

    assert status == 200
    assert body["pinned"] is pinned
    assert state._slots[body["key"]].pinned is pinned
    assert state._slots["parent"].pinned is pinned


@pytest.mark.asyncio
async def test_a_tail_fork_takes_the_pin_too(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, pinned=True)

    status, body = await _fork(state, {"at_message_index": 0, "direction": "tail"})

    assert status == 200
    assert state._slots[body["key"]].pinned is True


@pytest.mark.asyncio
async def test_the_pin_is_in_the_birth_save(tmp_path, monkeypatch) -> None:
    """The pin is set before the child's first save, so a restart that rehydrates
    the child from disk still finds it pinned."""
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history

    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path, pinned=True)

    status, body = await _fork(state)
    assert status == 200
    del state._slots[body["key"]]

    child = _rehydrate_slot_from_history(state, body["key"])

    assert child is not None
    assert child.pinned is True
