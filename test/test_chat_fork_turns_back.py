"""``POST /api/chat/slots/{slot}/fork`` with ``turns_back`` -- the ``/rewind [N]`` command.

``turns_back=N`` forks the session right before its Nth-last user message, so the
child holds the conversation as it stood before those N turns and the parent is
left untouched. These tests pin where that cut lands, and the refusals for a
count the session cannot satisfy or a body that names the fork point twice.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


def _three_turn_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("rw")
    for n in (1, 2, 3):
        slot.append("user", f"q{n}", f"msg msg-u{n}")
        slot.append("assistant", f"a{n}", f"msg msg-a{n}")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return state


async def _fork(state, payload):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/rw/fork", json=payload)
        return resp.status, await resp.json()


def _child_contents(state, key):
    return [
        m["content"] for m in state._slots[key].messages if m.get("role") in ("user", "assistant")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turns_back", "kept"),
    [
        (1, ["q1", "a1", "q2", "a2"]),
        (2, ["q1", "a1"]),
    ],
)
async def test_the_child_keeps_everything_before_the_nth_last_turn(
    tmp_path, monkeypatch, turns_back, kept
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _three_turn_state(tmp_path)
    status, body = await _fork(state, {"turns_back": turns_back})
    assert status == 200, body
    assert body["direction"] == "head"
    assert _child_contents(state, body["key"]) == kept
    # The parent is a fork source, never rewritten.
    assert _child_contents(state, "rw") == ["q1", "a1", "q2", "a2", "q3", "a3"]


@pytest.mark.asyncio
async def test_going_back_to_the_first_turn_keeps_nothing_and_is_refused(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_three_turn_state(tmp_path), {"turns_back": 3})
    assert status == 400
    assert body["code"] == "no_messages_before_turn"


@pytest.mark.asyncio
async def test_more_turns_than_the_session_has_is_refused_with_the_count(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_three_turn_state(tmp_path), {"turns_back": 4})
    assert status == 400
    assert body["code"] == "turns_back_out_of_range"
    assert "only 3 of your messages" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, True, "2", 1.5, 10_001])
async def test_a_turns_back_that_is_not_a_small_positive_integer_is_refused(
    tmp_path, monkeypatch, bad
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_three_turn_state(tmp_path), {"turns_back": bad})
    assert status == 400
    assert body["code"] == "invalid_field_type"


@pytest.mark.asyncio
@pytest.mark.parametrize("other", [{"at_message_index": 1}, {"at_message_id": "msg-a1"}])
async def test_turns_back_cannot_be_combined_with_another_fork_point(
    tmp_path, monkeypatch, other
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_three_turn_state(tmp_path), {"turns_back": 1, **other})
    assert status == 400
    assert body["code"] == "conflicting_fork_point"


@pytest.mark.asyncio
async def test_turns_back_is_head_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_three_turn_state(tmp_path), {"turns_back": 1, "direction": "tail"})
    assert status == 400
    assert body["code"] == "invalid_direction"


@pytest.mark.asyncio
async def test_a_mid_turn_steer_does_not_count_as_a_turn(tmp_path, monkeypatch) -> None:
    """A steer redirects the turn it lands in; ``/rewind 1`` drops that whole turn."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("rw")
    slot.append("user", "q1", "msg msg-u")
    slot.append("assistant", "a1", "msg msg-a")
    slot.append("user", "q2", "msg msg-u")
    slot.append("user", "steer q2", "msg msg-u", meta={"steer": True})
    slot.append("assistant", "a2", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    status, body = await _fork(state, {"turns_back": 1})
    assert status == 200, body
    assert _child_contents(state, body["key"]) == ["q1", "a1"]
