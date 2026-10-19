"""A wave digest must fire exactly once, even around done-but-unreported members.

``batch_members_pending()`` stops counting a member the moment ``info.done``
flips, but that member's contribution to the consumer's ``bp["done"]`` only
lands when its (shielded, possibly slow) terminal report actually reaches the
completion consumer. In that window a SIBLING completion sees
``done < total`` with no pending members, so the last-member fallback
finalized the wave early — and the in-flight report then re-created the
batch-progress record via ``setdefault`` and finalized the same wave a second
time. Reachable with two RUNNING members alone: member A
done-but-unreported while member B's report executes the consumer.

The fix holds the fallback open while ``batch_reports_in_flight()`` — any
registered member with ``done`` flipped whose report has not yet been consumed
— and the consumer clears the flag in the same synchronous block that lands
the done-count, so the flag and the count can never be observed apart.

These tests drive the REAL ``_subagent_done`` closure (captured from
``_init_subagents`` exactly like the cron-injection suite) with a scripted
manager, so the race window is deterministic instead of a timing lottery.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.types import STOP_CLASS_CANCELLED, STOP_REASON_CANCELLED, STOP_REASON_END_TURN
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import await_until


def _mk_real_manager(**kwargs):  # type: ignore[no-untyped-def]
    """A REAL ``SubagentManager`` (no scripted seams) for the run-side tests.

    Sessions double carries only the seams ``_run``'s terminal paths touch;
    every process boundary stays stubbed — nothing here launches a runtime.
    """
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    kwargs.setdefault("sessions", sessions)
    kwargs.setdefault("ctx_builder", None)
    return SubagentManager(**kwargs)  # type: ignore[arg-type]


def _build_gw():  # type: ignore[no-untyped-def]
    """Minimal gateway whose captured ``on_done`` closure is the unit under test.

    Same construction as ``TestCronSubagentInjection`` (test_cron_approval_mode):
    ``__new__`` plus only the attributes the consumer actually reads. Batch
    accounting additionally needs ``_batch_progress`` (normally created in
    ``__init__``) and a slack/dashboard-free routing surface so the wave digest
    lands on the patched cron-injection path.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.ctx_builder = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw._cron_injecting = {}
    gw._batch_progress = {}
    gw._cfg = MagicMock()
    gw._cfg.agent.max_subagents = 5
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    gw.sessions.cancel_current = AsyncMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.memory = MagicMock()
    gw._interactive_approval = MagicMock(return_value=AsyncMock(return_value=True))
    return gw


def _init_and_get_done_cb(gw):  # type: ignore[no-untyped-def]
    captured_done = None

    with patch("kiro_crew.slack.gateway.SubagentManager") as mock_cls:

        def capture_mgr(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal captured_done
            captured_done = kwargs["on_done"]
            mgr = MagicMock()
            mgr.running = []
            mgr.queued_count_for = MagicMock(return_value=0)
            return mgr

        mock_cls.side_effect = capture_mgr
        gw._init_subagents()

    assert captured_done is not None
    return captured_done


def _member(agent_id: str, *, total: int = 2) -> SubagentInfo:
    return SubagentInfo(
        id=agent_id,
        task=f"task {agent_id}",
        result=f"result {agent_id}",
        parent_session_key="cron:wave-parent",
        done=True,
        batch_id="w1",
        batch_total=total,
    )


def _patches(stream_rv: str = "ok"):  # type: ignore[no-untyped-def]
    return (
        patch(
            "kiro_crew.slack.gateway.stream_and_collect",
            AsyncMock(return_value=stream_rv),
        ),
        patch(
            "kiro_crew.slack.gateway.redact_exfiltration_urls",
            side_effect=lambda s: (s, False),
        ),
        patch(
            "kiro_crew.slack.gateway.redact_credentials",
            side_effect=lambda s: (s, False),
        ),
    )


class TestWaveDigestDoubleFinalize:
    def test_sibling_completion_in_the_done_but_unreported_window_does_not_close_the_wave(
        self,
    ) -> None:
        """THE RACE, scripted: member A is done-but-unreported while sibling B's
        report executes the consumer.

        ``batch_members_pending`` returns False (A's ``done`` flag has flipped,
        nothing queued) and ``batch_reports_in_flight`` returns True (A's
        contribution has not landed). Before the fix, B's event finalized the
        wave here — ``done=1 < total=2`` with the fallback tripping — and A's
        in-flight report then re-created the record and finalized it AGAIN.
        The wave must stay open until A's report is consumed, then close
        exactly once.
        """
        gw = _build_gw()
        done_cb = _init_and_get_done_cb(gw)
        gw.subagent_mgr.batch_members_pending = MagicMock(return_value=False)
        gw.subagent_mgr.batch_reports_in_flight = MagicMock(return_value=True)
        gw.subagent_mgr.finalize_batch = MagicMock()

        p1, p2, p3 = _patches()
        with p1, p2, p3:
            # Sibling B's report reaches the consumer inside A's window.
            asyncio.run(done_cb(_member("b")))

            # The wave is still open: not finalized, progress record retained,
            # B's result held for the wave-close digest.
            gw.subagent_mgr.finalize_batch.assert_not_called()
            assert "w1" in gw._batch_progress
            assert gw._batch_progress["w1"]["done"] == 1

            # A's report is finally consumed (the consumer clears the hold in
            # the same block that lands the count — modeled by the scripted
            # predicate flipping with the arrival).
            gw.subagent_mgr.batch_reports_in_flight.return_value = False
            asyncio.run(done_cb(_member("a")))

        # Exactly one finalize, and the record is gone — no second digest.
        gw.subagent_mgr.finalize_batch.assert_called_once_with("w1")
        assert "w1" not in gw._batch_progress

    def test_in_flight_report_cannot_refinalize_after_the_wave_closed(self) -> None:
        """No-double-fire control from the count side: once done reaches total,
        the wave closes on the count alone and a stray flush-only re-entry for
        the same batch does not resurrect or re-finalize it."""
        gw = _build_gw()
        done_cb = _init_and_get_done_cb(gw)
        gw.subagent_mgr.batch_members_pending = MagicMock(return_value=True)
        gw.subagent_mgr.batch_reports_in_flight = MagicMock(return_value=False)
        gw.subagent_mgr.finalize_batch = MagicMock()

        p1, p2, p3 = _patches()
        with p1, p2, p3:
            asyncio.run(done_cb(_member("a")))
            gw.subagent_mgr.batch_members_pending.return_value = False
            asyncio.run(done_cb(_member("b")))

            flush = SubagentInfo(
                id="fl1",
                task="(wave digest flush)",
                parent_session_key="cron:wave-parent",
                done=True,
                batch_id="w1",
                batch_total=2,
            )
            flush._digest_flush_only = True
            asyncio.run(done_cb(flush))

        gw.subagent_mgr.finalize_batch.assert_called_once_with("w1")
        assert "w1" not in gw._batch_progress

    def test_spawn_failure_fallback_still_closes_the_wave(self) -> None:
        """No-new-deny: the last-member fallback this fix constrains exists for
        members that failed AT SPAWN and never reach the consumer, so
        ``done`` can never hit ``total``. With nothing pending AND nothing
        in flight, a sibling completion must still close the wave."""
        gw = _build_gw()
        done_cb = _init_and_get_done_cb(gw)
        gw.subagent_mgr.batch_members_pending = MagicMock(return_value=False)
        gw.subagent_mgr.batch_reports_in_flight = MagicMock(return_value=False)
        gw.subagent_mgr.finalize_batch = MagicMock()

        p1, p2, p3 = _patches()
        with p1, p2, p3:
            asyncio.run(done_cb(_member("b")))

        gw.subagent_mgr.finalize_batch.assert_called_once_with("w1")
        assert "w1" not in gw._batch_progress

    def test_consumer_releases_the_hold_with_the_count(self) -> None:
        """The hold release and the count land in the same synchronous block:
        after the consumer accounts a member, that member does not read as an
        in-flight report (this is what lets the real predicate flip exactly
        when the scripted one did above). The release goes to the manager-level
        registry, so it survives the member having been popped from `_agents`
        by an operator clear mid-flight."""
        gw = _build_gw()
        done_cb = _init_and_get_done_cb(gw)
        gw.subagent_mgr.batch_members_pending = MagicMock(return_value=True)
        gw.subagent_mgr.batch_reports_in_flight = MagicMock(return_value=False)

        member = _member("a")
        p1, p2, p3 = _patches()
        with p1, p2, p3:
            asyncio.run(done_cb(member))
        gw.subagent_mgr.consume_report_hold.assert_called_once_with("w1", "a")

    def test_a_sibling_closing_during_the_membership_await_settles_every_named_member(
        self,
    ) -> None:
        """Member A suspends in the awaited membership lookup while sibling B
        runs the consumer to completion. Whoever closes the wave, every member
        its digest names must be in the closer's settle set: a member left
        ``_digest_held`` with no settler skips ``mark_delivered`` and is
        delivered a second time by restart reconciliation."""
        gw = _build_gw()
        done_cb = _init_and_get_done_cb(gw)
        in_flight = {"a", "b"}
        a_in_lookup = asyncio.Event()
        release_a = asyncio.Event()

        async def _pending_async(_batch_id: str) -> bool:
            if not a_in_lookup.is_set():  # the first lookup is A's
                a_in_lookup.set()
                await release_a.wait()
            return False

        gw.subagent_mgr.batch_members_pending_async = _pending_async
        gw.subagent_mgr.batch_reports_in_flight = MagicMock(side_effect=lambda _b: bool(in_flight))
        gw.subagent_mgr.consume_report_hold = MagicMock(
            side_effect=lambda _b, agent_id: in_flight.discard(agent_id)
        )
        gw.subagent_mgr.finalize_batch = MagicMock()
        a, b = _member("a"), _member("b")

        async def _drive() -> None:
            a_task = asyncio.create_task(done_cb(a))
            await await_until(a_in_lookup, what="A suspending in the membership lookup")
            await await_until(asyncio.ensure_future(done_cb(b)), what="B's consumer run")
            release_a.set()
            await await_until(a_task, what="A's consumer run")

        p1, p2, p3 = _patches()
        with p1, p2, p3:
            asyncio.run(_drive())

        gw.subagent_mgr.finalize_batch.assert_called_once_with("w1")
        assert "w1" not in gw._batch_progress
        settled = {
            d.agent_id
            for m in (a, b)
            for d in (getattr(m, "_digest_settle_deliveries", None) or [])
        }
        for m in (a, b):
            if getattr(m, "_digest_held", False) is True:
                assert m.id in settled, f"{m.id} is held with no settler"


class TestFailurePathFlipAndArmAreInseparable:
    """The done-flip and the hold-arm must never be observable apart — on the
    RUN side, not just inside the report machinery.

    The c2 revision armed the hold at the top of ``_report_terminal`` (the
    report task's first execution step). But on the failure paths
    (``_run``'s timeout / cancel / exception except-bodies) ``info.done``
    flips in the except-body, and the report task spawned by the ``finally``
    does not run until after the coroutine yields at
    ``_teardown_run_session``. At that yield a sibling completion reads the
    member as neither pending (``not a.done`` drops it) nor in flight (never
    armed) and closes the wave early; the member's report then finalizes it
    a second time — the exact double-digest the hold exists to prevent.

    These tests drive the REAL ``_run`` coroutine on a REAL manager and probe
    the registry at the real yield point (a checkpoint patched over
    ``_teardown_run_session``), so the window is checked deterministically
    where the adjudicated finding located it, not via a timing lottery.
    """

    @staticmethod
    def _mgr_and_member():  # type: ignore[no-untyped-def]
        mgr = _mk_real_manager()
        info = SubagentInfo(id="a-fail", task="t", batch_id="w9", batch_total=2)
        mgr._agents[info.id] = info
        return mgr, info

    @staticmethod
    def _checkpoint(mgr, info, seen):  # type: ignore[no-untyped-def]
        async def _teardown_probe(_info, _session_key):  # type: ignore[no-untyped-def]
            # The real run_lifecycle.py yield point: record what a sibling scheduled
            # here would observe about this member.
            seen.append((_info.done, mgr.batch_reports_in_flight(info.batch_id)))

        return _teardown_probe

    def test_exception_path_arms_in_the_same_synchronous_block(self) -> None:
        mgr, info = self._mgr_and_member()
        seen: list = []

        async def _boom(_info, _sk):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        async def _drive() -> None:
            with (
                patch.object(mgr, "_run_inner", _boom),
                patch.object(mgr, "_teardown_run_session", self._checkpoint(mgr, info, seen)),
            ):
                await mgr._run(info)

        asyncio.run(_drive())
        assert seen, "teardown checkpoint never reached"
        done_at_yield, in_flight_at_yield = seen[0]
        assert done_at_yield is True
        # The window under attack: done observable without the hold.
        assert in_flight_at_yield is True, (
            "done-but-unarmed window at the teardown yield: a sibling "
            "completion here closes the wave early and the report "
            "re-finalizes it"
        )

    def test_timeout_path_arms_in_the_same_synchronous_block(self) -> None:
        mgr, info = self._mgr_and_member()
        mgr._default_timeout = 0.001
        seen: list = []

        async def _slow(_info, _sk):  # type: ignore[no-untyped-def]
            await asyncio.Event().wait()  # parks until the run timeout cancels it

        async def _drive() -> None:
            with (
                patch.object(mgr, "_run_inner", _slow),
                patch.object(mgr, "_teardown_run_session", self._checkpoint(mgr, info, seen)),
            ):
                await mgr._run(info)

        asyncio.run(_drive())
        assert seen and seen[0] == (True, True)

    def test_cancel_path_arms_in_the_same_synchronous_block(self) -> None:
        mgr, info = self._mgr_and_member()
        info.user_stopped = True  # deterministic terminal (recovery-free) arm
        seen: list = []
        started = asyncio.Event()

        async def _hang(_info, _sk):  # type: ignore[no-untyped-def]
            started.set()
            await asyncio.Event().wait()  # parks until the test cancels it

        async def _drive() -> None:
            with (
                patch.object(mgr, "_run_inner", _hang),
                patch.object(mgr, "_teardown_run_session", self._checkpoint(mgr, info, seen)),
            ):
                run_task = asyncio.create_task(mgr._run(info))
                # Bounded: a run that fails before ``_hang`` signals fails
                # here, at this checkpoint, not at the suite's global timeout.
                await await_until(started, what="the run reaching _run_inner")
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task

        asyncio.run(_drive())
        assert seen and seen[0] == (True, True)


class TestStrandGuards:
    """An early-armed hold must always have a releaser.

    Arming at the flip (instead of inside the report body) creates one new
    hazard: a report/announce task cancelled BEFORE its first execution step
    never runs its body's structural ``finally`` release. A hold nothing
    releases pins ``batch_reports_in_flight`` forever — and the reaper's
    deadline sweep deliberately SKIPS waves with a report in flight, so even
    the last-resort exit is fenced off. The task done-callbacks own the
    rescue: by task-done time a report is not in flight on any path.
    """

    def test_report_task_cancelled_before_first_run_releases_the_hold(self) -> None:
        mgr = _mk_real_manager()
        info = SubagentInfo(id="a-strand", task="t", batch_id="w9", batch_total=2)
        mgr._agents[info.id] = info
        info.done = True
        mgr.arm_report_in_flight(info)  # the flip-site arm

        async def _drive() -> None:
            task = mgr._spawn_terminal_report(
                info,
                source="Subagent",
                injection_timeout_reason="t/o",
                mark_delivered_on_success=True,
            )
            task.cancel()  # before its first execution step
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Let the done-callback run.
            await asyncio.sleep(0)

        asyncio.run(_drive())
        assert mgr.batch_reports_in_flight("w9") is False, (
            "a report task cancelled before first run stranded the hold — "
            "the wave-close fallback and the reaper sweep are both fenced "
            "off forever"
        )

    def test_disowned_report_task_does_not_release_the_winning_holds(self) -> None:
        """The hold is keyed only by ``(batch_id, agent_id)``, so a reap/Stop
        teardown that launches a competing report for the SAME agent and then
        disowns its task must not let that dismissed task's done-callback
        release the hold the winning report still needs. ``_forget`` returns on
        ``owner is None`` BEFORE touching the hold, so a disowned task leaves
        the winner's hold intact; a sibling completion then still sees the wave
        as in flight and cannot finalize it early."""
        mgr = _mk_real_manager()
        info = SubagentInfo(id="a-win", task="t", batch_id="w9", batch_total=2)
        mgr._agents[info.id] = info
        info.done = True
        mgr.arm_report_in_flight(info)  # the winning report's hold

        async def _drive() -> None:
            task = mgr._spawn_terminal_report(
                info,
                source="Recovery",
                injection_timeout_reason="t/o",
                mark_delivered_on_success=True,
            )
            # Simulate the reap/Stop takeover: the competing path pops the
            # owner (disowns this task) before it is cancelled — exactly what
            # terminal.py does when the claim goes to the run's own report.
            mgr._report_owners.pop(task, None)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)  # let the done-callback run

        asyncio.run(_drive())
        assert mgr.batch_reports_in_flight("w9") is True, (
            "a DISOWNED report task released the winning report's hold — a "
            "sibling can now finalize the wave early and the winning report "
            "re-finalizes it"
        )

    def test_rejection_announce_cancelled_before_first_run_releases_the_hold(self) -> None:
        async def _noop_done(_info):  # type: ignore[no-untyped-def]
            return None

        mgr = _mk_real_manager(on_done=_noop_done)
        info = SubagentInfo(id="a-rej", task="t", batch_id="w9", batch_total=2)
        info.done = True
        mgr.arm_report_in_flight(info)  # the rejection flip-site arm

        async def _drive() -> None:
            mgr._announce_rejection(info)
            task = mgr._tasks[f"reject-{info.id}"]
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)

        asyncio.run(_drive())
        assert mgr.batch_reports_in_flight("w9") is False


# ── Only the flip that makes the done transition arms the hold ───────────────


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete(stop_reason: str = STOP_REASON_END_TURN) -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_COMPLETE,
        stop_reason=stop_reason,
        text="",
        title="",
        tool_input="",
        runtime_global=False,
        refusal=None,
    )


def _streaming_manager(stream_factory):  # type: ignore[no-untyped-def]
    """A REAL manager whose provider streams ``stream_factory``'s generator: the
    one seam the real ``spawn`` -> ``_run`` -> ``_run_inner`` path drives (the
    shape ``test_subagent_stop_reason_consistency`` uses)."""
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.context_used_tokens = lambda: 0
    provider.context_window_tokens = lambda: 0
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    return mgr


async def _settled(mgr: SubagentManager) -> None:
    await await_until(
        asyncio.gather(*mgr._report_tasks, return_exceptions=True),
        what="the manager's report tasks",
    )


@pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")
class TestOnlyTheTransitionArmsTheHold:
    """A flip site arms the hold only when it MAKES the done transition.

    A site reached after another path recorded the ending must not re-arm:
    that path armed at its own flip and its report releases the hold, possibly
    already, while the late site spawns no report for the claim it lost. A
    re-arm there outlives its only release: ``batch_reports_in_flight`` reads
    True until ``finalize_batch``, so a wave that needs the last-member fallback
    never closes and the reaper's deadline sweep keeps skipping it.
    """

    @pytest.mark.asyncio
    async def test_a_run_unwinding_after_its_reap_reported_leaves_no_hold(self) -> None:
        """The real run and the real reap: the reap records the ending, takes
        the claim, and its report reaches the consumer, which releases the hold.
        Only then does the run's stream end, and the run unwinds through its
        tail with ``was_done`` set."""
        streaming = asyncio.Event()
        stream_may_end = asyncio.Event()

        def factory(msg, *a, **kw):  # type: ignore[no-untyped-def]
            async def _gen():  # type: ignore[no-untyped-def]
                yield _text("partial ")
                streaming.set()
                try:
                    await asyncio.Event().wait()  # parked until the reap's cancel
                except asyncio.CancelledError:
                    # A transport that answers the cancel with a ``cancelled``
                    # completion once its backend acknowledges it, later than
                    # the reap's report: the test says when.
                    await stream_may_end.wait()
                    yield _complete(STOP_REASON_CANCELLED)

            return _gen()

        mgr = _streaming_manager(factory)
        consumed: list[str] = []

        async def _consumer(done: SubagentInfo) -> None:
            # The gateway consumer's release, in the block that lands the count.
            mgr.consume_report_hold(done.batch_id, done.id)
            consumed.append(done.id)

        mgr._on_done = _consumer
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn(
                "do work", parent_session_key="dashboard:p", batch_id="w1", batch_total=2
            )
            assert info is not None
            run = mgr._tasks[info.id]
            await await_until(streaming, what="the run streaming")
            reap = asyncio.ensure_future(mgr._force_reap(info.id, info, time.time() - info.started))
            await await_until(reap, what="the reap and its report")
            assert info.done and consumed == [info.id]
            assert mgr.batch_reports_in_flight("w1") is False
            stream_may_end.set()
            await await_until(run, what="the run unwinding through its tail")
            await _settled(mgr)

        assert info.stop_class == STOP_CLASS_CANCELLED, "the run never reached its tail"
        assert consumed == [info.id]
        assert mgr.batch_reports_in_flight("w1") is False, (
            "the run's tail re-armed a hold its reap's report had already "
            "released; nothing releases it now, so the wave can never close "
            "by its last-member fallback"
        )

    @pytest.mark.asyncio
    async def test_the_claimed_flip_does_not_rearm_an_ending_recorded_in_its_write(
        self,
    ) -> None:
        """The claimed-success flip runs after the bounded result write. No stop
        path records an ending once the claim is taken (``_force_reap`` returns
        on ``_ending_claimed``), so this models one inside that await (flip and
        arm, claim, report consumed) to pin the read-before-flip guard."""

        def factory(msg, *a, **kw):  # type: ignore[no-untyped-def]
            async def _gen():  # type: ignore[no-untyped-def]
                yield _text("THE ANSWER")
                yield _complete()

            return _gen()

        mgr = _streaming_manager(factory)
        real_write = mgr._write_finished_result_off_loop

        async def _ended_elsewhere(run_info, text, **kw):  # type: ignore[no-untyped-def]
            run_info.done = True
            mgr.arm_report_in_flight(run_info)
            assert mgr._claim_finalize(run_info)
            mgr.consume_report_hold(run_info.batch_id, run_info.id)
            return await real_write(run_info, text, **kw)

        mgr._write_finished_result_off_loop = _ended_elsewhere  # type: ignore[method-assign]
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn(
                "do work", parent_session_key="dashboard:p", batch_id="w1", batch_total=2
            )
            assert info is not None
            await await_until(mgr._tasks[info.id], what="the run")
            await _settled(mgr)

        assert info._ending_claimed
        assert (
            mgr.batch_reports_in_flight("w1") is False
        ), "the claimed flip re-armed an ending another path had already recorded and reported"

    @pytest.mark.asyncio
    async def test_a_spawn_approval_denied_after_a_stop_leaves_no_hold(self) -> None:
        """The approval rejection flip, reached after a Stop already ended the
        run (the sequence ``test_stop_during_pending_spawn_approval_reports_and_
        releases_once`` drives): the Stop's reap armed, reported and released,
        and the denial must not arm again under a claim that is gone."""
        mgr = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=4)
        mgr._fire_event = AsyncMock()  # type: ignore[method-assign]
        mgr._write_tombstone = MagicMock()  # type: ignore[method-assign]
        mgr._record_cost = MagicMock()  # type: ignore[method-assign]
        mgr._on_done = AsyncMock()
        mgr._sessions.reset = AsyncMock()
        info = SubagentInfo(
            id="a-appr",
            task="t",
            agent="",
            batch_id="w9",
            batch_total=2,
            started=time.time() - 5.0,
        )
        mgr._agents[info.id] = info
        mgr._running_count = 1
        asked = asyncio.Event()
        answered = asyncio.Event()

        async def _pending_then_denied(_rid, _preview, _parent):  # type: ignore[no-untyped-def]
            asked.set()
            await answered.wait()
            return False

        mgr._on_spawn_approval = _pending_then_denied  # type: ignore[assignment]
        approval = asyncio.ensure_future(mgr._spawn_with_approval(info))
        await await_until(asked, what="the spawn approval prompt opening")
        await mgr._force_reap(info.id, info, elapsed=1.0, reason="user_stop")
        assert info.done and mgr.batch_reports_in_flight("w9") is False
        answered.set()
        await await_until(approval, what="the denied approval unwinding")

        assert mgr.batch_reports_in_flight("w9") is False, (
            "the denied approval re-armed a hold the Stop's report had already "
            "released; its own announce is skipped (claim gone), so nothing "
            "releases it"
        )
