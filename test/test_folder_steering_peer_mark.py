"""A peer-backed conversation carries words that are not the person's.

``chat_folder_steering_set`` refuses a conversation foreign words have entered.
A session ADOPTED from a remote crew is a transcript authored on the peer, copied
into a fresh local slot by ``remote_adopt.apply_adopted_backfill``; and every
later turn of a remote-executor slot runs on that peer. Neither path set the mark,
so reading or forking such a slot handed its words to a clean local session that
the tool would then trust. These pins cover the adopt door, the slot predicate,
and the two propagation checks (fork, read).
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.remote_adopt import AdoptBackfill, apply_adopted_backfill


def _backfill(rows: int = 1) -> AdoptBackfill:
    return AdoptBackfill(
        rows=[
            {"role": "user", "content": f"peer row {i}", "cls": "msg msg-u", "ts": "", "meta": {}}
            for i in range(rows)
        ],
        notice="adopted from the peer",
    )


def test_adopting_a_peer_transcript_marks_the_slot(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("adopted")
    assert slot.carries_foreign_words() is False

    apply_adopted_backfill(slot, _backfill())

    assert slot._channel_turn_seen is True
    assert slot.to_dict()["channel_turn_seen"] is True


def test_adopting_an_empty_history_still_marks_the_slot(tmp_path):
    """Its later turns are peer turns whether or not rows came across."""
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("adopted")
    apply_adopted_backfill(slot, _backfill(rows=0))
    assert slot._channel_turn_seen is True


def test_a_remote_executor_slot_carries_foreign_words(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("remote")
    slot.executor = "remote"
    assert slot.carries_foreign_words() is True
    assert slot.to_dict()["channel_turn_seen"] is True


@pytest.mark.asyncio
async def test_a_fork_of_a_remote_executor_slot_is_marked(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("remote")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi from the peer", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    slot.executor = "remote"
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/remote/fork", json={})
        body = await resp.json()
    assert resp.status == 200, body
    assert state._slots[body["key"]]._channel_turn_seen is True


def test_the_read_and_send_checks_read_the_slot_predicate(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("remote")
    assert sc._carries_channel_words_in_memory(slot) is False
    slot.executor = "remote"
    assert sc._carries_channel_words_in_memory(slot) is True
