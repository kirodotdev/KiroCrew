"""The crew-log wake's batch window: reports arriving together share one turn.

``rfc-crew-log-wake``. With the window armed on the real service, a burst inside one
window costs one turn naming every item, an isolated report still wakes within the window,
a report that misses the batch tick is not lost, and a steady stream cannot hold the batch
open forever. The window itself never arms past the loop's own deadline, is tunable (an
unreadable value falls back), restores fire-on-first-report at zero, and needs both the
mark and a live timer. One pushed tick is in ``test_conductor_wake_pushed_ticks.py``; the
lookup and the triggers are in ``test_conductor_wake.py``; the shared ids, stubs and
drivers are in ``conductor_wake_helpers.py``.
"""

from __future__ import annotations

import asyncio

import pytest
from conductor_wake_helpers import _isolated_home  # noqa: F401
from conductor_wake_helpers import (
    CONDUCTOR,
    LOOP_ID,
    WORKER,
    WORKER_B,
    _bind,
    _fire,
    _Loop,
    _service,
    _Svc,
)

from kiro_crew import conductor_wake, work_ledger

WORKER_C = "chat-worker-c"


# ── reports arriving together share one turn ─────────────────────────────────


_BATCH_WINDOW = 7.0


def _batching_service(tmp_path, monkeypatch, *, window: float = _BATCH_WINDOW):
    """A real service with the wake window ARMED, instrumented for what a batch costs.

    Returns ``(svc, loop, arms, delivered, wakes)``: the arm delays every push asked for,
    the delivered turns, and the body of every WAKE verdict the real kernel reached with
    the real probe over the real ledger. ``wakes`` is what says a turn named every item
    that moved, since the brief the kernel joins is the only place the item ids appear.

    ``_arm_timer`` is replaced rather than wrapped, because an armed timer would really
    sleep the window and the point here is WHICH delay was asked for. What is kept is the
    one effect the batch depends on: an arm clears the loop's push mark, exactly as the
    real one does, so ``fire_now`` re-setting it is what opens the window. The caller runs
    the tick the window armed by calling ``_timer`` itself.
    """
    import kiro_crew.autonudge as autonudge
    from kiro_crew import irq

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, str(window))
    arms: list[float | None] = []
    delivered: list[int] = []
    wakes: list[str] = []
    real_poll = irq.poll

    def _spy_poll(identity, message, probe):
        verdict = real_poll(identity, message, probe)
        if verdict.outcome is irq.Outcome.WAKE:
            wakes.append(verdict.body)
        return verdict

    monkeypatch.setattr(irq, "poll", _spy_poll)

    async def _on_fire(_loop) -> bool:
        delivered.append(len(delivered) + 1)
        return True

    svc = _service(tmp_path / "an", on_fire=_on_fire, worker_closed=lambda _key: False)
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    return svc, arms, delivered, wakes


def _instrument_arms(svc, arms: "list[float | None]") -> None:
    """Record every arm's delay without sleeping it, keeping the two effects a batch reads.

    An armed timer would really sleep the window and the point is WHICH delay was asked
    for, so the task is a stand-in. What it keeps is what ``_window_open`` consults: the
    push mark cleared on every arm, exactly as the real ``_arm_timer`` clears it, and a
    LIVE entry in ``_timers`` for the loop -- without one the window would read as closed
    and every report would arm, which is the opposite of what these cases measure. The
    service's own ``stop`` cancels what is left in that table.
    """

    def _spy(armed, delay=None) -> None:
        arms.append(delay)
        svc._pushed_ticks.discard(armed.id)
        stale = svc._timers.get(armed.id)
        if stale is not None:
            stale.cancel()
        # flake-ok: pre-existing hour-long parked sleep standing in for a live armed timer, which the next arm or svc.stop() cancels, moved verbatim; tracked for a de-flake pass
        svc._timers[armed.id] = asyncio.get_running_loop().create_task(asyncio.sleep(3600))

    svc._arm_timer = _spy  # type: ignore[method-assign]


def test_a_burst_inside_one_window_costs_one_turn_naming_every_item(tmp_path, monkeypatch):
    """Three workers report inside the window: ONE arm, ONE turn, all three items named.

    This is the whole fix. Before it each report armed its own tick and bought its own
    turn, and each of those turns re-read the same board; the first report now arms a
    tick one window ahead and the other two ride it, because the tick reads the ledger
    when it RUNS rather than when it was armed.
    """
    first = _bind(WORKER)
    second = _bind(WORKER_B)
    third = _bind(WORKER_C)

    async def _drive() -> "tuple[list[float | None], list[int], list[str]]":
        svc, arms, delivered, wakes = _batching_service(tmp_path, monkeypatch)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            for worker, item, status in (
                (WORKER, first, "done"),
                (WORKER_B, second, "blocked"),
                (WORKER_C, third, "question"),
            ):
                work_ledger.apply_worker_report(CONDUCTOR, item, status=status, summary="s")
                await _fire(svc, worker)
            assert arms == [_BATCH_WINDOW], "only the first report armed a tick"
            assert loop.id in svc._pushed_ticks, "and the window it armed is still open"
            # The tick the window armed, run here rather than slept through.
            await svc._timer(loop, delay=0.0)
            return arms, list(delivered), list(wakes)
        finally:
            svc.stop()

    arms, delivered, wakes = asyncio.run(_drive())
    assert arms == [_BATCH_WINDOW], "the batch arms exactly once"
    assert delivered == [1], "and costs exactly one conductor turn"
    assert len(wakes) == 1
    for item_id in (first, second, third):
        assert item_id in wakes[0], "the one turn names every item that moved"


def test_an_isolated_report_still_wakes_within_the_window(tmp_path, monkeypatch):
    """One report, no company: one turn, armed at the window rather than at the cadence.

    The window IS this report's added latency, so what matters is that it is the window
    and not the loop's own hour-long patrol interval -- the fallback the batch must never
    degrade into.
    """
    only = _bind(WORKER)

    async def _drive() -> "tuple[list[float | None], list[int], list[str], float]":
        svc, arms, delivered, wakes = _batching_service(tmp_path, monkeypatch)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            work_ledger.apply_worker_report(CONDUCTOR, only, status="done", summary="s")
            await _fire(svc, WORKER)
            await svc._timer(loop, delay=0.0)
            return arms, list(delivered), list(wakes), float(loop.idle_secs)
        finally:
            svc.stop()

    arms, delivered, wakes, cadence = asyncio.run(_drive())
    assert arms == [_BATCH_WINDOW]
    assert _BATCH_WINDOW < cadence, "the window is a fraction of the loop's own cadence"
    assert delivered == [1]
    assert len(wakes) == 1 and only in wakes[0]


def test_a_report_that_misses_the_batch_tick_is_not_lost(tmp_path, monkeypatch):
    """A report landing after the batch tick started opens the NEXT window and is read.

    The push mark is cleared as the tick begins, which is the edge that makes this true:
    from then on a report is news that tick may not have seen, so it is free to arm a
    window of its own rather than being folded into a tick that already ran.
    """
    early = _bind(WORKER)
    late = _bind(WORKER_B)

    async def _drive() -> "tuple[list[float | None], list[int], list[str]]":
        svc, arms, delivered, wakes = _batching_service(tmp_path, monkeypatch)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            work_ledger.apply_worker_report(CONDUCTOR, early, status="done", summary="s")
            await _fire(svc, WORKER)
            await svc._timer(loop, delay=0.0)
            assert loop.id not in svc._pushed_ticks, "the tick that ran closed its window"
            work_ledger.apply_worker_report(CONDUCTOR, late, status="question", summary="s")
            await _fire(svc, WORKER_B)
            await svc._timer(loop, delay=0.0)
            return arms, list(delivered), list(wakes)
        finally:
            svc.stop()

    arms, delivered, wakes = asyncio.run(_drive())
    assert arms == [_BATCH_WINDOW, _BATCH_WINDOW], "the late report armed a window of its own"
    assert delivered == [1, 2]
    assert early in wakes[0] and late in wakes[1], "neither report was dropped"


def test_a_steady_stream_of_reports_cannot_hold_the_batch_open_forever(tmp_path, monkeypatch):
    """Reports arriving one after another must not each restart the window.

    The starvation case, and the reason a report inside an open window arms NOTHING
    instead of re-arming: ``_arm_timer`` replaces the armed timer, so a re-arm per report
    would push the batch's delivery out for as long as the stream lasted.
    """
    item = _bind(WORKER)

    async def _drive() -> "list[float | None]":
        svc, arms, _delivered, _wakes = _batching_service(tmp_path, monkeypatch)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            for n in range(10):
                work_ledger.apply_worker_report(CONDUCTOR, item, status="question", summary=f"{n}")
                await _fire(svc, WORKER)
            assert loop.id in svc._pushed_ticks, "the first window is the one still open"
            return arms
        finally:
            svc.stop()

    assert asyncio.run(_drive()) == [_BATCH_WINDOW], "ten reports, one arm"


# ── the window itself ────────────────────────────────────────────────────────


def test_the_window_never_arms_past_the_loops_own_deadline():
    """A push may only move a fire EARLIER, so the window is clamped by the deadline.

    ``_arm_timer`` REPLACES the armed timer, so a window reaching past a deadline about
    to fire would delay the loop's own scheduled tick by the difference -- a pull-forward
    that pushed work back.
    """
    from kiro_crew.autonudge_service import firing
    from kiro_crew.autonudge_service.model import NudgeLoop

    now = 1_000.0
    loop = NudgeLoop(id="L", slot_key=CONDUCTOR, message="m", idle_secs=3600)

    loop.next_due_ts = now + 3600
    assert firing._pushed_arm_delay(loop, 60.0, now) == 60.0, "room to spare: as asked"

    loop.next_due_ts = now + 5
    assert firing._pushed_arm_delay(loop, 60.0, now) == 5.0, "clamped to the deadline"

    loop.next_due_ts = now - 30
    assert firing._pushed_arm_delay(loop, 60.0, now) == 0.0, "an elapsed deadline fires now"

    loop.next_due_ts = 0.0
    assert firing._pushed_arm_delay(loop, 60.0, now) == 60.0, "no deadline to be later than"

    loop.next_due_ts = now + 5
    assert firing._pushed_arm_delay(loop, 0.0, now) == 0.0, "no window asked, none armed"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, conductor_wake.DEFAULT_WAKE_BATCH_SECS),
        ("", conductor_wake.DEFAULT_WAKE_BATCH_SECS),
        ("   ", conductor_wake.DEFAULT_WAKE_BATCH_SECS),
        ("90", 90.0),
        ("12.5", 12.5),
        ("0", 0.0),
        ("-30", 0.0),
        ("soon", conductor_wake.DEFAULT_WAKE_BATCH_SECS),
        ("nan", conductor_wake.DEFAULT_WAKE_BATCH_SECS),
    ],
)
def test_the_window_is_tunable_and_unreadable_values_fall_back(raw, expected, monkeypatch):
    """A malformed override must not disable the window silently.

    ``nan`` is listed because it PARSES: it would then lose every comparison, so the
    window would read as configured while behaving as none at all.
    """
    if raw is None:
        monkeypatch.delenv(conductor_wake.WAKE_BATCH_ENV, raising=False)
    else:
        monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, raw)
    assert conductor_wake.wake_batch_secs() == expected


def test_the_window_at_zero_restores_the_fire_on_first_report_behaviour(tmp_path, monkeypatch):
    """The documented opt-out: every report arms its own tick again, at delay zero."""
    first = _bind(WORKER)
    second = _bind(WORKER_B)

    async def _drive() -> "tuple[list[float | None], list[int]]":
        svc, arms, delivered, _wakes = _batching_service(tmp_path, monkeypatch, window=0.0)
        await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            for worker, item in ((WORKER, first), (WORKER_B, second)):
                work_ledger.apply_worker_report(CONDUCTOR, item, status="done", summary="s")
                await _fire(svc, worker)
            return arms, list(delivered)
        finally:
            svc.stop()

    arms, _delivered = asyncio.run(_drive())
    assert arms == [0.0, 0.0], "both reports armed their own tick, at once"


def test_a_stub_service_with_no_push_table_has_no_window():
    """A service that keeps no push marks cannot hold a window, so every push arms.

    The direction matters: a missing table must make the push LOUDER (one tick per
    report, which is what shipped), never silently swallow a report into a window
    nothing will ever close.
    """
    _bind()
    svc = _Svc(_Loop())
    assert not hasattr(svc, "_pushed_ticks")
    assert asyncio.run(_fire(svc, WORKER)) == LOOP_ID
    assert asyncio.run(_fire(svc, WORKER)) == LOOP_ID
    assert svc.fired == [LOOP_ID, LOOP_ID], "neither push was folded away"
    assert svc.delays == [conductor_wake.DEFAULT_WAKE_BATCH_SECS] * 2


def test_the_default_window_stays_on_the_seconds_scale():
    """Pinned, because the number is the whole user-visible cost of batching.

    ``rfc-crew-log-wake.md`` promises a ``question`` reaches its conductor in SECONDS,
    and the window IS the added latency of an isolated report, so a default that drifted
    into tens of seconds would quietly reverse that promise. Ten seconds has no measured
    basis -- no distribution of inter-report gaps has been measured -- so what is pinned
    is the scale, plus the exact value so a change to it has to be deliberate.
    """
    assert conductor_wake.DEFAULT_WAKE_BATCH_SECS == 10.0
    assert 0 < conductor_wake.DEFAULT_WAKE_BATCH_SECS <= 30.0, "an isolated report waits seconds"


def test_a_report_after_the_armed_tick_was_cancelled_opens_its_own_window(tmp_path, monkeypatch):
    """A cancelled timer closes the window, even though the push mark survives the cancel.

    ``_cancel_timer`` retires the task and drops the wake and floor-tick claims, but not
    the push mark -- and a user message in the watched session cancels exactly that way.
    If the mark alone opened the window, every report arriving before that user's turn
    ended would join a window nothing will ever close, arm nothing, and wait out the
    loop's whole cadence. The timer table is what settles it: the cancel POPS the task,
    so the next report finds no armed tick and arms one of its own.
    """
    first = _bind(WORKER)
    second = _bind(WORKER_B)

    async def _drive() -> "tuple[list[float | None], bool, bool]":
        svc, arms, _delivered, _wakes = _batching_service(tmp_path, monkeypatch)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        _instrument_arms(svc, arms)
        try:
            work_ledger.apply_worker_report(CONDUCTOR, first, status="done", summary="s")
            await _fire(svc, WORKER)
            assert conductor_wake._window_open(svc, loop.id), "precondition: the window opened"

            # What a user message does: the task is cancelled and popped, the mark stays.
            svc._cancel_timer(loop.id)
            marked = loop.id in svc._pushed_ticks
            still_open = conductor_wake._window_open(svc, loop.id)

            work_ledger.apply_worker_report(
                CONDUCTOR, second, status="question", summary="RULING: a -- b -- a"
            )
            await _fire(svc, WORKER_B)
            return arms, marked, still_open
        finally:
            svc.stop()

    arms, marked, still_open = asyncio.run(_drive())
    assert marked, "the cancel leaves the push mark behind -- that is the gap being closed"
    assert still_open is False, "but with no live timer the window is closed"
    assert arms == [_BATCH_WINDOW, _BATCH_WINDOW], "so the second report armed its own tick"


def test_the_window_needs_both_the_mark_and_a_live_timer():
    """Each half alone leaves the window closed, which is the direction that cannot lose.

    A mark with no armed tick is the cancel case; an armed tick with no mark is an
    ordinary scheduled timer, which carries no batch. Either way the next report arms a
    tick of its own, so the push stays louder rather than quieter.
    """
    from types import SimpleNamespace

    class _Armed:
        def done(self) -> bool:
            return False

    class _Finished:
        def done(self) -> bool:
            return True

    live = SimpleNamespace(_pushed_ticks={"L"}, _timers={"L": _Armed()})
    assert conductor_wake._window_open(live, "L") is True

    cancelled = SimpleNamespace(_pushed_ticks={"L"}, _timers={})
    assert conductor_wake._window_open(cancelled, "L") is False

    finished = SimpleNamespace(_pushed_ticks={"L"}, _timers={"L": _Finished()})
    assert conductor_wake._window_open(finished, "L") is False

    unmarked = SimpleNamespace(_pushed_ticks=set(), _timers={"L": _Armed()})
    assert conductor_wake._window_open(unmarked, "L") is False
