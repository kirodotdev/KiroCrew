"""A message the session's own human typed while a turn was running is shown as typed.

An ordinary send's row is stored and served unredacted. The same text sent while
the slot is busy -- queued, or steered into
the running turn -- follows that rule on its pending card, its cancel restore and
its steer row. These pins cover both halves: the composer's text round-trips as
typed there, and every other origin (a ``session_send`` peer, an app, a channel,
a restored entry) keeps the redaction. The drain's row and ``queue_pop`` frame
stay redacted, matching the text the drain hands the next turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard.chat import api_chat_slot_queue_cancel

_SECRET = "AKIAIOSFODNN7EXAMPLE"
_TYPED = f"use https://docs.example.com/page?key={_SECRET} for this"


@pytest.fixture
def _patch_sel():
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
        yield


def _frames(state, kind: str) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == kind]


def _steer_capable_slot(state, key: str = "test"):
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=True)
    slot._acp_client = client
    return slot


class TestDisplayHelper:
    def test_user_origin_text_is_returned_as_typed(self):
        from kiro_crew.dashboard.chat_delivery import queued_text_for_display

        assert queued_text_for_display(_TYPED, user_origin=True) == _TYPED

    def test_any_other_origin_is_redacted(self):
        from kiro_crew.dashboard.chat_delivery import queued_text_for_display

        assert _SECRET not in queued_text_for_display(_TYPED, user_origin=False)

    def test_entry_origin_reads_only_the_composer_stamp(self):
        from kiro_crew.dashboard.chat_delivery import queue_entry_is_user_origin

        assert queue_entry_is_user_origin({"_directive_user_origin": True}) is True
        assert queue_entry_is_user_origin({"_directive_channel_origin": True}) is False
        # A linked Slack channel's message carries BOTH stamps; its author is not
        # the dashboard's reader, so it stays redacted.
        assert (
            queue_entry_is_user_origin(
                {"_directive_user_origin": True, "_directive_channel_origin": True}
            )
            is False
        )
        # A recovery requeue inherits the turn's user stamp but its text is
        # host-built (tool titles, redirect targets), so any producer kind
        # keeps it redacted.
        assert (
            queue_entry_is_user_origin(
                {"_directive_user_origin": True, "kind": "synthetic_recovery"}
            )
            is False
        )
        assert queue_entry_is_user_origin({}) is False
        assert queue_entry_is_user_origin(None) is False


class TestQueueEgress:
    def test_queue_entry_view_follows_the_entry_origin(self):
        from kiro_crew.dashboard.chat_delivery import queue_entry_view

        mine = queue_entry_view({"id": "q1", "content": _TYPED, "_directive_user_origin": True})
        peer = queue_entry_view({"id": "q2", "content": _TYPED})
        assert mine["content"] == _TYPED
        assert _SECRET not in peer["content"]

    @pytest.mark.parametrize("user_origin", [True, False])
    def test_queue_push_follows_the_caller_origin(self, user_origin, tmp_path):
        from kiro_crew.dashboard.chat_delivery import queue_for_next_turn

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        state.broadcast_ws = MagicMock()
        with (
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        ):
            queue_for_next_turn(state, slot, _TYPED, directive_user_origin=user_origin)
        (frame,) = _frames(state, "queue_push")
        if user_origin:
            assert frame["content"] == _TYPED
        else:
            assert _SECRET not in frame["content"]
        # Either way the queued entry itself is the delivery payload, unchanged.
        assert slot._queue[-1]["content"] == _TYPED

    @pytest.mark.asyncio
    async def test_cancel_restores_the_users_own_text(self, tmp_path, monkeypatch, _patch_sel):
        """The cancel frame is what the composer is refilled from, so a redacted
        copy would replace the link the user typed with a placeholder."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        mine = slot.queue_append(_TYPED, directive_user_origin=True)
        peer = slot.queue_append(_TYPED)

        app = web.Application()
        app["state"] = state
        app.router.add_delete("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_cancel)
        async with TestClient(TestServer(app)) as client:
            resp_mine = await client.delete(f"/api/chat/slots/test/queue/{mine}")
            resp_peer = await client.delete(f"/api/chat/slots/test/queue/{peer}")
            assert (await resp_mine.json())["content"] == _TYPED
            assert _SECRET not in (await resp_peer.json())["content"]

        by_id = {f["queue_id"]: f["content"] for f in _frames(state, "queue_cancel")}
        assert by_id[mine] == _TYPED
        assert _SECRET not in by_id[peer]

    @pytest.mark.asyncio
    async def test_slot_detail_reload_follows_the_entry_origin(self, tmp_path):
        """A page reload rebuilds the queue from GET slot detail, whose snapshot
        must carry the origin stamps or the user's own card turns redacted."""
        state = _make_state(tmp_path)
        state.push_slots_update = lambda: None
        slot = state.get_or_create_slot("chat-1")
        slot.messages = [{"role": "user", "content": "hi"}]
        mine = slot.queue_append(_TYPED, directive_user_origin=True)
        channel = slot.queue_append(
            _TYPED, directive_user_origin=True, directive_channel_origin=True
        )
        peer = slot.queue_append(_TYPED)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/chat-1")
            assert resp.status == 200
            body = await resp.json()

        by_id = {q["id"]: q["content"] for q in body["queue"]}
        assert by_id[mine] == _TYPED
        assert _SECRET not in by_id[channel]
        assert _SECRET not in by_id[peer]

    @pytest.mark.asyncio
    async def test_drain_pop_frame_matches_the_row_the_drain_writes(self, tmp_path, monkeypatch):
        """The client rebuilds the drained user row from the ``queue_pop`` frame
        alone. The drain writes that row, and hands the turn its input, from the
        redacted text, so the frame carries the same text: a composer entry's
        pending card is as typed, its drained row is what the turn received."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append(_TYPED, directive_user_origin=True)
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        (pop,) = _frames(state, "queue_pop")
        row = next(m for m in slot.messages if m.get("role") == "user")
        assert _SECRET not in pop["content"]
        assert pop["content"] == row["content"]


class TestSteerEgress:
    @pytest.mark.asyncio
    async def test_composer_steer_row_and_push_are_as_typed(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _steer_capable_slot(state)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": _TYPED, "steer": True}
            )
            assert (await resp.json()).get("steered") is True

        steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert steer_row["content"] == _TYPED
        (push,) = _frames(state, "steer_push")
        assert push["content"] == _TYPED

    @pytest.mark.asyncio
    async def test_peer_steer_row_and_push_stay_redacted(self, tmp_path, monkeypatch):
        """``session_send`` steers another session with ``user_origin=False``: that
        text has no human author, so its row and card keep the redaction."""
        from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _steer_capable_slot(state)

        assert await steer_into_running_turn(state, slot, _TYPED) == STEER_STEERED

        steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert _SECRET not in steer_row["content"]
        (push,) = _frames(state, "steer_push")
        assert _SECRET not in push["content"]

    def test_state_patch_finds_a_row_stored_as_typed(self, tmp_path):
        """The lifecycle patch resolves a steer's row by content; a composer row
        now holds the text as typed, so the lookup must still find it."""
        from kiro_crew.dashboard.chat_delivery import STEER_STATE_WRITTEN, find_written_steer_row

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot.messages.append(
            {
                "role": "user",
                "content": _TYPED,
                "meta": {"steer": True, "steerState": STEER_STATE_WRITTEN},
            }
        )
        row = find_written_steer_row(slot, _TYPED, siblings=[_TYPED])
        assert row is not None and row["content"] == _TYPED
