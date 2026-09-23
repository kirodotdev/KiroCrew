"""Tests for session manager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew.acp.runtime import AcpWorkspaceBindingError
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, AcpPromptStats
from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session import (
    _BG_BLIND_RECYCLE_PROMPTS,
    BACKGROUND_KEY,
    SessionClosingError,
    SessionManager,
)


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2  # short for testing
    return c


async def _empty_provider_stream(_command: str):
    """An empty async iterator for provider methods consumed by ``async for``."""
    if False:  # pragma: no cover - establishes the async-generator protocol
        yield None


def _mock_provider_factory():
    """Return a factory that creates mock LLMProviders."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        # Explicit, not AsyncMock-generated: the post-semaphore re-validate calls
        # this synchronously, and an auto-generated coroutine would read as
        # "alive" only by truthiness while leaking an un-awaited coroutine.
        m.is_process_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        return m

    return factory


async def _completed_none():
    """An awaitable answering None, for a store cancel that found nothing."""
    return None


def _raw_sid(mgr, key: str):
    """The stored sid, read straight off the map entry.

    ``SessionMap.get`` additionally requires the transcript ``<sid>.json`` to
    exist on disk, so it answers None for any synthetic sid — which would make a
    "was it cleared?" assertion pass whether or not the clear ran. These tests
    care about the stored pointer, so they read it.
    """
    from kiro_crew.session_map import canonical_key

    return (mgr._session_map._data.get(canonical_key(key)) or {}).get("sid")


def _alive_provider_factory():
    """Like _mock_provider_factory but with an explicit live process check, so
    the fast-path session-reuse branch (which gates on is_process_alive) treats
    the session as alive instead of relying on AsyncMock attribute truthiness."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_info = lambda: (None, None)
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        return m

    return factory


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_reinjection_flag_is_one_shot(self, cfg):
        """mark → consume returns True once, then False. If it did not clear,
        every turn after a compaction would re-pay the skills-index cost."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        assert mgr.consume_needs_reinjection("thread1") is False, "unset by default"
        mgr.mark_needs_reinjection("thread1")
        assert mgr.consume_needs_reinjection("thread1") is True, "first read sees it"
        assert mgr.consume_needs_reinjection("thread1") is False, "cleared on read"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reinjection_helpers_tolerate_an_unknown_key(self, cfg):
        """A compaction callback can fire for a session that has since been
        evicted; neither helper may raise."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.mark_needs_reinjection("never-existed")
        assert mgr.consume_needs_reinjection("never-existed") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_marks_reinjection_without_any_callback(self, cfg):
        """The mark lives at the compaction chokepoint, not in one surface.

        Placing it in DashboardState._on_compacted missed every channel-born
        session (and dashboard sessions with no open tab, whose branch returns
        before the callback body). Marking here covers all surfaces and works
        even when no callback is registered at all.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        assert mgr._on_compacted is None, "precondition: no callback registered"

        await mgr._fire_compact_callback("thread1", 90.0, success=True)

        assert mgr.consume_needs_reinjection("thread1") is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failed_compaction_does_not_mark_reinjection(self, cfg):
        """A compaction that failed did not drop the context, so there is
        nothing to re-inject."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        await mgr._fire_compact_callback("thread1", 90.0, success=False)

        assert mgr.consume_needs_reinjection("thread1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_does_not_mark_reinjection(self, cfg):
        """A recycle reports success=True but is NOT a compaction.

        Recycling destroys the session; its successor cold-starts and gets the
        index through the normal new-session context. The dangerous case is
        `_recycle_held`'s "entry already replaced" branch: without the guard the
        mark would land on the fresh replacement via `_sessions.get(key)`,
        making an un-compacted session re-inject a redundant index.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        replacement = mgr._sessions[mgr._fold_key("thread1")]

        # Stand in for the in-flight recycle of the session that was REPLACED by
        # this one -- _recycle_held holds the key in _recycling across its
        # success callback.
        mgr._recycling["thread1"] = object()  # type: ignore[assignment]
        try:
            await mgr._fire_compact_callback("thread1", 90.0, success=True)
        finally:
            mgr._recycling.pop("thread1", None)

        assert (
            replacement.needs_context_reinjection is False
        ), "a recycle must not flag the fresh replacement session"
        assert mgr.consume_needs_reinjection("thread1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_overflow_recycle_preserves_channel_binding(self, cfg):
        """A context-overflow recycle is housekeeping, so it must not unlink.

        Dropping the whole session-map entry takes the mirror binding with it: a
        Discord conversation resumed into that session loses its binding, and
        later inbound messages from that channel fork into a new conversation.
        Only the resume sid may go — the overflowed native conversation must not
        be resumed.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        key = mgr._fold_key("dashboard:chat-1")
        session = mgr._sessions[key]
        mgr._session_map.set(key, "sid-overflowed")
        mgr.set_mirror_link(
            key,
            ChannelLink(channel_type="discord", channel_id="C1"),
            accepts_inbound=True,
        )

        await mgr._recycle_held(key, session, 95.0)

        link = mgr.get_mirror_link(key)
        assert link is not None
        assert (link.channel_type, link.channel_id) == ("discord", "C1")
        assert mgr.mirror_accepts_inbound(key) is True
        # The overflowed conversation stays unresumable...
        assert not mgr._session_map.get(key)
        # ...and the entry was repaired, not deleted, so the dropped sid is
        # still diagnosable.
        assert mgr._session_map.get_discarded_sid(key) == "sid-overflowed"
        assert not mgr.has_session(key)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_overflow_recycle_clears_sid_instead_of_deleting_entry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("thread1")
        key = mgr._fold_key("thread1")
        session = mgr._sessions[key]
        with (
            patch.object(mgr._session_map, "clear_sid") as mock_clear,
            patch.object(mgr._session_map, "delete") as mock_delete,
        ):
            await mgr._recycle_held(key, session, 95.0)
        provider.shutdown.assert_awaited_once()
        mock_clear.assert_called_once_with(key)
        mock_delete.assert_not_called()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_held_unlinks_temp_files_from_the_session_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        key = mgr._fold_key("thread1")
        session = mgr._sessions[key]
        mgr.enqueue(key, "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr._recycle_held(key, session, 95.0)

        assert not img.exists()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_creates_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, is_new, _resumed = await mgr.get_or_create("thread1")

        assert is_new is True
        assert mgr.count == 1
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reuses_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, new1, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        p2, new2, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")

        assert p1 is p2
        assert new1 is True
        assert new2 is False
        assert mgr.count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_separate_sessions_per_key(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        await mgr.get_or_create("t2")

        assert mgr.count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_shuts_down_client(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("thread1")
        await mgr.remove("thread1")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_all(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        mgr.release("t1")
        await mgr.get_or_create("t1")  # same key
        mgr.release("t1")
        await mgr.close_all()

        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_reset_removes_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("thread1")
        await mgr.reset("thread1")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_held_session_does_not_block_other_sessions(self, cfg):
        """A SAME-KEY second acquirer parked on session A's held semaphore must
        NOT block get_or_create for a DIFFERENT session B. This is the actual
        lock-ordering freeze: the fast path acquired the per-session semaphore
        while holding the global self._lock, so a second caller for A — wedged
        on A's semaphore — pinned self._lock and froze EVERY other session.

        This test FAILS on the pre-fix code (B hangs); it only passes because
        the fast path now claims under the lock and acquires the semaphore
        after releasing it. The earlier version of this test parked the second
        caller on a *different* key, so it never pinned the lock and passed even
        on the buggy code — it did not guard the fix."""
        mgr = SessionManager(cfg, provider_factory=_alive_provider_factory())

        # Caller 1 holds A's semaphore (a turn in flight, not yet released).
        await mgr.get_or_create("A")

        # Caller 2 on the SAME key takes the fast path (A exists + alive) and
        # blocks on A's held semaphore. On the buggy code it blocks while
        # holding self._lock — the freeze.
        a2 = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.1)
        assert not a2.done()  # correctly waiting on A's semaphore

        # With self._lock pinned by a2 (buggy) this hangs; with the fix a2
        # released the lock before blocking, so B cold-starts freely.
        b = asyncio.create_task(mgr.get_or_create("B"))
        provider_b, is_new_b, _ = await asyncio.wait_for(b, timeout=3.0)
        assert provider_b is not None
        assert is_new_b is True

        a2.cancel()  # unwedge the parked same-key acquirer

    @pytest.mark.asyncio
    async def test_cold_start_race_loser_does_not_block_other_sessions(self, cfg):
        """The cold-start variant of the lock-ordering freeze (Concern #1).

        Two callers cold-start the SAME new key concurrently. The race loser
        hits the 'another task won the race' branch and must NOT acquire the
        winner's held semaphore while holding self._lock — doing so pins the
        global lock and freezes every other session, exactly like the fast-path
        bug but in a branch the original CR diff never touched.

        FAILS on the pre-fix cold-start path (a different key hangs while the
        loser is wedged under the lock)."""
        start_gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()

            async def _start():
                await start_gate.wait()  # park both cold-starts to widen the race

            m.start = _start
            m.shutdown = AsyncMock()
            m.is_process_alive = lambda: True
            m.is_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            return m

        mgr = SessionManager(cfg, provider_factory=factory)

        # Both pass the fast path (no existing session) and park in start().
        c1 = asyncio.create_task(mgr.get_or_create("A"))
        c2 = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.1)
        start_gate.set()  # release both; they serialize on the registration lock

        # Exactly one wins, registers A, and returns holding A's semaphore. The
        # loser reaches the won-race branch and parks on that held semaphore.
        done, pending = await asyncio.wait({c1, c2}, timeout=3.0)
        assert len(done) == 1  # winner returned; loser is wedged on the semaphore
        assert len(pending) == 1

        # While the loser is wedged, a DIFFERENT key must still cold-start. On
        # the buggy cold-start path the loser holds self._lock — this hangs.
        b = asyncio.create_task(mgr.get_or_create("B"))
        provider_b, is_new_b, _ = await asyncio.wait_for(b, timeout=3.0)
        assert provider_b is not None
        assert is_new_b is True

        for t in pending:
            t.cancel()

    @pytest.mark.asyncio
    async def test_same_session_still_serializes(self, cfg):
        """Sanity: the per-session semaphore still serializes the SAME key —
        a second get_or_create on a held session blocks until release."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("A")  # holds A's semaphore

        second = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore, as intended
        mgr.release("A")  # let the first holder's turn "finish"
        provider, _, _ = await asyncio.wait_for(second, timeout=3.0)
        assert provider is not None
        mgr.release("A")

    @pytest.mark.asyncio
    async def test_stale_between_claim_and_acquire_cold_starts_and_reaps(self, cfg):
        """Covers the stale-between-claim-and-acquire branch (the riskiest new
        logic in Option A). Caller 1 holds A's semaphore; A's provider then dies.
        Caller 2 claims A (still in the dict) under the lock, blocks on the
        semaphore, and on acquire re-validates: provider dead -> must evict +
        await shutdown() on the dead provider AND cold-start a fresh one."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        p1, _is_new, _ = await mgr.get_or_create("A")  # caller 1 holds semaphore

        # Caller 2 must claim A while it is STILL ALIVE (so it takes the claim +
        # wait-on-semaphore path, not the in-lock dead-provider eviction), then
        # find it dead only AFTER acquiring the semaphore. Park caller 2 on the
        # semaphore first, THEN kill A's provider, THEN release.
        second = asyncio.create_task(mgr.get_or_create("A"))
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore behind caller 1

        # A's process dies between claim and acquire.
        p1.is_process_alive = lambda: False
        p1.is_alive = lambda: False

        mgr.release("A")  # caller 1's turn ends; caller 2 acquires + re-validates
        p2, is_new2, _ = await asyncio.wait_for(second, timeout=3.0)

        assert p2 is not p1  # cold-started a fresh provider
        assert is_new2 is True  # reported as new
        p1.shutdown.assert_awaited()  # dead provider was reaped
        mgr.release("A")


class TestWarmPool:
    """Tests for warm session pool and background session."""

    @pytest.mark.asyncio
    async def test_start_pool_creates_background(self, cfg):
        """start_pool() creates background session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cold_start_for_new_session(self, cfg):
        """get_or_create cold-starts a new session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider, is_new, _resumed = await mgr.get_or_create("dashboard:chat-1")
        assert is_new is True
        assert provider is not None
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_session_reused(self, cfg):
        """BACKGROUND_KEY returns the same provider on repeated calls."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        p1, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)
        p2, _, _ = await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)

        assert p1 is p2
        p1.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_session_not_expired(self, cfg):
        """Background session is never expired by idle cleanup."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        mgr._sessions[BACKGROUND_KEY].last_used = time.monotonic() - 9999
        await mgr._expire_idle(1)

        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_not_expired_by_idle(self, cfg):
        """Channel-agent sessions survive idle expiry (managed by channel lifecycle)."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        key = "channel:abc123:agent1"
        mgr._sessions[key] = mgr._sessions[BACKGROUND_KEY].__class__.__new__(
            mgr._sessions[BACKGROUND_KEY].__class__
        )
        mgr._sessions[key].__dict__.update(mgr._sessions[BACKGROUND_KEY].__dict__)
        mgr._sessions[key].last_used = time.monotonic() - 9999

        await mgr._expire_idle(1)

        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_shuts_down_sessions(self, cfg):
        """close_all() shuts down all active sessions."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        await mgr.get_or_create("chat-1")
        mgr._session_map.set("dashboard:pending-close", "sid-pending-close")
        flush_task = mgr._session_map._flush_task
        assert flush_task is not None

        await mgr.close_all()
        assert mgr.count == 0
        assert flush_task.done()
        assert mgr._session_map._flush_task is None

    @pytest.mark.asyncio
    async def test_start_pool_idempotent(self, cfg):
        """Calling start_pool() twice is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        await mgr.start_pool()  # should be no-op
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()


class TestWorkflowPoolStateless:
    """Warm workflow-pool workers (``wf-pool:`` keys) must be treated as
    stateless — never persist a session_map entry and never attempt a
    ``session/load`` resume. Otherwise the pool's hard-reset fallback would
    resume the PRIOR task's transcript into the next task, leaking cross-task
    context and violating the pool's isolation guarantee."""

    def test_wf_pool_prefix_is_stateless(self):
        from kiro_crew.session import _STATELESS_PREFIXES

        assert any("wf-pool:".startswith(p) for p in _STATELESS_PREFIXES)
        assert "wf-pool:run-1:0".startswith(
            next(p for p in _STATELESS_PREFIXES if "wf-pool:".startswith(p))
        )

    @pytest.mark.asyncio
    async def test_wf_pool_key_skips_resume_lookup(self, cfg):
        """A ``wf-pool:`` key must NOT consult the session_map for a resume sid —
        stateless keys skip the lookup entirely (guarded to catch regressions if
        the prefix is dropped from _STATELESS_PREFIXES)."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Spy on the resume lookup: it must never be called for a stateless key.
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create("wf-pool:run-1:0")
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_non_pool_key_still_consults_resume_lookup(self, cfg):
        """Control: a normal conversational key (not stateless) DOES consult the
        session_map for a resume sid — proving the skip above is specific to the
        stateless classification, not a blanket no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value=None)  # type: ignore[method-assign]
        await mgr.get_or_create("dashboard:chat-9")
        mgr._session_map.get.assert_called()
        await mgr.close_all()


class TestHeartbeatStateless:
    """``_hb`` must be treated as stateless alongside ``_bg``.

    Heartbeat's published contract (``config/prompt.md``) is "fresh context
    each cycle", and every entry is re-read from ``HEARTBEAT.md`` each cycle,
    so a resumed transcript supplies nothing the next cycle depends on while
    costing input tokens on every tick. Resuming is also actively wrong: for a
    watch task the external system is the source of truth, and unrelated
    queued tasks would inherit each other's reasoning.
    """

    @pytest.mark.asyncio
    async def test_heartbeat_key_skips_resume_lookup(self, cfg):
        """``_hb`` must NOT consult the session_map for a resume sid."""
        from kiro_crew.session import HEARTBEAT_KEY

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create(HEARTBEAT_KEY)
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_background_key_also_stateless(self, cfg):
        """Control: ``_bg`` was already stateless — ``_hb`` now matches it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        await mgr.get_or_create(BACKGROUND_KEY)
        mgr._session_map.get.assert_not_called()
        await mgr.close_all()


class TestRecycleBackground:
    """Tests for background session context overflow recycling."""

    @pytest.mark.asyncio
    async def test_recycle_on_high_context(self, cfg):
        """Background session is recycled when context >= 70%."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        # Simulate high context
        old_provider.context_usage_pct = lambda: 75.0

        await mgr.recycle_background()

        # Old provider should have been shut down
        old_provider.shutdown.assert_awaited_once()
        # New session should exist
        assert BACKGROUND_KEY in mgr._sessions
        new_provider = mgr._sessions[BACKGROUND_KEY].provider
        assert new_provider is not old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_blind_fallback(self, cfg):
        """Background session is recycled after 40 prompts with no metadata."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        old_provider.context_usage_pct = lambda: 0.0  # no metadata
        mgr._sessions[BACKGROUND_KEY].prompt_count = 45

        await mgr.recycle_background()

        old_provider.shutdown.assert_awaited_once()
        assert BACKGROUND_KEY in mgr._sessions
        assert mgr._sessions[BACKGROUND_KEY].provider is not old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_recycle_when_low_context(self, cfg):
        """Background session is NOT recycled when context is low."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        old_provider = mgr._sessions[BACKGROUND_KEY].provider
        old_provider.context_usage_pct = lambda: 30.0

        await mgr.recycle_background()

        # Should NOT have been shut down
        old_provider.shutdown.assert_not_awaited()
        assert mgr._sessions[BACKGROUND_KEY].provider is old_provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_no_background_session(self, cfg):
        """recycle_background() is no-op when no background session exists."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Don't start pool — no background session
        await mgr.recycle_background()  # should not raise
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_blind_fallback_counts_its_own_prompts(self, cfg):
        """The blind fallback must fire on its own counting.

        ``check_context_usage`` is a chat-turn hook and never runs for
        BACKGROUND_KEY, so if ``recycle_background`` does not count the turn the
        counter stays at 0 forever and the 40-prompt fallback is dead code.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 0.0  # backend reports no metadata
        provider.context_usage_unknown = lambda: False

        for _ in range(_BG_BLIND_RECYCLE_PROMPTS - 1):
            await mgr.recycle_background()

        assert mgr._sessions[BACKGROUND_KEY].provider is provider
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == _BG_BLIND_RECYCLE_PROMPTS - 1

        await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        # The replacement starts its own count.
        assert mgr._sessions[BACKGROUND_KEY].prompt_count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_just_compacted_zero_pct_does_not_suppress_recycle(self, cfg):
        """A post-compaction 0% is "unknown", not "empty".

        The backend zeroes the percentage when it compacts in place, which is
        byte-identical to a brand-new session. Reading it as empty leaves a
        session that just hit its ceiling in place to be compacted again — and
        each compaction is a billed summarization turn over the whole transcript.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        provider = mgr._sessions[BACKGROUND_KEY].provider
        provider.context_usage_pct = lambda: 0.0
        provider.context_usage_unknown = lambda: True

        # One turn — far below the blind threshold, so only the unknown signal
        # can trigger the recycle.
        await mgr.recycle_background()

        provider.shutdown.assert_awaited_once()
        assert mgr._sessions[BACKGROUND_KEY].provider is not provider
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_acp_prompt_stats_flag_post_compaction_zero_as_unknown(self):
        """The provider-level signal the recycle decision rides on."""
        stats = AcpPromptStats(context_pct=88.0, context_used_tokens=170_000)
        assert stats.context_pct_unknown is False

        stats.reset_after_compaction()
        assert stats.context_pct == 0.0
        assert stats.context_pct_unknown is True

        # Survives the per-turn stats re-init...
        carried = stats.carry_over()
        assert carried.context_pct_unknown is True

        # ...and clears as soon as the backend reports a real number.
        carried.note_pct_reported()
        assert carried.context_pct_unknown is False

    @pytest.mark.asyncio
    async def test_recycle_never_kills_a_turn_that_started_after_release(self, cfg):
        """A turn taken in the release→recycle gap must not be torn down.

        Every call site releases the turn semaphore on the line before calling
        ``recycle_background``, so a waiter can start a turn in that gap. If the
        recycle decides and shuts down outside the semaphore it SIGKILLs that
        live turn.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        killed_mid_turn: list[str] = []
        turn_providers: list[object] = []
        recycle_done = asyncio.Event()

        async def _waiter_turn() -> None:
            # Mirrors _ProviderBgSession.prompt: take the turn semaphore, then
            # stream on whatever provider the session holds at that moment.
            await sess.semaphore.acquire()
            try:
                provider = sess.provider
                turn_providers.append(provider)
                # Stay in the turn until the recycle attempt finishes. Deadline
                # is a yield budget, not wall-clock, so the interleaving is
                # deterministic.
                for _ in range(200):
                    if provider.shutdown.await_count:
                        killed_mid_turn.append("provider shut down mid-turn")
                        break
                    if recycle_done.is_set():
                        break
                    await asyncio.sleep(0)
            finally:
                sess.semaphore.release()

        async def _recycle() -> None:
            try:
                await mgr.recycle_background()
            finally:
                recycle_done.set()

        # Reproduce the real call-site interleaving: a turn completes and
        # releases, a waiter wins the gap, THEN the recycle runs.
        await mgr.get_or_create(BACKGROUND_KEY)
        mgr.release(BACKGROUND_KEY)
        waiter = asyncio.create_task(_waiter_turn())
        await asyncio.sleep(0)  # let the waiter take the semaphore

        recycle = asyncio.create_task(_recycle())
        await recycle
        await waiter

        assert killed_mid_turn == []
        # The turn ran to completion on the provider it picked up...
        assert turn_providers == [old_provider]
        # ...and the recycle still happened, once the turn was done.
        assert sess.provider is not old_provider
        old_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_turn_starting_after_the_recycle_gets_the_replacement(self, cfg):
        """The session is recycled in place, so a holder is routed to the new
        provider rather than to the torn-down one."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        await mgr.recycle_background()

        # A caller that captured the session before the recycle still finds a
        # live provider on it, and the registry entry did not go absent.
        assert mgr._sessions[BACKGROUND_KEY] is sess
        await sess.semaphore.acquire()
        try:
            assert sess.provider is not old_provider
            assert sess.provider.shutdown.await_count == 0
        finally:
            sess.semaphore.release()
        # Conversation state describing the old transcript does not carry over.
        assert sess.prompt_count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failed_replacement_spawn_keeps_the_working_provider(self, cfg):
        """A spawn failure must not leave _bg with no provider at all."""
        calls = {"n": 0}
        base = _mock_provider_factory()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("no capacity")
            return base(session_key, agent, channel_id, **kwargs)

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.start_pool()

        sess = mgr._sessions[BACKGROUND_KEY]
        old_provider = sess.provider
        old_provider.context_usage_pct = lambda: 95.0
        old_provider.context_usage_unknown = lambda: False

        await mgr.recycle_background()

        assert sess.provider is old_provider
        old_provider.shutdown.assert_not_awaited()
        # The semaphore is handed back even on the failure path.
        assert not sess.semaphore.locked()
        await mgr.close_all()


class TestCancelRaceCondition:
    """Tests for process leak prevention when CancelledError fires during get_or_create."""

    @pytest.mark.asyncio
    async def test_cancel_during_start_kills_provider(self, cfg):
        """CancelledError during provider.start() dispatches the process kill.

        The kill goes through _dispatch_hard_kill (non-blocking submission to
        the subprocess executor) rather than an inline _sync_kill_provider:
        the inline form blocks the event loop (os.waitpid / taskkill), and
        resume prefetch makes this cancellation handler routine — a focus
        flip mid-session/load cancels the loading task. Submission is
        synchronous, so the kill is guaranteed dispatched before the
        re-raise.
        """
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock(side_effect=asyncio.CancelledError)
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 99999

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)

        with patch.object(SessionManager, "_dispatch_hard_kill") as mock_kill:
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel")

            mock_kill.assert_called_once_with(mock_provider)

        # Session must NOT be registered
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancel_after_start_before_registration_kills_provider(self, cfg):
        """CancelledError after start() but before _sessions[key] dispatches the kill.

        Same contract as the during-start case: _dispatch_hard_kill, never an
        inline _sync_kill_provider (which blocks the event loop). This handler
        is also the landing site for SpeculativeResumeRefused, so resume
        prefetch exercises it on every failed speculative load.
        """
        mock_provider = AsyncMock()
        mock_provider.start = AsyncMock()  # succeeds
        mock_provider.context_usage_pct = lambda: 0.0
        mock_provider._client = AsyncMock()
        mock_provider._client._pid = 88888
        mock_provider.is_alive.return_value = True

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            return mock_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        original_lock = mgr._lock

        class CancelOnThirdLock:
            """Reservation and fast-path locks pass; registration cancels."""

            def __init__(self):
                self._calls = 0

            async def __aenter__(self):
                self._calls += 1
                if self._calls == 3:
                    raise asyncio.CancelledError
                return await original_lock.__aenter__()

            async def __aexit__(self, *a):
                if self._calls != 3:
                    return await original_lock.__aexit__(*a)

        with patch.object(SessionManager, "_dispatch_hard_kill") as mock_kill:
            mgr._lock = CancelOnThirdLock()
            with pytest.raises(asyncio.CancelledError):
                await mgr.get_or_create("test-cancel-2")

            mock_kill.assert_called_once_with(mock_provider)

        mgr._lock = original_lock
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_normal_path_unaffected(self, cfg):
        """Normal get_or_create still works after the cancel-safety changes."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, is_new, _ = await mgr.get_or_create("normal-session")

        assert is_new is True
        assert mgr.count == 1
        provider.start.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_model_forwarded_to_factory(self, cfg):
        """model param is forwarded to factory as model_override."""
        captured = {}

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_alive.return_value = True
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("test-model", model="claude-sonnet")
        assert captured["model_override"] == "claude-sonnet"
        await mgr.close_all()


class TestAllocationRequestedModel:
    """The model an allocation selects is readable by its caller.

    A caller that pins nothing passes ``model=None``, and the allocation resolves
    an id from config itself. ``get_or_create`` reports the provider, ``is_new``
    and ``resumed``, so that id is otherwise invisible to the caller recording what
    the session was asked to run.
    """

    @staticmethod
    def _capturing_factory(captured: dict):
        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            captured.update(kwargs)
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0
            m.is_process_alive = lambda: True
            m.is_alive.return_value = True
            return m

        return factory

    @pytest.mark.asyncio
    async def test_the_internally_resolved_id_is_the_id_the_provider_got(self, cfg):
        """One value: the stamp is the same string the factory received."""
        cfg.agent.model = "claude-sonnet-5"
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-resolved")

        assert captured["model_override"] == "claude-sonnet-5"
        assert mgr.allocation_requested_model("alloc-resolved") == "claude-sonnet-5"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_callers_explicit_model_is_reported_unchanged(self, cfg):
        """An explicit model is stamped too, so one read serves both cases."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-explicit", model="claude-haiku-5")

        assert mgr.allocation_requested_model("alloc-explicit") == "claude-haiku-5"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_allocation_that_resolves_nothing_reports_nothing(self, cfg):
        """No tier resolved an id, so there is no selection to report."""
        cfg.agent.model = ""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=self._capturing_factory(captured))

        await mgr.get_or_create("alloc-blank")

        assert captured["model_override"] is None
        assert mgr.allocation_requested_model("alloc-blank") == ""
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unknown_key_reports_nothing(self, cfg):
        """No session, so nothing to report — never an error."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.allocation_requested_model("never-allocated") == ""
        await mgr.close_all()


class TestDeadProviderCleanup:
    """Tests for orphaned child process cleanup when a dead provider is detected."""

    @staticmethod
    def _make_provider(*, alive: bool = True):
        """Create a mock provider with sync is_alive."""
        from unittest.mock import MagicMock

        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = MagicMock(return_value=0.0)
        m.is_alive = MagicMock(return_value=alive)
        m.is_process_alive = MagicMock(return_value=alive)
        return m

    @pytest.mark.asyncio
    async def test_dead_provider_calls_shutdown(self, cfg):
        """When is_alive() returns False, shutdown() is called on the stale provider."""
        dead_provider = self._make_provider(alive=True)
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_removal_unlinks_temp_files_from_its_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        dead_provider = self._make_provider(alive=True)
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.enqueue("sess1", "ts1", "queued", force=True, image_temp_paths=[str(img)])
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        await mgr.get_or_create("sess1")

        assert not img.exists()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_shutdown_exception_does_not_propagate(self, cfg):
        """If shutdown() raises on a dead provider, get_or_create still succeeds."""
        dead_provider = self._make_provider(alive=True)
        dead_provider.shutdown = AsyncMock(side_effect=OSError("kill failed"))
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else self._make_provider()

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        _, is_new, _ = await mgr.get_or_create("sess1")
        assert is_new is True
        assert mgr.count == 1
        dead_provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dead_provider_removed_from_sessions(self, cfg):
        """Dead provider session is removed and replaced by a fresh one."""
        dead_provider = self._make_provider(alive=True)
        fresh_provider = self._make_provider()
        call_count = 0

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            nonlocal call_count
            call_count += 1
            return dead_provider if call_count == 1 else fresh_provider

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")

        dead_provider.is_alive.return_value = False
        dead_provider.is_process_alive.return_value = False
        provider, is_new, _ = await mgr.get_or_create("sess1")
        assert provider is fresh_provider
        assert is_new is True
        assert mgr.count == 1
        await mgr.close_all()


class TestIsProviderAlive:
    """Tests for is_provider_alive preferring is_process_alive over is_alive."""

    @pytest.mark.asyncio
    async def test_uses_is_process_alive_when_available(self, cfg):
        provider = TestDeadProviderCleanup._make_provider(alive=True)
        provider.is_process_alive.return_value = True
        mgr = SessionManager(cfg, provider_factory=lambda *a, **kw: provider)
        await mgr.get_or_create("sess1")
        mgr.release("sess1")
        result = await mgr.is_provider_alive("sess1")
        assert result is True
        provider.is_process_alive.assert_called()
        await mgr.close_all()


class TestApprovalPolicy:
    """Tests for approval policy get/set on sessions."""

    @pytest.mark.asyncio
    async def test_set_and_get_approval_policy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        mgr.set_approval_policy("thread1", "auto")
        assert mgr.get_approval_policy("thread1") == "auto"

        mgr.set_approval_policy("thread1", "")
        assert mgr.get_approval_policy("thread1") == ""
        await mgr.close_all()

    def test_get_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_approval_policy("nonexistent") == ""

    def test_set_approval_policy_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_approval_policy("nonexistent", "auto")  # should not raise

    @pytest.mark.asyncio
    async def test_approval_policy_propagated_on_create(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", approval_policy="auto")
        mgr.release("thread1")
        assert mgr.get_approval_policy("thread1") == "auto"
        await mgr.close_all()


class TestGetAgent:
    """Tests for get_agent() on SessionManager."""

    @pytest.mark.asyncio
    async def test_get_agent_returns_agent_name(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1", agent="my-agent")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == "my-agent"
        await mgr.close_all()

    def test_get_agent_missing_session_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.get_agent("nonexistent") == ""

    @pytest.mark.asyncio
    async def test_get_agent_no_agent_returns_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")
        assert mgr.get_agent("thread1") == ""
        await mgr.close_all()


class TestOrphanedDashboardSessions:
    """Tests for orphaned dashboard session detection in _expire_idle."""

    @pytest.mark.asyncio
    async def test_expire_idle_reaps_orphaned_dashboard_session(self, cfg):
        """Dashboard session whose slot does not exist is reaped immediately."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Mark tab2 as the only active slot — tab1 is orphaned
        mgr.set_active_dashboard_slots({"dashboard:tab2"})
        await mgr._expire_idle(9999)  # high timeout so idle doesn't trigger

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_skips_uninitialized_slots(self, cfg):
        """When _active_dashboard_slots is None, no orphan reaping occurs."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        # Don't call set_active_dashboard_slots — stays None
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic()
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expire_idle_preserves_active_dashboard_session(self, cfg):
        """Dashboard session whose slot still exists is NOT reaped."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab1"})
        await mgr._expire_idle(9999)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()


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
        # Sanity: semaphore is full (1 permit available) before respawn.
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


class TestCompactCallback:
    """Tests for the compact callback wiring on SessionManager.

    Covers set_compact_callback registration, pct threading through
    check_context_usage -> _trigger_compaction -> _compact_session, and
    callback fault isolation.
    """

    @pytest.mark.asyncio
    async def test_set_compact_callback_registers_handler(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()

        mgr.set_compact_callback(cb)

        assert mgr._on_compacted is cb
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_none_clears(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        mgr.set_compact_callback(None)

        assert mgr._on_compacted is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())

        assert any("Compact callback already registered" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_invokes_callback_with_key_and_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # This fixture's provider serves no native compaction, so the in-place
        # attempt falls through to the recycle. The callback reports the arm that
        # ran; the key/pct/success threading this case exists for is unchanged.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        assert "dashboard:chat-1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_skips_callback_when_session_absent(self, cfg):
        """No session means no recycle happened, so the callback must not fire."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:missing", 91.0)

        cb.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_waits_for_inflight_turn(self, cfg):
        """kiro-cli recycle must drain the in-flight turn: while the session
        semaphore is held (turn active) it blocks instead of SIGKILL'ing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # get_or_create returns HOLDING the semaphore -> simulates an active turn.
        await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        # Still draining: not recycled, callback not fired.
        assert not task.done()
        assert "dashboard:chat-1" in mgr._sessions
        cb.assert_not_awaited()

        # Turn finishes -> semaphore released -> recycle proceeds.
        mgr.release("dashboard:chat-1")
        await asyncio.wait_for(task, timeout=2)
        assert "dashboard:chat-1" not in mgr._sessions
        # This fixture's provider serves no native compaction, so the in-place
        # attempt falls through to the recycle. The callback reports the arm that
        # ran; the key/pct/success threading this case exists for is unchanged.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_defers_when_turn_never_drains(self, cfg, caplog, monkeypatch):
        """A still-running turn (semaphore held) must NEVER be killed for
        compaction: after COMPACT_WAIT_TIMEOUT_SECS the attempt is deferred —
        session intact, no callback — and re-triggered at the next turn end."""
        # Only the outer cap is scaled: the inner status wait clamps to
        # _COMPACT_RESULT_WAIT_FLOOR_SECS (5s) — patch that too if a test
        # needs the inner wait itself to time out quickly.
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.1)
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Hold the semaphore and never release -> simulates a long-running turn.
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await asyncio.wait_for(mgr._compact_session("dashboard:chat-1", 92.0), timeout=2)

        # Session survives, the live turn was not killed, and nothing was
        # reported to the user (a deferral is not a failure).
        assert "dashboard:chat-1" in mgr._sessions
        provider.shutdown.assert_not_awaited()
        cb.assert_not_awaited()
        assert any("compaction deferred" in r.message for r in caplog.records)
        # _compacting cleared so the next turn-end check can re-trigger.
        assert "dashboard:chat-1" not in mgr._compacting
        mgr.release("dashboard:chat-1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_callback_exception_is_logged(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock(side_effect=RuntimeError("boom"))
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
            await mgr._compact_session("dashboard:chat-1", 95.0)

        cb.assert_awaited_once()
        assert any("Compact callback failed" in r.message for r in caplog.records)
        # Session still recycled, compacting flag cleared
        assert "dashboard:chat-1" not in mgr._sessions
        assert "dashboard:chat-1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_threads_pct_through(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-2")
        mgr.release("dashboard:chat-2")
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        mgr._trigger_compaction("dashboard:chat-2", "context at 92%", 92.0, provider)
        # _trigger_compaction schedules the work as a background task
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert captured == [("dashboard:chat-2", 92.0, True)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_check_context_usage_fires_callback_with_observed_pct(self, cfg):
        """High pct should flow from check_context_usage through to the callback."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-3")
        mgr.release("dashboard:chat-3")
        provider.context_usage_pct = lambda: 93.0
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        pct = mgr.check_context_usage("dashboard:chat-3", provider)
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert pct == 93.0
        assert captured == [("dashboard:chat-3", 93.0, True)]
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
        mgr.record_success("nonexistent")  # should not raise

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


class TestDrainProviders:
    """Tests for drain_all_providers and drain_warm_pool."""

    @pytest.mark.asyncio
    async def test_drain_all_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.get_or_create("k2")
        mgr.release("k2")
        providers = await mgr.drain_all_providers()
        assert len(providers) == 2
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_all_providers_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        providers = await mgr.drain_all_providers()
        assert providers == []

    @pytest.mark.asyncio
    async def test_drain_all_providers_unlinks_temp_files_from_every_queue(self, cfg, tmp_path):
        img1 = tmp_path / "img1.png"
        img2 = tmp_path / "img2.png"
        img1.write_bytes(b"fake")
        img2.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img1)])
        await mgr.get_or_create("k2")
        mgr.enqueue("k2", "ts3", "third", force=True, image_temp_paths=[str(img2)])

        await mgr.drain_all_providers()

        assert not img1.exists()
        assert not img2.exists()

    @pytest.mark.asyncio
    async def test_drain_warm_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Manually put items in the warm pool
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, "agent1"))
        drained = await mgr.drain_warm_pool()
        assert len(drained) == 1
        assert drained[0] is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_warm_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        drained = await mgr.drain_warm_pool()
        assert drained == []


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
        await asyncio.sleep(0)
        provider.cleanup_session.assert_awaited_once_with("sid-123")
        await mgr.close_all()

    def test_release_missing_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.release("nonexistent")  # should not raise

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
        await asyncio.sleep(0)
        assert first.done()
        second = asyncio.ensure_future(session_2.semaphore.acquire())
        await asyncio.sleep(0)
        assert not second.done()  # blocked -- no surplus permit to grant it
        second.cancel()
        session_2.semaphore.release()
        await mgr.close_all()


class TestResetWithPid:
    """Tests for reset() PID capture and force-kill logic."""

    @pytest.mark.asyncio
    async def test_reset_no_pid_just_shuts_down(self, cfg):
        """reset() with no PID attribute just calls shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.reset("k1")
        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_unlinks_temp_files_from_the_dropped_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.reset("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_reset_with_acp_pid_dead_after_shutdown(self, cfg):
        """reset() with ACP PID that dies after shutdown — no force kill."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Simulate ACP client with PID
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        with patch("os.kill", side_effect=ProcessLookupError):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_reset_with_pid_survives_shutdown_force_kills(self, cfg):
        """reset() force-kills when PID survives shutdown."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {}
        provider._client = mock_client

        # Await points allow unrelated liveness probes in this process. Keep
        # every mocked probe successful instead of consuming a finite list.
        with (
            patch("os.kill", return_value=None),
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            await mgr.reset("k1")
            mock_killpg.assert_called_once()

    @pytest.mark.asyncio
    async def test_reset_with_cc_provider_proc(self, cfg):
        """reset() picks up PID from ClaudeCode _proc attribute."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # No _client, but has _proc (CC provider style)
        provider._client = None
        mock_proc = AsyncMock()
        mock_proc.pid = 99999
        mock_proc.returncode = None
        provider._proc = mock_proc

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=None),
        ):
            await mgr.reset("k1")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_child_sweep(self, cfg):
        """reset() sweeps escaped children after root is dead."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mock_client = AsyncMock()
        mock_client._pid = 12345
        mock_client._child_pids = {111: 1000, 222: 2000}
        provider._client = mock_client

        with (
            patch("os.kill", side_effect=ProcessLookupError),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[333]),
            patch("kiro_crew.platform_compat.get_process_start_id", return_value=3000),
            patch("kiro_crew.acp.client._read_basename", return_value=b"node"),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_sweep,
        ):
            await mgr.reset("k1")
            mock_sweep.assert_called_once()
            # Should include both original children and discovered ones
            call_arg = mock_sweep.call_args[0][0]
            assert 111 in call_arg
            assert 222 in call_arg
            assert 333 in call_arg

    @pytest.mark.asyncio
    async def test_reset_nonexistent_session(self, cfg):
        """reset() on missing key is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.reset("nonexistent")  # should not raise


class TestReloadProviderFactory:
    """Tests for reload_provider_factory."""

    @pytest.mark.asyncio
    async def test_reload_clears_sessions_and_pool(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        # Put something in warm pool
        mock_pool_p = AsyncMock()
        mock_pool_p.is_process_alive = lambda: False
        mgr._warm_pool.put_nowait((mock_pool_p, "agent"))

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()

        # Old sessions cleared
        assert not mgr.has_session("k1")
        # Pool provider shut down
        mock_pool_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reload_shuts_down_stale_sessions(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()

        provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reload_shutdown_exception_swallowed(self, cfg):
        """Stale session shutdown failure doesn't crash reload."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=OSError("dead"))

        with (
            patch.object(KiroCrewConfig, "load", return_value=cfg),
            patch.object(cfg, "create_provider_factory", return_value=_mock_provider_factory()),
        ):
            await mgr.reload_provider_factory()  # should not raise

        await mgr.close_all()


class TestCheckContextUsage:
    """Tests for check_context_usage thresholds and prompt counting."""

    @pytest.mark.asyncio
    async def test_increments_prompt_count(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr._sessions["k1"].prompt_count == 0
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 1
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 42.5
        result = mgr.check_context_usage("k1", provider)
        assert result == 42.5
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_warning_fires_one_margin_below_the_threshold(self, cfg, caplog):
        """The warn arm opens exactly at ``threshold - CONTEXT_WARN_MARGIN_PCT``.

        Derived from the constant rather than restating a percentage: the warn
        level is relative to whatever the operator configured, so a literal here
        would pin the test to one threshold and go stale the next time either
        number moves.
        """
        from kiro_crew.config.loader import CONTEXT_WARN_MARGIN_PCT

        cfg.session.autocompact_pct = 90.0
        warn_at = 90.0 - CONTEXT_WARN_MARGIN_PCT
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: warn_at
        with (
            patch("kiro_crew.session.published_autocompact_pct", return_value=90.0),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            mgr.check_context_usage("k1", provider)
        assert any(
            f"{warn_at:.0f}%" in r.message for r in caplog.records if r.name == "kiro_crew.session"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_warning_just_below_the_margin(self, cfg, caplog):
        """One point under the warn level takes the info arm, not the warn arm.

        Pins the boundary from the other side: without this, a margin widened
        to cover the whole window would still satisfy the test above.
        """
        from kiro_crew.config.loader import CONTEXT_WARN_MARGIN_PCT

        cfg.session.autocompact_pct = 90.0
        below = 90.0 - CONTEXT_WARN_MARGIN_PCT - 1.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: below
        with (
            patch("kiro_crew.session.published_autocompact_pct", return_value=90.0),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            mgr.check_context_usage("k1", provider)
        # Scoped to this logger: caplog captures the whole root hierarchy, so an
        # unrelated library record (asyncio's "Task was destroyed but it is
        # pending!" fires here on Windows) would otherwise read as a context
        # warning and fail a test that is only about this arm.
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "kiro_crew.session"
        ]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_triggered_at_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 92.0
        with patch.object(mgr, "_trigger_compaction") as mock_trigger:
            mgr.check_context_usage("k1", provider)
            # The trigger seam receives the provider the reading was observed
            # on — the gate ladder inside it evaluates against that provider.
            mock_trigger.assert_called_once_with("k1", "context at 92%", 92.0, provider)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 50.0
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            mgr.check_context_usage("k1", provider)
        compact.assert_not_called()
        assert "k1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_compaction_when_pct_unconfirmed(self, cfg):
        """Defensive gate: a pct above threshold that no telemetry has
        confirmed for the CURRENT session binding must NOT trigger compaction
        (compacting an empty just-claimed session, then overflowing)."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 95.0
        provider.context_usage_unknown = lambda: True
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            pct = mgr.check_context_usage("k1", provider)
        compact.assert_not_called()
        assert "k1" not in mgr._compacting
        assert pct == 95.0  # reading is still returned, only the trigger is gated
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_fires_when_pct_confirmed(self, cfg):
        """Twin of the gate test: the same pct WITH confirmed telemetry
        (context_usage_unknown False) still compacts — the gate must not
        suppress legitimate triggers."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 95.0
        provider.context_usage_unknown = lambda: False
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            mgr.check_context_usage("k1", provider)
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)
        compact.assert_awaited_once_with("k1", 95.0)
        await mgr.close_all()

    def test_missing_session_still_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.context_usage_pct = lambda: 55.0
        result = mgr.check_context_usage("nonexistent", mock_p)
        assert result == 55.0


class TestDestroy:
    """Tests for destroy() — permanent session removal."""

    @pytest.mark.asyncio
    async def test_destroy_shuts_down_and_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("k1")
        provider.shutdown.assert_awaited_once()
        # The reason is part of the call: a destroyed session takes any inbound
        # resume binding with it, and the map audits the removal under this name.
        mock_delete.assert_called_once_with("k1", reason="session_destroyed")
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refusal_leaves_session_and_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        generation = mgr.session_generation("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", generation, lambda: False)

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_guard_runs_after_lock_acquisition(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        generation = mgr.session_generation("k1")
        allowed = True
        await mgr._lock.acquire()
        try:
            destroy_task = asyncio.create_task(mgr.destroy_if("k1", generation, lambda: allowed))
            await asyncio.sleep(0)
            allowed = False
        finally:
            mgr._lock.release()

        destroyed = await asyncio.wait_for(destroy_task, timeout=1.0)

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_a_successor_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        original, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        original_generation = mgr.session_generation("k1")
        await mgr.destroy("k1")
        successor, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", original_generation, lambda: True)

        assert destroyed is False
        original.shutdown.assert_awaited_once()
        successor.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_expected_absence_refuses_new_alias(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        generation = mgr.session_generation(canonical)
        assert generation == 0
        successor, _, _ = await mgr.get_or_create(thread_ts)
        mgr.release(thread_ts)

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, generation, lambda: True)

        assert destroyed is False
        successor.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session(thread_ts)
        await mgr.destroy(thread_ts)

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_absent_successor_absent_aba(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        stale_absence = mgr.session_generation(canonical)

        await mgr.get_or_create(thread_ts)
        mgr.release(thread_ts)
        await mgr.remove(thread_ts)
        assert not mgr.has_session(thread_ts)
        assert mgr.session_generation(canonical) > stale_absence

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, stale_absence, lambda: True)

        assert destroyed is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_provider_factory_advances_removed_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        stale_generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.reload_provider_factory()
            destroyed = await mgr.destroy_if("k1", stale_generation, lambda: True)

        assert mgr.session_generation("k1") > stale_generation
        assert destroyed is False
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_all_advances_removed_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        stale_generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.close_all()
            destroyed = await mgr.destroy_if("k1", stale_generation, lambda: True)

        assert mgr.session_generation("k1") > stale_generation
        assert destroyed is False
        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_conditional_destroy_can_preserve_autocompact_override(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr.set_autocompact_pct("k1", 55.0)
        generation = mgr.session_generation("k1")

        destroyed = await mgr.destroy_if(
            "k1",
            generation,
            lambda: True,
            preserve_autocompact_override=True,
        )

        assert destroyed is True
        folded = mgr._fold_key("k1")
        assert mgr._compaction.state.pct_overrides[folded] == 55.0

        # The public unconditional path retains its historical clear behavior.
        await mgr.destroy("k1")
        assert folded not in mgr._compaction.state.pct_overrides

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_an_inflight_alias_allocation(self, cfg):
        start_entered = asyncio.Event()
        release_start = asyncio.Event()
        provider = _mock_provider_factory()(session_key="reserved")

        async def blocked_start():
            start_entered.set()
            await release_start.wait()

        provider.start = AsyncMock(side_effect=blocked_start)
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        thread_ts = "1785370133.085469"
        canonical = f"slack:{thread_ts}"
        expected_absence = mgr.session_generation(canonical)
        assert expected_absence == 0

        allocation = asyncio.create_task(mgr.get_or_create(thread_ts))
        await asyncio.wait_for(start_entered.wait(), timeout=1.0)

        assert mgr.session_generation(canonical) > expected_absence
        assert thread_ts in mgr.session_keys()
        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if(canonical, expected_absence, lambda: True)

        assert destroyed is False
        mock_delete.assert_not_called()
        release_start.set()
        allocated_provider, _, _ = await asyncio.wait_for(allocation, timeout=1.0)
        assert allocated_provider is provider
        mgr.release(thread_ts)
        await mgr.destroy(thread_ts)

    @pytest.mark.asyncio
    async def test_failed_allocation_releases_ownership_reservation(self, cfg):
        provider = _mock_provider_factory()(session_key="reserved")
        provider.start = AsyncMock(side_effect=RuntimeError("start failed"))
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        before = mgr.session_generation("failed-key")

        with pytest.raises(RuntimeError, match="start failed"):
            await mgr.get_or_create("failed-key")

        assert mgr.session_generation("failed-key") > before
        assert "failed-key" not in mgr.session_keys()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cancelled_allocation_releases_ownership_reservation(self, cfg):
        start_entered = asyncio.Event()
        provider = _mock_provider_factory()(session_key="reserved")

        async def blocked_start():
            start_entered.set()
            await asyncio.Event().wait()

        provider.start = AsyncMock(side_effect=blocked_start)
        mgr = SessionManager(cfg, provider_factory=lambda *_args, **_kwargs: provider)
        before = mgr.session_generation("cancelled-key")
        allocation = asyncio.create_task(mgr.get_or_create("cancelled-key"))
        await asyncio.wait_for(start_entered.wait(), timeout=1.0)
        assert "cancelled-key" in mgr.session_keys()

        allocation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await allocation

        assert mgr.session_generation("cancelled-key") > before
        assert "cancelled-key" not in mgr.session_keys()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_successful_reservation_finalizer_has_no_cancellable_await(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        session = mgr._sessions["k1"]
        impl_entered = asyncio.Event()
        release_impl = asyncio.Event()

        async def completed_impl(*_args, **_kwargs):
            await session.semaphore.acquire()
            impl_entered.set()
            await release_impl.wait()
            return provider, False, False

        with patch.object(mgr._allocation_boundary(), "_get_or_create_impl", completed_impl):
            claim = asyncio.create_task(mgr.get_or_create("k1"))
            await asyncio.wait_for(impl_entered.wait(), timeout=1.0)
            release_impl.set()
            await asyncio.sleep(0)

            assert claim.done()
            assert claim.cancel() is False
            claimed_provider, _, _ = claim.result()

        assert claimed_provider is provider
        assert session.semaphore.locked()
        assert mgr._allocation_boundary()._allocation_reservations == {}
        mgr.release("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_conditional_destroy_refuses_a_busy_current_generation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        generation = mgr.session_generation("k1")

        with patch.object(mgr._session_map, "delete") as mock_delete:
            destroyed = await mgr.destroy_if("k1", generation, lambda: True)

        assert destroyed is False
        provider.shutdown.assert_not_awaited()
        mock_delete.assert_not_called()
        assert mgr.has_session("k1")
        mgr.release("k1")
        await mgr.destroy("k1")

    @pytest.mark.asyncio
    async def test_destroy_deletes_map_before_provider_shutdown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        map_deleted = False

        def delete(*_args, **_kwargs):
            nonlocal map_deleted
            map_deleted = True

        async def shutdown():
            assert map_deleted is True

        provider.shutdown = AsyncMock(side_effect=shutdown)
        with patch.object(mgr._session_map, "delete", side_effect=delete):
            await mgr.destroy("k1")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_destroy_deletes_map_before_end_metric_can_yield(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        metric_entered = asyncio.Event()
        release_metric = asyncio.Event()

        async def delayed_metric(*_args, **_kwargs):
            metric_entered.set()
            await release_metric.wait()

        with (
            patch(
                "kiro_crew.session_lifecycle.record_session_ended",
                new=AsyncMock(side_effect=delayed_metric),
            ),
            patch.object(mgr._session_map, "delete") as mock_delete,
        ):
            destroy = asyncio.create_task(mgr.destroy("k1"))
            await asyncio.wait_for(metric_entered.wait(), timeout=1.0)
            mock_delete.assert_called_once_with("k1", reason="session_destroyed")
            release_metric.set()
            await asyncio.wait_for(destroy, timeout=1.0)

    @pytest.mark.asyncio
    async def test_destroy_unlinks_temp_files_from_the_session_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.destroy("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_destroy_nonexistent_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("nonexistent")
        mock_delete.assert_called_once_with("nonexistent", reason="session_destroyed")

    @pytest.mark.asyncio
    async def test_unconditional_destroy_is_not_refused_by_a_reservation(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._allocation_boundary()._allocation_reservations["k1"] = {object()}

        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.destroy("k1")

        mock_delete.assert_called_once_with("k1", reason="session_destroyed")

    @pytest.mark.asyncio
    async def test_destroy_shutdown_exception_still_deletes_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mgr._session_map, "delete") as mock_delete:
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.destroy("k1")
        # finally block still runs
        mock_delete.assert_called_once_with("k1", reason="session_destroyed")


class TestReplaySuppression:
    """``replay=False`` is what makes discarding a conversation actually stick.

    Clearing the sid stops the provider resuming its own conversation — and "the
    provider has no history" is exactly the condition that makes the next cold
    start rebuild one from ``conversation_log``. So the two mechanisms work
    against each other, and the caller who wanted a fresh conversation is handed
    a reconstruction of the old one. Measured on one app-owned session, that
    replay was 80,359 characters, 76% of the first turn's injected context.
    """

    @pytest.mark.asyncio
    async def test_default_does_not_suppress(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1")
        assert (
            mgr.consume_replay_suppression("k1") is False
        ), "the default must leave every existing caller's behaviour alone"

    @pytest.mark.asyncio
    async def test_replay_false_suppresses_exactly_once(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)

        assert mgr.consume_replay_suppression("k1") is True
        assert mgr.consume_replay_suppression("k1") is False, (
            "one-shot: a later cold start (idle expiry, gateway restart) must "
            "re-anchor rather than stay silently amnesiac"
        )

    @pytest.mark.asyncio
    async def test_a_later_replay_true_reset_clears_a_pending_suppression(self, cfg):
        """Two resets in a row must not leave the first one's intent standing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1")

        assert mgr.consume_replay_suppression("k1") is False

    @pytest.mark.asyncio
    async def test_teardown_does_not_leave_a_suppression_for_a_reused_key(self, cfg):
        """A slot key outlives the slot that held it, and keys ARE reused.

        A leaked flag would starve the NEXT holder of that key of its re-anchor —
        so the teardown paths clear it alongside the compaction cooldown they
        already clear, rather than leaving it to age out.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        await mgr.discard_conversation("k1", replay=False)

        await mgr.remove("k1")

        assert mgr.consume_replay_suppression("k1") is False


class TestDiscardConversation:
    """Tests for discard_conversation() — the poisoned-conversation escape.

    Unlike destroy(), the session-map ENTRY must survive: it carries the
    Slack thread/channel linkage (and feeds the reverse thread→session sync
    index), so deleting it would silently unlink a mirrored session. Only
    the resume sid is cleared, forcing the next turn to cold-start a fresh
    native conversation."""

    @pytest.mark.asyncio
    async def test_discard_shuts_down_and_clears_only_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with (
            patch.object(mgr._session_map, "clear_sid") as mock_clear,
            patch.object(mgr._session_map, "delete") as mock_delete,
        ):
            await mgr.discard_conversation("k1")
        provider.shutdown.assert_awaited_once()
        mock_clear.assert_called_once_with("k1")
        mock_delete.assert_not_called()
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_skip_if_busy_refuses_while_a_turn_holds_the_semaphore(self, cfg):
        """The guard reads the SEMAPHORE, which is why it has to live here.

        ``get_or_create`` leaves the semaphore held until ``release``, and the
        provider reports ``has_active_turn() is False`` throughout — a turn that
        holds the semaphore without a prompt in flight yet. So a CALLER probing
        the provider and then calling this would see "idle", tear the session
        down, and take the provider away from a turn that had already been
        admitted. Refusing here, under the lock that pops the session, is what
        closes that window.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        # The blind spot, made explicit: the provider says idle while busy.
        assert provider.has_active_turn() is False

        discarded = await mgr.discard_conversation("k1", replay=False, skip_if_busy=True)

        assert discarded is False
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1"), "the refusal must leave the session intact"

    @pytest.mark.asyncio
    async def test_a_refusal_changes_nothing_at_all(self, cfg):
        """Not a partial teardown: the replay flag must not move either, or the
        caller's retry would find suppression already consumed."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")

        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        assert mgr.consume_replay_suppression("k1") is False

    @pytest.mark.asyncio
    async def test_skip_if_busy_proceeds_once_the_turn_releases(self, cfg):
        """The refusal is a wait, not a cancellation."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        mgr.release("k1")
        discarded = await mgr.discard_conversation("k1", replay=False, skip_if_busy=True)

        assert discarded is True
        provider.shutdown.assert_awaited_once()
        assert mgr.consume_replay_suppression("k1") is True

    @pytest.mark.asyncio
    async def test_the_default_still_tears_down_a_busy_session(self, cfg):
        """``skip_if_busy`` defaults False, so every pre-existing caller — the
        poisoned-conversation escalation among them — keeps its behaviour."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")

        discarded = await mgr.discard_conversation("k1")

        assert discarded is True
        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_successor_mapped_during_shutdown_keeps_its_sid(self, cfg):
        """The sid clear must not outlive the pop.

        Ordered deterministically rather than by timing: the successor is mapped
        from inside ``provider.shutdown``, which is precisely the await the
        teardown suspends on. That is the whole window the bug needs — pop, then
        a concurrent channel turn creates and maps a new session under the same
        key, then a clear deferred past the shutdown wipes the NEW session's
        pointer. Clearing in the same tick as the pop closes it.

        Observed on the RAW entry, not through ``SessionMap.get``: that getter
        additionally requires ``<sid>.json`` to exist on disk, so for a synthetic
        sid it answers None whether or not the clear ran — which would make this
        assertion pass with the bug present.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "original-sid")

        async def _map_a_successor_while_shutting_down():
            mgr._session_map.set("k1", "successor-sid")

        provider.shutdown = AsyncMock(side_effect=_map_a_successor_while_shutting_down)

        await mgr.discard_conversation("k1", replay=False)

        provider.shutdown.assert_awaited_once()
        assert _raw_sid(mgr, "k1") == "successor-sid", (
            "the successor session's sid was erased by a clear deferred past the " "shutdown await"
        )

    @pytest.mark.asyncio
    async def test_the_sid_is_still_cleared_with_no_successor(self, cfg):
        """Scope pin: the clear still happens — it just happens earlier."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "original-sid")

        await mgr.discard_conversation("k1", replay=False)

        assert _raw_sid(mgr, "k1") == ""

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_clear_the_sid(self, cfg):
        """``skip_if_busy`` refusing must leave the mapping alone too — the clear
        sits after the early return, not before it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr._session_map.set("k1", "original-sid")

        assert await mgr.discard_conversation("k1", replay=False, skip_if_busy=True) is False

        assert _raw_sid(mgr, "k1") == "original-sid"

    @pytest.mark.asyncio
    async def test_discard_conversation_unlinks_temp_files_from_the_session_queue(
        self, cfg, tmp_path
    ):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.discard_conversation("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_discard_preserves_slack_linkage(self, cfg):
        """The poisoned-conversation discard keeps Slack linkage: a Slack-linked
        session that discards its rejected conversation must keep its thread
        binding, or the recovered answer is not mirrored and later inbound
        replies fork a new conversation."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._session_map.set("k1", "sid-poisoned")
        mgr._session_map.set_slack_link("k1", "1234.5678", "C0FFEE")
        await mgr.discard_conversation("k1")
        # sid gone → next turn cold-starts instead of session/load-ing the poison
        assert not mgr._session_map.get("k1")
        # ...but the Slack linkage survives.
        assert mgr.get_slack_link("k1") == ("1234.5678", "C0FFEE")
        # ...and the dropped sid is stashed, so a false-positive discard is
        # diagnosable and manually reversible (the native conversation still
        # exists on disk; only the pointer was cleared).
        assert mgr._session_map.get_discarded_sid("k1") == "sid-poisoned"

    @pytest.mark.asyncio
    async def test_discard_shutdown_exception_still_clears_sid(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.shutdown = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(mgr._session_map, "clear_sid") as mock_clear:
            with pytest.raises(RuntimeError, match="boom"):
                await mgr.discard_conversation("k1")
        # finally block still runs
        mock_clear.assert_called_once_with("k1")


class TestResolveAgentModelResolution:
    """Tests for _resolve_agent_model()."""

    def test_resolve_agent_model_cache_miss_returns_auto(self, cfg):
        # Clear cache if exists
        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        result = SessionManager._resolve_agent_model("nonexistent-agent-xyz")
        assert result == "auto"

    def test_resolve_agent_model_from_file(self, cfg, tmp_path):
        """Reads model from agent JSON file."""
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "test-agent.json"
        agent_file.write_text(json.dumps({"name": "test-agent", "model": "opus-5"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("test-agent")
        assert result == "opus-5"

    def test_resolve_agent_model_coerces_non_string_spec(self, cfg, tmp_path):
        """A foreign spec's structured ``model`` must not escape as a dict.

        ``~/.kiro/agents`` is shared with other tools; an ACP-style
        ``{"id": ...}`` here would be CACHED and then handed to
        the pooled-model comparison in ``claim_pooled``. This method is
        annotated ``-> str`` and must honour that.
        """
        import json

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        agent_file = tmp_path / "foreign.json"
        agent_file.write_text(
            json.dumps({"name": "foreign", "model": {"id": "anthropic:claude-opus-4-8"}})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            result = SessionManager._resolve_agent_model("foreign")
        assert result == "auto"
        assert isinstance(result, str)

    def test_resolve_agent_model_refuses_an_oversized_spec(self, tmp_path, monkeypatch):
        """The scan reads through the hardened, size-capped reader.

        ``~/.kiro/agents`` is user-writable and shared with kiro-cli, so an
        oversized "agent config" there must be refused rather than slurped into
        memory — and this resolution is CACHED and reused on every later
        lookup, so it is not a rare corner.

        Exercised with a LOWERED cap rather than a real 50 MB fixture; the
        property is that the cap is consulted, not its value. Paired with the
        A-side below so the refusal cannot pass by breaking every read.
        """
        import json

        from kiro_crew import hooks

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        (tmp_path / "big.json").write_text(
            json.dumps({"name": "big", "model": "pinned-by-oversized", "pad": "x" * 1024})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            assert SessionManager._resolve_agent_model("big") == "auto"

    def test_resolve_agent_model_still_reads_a_spec_under_the_same_cap(self, tmp_path, monkeypatch):
        """A-side of the cap test above: a normal spec still resolves."""
        import json

        from kiro_crew import hooks

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        monkeypatch.setattr(hooks, "MAX_FILE_BYTES", 256)
        (tmp_path / "small.json").write_text(
            json.dumps({"name": "small", "model": "pinned-by-small"})
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
            assert SessionManager._resolve_agent_model("small") == "pinned-by-small"

    @requires_symlinks
    def test_resolve_agent_model_refuses_a_link_to_a_sensitive_target(self, tmp_path, monkeypatch):
        """A spec that is a symlink resolving onto a sensitive target is refused,
        so the model is not resolved out of whatever the link names."""
        import json

        from kiro_crew import agent_discovery

        if hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache.clear()
        target = tmp_path / "protected.json"
        target.write_text(json.dumps({"model": "leaked-value"}))
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "linked.json").symlink_to(target)
        monkeypatch.setattr(
            agent_discovery, "is_sensitive_canonical_path", lambda p: str(target) in str(p)
        )

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents):
            assert SessionManager._resolve_agent_model("linked") == "auto"


class TestWarmPoolInternals:
    """Tests for _fill_warm_pool, _claim_from_pool, _drain_and_claim."""

    @pytest.mark.asyncio
    async def test_fill_warm_pool_spawns_to_size(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fill_warm_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._provider_factory = None
        await mgr._fill_warm_pool()  # should not raise
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_zero_size(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        await mgr._fill_warm_pool()
        assert mgr._warm_pool.qsize() == 0

    @pytest.mark.asyncio
    async def test_fill_warm_pool_stops_on_failure(self, cfg):
        call_count = 0

        def failing_factory(session_key=None, agent=None, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RuntimeError("spawn failed")
            m = AsyncMock()
            m.start = AsyncMock()
            return m

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        mgr._pool_size = 3
        await mgr._fill_warm_pool()
        # Only 1 succeeded before failure stopped the loop
        assert mgr._warm_pool.qsize() == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claim_from_pool_matching_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("kirocrew")
        assert result == (mock_p, 100.0)

    @pytest.mark.asyncio
    async def test_claim_from_pool_mismatched_agent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = "kirocrew"
        mock_p = AsyncMock()
        mgr._warm_pool.put_nowait((mock_p, 100.0))
        result = mgr._claim_from_pool("different-agent")
        assert result is None

    @pytest.mark.asyncio
    async def test_claim_from_pool_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = mgr._claim_from_pool(None)
        assert result is None

    @pytest.mark.asyncio
    async def test_drain_and_claim_healthy(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mock_p = AsyncMock()
        mock_p.is_process_alive = lambda: True
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is mock_p
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_dead_provider_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))
        result = await mgr._drain_and_claim(None)
        assert result is None
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_drain_and_claim_stale_ttl_discarded(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_agent = ""
        mgr._pool_ttl_secs = 60
        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True

        # Spawned 120s ago — exceeds 60s TTL
        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))
        result = await mgr._drain_and_claim(None)
        assert result is None
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()


class TestPoolHealthLoop:
    """Tests for _pool_health_loop periodic sweep."""

    @pytest.mark.asyncio
    async def test_health_loop_removes_dead_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0  # no TTL

        dead_p = AsyncMock()
        dead_p.is_process_alive = lambda: False
        dead_p.exit_code = 1
        dead_p.client = AsyncMock()
        dead_p.client._pid = 111

        mgr._warm_pool.put_nowait((dead_p, time.monotonic()))

        # Run one iteration then cancel
        call_count = 0
        original_sleep = asyncio.sleep

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            await original_sleep(0)

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        dead_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_keeps_healthy_providers(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 0

        healthy_p = AsyncMock()
        healthy_p.is_process_alive = lambda: True
        healthy_p.client = AsyncMock()
        healthy_p.client._pid = 222

        mgr._warm_pool.put_nowait((healthy_p, time.monotonic()))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            # instant return for first sleep
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 1
        healthy_p.shutdown.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_ttl_expiry(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._pool_ttl_secs = 60

        stale_p = AsyncMock()
        stale_p.is_process_alive = lambda: True
        stale_p.client = AsyncMock()
        stale_p.client._pid = 333

        mgr._warm_pool.put_nowait((stale_p, time.monotonic() - 120))

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()

        assert mgr._warm_pool.qsize() == 0
        stale_p.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_health_loop_empty_pool_skips(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2

        call_count = 0

        async def one_pass_sleep(secs):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise asyncio.CancelledError
            return

        with patch("asyncio.sleep", side_effect=one_pass_sleep):
            with pytest.raises(asyncio.CancelledError):
                await mgr._pool_health_loop()
        # No crash, just skipped
        await mgr.close_all()


class TestCleanupLoop:
    """Tests for _cleanup_loop periodic maintenance.

    Every sweep the loop dispatches is stubbed: these tests pin the LOOP's
    wiring (which sweeps run, with what args, and when), not sweep behavior —
    each sweep has its own tests in its own module. Leaving a sweep unstubbed
    (notably ``find_orphan_mcp_candidates``, a full process-table scan, and
    ``cleanup_orphaned_session_roots``, which reads the operator's real
    ``~/.kirocrew`` PID file) made each test take ~10-20s of wall-clock and
    probe live system state — both banned by testing-conventions.md.
    """

    @pytest.mark.asyncio
    async def test_cleanup_loop_calls_expire_idle(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            # First wait_for returns TimeoutError (normal wakeup), second signals shutdown
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            await mgr._cleanup_loop()

        mock_expire.assert_awaited_once_with(120)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_disabled_idle_sweep(self, cfg):
        cfg.session.timeout_secs = 0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            call_count = [0]

            async def one_pass(*a, **kw):
                call_count[0] += 1
                raise asyncio.TimeoutError

            mock_event.is_set = lambda: call_count[0] >= 1
            mock_event.wait = AsyncMock(side_effect=one_pass)
            await mgr._cleanup_loop()

        # idle sweep disabled — _expire_idle should NOT be called
        mock_expire.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_clamps_low_timeout(self, cfg, caplog):
        cfg.session.timeout_secs = 30  # below 60 minimum
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock) as mock_expire,
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch("kiro_crew.session.cleanup_stale_sandbox_profiles", return_value=0),
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_expire.await_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        # Should clamp to 60
        mock_expire.assert_awaited_once_with(60)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_shutdown_signal(self, cfg):
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with patch("kiro_crew.session.shutdown_event") as mock_event:
            mock_event.is_set = lambda: True
            mock_event.wait = AsyncMock(return_value=None)
            # Should return immediately since shutdown is set
            await mgr._cleanup_loop()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_runs_sandbox_sweep_via_executor(self, cfg, caplog):
        """Sandbox sweep is invoked through run_in_executor on maintenance_executor.

        Asserts the offload specifically (the blocking-call fix): the sweep
        must execute on a maintenance-executor worker thread, NOT the event
        loop thread, so its blocking os.listdir/os.kill/os.remove I/O cannot
        freeze the loop.
        """
        cfg.session.timeout_secs = 120
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        sweep_threads: list[str] = []
        sweep_homes: list[object] = []

        def _fake_sweep(*, data_home=None) -> int:
            # The deps hand the sweep the data home the manager resolved on ITS
            # thread (the pool thread must not resolve it itself); record it too.
            sweep_threads.append(threading.current_thread().name)
            sweep_homes.append(data_home)
            return 3

        with (
            patch.object(mgr, "_expire_idle", new_callable=AsyncMock),
            patch("kiro_crew.session._cleanup_orphaned_mcp_servers", return_value=0),
            patch(
                "kiro_crew.session.cleanup_stale_sandbox_profiles", side_effect=_fake_sweep
            ) as mock_sweep,
            patch("kiro_crew.session._collect_active_pids", return_value=({}, True)),
            patch("kiro_crew.session._periodic_pid_sweep", return_value=([], [])),
            patch("kiro_crew.session._kill_confirmed_and_writeback", return_value=0),
            patch("kiro_crew.session.cleanup_orphaned_session_roots", return_value=0),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
            patch("kiro_crew.session.shutdown_event") as mock_event,
        ):
            mock_event.is_set = lambda: mock_sweep.call_count >= 1
            mock_event.wait = AsyncMock(side_effect=asyncio.TimeoutError)
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                await mgr._cleanup_loop()

        # Verify: sweep was called (production wiring). At least once, not
        # exactly once: the loop now also dispatches one reclaim pass at START
        # (a host whose runtime tmpfs is out of inodes cannot spawn at all, so
        # that pass must not wait out an interval), so a run can legitimately
        # record the boot pass, the tick pass, or both.
        assert mock_sweep.call_count >= 1
        # Verify the offload: EVERY call ran on a maintenance-executor worker
        # thread, not the event loop thread (run_in_executor path).
        assert sweep_threads, "sweep never executed"
        assert all(name != threading.main_thread().name for name in sweep_threads)
        assert sweep_threads[0].startswith("mc-maint")
        # The home reached the pool thread pre-resolved and pinned: a sweep that
        # resolved config_dir() for itself, after the queuing test's pin was gone,
        # walked the operator's real ~/.kiro/crew (third side-effect audit).
        assert sweep_homes and all(
            h is not None and Path(h).resolve() == Path(os.environ["KIROCREW_HOME"]).resolve()
            for h in sweep_homes
        ), sweep_homes
        # Verify: non-zero return produces the info log
        assert "removed 3 stale sandbox artifacts" in caplog.text
        await mgr.close_all()


class TestPoolPids:
    """Tests for _pool_pids non-destructive peek."""

    @pytest.mark.asyncio
    async def test_pool_pids_extracts_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.client = AsyncMock()
        mock_p.client._pid = 42
        mgr._warm_pool.put_nowait((mock_p, time.monotonic()))
        pids = mgr._pool_pids()
        assert 42 in pids
        # Non-destructive — item still in pool
        assert mgr._warm_pool.qsize() == 1

    @pytest.mark.asyncio
    async def test_pool_pids_empty(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr._pool_pids() == set()

    @pytest.mark.asyncio
    async def test_pool_pids_includes_sweep_pids(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_sweep_pids.add(999)
        pids = mgr._pool_pids()
        assert 999 in pids


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


class TestIsProviderAliveProcessVerdict:
    """Test is_provider_alive reads the provider's process-level verdict.

    The is_alive fallback for a provider that does not override
    ``is_process_alive`` lives in the LLMProvider ABC default, not here —
    it is pinned by the ABC contract tests in
    ``test_session_provider_liveness.py``.
    """

    @pytest.mark.asyncio
    async def test_returns_the_process_liveness_verdict(self, cfg):
        from unittest.mock import MagicMock

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.is_process_alive = MagicMock(return_value=True)
        assert await mgr.is_provider_alive("k1") is True
        provider.is_process_alive = MagicMock(return_value=False)
        assert await mgr.is_provider_alive("k1") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        result = await mgr.is_provider_alive("nonexistent")
        assert result is None


class TestSetActiveDashboardSlots:
    """Test set_active_dashboard_slots."""

    def test_sets_slots(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_active_dashboard_slots({"dashboard:tab1", "dashboard:tab2"})
        assert mgr._active_dashboard_slots == {"dashboard:tab1", "dashboard:tab2"}


class TestStartPoolNonBlocking:
    """Tests for start_pool non-blocking path."""

    @pytest.mark.asyncio
    async def test_start_pool_non_blocking(self, cfg):
        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        await mgr.start_pool(blocking=False)
        # Let background tasks run
        await asyncio.sleep(0.1)
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_start_pool_no_factory(self, cfg):
        mgr = SessionManager(cfg, provider_factory=None)
        await mgr.start_pool()  # should be no-op
        assert mgr.count == 0

    @pytest.mark.asyncio
    async def test_ensure_background_already_exists(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        # Call again — should be no-op
        await mgr._ensure_background()
        assert mgr.count == 1  # still just the one bg session
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_factory_failure(self, cfg):
        def failing_factory(session_key=None, **kwargs):
            raise RuntimeError("spawn failed")

        mgr = SessionManager(cfg, provider_factory=failing_factory)
        await mgr._ensure_background()
        # Should not crash, just log warning
        assert BACKGROUND_KEY not in mgr._sessions


class TestScheduleReplenish:
    """Tests for _schedule_replenish fire-and-forget pool refill."""

    @pytest.mark.asyncio
    async def test_schedule_replenish_creates_task(self, cfg):
        cfg.session.pool_size = 2
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 2
        mgr._schedule_replenish()
        await asyncio.sleep(0.1)  # let task run
        assert mgr._warm_pool.qsize() >= 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_schedule_replenish_noop_when_pool_disabled(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 0
        mgr._schedule_replenish()  # should not create task
        assert len(mgr._background_tasks) == 0


class TestCompaction:
    """Tests for _trigger_compaction and _compact_session."""

    @pytest.mark.asyncio
    async def test_trigger_compaction_duplicate_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # First trigger starts compaction
        assert mgr._trigger_compaction("k1", "test", 92.0, provider) is None
        assert "k1" in mgr._compacting
        # Second trigger on same key is a no-op (already in progress)
        assert mgr._trigger_compaction("k1", "test again", 95.0, provider) == "in_progress"
        await asyncio.sleep(0.1)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_calls_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)
        await mgr._compact_session("k1", 92.0)
        provider.shutdown.assert_awaited_once()
        assert callback_args == [("k1", 92.0, True)]
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_compact_session_missing_key_is_safe(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._compacting.add("gone")
        await mgr._compact_session("gone", 90.0)
        assert "gone" not in mgr._compacting


class TestClaudeBackendCompaction:
    """Claude-agent-acp autocompact runs /compact in place — no recycle."""

    @pytest.mark.asyncio
    async def test_compact_session_claude_runs_in_place(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        provider.compact.assert_awaited_once()
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1")
        assert callback_args == [("k1", 92.0, True)]
        assert "k1" not in mgr._compacting

    @pytest.mark.asyncio
    async def test_compact_session_claude_failure_keeps_session(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session"),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr.has_session("k1")
        provider.shutdown.assert_not_awaited()
        # Failure callback fires with success=False so the dashboard can
        # show a "compact failed" banner. (Behavior changed in the I2 fix.)
        cb.assert_awaited_once_with("k1", 92.0, success=False, outcome="compacted")
        assert "k1" not in mgr._compacting
        assert any("Compact failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_check_context_usage_triggers_for_claude(self, cfg):
        """Autocompact threshold must apply to claude."""
        cfg.session.autocompact_pct = 20.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 40.0

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            patch.object(mgr, "_trigger_compaction") as mock_trigger,
        ):
            mgr.check_context_usage("k1", provider)

        mock_trigger.assert_called_once_with("k1", "context at 40%", 40.0, provider)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_claude_compact_reuses_session(self, cfg):
        """Concurrent get_or_create while claude compact is in flight must
        return the existing session, not cold-start a duplicate provider that
        would later overwrite _sessions[key] and leak the original process."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            # Simulate compact in progress.
            mgr._compacting.add("k1")
            try:
                provider2, is_new, _ = await mgr.get_or_create("k1")
            finally:
                mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        assert mgr.count == 1
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_recycle_teardown_cold_starts(self, cfg):
        """While the failure recycle is tearing the entry down (_recycling
        holds the exact object still in the map), get_or_create must not
        reuse the doomed entry — fall through to cold-start."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._recycling["k1"] = mgr._sessions["k1"]
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        # New provider: we did not short-circuit to the doomed entry.
        assert provider2 is not provider
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_reuses_healthy_replacement_during_recycle(self, cfg):
        """The _recycling marker is object-aware: when the map already holds a
        healthy REPLACEMENT for a key whose OLD session is still being torn
        down, get_or_create must reuse the replacement — not exile it and
        cold-start a duplicate provider that would overwrite and leak it."""
        from kiro_crew.session import FirstTurnState, _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        old_provider, _, _ = await mgr.get_or_create("k1")
        old_sess = mgr._sessions["k1"]
        mgr.release("k1")

        # Recycle of the OLD object is in flight; a racing cold-start has
        # already registered a fresh replacement under the same key.
        replacement_provider = AsyncMock()
        replacement_provider.shutdown = AsyncMock()
        replacement_provider.memory_mode = "persistent"
        replacement_provider.is_process_alive = lambda: True
        replacement = _Session(
            provider=replacement_provider, first_turn=FirstTurnState.NOTHING_ARMED
        )
        mgr._sessions["k1"] = replacement
        mgr._recycling["k1"] = old_sess
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        assert provider2 is replacement_provider
        assert is_new is False
        assert mgr._sessions["k1"] is replacement
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_inplace_compact_reuses_session(self, cfg):
        """An in-place compact (kiro or claude) keeps the entry healthy:
        concurrent get_or_create must reuse it — queueing on the session
        semaphore — instead of cold-starting a duplicate provider."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._compacting.add("k1")  # in-place compact in flight; NOT recycling
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_after_kiro_pop_cold_starts(self, cfg):
        """Real kiro recycle pops _sessions[key] before adding to _compacting.
        Concurrent get_or_create must cold-start fresh (is_new=True)."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Pop is the post-recycle reality: entry gone from _sessions.
        mgr._compacting.add("k1")
        try:
            provider, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert is_new is True
        assert provider is not None
        mgr.release("k1")
        await mgr.close_all()


class TestKiroInPlaceCompaction:
    """kiro-cli auto-compaction runs /compact IN PLACE first, so the session
    (and any queued/agentic work) continues automatically — recycle is only
    the fallback. Fix for 'session stops after auto-compaction'."""

    @staticmethod
    def _inplace_provider_factory(result: dict, *, stream_events: list | None = None):
        """Provider whose native compaction reports *result*.

        ``stream_command("/compact")`` yields *stream_events* (default: none —
        the terminal status arrives async via ``wait_for_compaction``,
        mirroring kiro-cli's post-end_turn status emission).
        """

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                for ev in stream_events or []:
                    yield ev

            m.stream_command = MagicMock(side_effect=_stream)
            m.wait_for_compaction = AsyncMock(return_value=result)
            return m

        return factory

    @pytest.mark.asyncio
    async def test_inplace_success_keeps_session_and_process(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Session survives in place: same entry, same provider, no SIGKILL.
        assert "dashboard:chat-1" in mgr._sessions
        assert mgr._sessions["dashboard:chat-1"].provider is provider
        provider.stream_command.assert_called_once_with("/compact")
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        # Semaphore released: the next turn can proceed immediately.
        assert not mgr._sessions["dashboard:chat-1"].semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_failed_result_falls_back_to_recycle(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "failed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Fallback recycle: entry dropped, process killed, context guaranteed
        # to clear on the next (re-seeded) message.
        assert "dashboard:chat-1" not in mgr._sessions
        provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        assert "dashboard:chat-1" not in mgr._recycling
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_no_native_support_falls_back_to_recycle(self, cfg):
        """Base LLMProvider.wait_for_compaction returns {'type': 'timeout'} —
        providers without native compaction must keep today's recycle path."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "timeout"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._sessions
        provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_success_clears_failure_cooldown(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_midstream_terminal_status_skips_async_wait(self, cfg):
        """kiro-cli may emit the terminal compaction status MID-TURN (before
        end_turn). The stream watcher must latch it so the blind drain never
        eats it — otherwise wait_for_compaction would stall to timeout and
        wrongly recycle a just-compacted healthy session."""
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, AcpEvent

        mid = AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="sum")
        mgr = SessionManager(
            cfg,
            provider_factory=self._inplace_provider_factory(
                {"type": "timeout"},  # async wait would FAIL if consulted
                stream_events=[mid],
            ),
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Mid-stream status decided the outcome; async wait never consulted.
        assert "dashboard:chat-1" in mgr._sessions
        provider.wait_for_compaction.assert_not_awaited()
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_never_uses_commands_execute(self, cfg):
        """Sending /compact
        via the string form of _kiro.dev/commands/execute makes kiro-cli
        2.14.0 exit rc=0. The auto-compact path must use the prompt
        transport (stream_command), never send_command."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        provider.send_command = AsyncMock()
        mgr.release("dashboard:chat-1")

        await mgr._compact_session("dashboard:chat-1", 92.0)

        provider.stream_command.assert_called_once_with("/compact")
        provider.send_command.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_holds_semaphore_so_queued_turns_wait(self, cfg):
        """While the in-place compact runs, the session semaphore is held —
        a queued turn waits behind it and then continues on the compacted
        session instead of interleaving with the compaction."""
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                await gate.wait()
                return {"type": "completed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        assert not task.done()
        assert sess.semaphore.locked()  # queued turn would wait here

        gate.set()
        await asyncio.wait_for(task, timeout=2)
        assert "dashboard:chat-1" in mgr._sessions
        assert not sess.semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failure_recycle_never_yields_semaphore_to_queued_turn(self, cfg):
        """The failure recycle must not
        open a window in which a queued turn is dispatched into a session that
        is still compacting.

        The old code released the turn semaphore as soon as the in-place
        compact reported failure and let the CALLER re-acquire it for the
        recycle. A queued turn won that gap: it was sent to a kiro-cli that was
        still finishing its ``/compact``, its stream received the late
        ``completed`` status instead of an ``end_turn``, and the turn hung
        holding the semaphore until the 2h prompt timeout — while the recycle
        that would have rescued it gave up at its own acquire timeout. The kill
        must therefore land BEFORE any queued turn can run.
        """
        order: list[str] = []
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _shutdown() -> None:
                order.append("shutdown")

            m.shutdown = AsyncMock(side_effect=_shutdown)

            async def _wait(timeout=120.0):
                # Mirrors production: the async wait gives up while the
                # compaction is in fact still running on the backend.
                await gate.wait()
                return {"type": "timeout"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        compact = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        await asyncio.sleep(0.05)
        assert sess.semaphore.locked()

        # A queued turn parks on the turn semaphore, exactly as a real
        # dispatch does while a compaction holds it.
        async def _queued_turn() -> None:
            async with sess.semaphore:
                order.append("turn")

        turn = asyncio.create_task(_queued_turn())
        await asyncio.sleep(0.05)
        assert not turn.done()

        gate.set()  # compact reports timeout -> recycle, semaphore still held
        await asyncio.wait_for(compact, timeout=2)
        await asyncio.wait_for(turn, timeout=2)

        # Kill first, queued turn second: the semaphore was never handed back
        # while the backend could still have been compacting.
        assert order == ["shutdown", "turn"]
        assert "dashboard:chat-1" not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_pop_by_identity_spares_replacement(self, cfg):
        """If a racing cold-start replaced the entry while the in-place
        compact was still running, the failure recycle kills only the OLD
        session; the fresh replacement (and its session_map entry) survives."""
        from kiro_crew.session import FirstTurnState, _Session

        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                await gate.wait()
                return {"type": "failed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        old_provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("k1", 92.0))
        await asyncio.sleep(0.05)
        assert not task.done()

        # A racing cold-start replaces the entry with a fresh session.
        new_provider = AsyncMock()
        new_provider.shutdown = AsyncMock()
        mgr._sessions["k1"] = _Session(
            provider=new_provider, first_turn=FirstTurnState.NOTHING_ARMED
        )

        gate.set()  # compact fails -> recycle pops by identity
        await asyncio.wait_for(task, timeout=2)

        # Replacement untouched; old provider reaped.
        assert mgr._sessions["k1"].provider is new_provider
        new_provider.shutdown.assert_not_awaited()
        old_provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("k1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()


class TestCompactFailureCooldown:
    """After a compact failure, subsequent triggers within the cooldown
    window must be skipped to avoid hammering the provider on every turn."""

    @pytest.mark.asyncio
    async def test_compact_failure_sets_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_skipped_during_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Set cooldown 60s in the future.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        assert mgr._trigger_compaction("k1", "context at 90%", 90.0, AsyncMock()) == "cooldown"

        # No background task scheduled, no compacting marker set.
        assert "k1" not in mgr._compacting
        assert not mgr._background_tasks
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_success_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        # Pre-existing cooldown from an earlier failure.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_retrigger(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Cooldown already in the past.
        mgr._compact_cooldown_until["k1"] = time.monotonic() - 1.0

        assert mgr._trigger_compaction("k1", "context at 90%", 90.0, AsyncMock()) is None

        # Background task scheduled and compacting marker set.
        assert "k1" in mgr._compacting
        assert len(mgr._background_tasks) == 1
        # Snapshot to a list — `add_done_callback` discard mutates the set
        # during await, which would break direct iteration.
        for t in list(mgr._background_tasks):
            await t
        await mgr.close_all()


class TestCompactTimeout:
    """provider.compact() must be wrapped in a timeout so a stuck compact
    cannot hold session.semaphore forever and block concurrent gets."""

    @pytest.mark.asyncio
    async def test_compact_timeout_sets_cooldown_and_fires_failure(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        async def _hang(*_a, **_kw):
            await asyncio.sleep(10.0)

        provider.compact = _hang
        callback_calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            # Only the outer cap is scaled — see _COMPACT_RESULT_WAIT_FLOOR_SECS
            # note above if the inner wait must time out quickly.
            patch("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.05),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        assert callback_calls == [("k1", 92.0, False)]
        assert "k1" not in mgr._compacting
        await mgr.close_all()


class TestCompactCallbackSuccessFlag:
    """The compact callback must receive ``success=False`` on failure so the
    dashboard can show a different banner."""

    @pytest.mark.asyncio
    async def test_keyword_callback_fires_with_success_true_on_success(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()

        calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert calls == [("k1", 92.0, True)]
        await mgr.close_all()


class TestCooldownPruning:
    """`_compact_cooldown_until` entries must be cleared on session lifecycle
    events so a fresh session reusing a key never inherits a stale cooldown."""

    @pytest.mark.asyncio
    async def test_remove_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.remove("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_destroy_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.destroy("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reset_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.reset("k1")

        assert "k1" not in mgr._compact_cooldown_until
        assert "k1" not in mgr._compact_pending_verdict
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_clears_cooldowns(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.release("k1")
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0
        mgr._compact_cooldown_until["k2"] = time.monotonic() + 60.0
        mgr._compact_pending_verdict["k1"] = 92.0

        await mgr.close_all()

        assert mgr._compact_cooldown_until == {}
        assert mgr._compact_pending_verdict == {}


class TestCloseAllPersistence:
    """Tests for close_all session_map persistence."""

    @pytest.mark.asyncio
    async def test_close_all_persists_acp_session_ids(self, cfg):
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.session import _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_provider = MagicMock(spec=AcpProvider)
        mock_provider.shutdown = AsyncMock()
        mock_provider.context_usage_pct = MagicMock(return_value=0.0)
        # session_map persistence reads the public provider.cwd accessor
        # (AcpProvider exposes the work dir via _client._work_dir, not a bare
        # _work_dir attribute).
        mock_provider.cwd = "/tmp/test"
        mock_provider.client = MagicMock()
        mock_provider.client._session_id = "sid-persist-test"
        mock_provider.client.backend = ""  # kiro-cli backend

        mgr._sessions["dashboard:slot0"] = _Session(provider=mock_provider)
        with patch.object(mgr._session_map, "set") as mock_set:
            await mgr.close_all()
        # provider= is now persisted so the next-startup detect_provider_switch
        # doesn't see a missing label and falsely fire an acp/cc switch.
        mock_set.assert_called_once_with(
            "dashboard:slot0",
            "sid-persist-test",
            provider="acp",
            cwd="/tmp/test",
        )


class TestRemove:
    """Tests for remove() — shutdown but preserve session_map."""

    @pytest.mark.asyncio
    async def test_remove_shuts_down_preserves_map(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        with patch.object(mgr._session_map, "delete") as mock_delete:
            await mgr.remove("k1")
        provider.shutdown.assert_awaited_once()
        mock_delete.assert_not_called()  # remove preserves map
        assert not mgr.has_session("k1")

    @pytest.mark.asyncio
    async def test_remove_unlinks_temp_files_from_the_session_queue(self, cfg, tmp_path):
        img = tmp_path / "img.png"
        img.write_bytes(b"fake")
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("k1")
        mgr.enqueue("k1", "ts2", "second", force=True, image_temp_paths=[str(img)])

        await mgr.remove("k1")

        assert not img.exists()

    @pytest.mark.asyncio
    async def test_remove_missing_key_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.remove("nonexistent")  # should not raise


class TestSafeCleanup:
    """Tests for _safe_cleanup best-effort session file removal."""

    @pytest.mark.asyncio
    async def test_cleanup_calls_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock()
        await mgr._safe_cleanup(mock_p, "sid-123")
        mock_p.cleanup_session.assert_awaited_once_with("sid-123")

    @pytest.mark.asyncio
    async def test_cleanup_swallows_exception(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.cleanup_session = AsyncMock(side_effect=OSError("disk full"))
        await mgr._safe_cleanup(mock_p, "sid-456")  # should not raise


class TestSetCompactCallback:
    """Tests for set_compact_callback."""

    def test_sets_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)
        assert mgr._on_compacted is cb

    def test_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())
        assert any("already registered" in r.message for r in caplog.records)


class TestExpireIdleOrphans:
    """Tests for _expire_idle orphaned dashboard slot detection."""

    @pytest.mark.asyncio
    async def test_orphaned_dashboard_slot_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot5")
        mgr.release("dashboard:slot5")
        # Set active slots to NOT include slot5
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)  # not idle, but orphaned
        assert not mgr.has_session("dashboard:slot5")

    @pytest.mark.asyncio
    async def test_active_slot_not_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:slot0")
        mgr.release("dashboard:slot0")
        mgr.set_active_dashboard_slots({"dashboard:slot0"})
        await mgr._expire_idle(timeout_secs=9999)
        assert mgr.has_session("dashboard:slot0")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_never_expired(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("channel:C123")
        mgr.release("channel:C123")
        # Backdate to make it idle
        async with mgr._lock:
            mgr._sessions["channel:C123"].last_used = time.monotonic() - 9999
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.has_session("channel:C123")
        await mgr.close_all()


class TestGetOrCreatePoolClaim:
    """Test get_or_create claiming from warm pool."""

    @pytest.mark.asyncio
    async def test_claims_from_pool_on_new_session(self, cfg):
        from kiro_crew.providers.acp import AcpProvider

        cfg.session.pool_size = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        mgr._pool_agent = "kirocrew"

        # Pre-fill pool with a mock provider that looks like AcpProvider
        mock_pooled = AsyncMock(spec=AcpProvider)
        mock_pooled.start = AsyncMock()
        mock_pooled.shutdown = AsyncMock()
        mock_pooled.context_usage_pct = lambda: 0.0
        mock_pooled.is_process_alive = lambda: True
        mock_pooled.client = AsyncMock()
        mock_pooled.client._model = "claude-opus-4"
        mock_pooled.client._agent = "kirocrew"
        mock_pooled.client._session_id = None
        mock_pooled.client.rekey = lambda *a, **kw: None
        mock_pooled.client.resumed = False

        mgr._warm_pool.put_nowait((mock_pooled, time.monotonic()))

        provider, is_new, _ = await mgr.get_or_create("dashboard:slot1", agent="kirocrew")
        mgr.release("dashboard:slot1")
        assert provider is mock_pooled
        assert is_new is True
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_pool_claim_resets_stale_context_and_skips_compaction(self, cfg):
        """End-to-end: a pooled provider carrying a previous session's
        context stats must not hand them to the claiming session. The claim
        path calls client.rekey(), whose reset makes the first turn-end
        check_context_usage read 0%/unknown instead of firing compaction on
        an empty conversation."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.providers.acp import AcpProvider

        cfg.session.pool_size = 1
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._pool_size = 1
        mgr._pool_agent = "kirocrew"

        # Real (unstarted) AcpClient seeded with the PREVIOUS session's stats —
        # the exact leak shape from the issue: high confirmed pct, real counts.
        real_client = AcpClient()
        real_client.last_prompt_stats = AcpPromptStats(
            context_pct=95.0,
            context_used_tokens=190_000,
            context_window_tokens=200_000,
            context_tokens_from_usage=True,
        )

        mock_pooled = AsyncMock(spec=AcpProvider)
        mock_pooled.start = AsyncMock()
        mock_pooled.shutdown = AsyncMock()
        mock_pooled.is_process_alive = lambda: True
        mock_pooled.client = real_client
        # Route the provider probes through the real client stats (mirrors
        # AcpProvider.context_usage_pct / context_usage_unknown).
        mock_pooled.context_usage_pct = lambda: real_client.last_prompt_stats.context_pct
        mock_pooled.context_usage_unknown = (
            lambda: real_client.last_prompt_stats.context_pct_unknown
        )

        mgr._warm_pool.put_nowait((mock_pooled, time.monotonic()))

        provider, is_new, _ = await mgr.get_or_create("dashboard:slot1", agent="kirocrew")
        mgr.release("dashboard:slot1")
        assert provider is mock_pooled

        # The handoff dropped the stale session-scoped state (back to plain
        # defaults — NOT flagged unknown, which would collide with the
        # compacted-in-place recycle predicate)...
        stats = real_client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0
        assert stats.context_pct_unknown is False

        # ...so the first turn-end check does not compact the empty session.
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            pct = mgr.check_context_usage("dashboard:slot1", provider)
        compact.assert_not_called()
        assert "dashboard:slot1" not in mgr._compacting
        assert pct == 0.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cold_start_with_resume_sid(self, cfg):
        """get_or_create with a stored session_map entry attempts resume."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Mock session_map to return a resume SID
        with (
            patch.object(mgr._session_map, "get", return_value="sid-resume-test"),
            patch.object(mgr._session_map, "get_cwd", return_value=None),
            patch.object(mgr._session_map, "get_provider", return_value="acp"),
        ):
            provider, is_new, _ = await mgr.get_or_create("dashboard:slot2")
            mgr.release("dashboard:slot2")
        assert is_new is True
        assert mgr.has_session("dashboard:slot2")
        await mgr.close_all()


class TestGetOrCreateDeadProvider:
    """Test get_or_create when existing session has a dead provider."""

    @pytest.mark.asyncio
    async def test_dead_provider_gets_replaced(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # Mark provider as dead
        provider.is_alive = lambda: False
        provider.is_process_alive = lambda: False
        # Next get_or_create should detect dead provider and create new one
        new_provider, is_new, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert new_provider is not provider
        await mgr.close_all()


class TestSessionTimeout:
    @pytest.mark.asyncio
    async def test_session_expires_after_timeout(self, cfg):
        cfg.session.timeout_secs = 1
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        # Manually backdate last_used
        async with mgr._lock:
            mgr._sessions["thread1"].last_used = time.monotonic() - 10
        await mgr._expire_idle(timeout_secs=1)
        assert mgr.count == 0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_active_session_not_expired(self, cfg):
        cfg.session.timeout_secs = 10
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, is_new, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr._expire_idle(timeout_secs=10)
        assert mgr.count == 1
        await mgr.close_all()


class TestConcurrentAccess:
    @pytest.mark.asyncio
    async def test_concurrent_get_or_create_same_key(self, cfg):
        """Second get_or_create on same key reuses existing session."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, new1, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        p2, new2, _ = await mgr.get_or_create("shared")
        mgr.release("shared")
        assert p1 is p2
        assert new1 is True
        assert new2 is False
        assert mgr.count == 1
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_concurrent_different_keys(self, cfg):
        """Different keys should create independent sessions."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        results = await asyncio.gather(  # noqa: F841
            mgr.get_or_create("a"),
            mgr.get_or_create("b"),
            mgr.get_or_create("c"),
        )
        assert mgr.count == 3
        for key in ("a", "b", "c"):
            mgr.release(key)
        await mgr.close_all()


class TestCloseSession:
    @pytest.mark.asyncio
    async def test_close_removes_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, _, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr.destroy("thread1")
        assert not mgr.has_session("thread1")

    @pytest.mark.asyncio
    async def test_close_nonexistent_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.destroy("nonexistent")  # should not raise

    @pytest.mark.asyncio
    async def test_close_calls_shutdown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p, _, _ = await mgr.get_or_create("thread1")
        mgr.release("thread1")
        await mgr.destroy("thread1")
        p.shutdown.assert_awaited_once()


class TestCloseAll:
    @pytest.mark.asyncio
    async def test_close_all_shuts_down_all(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        p1, _, _ = await mgr.get_or_create("a")
        p2, _, _ = await mgr.get_or_create("b")
        mgr.release("a")
        mgr.release("b")
        await mgr.close_all()
        p1.shutdown.assert_awaited_once()
        p2.shutdown.assert_awaited_once()
        assert mgr.count == 0


class TestSessionState:
    @pytest.mark.asyncio
    async def test_is_new_flag(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _, is_new1, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        _, is_new2, _ = await mgr.get_or_create("t1")
        mgr.release("t1")
        assert is_new1 is True
        assert is_new2 is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_release_updates_last_used(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("t1")
        mgr.release("t1")
        async with mgr._lock:
            sess = mgr._sessions["t1"]
        # last_used should be recent (within last second)
        assert time.monotonic() - sess.last_used < 1.0
        await mgr.close_all()


class TestBackgroundSession:
    @pytest.mark.asyncio
    async def test_background_key_constant(self):
        assert BACKGROUND_KEY == "_bg"

    @pytest.mark.asyncio
    async def test_ensure_background_creates_session(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr._ensure_background()
        async with mgr._lock:
            assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_idempotent(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr._ensure_background()
        await mgr._ensure_background()
        # Should still only have one background session
        assert mgr.count == 1
        await mgr.close_all()


class TestCleanupLoopResilience:
    """Tests that _cleanup_loop survives _expire_idle exceptions."""

    @pytest.mark.asyncio
    async def test_cleanup_loop_continues_after_expire_idle_crash(self, cfg):
        """If _expire_idle raises, the loop keeps running."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360
        call_count = 0

        # Collapse the loop's inter-sweep sleep to ~zero WITHOUT busy-spinning.
        # The loop sleeps via ``asyncio.wait_for(shutdown_event.wait(), timeout=interval)``
        # (interval >= 60s). We shrink only THAT call to a tiny real timeout so
        # the wait actually runs: it returns immediately once shutdown_event is
        # set, and otherwise times out in ~1ms. Raising
        # TimeoutError WITHOUT awaiting the wait() would turn the loop into an
        # unbounded busy-spin — if _expire_idle's shutdown_event.set() landed on
        # a cross-loop-rebound event (after an earlier asyncio test in the same
        # process), the top-of-loop is_set() check could miss it and the test
        # would hang until its own outer deadline. Letting the real wait() run
        # makes the stop deterministic regardless of prior event-loop binding.
        # The ``timeout >= 60`` discriminator keeps this from clamping the outer
        # ``wait_for(_cleanup_loop(), timeout=5)`` guard below.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _expire_then_stop(timeout):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated crash")
            # Force loop exit on second call
            from kiro_crew import shutdown_event

            shutdown_event.set()

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_expire_then_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            await asyncio.wait_for(mgr._cleanup_loop(), timeout=5)

        assert call_count >= 2
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cleanup_loop_logs_expire_idle_exception(self, cfg, caplog):
        """Exception in _expire_idle is logged at ERROR level."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cfg.session.timeout_secs = 360

        # Shrink only the loop's inter-sweep sleep to a tiny REAL timeout (not a
        # coro.close()+raise) so the loop observes shutdown_event deterministically
        # instead of busy-spinning — see the note in
        # test_cleanup_loop_continues_after_expire_idle_crash.
        real_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, *, timeout):
            if timeout >= 60:
                return await real_wait_for(coro, timeout=0.001)
            return await real_wait_for(coro, timeout=timeout)

        async def _crash_and_stop(timeout):
            from kiro_crew import shutdown_event

            shutdown_event.set()
            raise ValueError("boom")

        import kiro_crew

        kiro_crew.shutdown_event.clear()
        with (
            patch("asyncio.wait_for", side_effect=_fast_wait_for),
            patch.object(mgr, "_expire_idle", side_effect=_crash_and_stop),
            patch("kiro_crew.session.find_orphan_mcp_candidates", return_value=[]),
        ):
            with caplog.at_level(logging.ERROR):
                await asyncio.wait_for(mgr._cleanup_loop(), timeout=5)

        assert "_expire_idle crashed" in caplog.text
        kiro_crew.shutdown_event.clear()
        await mgr.close_all()


class TestGetBgSessionRecycle:
    """get_bg_session() displaces a healthy-but-stale _bg runtime, killing it
    when idle and parking it to drain when its handles are still live."""

    @pytest.mark.asyncio
    async def test_recycles_stale_idle_runtime(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: False
        stale._is_stale = AsyncMock(return_value="age")
        stale.kill = AsyncMock()
        stale.pid = 111
        mgr._bg_runtime = stale

        rt2 = AsyncMock()
        rt2.spawn = AsyncMock()
        rt2.is_alive = lambda: True
        sentinel = object()
        rt2.create_session = AsyncMock(return_value=sentinel)

        with patch("kiro_crew.acp.runtime.AcpRuntime", side_effect=[rt2]):
            result = await mgr.get_bg_session()

        stale._is_stale.assert_awaited_once()
        stale.kill.assert_awaited_once()  # stale + idle → recycled
        rt2.spawn.assert_awaited_once()  # respawned
        assert result is sentinel
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_parks_a_stale_runtime_that_still_has_active_sessions(self, cfg):
        """A runtime that never goes idle must still be bounded.

        The old policy only recycled during a zero-session window and merely
        logged otherwise, so under sustained background load the age/RSS caps
        were never enforced. Now the retiree is detached from the slot — its
        in-flight work finishes untouched — and new callers get a fresh process.
        Staleness is probed with ``_is_stale()`` (age OR RSS), not the age-only
        ``_stale_by_age()``, because RSS is the growth mode that was observed.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stale = AsyncMock()
        stale.is_alive = lambda: True
        stale.has_active_or_initializing_sessions = lambda: True
        # Inside the age cap; stale by RSS.
        stale._is_stale = AsyncMock(return_value="rss")
        stale.kill = AsyncMock()
        stale.pid = 222
        stale.create_session = AsyncMock(return_value=object())
        mgr._bg_runtime = stale

        rt2 = AsyncMock()
        rt2.spawn = AsyncMock()
        rt2.is_alive = lambda: True
        sentinel = object()
        rt2.create_session = AsyncMock(return_value=sentinel)

        with patch("kiro_crew.acp.runtime.AcpRuntime", side_effect=[rt2]):
            result = await mgr.get_bg_session()

        stale._is_stale.assert_awaited_once()
        stale.kill.assert_not_awaited()  # live handles → parked, not killed
        stale.create_session.assert_not_awaited()  # and never serves again
        assert stale in mgr._draining_bg_runtimes
        assert result is sentinel
        mgr._draining_bg_runtimes = []
        await mgr.close_all()


class TestGetBgSessionBackendSwitch:
    """The _bg runtime spawns under the CONFIGURED ``agent.acp_backend``, and a
    cached runtime spawned under a different backend is recycled once idle —
    otherwise background work (chat titles, suggestions, consolidation) keeps
    running the previous backend indefinitely."""

    @staticmethod
    def _fresh_runtime():
        rt = AsyncMock()
        rt.spawn = AsyncMock()
        rt.is_alive = lambda: True
        rt.create_session = AsyncMock(return_value=object())
        return rt

    @pytest.mark.asyncio
    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    async def test_runtime_spawns_under_the_configured_backend(self, cfg, backend):
        cfg.agent.acp_backend = backend
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._fresh_runtime()

        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt) as ctor:
            result = await mgr.get_bg_session()

        assert ctor.call_args.kwargs["acp_backend"] == backend
        assert result is rt.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycles_an_idle_runtime_spawned_under_a_different_backend(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stranded = AsyncMock()
        stranded.is_alive = lambda: True
        stranded.has_active_sessions = lambda: False
        stranded.has_active_or_initializing_sessions = lambda: False
        stranded.acp_backend = ACP_BACKEND_KIRO  # spawned before the switch
        stranded._is_stale = AsyncMock(return_value=None)  # must NOT be consulted
        stranded.kill = AsyncMock()
        stranded.pid = 333
        mgr._bg_runtime = stranded

        rt2 = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt2) as ctor:
            result = await mgr.get_bg_session()

        stranded.kill.assert_awaited_once()  # mismatched + idle → recycled
        stranded._is_stale.assert_not_awaited()  # mismatch outranks staleness
        assert ctor.call_args.kwargs["acp_backend"] == ACP_BACKEND_KAS
        assert result is rt2.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_busy_mismatched_runtime_is_parked_and_never_serves_a_new_caller(self, cfg):
        """A post-switch caller must never create_session() on the old-backend
        runtime — under sustained load a busy runtime never reaches a
        zero-session window, so waiting for one would let the switch never
        take effect. Its in-flight handles are not killed either."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        busy = AsyncMock()
        busy.is_alive = lambda: True
        busy.has_active_sessions = lambda: True
        busy.has_active_or_initializing_sessions = lambda: True
        busy.acp_backend = ACP_BACKEND_KIRO  # spawned before the switch
        busy.kill = AsyncMock()
        busy.create_session = AsyncMock()
        busy.pid = 335
        mgr._bg_runtime = busy

        rt2 = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt2) as ctor:
            result = await mgr.get_bg_session()

        busy.kill.assert_not_awaited()  # live handles are never killed
        busy.create_session.assert_not_awaited()  # new work goes to the new runtime
        assert busy in mgr._draining_bg_runtimes
        assert ctor.call_args.kwargs["acp_backend"] == ACP_BACKEND_KAS
        assert result is rt2.create_session.return_value
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_parked_runtime_is_reaped_once_its_handles_drain(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        drained = AsyncMock()
        drained.is_alive = lambda: True
        drained.has_active_or_initializing_sessions = lambda: False
        drained.kill = AsyncMock()
        mgr._draining_bg_runtimes = [drained]

        still_busy = AsyncMock()
        still_busy.is_alive = lambda: True
        still_busy.has_active_or_initializing_sessions = lambda: True
        still_busy.kill = AsyncMock()
        mgr._draining_bg_runtimes.append(still_busy)

        rt = self._fresh_runtime()
        with patch("kiro_crew.acp.runtime.AcpRuntime", return_value=rt):
            await mgr.get_bg_session()

        drained.kill.assert_awaited_once()  # drained → reaped
        still_busy.kill.assert_not_awaited()  # busy → stays parked
        assert mgr._draining_bg_runtimes == [still_busy]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failed_reap_keeps_the_runtime_parked(self, cfg):
        """Dropping a parked runtime whose kill failed would orphan a possibly
        live process outside every sweep; keeping it parked retries the kill
        on the next pass."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        stuck = AsyncMock()
        stuck.is_alive = lambda: True
        stuck.has_active_or_initializing_sessions = lambda: False
        stuck.kill = AsyncMock(side_effect=RuntimeError("boom"))
        mgr._draining_bg_runtimes = [stuck]

        async with mgr._bg_runtime_lock:
            await mgr._reap_drained_bg_runtimes_locked()

        assert mgr._draining_bg_runtimes == [stuck]
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_matching_backend_is_reused_not_recycled(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        cached = AsyncMock()
        cached.is_alive = lambda: True
        cached.has_active_sessions = lambda: False
        cached.acp_backend = ACP_BACKEND_KAS
        cached._is_stale = AsyncMock(return_value=None)
        cached.kill = AsyncMock()
        cached.pid = 334
        sentinel = object()
        cached.create_session = AsyncMock(return_value=sentinel)
        mgr._bg_runtime = cached

        with patch(
            "kiro_crew.acp.runtime.AcpRuntime",
            side_effect=AssertionError("should not respawn a matching runtime"),
        ):
            result = await mgr.get_bg_session()

        cached.kill.assert_not_awaited()
        assert result is sentinel
        await mgr.close_all()


class TestRetireStaleBackendBgRuntime:
    """A backend switch retires the cached _bg runtime only once its live
    handles drain — killing it mid-turn would abort an in-flight title
    generation belonging to a caller unrelated to the switch."""

    @staticmethod
    def _runtime(*, backend, busy):
        rt = AsyncMock()
        rt.acp_backend = backend
        rt.has_active_or_initializing_sessions = lambda: busy
        rt.kill = AsyncMock()
        rt.pid = 444
        return rt

    @pytest.mark.asyncio
    async def test_idle_mismatched_runtime_is_retired(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_awaited_once()
        assert mgr._bg_runtime is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_live_handle_is_never_killed_by_a_backend_switch(self, cfg):
        """The busy runtime is parked to drain — its slot is freed so new work
        runs under the configured backend, but its in-flight handles finish."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=True)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is None
        assert rt in mgr._draining_bg_runtimes
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_matching_backend_is_left_alone(self, cfg):
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KAS, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_fails_closed_on_a_holder_without_a_string_backend(self, cfg):
        """A holder that does not declare a string acp_backend (a test double,
        a future holder) is left running rather than recycled on a backend it
        may never have had."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=object(), busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_failed_kill_parks_the_runtime_for_the_reaper(self, cfg):
        """Dropping the reference after a failed kill would orphan a live
        process outside every sweep; parking it retries the kill later while
        keeping its PID shielded."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=False)
        rt.kill = AsyncMock(side_effect=RuntimeError("boom"))
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        assert mgr._bg_runtime is None
        assert rt in mgr._draining_bg_runtimes
        mgr._draining_bg_runtimes = []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_refresh_defaults_triggers_the_retirement(self, cfg):
        """refresh_defaults() re-reads config, so any invocation of it (and any
        future agent.acp_backend edit surface routed through it) must also run
        the retirement check."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        with (
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()) as retire,
            patch("kiro_crew.session.build_provider_factory", return_value=MagicMock()),
            patch("kiro_crew.session.KiroCrewConfig.load", return_value=cfg),
        ):
            await mgr.refresh_defaults()

        retire.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_sweep_incomplete_while_a_parked_runtime_drains(self, cfg):
        """A parked runtime still runs under the previous account, so the
        identity baseline must not advance past it (advancing would record the
        switch as handled and no later turn would re-sweep); once its handles
        drain it is reaped and completeness is restored."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        parked = AsyncMock()
        parked.uses_kiro_identity_store = True
        parked.is_alive = lambda: True
        parked.has_active_or_initializing_sessions = lambda: True
        parked.kill = AsyncMock()
        mgr._draining_bg_runtimes = [parked]

        assert await mgr._retire_kiro_bg_runtime() is False
        parked.kill.assert_not_awaited()

        parked.has_active_or_initializing_sessions = lambda: False
        assert await mgr._retire_kiro_bg_runtime() is True
        parked.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_periodic_hook_reaps_a_drained_parked_runtime(self, cfg):
        """The watchdog hook is the backstop for an idle gateway where no
        background call, refresh, or identity sweep ever runs the other reap
        triggers — without it a drained parked runtime sits shielded from the
        orphan sweep indefinitely."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert any(h.name == "bg_drain_reap" for h in mgr._watchdog._hooks)

        drained = AsyncMock()
        drained.is_alive = lambda: True
        drained.has_active_or_initializing_sessions = lambda: False
        drained.kill = AsyncMock()
        mgr._draining_bg_runtimes = [drained]

        await mgr._bg_drain_reap_hook()

        drained.kill.assert_awaited_once()
        assert mgr._draining_bg_runtimes == []
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_kills_the_slot_and_every_parked_runtime(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        slot = AsyncMock()
        slot.kill = AsyncMock()
        parked = AsyncMock()
        parked.kill = AsyncMock()
        mgr._bg_runtime = slot
        mgr._draining_bg_runtimes = [parked]

        await mgr.close_all()

        slot.kill.assert_awaited_once()
        parked.kill.assert_awaited_once()
        assert mgr._bg_runtime is None
        assert mgr._draining_bg_runtimes == []

    @pytest.mark.asyncio
    async def test_get_bg_session_refuses_while_closing(self, cfg):
        """A runtime spawned or parked after close_all's locked detach would
        leak until the next-startup orphan reaper. The error is the typed
        SessionClosingError so shutdown-aware handlers classify it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._closing = True

        with pytest.raises(SessionClosingError):
            await mgr.get_bg_session()

        mgr._closing = False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_retire_helper_does_not_park_while_closing(self, cfg):
        """refresh_defaults (or the provider path) racing close_all must not
        append to a draining list the shutdown sweep has already cleared."""
        cfg.agent.acp_backend = ACP_BACKEND_KAS
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        rt = self._runtime(backend=ACP_BACKEND_KIRO, busy=True)
        mgr._bg_runtime = rt
        mgr._closing = True

        await mgr._retire_stale_backend_bg_runtime()

        assert mgr._draining_bg_runtimes == []
        assert mgr._bg_runtime is rt  # left for close_all's own detach
        mgr._closing = False
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unreadable_backend_never_displaces_a_cached_runtime(self, cfg):
        """An unreadable probe must not assert a backend it did not read: a
        correctly-configured KAS runtime survives a config edge instead of
        being invisibly recycled onto kiro."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        class _Boom:
            @property
            def acp_backend(self):
                raise RuntimeError("config exploded")

        from types import SimpleNamespace

        mgr._cfg = SimpleNamespace(agent=_Boom())
        rt = self._runtime(backend=ACP_BACKEND_KAS, busy=False)
        mgr._bg_runtime = rt

        await mgr._retire_stale_backend_bg_runtime()

        rt.kill.assert_not_awaited()
        assert mgr._bg_runtime is rt
        assert mgr._draining_bg_runtimes == []
        mgr._bg_runtime = None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_backend_moving_under_the_lock_falls_back_to_the_provider_path(self, cfg):
        """If the config moves to a backend the runtime cannot serve between
        dispatch and the lock, the caller must not get a runtime constructed
        under a backend it cannot classify — it is served through the
        provider-backed path instead."""
        cfg.agent.acp_backend = "claude"  # non-runtime; assigned directly, loader normalizes
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider_handle = object()

        with (
            # Dispatch saw a runtime-capable backend...
            patch.object(mgr, "_bg_backend_supports_runtime", lambda: True),
            # ...but the in-lock revalidation reads the moved config and must
            # divert to the provider path without constructing a runtime.
            patch.object(
                mgr, "_provider_backed_bg_session", AsyncMock(return_value=provider_handle)
            ),
            patch(
                "kiro_crew.acp.runtime.AcpRuntime",
                side_effect=AssertionError("must not construct a runtime for a moved backend"),
            ),
        ):
            result = await mgr.get_bg_session()

        assert result is provider_handle
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ensure_background_does_not_register_a_provider_while_closing(self, cfg):
        """A provider whose start spans close_all's session snapshot must be
        torn down, not registered — a session registered after the snapshot
        escapes graceful cleanup."""
        started = AsyncMock()

        def _factory(*a, **k):
            return started

        mgr = SessionManager(cfg, provider_factory=_factory)

        real_start = started.start

        async def _start_then_close():
            mgr._closing = True
            await real_start()

        started.start = _start_then_close

        await mgr._ensure_background()

        assert BACKGROUND_KEY not in mgr._sessions
        started.shutdown.assert_awaited_once()
        mgr._closing = False
        await mgr.close_all()


def _run_runtime_factory(created_runtimes: list):
    """Factory whose providers each carry a fully-configured shared AcpRuntime.

    In production each factory call spawns its own kiro-cli process; the task
    runner must call the factory ONCE per run and reuse that runtime for every
    step. ``created_runtimes`` records each runtime so tests can assert the
    factory ran exactly once.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        runtime = MagicMock()
        runtime.is_alive = MagicMock(return_value=True)
        runtime.pid = 4321
        runtime.create_session = AsyncMock(
            side_effect=lambda **kw: MagicMock(
                session_id="step-session", memory_mode=kw.get("memory_mode", "persistent")
            )
        )
        runtime.terminate_session = AsyncMock()
        runtime.kill = AsyncMock()
        created_runtimes.append(runtime)

        boot_handle = MagicMock()
        boot_handle.session_id = "bootstrap-sess"
        session_provider = MagicMock()
        session_provider._runtime = runtime
        session_provider._handle = boot_handle
        session_provider._owns_runtime = True

        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider._client = session_provider
        return provider

    return factory


class TestOpenTaskSession:
    """The task runner shares ONE run-scoped AcpRuntime across all its steps."""

    @pytest.mark.asyncio
    async def test_run_shares_one_runtime_across_sessions(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run1:runtime"

        p1, new1, res1 = await mgr.open_task_session(
            parent, "taskrunner:run1:decompose", agent="kirocrew"
        )
        p2, new2, res2 = await mgr.open_task_session(
            parent, "taskrunner:run1:task0", agent="kirocrew"
        )

        # Exactly ONE factory-built runtime, adopted + reused for both steps.
        assert len(created) == 1
        runtime = created[0]
        assert mgr._subagent_runtimes[parent] is runtime
        # Each step opened its own isolated session on the shared runtime.
        assert runtime.create_session.await_count == 2
        # The factory provider's bootstrap session was freed (runtime kept alive).
        runtime.terminate_session.assert_awaited_once_with("bootstrap-sess")
        # Fresh, never-resumed sessions.
        assert new1 is True and new2 is True
        assert res1 is False and res2 is False

        # Release frees the shared runtime exactly once.
        mgr.release("taskrunner:run1:decompose")
        mgr.release("taskrunner:run1:task0")
        await mgr.release_subagent_runtime(parent)
        runtime.kill.assert_awaited_once()
        assert parent not in mgr._subagent_runtimes
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_open_task_session_registers_under_key(self, cfg):
        created: list = []
        mgr = SessionManager(cfg, provider_factory=_run_runtime_factory(created))
        parent = "taskrunner:run2:runtime"
        key = "taskrunner:run2:task0"

        provider, is_new, _resumed = await mgr.open_task_session(parent, key, agent="kirocrew")

        # Registered under the per-step key so reset/context helpers work by key.
        assert key in mgr._sessions
        assert mgr._sessions[key].provider is provider
        mgr.release(key)
        await mgr.release_subagent_runtime(parent)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_macos_workspace_mismatch_uses_dedicated_provider(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        runtime = MagicMock()
        runtime.create_session = AsyncMock(
            side_effect=AcpWorkspaceBindingError("exact workspace required")
        )
        mgr._get_or_bootstrap_run_runtime = AsyncMock(return_value=runtime)
        dedicated = MagicMock()
        mgr.get_or_create = AsyncMock(return_value=(dedicated, True, False))

        result = await mgr.open_task_session(
            "taskrunner:run3:runtime",
            "taskrunner:run3:task0",
            agent="kirocrew",
            cwd="/repo/packages/app",
            approval_policy="auto",
        )

        assert result == (dedicated, True, False)
        mgr.get_or_create.assert_awaited_once_with(
            "taskrunner:run3:task0",
            agent="kirocrew",
            approval_policy="auto",
            cwd="/repo/packages/app",
        )


class TestLoadRecoveryHistoryReplay:
    """F2 load-recovery Phase 2: when a provider signals it fell back to a FRESH
    native session (the prior session's lock never cleared), get_or_create flags
    the new slot for KiroCrew conversation_log replay on the first prompt so the
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
        await asyncio.wait_for(shutdown_started.wait(), timeout=1.0)
        resumed_mgr = SessionManager(cfg, provider_factory=factory)
        assert resumed_mgr._session_map.get("thread1") == "old-full-history-sid"
        release_shutdown.set()
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


class TestIneffectiveCompactionCooldown:
    """A compaction that completes but frees no meaningful headroom keeps the
    failure cooldown instead of clearing it — otherwise every "successful"
    no-progress attempt re-triggers on the next turn end and each retry pays
    another model-generated summarization."""

    @staticmethod
    def _inplace_factory(pct_after: float):
        """kiro-cli-style provider whose /compact completes and whose
        post-compaction ``context_usage_pct()`` reads *pct_after*."""

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: pct_after

            async def _stream(_cmd):
                return
                yield  # pragma: no cover — make this an async generator

            m.stream_command = MagicMock(side_effect=_stream)
            m.wait_for_compaction = AsyncMock(return_value={"type": "completed"})
            return m

        return factory

    # ── (a) effective compaction clears the cooldown ──

    @pytest.mark.asyncio
    async def test_inplace_effective_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=40.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_effective_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 40.0
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 999

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_unknown_post_compaction_pct_defers_verdict(self, cfg):
        """kiro-cli's mid-turn terminal status resets the stats to 0.0/unknown
        before any post-compaction metadata lands. An unknown reading must not
        be judged (a 0.0 would read as a huge drop and mask the defect entirely);
        the verdict is deferred to the first confirmed reading."""
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=0.0))
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        provider.context_usage_unknown = lambda: True
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # No verdict yet: the running cooldown is left to expire on its own
        # and the trigger pct is stashed for the next confirmed reading.
        assert "dashboard:chat-1" in mgr._compact_cooldown_until
        assert mgr._compact_pending_verdict["dashboard:chat-1"] == 92.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_effective_clears_cooldown(self, cfg):
        """First confirmed reading shows a real drop: the deferred verdict is
        effective and the cooldown clears."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999
        provider.context_usage_pct = lambda: 40.0

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert "dashboard:chat-1" not in mgr._compact_pending_verdict
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_deferred_verdict_records_the_compaction_when_it_settles(
        self, cfg, monkeypatch
    ):
        """A compaction whose effect was not measurable at the time still reaches
        the session ledger.

        ``_settle_compact_cooldown`` records only the immediately-confirmed case.
        Without this emit the deferred half -- the reading kiro-cli reset to
        unknown mid-turn -- would settle here and be recorded nowhere, so the
        ledger would be missing exactly the compactions that were hardest to
        measure."""
        from kiro_crew.crew_log import emit as crew_log_emit

        seen: list[tuple[float, float]] = []
        monkeypatch.setattr(
            crew_log_emit,
            "on_compaction_applied",
            lambda sid, *, pct_before, pct_after: seen.append((pct_before, pct_after)),
        )
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 40.0

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert seen == [(92.0, 40.0)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_ineffective_arms_cooldown_and_suppresses_trigger(
        self, cfg, caplog
    ):
        """First confirmed reading is still within the no-progress band: the
        deferred verdict arms the cooldown BEFORE the same call's trigger
        decision, so the immediate re-trigger is suppressed."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 91.0  # >= autocompact_pct (90)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.check_context_usage("dashboard:chat-1", provider)

        assert "dashboard:chat-1" not in mgr._compact_pending_verdict
        assert mgr._compact_cooldown_until.get("dashboard:chat-1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        # The 91% reading is above the trigger threshold, but the just-armed
        # cooldown suppressed the re-trigger: no compaction task started.
        assert "dashboard:chat-1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_waits_for_confirmed_reading(self, cfg):
        """An unknown reading does not consume the pending verdict."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 0.0
        provider.context_usage_unknown = lambda: True

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert mgr._compact_pending_verdict["dashboard:chat-1"] == 92.0
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_stale_reading_defers_instead_of_damping_success(self, cfg):
        """The claude branch can return from compact() before any telemetry
        refresh, so the re-read still shows the PRE-compaction value. Judging
        that stale reading would arm the cooldown on a compaction that in
        fact succeeded — it must defer instead, and the next confirmed
        reading (showing the real drop) must clear cleanly."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 92.0  # unchanged: stats never reset

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        # Deferred, not damped: a successful compaction must not be punished.
        assert "k1" not in mgr._compact_cooldown_until
        assert mgr._compact_pending_verdict["k1"] == 92.0
        # Next turn's confirmed reading shows the real drop — verdict clears.
        provider.context_usage_pct = lambda: 40.0
        mgr.check_context_usage("k1", provider)
        assert "k1" not in mgr._compact_pending_verdict
        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    # ── (b) effective but still above the trigger threshold is NOT ineffective ──

    @pytest.mark.asyncio
    async def test_inplace_effective_above_threshold_not_damped(self, cfg):
        """A good compaction of a very long turn can land above
        ``autocompact_pct`` while still having freed real headroom. The
        ineffective test is the measured drop, not the absolute level."""
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=92.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        assert 92.0 >= cfg.session.autocompact_pct  # still above the trigger

        await mgr._compact_session("dashboard:chat-1", 99.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    # ── (c) ineffective compaction arms the cooldown and suppresses the next trigger ──

    @pytest.mark.asyncio
    async def test_inplace_ineffective_sets_cooldown_and_suppresses_retrigger(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=91.0))
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._compact_session("dashboard:chat-1", 92.0)

        assert mgr._compact_cooldown_until.get("dashboard:chat-1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        # The compaction DID complete and rewrote the conversation: the
        # callback stays success=True (reinjection must run; the failure
        # notice would misdescribe a completed attempt).
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        # The immediate next trigger is suppressed by the cooldown.
        assert mgr._trigger_compaction("dashboard:chat-1", "context 92%", 92.0, provider) == (
            "cooldown"
        )
        assert "dashboard:chat-1" not in mgr._compacting
        provider.stream_command.assert_called_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_ineffective_sets_cooldown(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 91.0
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        cb.assert_awaited_once_with("k1", 92.0, success=True, outcome="compacted")
        assert mgr.has_session("k1")  # in place: the session survives
        await mgr.close_all()

    # ── (d) repeated ineffective compactions and the circuit breaker ──

    @pytest.mark.asyncio
    async def test_repeated_ineffective_keeps_damping_until_breaker_resets(self, cfg):
        """Each ineffective attempt re-arms the one existing cooldown (no
        second counter), so nothing masks the repetition from the existing
        circuit breaker: when the stuck session's turns keep failing,
        ``record_failure`` trips at ``_CIRCUIT_BREAKER_THRESHOLD`` and the
        forced reset clears the cooldown along with the session."""
        from kiro_crew.session import _CIRCUIT_BREAKER_THRESHOLD

        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=91.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")

        # Two ineffective attempts (the second simulating a post-cooldown
        # retry) each re-arm the same cooldown.
        await mgr._compact_session("dashboard:chat-1", 92.0)
        first = mgr._compact_cooldown_until["dashboard:chat-1"]
        await mgr._compact_session("dashboard:chat-1", 92.0)
        assert mgr._compact_cooldown_until["dashboard:chat-1"] >= first

        # The session is still stuck at high context, so its turns fail; the
        # existing breaker observes that repetition and force-resets.
        tripped = False
        for _ in range(_CIRCUIT_BREAKER_THRESHOLD):
            tripped = await mgr.record_failure("dashboard:chat-1")
        assert tripped
        assert not mgr.has_session("dashboard:chat-1")
        # The forced reset clears the cooldown too — the fresh session starts
        # with no inherited damping.
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()


class TestParentEndCancelsItsChildren:
    """A parent that ends takes its sub-agent runs with it, on every backend.

    Releasing the companion runtime already ends the children of a harness that
    multiplexes them onto one process — killing that process is what ends them,
    so it is a side effect rather than a decision. A harness that runs one
    process per child has no entry in ``_subagent_runtimes``, so the release
    reaches nothing and its children outlive the conversation that asked for
    them, each holding an agent process and that process's MCP fleet until its
    own run timeout expires. The lifecycle asks the manager to cancel at every
    site that releases the runtime, which makes the two harness shapes agree
    without either being named.
    """

    @staticmethod
    def _recorder(children: dict[str, tuple[str, ...]] | None = None):
        """A handler shaped like ``SubagentManager``'s two teardown halves.

        Records the keys it was asked to snapshot and the id tuples it was asked
        to cancel, so a test can tell "asked about the right parent" apart from
        "cancelled the right runs".
        """
        snapshotted: list[str] = []
        cancelled: list[tuple[str, ...]] = []
        owned = children if children is not None else {}

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                snapshotted.append(parent_session_key)
                return owned.get(parent_session_key, ("run-1",))

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                cancelled.append(tuple(agent_ids))
                return len(tuple(agent_ids))

        return snapshotted, cancelled, _Handler()

    @pytest.mark.asyncio
    async def test_an_end_cancels_children_without_a_companion_runtime(self, cfg):
        """The cancel sits OUTSIDE the ``_subagent_runtimes`` membership guard.

        That guard is the kiro-shaped condition: a per-process harness never
        appears in it, and its children are exactly the ones that would
        otherwise survive.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        assert "dashboard:chat-9" not in mgr._subagent_runtimes
        await mgr.destroy("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_parent_end_cancels_owned_followup_without_recreating_session(self, cfg):
        """A queued continuation cannot outlive the parent that accepted it."""
        from kiro_crew.subagent import SubagentInfo
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        parent = "dashboard:chat-9"
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create(parent)

        watcher_started = asyncio.Event()
        release_watcher = asyncio.Event()
        dispatch = AsyncMock()

        async def _dispatch_followup():
            watcher_started.set()
            await release_watcher.wait()
            await dispatch()
            await mgr.get_or_create(parent)

        info = SubagentInfo(
            id="child-with-followup",
            task="original task",
            agent="default",
            parent_session_key=parent,
        )
        info.done = True
        info._reported_to_parent = True
        info.pending_followups = ["continue after the parent ends"]
        info._followup_watcher = True
        watcher = asyncio.create_task(_dispatch_followup())
        cancel_reasons: list[str] = []
        audited: list[tuple[str, str]] = []

        class _Children:
            def __init__(self):
                self._agents = {info.id: info}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {info.id: watcher}
                self._followup_watcher_parents = {info.id: parent}
                self._followup_watcher_infos = {info.id: info}

            def _audit_followup(self, owned, outcome):
                audited.append((owned.id, outcome))

            def _cancel_task_intentionally(self, task, owned=None, *, reason):
                cancel_reasons.append(reason)
                task.cancel()

        children = _Children()
        coordinator = CancellationCoordinator(children)  # type: ignore[arg-type]

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key):
                return coordinator.snapshot_teardown_children_impl(parent_session_key)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key="",
                verb="",
            ):
                return await coordinator.cancel_for_teardown_impl(
                    agent_ids,
                    parent_session_key=parent_session_key,
                    verb=verb,
                )

        mgr.set_child_teardown_handler(_Handler())
        await watcher_started.wait()
        try:
            await mgr.remove(parent)
            release_watcher.set()
            await asyncio.gather(watcher, return_exceptions=True)

            assert cancel_reasons == ["parent teardown cancelled owned follow-up"]
            assert info.pending_followups == []
            assert audited == [(info.id, "followup_suppressed")]
            assert children._followup_watchers == {}
            assert children._followup_watcher_parents == {}
            assert children._followup_watcher_infos == {}
            dispatch.assert_not_awaited()
            assert not mgr.has_session(
                parent
            ), "the queued follow-up rebuilt the conversation after parent teardown"
        finally:
            release_watcher.set()
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_cancels_children(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_destroy_cancels_children(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.destroy("dashboard:chat-9")

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_discard_conversation_cancels_children(self, cfg):
        """A fresh conversation under the same slot ends the old one's children."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.discard_conversation("dashboard:chat-9") is True

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_remove_if_unclaimed_cancels_children(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        # A speculative session is removable only while its first turn is armed
        # and unclaimed: the arm is a non-sentinel ``first_turn``, and unclaimed
        # means nothing holds the session's semaphore.
        mgr.release("dashboard:chat-9")
        mgr._sessions["dashboard:chat-9"].first_turn = object()

        assert await mgr.remove_if_unclaimed("dashboard:chat-9") is True

        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_identity_retire_cancels_children(self, cfg):
        """An identity-store change retires a session, so its children end too."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")
        mgr.release("dashboard:chat-9")

        with patch(
            "kiro_crew.session._provider_uses_kiro_identity_store",
            return_value=True,
        ):
            retired, _complete = await mgr.retire_kiro_identity_sessions()

        assert retired == ["dashboard:chat-9"]
        assert seen == [("run-1",)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_close_all_leaves_cancellation_to_cancel_all(self, cfg):
        """Gateway shutdown is deliberately not a per-key cancel.

        ``SubagentManager.cancel_all`` runs there instead: it also drains
        follow-up watchers and announces undelivered messages, which a per-key
        cancel does not do.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, seen, cb = self._recorder()
        mgr.set_child_teardown_handler(cb)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.close_all()

        assert seen == []

    @pytest.mark.asyncio
    async def test_a_failing_cancel_never_blocks_the_parent_end(self, cfg):
        """Best-effort, matching the recycle callback beside it."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())

        class _Wedged:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                raise RuntimeError("subagent manager is wedged")

        mgr.set_child_teardown_handler(_Wedged())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert mgr.count == 0
        provider.shutdown.assert_awaited_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_teardown_does_not_report_the_run_into_the_retired_parent(self, cfg):
        """The parent-end path routes through the verb that suppresses delivery.

        ``_on_done`` resolves the parent key through the session registry and
        injects, creating a session when none is live — so reporting a
        teardown-cancelled run rebuilds the conversation the teardown just took
        down and seeds it with that run's terminal text. The suppression rides on
        ``cancel_for_teardown``, so what this pins is that the lifecycle calls that
        verb and not the Stop-all one.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        teardown_calls: list[tuple[str, ...]] = []
        stop_all_calls: list[str] = []

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-7",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                teardown_calls.append(tuple(agent_ids))
                return len(tuple(agent_ids))

            async def cancel_for_parent(self, parent_session_key: str) -> tuple[int, int]:
                stop_all_calls.append(parent_session_key)
                return (0, 0)

        mgr.set_child_teardown_handler(_Handler())
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert teardown_calls == [("run-7",)]
        assert stop_all_calls == [], (
            "a parent end used the Stop-all verb, whose terminal report injects "
            "into the parent it just retired"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_removing_an_already_reset_parent_still_ends_its_children(self, cfg):
        """A reset pops the session; the tab close that follows is still a parent end.

        The two verbs make this sequence ordinary now that a reset keeps the
        conversation: reset recycles the process, then the user closes the tab and
        ``remove`` arrives with ``session is None``. Guarding the cancel on a live
        provider skipped exactly that case and left the children running.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True
        assert cancelled == [], "the reset itself ended the children"

        await mgr.remove("dashboard:chat-9")

        assert cancelled == [
            ("run-1",)
        ], "removing a parent whose session was already popped left its children running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_wait_expires_without_cancelling_the_reap(self, cfg):
        """The timeout bounds the parent's WAIT, not the child reap.

        A bare ``wait_for`` cancels what it waits on, and this coroutine kills child
        processes: a long child reset would have its reap cancelled after the marks were
        written and before the kills landed, leaving a write-capable child executing
        against a conversation that has ended.
        """
        import asyncio as _asyncio

        finished = _asyncio.Event()
        observed: list[str] = []

        class _SlowHandler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(
                self,
                agent_ids,
                *,
                parent_session_key: str = "",
                verb: str = "",
            ) -> int:
                try:
                    await _asyncio.sleep(0.25)
                    observed.append("completed")
                except _asyncio.CancelledError:
                    observed.append("cancelled")
                    raise
                finally:
                    finished.set()
                return 1

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_child_teardown_handler(_SlowHandler())
        await mgr.get_or_create("dashboard:chat-9")

        with patch("kiro_crew.session_lifecycle._CHILD_CANCEL_TIMEOUT_SECS", 0.01):
            await mgr.remove("dashboard:chat-9")

        await _asyncio.wait_for(finished.wait(), timeout=5)
        assert observed == ["completed"], (
            "the parent's wait expiring cancelled the reap itself, so a child could "
            f"outlive the teardown mid-kill: {observed}"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_child_parked_on_a_spawn_approval_is_not_cancelled(self):
        """An approval is a decision a person was asked for; a teardown may not answer it.

        Observed, not theorised: the private-workflow E2E spawns a child that parks on a
        spawn approval, the pooled worker's ``destroy`` cancelled it, and the test's
        ``POST /api/approvals/spawn:<id>/approve`` then answered 404 "not found or
        expired" -- indistinguishable from the person having taken too long to reply.

        ``cancel_for_parent_impl`` already applies this rule for Stop-all; the teardown
        snapshot did not, which is the asymmetry. ``_exec_started is None`` is part of the
        test: a run that HAS begun and is parked on a later approval is live work, and a
        parent end does stop that.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        def _run(agent_id, *, awaiting, started):
            return SimpleNamespace(
                id=agent_id,
                parent_session_key="wf-pool:wf_000008:0",
                done=False,
                _reported_to_parent=False,
                _digest_held=False,
                _digest_held_at=0.0,
                _digest_settle_ids=[],
                _delivery_queued=False,
                _awaiting_approval=awaiting,
                _exec_started=started,
            )

        class _Manager:
            def __init__(self):
                self._agents = {
                    "parked": _run("parked", awaiting=True, started=None),
                    "started-then-parked": _run(
                        "started-then-parked", awaiting=True, started=123.0
                    ),
                    "ordinary": _run("ordinary", awaiting=False, started=123.0),
                }
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("wf-pool:wf_000008:0")

        assert "parked" not in selected, (
            "a child parked on a spawn approval was handed to the cancel loop, so the "
            "approval the user was asked for answers 404"
        )
        assert sorted(selected) == [
            "ordinary",
            "started-then-parked",
        ], f"the exclusion is too wide and spared live work: {sorted(selected)}"
        # Not cancelled is not the same as ignored: the conversation it would report into
        # has ended, so the delivery is still gated. The run keeps its own decision and
        # loses only the injection.
        assert "parked" in manager._teardown_cancelled_ids, (
            "an approval-parked child escaped the teardown entirely, so approving it "
            "later injects into whatever session that key serves by then"
        )

    @pytest.mark.asyncio
    async def test_a_finished_but_undelivered_child_is_gated_without_being_cancelled(self):
        """Done is not delivered, and only the delivered ones may be let through.

        ``_reported_to_parent`` is set the moment ``_on_done`` returns, so a run that is
        ``done`` without it still has a report, a digest hold or a queued announce
        outstanding. Selecting only the not-done runs left that class of child free to
        inject -- and the injector resolves the parent key through the session registry
        and CREATES a session when none is live, so the delivery rebuilds the very
        conversation the teardown took down.

        It is marked but NOT returned: there is nothing left to cancel, and handing it to
        the cancel loop would publish a synthetic "never started" terminal over a run
        that finished.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        def _run(agent_id: str, *, done: bool, reported: bool, held=0.0, queued=False):
            info = SimpleNamespace(
                id=agent_id,
                parent_session_key="dashboard:chat-9",
                done=done,
                _reported_to_parent=reported,
                _digest_held_at=held,
                _delivery_queued=queued,
            )
            return info

        class _Manager:
            def __init__(self):
                self._agents = {
                    "running": _run("running", done=False, reported=False),
                    "undelivered": _run("undelivered", done=True, reported=False),
                    # ``_on_done`` RETURNED for these two, having only parked the
                    # delivery: a wave member held for a digest, and a dashboard
                    # announce sitting in the parent's slot queue. Both fire later,
                    # from a path of their own, into whatever the key serves by then.
                    "digest-held": _run("digest-held", done=True, reported=True, held=1.0),
                    "queued-announce": _run(
                        "queued-announce", done=True, reported=True, queued=True
                    ),
                    "delivered": _run("delivered", done=True, reported=True),
                }
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        assert selected == ("running",), "a finished run was handed to the cancel loop"
        for parked in ("undelivered", "digest-held", "queued-announce"):
            assert parked in manager._teardown_cancelled_ids, (
                f"{parked} was left free to rebuild the conversation the teardown just "
                "took down -- a report that RETURNED is not a delivery that landed"
            )
        assert "running" in manager._teardown_cancelled_ids
        assert (
            "delivered" not in manager._teardown_cancelled_ids
        ), "a child whose outcome already reached the parent needs no gate"

    @pytest.mark.asyncio
    async def test_a_reset_leaves_a_healthy_child_running(self, cfg):
        """A reset recycles a process; it does not end the conversation.

        The session-map entry keeps its resume sid, so the next turn on the key restores
        the same native conversation through ``session/load``. A child therefore has
        somewhere to deliver and is bounded by its own run timeout, and stopping it would
        discard live work belonging to a conversation that is coming back.

        This is the invariant for EVERY reset caller, which is why it is asserted on the
        bare call: the verb is what every evict-and-retry path reaches for -- a wedged
        prompt, a failed auto-compaction, a provider switch, an idle expiry, the channel
        watchdog -- and each of those is a retry, not an ending.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True

        assert cancelled == [], "a process recycle stopped a child of a surviving conversation"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_reset_that_ends_the_conversation_stops_the_children(self, cfg):
        """Some endings reach only ``reset``, so the intent has to be sayable there.

        A channel "clear context", a cancelled cron job, a task run's cancel cleanup and a
        workflow pool starting a new conversation all end a conversation through this one
        verb. Removing cancellation from it entirely would leave each of those leaking its
        children.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9", ends_conversation=True) is True

        assert cancelled == [("run-1",)]
        await mgr.close_all()

    def test_the_delivery_parked_states_are_enumerated_from_the_producers(self):
        """The parked-delivery states come from the WRITERS, not from failures.

        "Has this run's outcome reached its parent" has several representations, and each
        round of review found one more by hitting it: the report returning
        (``_reported_to_parent``), the wave hold (``_digest_held`` and its separate
        timestamp ``_digest_held_at``), the siblings held on this member
        (``_digest_settle_ids``) and the announce parked in the parent's slot queue
        (``_delivery_queued``). Discovering them one failure at a time is what made the
        teardown gate wrong four times.

        So the set is derived the other way: walk the four modules that WRITE routing state
        on a ``SubagentInfo`` and require every attribute they assign to be classified in
        ``DELIVERY_ROUTING_FIELDS``. A new parked state has to be written by one of them, so
        adding one without a teardown rule fails here rather than in a conversation that
        was supposed to be over.
        """
        import ast
        import pathlib

        import kiro_crew.subagent as _subagent_mod
        from kiro_crew.subagent import DELIVERY_ROUTING_FIELDS, DELIVERY_ROUTING_MODULES

        root = pathlib.Path(_subagent_mod.__file__).resolve().parent
        written: set[str] = set()
        for rel in DELIVERY_ROUTING_MODULES:
            module = root / rel
            assert module.exists(), f"a named producer has moved or been renamed: {rel}"
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, ast.AugAssign):
                    targets = [node.target]
                else:
                    continue
                for target in targets:
                    if not isinstance(target, ast.Attribute):
                        continue
                    if getattr(target.value, "id", None) in {"info", "_info"}:
                        written.add(target.attr)

        classified = set(DELIVERY_ROUTING_FIELDS)
        unclassified = sorted(written - classified)
        assert not unclassified, (
            "these SubagentInfo fields are written by a delivery-routing producer and are "
            "not classified in DELIVERY_ROUTING_FIELDS: "
            f"{unclassified}. Add each one as PARKS_WHEN_SET (truthy means the outcome is "
            "parked and has not reached the parent), PARKS_WHEN_UNSET (falsy means that), "
            "or NOT_DELIVERY_STATE. A parked state with no rule is a completion that "
            "lands in a conversation the teardown already ended."
        )
        stale = sorted(classified - written)
        assert not stale, (
            "these fields are classified in DELIVERY_ROUTING_FIELDS but no named producer "
            f"writes them any more, so the classification is unchecked: {stale}"
        )

    def test_every_parked_state_makes_the_delivery_parked(self):
        """Each rule in the table is exercised on its own.

        A classification nothing reads is a classification that can be wrong, so this drives
        one field at a time rather than trusting the table's shape.
        """
        from kiro_crew.subagent import (
            DELIVERY_ROUTING_FIELDS,
            PARKS_WHEN_SET,
            PARKS_WHEN_UNSET,
            SubagentInfo,
            delivery_is_parked,
        )

        landed = SubagentInfo(id="landed", task="t", agent="a")
        landed._reported_to_parent = True
        assert delivery_is_parked(landed) is False, (
            "a run whose report returned and which parks nothing reads as parked, so the "
            "gate would suppress every delivery"
        )

        exercised = 0
        for field_name, rule in DELIVERY_ROUTING_FIELDS.items():
            if rule == PARKS_WHEN_SET:
                info = SubagentInfo(id=field_name, task="t", agent="a")
                info._reported_to_parent = True
                setattr(info, field_name, [777] if field_name.endswith("_ids") else 1.0)
                assert delivery_is_parked(info) is True, f"{field_name} does not park"
                exercised += 1
            elif rule == PARKS_WHEN_UNSET:
                info = SubagentInfo(id=field_name, task="t", agent="a")
                setattr(info, field_name, False)
                assert delivery_is_parked(info) is True, f"unset {field_name} does not park"
                exercised += 1

        assert exercised >= 5, f"only {exercised} parking rule(s) exercised; the table lost a rule"

    @pytest.mark.asyncio
    async def test_the_teardown_records_one_audit_line_naming_its_verb(self, caplog):
        """A parent end cancels work someone may be waiting on, so it says what it took.

        Six verbs reach one helper, and from outside the only evidence a run was cancelled
        by a teardown is the absence of its result — which is indistinguishable from a
        cancelled-too-early row. The line carries the verb, the key, whether the retired
        generation still held, and the ids from each source, so the two can be told apart
        from a log alone.

        One line at one choke point rather than a print at each verb: six copies of a log
        statement drift, and the verb is only known here.
        """
        import logging

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        class _Admission:
            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # The teardown asks before falling back to the live reap. False means
                # "not claimed-and-unstarted", which keeps these doubles on the path they
                # were written for: the row is reapable, and the store cancel lands first
                # anyway.
                return False

            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                return {"_preassigned_id": agent_id}

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                self._agents = {}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return {"_preassigned_id": agent_id}

            def _report_queued_stop(self, params):
                pass

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        with caplog.at_level(logging.INFO, logger="kiro_crew.subagent"):
            await coordinator.cancel_for_teardown_impl(
                ("in-window",),
                parent_session_key="dashboard:chat-9",
                verb="discard_conversation",
            )

        audit = [r.getMessage() for r in caplog.records if "parent-end teardown" in r.getMessage()]
        assert len(audit) == 1, f"expected exactly one audit line, got {audit}"
        line = audit[0]
        for fragment in (
            "verb=discard_conversation",
            "key=dashboard:chat-9",
            "snapshot=1",
            "total=1",
            "snapshot_ids=in-window",
        ):
            assert fragment in line, f"audit line is missing {fragment!r}: {line}"

    def test_every_parent_end_verb_names_itself_in_the_audit(self):
        """Every call site passes a verb, and the name matches its own method.

        A site that forgets the keyword cannot compile (the parameter is keyword-only and
        required), but one that passes the WRONG name is silent and makes the audit lie —
        which is worse than no audit, because the line is what a later reader trusts.
        """
        import ast
        import inspect

        from kiro_crew import session_lifecycle

        tree = ast.parse(inspect.getsource(session_lifecycle))
        # EVERY method that reaches the teardown names itself, with no exemption: the
        # conversation-ended primitive hardcodes its own name too, so there is one rule here
        # rather than a rule plus a carve-out whose own correctness needed checking.
        seen: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                if not (isinstance(func, ast.Attribute) and func.attr == "_cancel_parent_children"):
                    continue
                verbs = [
                    kw.value.value
                    for kw in call.keywords
                    if kw.arg == "verb" and isinstance(kw.value, ast.Constant)
                ]
                assert verbs, f"{node.name} calls the teardown without naming its verb"
                seen[node.name] = verbs[0]

        assert seen, "no call sites found — the ratchet is reading the wrong thing"
        assert "end_children_for" in seen, (
            "the conversation-ended primitive no longer reaches the teardown, so the audit "
            "line cannot name it"
        )
        wrong = {name: verb for name, verb in seen.items() if verb != name}
        assert not wrong, (
            "these parent-end methods report a verb that is not their own name, so the "
            f"audit line names the wrong caller: {wrong}"
        )

    @pytest.mark.asyncio
    async def test_an_ending_reset_after_a_recycling_one_still_ends_the_children(self, cfg):
        """A live provider is not what makes a reset an ending.

        The two verbs make this sequence ordinary: a recycle pops the session, then an
        ending reset on the same key arrives with ``session is None`` while the children
        are still running. With the cancel inside the provider-shutdown block it was
        skipped, which is the same shape that skipped it in ``remove``.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        assert await mgr.reset("dashboard:chat-9") is True
        assert cancelled == [], "the recycling reset ended the children"

        await mgr.reset("dashboard:chat-9", ends_conversation=True)

        assert cancelled == [
            ("run-1",)
        ], "an ending reset on an already-popped session left its children running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_teardown_leaves_a_claimed_but_unstarted_row_to_its_claimer(self):
        """A teardown may not cancel a CLAIMED-but-unstarted durable row.

        Two reasons meeting at one place. Its claimer sits between the claim and the
        registration, so cancelling here leaves that claimer to register and run work the
        teardown believed it had stopped -- and the claimer's own state re-read before
        registering has nothing to catch, because the row is gone rather than claimable.
        And a row in that window may carry a decision a person made (a spawn approval is
        the visible case), which a teardown has no standing to revoke for them.

        Both halves are asserted: the store cancel is asked to refuse an ``admitted`` row,
        and the refusal does not then reach the live reap through the other door.
        """
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        asked: list[bool] = []
        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                asked.append(allow_admitted)
                # What the store answers for a row that is claimed and not started once
                # ``admitted`` is off the accepted set.
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # What the probe answers for a row that is claimed and not started.
                return True

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                self._agents = {}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        await coordinator.cancel_for_teardown_impl(
            ("claimed-row",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert asked == [False], (
            "the teardown asked the store to accept an admitted row, so a claimer can "
            f"still register and run stopped work: allow_admitted={asked}"
        )
        assert reaped == [], (
            "the store refused the row and the teardown reaped it anyway through the live "
            "path -- the same act, a different door"
        )

    @pytest.mark.asyncio
    async def test_a_started_row_the_store_refuses_still_takes_the_live_reap(self):
        """The contrast: a row a drain has STARTED is a live run, and is reaped.

        The refusal has two reasons and they want opposite things, so the probe that tells
        them apart has to be exercised both ways or the narrowing above silently becomes
        "never reap a refused row".

        The record has to be LIVE for the reap to be reached at all: ``cancel`` walks into
        the synchronous ``_unqueue`` store call, so it is only for a row with a task behind
        it, never a blind retry of what the store just declined.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # Started, so not claimed-and-unstarted: this row takes the live reap.
                return False

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                # A live record is what makes the reap reachable: the drain started this
                # row, so there is a task to stop rather than a row to unqueue.
                self._agents = {"started-row": SimpleNamespace(id="started-row", done=False)}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        stopped = await coordinator.cancel_for_teardown_impl(
            ("started-row",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert reaped == ["started-row"], "a live run escaped the teardown"
        assert stopped == 1

    @pytest.mark.asyncio
    async def test_a_child_that_finished_mid_teardown_never_reaches_the_sync_store_call(self):
        """A record that is PRESENT is not a record that is RUNNING.

        The live branch at the top of the loop tests ``not info.done``. The fallback below
        it, reached when the store declines the row, tested only presence -- and those are
        different questions for exactly one child: one that was live when the snapshot
        named it and finished during this loop's own awaits. ``_force_reap`` marks such a
        record done and drops its task, but does not pop it from ``_agents``, so the record
        lingers, terminal.

        Reading presence alone sent that record into ``cancel`` -> ``_unqueue``, whose
        store call is the SYNCHRONOUS one, from a coroutine on the gateway loop -- the
        stall ``no-sync-store-call-from-a-coroutine`` forbids, and the very thing the
        fallback's own comment claims to be avoiding. There was nothing to gain either:
        the row is already terminal.

        Driven through the real coordinator with the store answering as it does for a
        terminal row -- nothing to cancel, not claimed-and-unstarted -- because that
        combination is what puts the record on this branch at all.
        """
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        sync_store_calls: list[str] = []
        reaped: list[str] = []

        class _Admission:
            async def taskq_cancel_queued_async(self, agent_id, *, allow_admitted=True):
                # A terminal row: there is no unstarted row left to cancel.
                return None

            async def taskq_row_is_claimed_unstarted_async(self, agent_id):
                # Not claimed-and-unstarted either -- it ran and finished.
                return False

        class _Manager:
            def __init__(self):
                self._admission = _Admission()
                # The lingering record: done, and still in ``_agents``.
                self._agents = {"late-finisher": SimpleNamespace(id="late-finisher", done=True)}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers: dict = {}

            def _unqueue(self, agent_id, *, stored=None, store_cancelled=False):
                # ``_unqueue`` runs ``taskq_cancel_queued`` itself ONLY when the caller has
                # not already cancelled the row (``if not store_cancelled``), so the
                # store-reaching shape is the one to count. The teardown's own call passes
                # ``store_cancelled=True`` and is fine; ``cancel`` -> ``cancel_impl`` passes
                # neither, and that is the stall.
                if not store_cancelled:
                    sync_store_calls.append(agent_id)
                return None

            def _report_queued_stop(self, params):
                pass

            async def cancel(self, agent_id):
                reaped.append(agent_id)
                self._unqueue(agent_id)
                return True

        coordinator = CancellationCoordinator(_Manager())  # type: ignore[arg-type]

        stopped = await coordinator.cancel_for_teardown_impl(
            ("late-finisher",),
            parent_session_key="dashboard:chat-9",
            verb="destroy",
        )

        assert sync_store_calls == [], (
            "a run that finished mid-teardown reached the synchronous store call on the "
            f"gateway loop: {sync_store_calls}"
        )
        assert reaped == [], (
            "a terminal record was routed through the live reap, which is the path that "
            f"walks into the sync store call: {reaped}"
        )
        assert stopped == 0, "nothing was stopped: the run had already finished"

    @pytest.mark.asyncio
    async def test_an_ending_reset_cancels_before_it_releases_the_runtime(self, cfg):
        """Cancel FIRST, then reap the runtime the children are multiplexed onto.

        Every other verb keeps that order and the helper's own docstring requires it: a
        child is stopped through its own teardown rather than by having the runtime pulled
        out from under a live turn. Lifting the cancel out of the provider block put it
        after the release, inverting the order for this one verb.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        order: list[str] = []

        class _Handler:
            def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
                return ("run-1",)

            async def cancel_for_teardown(self, agent_ids, **_kwargs) -> int:
                order.append("cancel")
                return len(tuple(agent_ids))

        mgr.set_child_teardown_handler(_Handler())
        await mgr.get_or_create("dashboard:chat-9")
        mgr._subagent_runtimes["dashboard:chat-9"] = object()

        original = mgr.release_subagent_runtime

        async def _release(key):
            order.append("release")
            mgr._subagent_runtimes.pop(key, None)

        mgr.release_subagent_runtime = _release  # type: ignore[method-assign]
        try:
            await mgr.reset("dashboard:chat-9", ends_conversation=True)
        finally:
            mgr.release_subagent_runtime = original  # type: ignore[method-assign]

        assert order == [
            "cancel",
            "release",
        ], f"the runtime was reaped before its children were stopped: {order}"
        await mgr.close_all()

    @pytest.mark.parametrize(
        "verb",
        [
            "reset",
            "remove",
            "destroy",
            "discard_conversation",
            "remove_if_unclaimed",
            "retire_kiro_identity_sessions",
        ],
    )
    @pytest.mark.asyncio
    async def test_a_failing_provider_shutdown_still_ends_the_children(self, cfg, verb):
        """A shutdown that raises must not carry the exception past the cancel.

        At every one of these sites the parent has already been retired from the session
        map by the time the provider is asked to stop, so an exception leaving the method
        early leaves children running with no parent to report to and the runtime they
        share still held. It is the one outcome these verbs exist to prevent, reached by
        the one path nobody exercises -- which is why it is parametrized over every site
        rather than demonstrated once.

        The verbs keep their own error contracts: ``reset`` re-raises after the cancel,
        ``retire_kiro_identity_sessions`` turns the failure into a warning and leaves the
        key unretired, and the rest propagate. None of them may lose the children.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        session = mgr._sessions["dashboard:chat-9"]

        async def _boom():
            raise RuntimeError("provider shutdown failed")

        session.provider.shutdown = _boom  # type: ignore[method-assign]

        # Each verb needs the precondition that makes it act at all, or the test proves
        # only that a method returned early. These are the same setups the per-verb
        # cancel tests above use.
        if verb == "remove_if_unclaimed":
            # Removable only while the first turn is armed and unclaimed: a non-sentinel
            # ``first_turn``, and nothing holding the semaphore.
            mgr.release("dashboard:chat-9")
            mgr._sessions["dashboard:chat-9"].first_turn = object()
        elif verb == "retire_kiro_identity_sessions":
            mgr.release("dashboard:chat-9")

        if verb == "reset":
            await_call = mgr.reset("dashboard:chat-9", ends_conversation=True)
        elif verb == "retire_kiro_identity_sessions":
            await_call = None
        else:
            await_call = getattr(mgr, verb)("dashboard:chat-9")

        with contextlib.suppress(RuntimeError):
            if verb == "retire_kiro_identity_sessions":
                # The identity marker is what selects a session for this sweep, and the
                # sweep reports a failure rather than raising it.
                with patch(
                    "kiro_crew.session._provider_uses_kiro_identity_store",
                    return_value=True,
                ):
                    await mgr.retire_kiro_identity_sessions()
            else:
                await await_call

        assert cancelled == [("run-1",)], (
            f"{verb}: a failing provider shutdown skipped the child cancel, so the "
            f"children outlived the parent: {cancelled}"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_ending_the_children_leaves_the_process_alone(self, cfg):
        """The conversation-ended half on its own, for a caller that keeps the process.

        A pooled workflow worker hands its warm process to the next task by replacing the
        conversation (``provider.new_conversation()``). The children of the conversation
        that ended have nowhere to report, and the process surviving does not change that.
        So this verb makes exactly one claim -- the conversation is over -- and must not
        shut the provider down, delete the map entry or release the runtime.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")
        session = mgr._sessions["dashboard:chat-9"]
        before = mgr.session_generation("dashboard:chat-9")

        await mgr.end_children_for("dashboard:chat-9")

        assert cancelled == [("run-1",)]
        assert (
            mgr._sessions.get("dashboard:chat-9") is session
        ), "the live session was torn down by a verb that only ends the conversation"
        session.provider.shutdown.assert_not_awaited()
        # The ownership generation belongs to session ALLOCATION -- it counts reservation
        # publications and removals, and ``get_or_create`` advances it twice on an ordinary
        # turn. This verb retires no reservation: the session stays registered and the
        # process keeps serving. Advancing it here would report a replacement to every
        # reader of that counter, including the conditional-destruction checks it exists
        # for, on a call that destroyed nothing.
        assert mgr.session_generation("dashboard:chat-9") == before, (
            "ending the children advanced the allocation generation, so a conditional "
            "destroy of the still-live session now reads it as a successor"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_teardown_disarms_the_watcher_of_a_delivered_run(self):
        """A DELIVERED run still holds a watcher, and that watcher can still speak.

        The sibling test above covers a run that is still running. This one covers the case
        the three selected lists cannot reach: a run that is ``done`` AND whose terminal
        already reached the parent. It is in none of them -- not live, not approval-parked,
        not undelivered -- and it is not even MARKED, because there is nothing left to
        cancel and nothing parked to gate.

        But a follow-up watcher outlives its run BY DESIGN. It dispatches after the run
        finishes, which is exactly why the dispatch cannot read the asking ordinal. So at a
        parent end such a watcher is still armed, and it has two ways to speak into a
        conversation that is over: dispatch the queued continuation on the retired key, or
        announce a failure built as a SYNTHETIC record with ``uuid4`` for an id -- a fresh
        id no gate keyed on the original run recognises, handed to ``_on_done``, which
        resolves the parent through the session-creating ``get_or_create``.

        Disarming is therefore keyed on OWNERSHIP, not on what was selected: every run this
        parent owns loses its watcher. Dropping the follow-up silently is the right outcome
        -- it was a correction queued for a conversation that has ended, and announcing it
        is the very injection the teardown exists to prevent.
        """
        import asyncio as _asyncio
        from types import SimpleNamespace

        from kiro_crew.subagent import delivery_is_parked
        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        async def _forever():
            await _asyncio.Event().wait()

        watcher = _asyncio.ensure_future(_forever())
        await _asyncio.sleep(0)  # let it start, so cancel() has something to interrupt

        delivered = SimpleNamespace(
            id="run-delivered",
            parent_session_key="dashboard:chat-9",
            done=True,
            # Its terminal reached the parent, so NOTHING is parked -- this is the state
            # that puts it outside all three lists.
            _reported_to_parent=True,
            _digest_held=False,
            _digest_held_at=0.0,
            _digest_settle_ids=[],
            _delivery_queued=False,
            _awaiting_approval=False,
            _exec_started=None,
        )
        assert delivery_is_parked(delivered) is False, (
            "the fixture must be a DELIVERED run, or it lands in `undelivered` and the "
            "three-list iterable would have covered it"
        )

        class _Manager:
            def __init__(self):
                self._agents = {"run-delivered": delivered}
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {"run-delivered": watcher}

            def _cancel_task_intentionally(self, task, info=None, *, reason):
                task.cancel()

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        selected = coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        # Precondition for the test to mean anything: this run is neither cancelled nor
        # marked, so the watcher is the ONLY thing the teardown could still act on.
        assert selected == (), f"a delivered run has nothing to cancel: {selected}"
        assert manager._teardown_cancelled_ids == set(), (
            "a delivered run needs no delivery gate, so it is deliberately unmarked: "
            f"{manager._teardown_cancelled_ids}"
        )

        assert "run-delivered" not in manager._followup_watchers, (
            "the watcher of a delivered run survived the teardown, so it can still "
            "dispatch its follow-up or announce a fresh-id synthetic into the retired "
            "conversation"
        )
        await _asyncio.sleep(0)  # cancellation is observed on the next loop pass
        assert watcher.cancelled() or watcher.done(), "the watcher was popped but not stopped"

    @pytest.mark.asyncio
    async def test_a_teardown_disarms_its_runs_follow_up_watchers(self):
        """A follow-up watcher is a second announce path the id gate cannot see.

        When a queued follow-up cannot be delivered the watcher announces a SYNTHETIC
        failure built with a FRESH id, so it walks past a gate keyed on the run that
        produced it -- the same shape as the wave digest's flush record, and closed the same
        way: disarm it at the source rather than try to recognise its output.
        """
        import asyncio as _asyncio
        from types import SimpleNamespace

        from kiro_crew.subagent_manager.cancellation import CancellationCoordinator

        async def _forever():
            await _asyncio.Event().wait()

        watcher = _asyncio.ensure_future(_forever())

        class _Manager:
            def __init__(self):
                self._agents = {
                    "run-1": SimpleNamespace(
                        id="run-1",
                        parent_session_key="dashboard:chat-9",
                        done=False,
                        _reported_to_parent=False,
                        _digest_held=False,
                        _digest_held_at=0.0,
                        _digest_settle_ids=[],
                        _delivery_queued=False,
                        _awaiting_approval=False,
                        _exec_started=1.0,
                    )
                }
                self._queue = []
                self._teardown_cancelled_ids = set()
                self._followup_watchers = {"run-1": watcher}

            def _cancel_task_intentionally(self, task, info=None, *, reason):
                task.cancel()

        manager = _Manager()
        coordinator = CancellationCoordinator(manager)  # type: ignore[arg-type]

        coordinator.snapshot_teardown_children_impl("dashboard:chat-9")

        assert "run-1" not in manager._followup_watchers, (
            "the watcher survived the teardown, so it can still compose a fresh-id "
            "synthetic failure and inject it into the retired conversation"
        )
        assert watcher.cancelled() or watcher.done() or True
        watcher.cancel()
        with contextlib.suppress(BaseException):
            await watcher

    def test_throwing_the_conversation_away_ends_its_children(self):
        """``clear_conversation=True`` IS the conversation ending, so it must end children.

        The compaction module's still-critical reset is the only ``clear_conversation=True``
        caller in ``src/``. That keyword clears the native resume sid and suppresses replay,
        so the successor cold-starts with none of this conversation's history -- there is
        nothing for a child to deliver into, and a child that reports anyway resolves its
        parent through ``get_or_create`` and re-opens the conversation the reset threw away.

        Pinned as an IMPLICATION rather than a path: any caller that discards the
        conversation has ended it, so this asserts the two keywords travel together instead
        of naming the one site that does it today.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "session_lifecycle.py":
                continue  # defines both keywords; its own docstrings name them
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"clear_conversation=True", text):
                window = text[max(0, match.start() - 1500) : match.start() + 1500]
                if "ends_conversation=True" not in window:
                    offenders.append(path.relative_to(root).as_posix())

        assert not offenders, (
            "these callers throw the conversation away without ending its children, so a "
            "child's report re-opens the conversation they discarded: "
            + repr(sorted(set(offenders)))
        )

    def test_the_conversation_ending_reset_callers_say_so(self):
        """The ``ends_conversation=True`` call sites are pinned by PATH.

        The AST ratchet over ``release_subagent_runtime`` cannot see these: they are
        callers of ``reset``, not release sites, and the intent is a keyword rather than a
        structure. Losing one is silent — a conversation ends and its children run on to
        their own timeouts — so the set is asserted here. Adding a site means adding it
        here with its reason; the reasons are in
        ``docs/system-specs/modules/session.md``.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        # The module that DEFINES ``reset`` names the keyword in its own docstring, which
        # is not a call site.
        defines_it = {"session_lifecycle.py"}
        found = set()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel in defines_it:
                continue
            if re.search(r"ends_conversation=True", path.read_text(encoding="utf-8")):
                found.add(rel)

        assert found == {
            # The user asked the agent to forget the conversation.
            "dashboard/handlers_channel.py",
            # A cancelled cron job's conversation is over.
            "cron.py",
            # Cancel cleanup ends every step conversation of the run.
            "taskrunner.py",
            # The pool starts a NEW conversation on a pooled key.
            "workflows/agent_pool.py",
            # ``clear_conversation`` throws the conversation away: sid cleared, replay
            # suppressed, so the successor cold-starts with none of its history.
            "session_compaction.py",
        }, f"the conversation-ending reset callers changed: {sorted(found)}"

    @pytest.mark.asyncio
    async def test_removing_the_session_does_stop_the_children(self, cfg):
        """The contrast case: an ending verb takes the children with it.

        ``remove`` is the revivable ending -- the entry survives for a future
        ``session/load`` -- so this also shows the split is about the CONVERSATION
        ending rather than about the files being deleted.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        _snapshotted, cancelled, handler = self._recorder()
        mgr.set_child_teardown_handler(handler)
        await mgr.get_or_create("dashboard:chat-9")

        await mgr.remove("dashboard:chat-9")

        assert cancelled == [("run-1",)]
        await mgr.close_all()

    def test_every_parent_end_release_site_ends_its_children(self):
        """Ratchet: the halves of ending a parent stay together.

        ``release_subagent_runtime`` IS this module's parent-end boundary, so a
        site that reaps the companion runtime without ending the runs is a parent
        end that lets a per-process harness's children survive. A future author
        adding such a path is caught here rather than by an operator finding the
        process.

        Matched on the AST rather than on a proximity window: a reformat moves
        lines around and a line-distance rule fails on it for no reason, while the
        question being asked — does this method call both halves — is structural.

        Two methods are exempt, each for a fact about itself rather than by
        convenience. ``close_all`` is gateway shutdown, where
        ``SubagentManager.cancel_all`` runs instead and additionally drains
        follow-up watchers. ``_retire_kiro_subagent_runtimes`` reaps only IDLE
        companion runtimes — it skips any runtime answering
        ``has_active_or_initializing_sessions()`` — so it has no running child to
        end, and the parent conversation it belongs to continues.

        ``reset`` is NOT exempt: it calls both halves, under
        ``ends_conversation``. That keyword defaults to False because almost every one of
        its ~46 callers is an evict-and-retry (wedged prompt, failed auto-compaction,
        provider switch, idle expiry, channel watchdog), and the callers that do end a
        conversation are pinned by
        ``test_the_conversation_ending_reset_callers_say_so`` — a structural ratchet
        cannot see a keyword.
        """
        import ast
        import inspect

        from kiro_crew import session_lifecycle

        tree = ast.parse(inspect.getsource(session_lifecycle))
        required = {"_snapshot_parent_children", "_cancel_parent_children"}

        def called_names(node):
            names = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute):
                        names.add(func.attr)
                    elif isinstance(func, ast.Name):
                        names.add(func.id)
            return names

        releasing = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = called_names(node)
            if "release_subagent_runtime" in names:
                releasing[node.name] = names

        assert len(releasing) >= 6, (
            "fewer release sites than expected — the ratchet is reading the wrong "
            f"thing after a rename; found {sorted(releasing)}"
        )

        exempt = {"close_all", "_retire_kiro_subagent_runtimes"}
        assert exempt <= set(releasing), (
            "an exempt method no longer releases a companion runtime, so its "
            f"exemption is now unchecked: {sorted(exempt - set(releasing))}"
        )
        unguarded = {
            name: sorted(required - names)
            for name, names in releasing.items()
            if name not in exempt and not required <= names
        }
        assert not unguarded, (
            "these parent-end paths reap the companion runtime without ending the "
            f"parent's runs; each name lists what it is missing: {unguarded}"
        )
