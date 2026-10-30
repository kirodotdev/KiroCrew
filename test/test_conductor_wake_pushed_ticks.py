"""The crew-log wake on the REAL service: what one pushed tick costs its conductor.

``rfc-crew-log-wake``. A push the service cannot take mid-fire re-arms at delay zero
instead of being lost, and a report written across a restart wakes once at boot. A pushed
tick keeps the loop's earlier deadline, observes before it spends, and buys no further
turn without news; and one item cannot spend its conductor's turn budget. The lookup, the
bus trigger, what wakes nobody and the ``worker_closed`` probe input are in
``test_conductor_wake.py``; reports that arrive together and the batch window are in
``test_conductor_wake_batching.py``; the shared ids, stubs and drivers are in
``conductor_wake_helpers.py``.
"""

from __future__ import annotations

import asyncio

from conductor_wake_helpers import _isolated_home  # noqa: F401
from conductor_wake_helpers import CONDUCTOR, WORKER, WORKER_B, _bind, _fire, _item, _service

from kiro_crew import conductor_wake, work_ledger
from kiro_crew.testing.wait import await_until

# ── a push the service cannot take now is not lost ───────────────────────────


def test_a_push_during_the_fire_window_re_arms_at_delay_zero_after_the_cycle(tmp_path, monkeypatch):
    """The REAL service: ``fire_now`` refuses mid-fire, and the cycle's tail honours it.

    The push lands from inside the delivery callback, which is exactly the window
    ``_firing`` covers, and goes through the shared lookup every trigger uses. The tail
    must then arm at delay ZERO -- a deadline arm would hold the worker's report for the
    conductor's whole patrol cadence, since the cycle in flight read the ledger first.
    """
    import kiro_crew.autonudge as autonudge

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    _bind(status="progress")
    pushed: list[str] = []
    arms: list[tuple[str, float | None]] = []

    async def _drive() -> None:
        svc = _service(tmp_path / "an")
        monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")

        async def _on_fire(_loop) -> bool:
            assert loop.id in svc._firing, "precondition: the push lands mid-fire"
            pushed.append(await _fire(svc, WORKER))
            return True

        async def _not_quiet(_loop) -> bool:
            return False

        def _spy_arm(armed, delay=None) -> None:
            arms.append((armed.id, delay))

        svc._on_fire = _on_fire
        svc._monitor_tick_is_quiet = _not_quiet  # type: ignore[method-assign]
        svc._arm_timer = _spy_arm  # type: ignore[method-assign]
        try:
            await svc._timer(loop, delay=0.0)
        finally:
            svc.stop()
        assert arms and arms[-1] == (loop.id, 0.0)
        assert loop.id not in svc._pulled_forward, "the claim is released where applied"

    asyncio.run(_drive())
    assert pushed == [""], "the push itself was refused, as the window requires"


def _restored_service_ticks(
    tmp_path, monkeypatch, *, report_after: str, followup_ticks: int = 0
) -> "tuple[str, list[str]]":
    """Arm a work-ledger loop, tick it quiet, then restart and let ``start()`` run it.

    Returns the loop's id and the loop ids the restarted service delivered a turn for. The first service
    records the fingerprint of the ledger as it stood; *report_after* is a worker status
    written while the gateway is "down", or ``""`` for no new event at all.
    """
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    item_id = _bind(status="progress")
    base = tmp_path / "an"
    fired: list[str] = []
    arms: list[tuple[str, float | None]] = []

    async def _first() -> str:
        svc = _service(base)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        try:
            assert await svc._monitor_tick_is_quiet(loop) is True, "precondition: calm"
            if followup_ticks:
                assert loop.monitor is not None
                loop.monitor.followup_ticks = followup_ticks
            await svc._persist_locked()
        finally:
            svc.stop()
        return loop.id

    loop_id = asyncio.run(_first())
    if report_after:
        work_ledger.apply_worker_report(CONDUCTOR, item_id, status=report_after, summary="x")

    async def _second() -> None:
        async def _on_fire(loop) -> bool:
            fired.append(loop.id)
            return True

        svc = _service(base, on_fire=_on_fire)
        real_arm = svc._arm_timer

        def _spy_arm(armed, delay=None) -> None:
            arms.append((armed.id, delay))
            real_arm(armed, delay)

        svc._arm_timer = _spy_arm  # type: ignore[method-assign]
        try:
            await svc.start()
            assert arms[:1] == [(loop_id, 0.0)], "start() arms a work-ledger loop at once"
            await await_until(
                asyncio.shield(svc._timers[loop_id]), what="the restarted service's boot tick"
            )
        finally:
            svc.stop()

    asyncio.run(_second())
    return loop_id, fired


def test_a_report_written_across_a_restart_wakes_once_at_boot(tmp_path, monkeypatch):
    """The in-process push died with the process; the boot tick reads the ledger itself."""
    loop_id, fired = _restored_service_ticks(tmp_path, monkeypatch, report_after="blocked")
    assert fired == [loop_id]


def test_a_restart_with_nothing_new_ticks_quiet_and_spends_no_turn(tmp_path, monkeypatch):
    _loop_id, fired = _restored_service_ticks(tmp_path, monkeypatch, report_after="")
    assert fired == []


def test_a_restart_holding_a_followup_allowance_still_observes_before_spending(
    tmp_path, monkeypatch
):
    """The boot replay is a pushed tick, so the post-wake follow-up allowance does not apply.

    After a wake the gate lets the next SCHEDULED tick through unobserved, to protect work
    in progress. A loop restored with that allowance outstanding and nothing new in its
    ledger must not spend it on the replay: the replay exists to read the ledger, and a
    turn that skips the read is the cost the replay was built to avoid.
    """
    _loop_id, fired = _restored_service_ticks(
        tmp_path, monkeypatch, report_after="", followup_ticks=1
    )
    assert fired == []


# ── a pushed tick is extra: it keeps the deadline and observes ──────────────


def test_a_quiet_pushed_tick_keeps_the_earlier_deadline(tmp_path, monkeypatch):
    """A push that finds nothing must not move the scheduled check further out.

    Deadline T; a worker's push runs a tick 100 s before it and the gate answers quiet.
    The deadline stays T. The control is the same quiet tick NOT armed by a push, which
    is the loop's own scheduled tick and does re-arm a full interval from now.
    """
    import time

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")

    async def _drive() -> "tuple[float, float, float]":
        async def _never(_loop) -> bool:
            raise AssertionError("a quiet tick spends no turn")

        svc = _service(tmp_path / "an", on_fire=_never)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")

        async def _quiet(_loop) -> bool:
            return True

        svc._monitor_tick_is_quiet = _quiet  # type: ignore[method-assign]
        svc._arm_timer = lambda *_a, **_k: None  # type: ignore[method-assign]
        try:
            # flake-ok: pre-existing wall-clock deadline 100 s ahead that the test only compares with itself, moved verbatim; tracked for a de-flake pass
            deadline = time.time() + 100
            loop.next_due_ts = deadline
            svc._pushed_ticks.add(loop.id)
            await svc._timer(loop, delay=0.0)
            kept = loop.next_due_ts

            loop.next_due_ts = deadline
            # flake-ok: pre-existing wall-clock read before the control tick re-arms, the start of a gap the test bounds from below only, moved verbatim; tracked for a de-flake pass
            before = time.time()
            await svc._timer(loop, delay=0.0)
            return deadline, kept, loop.next_due_ts - before
        finally:
            svc.stop()

    deadline, kept, control_gap = asyncio.run(_drive())
    assert kept == deadline
    # flake-ok: pre-existing lower bound with 1 s slack on the re-armed interval, over a span the test measures itself, moved verbatim; tracked for a de-flake pass
    assert control_gap >= 3600 - 1, "control: an unpushed quiet tick re-arms a full interval"


def _ledger_service(tmp_path, monkeypatch, *, closed: "set[str]"):
    """A real service whose real gate reads the real ledger, counting delivered turns.

    The wake window is OFF here, so every case built on this harness keeps asking what it
    asked before the window existed: what a push, a close and a storm cost in delivered
    turns, with each push arming its own tick. The window's own cases arm it explicitly
    (``_batched_pushes``), and the two together are what say the accounting is the same
    whether reports are batched or not.
    """
    import kiro_crew.autonudge as autonudge

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, "0")
    delivered: list[int] = []
    during: list = []

    async def _on_fire(_loop) -> bool:
        delivered.append(len(delivered) + 1)
        if during:
            await during.pop(0)()
        return True

    svc = _service(tmp_path / "an", on_fire=_on_fire, worker_closed=lambda key: key in closed)
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    return svc, delivered, during


async def _push_and_settle(svc, loop_id: str, worker: str) -> None:
    """One worker push, then every tick it (and any tail it arms) runs, to completion."""
    await _fire(svc, worker)
    for _ in range(5):
        task = svc._timers.get(loop_id)
        if task is None or task.done():
            return
        if loop_id not in svc._pushed_ticks and loop_id not in svc._pushed_running:
            # Only an armed deadline tick is left, hours away: nothing more to settle.
            return
        await await_until(asyncio.shield(task), what="the pushed tick settling")


def test_a_close_after_a_done_wake_buys_no_second_turn(tmp_path, monkeypatch):
    """The done report wakes once; the close that follows it does not.

    A work-ledger wake leaves no follow-up allowance behind. A fresh ``question`` on
    another item is news, and buys exactly one turn.
    """
    closed: set[str] = set()
    done_item = _bind(WORKER)
    other_item = _bind(WORKER_B)

    async def _drive() -> "tuple[list[int], list[int], list[int], int]":
        svc, delivered, _during = _ledger_service(tmp_path, monkeypatch, closed=closed)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        try:
            work_ledger.apply_worker_report(CONDUCTOR, done_item, status="done", summary="d")
            await _push_and_settle(svc, loop.id, WORKER)
            after_done = list(delivered)
            assert loop.monitor.followup_ticks == 0, "the wake left no follow-up"

            closed.add(WORKER)
            await _push_and_settle(svc, loop.id, WORKER)
            after_close = list(delivered)

            work_ledger.apply_worker_report(
                CONDUCTOR, other_item, status="question", summary="RULING: a -- b -- a"
            )
            await _push_and_settle(svc, loop.id, WORKER_B)
            return after_done, after_close, list(delivered), loop.monitor.followup_ticks
        finally:
            svc.stop()

    after_done, after_close, after_question, followups = asyncio.run(_drive())
    assert after_done == [1]
    assert after_close == [1], "the close bought no second delivered turn"
    assert after_question == [1, 2], "a new question in the window is exactly one turn"
    assert followups == 0, "no wake left a free follow-up"


def _wake_then_scheduled_tick(
    tmp_path, monkeypatch, *, after_wake=None, refuse_first: bool = False
) -> "list[int]":
    """A worker's ``done`` wakes the conductor; then the loop's own next tick runs.

    The ledger does not move between the two. *after_wake* runs between them with the
    service and the loop, to record what became of the woken turn. With *refuse_first*
    the wake's first delivery is refused, as a busy slot refuses it.
    """
    item = _bind(WORKER)

    async def _drive() -> "list[int]":
        svc, delivered, _during = _ledger_service(tmp_path, monkeypatch, closed=set())
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        if refuse_first:
            real_on_fire = svc._on_fire
            refusals = [True]

            async def _refuse_once(fired) -> bool:
                if refusals:
                    refusals.pop()
                    return False
                return await real_on_fire(fired)

            svc._on_fire = _refuse_once
        try:
            work_ledger.apply_worker_report(CONDUCTOR, item, status="done", summary="d")
            await _push_and_settle(svc, loop.id, WORKER)
            if after_wake is not None:
                await after_wake(svc, loop)
            # The loop's own scheduled tick, run now instead of an interval from now.
            task = svc._timers.pop(loop.id, None)
            if task is not None:
                task.cancel()
            svc._arm_timer = lambda *_a, **_k: None  # type: ignore[method-assign]
            await svc._timer(loop, delay=0.0)
            return list(delivered)
        finally:
            svc.stop()

    return asyncio.run(_drive())


def test_the_tick_after_a_wake_reads_an_unchanged_ledger_and_spends_nothing(tmp_path, monkeypatch):
    """A work-ledger loop gets no free turn after a wake.

    The conductor's next news is a worker's report, and that report pulls the loop
    forward by itself. So the tick after a delivered wake is observed like any other,
    and an unchanged ledger is quiet.
    """
    assert _wake_then_scheduled_tick(tmp_path, monkeypatch) == [1]


def test_the_tick_after_a_wake_whose_turn_died_delivers_again(tmp_path, monkeypatch):
    """A woken turn that died read nothing, so the next tick brings the news back."""

    async def _died(svc, loop) -> None:
        await svc.notify_cycle_failed(
            loop.slot_key, loop_id=loop.id, expected_generation=loop.config_generation
        )

    assert _wake_then_scheduled_tick(tmp_path, monkeypatch, after_wake=_died) == [1, 2]


def test_the_tick_after_a_wake_whose_turn_never_started_delivers_again(tmp_path, monkeypatch):
    async def _never_started(svc, loop) -> None:
        svc.notify_cycle_start_failed(loop.slot_key)

    assert _wake_then_scheduled_tick(tmp_path, monkeypatch, after_wake=_never_started) == [1, 2]


def test_a_refused_wake_is_delivered_on_the_next_tick(tmp_path, monkeypatch):
    """A slot that refuses the wake's turn does not lose it: the next tick retries."""
    assert _wake_then_scheduled_tick(tmp_path, monkeypatch, refuse_first=True) == [1]


def _storm_during_a_turn(tmp_path, monkeypatch, *, news: bool) -> "list[int]":
    """Ten pushes from two workers land while the woken turn is in flight."""
    first = _bind(WORKER)
    second = _bind(WORKER_B)

    async def _drive() -> "list[int]":
        svc, delivered, during = _ledger_service(tmp_path, monkeypatch, closed=set())
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")

        async def _storm() -> None:
            assert loop.id in svc._firing, "precondition: the storm lands mid-turn"
            for n in range(5):
                for worker, item in ((WORKER, first), (WORKER_B, second)):
                    status = "question" if news and n == 4 and worker == WORKER_B else "progress"
                    work_ledger.apply_worker_report(CONDUCTOR, item, status=status, summary=f"{n}")
                    await _fire(svc, worker)

        during.append(_storm)
        try:
            work_ledger.apply_worker_report(
                CONDUCTOR, first, status="question", summary="RULING: a -- b -- a"
            )
            await _push_and_settle(svc, loop.id, WORKER)
            # Settle whatever the storm's deferred pull-forward armed.
            for _ in range(3):
                task = svc._timers.get(loop.id)
                if task is None or task.done() or loop.id not in svc._pushed_ticks:
                    break
                await await_until(asyncio.shield(task), what="the deferred pull-forward tick")
            return list(delivered)
        finally:
            svc.stop()

    return asyncio.run(_drive())


def test_a_storm_during_a_turn_buys_no_further_turn_without_news(tmp_path, monkeypatch):
    """The delivered turn re-reads the whole ledger; progress after it is not news."""
    assert _storm_during_a_turn(tmp_path, monkeypatch, news=False) == [1]


def test_a_storm_during_a_turn_with_news_buys_exactly_one_more(tmp_path, monkeypatch):
    assert _storm_during_a_turn(tmp_path, monkeypatch, news=True) == [1, 2]


# ── an item cannot spend its conductor's turn budget ─────────────────────────


def test_an_item_pulls_its_conductor_forward_at_most_twelve_times_an_hour(
    tmp_path, monkeypatch, caplog
):
    """The thirteenth report still lands in the ledger; it only stops pulling forward.

    Each report arrives after the previous pushed tick began (the mark is cleared as a
    tick start clears it), so none coalesces and each would arm a new tick. The loop's
    own deadline and active state are untouched, so its scheduled tick still runs.
    """
    import logging

    import kiro_crew.autonudge as autonudge

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    # A window far shorter than the loop's hour-long cadence, so each admitted push arms
    # at the window itself and the clamp against the deadline is not what is being read.
    window = 7.0
    monkeypatch.setenv(conductor_wake.WAKE_BATCH_ENV, str(window))
    item_id = _bind(status="progress")
    arms: list[float | None] = []
    cap = conductor_wake.ITEM_PULLS_PER_HOUR

    async def _drive() -> "tuple[list[str], bool]":
        svc = _service(tmp_path / "an")
        monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        deadline = loop.next_due_ts
        svc._arm_timer = lambda _l, delay=None: arms.append(delay)  # type: ignore[method-assign]
        fired: list[str] = []
        try:
            for n in range(cap + 1):
                work_ledger.apply_worker_report(
                    CONDUCTOR, item_id, status="question", summary=f"{n}"
                )
                fired.append(await _fire(svc, WORKER))
                svc._pushed_ticks.discard(loop.id)
            assert loop.next_due_ts == deadline, "the slow tick's deadline is untouched"
            return fired, loop.active
        finally:
            svc.stop()

    with caplog.at_level(logging.INFO, logger="kiro_crew.conductor_wake"):
        fired, active = asyncio.run(_drive())
    assert cap == 12
    assert fired[0] and fired[:cap] == [fired[0]] * cap, "the first twelve pull forward"
    assert fired[cap] == "", "the thirteenth does not"
    assert arms == [window] * cap, "each admitted push arms one window ahead"
    assert active is True, "the loop and its scheduled tick stay armed"
    assert _item(item_id).summary == str(cap), "the thirteenth report still landed"
    tripped = [r for r in caplog.records if "pull-forwards of loop" in r.getMessage()]
    assert len(tripped) == 1 and tripped[0].levelno == logging.INFO


def _admit_svc():
    """A bare stand-in carrying only the tables ``_admit`` reads and writes."""
    from types import SimpleNamespace

    return SimpleNamespace(
        _pull_forward_counts={},
        _pull_forward_capped=set(),
        _pushed_ticks=set(),
        _firing=set(),
        _pulled_forward=set(),
    )


def test_an_item_whose_window_has_aged_out_leaves_both_tables():
    """A finished item must not retain a stamp list or a capped pair for the loop's life.

    ``_admit`` runs only when a write lands, so an item that was pulled forward and then
    stopped writing (its ``it_<hex>`` retired when its work closed) would keep an
    aged-out key forever -- ``max_cycles = 0`` lets the loop outlive the gateway. The
    sibling-sweep on each admit evicts it, bounding the nested table at the count of
    items with a LIVE pull-forward (the persisted half is bounded the same way).
    """
    svc = _admit_svc()
    window = conductor_wake._ITEM_WINDOW_SECS
    stale, live = "it_stale", "it_live"
    # ``stale`` was pulled forward and capped an hour-plus ago; ``live`` just now.
    svc._pull_forward_counts["L"] = {stale: [1.0], live: [1_000_000.0]}
    svc._pull_forward_capped.update({("L", stale), ("L", live)})
    # A fresh write for ``live`` at a ``now`` past ``stale``'s whole window.
    now = 1_000_000.0 + window + 1.0
    assert conductor_wake._admit(svc, "L", live, now) is True
    per_loop = svc._pull_forward_counts["L"]
    assert stale not in per_loop, "the aged-out item is swept from the stamp table"
    assert ("L", stale) not in svc._pull_forward_capped, "and from the capped table"
    assert live in per_loop, "the item that just wrote is retained"


def test_a_capped_items_pair_clears_once_its_window_empties():
    """A capped item that goes quiet must not strand its ``capped`` pair."""
    svc = _admit_svc()
    window = conductor_wake._ITEM_WINDOW_SECS
    cap = conductor_wake.ITEM_PULLS_PER_HOUR
    item = "it_capped"
    svc._pull_forward_counts["L"] = {item: [5.0] * cap}
    svc._pull_forward_capped.add(("L", item))
    # A new write a full window later: its old stamps all age out, so it admits again
    # (below the cap) and the sweep clears the now-stale pair.
    now = 5.0 + window + 1.0
    assert conductor_wake._admit(svc, "L", item, now) is True
    assert ("L", item) not in svc._pull_forward_capped
