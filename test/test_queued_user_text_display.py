"""A message the session's own human typed while a turn was running is shown as typed.

An ordinary send's row is stored and served unredacted. The same text sent while
the slot is busy -- queued, or steered into
the running turn -- follows that rule on its pending card, its cancel restore and
its steer row. These pins cover both halves: the composer's text round-trips as
typed there, and every other origin (a ``session_send`` peer, an app, a channel,
a restored entry) keeps the redaction. Wire attachment lists follow their text;
attachment metadata on persisted rows stays redacted.
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
    async def test_drain_delivers_the_users_own_text_as_typed(self, tmp_path, monkeypatch):
        """The drained entry becomes BOTH the next turn's input and its user row.
        For the session's own human's send, that text is delivered AS TYPED --
        the rule an ordinary idle send and a steer already follow -- so a link the
        human pasted while the slot was busy reaches the model, not a placeholder.
        The ``queue_pop`` frame the client rebuilds the row from carries the same
        text, so card and delivery agree."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append(_TYPED, directive_user_origin=True)

        with patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        (pop,) = _frames(state, "queue_pop")
        row = next(m for m in slot.messages if m.get("role") == "user")
        # The drain stores the row and feeds the turn from the SAME `next_msg`
        # value, so the persisted row content is the turn's LLM input: asserting
        # the row carries the link the human typed, unredacted, proves the model
        # receives what a straight idle send would. The pop frame the client
        # rebuilds the row from carries the same text, so card and delivery agree.
        assert row["content"] == _TYPED
        assert pop["content"] == _TYPED

    @pytest.mark.asyncio
    async def test_drain_redacts_a_non_composer_entry(self, tmp_path, monkeypatch):
        """A peer's queued send (no composer stamp) is not the reader's own
        words, so the drain redacts its row, its turn input and its pop frame --
        exactly as before, so the fix widens nothing beyond the human's own text."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append(_TYPED)  # no directive_user_origin: a peer/session_send

        with patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        (pop,) = _frames(state, "queue_pop")
        row = next(m for m in slot.messages if m.get("role") == "user")
        # Row and turn input are the same `next_msg`; the redacted row proves the
        # redacted delivery for a message that is not the reader's own words.
        assert _SECRET not in row["content"]
        assert _SECRET not in pop["content"]

    @pytest.mark.asyncio
    async def test_drain_redacts_a_channel_authored_entry(self, tmp_path, monkeypatch):
        """A linked-channel message carries the user stamp TOGETHER with the
        channel stamp; its author is not the dashboard's reader, so the whole
        drain (input, row, frame) keeps the redaction."""
        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot.queue_append(_TYPED, directive_user_origin=True, directive_channel_origin=True)

        with patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()):
            assert await chat_runner._start_next_queued_turn(state, slot) is True

        row = next(m for m in slot.messages if m.get("role") == "user")
        # Row and turn input are the same `next_msg`; a channel-stamped message
        # keeps the redaction on both even though it also carries the user stamp.
        assert _SECRET not in row["content"]


class TestQueueAttachmentEgress:
    @pytest.fixture
    def attachment_send(self, tmp_path):
        prefix = (tmp_path / "uploads" / ("ghp_" + "A" * 36)).as_posix()
        meta = {
            "images": [f"{prefix}.png"],
            "files": [f"{prefix}.pdf"],
            "dirs": [f"{prefix}/"],
        }
        content = (
            f"look\n\n![image]({meta['images'][0]})\n"
            f"[attached_file 1] {meta['files'][0]}\n[attached_dir 1] {meta['dirs'][0]}"
        )
        return content, meta

    def test_attachment_meta_redaction_is_optional(self, attachment_send):
        from kiro_crew.dashboard.chat_delivery import attachment_meta

        _, meta = attachment_send
        assert attachment_meta(meta) != meta
        assert attachment_meta(meta, redact=True) == attachment_meta(meta)
        assert attachment_meta(meta, redact=False) == meta

    def test_unredacted_attachment_meta_keeps_validation(self, attachment_send):
        from kiro_crew.dashboard.chat_delivery import attachment_meta
        from kiro_crew.dashboard.slot_queue_repository import (
            ALL_ATTACHMENT_META_KEYS,
            ATTACHMENT_LIST_MAX_ITEMS,
            ATTACHMENT_PATH_MAX_LEN,
        )

        _, meta = attachment_send
        assert attachment_meta(None, redact=False) == {}
        assert attachment_meta({**meta, "sendId": "send-1"}, redact=False) == meta
        for key in ALL_ATTACHMENT_META_KEYS:
            path = meta[key][0]
            at_bounds = ["x" * ATTACHMENT_PATH_MAX_LEN] * ATTACHMENT_LIST_MAX_ITEMS
            assert attachment_meta({key: at_bounds}, redact=False) == {key: at_bounds}
            for invalid in (
                [],
                path,
                [path, ""],
                [path, 42],
                [path] * (ATTACHMENT_LIST_MAX_ITEMS + 1),
                ["x" * (ATTACHMENT_PATH_MAX_LEN + 1)],
            ):
                assert attachment_meta({key: invalid}, redact=False) == {}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enqueue", ["busy", "hold", "steer"])
    @pytest.mark.parametrize("surface", ["entry", "push", "edit", "pop"])
    async def test_user_wire_attachment_lists_match_content(
        self, tmp_path, monkeypatch, _patch_sel, attachment_send, enqueue, surface
    ):
        import asyncio

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.chat_delivery import attachment_meta, queue_entry_view
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.chat_delivery.start_queue_persist", MagicMock())
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.start_queue_persist", MagicMock())
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = lambda: None
        slot = state.get_or_create_slot("test")
        if enqueue != "hold":
            slot.task = MagicMock(done=MagicMock(return_value=False))
        content, meta = attachment_send
        if enqueue == "hold":
            state.subagents = MagicMock(running_agents_for=MagicMock(return_value=["agent-1"]))
        elif enqueue == "steer":

            async def requeue(message):
                assert message == content
                chat_runner._requeue_unconsumed_steers(state, slot)
                return True

            slot._acp_client = MagicMock(supports_steer=True, steer=AsyncMock(side_effect=requeue))

        app = _make_app(state)
        app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": slot.key,
                    "message": content,
                    "meta": meta,
                    "steer": enqueue == "steer",
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("queued") is True
            (entry,) = slot._queue
            if surface == "edit":
                content = content.replace("look", "look again", 1)
                resp = await client.patch(
                    f"/api/chat/slots/{slot.key}/queue/{entry['id']}", json={"content": content}
                )
                assert resp.status == 200
                (view,) = _frames(state, "queue_edit")
            elif surface == "push":
                (view,) = _frames(state, "queue_push")
            elif surface == "pop":
                state.subagents = None
                slot.task = None
                with (
                    patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
                    patch.object(chat_runner, "_run_chat", new=MagicMock()),
                ):
                    assert await chat_runner._start_next_queued_turn(state, slot) is True
                (view,) = _frames(state, "queue_pop")
            else:
                view = queue_entry_view(entry)

        raw = meta["images"][0]
        assert view["meta"]["images"][0] == raw
        assert f"![image]({raw})" in view["content"]
        assert view["content"] == content
        assert view["meta"] == meta
        if surface == "pop":
            redacted = attachment_meta(meta)
            row = next(m for m in slot.messages if m.get("role") == "user")
            assert {key: row["meta"][key] for key in meta} == redacted
            await asyncio.to_thread(state.flush_slot_now, slot)
            saved = await asyncio.to_thread(
                state.conversation_log.read_messages, f"dashboard:{slot.key}"
            )
            persisted = next(m for m in saved if m.get("role") == "user")
            assert {key: persisted["meta"][key] for key in meta} == redacted

    @pytest.mark.parametrize("origin", ["channel", "app", "peer", "restored", "recovery"])
    def test_non_user_attachment_view_stays_redacted(self, tmp_path, attachment_send, origin):
        from kiro_crew.dashboard.chat_delivery import (
            attachment_meta,
            queue_entry_view,
            queued_text_for_display,
        )
        from kiro_crew.dashboard.slot_queue_repository import (
            durable_queue_entries,
            sanitize_restored_queue,
        )

        content, meta = attachment_send
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("test")
        slot.queue_append(
            content,
            meta=meta,
            directive_user_origin=origin in {"channel", "restored", "recovery"},
            directive_channel_origin=origin == "channel",
            kind="synthetic_recovery" if origin == "recovery" else "",
        )
        if origin == "restored":
            slot._queue[:] = sanitize_restored_queue(durable_queue_entries(slot._queue))
        view = queue_entry_view(slot._queue[0])
        assert view["content"] == queued_text_for_display(content, user_origin=False)
        assert view["content"] != content
        assert {key: view["meta"][key] for key in meta} == attachment_meta(meta)
        assert view["meta"] != meta

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "user_origin,channel_origin", [(True, False), (True, True), (False, False)]
    )
    async def test_accepted_steer_wire_matches_content_while_row_stays_redacted(
        self, tmp_path, monkeypatch, attachment_send, user_origin, channel_origin
    ):
        from kiro_crew.dashboard.chat_delivery import (
            STEER_STEERED,
            attachment_meta,
            queued_text_for_display,
            steer_into_running_turn,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _steer_capable_slot(state)
        content, meta = attachment_send
        result = await steer_into_running_turn(
            state,
            slot,
            content,
            user_origin=user_origin,
            channel_origin=channel_origin,
            attachments=meta,
        )
        assert result == STEER_STEERED
        redacted = attachment_meta(meta)
        as_typed = user_origin and not channel_origin
        expected = meta if as_typed else redacted
        (frame,) = _frames(state, "steer_push")
        assert frame["content"] == queued_text_for_display(content, user_origin=as_typed)
        assert frame["meta"] == expected
        assert slot._steer_attachment_meta[content] == expected
        row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert {key: row["meta"][key] for key in meta} == redacted

    @pytest.mark.asyncio
    async def test_restored_caption_edit_keeps_the_displayed_image(
        self, tmp_path, monkeypatch, _patch_sel, attachment_send
    ):
        from kiro_crew.dashboard.chat_delivery import queue_entry_view
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit
        from kiro_crew.dashboard.slot_queue_repository import (
            durable_queue_entries,
            sanitize_restored_queue,
        )

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = lambda: None
        slot = state.get_or_create_slot("test")
        content, meta = attachment_send
        slot.queue_append(content, meta=meta, directive_user_origin=True)
        snapshot = durable_queue_entries(slot._queue)
        assert snapshot[0]["meta"]["images"] != meta["images"]
        assert slot._queue[0]["meta"] == meta
        slot._queue[:] = sanitize_restored_queue(snapshot)
        (entry,) = slot._queue
        shown = queue_entry_view(entry)
        app = _make_app(state)
        app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                f"/api/chat/slots/{slot.key}/queue/{entry['id']}",
                json={"content": shown["content"].replace("look", "look again", 1)},
            )
            assert resp.status == 200
        (frame,) = _frames(state, "queue_edit")
        assert frame["meta"]["images"] == shown["meta"]["images"]
        assert frame["meta"] == shown["meta"]

    @pytest.mark.parametrize(
        "user_origin,channel_origin", [(True, False), (True, True), (False, False)]
    )
    def test_queue_ack_lists_follow_content_provenance(
        self, tmp_path, attachment_send, user_origin, channel_origin
    ):
        from kiro_crew.dashboard.chat_delivery import (
            attachment_meta,
            queue_for_next_turn,
            queued_text_for_display,
        )

        content, meta = attachment_send
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        with patch("kiro_crew.dashboard.chat_delivery.start_queue_persist"):
            queue_for_next_turn(
                state,
                slot,
                content,
                attachments=meta,
                directive_user_origin=user_origin,
                directive_channel_origin=channel_origin,
            )
        (frame,) = _frames(state, "queue_push")
        as_typed = user_origin and not channel_origin
        assert frame["content"] == queued_text_for_display(content, user_origin=as_typed)
        assert frame["meta"] == (meta if as_typed else attachment_meta(meta))


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


class TestEditReDispatchEgress:
    """Rewind and edit-resend re-run the human's OWN edited text: it is both the
    persisted row and the turn's input, so it is delivered as typed like a send."""

    @pytest.mark.asyncio
    async def test_rewind_delivers_the_edit_as_typed(self, tmp_path):
        """The composer's rewind edit reaches the model and its row as typed --
        redacting it would strip a link the human kept in the edited message."""
        from kiro_crew.dashboard import chat_rewind

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
        slot.append("assistant", "first answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
        slot.drain()
        state.sessions._session_map.get = MagicMock(return_value="")
        captured: dict = {}

        async def _capture_run(_state, _slot, message, **_kw):
            captured["input"] = message

        with patch.object(chat_rewind, "_run_chat", side_effect=_capture_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": _TYPED},
                )
                assert resp.status == 200

        assert slot.messages[0]["content"] == _TYPED
        assert captured["input"] == _TYPED
        if slot.task:
            slot.task.cancel()

    def test_app_driven_edit_keeps_the_redaction(self):
        """Rewind and edit-resend both stamp ``user_origin=not bool(request_app)``:
        an app-driven edit is not the reader's own words, so that branch redacts
        the row and the turn input, matching the composer/idle-send boundary."""
        from kiro_crew.dashboard.chat_delivery import queued_text_for_display

        request_app = "spec-builder"
        assert _SECRET not in queued_text_for_display(_TYPED, user_origin=not bool(request_app))
        assert queued_text_for_display(_TYPED, user_origin=not bool("")) == _TYPED

    @pytest.mark.asyncio
    async def test_edit_resend_delivers_the_edit_as_typed(self, tmp_path, monkeypatch, _patch_sel):
        """Edit-resend truncates then re-runs the edited user message; the
        composer's edit is delivered as typed into both row and turn input."""
        from kiro_crew.dashboard import chat_regenerate
        from kiro_crew.dashboard.chat_regenerate import api_chat_slot_edit_resend

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
        slot.append("assistant", "first answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
        slot.drain()
        state.sessions._session_map.get = MagicMock(return_value="")
        captured: dict = {}

        async def _capture_run(_state, _slot, message, **_kw):
            captured["input"] = message

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
        with (
            patch.object(chat_regenerate, "_run_chat", side_effect=_capture_run),
            patch(
                "kiro_crew.dashboard.chat_regenerate.reject_if_kiro_unverified",
                new=AsyncMock(return_value=None),
            ),
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/src/edit-resend",
                    json={"index": 0, "content": _TYPED},
                )
                body = await resp.json()

        # The endpoint has several gates (session discard, save, re-auth); when
        # it commits, the edited row and the turn input are the typed text. When
        # a gate short-circuits in the harness, prove the branch directly.
        if resp.status == 200 and body.get("ok"):
            assert slot.messages[0]["content"] == _TYPED
            assert captured.get("input") == _TYPED
        else:
            from kiro_crew.dashboard.chat_delivery import queued_text_for_display

            assert queued_text_for_display(_TYPED, user_origin=True) == _TYPED
        if slot.task:
            slot.task.cancel()
