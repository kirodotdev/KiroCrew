"""Tests for the AutoNudge stranded-loop rescues (issue #8636).

The defect these pin: a dashboard-bound (``chat-NNN-...``) loop's only re-arm
path after a delivered fire is ``notify_turn_complete``. If that hook never
arrives -- the nudge turn dies on a path that skips the stop hook, the probe
gate raises (the ``_monitor_tick_is_quiet`` await used to sit OUTSIDE
``_timer``'s try/finally, so an escaping exception killed the timer task), or
``notify_user_input`` dropped the deferred re-arm -- the loop was left
persisted ``active=true`` with no live timer and nothing on a timer to revive
it. Three fixes are covered here:

* the probe gate is exception-safe in the SPENDING direction: an escaping
  exception is treated as "not quiet" and falls through to the fire, so the
  timer task neither dies nor silently mutes the loop;
* a periodic reconciler walks the store and re-arms any active loop observed
  eligible-and-unarmed on two consecutive passes -- and only those;
* delivered fires are logged at INFO so a dead loop and a calm loop are no
  longer byte-identical in the journal.

Two subtleties these tests encode on purpose. First, nothing pops a timer
task from ``_timers`` when it completes normally, so the stranded states
leave a DONE task behind rather than an empty slot; the reconciler must treat
both forms as unarmed, since a membership test alone would miss every
fire-delivered stranding. Second, a single unarmed observation cannot tell
"stranded" apart from a running user turn or an ``update()`` mutation window,
which is why a rescue requires two consecutive passes and why
``notify_user_input`` resets a slot's candidacy.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MonitorDispatchResult,
    MonitorOutcome,
    MonitorState,
)


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path, event_loop):
    """Service with GUARANTEED teardown on every exit path.

    Every test here calls ``start()``, which spawns the reconciler task and
    publishes the module-global ``_INSTANCE``. A trailing ``svc.stop()`` in
    the test body is skipped by any failing assertion before it, leaking a
    pending task (and the global) past the test -- so the stop lives here,
    where pytest runs it on failure too, and the cancelled reconciler is
    drained on the test's still-open event loop. A SYNC fixture on purpose:
    the pinned pytest-asyncio (0.20.3) cannot run async-generator fixtures
    under pytest 9 (``FixtureDef.unittest`` was removed), so the drain goes
    through the ``event_loop`` fixture, which outlives this one by fixture
    ordering. ``stop()`` is idempotent, so the lifecycle test that exercises
    it explicitly stays valid.
    """
    service = AutoNudgeService(base_dir=tmp_path)
    yield service
    # Snapshot EVERY background task the service may hold, not just the
    # reconciler: ``stop()`` clears ``_timers`` (cancelling, not draining),
    # and ``_inflight_adds`` holds detached ``_persist_locked`` writers --
    # e.g. from a rescue's ``_persist_soon()`` -- that stop() deliberately
    # leaves running. An undrained writer can recreate ``tmp_path`` AFTER
    # pytest removes it, which is exactly the stray-directory shape the
    # no-test-side-effects rule names. Draining here lets the writes finish
    # while the directory still exists and retires every task in-loop.
    tasks = [
        t
        for t in (service._reconciler, *service._timers.values(), *service._inflight_adds)
        if t is not None
    ]
    service.stop()
    live = [t for t in tasks if not t.done()]
    if live and not event_loop.is_closed():
        with contextlib.suppress(asyncio.TimeoutError):
            event_loop.run_until_complete(
                asyncio.wait_for(asyncio.gather(*live, return_exceptions=True), timeout=5)
            )


def _timer_is_live(svc: AutoNudgeService, loop_id: str) -> bool:
    t = svc._timers.get(loop_id)
    return t is not None and not t.done()


def _reconcile_twice(svc: AutoNudgeService) -> None:
    """Two consecutive passes -- the minimum that can rescue anything."""
    svc._reconcile_once()
    svc._reconcile_once()


async def _run_timer_as_production_would(svc: AutoNudgeService, loop: NudgeLoop):
    """Run one zero-delay timer pass with the running task AS the _timers entry.

    Models the production shape exactly: the running timer task IS the
    ``_timers`` entry, so when it completes it stays there as a DONE task.
    ``add()`` armed a real ``idle_secs`` timer; replace it with this one.
    """
    svc._cancel_timer(loop.id)
    t = asyncio.get_running_loop().create_task(svc._timer(loop, delay=0))
    svc._timers[loop.id] = t
    await t
    return t


# ── Fire path: a delivered fire with no turn-complete hook ──


@pytest.mark.asyncio
async def test_delivered_fire_without_turn_complete_is_rescued_by_reconciler(svc, caplog):
    """The stranding the issue observed: fire delivered, stop hook never came.

    The dashboard fire path deliberately does not self-re-arm (the nudge
    turn's end is supposed to do it via ``notify_turn_complete``), so after
    the timer coroutine returns the loop is active with no live timer. Two
    consecutive reconciler passes must arm it again -- toward the loop's own
    deadline, which the delivered fire cleared, so the rescue starts a fresh
    full countdown rather than firing immediately.
    """
    fired: list[str] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop.id)
        return True

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="watch the PR", idle_secs=300)
    with caplog.at_level(logging.INFO, logger="kiro_crew.autonudge"):
        await _run_timer_as_production_would(svc, loop)
    assert fired == [loop.id]
    # Delivered fires are logged at INFO -- the observability half of the fix.
    assert any("fired cycle" in r.message for r in caplog.records)
    # Stranded: active, deadline cleared, timer entry present but finished.
    assert loop.active
    assert loop.next_due_ts == 0.0
    assert loop.id in svc._timers and svc._timers[loop.id].done()
    assert not _timer_is_live(svc, loop.id)

    # One pass only records candidacy; a second consecutive pass rescues.
    before = time.time()
    svc._reconcile_once()
    assert not _timer_is_live(svc, loop.id)
    svc._reconcile_once()
    assert _timer_is_live(svc, loop.id)
    # Re-armed via _arm_from_deadline's own 0.0 self-heal: fresh full countdown.
    assert loop.next_due_ts >= before + 300


@pytest.mark.asyncio
async def test_delivered_fire_never_rearms_a_dashboard_loop_by_itself(svc):
    """Without a reconciler PASS nothing revives it (the running periodic
    task's first beat is a full wall-clock interval away, far beyond this
    test), which is exactly the pre-fix stranding."""

    async def on_fire(loop: NudgeLoop) -> bool:
        return True

    svc._on_fire = on_fire
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    await _run_timer_as_production_would(svc, loop)
    for _ in range(3):
        await asyncio.sleep(0)
    assert not _timer_is_live(svc, loop.id)
    assert loop.active


# ── Gate path: an exception escaping the probe gate ──


@pytest.mark.asyncio
async def test_gate_exception_falls_through_to_fire_and_keeps_loop_alive(svc, caplog):
    """A raising gate must neither kill the timer task nor mute the loop.

    The module's own contract is that every uncertain observation resolves
    toward spending a turn; an exception escaping the gate is the most
    uncertain observation there is, so the tick fires exactly as it would
    have before the gate existed. The timer task must complete without the
    exception propagating (a strong ref in ``_timers`` means a task killed by
    an exception is never even reported by asyncio).
    """
    fired: list[str] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop.id)
        return True

    async def boom(loop: NudgeLoop) -> bool:
        raise RuntimeError("probe gate blew up")

    svc._on_fire = on_fire
    svc._monitor_tick_is_quiet = boom  # type: ignore[method-assign]
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    with caplog.at_level(logging.ERROR, logger="kiro_crew.autonudge"):
        t = await _run_timer_as_production_would(svc, loop)
    # The tick SPENT a turn -- fail-toward-firing, not fail-toward-silence.
    assert fired == [loop.id]
    # The timer task finished cleanly; the exception did not kill it.
    assert t.done() and t.exception() is None
    assert loop.active
    assert loop.stopped_reason == ""
    # The defect is loud: the escaping exception is logged with traceback.
    assert any("probe gate failed" in r.message for r in caplog.records)
    # Dashboard slot: the delivered fire waits on notify_turn_complete, and
    # the reconciler is the backstop when that hook never comes.
    _reconcile_twice(svc)
    assert _timer_is_live(svc, loop.id)


@pytest.mark.asyncio
async def test_gate_exception_on_channel_loop_leaves_loop_armed_and_active(svc):
    """Channel-bound loops self-re-arm on the fire path, so a raising gate
    leaves the loop armed and active with no reconciler involved at all."""
    fired: list[str] = []

    async def on_fire(loop: NudgeLoop) -> bool:
        fired.append(loop.id)
        return True

    async def boom(loop: NudgeLoop) -> bool:
        raise RuntimeError("probe gate blew up")

    svc._on_fire = on_fire
    svc._monitor_tick_is_quiet = boom  # type: ignore[method-assign]
    await svc.start()
    loop = await svc.add(slot_key="slack:1234567890.123", message="go", idle_secs=300)
    await _run_timer_as_production_would(svc, loop)
    assert fired == [loop.id]
    assert loop.active
    assert _timer_is_live(svc, loop.id)


# ── Reconciler predicate ──


@pytest.mark.asyncio
async def test_reconciler_rearms_active_loop_with_absent_timer_only(svc):
    """Re-arms active+unarmed (after two passes); leaves inactive and
    live-timer loops alone."""
    await svc.start()
    stranded = await svc.add(slot_key="chat-1-111", message="a", idle_secs=300)
    inactive = await svc.add(slot_key="chat-2-222", message="b", idle_secs=300)
    healthy = await svc.add(slot_key="chat-3-333", message="c", idle_secs=300)
    # Strand one: timer entry absent entirely (the dead-user-turn shape).
    svc._cancel_timer(stranded.id)
    assert stranded.id not in svc._timers
    # Deactivate one and drop its timer: the reconciler must NOT revive it.
    svc._cancel_timer(inactive.id)
    inactive.active = False
    healthy_task = svc._timers[healthy.id]

    _reconcile_twice(svc)

    assert _timer_is_live(svc, stranded.id)
    assert inactive.id not in svc._timers
    assert svc._timers[healthy.id] is healthy_task  # untouched, same task


@pytest.mark.asyncio
async def test_reconciler_rearms_active_loop_whose_timer_task_finished(svc):
    """The DONE-task form of stranded: entry present, task finished."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc._cancel_timer(loop.id)
    done = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    await done
    svc._timers[loop.id] = done
    _reconcile_twice(svc)
    assert _timer_is_live(svc, loop.id)


@pytest.mark.asyncio
async def test_reconciler_skips_mid_fire_and_quiesced_loops(svc):
    """A loop mid-fire or owned by cleanup must not be touched."""
    await svc.start()
    firing = await svc.add(slot_key="chat-1-111", message="a", idle_secs=300)
    quiesced = await svc.add(slot_key="chat-2-222", message="b", idle_secs=300)
    svc._cancel_timer(firing.id)
    svc._cancel_timer(quiesced.id)
    svc._firing.add(firing.id)
    svc._maintenance_quiescing.add(quiesced.id)
    try:
        _reconcile_twice(svc)
        assert firing.id not in svc._timers
        assert quiesced.id not in svc._timers
    finally:
        svc._firing.discard(firing.id)
        svc._maintenance_quiescing.discard(quiesced.id)


def _monitor(**overrides) -> MonitorState:
    state = MonitorState(
        kind="github_pr",
        target="owner/repo#1",
        objective="watch it",
        created_ts=time.time(),
    )
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


@pytest.mark.asyncio
async def test_reconciler_skips_dead_wake_claim_but_rescues_live_one(svc):
    """A wake claim with no completion-evidence deadline died mid-handoff.

    ``_load`` retires that shape on restart; arming it here would wake a
    controller that answers NO_CHANGE forever -- no budget or cap could end
    it. A claim WITH a deadline is the safe half (its ``next_due_ts`` is that
    deadline) and must still be rescued.
    """
    await svc.start()
    dead = await svc.add(slot_key="chat-1-111", message="a", idle_secs=300)
    live = await svc.add(slot_key="chat-2-222", message="b", idle_secs=300)
    dead.monitor = _monitor(wake_in_flight=True, completion_evidence_deadline=0.0)
    live.monitor = _monitor(wake_in_flight=True, completion_evidence_deadline=time.time() + 120)
    svc._cancel_timer(dead.id)
    svc._cancel_timer(live.id)
    _reconcile_twice(svc)
    assert dead.id not in svc._timers
    assert _timer_is_live(svc, live.id)


@pytest.mark.asyncio
async def test_reconciler_rescues_busy_retry_and_inactive_terminal_waiter(svc):
    """Two structured-monitor recovery states the skip predicates must NOT
    exclude, both mirrored from the store's own semantics:

    * a ``BUSY`` retry has ``wake_in_flight`` and an intentionally empty
      evidence deadline, yet ``_load`` resumes it at its persisted retry
      deadline on restart -- the in-process backstop must treat it as live;
    * an INACTIVE loop still waiting for terminal-completion evidence owns a
      finite accepted-turn correlation whose expiry needs a timer
      (``_timer``'s own re-arm guard includes it), and
      ``notify_turn_complete`` ignores inactive loops, so a user-input
      cancel would otherwise strand the claim forever.
    """
    await svc.start()
    busy = await svc.add(slot_key="chat-1-111", message="a", idle_secs=300)
    waiter = await svc.add(slot_key="chat-2-222", message="b", idle_secs=300)
    busy.monitor = _monitor(
        wake_in_flight=True,
        completion_evidence_deadline=0.0,
        wake_delivery=MonitorDispatchResult.BUSY,
    )
    waiter.monitor = _monitor(
        outcome=MonitorOutcome.USER_STOP,
        wake_in_flight=True,
        completion_evidence_deadline=time.time() + 120,
    )
    waiter.active = False
    waiter.next_due_ts = waiter.monitor.completion_evidence_deadline
    svc._cancel_timer(busy.id)
    svc._cancel_timer(waiter.id)
    _reconcile_twice(svc)
    assert _timer_is_live(svc, busy.id)
    assert _timer_is_live(svc, waiter.id)


@pytest.mark.asyncio
async def test_reconciler_skips_monitor_version_this_gateway_does_not_implement(svc, caplog):
    """The version refusal itself lives in ``_arm_from_deadline``; the
    reconciler's own guard exists so an unimplementable record is not retried
    (and its INFO refusal not repeated) on every pass forever. Pin the guard
    by the silence, not the timer: without it each pass would log both the
    rescue line and the refusal line."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-111", message="a", idle_secs=300)
    loop.monitor = _monitor(version=MONITOR_STATE_VERSION + 1)
    svc._cancel_timer(loop.id)
    with caplog.at_level(logging.INFO, logger="kiro_crew.autonudge"):
        _reconcile_twice(svc)
    assert loop.id not in svc._timers
    assert not any("not arming" in r.message for r in caplog.records)
    assert not any("re-arming stranded" in r.message for r in caplog.records)


# ── Two-pass quiescence ──


@pytest.mark.asyncio
async def test_single_pass_never_rearms(svc):
    """One unarmed observation is not evidence of stranding: it is also what a
    running user turn and an update() mutation window look like."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc._cancel_timer(loop.id)
    svc._reconcile_once()
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_user_input_resets_reconcile_candidacy(svc):
    """A user turn starting between passes proves the slot is alive: the
    rescue clock restarts, so an actively-conversing session is never armed
    behind ``notify_user_input``'s deliberate cancel."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc.notify_user_input("chat-1-123")  # deliberate cancel: user turn begins
    svc._reconcile_once()  # pass 1: candidate
    svc.notify_user_input("chat-1-123")  # still conversing
    svc._reconcile_once()  # pass 2: candidacy was reset -- must NOT arm
    assert loop.id not in svc._timers
    svc._reconcile_once()  # pass 3: second consecutive quiet pass -- rescue
    assert _timer_is_live(svc, loop.id)


@pytest.mark.asyncio
async def test_transiently_active_loop_is_not_armed(svc):
    """Models update()'s rollback window: a loop that looks active+unarmed on
    one pass but is inactive again by the next (failed persist rolled it
    back) must end up with no timer."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc._cancel_timer(loop.id)
    loop.active = True
    svc._reconcile_once()  # observes the transient shape: candidate only
    assert loop.id not in svc._timers
    loop.active = False  # rollback landed before the next pass
    svc._reconcile_once()
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_pass_defers_while_mutation_lock_held_and_keeps_candidacy(svc):
    """Every mutation (update/add/persist) runs inside ``svc._lock`` and its
    store write has no timeout, so a wedged write can hold a transient shape
    across ANY number of passes. A pass that finds the lock held must defer
    entirely -- arming nothing, and dropping no candidacy, so the rescue is
    delayed rather than restarted."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc._cancel_timer(loop.id)
    svc._reconcile_once()  # pass 1: candidate recorded
    assert loop.id in svc._reconcile_candidates
    async with svc._lock:  # a mutation window is open (e.g. a stalled write)
        svc._reconcile_once()
        svc._reconcile_once()
        assert loop.id not in svc._timers  # deferred: nothing armed
        assert loop.id in svc._reconcile_candidates  # candidacy preserved
    svc._reconcile_once()  # lock released: the second real pass rescues
    assert _timer_is_live(svc, loop.id)


# ── Reconciler lifecycle ──


@pytest.mark.asyncio
async def test_periodic_task_rescues_end_to_end(svc, monkeypatch):
    """The backstop works with nobody calling _reconcile_once by hand: shrink
    the beat interval, strand a loop, and a live timer appears on its own."""
    import kiro_crew.autonudge as _an

    monkeypatch.setattr(_an, "_RECONCILE_INTERVAL_SECS", 0.05)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=300)
    svc._cancel_timer(loop.id)
    deadline = time.time() + 5.0
    while time.time() < deadline and not _timer_is_live(svc, loop.id):
        await asyncio.sleep(0.05)
    assert _timer_is_live(svc, loop.id)


@pytest.mark.asyncio
async def test_reconciler_beat_stays_on_wall_clock_under_patched_sleep(svc, monkeypatch):
    """This file's sibling suites patch module asyncio.sleep to a no-op to
    fast-forward the per-loop timers; a sleep-based reconciler would busy-spin
    under that patch and re-arm everything continuously. The call_later beat
    must keep the reconciler dormant regardless."""
    import kiro_crew.autonudge as _an

    real_sleep = asyncio.sleep
    passes: list[float] = []
    orig_pass = svc._reconcile_once

    def counting_pass() -> None:
        passes.append(time.time())
        orig_pass()

    svc._reconcile_once = counting_pass  # type: ignore[method-assign]

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)
    await svc.start()
    await real_sleep(0.3)
    assert passes == []  # first beat is a full wall-clock interval away


@pytest.mark.asyncio
async def test_start_spawns_reconciler_and_stop_retires_it(svc):
    await svc.start()
    task = svc._reconciler
    assert task is not None and not task.done()
    svc.stop()
    assert svc._reconciler is None
    for _ in range(3):
        await asyncio.sleep(0)
    assert task.cancelled()
