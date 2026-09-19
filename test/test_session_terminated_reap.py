"""Tests for reaping the runtime of a session whose owning slot is gone.

Every live session carries its own MCP server processes, so the live process
count on a host is the number of sessions times the servers each one spawns. A
session that is finished but still in the map keeps paying that cost, which on a
4 vCPU / 16 GB host is enough to saturate it.

The idle sweep expires such a session on two axes. The clock is one. The other
is that the session's owning dashboard slot is gone, and the authority for that
is ``active_dashboard_slots``, published by ``_sync_dashboard_slots``, which
carries the *effective* key of every open slot: a ``dashboard:`` key, a
channel-born slot's channel key, or a linked slot's ``linked_session_key`` such
as ``taskrunner:<id>:chat:<tok>`` or ``cron:<job>``. Without the second axis a
session of any non-``dashboard:`` shape waits out the full timeout, default 60
minutes, holding its runtime and its MCP children.

The reap may never touch a session that is live or resumable, which is why
absence from the live set is not on its own sufficient: it is equally true of a
cron fire, a task step or a hook that is running right now and never had a tab.
A key must have appeared in a published live set before its later absence means
anything. The remaining hazards are pinned below: a slot that reopens between
the scan and the reset, sub-agent work that outlives its parent's turn, and a
probe that cannot answer.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

# Far larger than any wall-clock this test spends, so every reap asserted below
# is on the terminated-owner axis and never on the idle clock.
NEVER_IDLE = 9999


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = NEVER_IDLE
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


async def _idle_session(mgr: SessionManager, key: str) -> None:
    """Create *key* and drop its permit, so only the sweep's rules decide."""
    await mgr.get_or_create(key)
    mgr.release(key)


class TestTerminatedSessionIsReaped:
    @pytest.mark.asyncio
    async def test_a_closed_tab_reaps_its_linked_session(self, cfg) -> None:
        """A non-dashboard key whose slot has closed is finished, so it goes."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})  # tab open
        mgr.set_active_dashboard_slots(set())  # tab closed

        await mgr._expire_idle(NEVER_IDLE)

        assert key not in mgr._sessions, "a finished session kept its runtime"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_cron_view_key_is_reaped_too(self, cfg) -> None:
        """A slot opened on a cron job's view contributes that job's key shape.

        The name of the shape is the point: this axis is not limited to the
        ``taskrunner:`` spelling, it covers any key a slot publishes.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        await mgr._expire_idle(NEVER_IDLE)

        assert key not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_dashboard_keys_keep_their_existing_behaviour(self, cfg) -> None:
        """A ``dashboard:`` key is slot-owned by construction, published or not.

        Pinned so the record requirement that protects other key shapes cannot
        narrow this population, which needs no record.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await _idle_session(mgr, "dashboard:tab1")
        mgr.set_active_dashboard_slots({"dashboard:tab2"})

        await mgr._expire_idle(NEVER_IDLE)

        assert "dashboard:tab1" not in mgr._sessions
        await mgr.close_all()


class TestLiveSessionsAreNeverReaped:
    @pytest.mark.asyncio
    async def test_a_session_that_never_had_a_slot_survives(self, cfg) -> None:
        """The whole safety of the change.

        A task step, a cron fire and a hook all run with no tab. They are absent
        from the live set for the entire time they are working, so reaping on
        absence alone would kill live work rather than finished work.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        step = "taskrunner:t1:task1"
        await _idle_session(mgr, step)
        mgr.set_active_dashboard_slots({"dashboard:tab1"})

        await mgr._expire_idle(NEVER_IDLE)

        assert step in mgr._sessions, "a live task step was reaped as terminated"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_channel_owned_key_is_never_recorded_or_reaped(self, cfg) -> None:
        """A tab viewing a Slack thread publishes the THREAD's key, not its own.

        Dismissing that viewer says nothing about whether the thread is
        finished, so the key must not enter the record and the session must
        survive. Reaping it would tear down the runtime of a conversation that
        is still live on the channel side.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "slack:1700000000.123456"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})

        assert (
            key not in mgr._cleanup_boundary().state.slot_owned_keys
        ), "a channel-owned key entered the slot-owned record"

        mgr.set_active_dashboard_slots(set())  # the viewer is dismissed
        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "a live channel conversation was reaped on tab close"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_fresh_incarnation_under_a_reaped_key_survives(self, cfg) -> None:
        """A recurring job's key is reused by every later fire.

        The record describes ONE incarnation: the session a slot actually
        claimed. Once that session is reaped, a fire arriving under the same key
        has never had a tab, so this axis must leave it alone. Otherwise every
        fire of a short-schedule job the user once opened cold-starts its
        runtime and respawns its MCP servers, which is the churn this reap is
        meant to reduce.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        await mgr._expire_idle(NEVER_IDLE)
        assert key not in mgr._sessions, "the incarnation a slot claimed was not reaped"

        await _idle_session(mgr, key)  # the job fires again, with no tab

        await mgr._expire_idle(NEVER_IDLE)

        assert (
            key in mgr._sessions
        ), "a fresh headless incarnation inherited the reaped key's record"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_nothing_is_reaped_before_any_live_set_is_published(self, cfg) -> None:
        """A build with no dashboard has no authority, so it reaps on this axis never."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await _idle_session(mgr, "dashboard:tab1")
        await _idle_session(mgr, "taskrunner:t1:chat:tok")

        assert mgr._cleanup_boundary().state.active_dashboard_slots is None
        await mgr._expire_idle(NEVER_IDLE)

        assert mgr.count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_open_tab_is_never_reaped(self, cfg) -> None:
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_turn_in_flight_survives_a_closed_tab(self, cfg) -> None:
        """The permit is still held, so the session has work in flight."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await mgr.get_or_create(key)  # permit deliberately held
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "a live turn was reaped as terminated"
        mgr.release(key)
        await mgr.close_all()


class TestResumeBoundary:
    @pytest.mark.asyncio
    async def test_a_slot_reopening_mid_sweep_spares_its_session(self, cfg) -> None:
        """The scan's answer goes stale while the sweep awaits.

        The candidate list is built under the lock and released before any
        reset, so a tab can reopen in between. The second key is put back into
        the live set while the FIRST key's reset is in flight, which is the only
        window that exists; reaping it on the scan's answer would tear down a
        session the user had just resumed.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        first, second = "taskrunner:a:chat:tok", "taskrunner:b:chat:tok"
        await _idle_session(mgr, first)
        await _idle_session(mgr, second)
        mgr.set_active_dashboard_slots({first, second})
        mgr.set_active_dashboard_slots(set())  # both tabs closed

        async def reset_and_reopen(key, **kwargs):
            if key == first:
                mgr.set_active_dashboard_slots({second})  # user reopens the tab
            return True

        mgr.reset = AsyncMock(side_effect=reset_and_reopen)  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        reaped = [c.args[0] for c in mgr.reset.await_args_list]
        assert reaped == [first], f"resumed session was reaped: {reaped}"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_resume_during_the_probe_await_spares_the_session(self, cfg) -> None:
        """The sub-agent probe is itself a window, and the last one.

        The probe reads the task store off the loop, so awaiting it suspends
        this sweep after any earlier check of the live set. A resume landing in
        that window must still be seen, which is only true while the live-set
        re-assert runs AFTER the probe rather than before it.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())  # tab closed

        async def probe_then_resume(k):
            await asyncio.sleep(0)
            mgr.set_active_dashboard_slots({key})  # user resumes mid-probe
            return False  # and no sub-agent work is attached

        mgr._has_attached_subagents = probe_then_resume  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "a session resumed during the probe was reaped"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_session_replaced_mid_sweep_keeps_its_runtime(self, cfg) -> None:
        """Every verdict in this sweep is about ONE incarnation.

        Two awaits separate the scan from the reset, so the key can change hands
        in between: a cron job fires again, or a tab reopens and takes a turn.
        Resetting by key alone would hand the newcomer a verdict reached about
        its predecessor, so the reset is pinned to the session that was judged.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        judged = mgr._sessions[key]

        async def replace_the_session(k):
            await asyncio.sleep(0)
            mgr._sessions.pop(key, None)
            await _idle_session(mgr, key)
            return False

        mgr._has_attached_subagents = replace_the_session  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "a fresh incarnation lost the runtime of its predecessor"
        assert mgr._sessions[key] is not judged
        assert (
            key not in mgr._cleanup_boundary().state.slot_owned_keys
        ), "the newcomer inherited a slot claim it never made"
        await mgr.close_all()


class TestSubagentWorkOutlivingItsTab:
    @pytest.mark.asyncio
    async def test_attached_subagent_work_keeps_the_session(self, cfg) -> None:
        """Children run on the parent's runtime after the parent's turn ends.

        The busy semaphore cannot see them, so the probe is the only witness
        that a closed tab still has work behind it.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        mgr._has_attached_subagents = lambda k: True  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "reaped a parent whose sub-agent was still working"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_awaitable_probe_answer_is_awaited(self, cfg) -> None:
        """The dashboard's probe reads the task store off the loop."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        async def slow_probe(k):
            await asyncio.sleep(0)
            return True

        mgr._has_attached_subagents = slow_probe  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_probe_that_cannot_answer_keeps_the_session(self, cfg) -> None:
        """Fail-closed: an unanswerable probe is not the same as 'no children'."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        def broken_probe(k):
            raise RuntimeError("task store unreadable")

        mgr._has_attached_subagents = broken_probe  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "a failed probe was read as 'no children'"
        await mgr.close_all()


class TestPendingInjectionIsSpared:
    """A turn committed to a session, before that session is acquired.

    The gateway raises its injection counter for a parent, then awaits the
    injected turn's store read before ``get_or_create``. Inside that await the
    session holds no permit, its delivering sub-agent is already ``done`` so no
    child is running, and a closed tab leaves no in-flight delivery either.
    Every other signal this sweep reads says "finished", and the reset would
    discard the runtime the injection is about to write into.
    """

    @pytest.mark.asyncio
    async def test_a_pending_injection_keeps_the_session(self, cfg) -> None:
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        mgr.set_injection_probe(lambda k: k == key)

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "reaped a session with a turn already committed to it"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_injection_answer_precedes_the_subagent_probe(self, cfg) -> None:
        """Asked first, so the cheap synchronous answer costs no await.

        Position is also what keeps it sound: the sub-agent probe suspends, and
        a read placed after it would be answering about a window that has
        already moved on.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        mgr.set_injection_probe(lambda k: True)
        asked: list[str] = []

        async def recording_probe(k):
            asked.append(k)
            return False

        mgr._has_attached_subagents = recording_probe  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions
        assert asked == [], "the sub-agent probe was consulted despite a pending injection"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_injection_probe_that_cannot_answer_keeps_the_session(self, cfg) -> None:
        """Fail-closed, the same rule the sub-agent probe follows."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        def broken_probe(k):
            raise RuntimeError("counter unreadable")

        mgr.set_injection_probe(broken_probe)

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "an unanswerable probe was read as 'nothing injecting'"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_session_is_reaped_once_the_injection_finishes(self, cfg) -> None:
        """The guard defers the reap; it does not exempt the key from it.

        Otherwise the runtime this change exists to release would be held for
        good by a counter that has already returned to zero.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        injecting = {key: 1}
        mgr.set_injection_probe(lambda k: injecting.get(k, 0) > 0)

        await mgr._expire_idle(NEVER_IDLE)
        assert key in mgr._sessions

        injecting[key] = 0
        await mgr._expire_idle(NEVER_IDLE)

        assert key not in mgr._sessions, "the deferred reap never happened"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_injection_starting_inside_the_probe_await_spares_it(self, cfg) -> None:
        """The pre-read cannot see a turn committed after it ran.

        The sub-agent probe suspends, and the injection this guard exists for
        begins exactly while a closed tab has nothing else running. So the
        counter is read again after the await, where nothing further suspends.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())
        injecting = {key: 0}
        mgr.set_injection_probe(lambda k: injecting.get(k, 0) > 0)

        async def probe_that_yields(k):
            await asyncio.sleep(0)
            injecting[key] = 1  # the gateway commits the turn mid-await
            return False

        mgr._has_attached_subagents = probe_that_yields  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert key in mgr._sessions, "reaped a session a turn was committed to mid-sweep"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_injection_starting_inside_the_rss_probe_await_spares_it(self, cfg) -> None:
        """The RSS ceiling reaches ``reset`` through the same suspending wrapper.

        So it inherits the same window, and closing it only on the idle sweep's
        orphan branch would leave the recycle able to discard a runtime a turn
        is committed to. Neither existing guard covers it: while the injection
        is in flight the session holds no semaphore, so ``skip_if_busy`` sees a
        free one, and the injected turn resolves to this same object, so
        ``expect_session`` matches.
        """
        from unittest.mock import MagicMock, patch

        import kiro_crew.session as session_mod

        cfg.session.watchdog_rss_max_mb = 1000
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        injecting = {key: 0}
        mgr.set_injection_probe(lambda k: injecting.get(k, 0) > 0)

        async def probe_that_yields(k):
            await asyncio.sleep(0)
            injecting[key] = 1  # the gateway commits the turn mid-await
            return False

        mgr._has_attached_subagents = probe_that_yields  # type: ignore[method-assign]
        recycled = AsyncMock(return_value=True)
        mgr.reset = recycled  # type: ignore[method-assign]
        mgr.get_pid = MagicMock(return_value=4242)  # type: ignore[method-assign]

        with (
            patch.object(session_mod.platform_compat, "IS_WINDOWS", False),
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await mgr._rss_threshold_check()

        recycled.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_rss_ceiling_still_recycles_when_nothing_is_injecting(self, cfg) -> None:
        """The new read must not disarm the ceiling it guards.

        Same path with the counter quiet: the victim is still recycled, so the
        guard above is a deferral and not an off switch.
        """
        from unittest.mock import MagicMock, patch

        import kiro_crew.session as session_mod

        cfg.session.watchdog_rss_max_mb = 1000
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_injection_probe(lambda k: False)
        mgr._has_attached_subagents = AsyncMock(return_value=False)  # type: ignore[method-assign]
        recycled = AsyncMock(return_value=True)
        mgr.reset = recycled  # type: ignore[method-assign]
        mgr.get_pid = MagicMock(return_value=4242)  # type: ignore[method-assign]

        with (
            patch.object(session_mod.platform_compat, "IS_WINDOWS", False),
            patch("kiro_crew.session._build_child_map", return_value={}),
            patch("kiro_crew.session._rss_mb_from_tree", return_value=2048),
        ):
            await mgr._rss_threshold_check()

        recycled.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_injection_starting_while_reset_awaits_the_lock_spares_it(self, cfg) -> None:
        """A caller's own read cannot cover the gap inside ``reset``.

        ``reset`` suspends on the registry lock before it re-validates anything,
        so an injection beginning while that lock is contended is invisible to
        every read the caller took first. ``skip_if_injecting`` asks the counter
        under that same lock, which is what makes the answer atomic with the pop.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        injecting = {key: 0}
        mgr.set_injection_probe(lambda k: injecting.get(k, 0) > 0)

        await mgr._lock.acquire()
        pending = asyncio.create_task(mgr.reset(key, skip_if_busy=True, skip_if_injecting=True))
        await asyncio.sleep(0)  # let reset reach the lock and suspend there
        injecting[key] = 1  # the gateway commits the turn inside that gap
        mgr._lock.release()

        assert await pending is False, "reset destroyed a runtime a turn was committed to"
        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_reset_that_does_not_opt_in_still_wins_over_an_injection(self, cfg) -> None:
        """The guard is opt-in, so an explicit reset is not blocked by a turn.

        Only the sweep asks for it. A user-initiated reset means the user wants
        the session gone, and deferring to an injection there would make the
        button silently do nothing.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_injection_probe(lambda k: True)

        assert await mgr.reset(key) is True
        assert key not in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_unreadable_counter_declines_the_locked_reset(self, cfg) -> None:
        """Fail closed under the lock too, and without escaping the sweep.

        An unreadable counter is an unknown turn, not the absence of one. It
        declines this one reset and is swallowed there, so a sweep with other
        candidates still visits them.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)

        def broken_probe(k):
            raise RuntimeError("counter unavailable")

        mgr.set_injection_probe(broken_probe)

        assert await mgr.reset(key, skip_if_busy=True, skip_if_injecting=True) is False
        assert key in mgr._sessions
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_the_idle_clock_also_defers_to_an_injection(self, cfg) -> None:
        """The axis that found the session says nothing about a committed turn.

        A never-tabbed ``cron:`` parent is not on the terminated-owner axis at
        all, so only the clock can elect it - and its ``last_used`` goes stale
        during a long sub-agent run, which is the same run whose completion
        injection is in flight. So the counter is read before the axis split.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_injection_probe(lambda k: k == key)

        await mgr._expire_idle(0)  # every session is idle at this timeout

        assert key in mgr._sessions, "the idle clock reaped a session mid-injection"
        await mgr.close_all()

    def test_the_gateway_installs_the_probe_over_its_counter(self) -> None:
        """The seam is worthless unless its one owner fills it.

        Read from source rather than by building a gateway: the counter is
        ``SlackGateway`` state and the install sits in the same constructor that
        mints the manager, so nothing short of that construction exercises it.
        """
        from pathlib import Path

        import kiro_crew.slack.gateway as gateway_mod

        src = Path(gateway_mod.__file__).read_text(encoding="utf-8")
        assert (
            "self.sessions.set_injection_probe(" in src
        ), "the gateway installs no injection probe"
        install = src.split("self.sessions.set_injection_probe(", 1)[1][:200]
        assert (
            "_cron_injecting" in install
        ), "the installed probe does not read the injection counter"


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_sweeps_at_once_reap_the_finished_one_only(self, cfg) -> None:
        """The periodic loop and an on-demand sweep can overlap.

        Both must agree, neither may deadlock on the registry lock, and the live
        step session must survive both passes.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        finished, live = "taskrunner:a:chat:tok", "taskrunner:b:task1"
        await _idle_session(mgr, finished)
        await _idle_session(mgr, live)
        mgr.set_active_dashboard_slots({finished})
        mgr.set_active_dashboard_slots(set())

        await asyncio.wait_for(
            asyncio.gather(mgr._expire_idle(NEVER_IDLE), mgr._expire_idle(NEVER_IDLE)),
            timeout=10,
        )

        assert finished not in mgr._sessions
        assert live in mgr._sessions, "a session that never had a slot was reaped"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_publish_during_a_sweep_does_not_lose_the_record(self, cfg) -> None:
        """A live-set publish lands while a sweep is in flight.

        The record is a union, so a publish arriving mid-sweep adds its own keys
        and drops none of the others. The key being reaped is a separate
        question: its record is released along with it, so a later incarnation
        under that same key cannot inherit a claim it never made.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        judged, other = "taskrunner:a:chat:tok", "taskrunner:b:chat:tok"
        await _idle_session(mgr, judged)
        await _idle_session(mgr, other)
        mgr.set_active_dashboard_slots({judged})
        mgr.set_active_dashboard_slots(set())

        async def publish_then_reset(key, **kwargs):
            mgr.set_active_dashboard_slots({other})
            return True

        mgr.reset = AsyncMock(side_effect=publish_then_reset)  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        recorded = mgr._cleanup_boundary().state.slot_owned_keys
        assert other in recorded, "a publish landing mid-sweep lost its own key"
        assert judged not in recorded, "the reaped key's record outlived its session"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_slot_reopening_inside_reset_keeps_its_record(self, cfg) -> None:
        """A publish inside reset's await claims the key again, and that sticks.

        This is why the release sits before the reset rather than after it
        succeeds: releasing afterwards would erase the claim the reopen just
        made, and the session the user reopened would then never be reaped on
        this axis when its tab closes for real.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:a:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        async def reopen_inside_reset(k, **kwargs):
            mgr.set_active_dashboard_slots({k})
            return True

        mgr.reset = AsyncMock(side_effect=reopen_inside_reset)  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        recorded = mgr._cleanup_boundary().state.slot_owned_keys
        assert key in recorded, "a slot reopening inside reset lost its record"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_declined_reset_keeps_the_key_on_the_axis(self, cfg) -> None:
        """A session surviving only because it was briefly busy stays eligible.

        The release runs before the reset, so a reset that DECLINES would
        otherwise strip the record from a session that is still here and still
        tab-less. That session would then sit outside this axis and hold its
        runtime until the idle clock, which is the cost the axis exists to cut
        short.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:a:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        real_reset = mgr.reset
        attempts: list[str] = []

        async def decline(k, **kwargs):
            attempts.append(k)
            return False

        mgr.reset = AsyncMock(side_effect=decline)  # type: ignore[method-assign]

        await mgr._expire_idle(NEVER_IDLE)

        assert attempts == [key], "the sweep never attempted the reset"
        assert key in mgr._sessions, "a declined reset must leave the session running"
        assert (
            key in mgr._cleanup_boundary().state.slot_owned_keys
        ), "a declined reset stripped the record from a live, tab-less session"

        # Keeping the record is only worth anything if the next sweep can act.
        mgr.reset = real_reset  # type: ignore[method-assign]
        await mgr._expire_idle(NEVER_IDLE)

        assert key not in mgr._sessions, "the key had left the terminated-owner axis"
        await mgr.close_all()


class TestRecordIsBounded:
    @pytest.mark.asyncio
    async def test_the_record_is_pruned_to_sessions_and_open_slots(self, cfg) -> None:
        """Otherwise the record grows with every tab ever opened, for uptime.

        The bound is the union of the live session map and the slots that are
        open right now, so a key with neither a session nor an open slot goes.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        present = "taskrunner:a:chat:tok"
        await _idle_session(mgr, present)
        mgr.set_active_dashboard_slots({present, "dashboard:closed-long-ago"})
        mgr.set_active_dashboard_slots({present})  # that tab has since closed

        await mgr._expire_idle(NEVER_IDLE)

        recorded = mgr._cleanup_boundary().state.slot_owned_keys
        assert recorded == {present}, f"record kept a key with no session or slot: {recorded}"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_declined_idle_reset_does_not_invent_a_record(self, cfg) -> None:
        """An idle reap that declines must not put a never-tabbed key on the axis.

        Restoring the record without asking which path released it would do
        exactly that: a cron fire no slot ever claimed would become reapable on
        the terminated-owner axis, which is the widening every safety case here
        exists to prevent.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "cron:nightly"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({"dashboard:someone-else"})

        async with mgr._lock:
            mgr._sessions[key].last_used = time.monotonic() - 10_000

        async def decline(k, **kwargs):
            return False

        mgr.reset = AsyncMock(side_effect=decline)  # type: ignore[method-assign]

        await mgr._expire_idle(timeout_secs=1)

        assert (
            key not in mgr._cleanup_boundary().state.slot_owned_keys
        ), "a declined idle reset invented a slot-owned record"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_idle_reap_under_an_open_slot_keeps_the_record(self, cfg) -> None:
        """The record is the SLOT's claim, so one session's idle death is not its end.

        A slot whose session goes idle while the tab is still open gets a fresh
        session on the next turn, and that session belongs to the same slot, so
        closing the tab later must still reap it. Releasing the record on the
        idle path would leave the slot's next session unrecorded.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:a:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})  # the tab stays open throughout

        async with mgr._lock:
            mgr._sessions[key].last_used = time.monotonic() - 10_000
        await mgr._expire_idle(timeout_secs=1)  # idle reap, owner NOT gone

        assert key not in mgr._sessions, "the idle axis stopped working"
        assert (
            key in mgr._cleanup_boundary().state.slot_owned_keys
        ), "an idle reap released the still-open slot's claim"

        await _idle_session(mgr, key)  # the next turn, same slot
        mgr.set_active_dashboard_slots(set())  # now the tab closes

        await mgr._expire_idle(NEVER_IDLE)

        assert key not in mgr._sessions, "the slot's next session escaped the axis"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_an_open_slot_keeps_its_record_before_its_session_exists(self, cfg) -> None:
        """A slot publishes its linked key before the first turn creates it.

        Pruning against the session map alone forgets the record here. The
        session then appears on the first turn, and nothing is left to say a
        slot ever claimed it, so on tab close it would sit outside this axis and
        wait out the idle clock instead.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:t1:chat:tok"
        mgr.set_active_dashboard_slots({key})  # slot open, no session yet

        await mgr._expire_idle(NEVER_IDLE)

        assert (
            key in mgr._cleanup_boundary().state.slot_owned_keys
        ), "the prune forgot an open slot whose session does not exist yet"

        await _idle_session(mgr, key)  # the first turn creates it
        mgr.set_active_dashboard_slots(set())  # the tab closes

        await mgr._expire_idle(NEVER_IDLE)

        assert (
            key not in mgr._sessions
        ), "the record was lost while the slot was open, so the session escaped the axis"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_publishing_alone_bounds_the_record(self, cfg) -> None:
        """The sweep is not guaranteed to run, so the publish bounds it too.

        ``session.timeout_secs=0`` disables the sweep, and the sweep is where the
        other prune lives, so without this the record would grow with every key
        ever published and nothing would shrink it.
        """
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        for i in range(50):
            mgr.set_active_dashboard_slots({f"taskrunner:t{i}:chat:tok"})
        mgr.set_active_dashboard_slots(set())

        assert mgr._cleanup_boundary().state.slot_owned_keys == set(), "the record grew unbounded"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_reaped_key_leaves_the_record(self, cfg) -> None:
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        key = "taskrunner:a:chat:tok"
        await _idle_session(mgr, key)
        mgr.set_active_dashboard_slots({key})
        mgr.set_active_dashboard_slots(set())

        await mgr._expire_idle(NEVER_IDLE)
        await mgr._expire_idle(NEVER_IDLE)  # second pass sees the map without it

        assert key not in mgr._cleanup_boundary().state.slot_owned_keys
        await mgr.close_all()


class TestIdleAxisIsUnchanged:
    @pytest.mark.asyncio
    async def test_a_session_with_no_slot_still_expires_on_the_clock(self, cfg) -> None:
        """The terminated-owner axis is added, not substituted for the timer."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        step = "taskrunner:t1:task1"
        await _idle_session(mgr, step)
        async with mgr._lock:
            mgr._sessions[step].last_used = time.monotonic() - 10_000

        await mgr._expire_idle(timeout_secs=1)

        assert step not in mgr._sessions, "the idle timer stopped working"
        await mgr.close_all()
