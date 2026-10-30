"""Tests for session manager.

The SessionManager core: ``get_or_create`` create and reuse under the per-key semaphore, the
reinjection and first-turn-history flags, the overflow recycle, the warm pool and the background
session's first use, and the stateless pool and heartbeat keys. More SessionManager tests sit beside
it in test_session_get_or_create.py, test_session_turn_control.py,
test_session_ending_verbs.py, test_session_compaction.py, test_session_pool_and_sweeps.py,
test_session_shared_runtimes.py, test_session_parent_end_verbs.py and
test_session_parent_end_teardown.py; the fixtures and doubles they share are in session_helpers.py.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _empty_provider_stream,
    _mock_provider_factory,
    _raw_sid,
    cfg_fixture,
)

from kiro_crew.acp.types import AcpPromptStats
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session import BACKGROUND_KEY, CONSOLIDATE_KEY, HEARTBEAT_KEY, SessionManager
from kiro_crew.start_priority import StartPriority
from kiro_crew.testing.wait import await_until

#: Ceiling for a wait that only fails on a hang: these steps finish in
#: milliseconds, so a test fails by name instead of losing the xdist worker.
_HANG_CEILING_S = 30


async def _completed_none():
    """An awaitable answering None, for a store cancel that found nothing."""
    return None


def _alive_provider_factory():
    """Like _mock_provider_factory but with an explicit live process check, so
    the fast-path session-reuse branch (which gates on is_process_alive) treats
    the session as alive instead of relying on AsyncMock attribute truthiness."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.memory_mode = kwargs.get("memory_mode", "persistent")
        m.shutdown = AsyncMock()
        m.disown_work_dir = MagicMock()
        m.is_process_alive = lambda: True
        m.is_alive = lambda: True
        m.context_usage_pct = lambda: 0.0
        m.context_window_tokens = lambda: 0
        m.has_active_turn = lambda: False
        m.runtime_abort_target = lambda: None
        m.stream_command = MagicMock(side_effect=_empty_provider_stream)
        m.spawn_identity = ""
        m._runtime = None
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
    async def test_release_refreshes_liveness(self, cfg):
        """Release marks the end of a live turn, not the start of idleness.

        A backdated session released after work must read fresh again, so the
        idle sweep measures from when the session went quiet rather than when
        it was acquired and a run working between tasks is not reaped mid-run.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr._sessions["thread1"].last_used = time.monotonic() - 9999
        mgr.release("thread1")
        assert mgr._sessions["thread1"].last_used > time.monotonic() - 5
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
    async def test_first_turn_history_owed_reads_without_clearing(self, cfg):
        """The FRESH first-turn debt is READ, not consumed, each turn.

        A pre-output failure the transient path re-queues onto the same live
        session reads it on the next turn too, so a read-and-clear here would let
        the first (failing) turn spend the debt and the replay would still go out
        bare. Only ``consume`` clears it, mirroring the replay lease.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        assert mgr.first_turn_history_owed_pending("thread1") is False, "unset by default"
        assert mgr.mark_first_turn_history_owed("thread1") is True
        assert mgr.first_turn_history_owed_pending("thread1") is True, "read 1 sees it"
        assert mgr.first_turn_history_owed_pending("thread1") is True, "read 2 still sees it"
        assert mgr.consume_first_turn_history_owed("thread1") is True, "consume clears it"
        assert mgr.first_turn_history_owed_pending("thread1") is False, "cleared"
        assert mgr.consume_first_turn_history_owed("thread1") is False, "already clear"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_first_turn_history_owed_tolerates_an_unknown_key(self, cfg):
        """A settle can fire for a session that has since been evicted; the
        helpers answer the safe default and never raise."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr.first_turn_history_owed_pending("never-existed") is False
        assert mgr.mark_first_turn_history_owed("never-existed") is False
        assert mgr.consume_first_turn_history_owed("never-existed") is False
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_first_turn_history_owed_is_independent_of_the_replay_lease(self, cfg):
        """The two debts are separate fields: arming one leaves the other alone.

        The replay lease also drives SID preservation in ``close_all``; keeping
        the FRESH first-turn debt on its own field is what leaves that role
        untouched.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("thread1")
        mgr.release("thread1")

        mgr.mark_first_turn_history_owed("thread1")
        assert mgr.provider_switch_replay_pending("thread1") is False
        mgr.consume_first_turn_history_owed("thread1")

        mgr.mark_provider_switch_replay("thread1")
        assert mgr.first_turn_history_owed_pending("thread1") is False
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
        mgr._session_map.set(key, "sid-overflowed")
        await mgr._recycle_held(key, session, 95.0)
        provider.shutdown.assert_awaited_once()
        assert not _raw_sid(mgr, key)
        # A deleted entry would answer "" here as well; the stash proves it was kept.
        assert mgr._session_map.get_discarded_sid(key) == "sid-overflowed"
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
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing window for the same-key caller to park on the held semaphore; kept in place and tracked for a de-flake pass
        await asyncio.sleep(0.1)
        assert not a2.done()  # correctly waiting on A's semaphore

        # With self._lock pinned by a2 (buggy) this hangs; with the fix a2
        # released the lock before blocking, so B cold-starts freely.
        b = asyncio.create_task(mgr.get_or_create("B"))
        provider_b, is_new_b, _ = await await_until(b, what="B cold-starting")
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
            # Sync on the real provider; the won-race arm calls it on the loser.
            m.disown_work_dir = MagicMock()
            m.is_process_alive = lambda: True
            m.is_alive = lambda: True
            m.context_usage_pct = lambda: 0.0
            return m

        mgr = SessionManager(cfg, provider_factory=factory)

        # Both pass the fast path (no existing session) and park in start().
        c1 = asyncio.create_task(mgr.get_or_create("A"))
        c2 = asyncio.create_task(mgr.get_or_create("A"))
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing window for both cold starts to park in start before the gate opens; kept in place and tracked for a de-flake pass
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
        provider_b, is_new_b, _ = await await_until(b, what="B cold-starting")
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
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing window for the second claim to park on the held semaphore; kept in place and tracked for a de-flake pass
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore, as intended
        mgr.release("A")  # let the first holder's turn "finish"
        provider, _, _ = await await_until(second, what="the serialized claim landing")
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
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing window for caller 2 to park on the held semaphore; kept in place and tracked for a de-flake pass
        await asyncio.sleep(0.2)
        assert not second.done()  # blocked on A's semaphore behind caller 1

        # A's process dies between claim and acquire.
        p1.is_process_alive = lambda: False
        p1.is_alive = lambda: False

        mgr.release("A")  # caller 1's turn ends; caller 2 acquires + re-validates
        p2, is_new2, _ = await await_until(second, what="the re-validated claim landing")

        assert p2 is not p1  # cold-started a fresh provider
        assert is_new2 is True  # reported as new
        p1.shutdown.assert_awaited()  # dead provider was reaped
        mgr.release("A")


class TestWarmPool:
    """Tests for warm session pool and background session."""

    @pytest.mark.asyncio
    async def test_start_pool_defers_background_on_a_runtime_backend(self, cfg):
        """Boot spawns no idle background agent on the default backend; first use does."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        assert mgr._bg_backend_supports_runtime()
        await mgr.start_pool()
        assert BACKGROUND_KEY not in mgr._sessions

        await mgr.get_or_create(BACKGROUND_KEY)
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_background_first_use_with_the_background_agent(self, cfg, monkeypatch):
        """Consolidation names the background agent; it must still go through
        ``_ensure_background`` rather than the generic cold start."""
        from kiro_crew.session import BACKGROUND_AGENT

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        assert BACKGROUND_KEY not in mgr._sessions

        calls: list[object] = []
        real_ensure = mgr._ensure_background

        async def _spy(**kwargs):
            calls.append(kwargs)
            await real_ensure(**kwargs)

        monkeypatch.setattr(mgr, "_ensure_background", _spy)
        await mgr.get_or_create(BACKGROUND_KEY, agent=BACKGROUND_AGENT)
        assert calls, "first use with the background agent skipped _ensure_background"
        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_background_first_use_by_a_custom_agent(self, cfg):
        """The task runner's lesson extraction asks for ``_bg`` with the run's own
        agent. When it is the first user, ``_bg`` must still be created as the
        background agent: the session is shared and persistent, so every later
        background job would otherwise run on the custom agent."""
        from kiro_crew.session import BACKGROUND_AGENT

        created: list[object] = []
        inner = _mock_provider_factory()

        def factory(session_key=None, agent=None, **kwargs):
            if session_key == BACKGROUND_KEY:
                created.append(agent)
            return inner(session_key, agent, **kwargs)

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.start_pool()
        assert BACKGROUND_KEY not in mgr._sessions

        await mgr.get_or_create(BACKGROUND_KEY, agent="my-custom-agent")
        mgr.release(BACKGROUND_KEY)

        assert created == [BACKGROUND_AGENT]
        assert mgr._sessions[BACKGROUND_KEY].agent == BACKGROUND_AGENT
        await mgr.close_all()

    @staticmethod
    def _gated_bg_factory(monkeypatch, gates):
        """A factory whose ``_bg`` providers block in ``start`` on ``gates[i]``.

        Returns ``(factory, starts, started)``: ``starts`` lists each ``_bg``
        provider as its start begins, ``started[i]`` is set when the i-th start
        begins. ``warm_agent_specs`` is stubbed so the path to ``start`` stays on
        the loop, and yielding the loop is enough for a second caller to reach
        its own start if nothing serializes it.
        """
        import kiro_crew.session_background as session_background

        async def _no_warm(**_kwargs):
            return None

        monkeypatch.setattr(session_background, "warm_agent_specs", _no_warm)
        starts: list[object] = []
        started = [asyncio.Event() for _ in gates]
        inner = _mock_provider_factory()

        def factory(session_key=None, agent=None, **kwargs):
            provider = inner(session_key, agent, **kwargs)
            if session_key == BACKGROUND_KEY:

                async def _start():
                    index = len(starts)
                    starts.append(provider)
                    started[index].set()
                    await gates[index].wait()

                provider.start = _start
            return provider

        return factory, starts, started

    @pytest.mark.asyncio
    async def test_concurrent_first_uses_start_one_background_session(self, cfg, monkeypatch):
        """Two first-time ``_bg`` users at once cold-start ONE process; the second
        waits for the first and reuses its session."""
        gate = asyncio.Event()
        factory, starts, started = self._gated_bg_factory(monkeypatch, [gate, gate])
        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.start_pool()
        assert BACKGROUND_KEY not in mgr._sessions

        first = asyncio.create_task(mgr._ensure_background())
        await asyncio.wait_for(started[0].wait(), _HANG_CEILING_S)
        second = asyncio.create_task(mgr._ensure_background())
        for _ in range(50):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), _HANG_CEILING_S)

        assert len(starts) == 1, f"{len(starts)} background cold starts ran at once"
        assert mgr._sessions[BACKGROUND_KEY].provider is starts[0]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_foreground_first_use_does_not_wait_behind_a_background_start(
        self, cfg, monkeypatch
    ):
        """A person waiting on ``_bg`` must not queue behind a background-priority
        cold start: the foreground caller runs its own start and returns while the
        background one is still in flight."""
        background_gate, foreground_gate = asyncio.Event(), asyncio.Event()
        foreground_gate.set()
        factory, starts, started = self._gated_bg_factory(
            monkeypatch, [background_gate, foreground_gate]
        )
        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.start_pool()

        background = asyncio.create_task(mgr._ensure_background())
        await asyncio.wait_for(started[0].wait(), _HANG_CEILING_S)
        await asyncio.wait_for(
            mgr._ensure_background(start_priority=StartPriority.FOREGROUND), _HANG_CEILING_S
        )

        assert not background.done()
        assert mgr._sessions[BACKGROUND_KEY].provider is starts[1]
        background_gate.set()
        await asyncio.wait_for(background, _HANG_CEILING_S)
        assert mgr._sessions[BACKGROUND_KEY].provider is starts[1]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_start_pool_keeps_the_boot_start_without_a_shared_runtime(self, cfg, monkeypatch):
        """Backends that route every background call through ``_bg`` start it at boot."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        monkeypatch.setattr(mgr, "_bg_backend_supports_runtime", lambda: False)
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
    async def test_a_cold_start_captures_the_store_it_supersedes(self, cfg):
        """The crew log a cold-started session supersedes is captured by the
        allocation itself -- inside the registration's critical section, before the
        new sid is mapped -- and read back after the claim. A caller reading the
        mapping around its own ``get_or_create`` can be suspended inside the
        allocation while a concurrent turn allocates and recycles an intermediate
        session, and would then cite the store before that one. The capture reads
        the mapping's live id or the stash a recycle leaves, follows every cold start,
        and is what a warm claim reads back too."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        key = "discord:kirocrew:direct:7:gen1"
        assert mgr.allocation_predecessor(key) == ""

        # The conversation served ``sid-p0`` and a failed compaction recycled it:
        # the pointer is emptied in place and the id stashed.
        mgr._session_map.set(key, "sid-p0")
        assert mgr._session_map.clear_sid(key) is True
        provider1, is_new, _ = await mgr.get_or_create(key)
        assert is_new is True
        assert mgr.allocation_predecessor(key) == "sid-p0"
        # A warm claim reads back what its live session was registered with.
        mgr.release(key)
        provider_again, is_new_again, _ = await mgr.get_or_create(key)
        assert provider_again is provider1 and is_new_again is False
        assert mgr.allocation_predecessor(key) == "sid-p0"
        mgr.release(key)

        # The successor was mapped and then recycled in turn: the next cold start
        # cites IT, not the store before it.
        mgr._session_map.set(key, "sid-p1")
        mgr._sessions.pop(key)
        assert mgr._session_map.clear_sid(key) is True
        provider2, is_new2, _ = await mgr.get_or_create(key)
        assert is_new2 is True and provider2 is not provider1
        assert mgr.allocation_predecessor(key) == "sid-p1"
        mgr.release(key)
        # The stamp lives on the session, so its teardown releases it: nothing keyed
        # by session key outlives the session (a ``/new`` or generation rotation
        # mints a fresh key every time, and a table of them would only ever grow).
        await mgr.close_all()
        assert mgr.allocation_predecessor(key) == ""
        assert not hasattr(mgr._allocation_boundary().state, "allocation_predecessors")

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
        await mgr._ensure_background()

        mgr._sessions[BACKGROUND_KEY].last_used = time.monotonic() - 9999
        await mgr._expire_idle(1)

        assert BACKGROUND_KEY in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_consolidation_expires_and_recreates_without_resume(self, cfg):
        factory = MagicMock(side_effect=_mock_provider_factory())
        mgr = SessionManager(cfg, provider_factory=factory)
        mgr._session_map.get = MagicMock(return_value="stale-sid")
        try:
            assert CONSOLIDATE_KEY not in mgr._sessions
            first, _, resumed = await mgr.get_or_create(CONSOLIDATE_KEY, agent="kirocrew-lite")
            assert not resumed
            mgr.release(CONSOLIDATE_KEY)
            assert _raw_sid(mgr, CONSOLIDATE_KEY) is None
            mgr._sessions[CONSOLIDATE_KEY].last_used = time.monotonic() - 9999
            await mgr._expire_idle(1)
            assert CONSOLIDATE_KEY not in mgr._sessions
            first.shutdown.assert_awaited_once()

            second, is_new, resumed = await mgr.get_or_create(
                CONSOLIDATE_KEY, agent="kirocrew-lite"
            )
            mgr.release(CONSOLIDATE_KEY)
            assert second is not first
            assert is_new and not resumed
            assert _raw_sid(mgr, CONSOLIDATE_KEY) is None
            assert factory.call_count == 2
            mgr._session_map.get.assert_not_called()
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_active_consolidation_survives_idle_sweep(self, cfg):
        from kiro_crew.llm_helpers import background_turn

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        try:
            async with background_turn(
                mgr, task="consolidation", agent="kirocrew-lite", session_key=CONSOLIDATE_KEY
            ) as provider:
                provider.last_prompt_stats = AcpPromptStats()
                mgr._sessions[CONSOLIDATE_KEY].last_used = time.monotonic() - 9999
                await mgr._expire_idle(1)
                assert mgr._sessions[CONSOLIDATE_KEY].provider is provider
                provider.shutdown.assert_not_awaited()
                # _bg must not queue behind the held consolidation semaphore; the
                # ceiling only turns a regression's hang into a named failure.
                await asyncio.wait_for(mgr.get_or_create(BACKGROUND_KEY), 30)
                mgr.release(BACKGROUND_KEY)
            # The finished turn stamped its clock; age it again so the next
            # sweep sees an idle session it is now free to reclaim.
            mgr._sessions[CONSOLIDATE_KEY].last_used = time.monotonic() - 9999
            await mgr._expire_idle(1)
            assert CONSOLIDATE_KEY not in mgr._sessions
            provider.shutdown.assert_awaited_once()
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_session_not_expired_by_idle(self, cfg):
        """Channel-agent sessions survive idle expiry (managed by channel lifecycle)."""
        import time

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.start_pool()
        await mgr._ensure_background()

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
    async def test_start_pool_idempotent(self, cfg, monkeypatch):
        """Calling start_pool() twice is a no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # A backend without a shared runtime, so the boot start is observable.
        monkeypatch.setattr(mgr, "_bg_backend_supports_runtime", lambda: False)
        await mgr.start_pool()
        first = mgr._sessions[BACKGROUND_KEY]

        await mgr.start_pool()  # should be no-op
        assert mgr._sessions[BACKGROUND_KEY] is first
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
        mgr._session_map.set_native_fork("wf-pool:run-1:0", "src-sid", "msg-1")
        await mgr.get_or_create("wf-pool:run-1:0")
        mgr._session_map.get.assert_not_called()
        # The history-restoring fork is read only on the same branch, so it is left unspent.
        assert mgr._session_map.get_native_fork("wf-pool:run-1:0") == ("src-sid", "msg-1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_non_pool_key_still_consults_resume_lookup(self, cfg):
        """Control: a normal conversational key (not stateless) DOES consult the
        session_map for a resume sid — proving the skip above is specific to the
        stateless classification, not a blanket no-op."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value=None)  # type: ignore[method-assign]
        mgr._session_map.set_native_fork("dashboard:chat-9", "src-sid", "msg-1")
        await mgr.get_or_create("dashboard:chat-9")
        assert "dashboard:chat-9" in [c.args[0] for c in mgr._session_map.get.call_args_list]
        # With no sid to resume, the stored fork is consumed by this one cold start.
        assert mgr._session_map.get_native_fork("dashboard:chat-9") is None
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

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param(HEARTBEAT_KEY, id="heartbeat_key_skips_resume_lookup"),
            # Control: ``_hb`` is stateless the same way ``_bg`` is.
            pytest.param(BACKGROUND_KEY, id="background_key_also_stateless"),
        ],
    )
    @pytest.mark.asyncio
    async def test_persistent_key_skips_resume_lookup(self, cfg, key):
        """Neither key may consult the session_map for a resume sid."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._session_map.get = MagicMock(return_value="stale-sid")  # type: ignore[method-assign]
        mgr._session_map.set_native_fork(key, "src-sid", "msg-1")
        await mgr.get_or_create(key)
        mgr._session_map.get.assert_not_called()
        assert mgr._session_map.get_native_fork(key) == ("src-sid", "msg-1")
        await mgr.close_all()
