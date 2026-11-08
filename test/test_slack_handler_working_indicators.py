"""Slack handler tests: a turn's working indicators come down on every exit.

The "…is working" thread status and the inline Stop block are cleared when a
turn ends normally, when it is cancelled in any phase, and are left alone once
another owner (a review draft, a compaction replay) holds the status.
"""

from __future__ import annotations

import asyncio

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager
from test_slack_handler_turn_outcomes import _abandoned, _SequencedProvider

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message


class TestWorkingIndicatorCleanup:
    """Working indicators cleared on every turn exit, including CancelledError."""

    class _CancellingProvider(FakeProvider):
        """Yields one text chunk then raises CancelledError to simulate mid-stream cancel."""

        async def stream(self, message, timeout=120.0):
            yield LLMEvent(kind="text_chunk", text="partial")
            raise asyncio.CancelledError()

    @pytest.mark.asyncio
    async def test_cancelled_turn_clears_thread_status(self):
        """A CancelledError mid-stream clears the native thread status."""
        slack = MockSlackClient()
        sessions = FakeSessionManager(self._CancellingProvider())
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")
        # Let the background cleanup task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert (
            status_clears
        ), f"Thread status was never cleared after CancelledError; actions={slack.actions}"

    @pytest.mark.asyncio
    async def test_cancelled_turn_deletes_working_block(self):
        """A CancelledError mid-stream deletes the inline Stop block."""
        slack = MockSlackClient()
        sessions = FakeSessionManager(self._CancellingProvider())
        # thread_ts is required for the Stop block to be posted.
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", "thread1", "msg1", "U1")
        # Let the background cleanup task run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # The Stop block is posted via post_blocks (action tag "blocks").
        block_posts = [a for a in slack.actions if a[0] == "blocks"]
        assert block_posts, "Expected at least one blocks post (the Stop block)"
        working_ts = block_posts[0][1]["ts"]
        deletes = [a for a in slack.actions if a[0] == "delete" and a[1]["ts"] == working_ts]
        assert deletes, (
            f"Working block {working_ts!r} was never deleted after CancelledError; "
            f"actions={slack.actions}"
        )

    @pytest.mark.asyncio
    async def test_cancelled_turn_propagates_cancelled_error(self):
        """CancelledError propagates out of handle_message (not swallowed)."""
        slack = MockSlackClient()
        sessions = FakeSessionManager(self._CancellingProvider())
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

    @pytest.mark.asyncio
    async def test_normal_turn_end_still_clears_thread_status(self):
        """Regression: a successful (non-cancelled) turn still clears the status."""
        slack = MockSlackClient()
        sessions = FakeSessionManager(FakeProvider())
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert status_clears, f"Thread status not cleared on normal exit; actions={slack.actions}"

    @pytest.mark.asyncio
    async def test_slack_error_during_cleanup_does_not_swallow_cancellation(self):
        """A Slack API failure inside cleanup does not suppress CancelledError."""

        class _ErroringSlack(MockSlackClient):
            async def set_thread_status(self, channel, thread_ts, status):
                if status == "":
                    raise RuntimeError("Slack API down")
                await super().set_thread_status(channel, thread_ts, status)

        slack = _ErroringSlack()
        sessions = FakeSessionManager(self._CancellingProvider())
        # CancelledError must still propagate even when the cleanup call raises.
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")
        # Drain the event loop so the background task that raised does not leak.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_cancel_in_delivery_clears_both_indicators(self):
        """A cancel in the delivery phase, before its own clear, still clears both."""

        class _CancelAtDelivery(FakeSessionManager):
            def is_cancelled(self, key, msg_ts):
                raise asyncio.CancelledError()

        slack = MockSlackClient()
        sessions = _CancelAtDelivery(FakeProvider())
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", "thread1", "msg1", "U1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert status_clears, slack.actions
        working_ts = [a for a in slack.actions if a[0] == "blocks"][0][1]["ts"]
        assert [a for a in slack.actions if a[0] == "delete" and a[1]["ts"] == working_ts]

    @pytest.mark.asyncio
    async def test_cancel_after_delivery_cleared_adds_no_second_clear(self):
        """Once delivery took the indicators down, a later cancel clears nothing more."""

        class _CancelAfterClear(MockSlackClient):
            async def update_message(self, *args, **kwargs):
                if any(a[0] == "set_thread_status" and a[1]["status"] == "" for a in self.actions):
                    raise asyncio.CancelledError()
                return await super().update_message(*args, **kwargs)

        slack = _CancelAfterClear()
        sessions = FakeSessionManager(FakeProvider())
        with pytest.raises(asyncio.CancelledError):
            await handle_message(slack, sessions, "C1", "hi", "thread1", "msg1", "U1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert len(status_clears) == 1, slack.actions

    @pytest.mark.asyncio
    async def test_review_cancel_before_draft_lands_clears_status(self, monkeypatch):
        """Review mode keeps its status for the draft; a cancel before it lands clears it."""
        import kiro_crew.slack.handler as handler_mod
        from kiro_crew.slack.handler import ACTIVATION_REVIEW

        async def _cancel_draft(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(handler_mod, "_post_review_draft", _cancel_draft)
        slack = MockSlackClient()
        sessions = FakeSessionManager(FakeProvider())
        with pytest.raises(asyncio.CancelledError):
            await handle_message(
                slack,
                sessions,
                "C1",
                "hi",
                "thread1",
                "msg1",
                "U1",
                channel_activation=ACTIVATION_REVIEW,
            )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert status_clears, slack.actions

    @pytest.mark.asyncio
    async def test_cancel_after_a_replay_leaves_the_replays_status(self, monkeypatch):
        """A cancel that unwinds the parent after a compaction replay does not clear
        the status the nested turn now owns (e.g. its "Awaiting review…")."""
        import kiro_crew.slack.handler as handler_mod

        real = handler_mod.handle_message

        async def _replay_then_cancel(*args, **kwargs):
            if kwargs.get("_compaction_replay") is not None:
                slack_, _sessions, channel, *_rest = args
                await slack_.set_thread_status(channel, "thread1", "Awaiting review…")
                raise asyncio.CancelledError()
            return await real(*args, **kwargs)

        monkeypatch.setattr(handler_mod, "handle_message", _replay_then_cancel)
        slack = MockSlackClient()
        sessions = FakeSessionManager(_SequencedProvider([_abandoned()], transient=True))
        with pytest.raises(asyncio.CancelledError):
            await real(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        statuses = [a[1]["status"] for a in slack.actions if a[0] == "set_thread_status"]
        assert statuses[-1] == "Awaiting review…", slack.actions

    @pytest.mark.asyncio
    async def test_a_replay_cancelled_before_its_status_clears_the_first_attempts(
        self, monkeypatch
    ):
        """A cancel that lands in the nested replay before it sets its own status
        leaves no "…is working" status from the abandoned first attempt."""
        import kiro_crew.slack.handler as handler_mod

        real = handler_mod.handle_message

        async def _cancel_replay_early(*args, **kwargs):
            if kwargs.get("_compaction_replay") is not None:
                raise asyncio.CancelledError()
            return await real(*args, **kwargs)

        monkeypatch.setattr(handler_mod, "handle_message", _cancel_replay_early)
        slack = MockSlackClient()
        sessions = FakeSessionManager(_SequencedProvider([_abandoned()], transient=True))
        with pytest.raises(asyncio.CancelledError):
            await real(slack, sessions, "C1", "hello", "thread1", "msg1", "U1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        statuses = [a[1]["status"] for a in slack.actions if a[0] == "set_thread_status"]
        assert statuses and statuses[-1] == "", slack.actions


class TestEarlyExitsClearBothIndicators:
    """Every exit before the stream opens takes down the status and the Stop block."""

    @staticmethod
    def _assert_both_cleared(slack: MockSlackClient) -> None:
        status_clears = [
            a for a in slack.actions if a[0] == "set_thread_status" and a[1]["status"] == ""
        ]
        assert status_clears, slack.actions
        working_ts = [a for a in slack.actions if a[0] == "blocks"][0][1]["ts"]
        assert [
            a for a in slack.actions if a[0] == "delete" and a[1]["ts"] == working_ts
        ], slack.actions

    @pytest.mark.asyncio
    async def test_cancelled_before_the_llm_call(self):
        class _Cancelled(FakeSessionManager):
            def is_cancelled(self, key, msg_ts):
                return True

        slack = MockSlackClient()
        await handle_message(slack, _Cancelled(FakeProvider()), "C1", "hi", "thread1", "msg1", "U1")
        self._assert_both_cleared(slack)

    @pytest.mark.asyncio
    async def test_gateway_shutting_down(self):
        from kiro_crew.session import SessionClosingError

        class _Closing(FakeSessionManager):
            def begin_turn(self, key):
                raise SessionClosingError("closing")

        slack = MockSlackClient()
        await handle_message(slack, _Closing(FakeProvider()), "C1", "hi", "thread1", "msg1", "U1")
        self._assert_both_cleared(slack)

    @pytest.mark.asyncio
    async def test_a_failed_status_clear_still_deletes_the_stop_block(self):
        class _Cancelled(FakeSessionManager):
            def is_cancelled(self, key, msg_ts):
                return True

        class _StatusDown(MockSlackClient):
            async def set_thread_status(self, channel, thread_ts, status):
                if status == "":
                    raise RuntimeError("Slack API down")
                await super().set_thread_status(channel, thread_ts, status)

        slack = _StatusDown()
        await handle_message(slack, _Cancelled(FakeProvider()), "C1", "hi", "thread1", "msg1", "U1")
        working_ts = [a for a in slack.actions if a[0] == "blocks"][0][1]["ts"]
        assert [
            a for a in slack.actions if a[0] == "delete" and a[1]["ts"] == working_ts
        ], slack.actions
