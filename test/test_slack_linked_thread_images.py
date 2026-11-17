"""A picture sent in a Slack thread linked to a dashboard chat reaches that chat's turn.

The Slack event path deletes a message's downloaded files when its handler task
ends, and the linked chat's turn runs after that. ``route_linked_thread`` stores the
pictures in the linked session's attachments first
(:mod:`kiro_crew.slack.linked_thread_images`). Each test deletes the temp file the
way the handler's cleanup does, then checks what the turn reads.
"""

from __future__ import annotations

import os
import struct
import tempfile
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from kiro_crew.chat_attachments import attachments_dir
from kiro_crew.dashboard.chat_utils_slots import slot_history_key
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.attachments import image_location_note
from kiro_crew.slack import linked_thread_images


def _png() -> bytes:
    """A real 2x2 RGB PNG: the prompt builder decodes what it inlines."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _temp(data: bytes, suffix: str) -> str:
    """A temp file named the way the Slack ingest names its downloads."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _slack_text(words: str, picture: str) -> str:
    """The text the event path builds for a message with one picture."""
    return f"{words}\n{picture}\n\n{image_location_note(picture)}"


def _image_blocks(message: str) -> list[dict]:
    from kiro_crew.acp.prompt_blocks import build_prompt_blocks

    return [b for b in build_prompt_blocks(message) if b.get("type") == "image"]


def _state(slot, log: ConversationLog) -> MagicMock:
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._slots = {slot.key: slot}
    ds._background_tasks = set()
    ds.broadcast_ws = MagicMock()
    ds.push_slots_update = MagicMock()
    ds.conversation_log = log
    return ds


async def _route(ds, text: str) -> AsyncMock:
    from kiro_crew.slack import handler

    slack = MagicMock()
    slack.post_message = AsyncMock()
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
    ):
        await handler.handle_message(slack, MagicMock(), "C1", text, "t1", "msg1", "U1")
    return run_chat


@pytest.fixture
def log(tmp_path: Path) -> ConversationLog:
    return ConversationLog(base_dir=tmp_path / "sessions")


def _store(log: ConversationLog, slot) -> Path:
    path = log._path(slot_history_key(slot))
    return attachments_dir(path.parent, path.stem)


class TestLinkedThreadPictures:
    @pytest.mark.asyncio
    async def test_the_turn_reads_a_stored_copy_after_the_cleanup(self, log):
        slot = _ChatSlot("slot1")
        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        try:
            run_chat = await _route(_state(slot, log), _slack_text("what is this?", picture))
        finally:
            os.unlink(picture)  # what the handler task's cleanup does
        turn_text = run_chat.call_args[0][2]
        assert picture not in turn_text
        stored = list(_store(log, slot).iterdir())
        assert len(stored) == 1 and stored[0].read_bytes() == _png()
        assert turn_text.startswith("what is this?\n![image](")
        assert len(_image_blocks(turn_text)) == 1
        # The dashboard row names the same copy, so it still renders after the turn.
        assert slot.messages[0]["content"] == turn_text

    @pytest.mark.asyncio
    async def test_without_the_store_the_picture_is_lost(self, log):
        """The defect: the text the event path built, read after its cleanup."""
        slot = _ChatSlot("slot1")
        picture = _temp(_png(), ".png")
        try:
            run_chat = await _route(_state(slot, log), _slack_text("what is this?", picture))
        finally:
            os.unlink(picture)
        assert _image_blocks(run_chat.call_args[0][2]) == []

    @pytest.mark.asyncio
    async def test_a_queued_message_names_the_stored_copy(self, log):
        slot = _ChatSlot("slot1")
        queued: list[str] = []
        picture = _temp(_png(), ".jpg")
        linked_thread_images.set_inbound_files([picture])
        try:
            with (
                patch.object(_ChatSlot, "running", new_callable=PropertyMock, return_value=True),
                patch.object(
                    _ChatSlot,
                    "queue_append",
                    lambda _self, content, **_kw: queued.append(content) or "q1",
                ),
            ):
                await _route(_state(slot, log), _slack_text("later", picture))
        finally:
            os.unlink(picture)
        assert len(queued) == 1 and picture not in queued[0]
        assert len(_image_blocks(queued[0])) == 1
        assert len(list(_store(log, slot).iterdir())) == 1

    @pytest.mark.asyncio
    async def test_a_file_that_is_not_a_picture_keeps_its_line(self, log):
        slot = _ChatSlot("slot1")
        doc = _temp(b"%PDF-1.4", ".pdf")
        linked_thread_images.set_inbound_files([doc])
        try:
            run_chat = await _route(_state(slot, log), f"see this\n{doc}")
        finally:
            os.unlink(doc)
        assert run_chat.call_args[0][2] == f"see this\n{doc}"
        assert not _store(log, slot).exists()

    @pytest.mark.asyncio
    async def test_the_recorded_files_are_taken_once(self, log):
        slot = _ChatSlot("slot1")
        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        try:
            text = _slack_text("x", picture)
            assert (
                await linked_thread_images.store_for_slot(
                    _state(slot, log), slot, text, still_current=lambda: True
                )
                != text
            )
            assert (
                await linked_thread_images.store_for_slot(
                    _state(slot, log), slot, text, still_current=lambda: True
                )
                == text
            )
        finally:
            os.unlink(picture)

    @pytest.mark.asyncio
    async def test_no_conversation_log_leaves_the_text(self, log):
        slot = _ChatSlot("slot1")
        ds = _state(slot, log)
        ds.conversation_log = None
        linked_thread_images.set_inbound_files(["/tmp/tmpabcdefgh.png"])
        text = _slack_text("x", "/tmp/tmpabcdefgh.png")
        assert (
            await linked_thread_images.store_for_slot(ds, slot, text, still_current=lambda: True)
            == text
        )


class TestSlotChangedDuringCopy:
    """The copy runs off the loop; the slot can be deleted or relinked meanwhile."""

    @pytest.mark.asyncio
    async def test_a_session_deleted_before_the_copy_gets_no_attachments_back(self, log):
        """A permanent delete that wins the lock before the copy: the store refuses
        under the lock, so the reclaimed attachments directory is not recreated."""
        slot = _ChatSlot("slot1")
        key = slot_history_key(slot)
        log.append(key, "user", "earlier")
        meta, _ = log.get_metadata_status(key)
        slot._disk_meta_created_at = str(meta.get("created_at") or "")
        assert slot._disk_meta_created_at
        ds = _state(slot, log)
        real_store = ConversationLog.store_ingested_images

        def delete_before_copying(self_, key_, content, images, **kw):
            assert self_.delete_session(key_)
            return real_store(self_, key_, content, images, **kw)

        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        text = _slack_text("what is this?", picture)
        try:
            with patch.object(ConversationLog, "store_ingested_images", delete_before_copying):
                stored = await linked_thread_images.store_for_slot(
                    ds, slot, text, still_current=lambda: True
                )
        finally:
            os.unlink(picture)
        assert stored == text
        assert not _store(log, slot).exists()

    def test_a_delete_in_flight_stores_nothing(self, log):
        key = "dashboard:slot1"
        picture = _temp(_png(), ".png")
        text = _slack_text("x", picture)
        try:
            with log.delete_in_flight_window(key):
                assert log.store_ingested_images(key, text, [picture]) == text
        finally:
            os.unlink(picture)
        path = log._path(key)
        assert not attachments_dir(path.parent, path.stem).exists()

    @pytest.mark.asyncio
    async def test_a_slot_deleted_during_the_copy_gets_no_turn(self, log):
        slot = _ChatSlot("slot1")
        ds = _state(slot, log)
        real_store = ConversationLog.store_ingested_images

        def delete_while_copying(self_, key, content, images, **kw):
            stored = real_store(self_, key, content, images, **kw)
            ds._slots.pop(slot.key)  # what the dashboard's delete does
            ds.get_linked_slot.return_value = None
            return stored

        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        from kiro_crew.slack import handler

        slack = MagicMock()
        slack.post_message = AsyncMock()
        try:
            with (
                patch.object(ConversationLog, "store_ingested_images", delete_while_copying),
                patch.object(handler, "_dashboard_state", ds),
                patch.object(handler, "is_allowed_user", return_value=True),
                patch(
                    "kiro_crew.slack.handler_runtime.inbound.closed_dashboard_owner_refused",
                    new=AsyncMock(return_value=False),
                ),
                patch("kiro_crew.dashboard.chat._run_chat", new_callable=AsyncMock) as run_chat,
            ):
                outcome = await handler.route_linked_thread(
                    _slack_text("what is this?", picture), "slack:t1", "U1", "C1", slack, "t1"
                )
        finally:
            os.unlink(picture)
        # Not routed: the plain Slack path handles it, with the temp files still there.
        assert outcome is handler.LinkedRoute.UNROUTED
        run_chat.assert_not_called()
        assert slot.messages == []
        # The files are recorded again for whatever handles the message next.
        assert linked_thread_images._INBOUND_FILES.get() == (picture,)

    @pytest.mark.asyncio
    async def test_a_thread_relinked_during_the_copy_runs_on_the_new_slot(self, log):
        old, new = _ChatSlot("slot1"), _ChatSlot("slot2")
        ds = _state(old, log)
        ds._slots[new.key] = new
        real_store = ConversationLog.store_ingested_images

        def relink_while_copying(self_, key, content, images, **kw):
            stored = real_store(self_, key, content, images, **kw)
            ds.get_linked_slot.return_value = new
            return stored

        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        try:
            with patch.object(ConversationLog, "store_ingested_images", relink_while_copying):
                run_chat = await _route(ds, _slack_text("what is this?", picture))
        finally:
            os.unlink(picture)
        assert run_chat.call_args[0][1] is new
        assert old.messages == []
        assert len(list(_store(log, new).iterdir())) == 1
        assert len(_image_blocks(run_chat.call_args[0][2])) == 1

    @pytest.mark.asyncio
    async def test_a_relink_keeps_a_picture_another_row_already_stored(self, log):
        """Stored names come from the content, so the old session's copy can be the
        same file a committed row names; a relink must leave it in place."""
        old, new = _ChatSlot("slot1"), _ChatSlot("slot2")
        ds = _state(old, log)
        ds._slots[new.key] = new
        earlier = _temp(_png(), ".png")
        try:
            log.store_ingested_images(
                slot_history_key(old), _slack_text("earlier", earlier), [earlier]
            )
        finally:
            os.unlink(earlier)
        kept = list(_store(log, old).iterdir())
        assert len(kept) == 1
        real_store = ConversationLog.store_ingested_images

        def relink_while_copying(self_, key, content, images, **kw):
            stored = real_store(self_, key, content, images, **kw)
            ds.get_linked_slot.return_value = new
            return stored

        picture = _temp(_png(), ".png")
        linked_thread_images.set_inbound_files([picture])
        try:
            with patch.object(ConversationLog, "store_ingested_images", relink_while_copying):
                await _route(ds, _slack_text("what is this?", picture))
        finally:
            os.unlink(picture)
        assert all(path.exists() for path in kept)


class TestQueuedEntryFiles:
    @pytest.mark.asyncio
    async def test_a_queued_entry_routes_with_its_own_files(self):
        """The drain task inherits the context of the message whose task queued it;
        it must route with the entry's own files, or with none."""
        from kiro_crew.slack import events, events_queue

        seen: list[tuple[str, ...]] = []

        async def capture(*_a, **_k):
            seen.append(linked_thread_images._INBOUND_FILES.get())

        orch = MagicMock()
        orch.slack = MagicMock()
        orch.slack.remove_reaction = AsyncMock()
        linked_thread_images.set_inbound_files(["/tmp/tmpfirstmsg.png"])
        with (
            # Through the facade: the events family shares these names (facade mirror).
            patch.object(events, "handle_message_transport", side_effect=capture),
            patch.object(events, "handle_message", side_effect=capture),
        ):
            await events_queue._dispatch_queued(
                orch,
                "sk",
                "2.0",
                "queued text",
                {"channel": "C1", "thread_ts": "t1", "image_temp_paths": ["/tmp/tmpqueued1.png"]},
                slack=orch.slack,
            )
            await events_queue._dispatch_queued(
                orch, "sk", "3.0", "plain", {"channel": "C1", "thread_ts": "t1"}, slack=orch.slack
            )
        assert seen == [("/tmp/tmpqueued1.png",), ()]


class TestEventPathRecordsFiles:
    @pytest.mark.asyncio
    async def test_the_event_path_records_the_message_files(self):
        """``_route_message`` hands the router the files it downloaded for this message."""
        from kiro_crew.slack import events

        seen: list[tuple[str, ...]] = []

        async def capture(*_a, **_k):
            seen.append(linked_thread_images._INBOUND_FILES.get())

        picture = _temp(_png(), ".png")
        ch_cfg = MagicMock()
        ch_cfg.activation = "mention"
        ch_cfg.thread_follow = True
        orch = AsyncMock()
        orch._cfg = MagicMock()
        orch._cfg.channel_config.return_value = ch_cfg
        orch.channel_history = None
        orch.sessions = None
        orch.conv_log = None
        orch.slack = MagicMock()
        orch.slack.add_reaction = AsyncMock()
        orch.slack.remove_reaction = AsyncMock()
        orch._session_tasks = {}
        orch._handler_tasks = set()
        seen_cache = MagicMock()
        seen_cache.check_and_add = lambda _x: False
        seen_cache.check = lambda _x: False
        seen_cache.add = lambda _x: None
        event = {
            "user": "U1",
            "channel": "C1",
            "text": "what is this?",
            "ts": "1.0",
            "team": "T1",
            "files": [{"id": "F1", "name": "photo.png", "mimetype": "image/png"}],
        }
        linked_thread_images.set_inbound_files([])
        try:
            with (
                patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
                patch.object(events, "sel") as sel,
                patch.object(events, "is_allowed_user", return_value=True),
                patch.object(events, "is_owner", return_value=True),
                patch.object(
                    events, "process_slack_files", new=AsyncMock(return_value=([picture], []))
                ),
                patch.object(events, "code_channel_dispatch", new=AsyncMock(return_value=False)),
                patch.object(events, "hold_if_slack_down", return_value=False),
                patch.object(events, "handle_message_transport", side_effect=capture),
                patch.object(events, "handle_message", side_effect=capture),
            ):
                sel.return_value.log_api_access = lambda **_kw: None
                await events._route_message(orch, event, seen_cache, is_mention=True)
                # The turn runs as its own task, which inherits this context.
                for task in list(orch._session_tasks.values()):
                    await task
        finally:
            if os.path.exists(picture):
                os.unlink(picture)
        assert seen == [(picture,)]
