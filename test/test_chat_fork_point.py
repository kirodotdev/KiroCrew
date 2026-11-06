"""A fork records where in its parent it was cut.

The child keeps the ``meta.mid`` and ``ts`` of the parent row the cut sits after,
and the direction, so the parent's transcript can mark the point and link the
child. Both fork routes share ``chat_fork.fork_slot``: the dashboard's
``POST /api/chat/slots/{slot}/fork`` and the agent's ``session_fork``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key


def _parent(state, *, rows: int = 4):
    slot = state.get_or_create_slot("src")
    for i in range(rows):
        role = "user" if i % 2 == 0 else "assistant"
        slot.append(role, f"row {i}", f"msg msg-{role[0]}", ts=f"2026-10-29T10:00:0{i}Z")
    slot.drain()
    return slot


def _point(slot) -> tuple:
    return (slot.fork_point_mid, slot.fork_point_ts, slot.fork_direction)


async def _fork_over_http(state, body: dict) -> dict:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/src/fork", json=body)
        assert resp.status == 200, await resp.text()
        return await resp.json()


@pytest.mark.asyncio
async def test_a_fork_at_a_message_records_that_row(tmp_path):
    state = _make_state(tmp_path)
    parent = _parent(state)
    target = parent.messages[1]

    data = await _fork_over_http(state, {"at_message_id": target["meta"]["mid"]})

    child = state._slots[data["key"]]
    assert _point(child) == (target["meta"]["mid"], target["ts"], "head")


@pytest.mark.asyncio
async def test_a_whole_transcript_fork_records_the_last_row(tmp_path):
    state = _make_state(tmp_path)
    parent = _parent(state)

    data = await _fork_over_http(state, {})

    last = parent.messages[-1]
    assert _point(state._slots[data["key"]]) == (last["meta"]["mid"], last["ts"], "head")


@pytest.mark.asyncio
async def test_a_tail_fork_records_the_row_it_was_cut_after(tmp_path, monkeypatch):
    """A tail fork's first copied row is AFTER the point; the point is the parent
    row it was cut at, the same row a head fork at that index would end on."""
    cfg = MagicMock()
    cfg.dashboard.tail_fork_enabled = True
    monkeypatch.setattr("kiro_crew.dashboard.chat_fork.KiroCrewConfig.load", lambda: cfg)
    state = _make_state(tmp_path)
    parent = _parent(state)
    cut = parent.messages[1]

    data = await _fork_over_http(state, {"at_message_index": 1, "direction": "tail"})

    child = state._slots[data["key"]]
    assert _point(child) == (cut["meta"]["mid"], cut["ts"], "tail")
    assert child.messages[0]["content"] == "row 2"


@pytest.mark.asyncio
async def test_a_row_without_an_id_leaves_only_its_timestamp(tmp_path):
    """Rows saved before ids were minted have none; the point is then found by ts."""
    state = _make_state(tmp_path)
    parent = _parent(state, rows=2)
    for row in parent.messages:
        row["meta"].pop("mid", None)
    parent._dirty = True

    data = await _fork_over_http(state, {})

    assert _point(state._slots[data["key"]]) == (None, parent.messages[-1]["ts"], "head")


@pytest.mark.asyncio
async def test_an_out_of_bound_row_id_is_not_recorded(tmp_path):
    """The parent's rows can come from an agent-written transcript; the fork bounds
    what it records the same way a restore does, so nothing live outruns the read."""
    state = _make_state(tmp_path)
    parent = _parent(state, rows=2)
    parent.messages[-1]["meta"]["mid"] = "m" * 129
    parent._dirty = True

    data = await _fork_over_http(state, {})

    assert _point(state._slots[data["key"]]) == (None, parent.messages[-1]["ts"], "head")


@pytest.mark.asyncio
async def test_the_point_is_in_the_childs_birth_save_and_its_slot_row(tmp_path):
    state = _make_state(tmp_path)
    parent = _parent(state)
    target = parent.messages[2]

    data = await _fork_over_http(state, {"at_message_id": target["meta"]["mid"]})

    child = state._slots[data["key"]]
    meta = state.conversation_log.get_metadata(slot_history_key(child))
    expected = {
        "fork_point_mid": target["meta"]["mid"],
        "fork_point_ts": target["ts"],
        "fork_direction": "head",
    }
    assert {k: meta.get(k) for k in expected} == expected
    row = child.to_dict()
    assert {k: row[k] for k in expected} == expected
    assert row["forked_from"] == "dashboard:src"


def test_a_slot_that_is_not_a_fork_projects_no_point(tmp_path):
    state = _make_state(tmp_path)
    row = _parent(state).to_dict()
    assert (row["fork_point_mid"], row["fork_point_ts"], row["fork_direction"]) == (
        None,
        None,
        None,
    )


def test_an_agent_session_fork_records_the_point_too(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)
    monkeypatch.setattr(chat_fork, "sel", lambda: MagicMock())
    monkeypatch.setattr(sc, "sel", lambda: MagicMock())
    # The per-caller create budget is process-wide; a fork spends it.
    create_rate_limit.reset_for_tests()
    state = _make_state(tmp_path)
    caller = state.get_or_create_slot("chat-1")
    for i in range(2):
        caller.append("user", f"question {i}", "msg msg-u")
        caller.append("assistant", f"answer {i}", "msg msg-a")
    caller.drain()
    _save_slot_to_history(state, caller, closed=False)
    caller._resumed_count = caller._disk_window_len = len(caller.messages)
    caller._dirty = False
    target = caller.messages[2]

    try:
        result = asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(caller), at_message_index=2)
        )
    finally:
        create_rate_limit.reset_for_tests()

    child = state.get_slot(result["target"])
    assert _point(child) == (target["meta"]["mid"], target["ts"], "head")
