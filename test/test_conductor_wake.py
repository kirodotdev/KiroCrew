"""The crew-log wake: a worker's REPORT pulls its conductor forward, and nothing else.

``rfc-crew-log-wake``. One lookup (``conductor_wake``) and one trigger -- the ``work``
fold advancing on the crew-log bus. What is pinned here is the WIRING: that the trigger
finds the conductor's armed work-ledger loop and calls ``fire_now`` on it, that it does
so only for a report whose status the conductor must act on, and that a worker's session
closing or a turn of its ending reaches no wake at all. What the conductor then DOES with
the tick is Phase 3's gate and is pinned in ``test_probe_work_ledger.py``; the one piece
of that this file does own is the ``worker_closed`` probe input, because that is what
covers a worker whose session is gone now that its close pushes nothing.

No gateway runs in most of these. The service is a stub with the three attributes the
lookup reads, which is what makes a wiring defect here impossible to mistake for a
service defect.

What the real service does with a push is in two siblings: one pushed tick (a push it
cannot take mid-fire, the deadline it keeps, the turn budget) in
``test_conductor_wake_pushed_ticks.py``, and reports that arrive together with the batch
window in ``test_conductor_wake_batching.py``. The ids, stubs and drivers these modules
share are in ``conductor_wake_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from conductor_wake_helpers import _isolated_home  # noqa: F401
from conductor_wake_helpers import (
    CONDUCTOR,
    LOOP_ID,
    WORKER,
    _bind,
    _fire,
    _item,
    _Loop,
    _service,
    _Svc,
)

from kiro_crew import conductor_wake, work_ledger
from kiro_crew.testing.wait import async_wait_until, default_timeout

# ── the shared lookup ────────────────────────────────────────────────────────


def test_a_bound_workers_slot_resolves_its_conductors_armed_work_ledger_loop():
    _bind()
    svc = _Svc(_Loop())
    assert asyncio.run(_fire(svc, WORKER)) == LOOP_ID
    assert svc.fired == [LOOP_ID]


def test_an_unbound_slot_fires_nothing():
    """The whole no-op case: a slot no board names as a worker resolves nothing.

    A conductor's OWN slot takes this path too when its session closes or its turn
    ends; no board lists a conductor as its worker, so it resolves nothing without this
    module knowing what a conductor is.
    """
    _bind()
    svc = _Svc(_Loop())
    assert asyncio.run(_fire(svc, "chat-stranger")) == ""
    assert svc.fired == []


def test_a_loop_watching_another_kind_is_not_fired():
    """Its budget was armed for a pull request; ledger news is not its subject."""
    _bind()
    svc = _Svc(_Loop(kind="gh-pr"))
    assert asyncio.run(_fire(svc, WORKER)) == ""
    assert svc.fired == []


def test_an_inactive_loop_is_not_fired():
    _bind()
    svc = _Svc(_Loop(active=False))
    assert asyncio.run(_fire(svc, WORKER)) == ""
    assert svc.fired == []


def test_a_conductor_with_no_loop_at_all_fires_nothing():
    _bind()
    svc = _Svc(None)
    assert asyncio.run(_fire(svc, WORKER)) == ""


def test_a_monitor_record_from_a_newer_gateway_is_not_pulled_forward():
    """``fire_now`` arms through ``_arm_timer``, which carries no version test.

    ``_arm_from_deadline`` refuses a record this gateway cannot interpret, because running
    the loop would deliver an unattended turn under a newer gateway's policy. A work-ledger
    watch is a ``gate=True`` prompt loop, so ``_load`` leaves such a row ACTIVE -- so the
    push has to carry the same refusal or it is a way around that one.
    """
    from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

    _bind()
    loop = _Loop()
    loop.monitor.version = MONITOR_STATE_VERSION + 1
    svc = _Svc(loop)
    assert asyncio.run(_fire(svc, WORKER)) == ""
    assert svc.fired == []


def test_the_startup_resume_refuses_a_newer_gateways_record():
    """The same refusal on the boot path, where the zero-delay arm bypassed it.

    Asserted through the predicate the resume branches on rather than by booting a service:
    a False answer sends the row to ``_arm_from_deadline``, which already refuses it and
    logs why, so the refusal lives in one place.
    """
    from kiro_crew.autonudge import AutoNudgeService
    from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

    current = _Loop()
    assert AutoNudgeService._observes_work_ledger(None, current) is True
    future = _Loop()
    future.monitor.version = MONITOR_STATE_VERSION + 1
    assert AutoNudgeService._observes_work_ledger(None, future) is False
    assert AutoNudgeService._observes_work_ledger(None, _Loop(kind="gh-pr")) is False
    assert AutoNudgeService._observes_work_ledger(None, _Loop(kind="")) is False


def test_a_refused_fire_does_not_raise_and_records_the_deferred_pull_forward():
    """``fire_now``'s mid-fire 409 is an answer, not a fault.

    Two things are pinned: the caller sees ``""`` rather than an exception -- a trigger
    that has already observed the entry must not break on the attempt to tell someone --
    and ``defer_if_firing`` is passed, so the re-arm at the end of the in-flight cycle
    runs at delay zero instead of aiming at the loop's own deadline.
    """
    _bind()
    svc = _Svc(_Loop(), refuse="loop is already firing")
    assert asyncio.run(_fire(svc, WORKER)) == ""
    assert svc.fired == []
    assert svc.deferred == [LOOP_ID]


# ── trigger one: the ``work`` fold advancing on the crew-log bus ──────────────

#: The status an item carries when its status is not what the case is about: a waking
#: one, so a moved stamp pushes and the case reads the DIFF rather than the gate. The
#: gate itself has its own cases further down.
_WAKING = "done"


def _rows(*items) -> "list[tuple[str, str, str]]":
    """``(item_id, stamp)`` or ``(item_id, stamp, status)`` as the registry reads them."""
    return [(row[0], row[1], row[2] if len(row) > 2 else _WAKING) for row in items]


def _event(board: str, items: list, *, scope: str = "slot", fold: str = "work"):
    """A ``FoldAdvanced`` as ``eager._publish_fold`` builds one for *board*'s work fold."""
    from kiro_crew.crew_log import bus

    value = {
        "conductor": {"slot": board},
        "items": [
            {
                "item_id": item_id,
                "last_report_at": stamp,
                "status": status,
                "state": "open",
            }
            for item_id, stamp, status in _rows(*items)
        ],
        "omitted": 0,
    }
    return bus.FoldAdvanced(scope=scope, key=board, fold=fold, revision=1, value=value, seq=1)


def _record_fires(monkeypatch) -> "list[tuple[str, str]]":
    """Replace ``_fire`` with a recorder of ``(board, item_id)``."""
    fired: list[tuple[str, str]] = []

    async def _fake_fire(_svc, board, item_id=""):
        fired.append((board, item_id))
        return LOOP_ID

    monkeypatch.setattr(conductor_wake, "_fire", _fake_fire)
    return fired


def test_a_workers_report_moves_its_stamp_and_fires_for_that_item(monkeypatch):
    """The diff is the whole selection rule: the item whose ``last_report_at`` moved is
    the worker that reported, and the fire names it so the pull-forward cap counts it."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    first = _rows(("it_a", "t1"), ("it_b", "t1"))
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, first))
    fired.clear()
    moved = _rows(("it_a", "t2"), ("it_b", "t1"))
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, moved))
    assert fired == [(CONDUCTOR, "it_a")]


def test_a_conductors_own_write_moves_no_stamp_and_pushes_nothing(monkeypatch):
    """``create``, ``bind``, ``decide`` and ``close`` advance the fold too, and none of
    them is a worker's news. With every stamp unchanged the subscriber does nothing --
    which is also what keeps a NESTED conductor's bookkeeping on its own board from
    pulling its parent forward: the parent's board did not move."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    items = _rows(("it_a", "t1"))
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, items))
    fired.clear()
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, items + _rows(("it_new", ""))))
    assert fired == []


# ── the gate: only a report the conductor must act on pushes ─────────────────


def test_a_progress_report_moves_the_stamp_and_pushes_nothing(monkeypatch):
    """A moved stamp is necessary and not sufficient.

    ``progress`` says the worker is moving and needs nobody, and the conductor's own
    gate refuses it, so pulling the tick forward would buy a cycle that reads the board
    and answers quiet. The write still landed and the loop's scheduled tick still reads
    it; what is withheld is only the early tick.
    """
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t1", "progress"))))
    fired.clear()
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t2", "progress"))))
    assert fired == []
    assert conductor_wake._registry.boards[CONDUCTOR] == {"it_a": "t2"}, "still recorded"


def test_each_waking_status_pushes(monkeypatch):
    """``done``, ``blocked`` and ``question`` are the whole waking set.

    Driven one status at a time from a fresh registry, so each answer is its own and not
    a diff against a sibling's stamp.
    """
    for status in ("done", "blocked", "question"):
        conductor_wake.reset_for_tests()
        fired = _record_fires(monkeypatch)
        svc = _Svc(_Loop())
        asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t1", status))))
        asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t2", status))))
        assert fired == [(CONDUCTOR, "it_a")], status


def test_an_item_that_has_not_reported_yet_pushes_nothing(monkeypatch):
    """A board whose item carries no status is a ``create`` or a ``bind``, not news."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t1", ""))))
    fired.clear()
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t2", ""))))
    assert fired == []


def test_the_statuses_this_trigger_pushes_for_are_the_ones_the_gate_spends_on():
    """One set, two readers.

    The trigger pulls a tick forward and the probe decides whether that tick spends a
    turn. A status in one set and not the other is either a wake nobody can see is
    wasted or news that waits out the whole patrol cadence, so both read
    ``work_vocab.WORK_WAKE_STATUSES`` and this pins them equal.
    """
    from kiro_crew import ledger_wake
    from kiro_crew.work_vocab import WORK_WAKE_STATUSES, WORK_WORKER_STATUSES

    assert ledger_wake.WAKE_STATUSES == frozenset(WORK_WAKE_STATUSES)
    assert set(WORK_WAKE_STATUSES) < set(WORK_WORKER_STATUSES)
    assert "progress" not in WORK_WAKE_STATUSES


def test_the_first_board_seen_records_and_pushes_nothing(monkeypatch):
    """MUTATION-SENSITIVE. The first sight is the join's baseline: the board as it stood,
    which says nothing new. At boot ``start`` already armed the loop at delay zero to read
    it, and a push into that tick would buy a second cycle at its tail. The next report
    is diffed against what was recorded."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t1"), ("it_b", ""))))
    assert fired == []
    assert conductor_wake._registry.boards[CONDUCTOR] == {"it_a": "t1", "it_b": ""}
    asyncio.run(
        conductor_wake._observe_board(svc, CONDUCTOR, _rows(("it_a", "t1"), ("it_b", "t2")))
    )
    assert fired == [(CONDUCTOR, "it_b")]


def test_two_reports_in_one_second_each_push(monkeypatch):
    """``last_report_at`` has whole-second resolution; the newest report event's id is
    what tells two same-second reports apart."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())

    def board(*event_ids: str) -> list:
        value = {
            "items": [
                {
                    "item_id": "it_a",
                    "last_report_at": "2026-10-02T00:00:01Z",
                    "status": _WAKING,
                    "worker_session_key": WORKER,
                    "state": "open",
                    "events": [{"id": e, "kind": "report"} for e in event_ids],
                }
            ]
        }
        return conductor_wake._board_items(value)

    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, board("ev1")))
    fired.clear()
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, board("ev1", "ev2")))
    assert fired == [(CONDUCTOR, "it_a")]
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, board("ev1", "ev2")))
    assert fired == [(CONDUCTOR, "it_a")], "an unmoved board pushes nothing"


def test_a_same_second_report_with_the_same_event_id_still_pushes_on_new_fields(
    monkeypatch,
):
    """A report event id is content-addressed over status and summary, so a second report
    in the same second with the same pair but new artifacts or pr shares it. The stamp
    digests every field a report writes, so that report still pushes."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())

    def board(artifacts: dict) -> list:
        value = {
            "items": [
                {
                    "item_id": "it_a",
                    "last_report_at": "2026-10-02T00:00:01Z",
                    "status": "done",
                    "summary": "s",
                    "artifacts": artifacts,
                    "pr": None,
                    "events": [{"id": "ev1", "kind": "report"}],
                }
            ]
        }
        return conductor_wake._board_items(value)

    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, board({})))
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, board({"pr": "16076"})))
    assert fired == [(CONDUCTOR, "it_a")]


def test_the_report_fields_this_module_digests_are_the_folds():
    from kiro_crew.crew_log import projection

    assert conductor_wake._REPORT_FIELDS == projection._WORK_WORKER_FIELDS


def test_a_board_with_no_active_work_ledger_loop_is_not_retained(monkeypatch):
    """The registry's bound: a board nobody is watching is read and dropped, so the table
    holds at most one entry per active work-ledger loop."""
    fired = _record_fires(monkeypatch)
    items = _rows(("it_a", "t1"))
    asyncio.run(conductor_wake._observe_board(_Svc(_Loop()), CONDUCTOR, items))
    assert CONDUCTOR in conductor_wake._registry.boards
    asyncio.run(conductor_wake._observe_board(_Svc(_Loop(active=False)), CONDUCTOR, items))
    assert CONDUCTOR not in conductor_wake._registry.boards
    assert fired == []


def test_a_board_whose_loop_ended_is_swept_on_the_next_observation(monkeypatch):
    """The bound the class promises: a board retained while its loop ran leaves on the
    next observation of ANY board once that loop is gone, not only on a further write to
    its own -- a conductor that closed its last item and whose loop then hit its cap
    writes nothing more."""
    _record_fires(monkeypatch)
    live: dict[str, bool] = {"chat-c1": True, "chat-c2": True}
    svc = _Svc(_Loop())
    monkeypatch.setattr(
        svc, "get_by_slot", lambda slot_key: _Loop(active=live.get(slot_key, False))
    )
    asyncio.run(conductor_wake._observe_board(svc, "chat-c1", _rows(("it_a", "t1"))))
    asyncio.run(conductor_wake._observe_board(svc, "chat-c2", _rows(("it_b", "t1"))))
    assert set(conductor_wake._registry.boards) == {"chat-c1", "chat-c2"}
    live["chat-c1"] = False
    asyncio.run(conductor_wake._observe_board(svc, "chat-c2", _rows(("it_b", "t2"))))
    assert set(conductor_wake._registry.boards) == {"chat-c2"}
    # A loop-table change sweeps too, through the subscription sync.
    live["chat-c2"] = False
    monkeypatch.setattr(svc, "list_all", lambda: [], raising=False)

    async def _sync() -> None:
        conductor_wake.sync_subscriptions(svc)

    asyncio.run(_sync())
    assert conductor_wake._registry.boards == {}


def test_the_service_stop_clears_the_registry():
    conductor_wake._registry.observe(CONDUCTOR, _rows(("it_a", "t1")))
    conductor_wake.dispose_all()
    assert conductor_wake._registry.boards == {}


def test_other_scopes_and_folds_are_not_this_subscribers(monkeypatch):
    """A session-scoped event and another slot fold cost one comparison each."""
    import kiro_crew.autonudge as autonudge

    monkeypatch.setattr(autonudge, "get_instance", lambda: _Svc(_Loop()))
    assert conductor_wake._observe_event(_event(CONDUCTOR, [], scope="session")) is None
    assert conductor_wake._observe_event(_event(CONDUCTOR, [], fold="ledger")) is None
    assert conductor_wake._observe_event(object()) is None


def test_a_work_fold_event_is_handed_to_the_service_loop(monkeypatch):
    """The bus fans out on the fold worker's thread; the callback copies the stamps and
    schedules the observation on the service's loop, then returns without waiting.

    A real event loop runs on a helper thread so ``run_coroutine_threadsafe`` has a
    live target, mirroring the fold worker calling into a gateway loop.
    """
    import threading

    import kiro_crew.autonudge as autonudge

    fired = _record_fires(monkeypatch)
    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def _run():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    ready.wait(timeout=5)
    svc = _Svc(_Loop())
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    monkeypatch.setattr(conductor_wake, "_service_loop", lambda _s: loop)
    try:
        first = conductor_wake._observe_event(_event(CONDUCTOR, [("it_a", "t1")]))
        assert first is not None
        first.result(timeout=default_timeout())
        assert set(conductor_wake._registry.boards[CONDUCTOR]) == {"it_a"}
        moved = conductor_wake._observe_event(_event(CONDUCTOR, [("it_a", "t2")]))
        assert moved is not None
        moved.result(timeout=default_timeout())
        assert fired == [(CONDUCTOR, "it_a")], "the report reached the loop and fired"
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()


# ── the keyed bus subscription, end to end ───────────────────────────────────
#
# These run the REAL path: a crew log on disk, the real append, the eager folder's own
# worker thread folding and publishing, the bus routing by key, and this module's
# callback scheduling onto a real event loop. Only the service is a stub, for the reason
# the module docstring gives.

OTHER = "chat-other-conductor"


class _Listing(_Svc):
    """A stub service whose loop table can be walked, as ``sync_subscriptions`` does."""

    def list_all(self):
        return [self._loop] if self._loop is not None else []


@pytest.fixture
def real_crew_log(monkeypatch):
    """A live crew log with the eager folder running and an empty bus."""
    from kiro_crew.crew_log import bus
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log import projection as crew_log

    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    yield
    conductor_wake.dispose_all()
    bus.reset_for_tests()
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()


def _unit(unit: str, slot: str) -> None:
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)


def _write(unit: str, **fields) -> None:
    """Append one ``work/recorded`` entry through the real append path, then let the
    eager folder drain it -- one batch, folded and published."""
    from kiro_crew.crew_log import eager as crew_log_eager
    from kiro_crew.crew_log import emit as crew_log_emit

    assert crew_log_emit.on_work_recorded(unit, dict(fields), timeout=5.0) is True
    crew_log_emit.flush(timeout=5.0)
    assert crew_log_eager.drain(timeout=10.0)


def _board(unit: str, board: str, worker: str) -> None:
    """A conductor's board with one item, ``it_p``, bound to *worker*."""
    base = {"slot": board, "actor": "conductor", "by": board}
    _unit(unit, board)
    _write(unit, **base, action="goal", goal="g", round=1)
    _write(
        unit,
        **base,
        action="create",
        item_id="it_p",
        title="t",
        acceptance={"kind": "human_approval"},
    )
    _write(unit, **base, action="bind", item_id="it_p", worker_session_key=worker)


#: Each board's own unit, where the report route writes the board's copy of a report.
_BOARD_UNITS = {CONDUCTOR: "acp-c", OTHER: "acp-o"}


def _report(unit: str, board: str, worker: str, stamp: str, *, status: str = "done") -> None:
    """A worker's report as the route records it: the worker's copy, then the board's."""
    fields = dict(
        slot=board,
        actor="worker",
        by=worker,
        action="report",
        item_id="it_p",
        status=status,
        summary="s",
        last_report_at=stamp,
    )
    _write(unit, **fields)
    if _BOARD_UNITS[board] != unit:
        _write(_BOARD_UNITS[board], **fields)


async def _until(predicate, timeout: float | None = None) -> bool:
    """True once ``predicate()`` holds; a wait that never does fails by name."""
    await async_wait_until(predicate, timeout=timeout)
    return True


async def _sync_and_join(svc) -> None:
    """Sync the subscriptions, then wait for every join it started to land."""
    conductor_wake.sync_subscriptions(svc)
    await asyncio.gather(*list(conductor_wake._subscriptions.joining.values()))
    # A board with no warm cell gets no baseline on the join: one background prime folds
    # it and its first push is the board's first sight. Wait for that prime, then for the
    # observations it scheduled on this loop, so every case starts from the board's
    # first sight already recorded.
    from kiro_crew.crew_log import slot_prime

    assert await asyncio.to_thread(slot_prime.drain, 30.0), "a background prime never finished"
    live = set(conductor_wake._subscriptions.live)
    await _until(lambda: live <= set(conductor_wake._registry.boards))


def _on_this_loop(monkeypatch, svc) -> None:
    """Point the module at *svc* and at the test's own running loop."""
    import kiro_crew.autonudge as autonudge

    running = asyncio.get_running_loop()
    monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
    monkeypatch.setattr(conductor_wake, "_service_loop", lambda _s: running)


def _spy_events(monkeypatch) -> "list[tuple[str, str, str]]":
    """Record the (scope, key, fold) of every event the bus hands this module."""
    seen: "list[tuple[str, str, str]]" = []
    original = conductor_wake._observe_event

    def _spy(event):
        seen.append(
            (getattr(event, "scope", ""), getattr(event, "key", ""), getattr(event, "fold", ""))
        )
        return original(event)

    monkeypatch.setattr(conductor_wake, "_observe_event", _spy)
    return seen


def test_a_workers_report_wakes_its_conductor_through_the_bus(real_crew_log, monkeypatch):
    """MUTATION-SENSITIVE. A worker's ``work_report`` append wakes the bound conductor's
    loop within the one eager batch that folds it -- through the bus, not through a call
    the folder makes.

    The baseline is the board's first sight and pushes nothing; the report then moves
    ``it_p``'s stamp and fires for that item. Re-adding a direct call from the eager
    folder would leave this green, so ``test_the_eager_folder_names_no_conductor`` pins
    that side.
    """
    _board("acp-c", CONDUCTOR, WORKER)
    _unit("acp-w", WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        await _sync_and_join(svc)
        assert set(conductor_wake._subscriptions.live) == {CONDUCTOR}
        assert svc.fired == [], "the baseline is the first sight and pushes nothing"
        assert set(conductor_wake._registry.boards[CONDUCTOR]) == {"it_p"}
        await asyncio.to_thread(_report, "acp-w", CONDUCTOR, WORKER, "2026-10-02T00:00:01Z")
        assert await _until(lambda: svc.fired), "the report reached the conductor's loop"
        assert svc.fired == [LOOP_ID]

    asyncio.run(body())


def test_a_progress_report_wakes_nobody_through_the_bus(real_crew_log, monkeypatch):
    """END TO END, and the case the whole change exists for.

    A worker that stops to wait reports ``progress``. That entry folds, the board's
    revision advances and ``it_p``'s stamp moves, so every step before the gate happens
    exactly as for a waking report -- and the conductor's loop is still not fired. The
    ``question`` that follows proves the path is live rather than broken: the same
    worker, the same board, one eager batch later, and the loop fires.
    """
    _board("acp-c", CONDUCTOR, WORKER)
    _unit("acp-w", WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        await _sync_and_join(svc)
        before = dict(conductor_wake._registry.boards[CONDUCTOR])
        await asyncio.to_thread(
            _report, "acp-w", CONDUCTOR, WORKER, "2026-10-02T00:00:01Z", status="progress"
        )
        assert await _until(
            lambda: conductor_wake._registry.boards[CONDUCTOR] != before
        ), "the progress report folded and the registry saw its stamp move"
        assert svc.fired == [], "and it bought its conductor no tick"
        await asyncio.to_thread(
            _report, "acp-w", CONDUCTOR, WORKER, "2026-10-02T00:00:02Z", status="question"
        )
        assert await _until(lambda: svc.fired), "the question did reach the loop"
        assert svc.fired == [LOOP_ID]

    asyncio.run(body())


def test_only_the_watched_boards_work_fold_reaches_the_subscriber(real_crew_log, monkeypatch):
    """MUTATION-SENSITIVE: the subscription is KEYED. Another conductor's board, and every
    session-scoped fold the same writes advance, never reach this module -- an unkeyed
    subscription would hand it every event the folder publishes and this goes red."""
    seen = _spy_events(monkeypatch)
    _board("acp-c", CONDUCTOR, WORKER)
    _board("acp-o", OTHER, "chat-other-worker")
    _unit("acp-w", WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        await _sync_and_join(svc)
        svc.fired.clear()
        await asyncio.to_thread(
            _report, "acp-o", OTHER, "chat-other-worker", "2026-10-02T00:00:02Z"
        )
        await asyncio.to_thread(_report, "acp-w", CONDUCTOR, WORKER, "2026-10-02T00:00:03Z")
        assert await _until(lambda: svc.fired)
        assert svc.fired == [LOOP_ID], "one wake, for the watched board's report only"

    asyncio.run(body())
    assert seen, "the watched board's events arrived"
    assert set(seen) == {("slot", CONDUCTOR, "work")}


def test_a_disposed_subscription_wakes_nothing(real_crew_log, monkeypatch):
    """The loop ends, the next loop-table sync disposes the board's subscription, and a
    later report reaches neither the module nor the loop."""
    from kiro_crew.crew_log import bus

    seen = _spy_events(monkeypatch)
    _board("acp-c", CONDUCTOR, WORKER)
    _unit("acp-w", WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        await _sync_and_join(svc)
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 1
        svc._loop.active = False
        conductor_wake.sync_subscriptions(svc)
        assert conductor_wake._subscriptions.joining == {}
        assert conductor_wake._subscriptions.live == {}
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 0
        seen.clear()
        svc.fired.clear()
        await asyncio.to_thread(_report, "acp-w", CONDUCTOR, WORKER, "2026-10-02T00:00:04Z")
        await asyncio.sleep(0.2)
        assert svc.fired == []

    asyncio.run(body())
    assert seen == []


def test_the_service_stop_disposes_every_subscription(real_crew_log, monkeypatch):
    from kiro_crew.crew_log import bus

    _board("acp-c", CONDUCTOR, WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        await _sync_and_join(svc)
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 1
        conductor_wake.dispose_all()
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 0
        # A second sync joins again: a restarted service re-subscribes.
        await _sync_and_join(svc)
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 1

    asyncio.run(body())


def test_a_join_that_lands_after_stop_keeps_nothing(real_crew_log, monkeypatch):
    """A join in flight when the service stops disposes what it gets."""
    from kiro_crew.crew_log import bus

    _board("acp-c", CONDUCTOR, WORKER)

    async def body() -> None:
        svc = _Listing(_Loop())
        _on_this_loop(monkeypatch, svc)
        conductor_wake.sync_subscriptions(svc)
        joins = list(conductor_wake._subscriptions.joining.values())
        conductor_wake.dispose_all()
        await asyncio.gather(*joins)
        assert conductor_wake._subscriptions.live == {}
        assert bus.subscriber_count(bus.FOLD_ADVANCED) == 0

    asyncio.run(body())


def test_the_eager_folder_names_no_conductor():
    """MUTATION-SENSITIVE: the folder folds and publishes; it does not know who listens.
    A direct call into ``conductor_wake`` from it -- the shape this change removed --
    fails here."""
    import ast
    import inspect

    from kiro_crew.crew_log import eager

    tree = ast.parse(inspect.getsource(eager))
    named = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("conductor_wake" in name for name in named)
    assert "conductor_wake" not in {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def test_the_service_start_installs_one_observer(monkeypatch):
    """``install`` attaches its loop-table observer once per service object, however
    often the service starts."""
    attached: list[object] = []

    class _Observed(_Listing):
        def subscribe(self, cb):
            attached.append(cb)

    svc = _Observed(None)

    async def body() -> None:
        conductor_wake.install(svc)
        conductor_wake.install(svc)

    asyncio.run(body())
    assert len(attached) == 1


# ── what wakes nobody: a close, and a turn end ───────────────────────────────


def test_a_worker_session_closing_reaches_no_wake():
    """A close carries no statement from the worker, so it wakes nobody.

    Structural, and that is the point: there is no hook to call. A close is the
    dashboard's event, not the worker's word, so a push from one tells a conductor
    something it cannot tell apart from the worker having spoken. What covers a worker
    whose session is gone is the item's own ``stale`` flag on the next patrol tick --
    no window needed, as ``is_stale`` answers at once for a closed worker that has
    reported -- and that same flag is the only cover for a worker whose process dies
    with its slot still open.
    """
    import importlib
    import pkgutil

    from kiro_crew.dashboard import chat_api, chat_handlers

    # The handlers facade and every ``chat_api`` owner it composes.
    owners = [
        importlib.import_module(f"{chat_api.__name__}.{info.name}")
        for info in pkgutil.iter_modules(chat_api.__path__)
    ]
    for module in (chat_handlers, *sorted(owners, key=lambda owner: owner.__name__)):
        source = inspect.getsource(module)
        assert "conductor_wake" not in source, f"{module.__name__} reaches the wake again"


def test_a_turn_ending_reaches_no_wake():
    """A turn end says nothing about what the turn DID, so it wakes nobody.

    The hook runs after HOOK_EVENT_STOP for every turn of every session, so a turn that
    raised, produced nothing, parked itself in a wait, or reported ``progress`` all
    reach it identically. A push from there buys a cycle the gate answers quiet, and the
    quiet streak's floor then delivers a turn anyway.
    """
    from kiro_crew.autonudge_service import timers

    assert "conductor_wake" not in inspect.getsource(timers)


def test_a_turn_end_on_a_bound_workers_slot_fires_nothing(tmp_path, monkeypatch):
    """The same contract read through the live service rather than its source.

    A real work-ledger loop on the conductor, a real binding to the worker, and the
    worker's turn ends: the loop's arm count must not move. ``notify_turn_complete``
    looks for a loop on the slot it is given, and a worker has none, so every branch
    below the lookup is the no-op it always was for a worker.
    """
    import kiro_crew.autonudge as autonudge

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    _bind(status="progress")
    arms: list[float | None] = []

    async def _drive() -> None:
        svc = _service(tmp_path / "an")
        monkeypatch.setattr(autonudge, "get_instance", lambda: svc)
        loop = await svc.add(CONDUCTOR, "patrol the fleet", idle_secs=3600, watch="work-ledger")
        svc._arm_timer = lambda _l, delay=None: arms.append(delay)  # type: ignore[method-assign]
        try:
            svc.notify_turn_complete(WORKER)
            await asyncio.sleep(0)
            assert loop.id not in svc._pushed_ticks, "no window was armed for the conductor"
        finally:
            svc.stop()

    asyncio.run(_drive())
    assert arms == [], "a worker's turn end arms nothing on its conductor's loop"


# ── the probe's new input: worker_closed ─────────────────────────────────────


def test_an_open_worker_move_item_whose_worker_closed_is_stale_at_once():
    """The window separates a thinking worker from a gone one; a close is not ambiguous."""
    item_id = _bind(status="progress")
    item = _item(item_id)
    assert not work_ledger.is_stale(item, worker_running=False, window_secs=3600)
    assert work_ledger.is_stale(item, worker_running=False, worker_closed=True, window_secs=3600)


def test_a_done_item_is_not_woken_by_its_workers_close():
    """The move is the conductor's, so the worker's silence is the expected end."""
    item_id = _bind(status="done")
    item = _item(item_id)
    assert not work_ledger.is_stale(item, worker_running=False, worker_closed=True, window_secs=0)


def test_a_done_item_ruled_fail_is_woken_by_the_close():
    """A failed verdict on a still-open item hands the retry back to the worker."""
    item_id = _bind(status="done", verdict="fail")
    item = _item(item_id)
    assert work_ledger.is_stale(item, worker_running=False, worker_closed=True, window_secs=3600)


def test_an_item_that_never_reported_keeps_the_window_even_when_closed():
    """The bind-to-first-report gap, which is what the window was written for.

    ``worker_closed`` is answered by FAILING to find a session, and for a worker that has
    never reported that absence is as likely to mean "not registered yet" as "gone" -- a
    slot table still rehydrating, a boot tick, a key that table does not carry. This PR
    makes ticks land in those moments far more often (a boot tick, and a pull-forward on
    any sibling worker's write), so without the report requirement the conductor would
    spend a turn on "worker gone" for a worker about to register.
    """
    item_id = _bind()
    item = _item(item_id)
    assert item.last_report_at in (None, "")
    assert not work_ledger.is_stale(
        item, worker_running=False, worker_closed=True, window_secs=3600
    )
    # The window still decides it, exactly as before this input existed.
    assert work_ledger.is_stale(item, worker_running=False, worker_closed=True, window_secs=0)


def test_a_running_worker_is_never_stale_even_closed():
    """``worker_closed`` removes the window, never the rest of the conjunction."""
    item_id = _bind(status="progress")
    item = _item(item_id)
    assert not work_ledger.is_stale(item, worker_running=True, worker_closed=True, window_secs=0)


def test_the_probe_reads_a_closed_worker_through_its_injected_resolver():
    """The probe takes the answer as a value, like liveness, and for the same reason."""
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    probe = WorkLedgerProbe(worker_closed=lambda key: key == WORKER)
    assert probe._worker_closed(WORKER) is True
    assert probe._worker_closed("chat-other") is False
    # And a build with no resolver measures the window exactly as it did before.
    assert WorkLedgerProbe()._worker_closed(WORKER) is False


def test_build_passes_both_resolvers_to_the_work_ledger_probe():
    from kiro_crew import probes

    probe = probes.build(
        probes.WORK_LEDGER, worker_running=lambda _k: True, worker_closed=lambda _k: True
    )
    assert probe is not None
    assert probe._worker_running(WORKER) is True
    assert probe._worker_closed(WORKER) is True


def test_worker_closed_reads_existence_not_liveness():
    """``ledger_wake``'s binding: a slot that answers is open, whatever it is doing."""
    from kiro_crew import ledger_wake

    class _Table:
        def __init__(self, keys):
            self._keys = keys

        def slot_exists(self, key):
            return key in self._keys

    assert ledger_wake.worker_closed(_Table({WORKER}), WORKER) is False
    assert ledger_wake.worker_closed(_Table({f"dashboard_{WORKER}"}), WORKER) is False
    assert ledger_wake.worker_closed(_Table(set()), WORKER) is True
    # An unreadable table cannot PROVE a close, so it reports none.
    assert ledger_wake.worker_closed(None, WORKER) is False
    assert ledger_wake.worker_closed(_Table(set()), "") is False


def test_a_table_that_cannot_answer_existence_reports_no_close():
    """``get_slot`` alone is an acquisition door, not an existence one.

    It hides a slot still being built, so reading its ``None`` as "gone" is the defect
    this function exists to avoid. A table offering only that door cannot prove a close.
    """
    from kiro_crew import ledger_wake

    class _AcquireOnly:
        def get_slot(self, _key):
            return None

    assert ledger_wake.worker_closed(_AcquireOnly(), WORKER) is False


def test_a_reported_worker_under_construction_is_not_closed(tmp_path):
    """A rehydrating or resuming worker is open, though ``get_slot`` hides it.

    Driven against a REAL ``DashboardState``, because the hiding is that class's own
    rule and a stub table would only pin this test against itself. Both shapes the
    construction path takes are covered: the slot registered while marked, and the
    slot retracted from ``_slots`` across the import tail with the mark still set.
    The control is the same key with no mark and no slot, which must read as closed --
    otherwise the two False answers would pass for a function that never says True.
    """
    from chat_test_helpers import _make_state

    from kiro_crew import ledger_wake

    item_id = _bind(status="progress")
    state = _make_state(tmp_path)

    state.get_or_create_slot(WORKER)
    state._slots_under_construction.add(WORKER)
    assert state.get_slot(WORKER) is None, "precondition: get_slot hides the slot"
    closed = ledger_wake.worker_closed(state, WORKER)
    assert closed is False
    # And the gate, fed that answer, keeps the window for an item that already reported.
    assert not work_ledger.is_stale(
        _item(item_id), worker_running=False, worker_closed=closed, window_secs=3600
    )

    state._slots.pop(WORKER, None)
    assert ledger_wake.worker_closed(state, WORKER) is False, "retracted but still building"

    state._slots_under_construction.discard(WORKER)
    assert ledger_wake.worker_closed(state, WORKER) is True, "control: really gone"


def _stall_keys(state) -> "list[str]":
    """The stall observation keys one probe tick over the real ledger produces."""
    from kiro_crew import ledger_wake
    from kiro_crew.probes.work_ledger import WorkLedgerProbe

    probe = WorkLedgerProbe(worker_closed=lambda key: ledger_wake.worker_closed(state, key))
    probe._conductor = CONDUCTOR
    tick = probe.observe(object())
    return [obs.key for obs in tick.observations if obs.key.startswith("stall:")]


def test_a_reported_worker_whose_restore_read_failed_is_not_closed(tmp_path):
    """A boot metadata read failure parks the key with no slot; that is not a close.

    The restore keeps such a key in ``unrestored_slot_keys`` so the next snapshot does
    not erase it, which is the repo's own statement that the session may still exist.
    Reading it as closed would skip the window for an item that already reported and
    persist a stall that never resets. The control drops the key and must stall.
    """
    from chat_test_helpers import _make_state

    from kiro_crew import ledger_wake

    _bind(status="progress")
    state = _make_state(tmp_path)
    state.unrestored_slot_keys = {WORKER}
    assert state.get_slot(WORKER) is None, "precondition: no slot object exists"
    assert ledger_wake.worker_closed(state, WORKER) is False
    assert _stall_keys(state) == [], "no stall observation for an unread key"

    state.unrestored_slot_keys = set()
    assert ledger_wake.worker_closed(state, WORKER) is True, "control: really gone"
    assert len(_stall_keys(state)) == 1, "control: the same item stalls once gone"


def test_absence_during_an_in_flight_restore_proves_no_close(tmp_path):
    """A tab the restore has not reached yet has no slot, and is not closed."""
    from chat_test_helpers import _make_state

    from kiro_crew import ledger_wake

    state = _make_state(tmp_path)
    state.restoring_open_slots = True
    assert ledger_wake.worker_closed(state, WORKER) is False
    state.restoring_open_slots = False
    assert ledger_wake.worker_closed(state, WORKER) is True, "control: really gone"


# ── trigger two, ordering: only a committed close wakes ──────────────────────


def _closing_state(tmp_path):
    from chat_test_helpers import _make_state

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(WORKER)
    slot.append("user", "do the item")
    slot.append("assistant", "doing it")
    slot.drain()
    return state, slot


def _record_close(monkeypatch, *, save_fails: bool) -> list[str]:
    """Record the archival save and the wake in the order ``close_slot`` reaches them."""
    from kiro_crew import autonudge
    from kiro_crew.dashboard import chat_handlers

    events: list[str] = []
    monkeypatch.setattr(autonudge, "_INSTANCE", None)

    async def _save(_state, _slot, *_a, **kw) -> None:
        if kw.get("closed"):
            events.append("save")
            if save_fails:
                raise OSError("disk full")

    async def _wake(_svc, board: str, item_id: str = "") -> str:
        events.append(f"wake:{board}:{item_id}")
        return ""

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)
    monkeypatch.setattr(conductor_wake, "_fire", _wake)
    return events


def test_a_close_whose_archival_fails_fires_no_wake(tmp_path, monkeypatch):
    """The failure arm restores the slot, and nothing is pushed either way.

    It reads the ordering a close owes its own rollback: the save is attempted, the
    restore puts the slot back, and no push names it. A tick fired before the save
    would read the popped slot as closed and persist a ``worker_closed`` stall the
    restore cannot retract.
    """
    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.dashboard.chat_handlers import SlotCloseError

    events = _record_close(monkeypatch, save_fails=True)
    state, slot = _closing_state(tmp_path)

    async def _drive() -> None:
        state.sessions.remove = _noop_remove  # type: ignore[assignment]
        with pytest.raises(SlotCloseError):
            await chat_handlers.close_slot(state, slot, WORKER)

    asyncio.run(_drive())
    assert events == ["save"], "the save was attempted and nothing woke"
    assert state._slots.get(WORKER) is slot, "precondition: the failure arm restored it"


def test_a_committed_close_saves_and_still_fires_no_wake(tmp_path, monkeypatch):
    """The ordinary close: the slot is archived and gone, and no conductor was pushed."""
    from kiro_crew.dashboard import chat_handlers

    events = _record_close(monkeypatch, save_fails=False)
    state, slot = _closing_state(tmp_path)

    async def _drive() -> None:
        state.sessions.remove = _noop_remove  # type: ignore[assignment]
        await chat_handlers.close_slot(state, slot, WORKER)

    asyncio.run(_drive())
    assert events == ["save"]
    assert WORKER not in state._slots


async def _noop_remove(_key) -> None:
    return None
