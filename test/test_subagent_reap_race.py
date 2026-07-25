"""Regression: a reaped subagent is reported EXACTLY once and frees EXACTLY one slot.

Two paths finish a subagent — ``_force_reap`` and ``_run``'s ``finally`` — and
between them there are FOUR distinct one-time concerns. Earlier revisions tried
to arbitrate them with two flags (``reaped`` and ``done``) and each attempt
satisfied two while breaking a third:

===============================  ==========================================
``reaped`` set after teardown    duplicate delivery
``reaped`` set before teardown   outcome lost if the reaper is cancelled
+ hand the claim back on cancel  outcome lost if ``_run`` already exited
separate ``_finalized`` claim    outcome lost if the claimer is cancelled
claim gated on ``not done``      NO reporter *and* a leaked slot
===============================  ==========================================

The design under test separates all of them:

* ``info.reaped``      — CLASSIFICATION (was this a deliberate reap?). The
  cancel-recovery scheduler reads it; the marker must precede the intentional
  cancel or an unexpected-cancel respawn fires.
* ``if not info.done`` — the terminal RECORD (error/stat/tombstone/cost),
  first-arrival-wins.
* ``_release_slot``    — SLOT accounting, its own one-shot token, so
  ``_running_count`` is decremented exactly once regardless of report or record
  ordering.
* ``_claim_finalize``  — REPORT ownership (``subagent_done`` + ``_on_done``),
  deliberately independent of ``done``, and executed under ``asyncio.shield`` so
  an interrupted claimer cannot strand the outcome.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.subagent import SubagentInfo, SubagentManager


def _make_manager(max_concurrent: int = 4) -> SubagentManager:
    mgr = SubagentManager(
        sessions=MagicMock(), ctx_builder=MagicMock(), max_concurrent=max_concurrent
    )
    mgr._fire_event = AsyncMock()
    mgr._write_tombstone = MagicMock()
    mgr._record_cost = MagicMock()
    mgr._on_done = AsyncMock()
    return mgr


def _info(**overrides) -> SubagentInfo:
    info = SubagentInfo(id="a1b2c3d4", task="t", agent="")
    for k, v in overrides.items():
        setattr(info, k, v)
    return info


def _done_events(mgr: SubagentManager) -> list:
    """The `subagent_done` fire_event calls only — `_fire_event` carries others."""
    return [c for c in mgr._fire_event.call_args_list if c.args and c.args[0] == "subagent_done"]


async def _noop_reset(session_key):
    await asyncio.sleep(0)


async def _schedule_recovery(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Arm cancel-recovery and let it run to completion.

    `_schedule_cancel_recovery` captures ``asyncio.current_task()`` and its
    respawn waits for that task's teardown to finish, so it must be armed from a
    task that then EXITS — arming it from the test body would sit through the
    real ``_RESET_TIMEOUT + 60`` handshake.
    """
    async def _arm() -> None:
        mgr._schedule_cancel_recovery(info)

    await asyncio.create_task(_arm())
    for _ in range(4):
        await asyncio.gather(*list(mgr._tasks.values()), return_exceptions=True)
        await asyncio.sleep(0)


# ── the claim and the slot token in isolation ────────────────────────


def test_report_claim_granted_exactly_once():
    mgr = _make_manager()
    info = _info()
    assert mgr._claim_finalize(info) is True
    assert mgr._claim_finalize(info) is False


def test_report_claim_ignores_done():
    """The round-5 defect: gating the claim on `done` let both paths decline."""
    mgr = _make_manager()
    assert mgr._claim_finalize(_info(done=True)) is True


def test_report_claim_withheld_but_open_while_recovering():
    mgr = _make_manager()
    info = _info(_recovering=True)
    assert mgr._claim_finalize(info) is False
    assert info._finalized is False, "claim must stay OPEN for the respawn"
    info._recovering = False
    assert mgr._claim_finalize(info) is True


def test_slot_token_granted_exactly_once():
    mgr = _make_manager()
    info = _info()
    assert mgr._release_slot(info) is True
    assert mgr._release_slot(info) is False


def test_slot_token_independent_of_reaped_and_done():
    mgr = _make_manager()
    assert mgr._release_slot(_info(reaped=True, done=True)) is True


# ── the round-5 regression: done set mid-teardown ────────────────────


@pytest.mark.asyncio
async def test_done_set_during_reap_teardown_still_reports_and_frees_one_slot():
    """THE round-5 regression.

    ``_run_inner`` sets ``info.done`` while ``_force_reap`` is suspended in the
    session reset. Previously the reaper then declined the report claim (it was
    gated on ``not info.done``), still set ``reaped``, and ``_run``'s finally
    skipped BOTH its claim and its slot decrement — so nothing was reported and
    ``_running_count`` stayed inflated, starving the spawn queue.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1

    async def _reset_then_finish(session_key):
        # A concurrently-finishing _run_inner marking the record terminal.
        info.done = True
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_then_finish

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1, "outcome was not reported exactly once"
    assert mgr._on_done.await_count == 1, "completion never reached the parent"
    assert mgr._running_count == 0, (
        f"slot not freed exactly once (_running_count={mgr._running_count})"
    )


@pytest.mark.asyncio
async def test_reap_after_run_released_slot_does_not_double_decrement():
    """Reverse order: `_run` already freed the slot; the reap must not re-free."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 2
    # Stand in for _run's finally having already released.
    assert mgr._release_slot(info) is True
    mgr._running_count -= 1

    mgr._sessions.reset = _noop_reset
    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert mgr._running_count == 1, "slot double-decremented"


# ── exactly-once reporting across the racing paths ───────────────────


@pytest.mark.asyncio
async def test_run_claiming_during_reap_teardown_yields_one_report():
    """A `_run` finishing inside the reap's teardown window claims first."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    reports: list[str] = []

    async def _reset_with_race(session_key):
        if mgr._claim_finalize(info):
            reports.append("run")
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_with_race

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert reports == ["run"]
    # The reap lost the claim, so it must not have reported.
    assert len(_done_events(mgr)) == 0
    assert mgr._on_done.await_count == 0


@pytest.mark.asyncio
async def test_uncontested_reap_reports_once():
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1
    assert mgr._on_done.await_count == 1
    assert info.done is True and info.reaped is True
    mgr._write_tombstone.assert_called_once()


# ── the shield: an interrupted claimer still delivers ────────────────


@pytest.mark.asyncio
async def test_cancel_during_subagent_done_still_delivers_once():
    """Cancelling the claimer while it fires `subagent_done`.

    The report runs on a shielded task, so it COMPLETES (delivery reaches the
    parent) even though the caller receives CancelledError. Asserts completion,
    not mere invocation — a cancelled-mid-flight injection is invoked but never
    completes, which is how an earlier revision's test passed against broken
    code.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    delivered: list[str] = []

    async def _slow_event(name, _info, _payload=None):
        if name == "subagent_done":
            await asyncio.sleep(0.05)

    async def _record_delivery(_info):
        delivered.append("done")

    mgr._fire_event = AsyncMock(side_effect=_slow_event)
    mgr._on_done = AsyncMock(side_effect=_record_delivery)

    task = asyncio.ensure_future(
        mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")
    )
    await asyncio.sleep(0.01)  # let it enter the shielded report
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The shielded report survives its awaiter's cancellation.
    await asyncio.wait_for(
        asyncio.gather(*[t for t in mgr._report_tasks if not t.done()]), timeout=2
    )
    assert delivered == ["done"], "shielded report did not complete delivery"


@pytest.mark.asyncio
async def test_cancel_during_on_done_produces_no_second_delivery():
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    calls: list[str] = []

    async def _slow_on_done(_info):
        calls.append("start")
        await asyncio.sleep(0.05)
        calls.append("end")

    mgr._on_done = AsyncMock(side_effect=_slow_on_done)

    task = asyncio.ensure_future(
        mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(
        asyncio.gather(*[t for t in mgr._report_tasks if not t.done()]), timeout=2
    )

    # Exactly one injection, and the claim is consumed so no other path retries.
    assert calls == ["start", "end"]
    assert info._finalized is True


# ── the classification contract is untouched ─────────────────────────


@pytest.mark.asyncio
async def test_reaped_precedes_intentional_cancel():
    """A cancel seen with `reaped is False` is read as an UNEXPECTED external
    cancel and the run is respawned — so the marker must still be set first."""
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset
    seen: dict[str, bool] = {}

    async def _never():
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_never())
    mgr._tasks["a1b2c3d4"] = task
    real_cancel = mgr._cancel_task_intentionally

    def _spy(t, i, *, reason=""):
        seen["reaped_at_cancel"] = i.reaped
        return real_cancel(t, i, reason=reason)

    mgr._cancel_task_intentionally = _spy

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert seen["reaped_at_cancel"] is True
    task.cancel()


# ── reap during _recovering must not strand the outcome ──────────────


@pytest.mark.asyncio
async def test_reap_during_recovering_still_reports_and_supersedes_respawn():
    """A user Stop / reap landing inside the cancel-recovery window.

    `_claim_finalize` withholds the claim while `_recovering` so a pending
    respawn is not reported done prematurely. But a reap is DEFINITIVELY
    terminal: previously it was refused the claim, did its teardown, set
    `reaped=True` and reported nothing — and `_resume`'s `reaped` abort path
    bare-returns, so no path ever reported. The agent sat unfinished until the
    reaper's wall-clock deadline.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, _recovering=True)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert len(_done_events(mgr)) == 1, "reap during recovery reported nothing"
    assert mgr._on_done.await_count == 1, "completion never reached the parent"
    assert info._recovering is False, "a killed agent must not stay pending a respawn"
    assert info.done is True
    assert mgr._running_count == 0


@pytest.mark.asyncio
async def test_reap_cancels_pending_recovery_task():
    """The superseded respawn task is cancelled, not left in its ~90s wait."""
    mgr = _make_manager()
    info = _info(_session_sharing=False, _recovering=True)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    async def _long_wait():
        await asyncio.sleep(3600)

    recovery = asyncio.ensure_future(_long_wait())
    mgr._tasks["a1b2c3d4:recovery"] = recovery

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert recovery.cancelled() or recovery.done(), "recovery task left running"
    assert "a1b2c3d4:recovery" not in mgr._tasks


@pytest.mark.asyncio
async def test_run_path_claim_still_withheld_during_recovering():
    """The override is scoped to terminal callers only — `_run`'s finally must
    still leave the claim OPEN for the respawned run."""
    mgr = _make_manager()
    info = _info(_recovering=True)
    assert mgr._claim_finalize(info) is False
    assert info._finalized is False
    assert info._recovering is True, "the non-terminal path must not clear it"


# ── the shutdown drain must not abandon stragglers ───────────────────


@pytest.mark.asyncio
async def test_cancel_all_cancels_reports_that_exceed_the_drain_timeout(monkeypatch):
    """`asyncio.wait` returns on timeout WITHOUT touching pending tasks.

    Leaving them pending is worse than not shielding: shutdown proceeds while
    they keep invoking `_on_done` against tearing-down state, then they die when
    the loop closes. They must be cancelled and gathered.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()

    started = asyncio.Event()

    async def _hangs_forever():
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.ensure_future(_hangs_forever())
    mgr._report_tasks.add(task)
    await started.wait()

    await mgr.cancel_all()

    assert task.done(), "straggler report was abandoned, not cancelled"
    assert task.cancelled(), "straggler should have been cancelled"


@pytest.mark.asyncio
async def test_cancel_all_readmits_an_undelivered_report_to_orphan_recovery(monkeypatch):
    """A report cancelled by shutdown must not become an unrecoverable loss.

    The terminal RECORD (including the tombstone) is written before delivery is
    attempted, and `list_orphans()` uses the tombstone to EXCLUDE a folder from
    the next start's reconciliation. So cancelling a still-pending report leaves
    an outcome that was never injected AND is invisible to the only path that
    could still inject it. Shutdown must clear the tombstone in that case.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()
    info = _info()
    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_tombstone", lambda aid: (cleared.append(aid), True)[1])

    started = asyncio.Event()

    async def _wedged_delivery(_info):
        started.set()
        await asyncio.sleep(3600)

    mgr._on_done = AsyncMock(side_effect=_wedged_delivery)
    assert mgr._claim_finalize(info) is True
    task = mgr._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="r",
        mark_delivered_on_success=True,
    )
    await started.wait()

    await mgr.cancel_all()

    assert task.cancelled(), "straggler should have been cancelled"
    assert cleared == [info.id], (
        "undelivered completion was left tombstoned — unrecoverable on restart"
    )


@pytest.mark.asyncio
async def test_cancel_all_keeps_the_tombstone_when_delivery_already_happened(monkeypatch):
    """The converse: a report cancelled AFTER `_on_done` returned is delivered.

    Re-admitting it would make the next start inject the same completion a
    second time — the duplicate delivery this PR exists to remove. Only
    `_reported_to_parent == False` may be re-admitted.
    """
    import kiro_crew.subagent as mod

    monkeypatch.setattr(mod, "_REPORT_DRAIN_TIMEOUT", 0.05)
    mgr = _make_manager()
    info = _info()
    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_tombstone", lambda aid: (cleared.append(aid), True)[1])

    delivered = asyncio.Event()

    async def _on_done(_info):
        delivered.set()

    mgr._on_done = AsyncMock(side_effect=_on_done)
    # A teardown gate that never opens, so the report is cancelled in the wait
    # that follows a SUCCESSFUL delivery.
    never = asyncio.Event()
    assert mgr._claim_finalize(info) is True
    mgr._spawn_terminal_report(
        info,
        source="test",
        injection_timeout_reason="r",
        mark_delivered_on_success=True,
        teardown_done=never,
    )
    await delivered.wait()
    await asyncio.sleep(0)

    await mgr.cancel_all()

    assert info._reported_to_parent is True, "delivery marker not set after _on_done"
    assert cleared == [], "a delivered completion was re-admitted — restart will duplicate it"


# ── every reporter goes through the claim, including recovery failure ─


@pytest.mark.asyncio
async def test_recovery_failure_reports_through_the_claim():
    """A failed cancel-recovery respawn must report via `_claim_finalize`.

    This site used to fire `subagent_done` and `_on_done` DIRECTLY, gated only
    on `done`/`reaped` — a fourth reporter outside the claim, so it could
    deliver on top of a concurrent reaper. Here the claim is already spent (as
    a reaper would leave it) and the respawn is forced to fail, so a compliant
    implementation stays silent.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    # Someone (the reaper) already owns and completed the report.
    assert mgr._claim_finalize(info) is True

    def _boom(_info):
        raise RuntimeError("respawn failed")

    mgr._run = _boom  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert _done_events(mgr) == [], "recovery failure reported over a spent claim"
    assert mgr._on_done.await_count == 0, "duplicate delivery reached the parent"


@pytest.mark.asyncio
async def test_recovery_failure_still_reports_when_it_owns_the_claim():
    """The converse: with the claim OPEN the failure must still be delivered.

    Guards against 'fixing' the duplicate above by making this path silent —
    the UI must never be left on a running card.
    """
    mgr = _make_manager()
    # Pin `started` into the past: on Windows `time.time()` has ~16ms
    # granularity, so a same-tick elapsed computes to exactly 0.0 and a bare
    # `> 0` assertion is a false failure rather than a real signal.
    info = _info(_session_sharing=False, started=time.time() - 5.0)

    def _boom(_info):
        raise RuntimeError("respawn failed")

    mgr._run = _boom  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert len(_done_events(mgr)) == 1, "recovery failure never reported"
    assert mgr._on_done.await_count == 1, "parent never heard about the failure"
    assert info.done is True
    assert info.elapsed >= 5.0, "report carried no elapsed"
    assert info._recovering is False


@pytest.mark.asyncio
async def test_reap_suppression_marker_is_set_before_the_teardown_await():
    """The RESPAWN-suppression marker must be visible during teardown.

    The marker and the recovery-task cancel used to sit AFTER the session reset
    await. A recovery task whose bounded handshake expired inside that window
    respawned the run being killed — tools running after a user Stop. Asserting
    from inside the reset proves the ordering.

    Note this is `_reap_started`, not `reaped`: see
    `test_run_woken_by_reaper_reset_still_synthesizes_its_error` for why setting
    `reaped` this early causes a false success instead.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    seen: dict[str, bool] = {}

    async def _observing_reset(session_key):
        seen["suppression_during_teardown"] = info._reap_started
        # `.cancelled()` only flips once the task runs, so assert the
        # observable that ordering guarantees: it is already de-registered
        # (popped + cancel requested) before teardown suspends.
        seen["recovery_deregistered"] = "a1b2c3d4:recovery" not in mgr._tasks
        await asyncio.sleep(0)

    async def _long_wait():
        await asyncio.sleep(3600)

    recovery = asyncio.ensure_future(_long_wait())
    mgr._tasks["a1b2c3d4:recovery"] = recovery
    mgr._sessions.reset = _observing_reset

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="reaped")

    assert seen["suppression_during_teardown"] is True, (
        "respawn suppression was not set while teardown was suspended — a "
        "recovery handshake expiring here would respawn the run being killed"
    )
    assert seen["recovery_deregistered"] is True, (
        "recovery task was still registered while teardown was suspended"
    )
    await asyncio.sleep(0)
    assert recovery.cancelled(), "recovery task was never actually cancelled"


@pytest.mark.asyncio
async def test_recovery_respawn_releases_its_fresh_slot():
    """The respawn's slot token must be re-armed, and then actually spent.

    The interrupted run's `finally` consumed this info's one-shot slot token, so
    without re-arming (`info._slot_released = False`) the respawned run's own
    release no-ops and `_running_count` stays inflated forever, starving the
    spawn queue. Reverting the re-arm line must fail here: the test drives the
    respawn to completion and asserts the count returns to its pre-spawn value.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    # State the interrupted run leaves behind: its finally already released.
    assert mgr._release_slot(info) is True
    mgr._running_count = 0

    ran = asyncio.Event()

    async def _fake_run(_info):
        ran.set()
        # Whatever the respawned run does, its finally releases the slot.
        if mgr._release_slot(_info):
            mgr._running_count = max(0, mgr._running_count - 1)

    mgr._run = _fake_run  # type: ignore[assignment]
    await _schedule_recovery(mgr, info)

    assert ran.is_set(), "respawn never ran"
    assert mgr._running_count == 0, (
        "slot token was not re-armed for the respawn — `_running_count` "
        f"permanently inflated at {mgr._running_count}"
    )


# ── the reap marker is split: early for respawn, late for records ────


@pytest.mark.asyncio
async def test_run_woken_by_reaper_reset_still_synthesizes_its_error():
    """A run woken by the REAPER's own session reset must not report success.

    `reaped` carries two incompatible requirements. The recovery scheduler needs
    it set before the teardown awaits (or it respawns the run being killed), so
    an earlier revision hoisted it to the top of `_force_reap`. But `_run` skips
    its error synthesis when `reaped` is set — so a run woken by the reaper's
    reset (the reset kills the provider, `_run_inner` raises) fell through with
    NO error, claimed the report first while the reaper was still tearing down,
    and delivered a FALSE SUCCESS the reaper could no longer correct.

    The flag is therefore split: `_reap_started` early (respawn suppression),
    `reaped` late (record/teardown ownership).
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, started=time.time() - 5.0)
    mgr._running_count = 1
    observed: dict[str, bool] = {}

    async def _reset_wakes_the_run(session_key):
        # Exactly the window under test: the reap is in flight and suspended in
        # teardown. A run waking here must still see `reaped == False`.
        observed["reaped"] = info.reaped
        observed["reap_started"] = info._reap_started
        await asyncio.sleep(0)

    mgr._sessions.reset = _reset_wakes_the_run

    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="timeout")

    assert observed["reap_started"] is True, (
        "recovery suppression marker not set before teardown — a pending "
        "respawn would relaunch the run being killed"
    )
    assert observed["reaped"] is False, (
        "`reaped` was already set while the reaper was still tearing down: a run "
        "woken by this very reset would skip error synthesis and report success"
    )
    # And the reap still owns the record by the time it writes one.
    assert info.reaped is True
    assert info.error, "reaped agent recorded no error"


@pytest.mark.asyncio
async def test_cancelled_teardown_still_releases_slot_and_gate():
    """A cancellation at a teardown await must not skip the bookkeeping.

    Every statement in the teardown awaits, and `CancelledError` is not caught by
    the `except Exception` arms — so it propagated straight out of `_run`'s
    `finally`, skipping the slot release, the task pop and the teardown gate.
    That leaks a concurrency slot (the bug this PR exists to fix) and leaves an
    injected result unmarked, so restart reconciliation re-injects it.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    info._slot_released = False  # the run owns an unspent slot token

    async def _cancelled_teardown(_info, _key):
        raise asyncio.CancelledError()

    mgr._teardown_run_session = _cancelled_teardown  # type: ignore[assignment]

    async def _run_inner_ok(_info, _key):
        _info.result = "done"

    mgr._run_inner = _run_inner_ok  # type: ignore[assignment]

    task = asyncio.ensure_future(mgr._run(info))
    with pytest.raises(asyncio.CancelledError):
        await task

    assert mgr._running_count == 0, (
        f"cancelled teardown leaked the slot (_running_count={mgr._running_count})"
    )
    assert "a1b2c3d4" not in mgr._tasks, "cancelled teardown left the task registered"


@pytest.mark.asyncio
async def test_run_does_not_block_on_its_report_during_shutdown():
    """`_run` must hand its report to the bounded drain once shutting down.

    `_run`'s `CancelledError` arm does not re-raise, so by the time it reaches
    `_await_report` the cancellation has been CONSUMED — `shield` would then wait
    out the full `_ON_DONE_TIMEOUT` injection cap and hold `cancel_all()`'s
    gather for it. (An earlier version of this test kept the cancellation pending
    and so measured a path the real `_run` never takes.)
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False)
    mgr._running_count = 1
    mgr._shutting_down = True

    async def _run_inner_ok(_info, _key):
        _info.result = "done"

    async def _noop_teardown(_info, _key):
        await asyncio.sleep(0)

    mgr._run_inner = _run_inner_ok  # type: ignore[assignment]
    mgr._teardown_run_session = _noop_teardown  # type: ignore[assignment]

    wedged = asyncio.Event()

    async def _wedged_on_done(_info):
        wedged.set()
        await asyncio.sleep(3600)

    mgr._on_done = _wedged_on_done

    # Must return promptly even though the injection is wedged.
    await asyncio.wait_for(mgr._run(info), timeout=5)

    assert wedged.is_set(), "report never started"
    pending = [t for t in mgr._report_tasks if not t.done()]
    assert pending, "report should still be pending, owned by cancel_all's drain"
    for t in pending:
        t.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_user_stop_during_pending_recovery_is_not_recorded_as_failure():
    """A neutral user Stop must not be persisted as a failure by the recovery arm.

    `_force_reap` cancels a pending recovery task BEFORE setting `reaped` (which
    must stay false until the reaper owns the record). `_resume_guarded`'s
    CancelledError arm consulted only `reaped`, so it won that race and wrote
    `error="cancelled"` plus a failure stat over a neutral stop — an outcome the
    reaper could no longer correct. It must consult `_reap_started`.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, user_stopped=True, started=time.time() - 5.0)
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    failures: list[int] = []

    async def _arm() -> None:
        mgr._schedule_cancel_recovery(info)

    await asyncio.create_task(_arm())
    recovery = mgr._tasks.get("a1b2c3d4:recovery")
    assert recovery is not None, "recovery task was not registered"

    import kiro_crew.subagent as mod

    class _Stats:
        def inc_subagent_failed(self):
            failures.append(1)

    orig = mod.Stats
    mod.Stats = lambda: _Stats()  # type: ignore[assignment]
    try:
        await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="user_stop")
        await asyncio.gather(recovery, return_exceptions=True)
    finally:
        mod.Stats = orig  # type: ignore[assignment]

    assert failures == [], (
        "a neutral user Stop was counted as a subagent failure by the "
        "cancelled-recovery arm"
    )
    assert info.error == "", f"user stop synthesized an error: {info.error!r}"


@pytest.mark.asyncio
async def test_stop_during_pending_spawn_approval_reports_and_releases_once():
    """The spawn-approval rejection path is a FIFTH terminal site.

    It set `done`, decremented `_running_count` with a bare decrement and
    announced via `_safe_announce` — outside both one-shot tokens. A user Stop
    funnels into `_force_reap` and can land while the approval is still pending
    (a human prompt has no deadline), so the reap released the slot and reported,
    then the rejection path released and reported AGAIN: a negative concurrency
    count and a duplicate completion.
    """
    mgr = _make_manager()
    info = _info(_session_sharing=False, started=time.time() - 5.0)
    mgr._agents["a1b2c3d4"] = info
    mgr._running_count = 1
    mgr._sessions.reset = _noop_reset

    announced: list[str] = []

    async def _safe_announce(_info):
        announced.append(_info.id)

    mgr._safe_announce = _safe_announce  # type: ignore[assignment]

    release_stop = asyncio.Event()

    async def _pending_then_denied(_rid, _preview, _parent):
        await release_stop.wait()
        return False

    mgr._on_spawn_approval = _pending_then_denied  # type: ignore[assignment]

    approval_task = asyncio.ensure_future(mgr._spawn_with_approval(info))
    await asyncio.sleep(0)

    # User Stop lands while the approval is still outstanding.
    await mgr._force_reap("a1b2c3d4", info, elapsed=1.0, reason="user_stop")
    release_stop.set()
    await approval_task

    assert mgr._running_count == 0, (
        f"slot released twice (_running_count={mgr._running_count}); a negative "
        "count permanently inflates apparent capacity"
    )
    total_reports = len(_done_events(mgr)) + len(announced)
    assert total_reports == 1, (
        f"terminal outcome delivered {total_reports} times, expected exactly once"
    )
