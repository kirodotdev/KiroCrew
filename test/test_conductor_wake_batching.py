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
    and every report would arm, which is the opposite of what these cases measure. Every
    stand-in parks on one event this helper owns. The next arm cancels the stand-in it
    replaces, and ``stop`` cancels what is left in that table and is then followed by
    setting the event, so a park ends on a signal the test sets and is not left waiting.
    """
    release = asyncio.Event()
    real_stop = svc.stop

    def _stop(*args, **kwargs) -> None:
        try:
            real_stop(*args, **kwargs)
        finally:
            release.set()

    def _spy(armed, delay=None) -> None:
        arms.append(delay)
        svc._pushed_ticks.discard(armed.id)
        stale = svc._timers.get(armed.id)
        if stale is not None:
            stale.cancel()
        svc._timers[armed.id] = asyncio.get_running_loop().create_task(release.wait())

    svc._arm_timer = _spy  # type: ignore[method-assign]
    svc.stop = _stop  # type: ignore[method-assign]


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

    A user message cancels via ``_cancel_timer``, which keeps the push mark; if the mark
    opened the window, later reports would wait the whole cadence. The cancel pops the
    timer, so the next report arms its own tick.
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


# ── a report with no company waits out no window ─────────────────────────────


def _board(*items: "tuple[str, str, str, str]") -> dict:
    """A rendered board of ``(item_id, report stamp, state, worker)`` rows.

    A row with a stamp has reported ``done``; one without has not reported.
    """
    return {
        "items": [
            {
                "item_id": item_id,
                "last_report_at": stamp,
                "status": "done" if stamp else None,
                "state": state,
                "worker_session_key": worker or None,
            }
            for item_id, stamp, state, worker in items
        ]
    }


def _see(svc, board: dict, order: "list[str] | None" = None) -> None:
    """Hand *board* to the trigger the way a ``work`` event does, live flags included."""
    asyncio.run(
        conductor_wake._observe_board(
            svc,
            CONDUCTOR,
            conductor_wake._board_items(board),
            order,
            conductor_wake._board_live(board),
        )
    )


@pytest.fixture
def windowed(monkeypatch) -> "_Svc":
    """A stub service under an armed window, so a delay of zero is the rule's doing."""
    monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, str(_BATCH_WINDOW))
    return _Svc(_Loop())


def test_the_only_item_that_can_report_is_armed_at_once(windowed):
    """One worker out, nobody to batch with: its report arms the tick with no window.

    The window buys one turn for several reports. With a single open, bound item no
    second report can arrive, so waiting it out is latency with nothing bought. This is
    every hand-off of a conductor that runs its items one after another.
    """
    _see(windowed, _board(("it_a", "", "open", WORKER)))
    _see(windowed, _board(("it_a", "t1", "open", WORKER)))
    assert windowed.fired == [LOOP_ID]
    assert windowed.delays == [0.0]


def test_a_report_with_another_worker_still_out_waits_the_window(windowed):
    """Two workers out: the first report still arms one window ahead, so the second,
    finishing close behind, shares its turn."""
    _see(windowed, _board(("it_a", "", "open", WORKER), ("it_b", "", "open", WORKER_B)))
    _see(windowed, _board(("it_a", "t1", "open", WORKER), ("it_b", "", "open", WORKER_B)))
    assert windowed.delays == [_BATCH_WINDOW]


@pytest.mark.parametrize(
    "other",
    [("it_a", "t0", "accepted", WORKER), ("it_a", "", "open", "")],
    ids=["closed", "never-dispatched"],
)
def test_an_item_no_report_can_come_from_is_not_company(windowed, other):
    """A closed item's worker has no report channel, and an item nobody was dispatched
    to has no worker: neither can send the second report a window waits for."""
    _see(windowed, _board(other, ("it_b", "", "open", WORKER_B)))
    _see(windowed, _board(other, ("it_b", "t1", "open", WORKER_B)))
    assert windowed.delays == [0.0]


def test_the_last_worker_out_is_armed_at_once_after_the_others_closed(windowed):
    """A round of two: the first report waits the window, and once that item is closed
    the second one's report has no company left."""
    _see(windowed, _board(("it_a", "", "open", WORKER), ("it_b", "", "open", WORKER_B)))
    _see(windowed, _board(("it_a", "t1", "open", WORKER), ("it_b", "", "open", WORKER_B)))
    _see(windowed, _board(("it_a", "t1", "accepted", WORKER), ("it_b", "", "open", WORKER_B)))
    _see(windowed, _board(("it_a", "t1", "accepted", WORKER), ("it_b", "t2", "open", WORKER_B)))
    assert windowed.delays == [_BATCH_WINDOW, 0.0]


def test_a_board_never_seen_whole_keeps_the_window(windowed):
    """With nothing known about who else is out, a second worker may be about to
    report, so the window stays. Two ways to know nothing: a push for a board the
    trigger never observed, and a delta that arrived before any whole board."""
    _bind()
    assert asyncio.run(_fire(windowed, WORKER)) == LOOP_ID
    assert windowed.delays == [_BATCH_WINDOW]

    conductor_wake._registry.note_live(CONDUCTOR, {"it_a": True}, ["it_a"])
    assert conductor_wake._registry.reports_alone(CONDUCTOR, "it_a") is False


def test_a_delta_keeps_the_live_set_in_step_with_the_board(windowed):
    """A delta carries only what changed. A closed item leaves the set, an item the
    board stops naming leaves it, and an item the delta did not send stays."""
    registry = conductor_wake._registry
    _see(windowed, _board(("it_a", "", "open", WORKER), ("it_b", "", "open", WORKER_B)))
    assert registry.reports_alone(CONDUCTOR, "it_a") is False

    closed = _board(("it_b", "", "accepted", WORKER_B))
    _see(windowed, closed, ["it_a", "it_b"])
    assert registry.live[CONDUCTOR] == {"it_a"}, "the unsent item stayed, the closed one left"

    _see(windowed, _board(("it_c", "", "open", WORKER_C)), ["it_a", "it_c"])
    assert registry.reports_alone(CONDUCTOR, "it_a") is False, "a new worker is company"

    _see(windowed, _board(), ["it_a"])
    assert registry.reports_alone(CONDUCTOR, "it_a") is True, "and one the board dropped is not"


def test_a_lone_report_cuts_an_open_window_short():
    """A window an earlier report opened is not waited out once the reporter is alone.

    The tick armed at once replaces the armed one and reads the ledger when it runs, so
    it delivers the earlier report too. Joining the open window instead would hold a
    report nobody can add to.
    """
    from types import SimpleNamespace

    svc = _Svc(_Loop())
    svc._pushed_ticks = {LOOP_ID}
    svc._timers = {LOOP_ID: SimpleNamespace(done=lambda: False, get_loop=lambda: None)}
    assert conductor_wake._window_open(svc, LOOP_ID), "precondition: a window is open"

    _see(svc, _board(("it_a", "t1", "accepted", WORKER), ("it_b", "", "open", WORKER_B)))
    _see(svc, _board(("it_a", "t1", "accepted", WORKER), ("it_b", "t2", "open", WORKER_B)))
    assert svc.fired == [LOOP_ID], "armed, not joined"
    assert svc.delays == [0.0]


def test_a_lone_report_landing_mid_cycle_is_deferred_not_dropped(monkeypatch):
    """The conductor's cycle is still running when the report lands: ``fire_now`` refuses,
    and the refusal is remembered so that cycle's tail arms the tick the report asked for."""
    monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, str(_BATCH_WINDOW))
    svc = _Svc(_Loop(), refuse="the loop is mid-fire")
    _see(svc, _board(("it_a", "", "open", WORKER)))
    _see(svc, _board(("it_a", "t1", "open", WORKER)))
    assert svc.fired == []
    assert svc.deferred == [LOOP_ID]
    assert svc.delays == [0.0]


def test_a_worker_that_never_reports_arms_nothing(windowed):
    """The live set is read only when a report arrives. A lone worker that stays silent
    moves no stamp, so nothing is pushed and the loop's own tick is what finds it."""
    _see(windowed, _board(("it_a", "", "open", WORKER)))
    _see(windowed, _board(("it_a", "", "open", WORKER)))
    assert windowed.fired == [] and windowed.delays == []


def test_a_board_that_leaves_the_registry_takes_its_live_set_with_it(windowed):
    """The live set is bounded by the stamps' own bound: one drop removes both."""
    _see(windowed, _board(("it_a", "", "open", WORKER)))
    assert CONDUCTOR in conductor_wake._registry.live
    conductor_wake._registry.forget(CONDUCTOR)
    assert CONDUCTOR not in conductor_wake._registry.live

    _see(windowed, _board(("it_a", "", "open", WORKER)))
    conductor_wake.dispose_all()
    assert conductor_wake._registry.live == {}
