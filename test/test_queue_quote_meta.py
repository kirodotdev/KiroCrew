"""A whole-message quote (``meta.quote``) survives the queue.

The dashboard prepends the quoted message to the text it sends and rides the
same record on ``meta.quote`` so the user bubble draws a card instead of the raw
blockquote. A send that lands on a BUSY slot is queued, and the drained row is
rebuilt from the entry's meta -- so the quote rides the entry, bounded and
redacted like the attachment lists beside it,
and a quoting entry must drain alone so the block stays at the head of its row.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from chat_test_helpers import _make_state as _make_full_state

from kiro_crew.dashboard.chat_delivery import (
    QUOTE_FIELD_MAX_LEN,
    QUOTE_TEXT_MAX_LEN,
    queue_entry_view,
    queue_for_next_turn,
    quote_meta,
)
from kiro_crew.dashboard.chat_utils import carries_attachments
from kiro_crew.dashboard.state import DashboardState

_QUOTE = {
    "role": "assistant",
    "text": "Three things landed.",
    "ts": "2026-09-29T09:12:40Z",
    "mid": "m2",
}


def _make_state() -> DashboardState:
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    state._ws_clients = []
    state._sse_queues = []
    state._notify_event = MagicMock()
    state._background_tasks = set()
    state._yolo = False
    state._yolo_expires_at = 0.0
    state._restricted_keys = set()
    state.sessions = None
    state.conversation_log = None
    state.channel_manager = None
    state.broadcast_ws = MagicMock()
    return state


class TestQuoteMeta:
    def test_well_formed_record_is_kept_whole(self):
        assert quote_meta({"quote": {**_QUOTE, "author": "Worker"}}) == {
            "quote": {**_QUOTE, "author": "Worker"}
        }

    def test_optional_fields_that_are_not_strings_are_dropped(self):
        assert quote_meta({"quote": {"role": "user", "text": "hi", "ts": 5, "mid": None}}) == {
            "quote": {"role": "user", "text": "hi"}
        }

    def test_anything_a_card_could_not_draw_is_refused_whole(self):
        assert quote_meta(None) == {}
        assert quote_meta({}) == {}
        assert quote_meta({"quote": "text"}) == {}
        assert quote_meta({"quote": {"role": "system", "text": "hi"}}) == {}
        assert quote_meta({"quote": {"role": "user", "text": "   "}}) == {}
        assert quote_meta({"quote": {"role": "user"}}) == {}
        # An unhashable role refuses the record instead of raising out of the send.
        assert quote_meta({"quote": {"role": ["user"], "text": "hi"}}) == {}
        assert quote_meta({"quote": {"role": {"a": 1}, "text": "hi"}}) == {}

    def test_over_bound_text_or_field_refuses_the_record_rather_than_trimming(self):
        assert quote_meta({"quote": {"role": "user", "text": "x" * (QUOTE_TEXT_MAX_LEN + 1)}}) == {}
        assert (
            quote_meta(
                {"quote": {"role": "user", "text": "ok", "author": "a" * (QUOTE_FIELD_MAX_LEN + 1)}}
            )
            == {}
        )

    def test_a_non_user_origin_record_gets_the_display_redaction(self):
        # An app token's quote is not the session's human's words: it rides
        # every frame redacted, the same as its text.
        out = quote_meta(
            {"quote": {"role": "user", "text": "key AKIAIOSFODNN7EXAMPLE here"}}, user_origin=False
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in out["quote"]["text"]

    def test_a_user_row_s_record_survives_the_emit_redaction_and_another_role_s_does_not(self):
        from kiro_crew.dashboard.chat_utils import _redact_meta_for_role

        meta = {"quote": {"role": "user", "text": "key AKIAIOSFODNN7EXAMPLE here"}, "sendId": "s-1"}
        assert (
            _redact_meta_for_role("user", meta)["quote"]["text"] == "key AKIAIOSFODNN7EXAMPLE here"
        )
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in _redact_meta_for_role("assistant", meta)["quote"]["text"]
        )

    def test_a_user_row_s_record_is_still_bounded_on_emit(self):
        # A transcript line is attacker-writable: an oversized or malformed
        # record is dropped at emit, not passed through because the row is a user's.
        from kiro_crew.dashboard.chat_utils import _redact_meta_for_role

        huge = {"quote": {"role": "user", "text": "x" * (QUOTE_TEXT_MAX_LEN + 1)}, "sendId": "s-1"}
        out = _redact_meta_for_role("user", huge)
        assert "quote" not in out and out["sendId"] == "s-1"
        bad = {"quote": {"role": ["user"], "text": "x"}}
        assert "quote" not in _redact_meta_for_role("user", bad)
        extra = {"quote": {"role": "user", "text": "ok", "evil": "field"}}
        assert _redact_meta_for_role("user", extra)["quote"] == {"role": "user", "text": "ok"}

    def test_the_quoted_text_is_stored_as_given_so_it_matches_the_row_block(self):
        # The row's content opens with the same block as typed; a record
        # redacted on its own would never match it and the card would not draw.
        out = quote_meta({"quote": {"role": "user", "text": "key AKIAIOSFODNN7EXAMPLE here"}})
        assert out["quote"]["text"] == "key AKIAIOSFODNN7EXAMPLE here"


class TestQuoteRidesTheQueue:
    def test_queue_for_next_turn_stamps_the_quote_on_the_entry_and_the_push_frame(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        with (
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        ):
            qid = queue_for_next_turn(
                state, slot, "> Three things landed.\n\nwhy?", quote=dict(_QUOTE)
            )
        entry = next(e for e in slot._queue if e["id"] == qid)
        assert entry["meta"]["quote"] == _QUOTE
        frame = next(
            payload
            for kind, payload in (c.args for c in state.broadcast_ws.call_args_list)
            if kind == "queue_push"
        )
        assert frame["meta"] == {"quote": _QUOTE}

    def test_queue_for_next_turn_without_a_quote_keeps_the_prior_shape(self):
        state = _make_state()
        slot = state.get_or_create_slot("chat-1")
        with (
            patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
            patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"),
        ):
            qid = queue_for_next_turn(state, slot, "plain")
        entry = next(e for e in slot._queue if e["id"] == qid)
        assert "quote" not in (entry.get("meta") or {})
        frame = next(
            payload
            for kind, payload in (c.args for c in state.broadcast_ws.call_args_list)
            if kind == "queue_push"
        )
        assert "meta" not in frame

    def test_queue_entry_view_redacts_the_quote_of_an_entry_that_is_not_the_human_s(self):
        # A restored entry carries no origin stamp: it is not the dashboard's
        # human's and its record is redacted like its text.
        hot = {"role": "user", "text": "key AKIAIOSFODNN7EXAMPLE here"}
        view = queue_entry_view({"id": "q1", "content": "x", "meta": {"quote": hot}})
        assert "AKIAIOSFODNN7EXAMPLE" not in view["meta"]["quote"]["text"]
        view = queue_entry_view(
            {"id": "q1", "content": "x", "meta": {"quote": hot}, "_directive_user_origin": True}
        )
        assert view["meta"]["quote"]["text"] == hot["text"]

    def test_queue_entry_view_carries_the_quote(self):
        view = queue_entry_view({"id": "q1", "content": "why?", "meta": {"quote": _QUOTE}})
        assert view["meta"] == {"quote": _QUOTE}

    def test_a_quoting_entry_drains_alone(self):
        assert carries_attachments({"content": "x", "meta": {"quote": _QUOTE}}) is True
        assert carries_attachments({"content": "x", "meta": {}}) is False


def _busy_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_full_state(tmp_path)
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("busy-chat")
    slot._turn_admission_reserved = True  # force the busy queue path
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", MagicMock())
    return state, slot


async def _post(state, slot_key, message, meta, **extra):
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat", json={"slot": slot_key, "message": message, "meta": meta, **extra}
        )
        assert resp.status == 200
        return await resp.json()


class TestQuoteThroughTheHandler:
    @pytest.mark.asyncio
    async def test_busy_slot_entry_and_push_frame_carry_the_bounded_quote(
        self, tmp_path, monkeypatch
    ):
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post(
            state, "busy-chat", "> q\n\nwhy?", {"sendId": "s-1", "quote": {**_QUOTE, "junk": 1}}
        )
        entry = next(i for i in slot._queue if i["content"] == "> q\n\nwhy?")
        assert entry["meta"]["quote"] == _QUOTE
        frames = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
        assert frames and frames[-1]["meta"] == {"quote": _QUOTE}

    @pytest.mark.asyncio
    async def test_an_oversized_quote_is_dropped_on_the_busy_path_too(self, tmp_path, monkeypatch):
        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post(
            state,
            "busy-chat",
            "why?",
            {"quote": {"role": "user", "text": "x" * (QUOTE_TEXT_MAX_LEN + 1)}},
        )
        entry = next(i for i in slot._queue if i["content"] == "why?")
        assert "quote" not in entry["meta"]

    @pytest.mark.asyncio
    async def test_a_steer_retains_the_quote_for_the_requeue(self, tmp_path, monkeypatch):
        """A steer the turn ends before consuming drains as a queued row rebuilt
        from `_steer_attachment_meta`; the quote rides that map with the lists."""
        state, slot = _busy_state(tmp_path, monkeypatch)
        steer = AsyncMock(return_value=True)
        slot._acp_client = MagicMock(supports_steer=True, steer=steer)
        body = await _post(
            state,
            "busy-chat",
            "> q\n\nwhy?",
            {"sendId": "s-2", "quote": dict(_QUOTE), "files": ["/tmp/a.pdf"]},
            steer=True,
        )
        assert body.get("steered") is True
        assert slot._steer_attachment_meta["> q\n\nwhy?"] == {
            "files": ["/tmp/a.pdf"],
            "quote": _QUOTE,
        }

    @pytest.mark.asyncio
    async def test_the_queue_pop_frame_carries_the_quote_beside_the_lists(
        self, tmp_path, monkeypatch
    ):
        """The drain's `queue_pop` is the frame the client rebuilds the user row
        from (no `chat_message` echo follows for a user row); the quote rides it
        so the rebuilt row draws the card rather than the raw blockquote."""
        from test_queue_drain_preserve_attachments import _drain_once

        state, slot = _busy_state(tmp_path, monkeypatch)
        await _post(
            state,
            "busy-chat",
            "> q\n\nwhy?",
            {"quote": dict(_QUOTE), "files": ["/tmp/a.pdf"]},
        )
        state.subagents = None
        slot._turn_admission_reserved = False
        await _drain_once(state, slot)
        pops = [
            c.args[1]
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "queue_pop"
        ]
        pop = next(p for p in pops if p.get("content") == "> q\n\nwhy?")
        assert pop["meta"] == {"files": ["/tmp/a.pdf"], "quote": _QUOTE}

    @pytest.mark.asyncio
    async def test_the_drained_row_s_quote_follows_the_row_s_redaction_rule(
        self, tmp_path, monkeypatch
    ):
        """An entry restored from disk carries no origin stamp: its row text is
        display-redacted, and so is the quote record the row carries."""
        from test_queue_drain_preserve_attachments import _drain_once, _user_rows

        state, slot = _busy_state(tmp_path, monkeypatch)
        hot = {"role": "user", "text": "key AKIAIOSFODNN7EXAMPLE here"}
        slot.queue_append("> key AKIAIOSFODNN7EXAMPLE here\n\nwhy?", meta={"quote": dict(hot)})
        state.subagents = None
        slot._turn_admission_reserved = False
        await _drain_once(state, slot)
        row = _user_rows(slot)[-1]
        assert "AKIAIOSFODNN7EXAMPLE" not in row["meta"]["quote"]["text"]

    def test_the_requeue_push_frame_carries_the_quote_beside_the_lists(self, tmp_path, monkeypatch):
        """The `queue_push` a requeued steer broadcasts is the card every open
        tab draws until the next hydration; it carries the quote, not only the
        attachment lists, so no tab shows the blockquote as text."""
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_full_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("requeue")
        slot._pending_steers = ["> q\n\nwhy?"]
        slot._steer_attachment_meta = {
            "> q\n\nwhy?": {"files": ["/tmp/a.pdf"], "quote": dict(_QUOTE)}
        }
        _requeue_unconsumed_steers(state, slot)
        assert slot._queue[0]["meta"]["quote"] == _QUOTE
        pushes = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"]
        assert len(pushes) == 1
        assert pushes[0]["meta"] == {"files": ["/tmp/a.pdf"], "quote": _QUOTE}


class TestQueueEditPrunesTheQuote:
    def _entry(self, content, meta):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("s1")
        qid = slot.queue_append(content, meta=dict(meta))
        return slot, qid

    _BLOCK = "> Three things landed.\n> — quoting an earlier message from the assistant"

    def test_editing_the_text_under_an_intact_block_keeps_the_card(self):
        slot, qid = self._entry(self._BLOCK + "\n\nwhy?", {"quote": dict(_QUOTE)})
        assert slot.queue_edit_by_id(qid, self._BLOCK + "\n\nwhy not?") is True
        entry = next(i for i in slot._queue if i["id"] == qid)
        assert entry["meta"]["quote"] == _QUOTE

    def test_editing_the_block_away_withdraws_the_card(self):
        slot, qid = self._entry(self._BLOCK + "\n\nwhy?", {"quote": dict(_QUOTE), "sendId": "s-1"})
        assert slot.queue_edit_by_id(qid, "why, with the quote deleted?") is True
        entry = next(i for i in slot._queue if i["id"] == qid)
        assert "quote" not in entry["meta"]
        assert entry["meta"]["sendId"] == "s-1"

    def test_a_marker_inside_the_quoted_text_is_not_this_entry_s_attachment(self):
        """Quoting a message that itself shows an attachment marker: an edit
        that drops one of THIS entry's attachments renumbers the body's markers
        only, leaves the block byte-for-byte, and keeps the card."""
        from kiro_crew.dashboard.slot_queue_repository import quote_block

        # The quoted message named the SAME file this entry attaches, so a
        # whole-text prune would read the block's marker as this entry's own.
        quoted = {"role": "user", "text": "[attached_file 1] /tmp/a.pdf see this"}
        block = quote_block(quoted)
        body_before = "[attached_file 1] /tmp/a.pdf\n[attached_file 2] /tmp/b.pdf\nwhy?"
        slot, qid = self._entry(
            block + "\n\n" + body_before,
            {"quote": quoted, "files": ["/tmp/a.pdf", "/tmp/b.pdf"]},
        )
        assert slot.queue_edit_by_id(qid, block + "\n\n[attached_file 2] /tmp/b.pdf\nwhy?") is True
        entry = next(i for i in slot._queue if i["id"] == qid)
        assert entry["content"] == block + "\n\n[attached_file 1] /tmp/b.pdf\nwhy?"
        assert entry["meta"]["files"] == ["/tmp/b.pdf"]
        assert entry["meta"]["quote"] == quoted

    def test_quote_block_mirrors_the_client_serialization(self):
        from kiro_crew.dashboard.slot_queue_repository import quote_block

        assert quote_block(_QUOTE) == self._BLOCK
        assert (
            quote_block({"role": "user", "text": "a\nb"})
            == "> a\n> b\n> — quoting an earlier message from the user"
        )
        assert quote_block({"role": "system", "text": "x"}) is None
        assert quote_block("x") is None
        # A restored entry with an unhashable role refuses, never raises.
        assert quote_block({"role": ["user"], "text": "x"}) is None
        assert quote_block({"role": {"a": 1}, "text": "x"}) is None
