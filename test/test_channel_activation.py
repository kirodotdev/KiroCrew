"""Tests for per-channel activation mode filtering in Slack event routing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    ChannelConfig,
    KiroCrewConfig,
    MessagingConfig,
)
from kiro_crew.slack.events import SeenCache, _dispatch_queued, _route_message


def _make_orch(
    channels: dict[str, ChannelConfig] | None = None,
    dm_activation: str = ACTIVATION_ALWAYS,
) -> MagicMock:
    """Build a minimal mock GatewayOrchestrator with channel config."""
    orch = MagicMock()
    cfg = KiroCrewConfig(
        slack_channels=channels or {},
        slack_dm_activation=dm_activation,
        messaging=MessagingConfig(use_transport=False),
    )
    orch._cfg = cfg
    orch.channel_history = MagicMock()
    orch.slack = MagicMock()
    orch.sessions = AsyncMock()
    orch.sessions.enqueue = MagicMock(return_value=False)
    # Sync accessor on an AsyncMock: left unset it returns a truthy coroutine,
    # which would route the message down the mid-turn steer path and return
    # before the handler processes it.
    orch.sessions.is_busy = MagicMock(return_value=False)
    orch.sessions.is_cancelled = MagicMock(return_value=False)
    orch.sessions.dequeue = MagicMock(return_value=None)
    orch.sessions.clear_queue = MagicMock()
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


class TestChannelActivationRouting:
    @pytest.mark.asyncio
    async def test_dm_always_by_default(self):
        """DM messages are processed by default (activation=always)."""
        orch = _make_orch()
        seen = SeenCache()
        event = {"user": "U1", "channel": "D1234", "text": "hello", "ts": "1.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                # handle_message is dispatched via asyncio.create_task, give it a tick
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                # Wait for the task to complete
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_channel_mention_mode_ignores_plain_message(self):
        """Group channel with mention mode ignores non-mention messages."""
        orch = _make_orch()
        seen = SeenCache()
        event = {"user": "U1", "channel": "C1234", "text": "hello", "ts": "2.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_group_channel_mention_mode_processes_mention(self):
        """Group channel with mention mode processes @mention events."""
        orch = _make_orch()
        seen = SeenCache()
        event = {
            "user": "U1",
            "channel": "C1234",
            "text": "<@UBOT> what is this?",
            "ts": "3.0",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=True)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                # Text should have @mention stripped
                call_args = mock_hm.call_args
                assert call_args[1].get("channel_agent") is None
                # The text arg (positional arg 3) should be cleaned
                text_arg = call_args[0][3]
                assert "<@UBOT>" not in text_arg
                assert "what is this?" in text_arg

    @pytest.mark.asyncio
    async def test_group_channel_mention_mode_allows_thread_replies_with_session(self):
        """In mention mode, thread replies are processed if the bot has an active session."""
        orch = _make_orch()
        # Simulate an existing session for this thread (bot was previously @mentioned)
        orch.sessions = MagicMock()
        # A bare MagicMock returns a truthy Mock for every accessor, so an
        # unconfigured is_busy would route this message down the mid-turn
        # steer path and the handler would return before processing it.
        orch.sessions.is_busy.return_value = False
        orch.sessions.has_session = MagicMock(return_value=True)
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        seen = SeenCache()
        event = {
            "user": "U1",
            "channel": "C1234",
            "text": "follow up question",
            "ts": "3.5",
            "team": "TTEST",
            "thread_ts": "3.0",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()
                orch.sessions.has_session.assert_called_with("3.0")

    @pytest.mark.asyncio
    async def test_group_channel_mention_mode_ignores_thread_without_session(self):
        """In mention mode, thread replies are ignored if the bot has no session for that thread."""
        orch = _make_orch()
        orch.sessions = MagicMock()
        # A bare MagicMock returns a truthy Mock for every accessor, so an
        # unconfigured is_busy would route this message down the mid-turn
        # steer path and the handler would return before processing it.
        orch.sessions.is_busy.return_value = False
        orch.sessions.has_session = MagicMock(return_value=False)
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        seen = SeenCache()
        event = {
            "user": "U1",
            "channel": "C1234",
            "text": "follow up in random thread",
            "ts": "3.6",
            "team": "TTEST",
            "thread_ts": "3.0",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            await _route_message(orch, event, seen, is_mention=False)
            # Drain any tasks _route_message may have scheduled — relying on
            # `asyncio.sleep(0)` is too tight on the build farm where worker
            # contention occasionally delays the spawned coroutine past the
            # single tick. Awaiting actual task completion makes the assertion
            # deterministic.
            for _ in range(5):
                await asyncio.sleep(0)
            tasks = list(getattr(orch, "_handler_tasks", []))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_off_mode_ignores_all(self):
        """Channel with activation=off ignores all messages."""
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_OFF)})
        seen = SeenCache()
        event = {"user": "U1", "channel": "C1234", "text": "hello", "ts": "4.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            await _route_message(orch, event, seen, is_mention=True)
            await asyncio.sleep(0)
            mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_off_does_not_record_history(self):
        """Channel with activation=off does not record channel history."""
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_OFF)})
        seen = SeenCache()
        event = {"user": "U1", "channel": "C1234", "text": "hello", "ts": "5.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.sel") as mock_sel:
            mock_sel.return_value.log_api_access = MagicMock()
            await _route_message(orch, event, seen, is_mention=False)
            orch.channel_history.push.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_always_mode_processes_plain_message(self):
        """Channel with activation=always processes plain messages."""
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        seen = SeenCache()
        event = {"user": "U1", "channel": "C1234", "text": "hello", "ts": "6.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                assert len(tasks) == 1
                await asyncio.gather(*tasks, return_exceptions=True)
                mock_hm.assert_called_once()

    @pytest.mark.asyncio
    async def test_per_channel_agent_override(self):
        """Per-channel agent override is passed to handle_message."""
        orch = _make_orch(
            channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS, agent="ops")}
        )
        seen = SeenCache()
        event = {
            "user": "U1",
            "channel": "C1234",
            "text": "check status",
            "ts": "7.0",
            "team": "TTEST",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm:
            with patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
                await _route_message(orch, event, seen, is_mention=False)
                await asyncio.sleep(0)
                tasks = list(orch._handler_tasks)
                await asyncio.gather(*tasks, return_exceptions=True)
                call_kwargs = mock_hm.call_args[1]
                assert call_kwargs["channel_agent"] == "ops"


class TestTransportGateReviewMode:
    """The transport gate must EXCLUDE review-mode channels: review mode's
    privacy machinery (suppress public output + ephemeral approve/edit/cancel
    draft) lives only in native handle_message, so review-mode channels must
    route to native even when messaging.use_transport is True."""

    @pytest.mark.asyncio
    async def test_review_mode_routes_to_native_even_with_transport_on(self):
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_REVIEW)})
        orch._cfg.messaging = MessagingConfig(use_transport=True)  # transport ON
        seen = SeenCache()
        # Review mode requires a mention to be processed at all.
        event = {"user": "U1", "channel": "C1234", "text": "hi", "ts": "9.0", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock) as mock_tr, \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, seen, is_mention=True)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
            # Native owns review mode; transport path is skipped.
            mock_hm.assert_called_once()
            mock_tr.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_review_channel_uses_transport_when_on(self):
        # Contrast: a normal channel with transport ON DOES take the transport
        # path — the guard is narrow to review mode only.
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        orch._cfg.messaging = MessagingConfig(use_transport=True)
        seen = SeenCache()
        event = {"user": "U1", "channel": "C1234", "text": "hi", "ts": "9.1", "team": "TTEST"}

        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock) as mock_tr, \
             patch("kiro_crew.slack.events.is_allowed_user", return_value=True):
            await _route_message(orch, event, seen, is_mention=False)
            await asyncio.sleep(0)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
            mock_tr.assert_called_once()
            mock_hm.assert_not_called()


class TestHandlerChannelAgent:
    @pytest.mark.asyncio
    async def test_channel_agent_passed_to_session(self):
        """channel_agent parameter is used for session agent selection."""
        from conftest import MockSlackClient
        from kiro_crew.slack.handler import handle_message

        class FakeProvider:
            async def stream(self, message, timeout=120.0):
                yield MagicMock(kind="text_chunk", text="ok")
                yield MagicMock(kind="complete")

            def context_usage_pct(self):
                return 0.0

        class FakeSessionManager:
            def __init__(self):
                self.last_agent: str | None = None

            async def get_or_create(self, key, agent=None, channel_id=None):
                self.last_agent = agent
                return FakeProvider(), True, False

            def check_context_usage(self, key, provider):
                pass

            def record_success(self, key):
                pass

            async def record_failure(self, key):
                return False

            def release(self, key):
                pass

            def get_pid(self, key):
                return None

            async def set_channel(self, key, channel_id):
                pass

            def get_session_for_thread(self, thread_ts):
                return None

            def set_slack_link(self, key, thread_ts, channel_id):
                pass

            def enqueue(self, key, msg_ts, text, **kwargs):
                return False

            def is_cancelled(self, key, msg_ts):
                return False

            def dequeue(self, key):
                return None

            def clear_queue(self, key):
                pass

        slack = MockSlackClient()
        sessions = FakeSessionManager()
        await handle_message(
            slack, sessions, "C1", "hello", None, "msg1", "U1", channel_agent="ops"
        )
        assert sessions.last_agent == "ops"


class TestRouteMessageStop:
    """Integration tests for !stop interception in _route_message."""

    @pytest.mark.asyncio
    async def test_stop_cancels_active_task_and_cleans_up(self):
        """!stop in _route_message cancels the asyncio task and cleans _session_tasks."""
        orch = _make_orch()
        orch.sessions = AsyncMock()
        orch.sessions.has_session.return_value = True
        orch.sessions.stop_turn = AsyncMock(return_value="soft")
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch.sessions.clear_queue = MagicMock()
        orch.slack.post_message = AsyncMock()
        orch.slack.post_ephemeral = AsyncMock()
        seen = SeenCache()

        # Create a long-running task to simulate active execution
        active_task = asyncio.ensure_future(asyncio.sleep(999))
        orch._session_tasks["thread1"] = active_task

        event = {
            "user": "U1",
            "channel": "D1234",
            "text": "!stop",
            "ts": "1.0",
            "thread_ts": "thread1",
            "team": "TTEST",
        }

        with patch("kiro_crew.slack.events.is_owner", return_value=True), patch(
            "kiro_crew.slack.events.is_allowed_user", return_value=True
        ), patch("kiro_crew.slack.enterprise.check_message_origin", return_value=True):
            await _route_message(orch, event, seen, is_mention=False)

        # Let cancellation propagate — await the task so CancelledError
        # is raised and the task transitions to the cancelled state.
        try:
            await active_task
        except asyncio.CancelledError:
            pass
        # Task should be cancelled
        assert active_task.cancelled()
        # _session_tasks should be cleaned up (popped during !stop)
        assert "thread1" not in orch._session_tasks
        # stop_turn called (replaces direct reset)
        orch.sessions.stop_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_bypasses_semaphore(self):
        """!stop is handled before handle_message — never enters the semaphore path."""
        orch = _make_orch()
        orch.sessions = AsyncMock()
        orch.sessions.has_session.return_value = True
        orch.sessions.stop_turn = AsyncMock(return_value="soft")
        orch.sessions.enqueue = MagicMock(return_value=False)
        orch.sessions.dequeue = MagicMock(return_value=None)
        orch.sessions.clear_queue = MagicMock()
        orch.slack.post_message = AsyncMock()
        orch.slack.post_ephemeral = AsyncMock()
        seen = SeenCache()

        event = {
            "user": "U1",
            "channel": "D1234",
            "text": "!stop",
            "ts": "1.0",
            "thread_ts": "thread1",
            "team": "TTEST",
        }

        with patch(
            "kiro_crew.slack.events.handle_message", new_callable=AsyncMock
        ) as mock_hm, patch("kiro_crew.slack.events.is_owner", return_value=True), patch(
            "kiro_crew.slack.events.is_allowed_user", return_value=True
        ), patch(
            "kiro_crew.slack.enterprise.check_message_origin", return_value=True
        ):
            await _route_message(orch, event, seen, is_mention=False)
            await asyncio.sleep(0)
            # handle_message should never be called — !stop is intercepted before it
            mock_hm.assert_not_called()


class TestDispatchQueuedRouting:
    """Queued follow-up messages must drain through the SAME gate as the
    initial message: a transport thread keeps using transport for its queued
    follow-ups (parity), while review-mode / opt-out channels stay on native."""

    def _kwargs(self):
        return {"channel": "C1234", "thread_ts": "9.0", "sender_id": "U1"}

    @pytest.mark.asyncio
    async def test_queued_drains_to_transport_when_on(self):
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        orch._cfg.messaging = MessagingConfig(use_transport=True)
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock) as mock_tr:
            await _dispatch_queued(orch, "9.0", "9.0", "follow up", self._kwargs())
            mock_tr.assert_awaited_once()
            mock_hm.assert_not_called()

    @pytest.mark.asyncio
    async def test_queued_review_channel_drains_to_native(self):
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_REVIEW)})
        orch._cfg.messaging = MessagingConfig(use_transport=True)
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock) as mock_tr:
            await _dispatch_queued(orch, "9.0", "9.0", "follow up", self._kwargs())
            mock_hm.assert_awaited_once()
            mock_tr.assert_not_called()

    @pytest.mark.asyncio
    async def test_queued_drains_to_native_when_transport_off(self):
        orch = _make_orch(channels={"C1234": ChannelConfig(activation=ACTIVATION_ALWAYS)})
        orch._cfg.messaging = MessagingConfig(use_transport=False)
        with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as mock_hm, \
             patch("kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock) as mock_tr:
            await _dispatch_queued(orch, "9.0", "9.0", "follow up", self._kwargs())
            mock_hm.assert_awaited_once()
            mock_tr.assert_not_called()
