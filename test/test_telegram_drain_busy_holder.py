"""A Telegram drain that finds its session held by another turn.

The drain runs after a Telegram turn releases the session. If something else took the
lease in between -- a dashboard, cron or auto-nudge turn on the same key, an end-of-turn
compaction, or another channel's turn under a unified ``dm_scope`` -- replaying the
queued message only reaches ``_handle_busy``, which queues the same text again, and the
pump dequeued it straight back. Every lap flipped the receipt to "Now answering" without
a turn starting.

Covered here: the drain leaves the queue untouched and announces nothing while the
session is held; ``drain_when_free`` runs it again once the lease is free, however the
holder let go; the message is then answered once with one notice; and the retry is
deduplicated, bounded, and cannot run two drains of one channel on one queue.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher

from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging import queue_drain
from kiro_crew.messaging.queue_drain import drain_when_free, register_drain, reset_drains
from kiro_crew.session import SessionManager
from kiro_crew.telegram.transport import TelegramInboundMessage
from kiro_crew.telegram.transport_dispatch import _inbound_origin
from kiro_crew.testing.wait import async_wait_until

_KEY = "unified:kirocrew"
_SLOT = ("telegram", _KEY)


@pytest.fixture(autouse=True)
def _clean_registry():
    """A drain registration and a pending retry are process-wide; never inherit one."""
    reset_drains()
    yield
    reset_drains()


@pytest.fixture
def _fast_poll(monkeypatch):
    """Look at the lease on every loop turn, so a test waits on events, not seconds."""
    monkeypatch.setattr(queue_drain, "_HELD_POLL_FIRST_SECS", 0.0)
    monkeypatch.setattr(queue_drain, "_HELD_POLL_MAX_SECS", 0.0)


def _msg(text: str) -> TelegramInboundMessage:
    return TelegramInboundMessage(
        channel_type="telegram",
        user_id="7",
        conversation_id="70",
        text=text,
        message_id=11,
        chat_type="private",
    )


async def _queue_one(d: Any, sess: Any, text: str) -> None:
    """Queue *text* through the production mid-turn writer, as a busy turn would."""
    sess._busy = True
    assert await d._enqueue_with_receipt(
        _KEY, 70, text, origin=_inbound_origin(_msg(text))
    ), "the fake session must accept a mid-turn enqueue"


def _now_answering(cli: Any) -> list[str]:
    return [txt for _mid, txt, _ in cli.edits if "Now answering" in txt]


def _spy_replays(d: Any) -> list[Any]:
    replayed: list[Any] = []

    async def _spy(msg: Any, **kw: Any) -> None:
        replayed.append(msg)

    d.handle_message = _spy
    return replayed


class TestTheDrainLeavesAHeldSessionAlone:
    @pytest.mark.asyncio
    async def test_a_held_session_is_not_announced_and_keeps_its_queue(self) -> None:
        """The session is still held when the drain runs.

        Before the fix the pump flipped the receipt and replayed the message anyway --
        the first lap of the dequeue -> "Now answering" -> requeue loop.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        replayed = _spy_replays(d)
        # Still busy: another turn holds the lease the Telegram turn just released.
        await d._drain_queue(_KEY)

        assert replayed == [], "no turn can start in a held session"
        assert _now_answering(cli) == [], "so nothing may claim one has"
        assert [e[1] for e in sess.queued] == ["hello"], "the message stays queued"
        assert _SLOT in queue_drain._HELD_RETRIES, "and a drain waits for the lease"
        reset_drains()  # cancel that wait on this test's own loop

    @pytest.mark.asyncio
    async def test_the_real_replay_path_does_not_loop_on_a_held_session(self) -> None:
        """End to end through the production ``handle_message`` and ``_handle_busy``.

        Before the fix the pump dequeued the message and replayed it into the held
        session; in production that replay requeues it and the pump repeats. Here the
        first such dequeue already fails the test, and the lap cap and bounded wait
        keep a regressed loop from hanging it.
        """
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        laps = 0
        real_dequeue = sess.dequeue

        def _counted(key: str) -> Any:
            nonlocal laps
            item = real_dequeue(key)
            if item is not None:
                laps += 1
            if laps > 50:
                sess._busy = False  # let a regressed loop end so the test can fail
            return item

        sess.dequeue = _counted  # type: ignore[method-assign]
        await asyncio.wait_for(d._drain_queue(_KEY), timeout=30)

        assert laps == 0, "nothing is dequeued while the session is held"
        assert _now_answering(cli) == []
        assert [e[1] for e in sess.queued] == ["hello"]
        reset_drains()  # cancel the retry still waiting on this test's own loop

    @pytest.mark.asyncio
    async def test_once_free_the_message_is_answered_once_with_one_notice(self, _fast_poll) -> None:
        """The other half: leaving the queue alone must not strand the message. Nothing
        but the lease coming free starts the answer -- no new input, no second call."""
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        replayed = _spy_replays(d)
        await d._drain_queue(_KEY)  # held: deferred
        assert replayed == []

        sess._busy = False  # the holder let go
        await async_wait_until(lambda: replayed, timeout=10, describe=lambda: sess.queued)
        await async_wait_until(lambda: _SLOT not in queue_drain._HELD_RETRIES, timeout=10)

        assert [m.text for m in replayed] == ["hello"], "answered exactly once"
        assert len(_now_answering(cli)) == 1, "with exactly one notice"
        assert sess.queued == []

    @pytest.mark.asyncio
    async def test_a_free_session_drains_without_waiting(self) -> None:
        """The guard against fixing the loop by never draining: the ordinary path."""
        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        sess._busy = False
        replayed = _spy_replays(d)
        await d._drain_queue(_KEY)

        assert [m.text for m in replayed] == ["hello"]
        assert _SLOT not in queue_drain._HELD_RETRIES


class TestDrainWhenFree:
    @pytest.mark.asyncio
    async def test_a_raw_permit_hand_back_still_starts_the_drain(self, _fast_poll) -> None:
        """A compaction, recycle or stop gives the permit back with a bare
        ``semaphore.release()``, never reaching ``SessionManager.release``. The retry
        watches the lease itself, so it starts the drain anyway."""

        def _factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.cwd = ""
            m.disown_work_dir = MagicMock()
            m.memory_mode = "persistent"
            m.is_process_alive = lambda: True
            m.context_usage_pct = lambda: 10.0
            m.context_usage_unknown = lambda: False
            m.context_window_tokens = lambda: 0
            m.has_active_turn = lambda: False
            m.runtime_info = lambda: (None, None)
            return m

        drained: list[str] = []

        async def _drain(key: str) -> None:
            drained.append(key)

        register_drain("telegram", _drain)
        mgr = SessionManager(KiroCrewConfig(), provider_factory=_factory)
        try:
            await mgr.get_or_create(_KEY)  # the holder
            key = mgr._fold_key(_KEY)
            assert mgr.is_busy(key)
            assert drain_when_free(channel="telegram", session_key=key, is_busy=mgr.is_busy)
            for _ in range(5):
                await asyncio.sleep(0)
            assert drained == [], "not while the lease is held"

            mgr._sessions[key].semaphore.release()  # the compaction-style hand back
            await async_wait_until(lambda: drained, timeout=10)
            assert drained == [key]
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_one_retry_waits_per_channel_and_session(self) -> None:
        busy = True
        assert drain_when_free(channel="telegram", session_key=_KEY, is_busy=lambda _k: busy)
        assert not drain_when_free(channel="telegram", session_key=_KEY, is_busy=lambda _k: busy)
        assert drain_when_free(channel="discord", session_key=_KEY, is_busy=lambda _k: busy)
        assert drain_when_free(channel="telegram", session_key="other", is_busy=lambda _k: busy)
        reset_drains()  # cancel the three waits on this test's own loop

    @pytest.mark.asyncio
    async def test_a_drain_that_finds_the_lease_taken_again_can_wait_again(
        self, _fast_poll
    ) -> None:
        """The retry drops itself before it drains, so a drain that loses the race for
        the lease to a new holder schedules the next retry instead of being refused."""
        rescheduled: list[bool] = []

        async def _drain(key: str) -> None:
            rescheduled.append(
                drain_when_free(channel="telegram", session_key=key, is_busy=lambda _k: True)
            )

        register_drain("telegram", _drain)
        assert drain_when_free(channel="telegram", session_key=_KEY, is_busy=lambda _k: False)
        await async_wait_until(lambda: rescheduled, timeout=10)
        assert rescheduled == [True]
        assert _SLOT in queue_drain._HELD_RETRIES
        reset_drains()  # cancel the new wait on this test's own loop

    @pytest.mark.asyncio
    async def test_the_wait_is_bounded_and_leaves_the_queue(self, monkeypatch, caplog) -> None:
        monkeypatch.setattr(queue_drain, "_HELD_POLL_FIRST_SECS", 0.0)
        monkeypatch.setattr(queue_drain, "_HELD_POLL_MAX_SECS", 0.0)
        monkeypatch.setattr(queue_drain, "_HELD_WAIT_LIMIT_SECS", 0.0)
        drained: list[str] = []

        async def _drain(key: str) -> None:
            drained.append(key)

        register_drain("telegram", _drain)
        with caplog.at_level(logging.WARNING, logger=queue_drain.__name__):
            assert drain_when_free(channel="telegram", session_key=_KEY, is_busy=lambda _k: True)
            await async_wait_until(lambda: _SLOT not in queue_drain._HELD_RETRIES, timeout=10)

        assert drained == [], "a holding past the limit never starts the drain"
        assert "stayed held" in caplog.text

    @pytest.mark.asyncio
    async def test_a_channel_with_no_registered_drain_is_skipped(self, _fast_poll) -> None:
        assert drain_when_free(channel="telegram", session_key=_KEY, is_busy=lambda _k: False)
        await async_wait_until(lambda: _SLOT not in queue_drain._HELD_RETRIES, timeout=10)


class TestTheClientCloseRetiresTheDrain:
    @pytest.mark.asyncio
    async def test_a_retry_waiting_when_the_channel_closes_answers_nothing(
        self, _fast_poll
    ) -> None:
        """The channel is disabled while a retry waits on a held lease. When the lease
        frees, the retry must not reach the closed dispatcher and answer on it."""
        from kiro_crew.messaging.spawn_approval_delivery import register_channel_delivery
        from kiro_crew.telegram.gateway import _client_close_hook

        d, cli, sess = _dispatcher({7}, dm_scope="unified")
        await _queue_one(d, sess, "hello")
        replayed = _spy_replays(d)
        await d._drain_queue(_KEY)  # held: a retry now waits
        assert _SLOT in queue_drain._HELD_RETRIES

        register_channel_delivery("telegram", d.deliver_spawn_approval)
        _client_close_hook(d, d.deliver_spawn_approval)()  # the operator turns it off
        assert "telegram" not in queue_drain._DRAINS

        sess._busy = False  # the holder lets go
        await async_wait_until(lambda: _SLOT not in queue_drain._HELD_RETRIES, timeout=10)
        assert replayed == [], "nothing is answered on a closed channel"
        assert _now_answering(cli) == []
        assert [e[1] for e in sess.queued] == ["hello"], "the message is left queued"

    def test_the_close_also_retires_spawn_approval_delivery(self) -> None:
        from kiro_crew.messaging.spawn_approval_delivery import (
            register_channel_delivery,
            resolve_channel_delivery,
        )
        from kiro_crew.telegram.gateway import _client_close_hook

        d, _cli, _sess = _dispatcher({7})
        register_channel_delivery("telegram", d.deliver_spawn_approval)
        _client_close_hook(d, d.deliver_spawn_approval)()
        assert resolve_channel_delivery("telegram:kirocrew:direct:7") is None

    def test_an_old_clients_close_keeps_a_restarted_dispatchers_drain(self) -> None:
        """A restart registers the replacement before the old client's close runs."""
        from kiro_crew.telegram.gateway import _client_close_hook

        old, _c1, _s1 = _dispatcher({7})
        new, _c2, _s2 = _dispatcher({7})  # registers itself, replacing ``old``
        _client_close_hook(old, old.deliver_spawn_approval)()
        assert queue_drain._DRAINS.get("telegram") == new._drain_queue


class TestTheDrainIsNotReEntered:
    @pytest.mark.asyncio
    async def test_a_second_drain_of_the_same_channel_is_retained_not_run(self) -> None:
        """A turn tail reaching the drain while a retried drain of the same channel is
        still pumping must not start a second pump on the same queue."""
        rounds: list[int] = []
        inner_ran: list[bool] = []

        async def _inner_pump(foreign: set[str]) -> None:
            inner_ran.append(True)

        async def _outer_pump(foreign: set[str]) -> None:
            rounds.append(len(rounds))
            if len(rounds) == 1:
                await queue_drain.drain_until_quiet(
                    channel="telegram", session_key=_KEY, pump=_inner_pump
                )

        await queue_drain.drain_until_quiet(channel="telegram", session_key=_KEY, pump=_outer_pump)

        assert inner_ran == [], "the nested drain did not pump"
        assert rounds == [0, 1], "the running drain re-pumped once for it instead"
        assert _KEY not in queue_drain._ACTIVE, "and the marker is released at the end"
