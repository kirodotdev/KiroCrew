"""A queued send keeps its client ``meta.sendId`` through the drain.

A dispatched send persists the client's ``meta.sendId`` on its user row; a send
that arrived while the slot was busy went through the queue instead, and the
queue entry carried only the containment snapshot -- so the row the drain wrote
was id-less. A client that had to prove ITS message landed (an unconfirmed-send
notice deciding whether to retire) had nothing to match on but text, which a
same-text resend or an injection can share.

These pins cover the producer (``/api/chat`` busy-slot and sub-agent-hold
branches stamp the id onto the entry), the leg the fix relies on (the drain's
meta union carries it onto the row), and the merged-row shape (``sendIds``
names every send a merged row stands for).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

_TEXT = "queued while busy"
_SEND_ID = "s-m4k2p1-9x7"


async def _post_busy(state, slot_key: str, message: str, meta: dict | None):
    body: dict = {"message": message, "slot": slot_key}
    if meta is not None:
        body["meta"] = meta
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat", json=body)
        assert resp.status == 200
        payload = await resp.json()
        assert payload.get("queued") is True
        return payload


async def _drain_once(state, slot) -> None:
    from kiro_crew.dashboard import chat_runner

    with (
        patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
        patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
    ):
        assert await chat_runner._start_next_queued_turn(state, slot) is True


def _user_rows(slot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "user"]


class TestBusySlotQueueEntry:
    @pytest.mark.asyncio
    async def test_entry_carries_the_client_send_id(self, tmp_path, monkeypatch):
        """The producer half: the busy-slot queue branch stamps `sendId` onto the
        entry, next to the containment snapshot it already carried."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True  # force the busy queue path
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        payload = await _post_busy(state, "busy-chat", _TEXT, {"sendId": _SEND_ID})

        entry = next(i for i in slot._queue if i["content"] == _TEXT)
        assert entry["meta"].get("sendId") == _SEND_ID, (
            "the drain unions entry meta onto the row it writes, so the entry is the "
            "only place the id can be put for a send that never persists its own row"
        )
        # The receipt contract is unchanged: `queue_id` still names this entry.
        assert payload.get("queue_id") == entry["id"]

    @pytest.mark.asyncio
    async def test_a_send_without_an_id_keeps_the_prior_entry_shape(self, tmp_path, monkeypatch):
        """Additive, not mandatory: an old client's POST carries no id.

        Pinned as an ABSENT KEY rather than a falsy value -- an empty string would
        travel to the row and give a client an id that matches nothing.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        await _post_busy(state, "busy-chat", _TEXT, None)

        entry = next(i for i in slot._queue if i["content"] == _TEXT)
        assert "sendId" not in entry["meta"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_id",
        [
            "",  # empty
            "has.dots.like/a.token=",  # outside the id alphabet
            "x" * 129,  # over SEND_ID_MAX_LEN
            42,  # not a string
        ],
    )
    async def test_an_unusable_id_is_treated_as_absent(self, tmp_path, monkeypatch, bad_id):
        """Same deny-by-default gate as the steer path (`normalize_send_id`): raw
        client input that fails the shape check is dropped, never truncated or
        rewritten -- a rewritten id would silently mismatch the client's copy."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        await _post_busy(state, "busy-chat", _TEXT, {"sendId": bad_id})

        entry = next(i for i in slot._queue if i["content"] == _TEXT)
        assert "sendId" not in entry["meta"]


class TestSubagentHoldQueueEntry:
    @pytest.mark.asyncio
    async def test_entry_carries_the_client_send_id(self, tmp_path, monkeypatch):
        """The idle-slot hold (background sub-agents still running) is the other
        queue producer a composer send can reach; it stamps the id the same way."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["agent-1"])
        slot = state.get_or_create_slot("held-chat")
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        payload = await _post_busy(state, "held-chat", _TEXT, {"sendId": _SEND_ID})

        entry = next(i for i in slot._queue if i["content"] == _TEXT)
        assert entry["meta"].get("sendId") == _SEND_ID
        assert payload.get("queue_id") == entry["id"]


class TestDrainedRow:
    @pytest.mark.asyncio
    async def test_drained_row_carries_the_send_id(self, tmp_path, monkeypatch):
        """End to end: the id on the entry is worth nothing on its own -- the row
        is what a client reads back off the slot detail."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        await _post_busy(state, "busy-chat", _TEXT, {"sendId": _SEND_ID})

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows, "the drain must have written a user row for the queued send"
        meta = rows[-1].get("meta") or {}
        assert meta.get("sendId") == _SEND_ID
        # The single-entry case keeps the plain-send shape: one `sendId`, no list.
        assert "sendIds" not in meta
        # Queue plumbing must not ride into the persisted row.
        from kiro_crew.dashboard.session_control import QUEUED_CONTAINMENT_META_KEY

        assert QUEUED_CONTAINMENT_META_KEY not in meta

    @pytest.mark.asyncio
    async def test_drained_row_without_an_id_has_no_send_id_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("busy-chat")
        slot._in_stage_execution = True
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())

        await _post_busy(state, "busy-chat", _TEXT, None)

        state.subagents = None
        slot._in_stage_execution = False
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows
        assert "sendId" not in (rows[-1].get("meta") or {})
        assert "sendIds" not in (rows[-1].get("meta") or {})

    @pytest.mark.asyncio
    async def test_merged_row_names_every_send_id(self, tmp_path, monkeypatch):
        """With `merge_queued_messages` on, several queued sends fold into one row.
        That row stands for all of them, so it has to name all of them: `sendIds`
        lists each in queue order, and `sendId` is still present (the union's
        last writer) so the key is never absent when any entry had one."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.subagents = None
        slot = state.get_or_create_slot("busy-chat")

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        queue_for_next_turn(state, slot, "first", directive_user_origin=True, send_id="s-aaa-1")
        queue_for_next_turn(state, slot, "second", directive_user_origin=True, send_id="s-bbb-2")
        # A third entry from an old client carries no id and must not poison the list.
        queue_for_next_turn(state, slot, "third", directive_user_origin=True, send_id=None)

        cfg = MagicMock()
        cfg.load.return_value.dashboard.merge_queued_messages = True
        monkeypatch.setattr(chat_runner, "KiroCrewConfig", cfg)
        await _drain_once(state, slot)

        rows = _user_rows(slot)
        assert rows
        meta = rows[-1].get("meta") or {}
        assert meta.get("sendIds") == ["s-aaa-1", "s-bbb-2"]
        assert meta.get("sendId") in ("s-aaa-1", "s-bbb-2")
        assert "3 queued messages merged" in rows[-1].get("content", "")


class TestQueueForNextTurnSeam:
    def test_send_id_is_stamped_only_when_given(self, tmp_path, monkeypatch):
        """The helper's own contract, independent of the HTTP handler."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("seam")

        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        qid_with = queue_for_next_turn(state, slot, "with id", send_id=_SEND_ID)
        qid_without = queue_for_next_turn(state, slot, "without id")

        by_id = {i["id"]: i for i in slot._queue}
        assert by_id[qid_with]["meta"].get("sendId") == _SEND_ID
        assert "sendId" not in by_id[qid_without]["meta"]
        # The queue_push announce shape is unchanged: no id leaks onto the broadcast.
        for call in state.broadcast_ws.call_args_list:
            event, payload = call.args
            assert event == "queue_push"
            assert "sendId" not in payload
