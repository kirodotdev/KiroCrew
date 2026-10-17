"""The idle sweep must consult the attached-sub-agent probe before a reset.

``_rss_threshold_check`` already does: a free semaphore only proves the
parent's own turn is over, and sub-agents dispatched by that turn keep running
on the parent's runtime, so a reset discards their work. ``_expire_idle``
opened with the same semaphore check and reset without asking the probe, on
both its branches. The orphan branch is the reachable one: it ignores the
clock, so a parent whose tab closed while its background sub-agents were still
running was reaped on the very next sweep.

These tests install a probe that answers "attached" and pin that neither branch
resets the session, that a probe which raises keeps the session (fail closed,
as on the RSS path), and that a probe answering "none" still lets the sweep
expire the session so the guard does not turn into a leak.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _mock_provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.has_active_turn = lambda: False
        return m

    return factory


async def _idle_parent(cfg) -> SessionManager:
    """A parent whose own turn is over (permit released) and looks idle."""
    mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
    await mgr.get_or_create("dashboard:tab1")
    mgr.release("dashboard:tab1")
    async with mgr._lock:
        mgr._sessions["dashboard:tab1"].last_used = time.monotonic() - 10_000
    return mgr


class TestIdleSweepAttachedSubagentGuard:
    @pytest.mark.asyncio
    async def test_idle_parent_with_attached_subagents_is_kept(self, cfg) -> None:
        mgr = await _idle_parent(cfg)
        asked: list[str] = []

        def probe(key: str) -> bool:
            asked.append(key)
            return True

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert asked == ["dashboard:tab1"], "the sweep never asked the probe"
        assert "dashboard:tab1" in mgr._sessions, "reaped a parent with sub-agents running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_orphaned_parent_with_attached_subagents_is_kept(self, cfg) -> None:
        """The orphan branch ignores the clock, so it needs the guard most."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab2"})  # tab1 looks orphaned
        mgr.set_subagent_probe(lambda key: True)

        await mgr._expire_idle(9999)

        assert (
            "dashboard:tab1" in mgr._sessions
        ), "reaped an orphaned parent with sub-agents running"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_awaitable_probe_answer_is_awaited(self, cfg) -> None:
        """The dashboard installs a coroutine probe; its answer must be awaited, not bool()-ed."""
        mgr = await _idle_parent(cfg)

        async def probe(key: str) -> bool:
            return True

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_raising_probe_keeps_the_session(self, cfg) -> None:
        """A probe that cannot see the children is not a session with none."""
        mgr = await _idle_parent(cfg)

        def probe(key: str) -> bool:
            raise RuntimeError("task store unavailable")

        mgr.set_subagent_probe(probe)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions, "a raising probe must fail closed"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_idle_parent_without_subagents_still_expires(self, cfg) -> None:
        """The guard must not turn the sweep into a leak."""
        mgr = await _idle_parent(cfg)
        mgr.set_subagent_probe(lambda key: False)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" not in mgr._sessions, "an idle parent with no children must expire"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_idle_reset_is_pinned_to_the_incarnation_the_probe_judged(self, cfg) -> None:
        """The probe suspends on the idle axis too, so the key can change hands.

        A key-only idle reset would then shut down the replacement's runtime on
        a verdict reached about its predecessor. The reset must be pinned to the
        session that was scanned and decline when another one holds the key.
        """
        mgr = await _idle_parent(cfg)
        judged = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def replace_the_session(key: str) -> bool:
            await asyncio.sleep(0)
            mgr._sessions.pop("dashboard:tab1", None)
            await mgr.get_or_create("dashboard:tab1")
            mgr.release("dashboard:tab1")
            return False

        mgr.set_subagent_probe(replace_the_session)

        await mgr._expire_idle(timeout_secs=1)

        assert "dashboard:tab1" in mgr._sessions, "a fresh incarnation lost its runtime"
        assert mgr._sessions["dashboard:tab1"] is not judged
        assert expired == [], "the replacement's transcript was consolidated on a stale verdict"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_reopened_slot_keeps_replacement_owned_after_incarnation_mismatch(
        self, cfg
    ) -> None:
        """A live slot's fresh claim survives a stale orphan verdict."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "tab1"
        await mgr.get_or_create(key)
        mgr.release(key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        judged = mgr._sessions[key]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def reopen_and_replace(session_key: str) -> bool:
            await asyncio.sleep(0)
            mgr.set_active_dashboard_slots({key})
            mgr._sessions.pop(key)
            await mgr.get_or_create(key)
            mgr.release(key)
            return False

        mgr.set_subagent_probe(reopen_and_replace)

        await mgr._expire_idle(timeout_secs=9999)

        replacement = mgr._sessions[key]
        assert replacement is not judged
        assert expired == [], "the replacement's transcript was consolidated"
        assert replacement.provider.shutdown.await_count == 0, "the replacement was reset"
        assert key in mgr._cleanup_boundary().state.slot_owned_keys

        mgr.set_active_dashboard_slots(set())
        mgr.set_subagent_probe(lambda session_key: False)
        await mgr._expire_idle(timeout_secs=9999)

        assert key not in mgr._sessions, "the replacement was not reaped after its slot closed"
        assert expired == [key]
        await mgr.close_all()


class TestIdleSweepRejudgesAfterTheProbe:
    """The probe await sits before the expiry side effects, on both axes.

    ``on_session_expire`` consolidates the transcript. A turn that begins inside
    the await has already flushed its user row into that transcript, so a
    consolidation on the stale idle verdict marks an unanswered prompt as done
    even though ``reset`` then declines on the busy semaphore. The sweep must
    re-judge the candidate after the probe and stop BEFORE the callback.
    """

    @pytest.mark.asyncio
    async def test_a_turn_that_begins_inside_the_probe_stops_the_idle_expiry(self, cfg) -> None:
        mgr = await _idle_parent(cfg)
        sess = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def a_turn_starts(key: str) -> bool:
            await asyncio.sleep(0)
            await sess.semaphore.acquire()  # the turn is now in flight
            sess.last_used = time.monotonic()
            return False

        mgr.set_subagent_probe(a_turn_starts)

        await mgr._expire_idle(timeout_secs=1)

        assert expired == [], "consolidated a transcript with a turn in flight"
        assert mgr._sessions.get("dashboard:tab1") is sess
        sess.semaphore.release()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_turn_that_completes_inside_the_probe_stops_the_idle_expiry(self, cfg) -> None:
        """The semaphore is free again, but ``last_used`` says the session is not idle now."""
        mgr = await _idle_parent(cfg)
        sess = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def a_turn_completes(key: str) -> bool:
            await asyncio.sleep(0)
            sess.last_used = time.monotonic()
            return False

        mgr.set_subagent_probe(a_turn_completes)

        await mgr._expire_idle(timeout_secs=1)

        assert expired == [], "consolidated a session that was just used"
        assert mgr._sessions.get("dashboard:tab1") is sess
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_held_permit_alone_stops_the_idle_expiry(self, cfg) -> None:
        """A turn that took the permit inside the probe WITHOUT bumping ``last_used``.

        The clock re-read cannot see it, so the idle axis relies on the
        semaphore re-read directly rather than transitively through the clock.
        """
        mgr = await _idle_parent(cfg)
        sess = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def a_permit_is_taken(key: str) -> bool:
            await asyncio.sleep(0)
            await sess.semaphore.acquire()  # last_used deliberately left stale
            return False

        mgr.set_subagent_probe(a_permit_is_taken)

        await mgr._expire_idle(timeout_secs=1)

        assert expired == [], "consolidated an idle session with a turn in flight"
        assert mgr._sessions.get("dashboard:tab1") is sess
        sess.semaphore.release()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_injection_that_begins_inside_the_probe_stops_the_idle_expiry(
        self, cfg
    ) -> None:
        """The injection counter re-read guards the idle axis too, not only the orphan one.

        An injection commits its turn before acquiring the session, so inside
        the probe the permit stays free and ``last_used`` stays stale: the
        semaphore and clock re-reads both still say "idle". Only the counter
        re-read sees it.
        """
        mgr = await _idle_parent(cfg)
        sess = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append
        injecting = {"dashboard:tab1": 0}
        mgr.set_injection_probe(lambda k: injecting.get(k, 0) > 0)

        async def an_injection_starts(key: str) -> bool:
            await asyncio.sleep(0)
            injecting[key] = 1  # permit and last_used deliberately untouched
            return False

        mgr.set_subagent_probe(an_injection_starts)

        await mgr._expire_idle(timeout_secs=1)

        assert expired == [], "consolidated an idle session with an injection committed to it"
        assert mgr._sessions.get("dashboard:tab1") is sess
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_turn_that_begins_inside_the_probe_stops_the_orphan_expiry(self, cfg) -> None:
        """The orphan axis ignores the clock, so the semaphore re-check is its guard."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:tab1")
        mgr.release("dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab2"})  # tab1 looks orphaned
        sess = mgr._sessions["dashboard:tab1"]
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def a_turn_starts(key: str) -> bool:
            await asyncio.sleep(0)
            await sess.semaphore.acquire()
            return False

        mgr.set_subagent_probe(a_turn_starts)

        await mgr._expire_idle(9999)

        assert expired == [], "consolidated an orphan with a turn in flight"
        assert mgr._sessions.get("dashboard:tab1") is sess
        sess.semaphore.release()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_untouched_candidate_still_expires_after_the_probe(self, cfg) -> None:
        """The re-judgement must not turn the sweep into a leak."""
        mgr = await _idle_parent(cfg)
        expired: list[str] = []
        mgr.on_session_expire = expired.append

        async def nothing_happens(key: str) -> bool:
            await asyncio.sleep(0)
            return False

        mgr.set_subagent_probe(nothing_happens)

        await mgr._expire_idle(timeout_secs=1)

        assert expired == ["dashboard:tab1"]
        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()


class TestProbeFailureVisibility:
    """Fail-closed must not be silent: a broken probe holds every reap.

    A probe that raises keeps every candidate on the idle sweep and the RSS
    recycle alike, so a probe broken system-wide disables both without
    reaping a thing. That surfaces at WARNING -- but bounded to one line per
    interval across all keys, never one per candidate per tick, which is the
    noise a debug-only line was avoiding.
    """

    @pytest.mark.asyncio
    async def test_a_raising_probe_warns_once_per_interval_not_per_sweep(self, cfg, caplog) -> None:
        mgr = await _idle_parent(cfg)
        await mgr.get_or_create("dashboard:tab2")
        mgr.release("dashboard:tab2")
        async with mgr._lock:
            mgr._sessions["dashboard:tab2"].last_used = time.monotonic() - 10_000

        def probe(key: str) -> bool:
            raise RuntimeError("task store unavailable")

        mgr.set_subagent_probe(probe)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._expire_idle(timeout_secs=1)
            await mgr._expire_idle(timeout_secs=1)

        warned = [r for r in caplog.records if "work probe failed" in r.message]
        assert len(warned) == 1, "two candidates over two sweeps must warn exactly once"
        assert warned[0].levelno == logging.WARNING
        assert "held until it answers again" in warned[0].message
        assert set(mgr._sessions) >= {"dashboard:tab1", "dashboard:tab2"}, "must still fail closed"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_warning_repeats_once_the_interval_has_passed(self, cfg, caplog) -> None:
        """A persistent break stays visible in a long-running log, not only at its onset."""
        mgr = await _idle_parent(cfg)

        def probe(key: str) -> bool:
            raise RuntimeError("task store unavailable")

        mgr.set_subagent_probe(probe)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._expire_idle(timeout_secs=1)
            state = mgr._cleanup_state_boundary()
            assert state.probe_failure_warned_at is not None
            state.probe_failure_warned_at -= (
                mgr._cleanup_boundary().PROBE_FAILURE_WARN_INTERVAL_SECS
            )
            await mgr._expire_idle(timeout_secs=1)

        warned = [r for r in caplog.records if "work probe failed" in r.message]
        assert len(warned) == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_probe_that_answers_leaves_the_log_quiet(self, cfg, caplog) -> None:
        mgr = await _idle_parent(cfg)
        mgr.set_subagent_probe(lambda key: True)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._expire_idle(timeout_secs=1)

        assert not [r for r in caplog.records if "work probe failed" in r.message]
        await mgr.close_all()
