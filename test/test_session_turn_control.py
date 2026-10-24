"""SessionManager turns on a held session and the per-key reads beside them.

Among them: ``stop_turn`` and the eager respawn, ``cancel_owned_turn``, the message queue, the
success/failure record behind the circuit breaker, ``release``, the provider, PID and Slack-link
helpers, and the history replay a slot owes its first prompt after a load falls back to a fresh
session.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until


class TestStopTurn:
    """Tests for stop_turn(), _eager_respawn(), and cancel_current backcompat."""

    @pytest.mark.asyncio
    async def test_stop_turn_idle_no_session(self, cfg):
        """No session for key → returns 'idle'."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = await mgr.stop_turn("nonexistent")
        assert result == "idle"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_soft_ack(self, cfg):
        """Provider returns 'acked' → stop_turn returns 'soft', on_soft called."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="acked")
        on_soft = AsyncMock()
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_soft=on_soft, on_hard=on_hard)

        assert result == "soft"
        on_soft.assert_awaited_once()
        on_hard.assert_not_awaited()
        # Session should still exist (not reset)
        assert mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_hard_on_timeout(self, cfg):
        """Provider returns 'timeout' → stop_turn returns 'hard', on_hard called."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="timeout")
        on_soft = AsyncMock()
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_soft=on_soft, on_hard=on_hard)

        assert result == "hard"
        on_soft.assert_not_awaited()
        on_hard.assert_awaited_once()
        # Session should have been reset
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_hard_on_error(self, cfg):
        """Provider returns 'error' → stop_turn returns 'hard'."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="error")
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", on_hard=on_hard)

        assert result == "hard"
        on_hard.assert_awaited_once()
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_force_skips_cancel(self, cfg):
        """force=True goes straight to reset without calling provider.cancel."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="acked")
        on_hard = AsyncMock()

        result = await mgr.stop_turn("key1", force=True, on_hard=on_hard)

        assert result == "hard"
        provider.cancel.assert_not_awaited()
        on_hard.assert_awaited_once()
        assert not mgr.has_session("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_clears_queue_first(self, cfg):
        """stop_turn clears the message queue regardless of outcome."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        # Populate queue
        mgr.enqueue("key1", "ts1", "msg1", force=True)
        mgr.enqueue("key1", "ts2", "msg2", force=True)

        provider.cancel = AsyncMock(return_value="acked")
        await mgr.stop_turn("key1")

        # Queue should be empty
        assert mgr.dequeue("key1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_stop_turn_idle_still_clears_queue(self, cfg):
        """Even when provider returns 'no_turn', queue is cleared."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        mgr.enqueue("key1", "ts1", "msg1", force=True)

        provider.cancel = AsyncMock(return_value="no_turn")
        result = await mgr.stop_turn("key1")

        assert result == "idle"
        assert mgr.dequeue("key1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_called(self, cfg):
        """Hard path schedules _eager_respawn via asyncio.create_task."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="timeout")

        with patch.object(mgr, "_eager_respawn", new_callable=AsyncMock) as mock_respawn:
            await mgr.stop_turn("key1")
            # Allow the created task to run
            # flake-ok: one yield runs the respawn task stop_turn creates; the patched AsyncMock finishes in that step so FIFO order makes the check exact; moved verbatim
            await asyncio.sleep(0)
            mock_respawn.assert_awaited_once_with("key1")

        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_failure_logged(self, cfg, caplog):
        """_eager_respawn swallows exceptions and logs at debug."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with patch.object(
            mgr, "get_or_create", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ):
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.session"):
                await mgr._eager_respawn("key1")

        assert "Eager respawn failed" in caplog.text
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_eager_respawn_releases_semaphore(self, cfg):
        """_eager_respawn must release the semaphore acquired by get_or_create,
        else the next user message deadlocks waiting on it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Prime the session so get_or_create takes the fast path.
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")
        sess = mgr._sessions["key1"]
        # Check: semaphore is full (1 permit available) before respawn.
        assert sess.semaphore.locked() is False

        await mgr._eager_respawn("key1")

        # After respawn the semaphore MUST be released, otherwise the next
        # caller of get_or_create would hang on sess.semaphore.acquire().
        assert sess.semaphore.locked() is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_current_backcompat_default(self, cfg):
        """Existing cancel_current(key) call with no kwargs still works."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        mgr.release("key1")

        provider.cancel = AsyncMock(return_value="no_turn")
        result = await mgr.cancel_current("key1")

        assert result == "no_turn"
        provider.cancel.assert_awaited_once_with(wait_ack_timeout=0.0)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_owned_turn_refuses_a_different_lease_owner(self, cfg):
        """A deadline must not cancel another task sharing the folded key."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        provider.cancel = AsyncMock(return_value="acked")

        result = await mgr.cancel_owned_turn(
            "key1",
            object(),
            wait_ack_timeout=1.0,
        )

        assert result is None
        provider.cancel.assert_not_awaited()
        mgr.release("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_owned_turn_skips_a_natively_completed_owner(self, cfg):
        """Python teardown after native completion must not arm false replay."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        provider.has_unfinished_turn = MagicMock(return_value=False)
        provider.cancel = AsyncMock(return_value="no_turn")
        owner = asyncio.current_task()
        assert owner is not None

        result = await mgr.cancel_owned_turn(
            "key1",
            owner,
            wait_ack_timeout=1.0,
        )

        assert result is None
        assert mgr._sessions["key1"].prev_turn_cancelled is False
        provider.cancel.assert_not_awaited()
        mgr.release("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_owned_turn_keeps_unfinished_no_turn_fail_closed(self, cfg):
        """An inactive but unfinished native turn is not proven stopped."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")
        provider.has_unfinished_turn = MagicMock(return_value=True)
        provider.cancel = AsyncMock(return_value="no_turn")
        owner = asyncio.current_task()
        assert owner is not None

        result = await mgr.cancel_owned_turn(
            "key1",
            owner,
            wait_ack_timeout=1.0,
        )

        assert result == "no_turn"
        assert mgr._sessions["key1"].prev_turn_cancelled is True
        provider.cancel.assert_awaited_once_with(wait_ack_timeout=1.0)
        mgr.release("key1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_owned_turn_marks_acknowledged_turn_for_reinjection(self, cfg):
        """Native ACP drops cancelled turns, so the next prompt must replay it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("key1")

        async def _cancel(*, wait_ack_timeout):
            assert wait_ack_timeout == 1.0
            assert mgr._sessions["key1"].prev_turn_cancelled is True
            return "acked"

        provider.cancel = AsyncMock(side_effect=_cancel)
        owner = asyncio.current_task()
        assert owner is not None

        result = await mgr.cancel_owned_turn(
            "key1",
            owner,
            wait_ack_timeout=1.0,
        )

        assert result == "acked"
        assert mgr._sessions["key1"].prev_turn_cancelled is True
        provider.cancel.assert_awaited_once_with(wait_ack_timeout=1.0)
        mgr.release("key1")
        await mgr.close_all()


class TestRecordSuccessFailure:
    """Tests for record_success and record_failure circuit breaker."""

    @pytest.mark.asyncio
    async def test_get_provider_returns_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr.get_provider("k1") is provider
        await mgr.close_all()

    def test_get_provider_missing_returns_none(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_provider("nonexistent") is None

    @pytest.mark.asyncio
    async def test_pool_size_clamping(self, cfg, caplog):
        cfg.session.pool_size = 999
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert any("exceeds max" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_success_resets_counter(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].consecutive_failures = 3
        mgr.record_success("k1")
        assert mgr._sessions["k1"].consecutive_failures == 0
        await mgr.close_all()

    def test_record_success_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.record_success("nonexistent") is None
        assert not mgr.has_session("nonexistent"), "no session is created for the key"

    @pytest.mark.asyncio
    async def test_record_failure_increments(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        tripped = await mgr.record_failure("k1")
        assert tripped is False
        assert mgr._sessions["k1"].consecutive_failures == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_failure_trips_circuit_breaker(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].consecutive_failures = 4  # one below threshold
        tripped = await mgr.record_failure("k1")
        assert tripped is True
        assert not mgr.has_session("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_record_failure_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        tripped = await mgr.record_failure("nonexistent")
        assert tripped is False


class TestMessageQueue:
    """Tests for enqueue, dequeue, cancel_queued, is_cancelled."""

    @pytest.mark.asyncio
    async def test_enqueue_when_busy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # semaphore is locked (acquired by get_or_create)
        queued = mgr.enqueue("k1", "ts1", "hello")
        assert queued is True
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_enqueue_when_idle_returns_false(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        queued = mgr.enqueue("k1", "ts1", "hello")
        assert queued is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_enqueue_force(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        queued = mgr.enqueue("k1", "ts1", "hello", force=True)
        assert queued is True
        await mgr.close_all()

    def test_enqueue_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.enqueue("nope", "ts1", "hi") is False

    @pytest.mark.asyncio
    async def test_dequeue_fifo(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "first")
        mgr.enqueue("k1", "ts2", "second")
        mgr.release("k1")
        result = mgr.dequeue("k1")
        assert result == ("ts1", "first", {})
        result = mgr.dequeue("k1")
        assert result == ("ts2", "second", {})
        assert mgr.dequeue("k1") is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dequeue_skips_cancelled(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "first")
        mgr.enqueue("k1", "ts2", "second")
        mgr._sessions["k1"].cancelled.add("ts1")
        mgr.release("k1")
        result = mgr.dequeue("k1")
        assert result == ("ts2", "second", {})
        await mgr.close_all()

    def test_dequeue_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.dequeue("nope") is None

    @pytest.mark.asyncio
    async def test_cancel_queued_removes_from_queue(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts1", "msg1")
        mgr.enqueue("k1", "ts2", "msg2")
        mgr.release("k1")
        removed = mgr.cancel_queued("k1", "ts1")
        assert removed is True
        result = mgr.dequeue("k1")
        assert result[0] == "ts2"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_queued_marks_inflight(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # semaphore locked = something in-flight
        removed = mgr.cancel_queued("k1", "ts_inflight")
        assert removed is False
        assert "ts_inflight" in mgr._sessions["k1"].cancelled
        mgr.release("k1")
        await mgr.close_all()

    def test_cancel_queued_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.cancel_queued("nope", "ts1") is False

    @pytest.mark.asyncio
    async def test_is_cancelled_consumes(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._sessions["k1"].cancelled.add("ts1")
        assert mgr.is_cancelled("k1", "ts1") is True
        # Second call returns False (consumed)
        assert mgr.is_cancelled("k1", "ts1") is False
        await mgr.close_all()

    def test_is_cancelled_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.is_cancelled("nope", "ts1") is False


class TestRelease:
    """Tests for release() with subagent cleanup."""

    @pytest.mark.asyncio
    async def test_release_normal_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        # Semaphore should be locked after get_or_create
        assert mgr._sessions["k1"].semaphore.locked()
        mgr.release("k1")
        assert not mgr._sessions["k1"].semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_release_subagent_with_cleanup(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("subagent:abc")
        provider.session_id = "sid-123"
        provider.cleanup_session = AsyncMock()
        mgr.release("subagent:abc", cleanup=True)
        # Allow the ensure_future to run
        # flake-ok: one yield runs the cleanup future release schedules; its AsyncMock cleanup finishes without suspending so one step is exact; moved verbatim
        await asyncio.sleep(0)
        provider.cleanup_session.assert_awaited_once_with("sid-123")
        await mgr.close_all()

    def test_release_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.release("nonexistent") is None
        assert not mgr.has_session("nonexistent"), "no session is created for the key"

    @pytest.mark.asyncio
    async def test_stray_release_after_reset_does_not_over_permit_the_replacement(
        self, cfg, caplog
    ):
        """A failure-handling caller that still holds session A's semaphore may
        call ``reset(key)`` (as ``record_failure`` does) before its own
        ``finally`` reaches ``release(key)``. ``reset`` pops the session object
        WITHOUT releasing its semaphore, and a concurrent ``get_or_create`` for
        the same key can register a brand-new session in the meantime, with its
        own fresh semaphore. By the time the original caller's late
        ``release(key)`` runs, that new session may already have finished ITS
        own turn and released its own semaphore normally -- so the stray
        release lands on an already-full semaphore. A plain ``Semaphore`` would
        silently accept it, permanently minting a second standing permit and
        letting two turns run concurrently on the session forever after. The
        bounded semaphore must instead reject it (logged, not raised into the
        caller), leaving exactly one permit.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("A")  # caller 1: holds session-1's semaphore

        await mgr.reset("A")  # e.g. record_failure's circuit-breaker path
        assert "A" not in mgr._sessions  # session-1 discarded; semaphore never released

        await mgr.get_or_create(
            "A"
        )  # a concurrent caller 2: registers session-2, holds ITS semaphore
        session_2 = mgr._sessions["A"]
        assert session_2.semaphore.locked()
        mgr.release("A")  # caller 2's OWN legitimate finally, already run
        assert not session_2.semaphore.locked()

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.release("A")  # caller 1's finally, arriving late on a stale key lookup

        assert "session was replaced" in caplog.text
        # A single extra permit must not have been minted: only one acquire can
        # succeed at a time, not two run concurrently.
        first = asyncio.ensure_future(session_2.semaphore.acquire())
        # flake-ok: one yield runs the first acquire task; a free permit returns without suspending so it is done after exactly that step; moved verbatim
        await asyncio.sleep(0)
        assert first.done()
        second = asyncio.ensure_future(session_2.semaphore.acquire())
        # flake-ok: one yield runs the second acquire task; a surplus permit would finish it in that step so the not-done check is exact; moved verbatim
        await asyncio.sleep(0)
        assert not second.done()  # blocked -- no surplus permit to grant it
        second.cancel()
        session_2.semaphore.release()
        await mgr.close_all()


class TestSlackLinkHelpers:
    """Tests for set/get slack_link, thread, channel helpers."""

    @pytest.mark.asyncio
    async def test_set_and_get_slack_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", "C001")
        assert mgr.get_slack_link("k1") == ("ts123", "C001")

    @pytest.mark.asyncio
    async def test_get_session_for_thread(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", "C001")
        assert mgr.get_session_for_thread("ts123") == "k1"
        assert mgr.get_session_for_thread("unknown") is None

    @pytest.mark.asyncio
    async def test_set_channel_compat(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "ts123", None)
        await mgr.set_channel("k1", "C002")
        assert mgr.get_channel("k1") == "C002"

    @pytest.mark.asyncio
    async def test_set_thread_compat(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_slack_link("k1", "", "C001")
        await mgr.set_thread("k1", "ts456")
        assert mgr.get_thread("k1") == "ts456"

    def test_get_channel_no_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_channel("nonexistent") is None

    def test_get_thread_no_link(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_thread("nonexistent") is None

    def test_find_key_by_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.set("k1", "sid-abc")
        assert mgr.find_key_by_sid("sid-abc") == "k1"
        assert mgr.find_key_by_sid("unknown") is None


class TestGetPid:
    """Tests for get_pid."""

    @pytest.mark.asyncio
    async def test_get_pid_returns_pid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.client = AsyncMock()
        provider.client._pid = 12345
        assert mgr.get_pid("k1") == 12345
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_pid_no_client(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Remove client attr
        del provider.client
        assert mgr.get_pid("k1") is None
        await mgr.close_all()

    def test_get_pid_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_pid("nonexistent") is None


class TestLoadRecoveryHistoryReplay:
    """F2 load-recovery Phase 2: when a provider signals it fell back to a FRESH
    native session (the prior session's lock never cleared), get_or_create flags
    the new slot for Kiro Crew conversation_log replay on the first prompt so the
    slot is not context-free."""

    @staticmethod
    def _factory(history_replay_needed: bool):
        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m._history_replay_needed = history_replay_needed
            return m

        return factory

    @pytest.mark.asyncio
    async def test_fresh_fallback_triggers_history_replay(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._factory(True))
        _provider, is_new, _resumed = await mgr.get_or_create("thread1")
        assert is_new is True
        sess = next(iter(mgr._sessions.values()))
        assert sess.provider_switch_replay is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_native_resume_does_not_trigger_replay(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._factory(False))
        await mgr.get_or_create("thread1")
        sess = next(iter(mgr._sessions.values()))
        assert sess.provider_switch_replay is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_replay_marker_survives_reads_until_prompt_acknowledges_it(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._factory(True))
        await mgr.get_or_create("thread1")

        assert mgr.provider_switch_replay_pending("thread1") is True
        assert mgr.provider_switch_replay_pending("thread1") is True
        assert mgr.consume_provider_switch_replay("thread1") is True
        assert mgr.provider_switch_replay_pending("thread1") is False
        assert mgr.consume_provider_switch_replay("thread1") is False
        assert mgr.mark_provider_switch_replay("thread1") is True
        assert mgr.provider_switch_replay_pending("thread1") is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_non_acp_provider_switch_replay_settles_without_sid_promotion(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        session = next(iter(mgr._sessions.values()))
        session.provider_switch_replay = True

        assert mgr.commit_provider_switch_replay_sid("thread1") is True
        assert session.provider_switch_replay is False
        assert mgr.provider_switch_replay_pending("thread1") is False

        mgr.release("thread1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_pending_replay_preserves_prior_sid_until_commit(
        self, cfg, monkeypatch, tmp_path
    ):
        from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
        from kiro_crew.providers.acp import AcpProvider

        shutdown_started = asyncio.Event()
        release_shutdown = asyncio.Event()

        async def concrete_shutdown():
            shutdown_started.set()
            # flake-ok: shutdown double parks until the test sets release_shutdown; close_all caps shutdown at 5 s and the set event frees the later close; moved verbatim
            await release_shutdown.wait()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = object.__new__(AcpProvider)
            provider._client = MagicMock()
            provider._client._session_id = "fresh-replayed-sid"
            provider._client._work_dir = "/new-workspace"
            provider._client._pid = None
            provider._client.backend = ACP_BACKEND_KIRO
            provider._client.resumed = False
            provider._client.set_resume_session_id = MagicMock()
            provider._history_replay_needed = True
            provider._defer_replay_sid_promotion = True
            provider.start = AsyncMock()
            provider.shutdown = AsyncMock(side_effect=concrete_shutdown)
            provider.context_usage_pct = MagicMock(return_value=0.0)
            return provider

        native_sessions = tmp_path / "native-sessions"
        native_sessions.mkdir()
        (native_sessions / "old-full-history-sid.json").write_text("{}", encoding="utf-8")
        (native_sessions / "old-full-history-sid.jsonl").write_text(
            '{"role":"user","content":"prior history"}\n',
            encoding="utf-8",
        )
        (native_sessions / "fresh-replayed-sid.json").write_text("{}", encoding="utf-8")
        (native_sessions / "fresh-replayed-sid.jsonl").write_text(
            '{"role":"user","content":"replayed history"}\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "kiro_crew.session_map._kiro_sessions_dir",
            lambda: native_sessions,
        )

        mgr = SessionManager(cfg, provider_factory=factory)
        mgr._session_map.set(
            "thread1",
            "old-full-history-sid",
            provider=PROVIDER_LABEL_DEFAULT,
            cwd="/old-workspace",
        )

        await mgr.get_or_create("thread1")

        assert mgr._session_map.get("thread1") == "old-full-history-sid"
        assert mgr.provider_switch_replay_pending("thread1") is True

        # close_all flushes SessionMap before provider shutdown. Hold it at that
        # boundary and create a new manager, exactly as an update restart can.
        close_task = asyncio.create_task(mgr.close_all())
        await await_until(shutdown_started, what="close_all at the flush boundary")
        resumed_mgr = SessionManager(cfg, provider_factory=factory)
        assert resumed_mgr._session_map.get("thread1") == "old-full-history-sid"
        release_shutdown.set()
        # flake-ok: awaits close_all once release_shutdown is set; close_all caps provider shutdown with its own 5 s wait_for so this stays bounded; moved verbatim
        await close_task

        await resumed_mgr.get_or_create("thread1")
        assert resumed_mgr.provider_switch_replay_pending("thread1") is True
        assert resumed_mgr._session_map.get("thread1") == "old-full-history-sid"
        assert resumed_mgr.commit_provider_switch_replay_sid("thread1") is True
        assert resumed_mgr.provider_switch_replay_pending("thread1") is False
        assert resumed_mgr._session_map.get("thread1") == "fresh-replayed-sid"
        await resumed_mgr.close_all()

    @pytest.mark.asyncio
    async def test_generic_load_recovery_promotes_fresh_sid_immediately(
        self, cfg, monkeypatch, tmp_path
    ):
        from kiro_crew.acp.types import PROVIDER_LABEL_DEFAULT
        from kiro_crew.providers.acp import AcpProvider

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            provider = object.__new__(AcpProvider)
            provider._client = MagicMock()
            provider._client._session_id = "fresh-recovery-sid"
            provider._client._work_dir = "/new-workspace"
            provider._client._pid = None
            provider._client.backend = ACP_BACKEND_KIRO
            provider._client.resumed = False
            provider._client.set_resume_session_id = MagicMock()
            provider._history_replay_needed = True
            provider._defer_replay_sid_promotion = False
            provider.start = AsyncMock()
            provider.shutdown = AsyncMock()
            provider.context_usage_pct = MagicMock(return_value=0.0)
            return provider

        native_sessions = tmp_path / "native-sessions"
        native_sessions.mkdir()
        for sid, content in (
            ("old-full-history-sid", "prior history"),
            ("fresh-recovery-sid", "replayed history"),
        ):
            (native_sessions / f"{sid}.json").write_text("{}", encoding="utf-8")
            (native_sessions / f"{sid}.jsonl").write_text(
                f'{{"role":"user","content":"{content}"}}\n',
                encoding="utf-8",
            )
        monkeypatch.setattr(
            "kiro_crew.session_map._kiro_sessions_dir",
            lambda: native_sessions,
        )

        mgr = SessionManager(cfg, provider_factory=factory)
        mgr._session_map.set(
            "thread1",
            "old-full-history-sid",
            provider=PROVIDER_LABEL_DEFAULT,
            cwd="/old-workspace",
        )

        await mgr.get_or_create("thread1")

        assert mgr.provider_switch_replay_pending("thread1") is True
        assert mgr._session_map.get("thread1") == "fresh-recovery-sid"
        await mgr.close_all()

        reloaded_mgr = SessionManager(cfg, provider_factory=factory)
        assert reloaded_mgr._session_map.get("thread1") == "fresh-recovery-sid"
        await reloaded_mgr.close_all()
