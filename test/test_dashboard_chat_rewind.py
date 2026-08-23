"""Tests for POST /api/chat/slots/{slot}/rewind — edit-and-rewind in place."""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture(autouse=True)
def _mock_run_chat(monkeypatch):
    """Replace _run_chat with a no-op so tests don't try to spawn kiro-cli."""
    run_chat_mock = AsyncMock(return_value=None)
    monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._run_chat", run_chat_mock)
    return run_chat_mock


def _populate_slot(state, key="src"):
    """Create a slot with 4 visible messages: u/a/u/a."""
    slot = state.get_or_create_slot(key)
    slot.title = "My Chat"
    slot._titled = True
    slot.append("user", "first question", "msg msg-u", ts="2026-05-21T16:00:00Z")
    slot.append("assistant", "first answer", "msg msg-a", ts="2026-05-21T16:00:01Z")
    slot.append("user", "second question", "msg msg-u", ts="2026-05-21T16:00:02Z")
    slot.append("assistant", "second answer", "msg msg-a", ts="2026-05-21T16:00:03Z")
    slot.drain()
    return slot


class TestRewindSlot:
    """POST /api/chat/slots/{slot}/rewind — edit any past user message in place."""

    @pytest.mark.asyncio
    async def test_rewind_first_user_message_truncates_all(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        # session_map.get returns "" so orphan cleanup is skipped
        state.sessions._session_map.get = MagicMock(return_value="")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["at_message_index"] == 0

        # Slot keeps its identity but messages are truncated to just the new user msg
        assert slot.title == "My Chat"  # unchanged
        assert slot.key == "src"  # unchanged
        roles = [m["role"] for m in slot.messages]
        assert roles == ["user"]
        assert slot.messages[0]["content"] == "edited first question"
        # The replacement must not resume the discarded native conversation.
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:src", skip_if_busy=True
        )
        state.sessions.remove.assert_not_awaited()
        # Cleanup runs
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_discards_queued_successors(self, tmp_path):
        """Queued work from the discarded suffix must not run afterwards."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        queue_id = slot.queue_append("discarded queued prompt")
        slot.append("queued", "discarded queued prompt", json.dumps({"queue_id": queue_id}))
        slot.drain()
        state.sessions._session_map.get = MagicMock(return_value="")

        with patch.object(state, "broadcast_ws") as broadcast:
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": "edited first question"},
                )
                assert resp.status == 200

        assert slot.queue_depth == 0
        assert all(message["role"] != "queued" for message in slot.messages)
        broadcast.assert_any_call("queue_cancel", {"slot": "src", "queue_id": queue_id})
        assert not any(call.args[0] == "queue_pop" for call in broadcast.call_args_list)
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_failed_save_keeps_live_and_persisted_branch_during_flush(
        self, tmp_path, monkeypatch
    ):
        """A flush pending beside a rejected rewrite must save the original window."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        queue_id = slot.queue_append("discarded queued prompt")
        slot.append("queued", "discarded queued prompt", json.dumps({"queue_id": queue_id}))
        slot.drain()
        state.sessions._session_map.get = MagicMock(return_value="")
        original_messages = list(slot.messages)
        original_queue = list(slot._queue)
        slot._question_pending = {"question-1": {"blocking": False}}
        retired = MagicMock()
        slot._on_question_retired = retired
        slot._dirty = True

        save_started = threading.Event()
        fail_save = threading.Event()

        def _wait_then_fail(_state, saved_slot, _messages, **kwargs):
            # The save goes through the LIVE slot so its own
            # expected_history_key guard can see a concurrent rebind; the
            # candidate window travels as the explicit messages snapshot and
            # the live slot is not mutated before the commit.
            assert saved_slot is slot
            assert kwargs.get("expected_history_key")
            assert slot._question_pending == {"question-1": {"blocking": False}}
            retired.assert_not_called()
            save_started.set()
            fail_save.wait()
            raise OSError("disk full")

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_rewind._save_slot_to_history", _wait_then_fail
        )
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            request_task = asyncio.create_task(
                client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": "edited first question"},
                )
            )
            try:
                await asyncio.wait_for(asyncio.to_thread(save_started.wait), timeout=1)
                await asyncio.to_thread(state.flush_slot_now, slot)
                fail_save.set()
                resp = await request_task
            finally:
                fail_save.set()
                if not request_task.done():
                    await request_task

            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

        persisted = state.conversation_log.read_messages("dashboard:src")
        assert [(m["role"], m["content"]) for m in persisted] == [
            (m["role"], m["content"]) for m in original_messages if m["role"] != "queued"
        ]
        assert slot.messages == original_messages
        assert slot._queue == original_queue
        assert slot._question_pending == {"question-1": {"blocking": False}}
        retired.assert_not_called()

    @pytest.mark.asyncio
    async def test_rewind_rejects_when_the_boundary_cannot_be_saved(self, tmp_path, monkeypatch):
        """A failed rewrite must not start a replacement from stale disk state."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")

        def _fail_save(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._save_slot_to_history", _fail_save)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

        assert slot.messages == original_messages
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:src", skip_if_busy=True
        )

    @pytest.mark.asyncio
    async def test_rewind_rejects_when_the_native_boundary_cannot_be_saved(self, tmp_path):
        """A failed resume-sid write must leave the old branch in place."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")
        state.sessions.discard_conversation = AsyncMock(side_effect=OSError("map write failed"))

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_prepare_failed"

        assert slot.messages == original_messages

    @pytest.mark.asyncio
    async def test_rewind_rejects_when_the_save_is_refused(self, tmp_path, monkeypatch):
        """A save refused by its own guards (returns False) must 503, not dispatch.

        ``_save_slot_to_history`` returns ``False`` without writing when the
        session was permanently deleted or the slot was rebound while the
        write awaited its lock; reporting success would dispatch a turn from
        state that was never persisted.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_rewind._save_slot_to_history",
            MagicMock(return_value=False),
        )

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_save_failed"

        assert slot.messages == original_messages

    @pytest.mark.asyncio
    async def test_rewind_reserves_the_slot_and_keeps_concurrent_queue_entries(
        self, tmp_path, _mock_run_chat
    ):
        """A send arriving during the awaited boundaries queues and survives.

        The reservation makes ``slot.running`` read True while the durable
        boundaries are pending, so a concurrent send takes the queue path
        instead of starting a competing turn -- and the commit removes only
        the pre-await snapshot, never the entry that arrived during it.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        discarded_id = slot.queue_append("discarded queued prompt")
        state.sessions._session_map.get = MagicMock(return_value="")
        observed: dict = {}

        async def _discard(key, **kwargs):
            # Runs inside the awaited boundary: the reservation must already
            # be visible, and a producer can still reach the queue.
            observed["running"] = slot.running
            observed["arrived_id"] = slot.queue_append("queued during rewind")
            return True

        state.sessions.discard_conversation = AsyncMock(side_effect=_discard)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 200

        assert observed["running"] is True
        queued_ids = [entry["id"] for entry in slot._queue]
        assert observed["arrived_id"] in queued_ids
        assert discarded_id not in queued_ids
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_commit_keeps_the_post_save_persistence_witnesses(
        self, tmp_path, monkeypatch, _mock_run_chat
    ):
        """The commit must not restore pre-save persistence witnesses.

        The save runs on the LIVE slot and stamps the post-rewrite truth:
        ``_pending_rewrite`` cleared and the ``_disk_*`` witnesses matching
        the truncated file. Copying the prospective slot's pre-save values
        back would re-arm ``_pending_rewrite`` -- so the NEXT flush repeats
        the destructive rewrite and can discard a cross-process append that
        landed in between -- and would move the monotone ``_disk_tail_ts``
        floor backwards.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        slot._pending_rewrite = True  # pre-save: a rewrite is owed
        slot._disk_tail_ts = "2026-05-21T15:00:00Z"
        state.sessions._session_map.get = MagicMock(return_value="")

        def _save_stamps_witnesses(_state, saved_slot, msgs, *, expected_history_key):
            # Emulate the real save's post-write bookkeeping on the live slot.
            saved_slot._pending_rewrite = False
            saved_slot._disk_window_len = len(msgs)
            saved_slot._disk_meta_observed = True
            saved_slot.note_disk_tail("2026-05-21T16:00:05Z")
            return True

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_rewind._save_slot_to_history",
            _save_stamps_witnesses,
        )

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 200

        assert slot._pending_rewrite is False
        assert slot._disk_window_len == 1
        assert slot._disk_meta_observed is True
        assert slot._disk_tail_ts == "2026-05-21T16:00:05Z"
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_app_cannot_reach_a_channel_linked_session(self, tmp_path):
        """An app-owned slot with a channel link must not rewind through it.

        ``effective_session_key`` resolves a linked slot to the channel's own
        session, so an app rewind would clear the native identity of a
        conversation the app does not own. Denied with the same 404 shape as
        the ownership check (anti-enumeration).
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        slot._app = "some-app"
        slot.linked_session_key = "slack:1234567890.123"
        original_messages = list(slot.messages)

        from aiohttp import web as _web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

        app = _make_app(state)
        fake_request = make_mocked_request(
            "POST",
            "/api/chat/slots/src/rewind",
            match_info={"slot": "src"},
            app=app,
        )
        fake_request["app"] = "some-app"  # owns the slot, but the slot is linked

        async def _json():
            return {"at_message_index": 0, "content": "x"}

        fake_request.json = _json  # type: ignore[method-assign]
        try:
            resp = await api_chat_slot_rewind(fake_request)
        except _web.HTTPException as exc:
            resp = exc
        assert resp.status == 404
        assert slot.messages == original_messages
        state.sessions.discard_conversation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewind_rejects_when_the_sid_flush_fails(self, tmp_path):
        """The cleared resume sid must be durable before the commit.

        ``discard_conversation`` lands the sid clear in the session map's
        debounced writer; the endpoint forces the durability point and a
        flush failure takes the same 503 path as a failed discard.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")
        state.sessions.aflush = AsyncMock(side_effect=OSError("map write failed"))

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 503
            assert (await resp.json())["code"] == "rewind_prepare_failed"

        assert slot.messages == original_messages
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:src", skip_if_busy=True
        )
        state.sessions.aflush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rewind_refuses_the_commit_when_the_slot_is_rebound(self, tmp_path):
        """A slot rebound to another transcript mid-save must not be replaced.

        A cron injection can re-link the slot (and hydrate it with another
        conversation's state) while the history write is in flight; the
        commit re-checks the history key and refuses instead of overwriting
        the injected state.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")

        async def _rebinding_discard(key, **kwargs):
            # Runs inside the awaited boundary: the slot moves to another
            # transcript while the rewind persists.
            slot.linked_session_key = "slack:9876543210.999"
            return True

        state.sessions.discard_conversation = AsyncMock(side_effect=_rebinding_discard)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 503
            # Two fences cover different windows: the save's own
            # expected_history_key guard (rewind_save_failed) catches a rebind
            # visible at write time; the commit-side re-check
            # (rewind_slot_rebound) catches one landing after the save
            # returned. Either refusal is correct -- the point is that the
            # commit never happens.
            assert (await resp.json())["code"] in {
                "rewind_save_failed",
                "rewind_slot_rebound",
            }

        assert slot.messages == original_messages
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_refuses_while_a_channel_turn_holds_the_session(self, tmp_path):
        """A busy session (inbound channel reply in flight) must 409, not discard.

        An inbound channel turn holds the session semaphore while
        ``slot.running`` reads False, so the idle check cannot see it; the
        discard is asked with ``skip_if_busy`` and its refusal surfaces as a
        retryable 409 with the slot untouched.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        original_messages = list(slot.messages)
        state.sessions._session_map.get = MagicMock(return_value="")
        state.sessions.discard_conversation = AsyncMock(return_value=False)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "edited first question"},
            )
            assert resp.status == 409
            assert (await resp.json())["code"] == "rewind_session_busy"

        assert slot.messages == original_messages
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:src", skip_if_busy=True
        )
        state.sessions.aflush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rewind_cancelled_mid_save_still_commits_the_landed_rewrite(
        self, tmp_path, monkeypatch, _mock_run_chat
    ):
        """A client disconnect during the save must not abandon the rewrite.

        The worker thread finishes the destructive rewrite regardless of the
        handler's fate; on cancellation the handler waits for the worker's
        outcome, commits the live state to match the persisted one, and the
        reserved dispatch task still runs the edited prompt.
        """
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        state.sessions._session_map.get = MagicMock(return_value="")

        save_started = threading.Event()
        release = threading.Event()

        def _gated_save(*_args, **_kwargs):
            save_started.set()
            release.wait()
            return True

        monkeypatch.setattr("kiro_crew.dashboard.chat_rewind._save_slot_to_history", _gated_save)

        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

        app = _make_app(state)
        fake_request = make_mocked_request(
            "POST", "/api/chat/slots/src/rewind", match_info={"slot": "src"}, app=app
        )
        fake_request["app"] = ""

        async def _json():
            return {"at_message_index": 0, "content": "edited first question"}

        fake_request.json = _json  # type: ignore[method-assign]
        handler_task = asyncio.create_task(api_chat_slot_rewind(fake_request))
        await asyncio.wait_for(asyncio.to_thread(save_started.wait), timeout=2)
        handler_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await handler_task

        # The commit landed: truncated window plus the edited user row.
        assert [m["content"] for m in slot.messages] == ["edited first question"]
        # The reserved dispatch task still runs the edited prompt.
        for _ in range(50):
            if _mock_run_chat.await_count:
                break
            await asyncio.sleep(0.02)
        _mock_run_chat.assert_awaited_once()
        assert _mock_run_chat.await_args.args[2] == "edited first question"

    @pytest.mark.asyncio
    async def test_rewind_middle_user_message_keeps_prior_turns(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        state.sessions._session_map.get = MagicMock(return_value="")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # Index 2 is the second user message — keep first u/a, replace second user
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 2, "content": "edited second question"},
            )
            assert resp.status == 200

        roles = [m["role"] for m in slot.messages]
        # Expect: first user, first assistant, edited user (no second assistant)
        assert roles == ["user", "assistant", "user"]
        assert slot.messages[0]["content"] == "first question"
        assert slot.messages[1]["content"] == "first answer"
        assert slot.messages[2]["content"] == "edited second question"
        state.sessions.discard_conversation.assert_awaited_once_with(
            "dashboard:src", skip_if_busy=True
        )
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_by_ts_resolves_to_correct_message(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        state.sessions._session_map.get = MagicMock(return_value="")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # Use the second user message's ts
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"ts": "2026-05-21T16:00:02Z", "content": "edited via ts"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["at_message_index"] == 2

        assert slot.messages[2]["content"] == "edited via ts"
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_missing_slot_404(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/nope/rewind",
                json={"at_message_index": 0, "content": "x"},
            )
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rewind_running_slot_409(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        # slot.running is a computed property (true while slot.task is unfinished)
        loop = asyncio.get_running_loop()
        pending = loop.create_future()
        slot.task = pending  # type: ignore[assignment]
        try:
            app = _make_app(state)
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": "x"},
                )
                assert resp.status == 409
        finally:
            pending.set_result(None)

    @pytest.mark.asyncio
    async def test_rewind_empty_content_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": ""},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_too_long_content_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "x" * 32_769},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_non_string_content_rejected(self, tmp_path):
        """Non-string truthy content (int, list, dict) must be rejected before strip()."""
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            for bad_content in (123, ["x"], {"text": "y"}, True):
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": bad_content},
                )
                assert resp.status == 400, f"expected 400 for content={bad_content!r}"

    @pytest.mark.asyncio
    async def test_rewind_index_out_of_range_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 99, "content": "x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_negative_index_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": -1, "content": "x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_bool_index_rejected(self, tmp_path):
        """Booleans are int subclasses in Python — reject them explicitly."""
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": True, "content": "x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_assistant_index_rejected(self, tmp_path):
        """Index pointing at an assistant message must be rejected."""
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 1, "content": "x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_unknown_ts_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"ts": "no-such-ts", "content": "x"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_invalid_json_body_400(self, tmp_path):
        state = _make_state(tmp_path)
        _populate_slot(state)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rewind_deletes_orphan_kiro_session(self, tmp_path):
        """When session_map has a kiro session_id, it must be deleted from disk."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        # Simulate session_map containing a kiro-cli session id
        state.sessions._session_map.get = MagicMock(return_value="orphan-session-uuid")

        app = _make_app(state)
        with patch(
            "kiro_crew.dashboard.chat_rewind._delete_orphan_kiro_session",
            new=AsyncMock(),
        ) as mock_delete:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": "edited"},
                )
                assert resp.status == 200
            mock_delete.assert_awaited_once_with("orphan-session-uuid")
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_skips_orphan_cleanup_when_no_session_id(self, tmp_path):
        """When session_map has no entry, skip the cleanup attempt entirely."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        state.sessions._session_map.get = MagicMock(return_value=None)

        app = _make_app(state)
        with patch(
            "kiro_crew.dashboard.chat_rewind._delete_orphan_kiro_session",
            new=AsyncMock(),
        ) as mock_delete:
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/chat/slots/src/rewind",
                    json={"at_message_index": 0, "content": "edited"},
                )
                assert resp.status == 200
            mock_delete.assert_not_awaited()
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_runs_chat_with_redacted_content(self, tmp_path, _mock_run_chat):
        """The new edited prompt must be the argument to _run_chat."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        state.sessions._session_map.get = MagicMock(return_value="")

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 0, "content": "  edited  "},
            )
            assert resp.status == 200

        # Wait briefly for the create_task'd _run_chat to be picked up
        for _ in range(20):
            if _mock_run_chat.await_count > 0:
                break
            await asyncio.sleep(0.01)
        _mock_run_chat.assert_awaited_once()
        args = _mock_run_chat.await_args.args
        # _run_chat(state, slot, content) — content should be stripped
        assert args[2] == "edited"
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_app_isolation(self, tmp_path):
        """A request from app X cannot rewind a slot owned by app Y or no app."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        # Slot was created without an app, but request comes from an app
        slot._app = ""

        app = _make_app(state)
        # Inject a fake app marker into the request — emulating App Kit's
        # middleware. Since _make_app doesn't include that middleware, we
        # instead validate the app-isolation handler logic directly by
        # patching the request lookup.
        from aiohttp import web as _web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.chat_rewind import api_chat_slot_rewind

        fake_request = make_mocked_request(
            "POST",
            "/api/chat/slots/src/rewind",
            match_info={"slot": "src"},
            app=app,
        )
        fake_request["app"] = "some-app"  # request_app = "some-app" but slot._app = ""
        # Inject a JSON body coroutine
        fake_request._read_bytes = b'{"at_message_index":0,"content":"x"}'

        async def _json():
            return {"at_message_index": 0, "content": "x"}

        fake_request.json = _json  # type: ignore[method-assign]
        try:
            resp = await api_chat_slot_rewind(fake_request)
        except _web.HTTPException as exc:
            resp = exc
        # Cross-app access returns 404 (indistinguishable from a missing slot)
        # to prevent slot enumeration; SEL still records the true reason.
        assert resp.status == 404


class TestRewindChainedHistory:
    """POST /api/chat/slots/{slot}/rewind — chained-history index handling.

    ``slot.messages`` holds at most the last 500
    messages of the chained view; older messages live in archived sibling
    session files and ``slot._disk_older_count`` records how many. The
    handler must validate against the chained length so error messages
    match what the user sees, and translate the chained index back to a
    ``slot.messages``-relative offset for the truncation.
    """

    @pytest.mark.asyncio
    async def test_rewind_at_chained_index_in_window_translates(self, tmp_path):
        """A chained at_message_index inside the in-memory window resolves correctly."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        slot._disk_older_count = 100  # simulate 100 archived chained messages

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            # Frontend chained-index 102 corresponds to slot.messages[2]
            # (the second user message). Pre-fix, this would fail with
            # "out of range (have 4 messages)".
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 102, "content": "edited"},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            # The persisted index returned to the caller is in the chained
            # space so the frontend can correlate it with what it rendered.
            assert data["at_message_index"] == 102

        # Truncation happened at slot.messages[2:], leaving the first two
        # messages plus the new edited prompt appended.
        assert len(slot.messages) == 3
        assert slot.messages[-1]["content"] == "edited"
        assert slot.messages[-1]["role"] == "user"
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_at_chained_index_in_archive_400(self, tmp_path):
        """A chained at_message_index in the archived portion is refused clearly."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        slot._disk_older_count = 100  # 100 archived chained messages

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 50, "content": "edited"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "archived history" in data["error"]
        # Slot left untouched.
        assert len(slot.messages) == 4
        assert slot.messages[-1]["content"] == "second answer"

    @pytest.mark.asyncio
    async def test_rewind_at_chained_index_out_of_range_uses_chained_len(self, tmp_path):
        """Out-of-range error reports the chained length, not just slot.messages length."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        slot._disk_older_count = 100

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 999, "content": "edited"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "have 104 messages" in data["error"]  # disk_older + len(msgs)

    @pytest.mark.asyncio
    async def test_rewind_at_index_no_archive_unchanged(self, tmp_path):
        """When _disk_older_count is 0, behavior is identical to pre-fix path."""
        state = _make_state(tmp_path)
        slot = _populate_slot(state)
        # _disk_older_count defaults to 0; no chained translation needed.

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/rewind",
                json={"at_message_index": 2, "content": "edited"},
            )
            assert resp.status == 200, await resp.text()
        # slot.messages truncated at index 2, then edited prompt appended
        assert len(slot.messages) == 3
        assert slot.messages[-1]["content"] == "edited"
        if slot.task:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_rewind_by_ts_in_archived_chained_400(self, tmp_path):
        """A ts that resolves to a message in archived chained history is refused."""
        state = _make_state(tmp_path)
        tab_id = "tab12345abcd"
        archived_ts = "2026-05-21T12:00:00Z"

        # Older sibling session file with same tab_id holding the ts.
        older_key = "dashboard:chat-arc-1-old"
        state.conversation_log.append(older_key, "user", "archived-q", tab_id=tab_id)
        # Patch the ts on the just-written line to a known value.
        path = tmp_path / "dashboard_chat-arc-1-old.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        # Last line is the user message — rewrite its ts.
        last = json.loads(lines[-1])
        last["ts"] = archived_ts
        lines[-1] = json.dumps(last)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Current slot file with same tab_id.
        current_key = "dashboard:chat-arc-2-new"
        state.conversation_log.append(current_key, "user", "live-q", tab_id=tab_id)
        state.conversation_log.invalidate_tab_id_cache()

        slot = state.get_or_create_slot("chat-arc-2-new")
        slot._tab_id = tab_id
        slot._disk_older_count = 1  # 1 archived chained message
        slot.append("user", "live-q", "msg msg-u", ts="2026-05-22T14:00:00Z")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/chat-arc-2-new/rewind",
                json={"ts": archived_ts, "content": "edited"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "archived history" in data["error"]
        # Slot left untouched.
        assert slot.messages[-1]["content"] == "live-q"
