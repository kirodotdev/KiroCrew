"""Tests for Slack message queue on SessionManager."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.session import SessionManager, _Session

# ── Unit tests for _Session queue fields ──


class TestSessionQueue:
    def _make_session(self) -> _Session:
        provider = MagicMock()
        provider.is_alive.return_value = True
        return _Session(provider=provider)

    def test_queue_starts_empty(self):
        sess = self._make_session()
        assert len(sess.queue) == 0
        assert sess.cancelled == set()

    def test_cancelled_independent_per_session(self):
        s1 = self._make_session()
        s2 = self._make_session()
        s1.cancelled.add("ts1")
        assert "ts1" not in s2.cancelled


# ── Unit tests for SessionManager queue methods ──


class TestSessionManagerQueue:
    @staticmethod
    def _make_mgr() -> tuple[SessionManager, _Session]:
        mgr = SessionManager.__new__(SessionManager)
        mgr._sessions = {}
        mgr._lock = asyncio.Lock()
        provider = MagicMock()
        provider.is_alive.return_value = True
        sess = _Session(provider=provider)
        mgr._sessions["thread1"] = sess
        return mgr, sess

    def test_enqueue_returns_false_when_unlocked(self):
        mgr, sess = self._make_mgr()
        assert mgr.enqueue("thread1", "ts1", "hello") is False
        assert len(sess.queue) == 0

    def test_enqueue_force_bypasses_lock_check(self):
        mgr, sess = self._make_mgr()
        assert mgr.enqueue("thread1", "ts1", "hello", force=True) is True
        assert len(sess.queue) == 1

    @pytest.mark.asyncio
    async def test_enqueue_returns_true_when_locked(self):
        mgr, sess = self._make_mgr()
        await sess.semaphore.acquire()
        assert mgr.enqueue("thread1", "ts1", "hello", channel="C1") is True
        assert len(sess.queue) == 1
        assert sess.queue[0] == ("ts1", "hello", {"channel": "C1"})
        sess.semaphore.release()

    def test_enqueue_unknown_session(self):
        mgr, _ = self._make_mgr()
        assert mgr.enqueue("unknown", "ts1", "hi") is False

    def test_dequeue_empty(self):
        mgr, _ = self._make_mgr()
        assert mgr.dequeue("thread1") is None

    def test_dequeue_unknown_session(self):
        mgr, _ = self._make_mgr()
        assert mgr.dequeue("unknown") is None

    @pytest.mark.asyncio
    async def test_dequeue_fifo(self):
        mgr, sess = self._make_mgr()
        await sess.semaphore.acquire()
        mgr.enqueue("thread1", "ts1", "first")
        mgr.enqueue("thread1", "ts2", "second")
        sess.semaphore.release()
        result = mgr.dequeue("thread1")
        assert result is not None
        assert result[0] == "ts1"
        assert result[1] == "first"

    @pytest.mark.asyncio
    async def test_dequeue_skips_cancelled(self):
        mgr, sess = self._make_mgr()
        await sess.semaphore.acquire()
        mgr.enqueue("thread1", "ts1", "first")
        mgr.enqueue("thread1", "ts2", "second")
        sess.semaphore.release()
        sess.cancelled.add("ts1")
        result = mgr.dequeue("thread1")
        assert result is not None
        assert result[0] == "ts2"
        assert "ts1" not in sess.cancelled  # cleaned up

    def test_cancel_queued_removes_from_queue(self):
        mgr, sess = self._make_mgr()
        sess.queue.append(("ts1", "hello", {}))
        sess.queue.append(("ts2", "world", {}))
        assert mgr.cancel_queued("thread1", "ts1") is True
        assert len(sess.queue) == 1
        assert sess.queue[0][0] == "ts2"

    @pytest.mark.asyncio
    async def test_cancel_queued_adds_to_cancelled_if_not_in_queue(self):
        mgr, sess = self._make_mgr()
        await sess.semaphore.acquire()  # simulate in-flight processing
        assert mgr.cancel_queued("thread1", "ts_inflight") is False
        assert "ts_inflight" in sess.cancelled
        sess.semaphore.release()

    def test_cancel_queued_skips_cancelled_when_not_inflight(self):
        mgr, sess = self._make_mgr()
        assert mgr.cancel_queued("thread1", "ts_stale") is False
        assert "ts_stale" not in sess.cancelled

    def test_cancel_queued_unknown_session(self):
        mgr, _ = self._make_mgr()
        assert mgr.cancel_queued("unknown", "ts1") is False

    def test_is_cancelled_consumes_flag(self):
        mgr, sess = self._make_mgr()
        sess.cancelled.add("ts1")
        assert mgr.is_cancelled("thread1", "ts1") is True
        assert mgr.is_cancelled("thread1", "ts1") is False  # consumed

    def test_is_cancelled_unknown_session(self):
        mgr, _ = self._make_mgr()
        assert mgr.is_cancelled("unknown", "ts1") is False

    def test_clear_queue(self):
        mgr, sess = self._make_mgr()
        sess.queue.append(("ts1", "hello", {}))
        sess.cancelled.add("ts2")
        mgr.clear_queue("thread1")
        assert len(sess.queue) == 0
        assert sess.cancelled == set()


# ── Integration test: message_deleted event handling ──


class TestMessageDeletedEvent:
    @pytest.mark.asyncio
    async def test_message_deleted_removes_from_queue(self):
        """message_deleted subtype should cancel a queued message."""
        mgr, sess = TestSessionManagerQueue._make_mgr()
        await sess.semaphore.acquire()
        mgr.enqueue("thread1", "ts_queued", "will be deleted", channel="C1")
        assert len(sess.queue) == 1
        was_queued = mgr.cancel_queued("thread1", "ts_queued")
        assert was_queued is True
        assert len(sess.queue) == 0
        sess.semaphore.release()


class TestHandleMessageDeleted:
    """Tests for the extracted _handle_message_deleted function."""

    @staticmethod
    def _make_event(deleted_ts="ts_del", thread_ts="thread1", channel="C1", user="U_ALLOWED"):
        return {
            "deleted_ts": deleted_ts,
            "channel": channel,
            "previous_message": {"thread_ts": thread_ts, "user": user},
        }

    @staticmethod
    def _make_orch():
        orch = MagicMock()
        orch.sessions = MagicMock()
        # A bare MagicMock returns a truthy Mock for every accessor, so an
        # unconfigured is_busy would route this message down the mid-turn
        # steer path and the handler would return before processing it.
        orch.sessions.is_busy.return_value = False
        orch.sessions.cancel_queued = MagicMock(return_value=False)
        orch._pending_queue = {}
        return orch

    @pytest.mark.asyncio
    async def test_unauthorized_user_ignored(self):
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        event = self._make_event(user="U_BAD")
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=False), \
             patch("kiro_crew.slack.events.sel"):
            await _handle_message_deleted(orch, event)
        orch.sessions.cancel_queued.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancels_from_session_queue(self):
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        orch.sessions.cancel_queued.return_value = True
        event = self._make_event()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.sel") as mock_sel:
            await _handle_message_deleted(orch, event)
        orch.sessions.cancel_queued.assert_called_once_with("thread1", "ts_del")
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancels_from_pending_queue(self):
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        orch._pending_queue = {"thread1": [("ts_del", "hello", {}), ("ts_other", "keep", {})]}
        event = self._make_event()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.sel"):
            await _handle_message_deleted(orch, event)
        assert orch._pending_queue == {"thread1": [("ts_other", "keep", {})]}

    @pytest.mark.asyncio
    async def test_pending_queue_cleaned_when_empty(self):
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        orch._pending_queue = {"thread1": [("ts_del", "hello", {})]}
        event = self._make_event()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.sel"):
            await _handle_message_deleted(orch, event)
        assert "thread1" not in orch._pending_queue

    @pytest.mark.asyncio
    async def test_session_key_falls_back_to_deleted_ts(self):
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        orch.sessions.cancel_queued.return_value = True
        event = {"deleted_ts": "ts_dm", "channel": "D1", "previous_message": {"user": "U1"}}
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.sel"):
            await _handle_message_deleted(orch, event)
        # No thread_ts → session_key = deleted_ts
        orch.sessions.cancel_queued.assert_called_once_with("ts_dm", "ts_dm")

    @pytest.mark.asyncio
    async def test_pending_queue_cleaned_when_sessions_none(self):
        """_pending_queue cleanup must work even when orch.sessions is None."""
        from kiro_crew.slack.events import _handle_message_deleted

        orch = self._make_orch()
        orch.sessions = None  # startup window — no session manager yet
        orch._pending_queue = {"thread1": [("ts_del", "hello", {})]}
        event = self._make_event()
        with patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.events.sel"):
            await _handle_message_deleted(orch, event)
        assert "thread1" not in orch._pending_queue


# ── Events.py: _dispatch_queued ──


class TestDispatchQueued:
    @pytest.mark.asyncio
    async def test_removes_reaction_and_calls_handler(self):
        from kiro_crew.slack.events import _dispatch_queued

        orch = MagicMock()
        orch.slack = AsyncMock()
        orch.sessions = MagicMock()
        # A bare MagicMock returns a truthy Mock for every accessor, so an
        # unconfigured is_busy would route this message down the mid-turn
        # steer path and the handler would return before processing it.
        orch.sessions.is_busy.return_value = False
        orch.sessions.is_cancelled = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch.sessions.clear_queue = MagicMock()
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.ctx_builder = None
        orch.cron_svc = None
        orch.conv_log = None
        orch.consolidator = None
        orch.subagent_mgr = None
        orch.task_runner = None
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            await _dispatch_queued(orch, "thread1", "ts_q", "hello", {"channel": "C1", "thread_ts": "thread1"})
        orch.slack.remove_reaction.assert_awaited_once_with("C1", "ts_q", "hourglass_flowing_sand")
        mock_hm.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_swallows_reaction_error(self):
        from kiro_crew.slack.events import _dispatch_queued

        orch = MagicMock()
        orch.slack = AsyncMock()
        orch.slack.remove_reaction = AsyncMock(side_effect=Exception("gone"))
        orch.sessions = MagicMock()
        # A bare MagicMock returns a truthy Mock for every accessor, so an
        # unconfigured is_busy would route this message down the mid-turn
        # steer path and the handler would return before processing it.
        orch.sessions.is_busy.return_value = False
        orch.sessions.is_cancelled = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch.sessions.clear_queue = MagicMock()
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.ctx_builder = None
        orch.cron_svc = None
        orch.conv_log = None
        orch.consolidator = None
        orch.subagent_mgr = None
        orch.task_runner = None
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            await _dispatch_queued(orch, "thread1", "ts_q", "hello", {"channel": "C1"})
        mock_hm.assert_awaited_once()


# ── Events.py: queue routing in _route_message ──


def _make_route_orch() -> MagicMock:
    """Minimal mock orch that passes _route_message guards."""
    from kiro_crew.config.loader import ACTIVATION_ALWAYS, KiroCrewConfig

    orch = MagicMock()
    orch._cfg = KiroCrewConfig(slack_channels={}, slack_dm_activation=ACTIVATION_ALWAYS)
    orch.channel_history = MagicMock()
    orch.slack = AsyncMock()
    orch.sessions = MagicMock()
    # A bare MagicMock returns a truthy Mock for every accessor, so an
    # unconfigured is_busy would route this message down the mid-turn
    # steer path and the handler would return before processing it.
    orch.sessions.is_busy.return_value = False
    orch.sessions.enqueue = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.cancel_queued = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.clear_queue = MagicMock()
    orch.sessions.has_session = MagicMock(return_value=False)
    orch.ctx_builder = None
    orch.cron_svc = None
    orch.conv_log = None
    orch.consolidator = None
    orch.subagent_mgr = None
    orch.task_runner = None
    orch._handler_tasks = set()
    orch._session_tasks = {}
    orch._pending_queue = {}
    return orch


_ROUTE_PATCHES = [
    patch("kiro_crew.slack.events.is_allowed_user", return_value=True),
    patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True),
]


class TestQueueRouting:
    @pytest.mark.asyncio
    async def test_busy_session_enqueues_with_force(self):
        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_route_orch()
        orch._session_tasks["ts_new"] = MagicMock()  # DM: session_key = msg_ts
        orch.sessions.enqueue.return_value = True
        event = {"user": "U1", "text": "queued", "ts": "ts_new", "channel": "D1", "channel_type": "im", "team": "T1"}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
            for p in _ROUTE_PATCHES:
                p.start()
            try:
                await _route_message(orch, event, SeenCache(), is_mention=True)
            finally:
                for p in _ROUTE_PATCHES:
                    p.stop()
        orch.sessions.enqueue.assert_called_once()
        assert orch.sessions.enqueue.call_args[1].get("force") is True
        orch.slack.add_reaction.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_busy_session_falls_back_to_pending_queue(self):
        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_route_orch()
        orch._session_tasks["thread1"] = MagicMock()
        orch.sessions.enqueue.return_value = False  # no session object
        event = {"user": "U1", "text": "queued", "ts": "ts_new", "thread_ts": "thread1", "channel": "C1", "channel_type": "channel", "team": "T1"}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
            for p in _ROUTE_PATCHES:
                p.start()
            try:
                await _route_message(orch, event, SeenCache(), is_mention=True)
            finally:
                for p in _ROUTE_PATCHES:
                    p.stop()
        assert hasattr(orch, "_pending_queue")
        assert "thread1" in orch._pending_queue
        assert orch._pending_queue["thread1"][0][0] == "ts_new"

    @pytest.mark.asyncio
    async def test_non_busy_enqueue_returns_true_queues(self):
        """elif branch: no task running but enqueue returns True (semaphore locked)."""
        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_route_orch()
        orch.sessions.enqueue.return_value = True  # semaphore locked
        event = {"user": "U1", "text": "queued", "ts": "ts_new", "channel": "D1", "channel_type": "im", "team": "T1"}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock):
            for p in _ROUTE_PATCHES:
                p.start()
            try:
                await _route_message(orch, event, SeenCache(), is_mention=True)
            finally:
                for p in _ROUTE_PATCHES:
                    p.stop()
        orch.slack.add_reaction.assert_awaited_once()


class TestOnDoneDrain:
    @pytest.mark.asyncio
    async def test_drains_session_queue_after_task(self):
        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_route_orch()
        orch.sessions.enqueue.return_value = False
        # After task completes, dequeue returns a queued message once then None
        orch.sessions.dequeue.side_effect = [
            ("ts_q", "queued text", {"channel": "C1", "thread_ts": "thread1"}),
            None,
        ]
        event = {"user": "U1", "text": "first", "ts": "ts1", "channel": "D1", "channel_type": "im", "team": "T1"}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock), \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=True)
            await asyncio.sleep(0.05)
            # Drain should have dispatched the queued message via _dispatch_queued
            tasks = list(orch._handler_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        # dequeue was called in _on_done
        orch.sessions.dequeue.assert_called()

    @pytest.mark.asyncio
    async def test_drains_pending_queue_after_task(self):
        from kiro_crew.slack.events import SeenCache, _route_message

        orch = _make_route_orch()
        orch.sessions.enqueue.return_value = False
        orch.sessions.dequeue.return_value = None  # session queue empty
        # Stash in pending queue
        orch._pending_queue = {"ts1": [("ts_pq", "pending", {"channel": "C1"})]}
        event = {"user": "U1", "text": "first", "ts": "ts1", "channel": "D1", "channel_type": "im", "team": "T1"}
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock), \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True), \
             patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True):
            await _route_message(orch, event, SeenCache(), is_mention=True)
            await asyncio.sleep(0.05)
            tasks = list(orch._handler_tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        # pending queue should have been drained
        assert "ts1" not in getattr(orch, "_pending_queue", {})


# ── Tests for stop_turn preserve_queue (interrupt flow) ──


class TestStopTurnPreserveQueue:
    """Tests that stop_turn(preserve_queue=True) keeps the queue intact."""

    @staticmethod
    def _make_mgr_with_cfg() -> tuple[SessionManager, _Session]:
        mgr = SessionManager.__new__(SessionManager)
        mgr._sessions = {}
        mgr._lock = asyncio.Lock()
        mgr._background_tasks = set()
        cfg = MagicMock()
        cfg.agent.soft_stop_budget_secs = 5.0
        mgr._cfg = cfg
        provider = AsyncMock()
        provider.cancel = AsyncMock(return_value="acked")
        provider.is_alive.return_value = True
        sess = _Session(provider=provider)
        mgr._sessions["slot1"] = sess
        return mgr, sess

    @pytest.mark.asyncio
    async def test_stop_turn_clears_queue_by_default(self):
        mgr, sess = self._make_mgr_with_cfg()
        mgr.enqueue("slot1", "ts1", "msg1", force=True)
        assert len(sess.queue) == 1
        await mgr.stop_turn("slot1")
        assert len(sess.queue) == 0

    @pytest.mark.asyncio
    async def test_stop_turn_preserves_queue_when_flag_set(self):
        mgr, sess = self._make_mgr_with_cfg()
        mgr.enqueue("slot1", "ts1", "msg1", force=True)
        mgr.enqueue("slot1", "ts2", "msg2", force=True)
        assert len(sess.queue) == 2
        await mgr.stop_turn("slot1", preserve_queue=True)
        # Queue should still have both messages
        assert len(sess.queue) == 2
        assert sess.queue[0][1] == "msg1"
        assert sess.queue[1][1] == "msg2"

    @pytest.mark.asyncio
    async def test_stop_turn_preserve_queue_still_cancels_provider(self):
        mgr, sess = self._make_mgr_with_cfg()
        mgr.enqueue("slot1", "ts1", "msg1", force=True)
        outcome = await mgr.stop_turn("slot1", preserve_queue=True)
        # Provider cancel should still be called
        sess.provider.cancel.assert_called_once()
        assert outcome == "soft"

    @pytest.mark.asyncio
    async def test_stop_turn_preserve_queue_marks_prev_turn_cancelled(self):
        mgr, sess = self._make_mgr_with_cfg()
        mgr.enqueue("slot1", "ts1", "msg1", force=True)
        await mgr.stop_turn("slot1", preserve_queue=True)
        assert sess.prev_turn_cancelled is True


class TestQueuedMessageImagePaths:
    """A message with image attachments that arrives while the session is busy
    is enqueued with its clean_text embedding the downloaded image temp-file
    paths. The enqueue path previously called _cleanup_image_temps() immediately,
    os.unlink()ing those files before the queued turn ran — so at dispatch
    p.is_file() was False and _send_prompt silently dropped the images. The fix
    carries the paths in the queue kwargs and defers unlink to _dispatch_queued
    (after the turn consumes them)."""

    def test_enqueue_round_trips_image_temp_paths(self):
        mgr = SessionManager.__new__(SessionManager)
        mgr._sessions = {}
        mgr._lock = asyncio.Lock()
        mgr._sessions["thread1"] = _Session(provider=MagicMock())
        assert mgr.enqueue(
            "thread1", "ts1", "look at this\n/tmp/img_abc.png",
            force=True, image_temp_paths=["/tmp/img_abc.png"],
        ) is True
        msg_ts, text, kwargs = mgr.dequeue("thread1")
        assert msg_ts == "ts1"
        assert kwargs["image_temp_paths"] == ["/tmp/img_abc.png"]

    @pytest.mark.asyncio
    async def test_dispatch_queued_unlinks_images_after_turn(self, tmp_path):
        import kiro_crew.slack.events as events

        # A real temp file that must survive until dispatch, then be cleaned up.
        img = tmp_path / "img_queued.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        assert img.is_file()

        seen_paths: dict = {}

        async def fake_handle_message(*args, **kwargs):
            # At dispatch time the image file must still exist (the bug deleted
            # it before this point). Record its existence for the assertion.
            seen_paths["existed_at_dispatch"] = img.is_file()

        orch = MagicMock()
        orch.slack = None  # skip reaction removal

        with patch.object(events, "handle_message", fake_handle_message):
            await events._dispatch_queued(
                orch, "thread1", "ts1", f"see {img}",
                {"sender_id": "U1", "image_temp_paths": [str(img)]},
            )

        assert seen_paths["existed_at_dispatch"] is True  # survived until the turn
        assert not img.is_file()  # cleaned up afterwards (no leak)
