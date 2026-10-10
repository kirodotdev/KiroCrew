"""The crew-log wake: a worker's write, close or turn end pulls its conductor forward.

``rfc-crew-log-wake``. One lookup (``conductor_wake``) and three triggers that share it.
What is pinned here is the WIRING -- that each trigger resolves the binding, finds the
conductor's armed work-ledger loop and calls ``fire_now`` on it, and that none of them
fires for a slot with no binding. What the conductor then DOES with the tick is Phase 3's
gate and is pinned in ``test_probe_work_ledger.py``; the one piece of that this file does
own is the ``worker_closed`` probe input, because it is this change's addition to the
staleness conjunction.

No gateway runs in any of these. The service is a stub with the three attributes the
lookup reads, which is what makes a wiring defect here impossible to mistake for a
service defect.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import conductor_wake, work_ledger

CONDUCTOR = "chat-conductor"
WORKER = "chat-worker"
LOOP_ID = "loop-1"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own data home per test, so no binding file outlives its own test; and an empty
    registry, so no board a previous test observed answers this one's lookup."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    conductor_wake.reset_for_tests()
    yield
    conductor_wake.reset_for_tests()


# ── stubs ────────────────────────────────────────────────────────────────────


class _Monitor:
    def __init__(self, kind: str = "work-ledger") -> None:
        self.kind = kind
        self.target = CONDUCTOR
        # The REAL current version, read rather than written: both zero-delay arms refuse a
        # record this gateway cannot interpret, so a stub pinning a literal here would stop
        # exercising that refusal the moment the version moved.
        from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

        self.version = MONITOR_STATE_VERSION


class _Loop:
    def __init__(self, *, kind: str = "work-ledger", active: bool = True) -> None:
        self.id = LOOP_ID
        self.slot_key = CONDUCTOR
        self.active = active
        self.monitor = _Monitor(kind) if kind else None


class _Svc:
    """The three things the lookup reads off a service, and nothing else.

    ``_reconciler`` is how the thread entry point finds the event loop to schedule onto;
    the real service holds the same attribute for the same task.
    """

    def __init__(self, loop: "_Loop | None" = None, *, refuse: str = "") -> None:
        self._loop = loop
        self._refuse = refuse
        self.fired: list[str] = []
        self.deferred: list[str] = []
        self.delays: list[float] = []
        self._reconciler = None
        self._timers: dict[str, object] = {}

    def get_by_slot(self, slot_key: str) -> "_Loop | None":
        return self._loop if self._loop is not None and slot_key == CONDUCTOR else None

    async def fire_now(self, loop_id, *, defer_if_firing=False, delay=0.0):
        self.delays.append(delay)
        if self._refuse:
            if defer_if_firing:
                self.deferred.append(loop_id)
            return None, self._refuse, 409
        self.fired.append(loop_id)
        return self._loop, "", 200


def _bind(worker: str = WORKER, *, status: str | None = None, verdict: str = "") -> str:
    """A real conductor ledger with one item bound to *worker*. Returns the item id.

    A real store rather than a fake one: the loop-side lookup reads the binding this
    writes, and the probe tests below read the item back through it.
    """
    work_ledger.ensure_conductor(CONDUCTOR, goal="drive the fleet")
    created = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="t", acceptance={"kind": "human_approval"}
    )
    item_id = str(created["item"].item_id)
    work_ledger.apply_conductor_action(
        CONDUCTOR, "bind", item_id=item_id, worker_session_key=worker
    )
    if status is not None:
        work_ledger.apply_worker_report(CONDUCTOR, item_id, status=status, summary="s")
    if verdict:
        work_ledger.apply_conductor_action(
            CONDUCTOR, "verdict", item_id=item_id, verdict=verdict, fails=1
        )
    return item_id


def _item(item_id: str):
    found = work_ledger.read_work_item(CONDUCTOR, item_id)
    assert found is not None
    return found


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


async def _fire(svc, worker_slot: str) -> str:
    """Drive ``fire_for_worker_slot`` with *svc* standing in for the live service."""
    import kiro_crew.autonudge as autonudge

    original = autonudge.get_instance
    autonudge.get_instance = lambda: svc  # type: ignore[assignment]
    try:
        return await conductor_wake.fire_for_worker_slot(worker_slot)
    finally:
        autonudge.get_instance = original  # type: ignore[assignment]


# ── trigger one: the ``work`` fold advancing on the crew-log bus ──────────────


def _event(board: str, items: list, *, scope: str = "slot", fold: str = "work"):
    """A ``FoldAdvanced`` as ``eager._publish_fold`` builds one for *board*'s work fold."""
    from kiro_crew.crew_log import bus

    value = {
        "conductor": {"slot": board},
        "items": [
            {
                "item_id": item_id,
                "last_report_at": stamp,
                "state": "open",
            }
            for item_id, stamp in items
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
    first = [("it_a", "t1"), ("it_b", "t1")]
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, first))
    fired.clear()
    moved = [("it_a", "t2"), ("it_b", "t1")]
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, moved))
    assert fired == [(CONDUCTOR, "it_a")]


def test_a_conductors_own_write_moves_no_stamp_and_pushes_nothing(monkeypatch):
    """``create``, ``bind``, ``decide`` and ``close`` advance the fold too, and none of
    them is a worker's news. With every stamp unchanged the subscriber does nothing --
    which is also what keeps a NESTED conductor's bookkeeping on its own board from
    pulling its parent forward: the parent's board did not move."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    items = [("it_a", "t1")]
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, items))
    fired.clear()
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, items + [("it_new", "")]))
    assert fired == []


def test_the_first_board_seen_records_and_pushes_nothing(monkeypatch):
    """MUTATION-SENSITIVE. The first sight is the join's baseline: the board as it stood,
    which says nothing new. At boot ``start`` already armed the loop at delay zero to read
    it, and a push into that tick would buy a second cycle at its tail. The next report
    is diffed against what was recorded."""
    fired = _record_fires(monkeypatch)
    svc = _Svc(_Loop())
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, [("it_a", "t1"), ("it_b", "")]))
    assert fired == []
    assert conductor_wake._registry.boards[CONDUCTOR] == {"it_a": "t1", "it_b": ""}
    asyncio.run(conductor_wake._observe_board(svc, CONDUCTOR, [("it_a", "t1"), ("it_b", "t2")]))
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
    items = [("it_a", "t1")]
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
    asyncio.run(conductor_wake._observe_board(svc, "chat-c1", [("it_a", "t1")]))
    asyncio.run(conductor_wake._observe_board(svc, "chat-c2", [("it_b", "t1")]))
    assert set(conductor_wake._registry.boards) == {"chat-c1", "chat-c2"}
    live["chat-c1"] = False
    asyncio.run(conductor_wake._observe_board(svc, "chat-c2", [("it_b", "t2")]))
    assert set(conductor_wake._registry.boards) == {"chat-c2"}
    # A loop-table change sweeps too, through the subscription sync.
    live["chat-c2"] = False
    monkeypatch.setattr(svc, "list_all", lambda: [], raising=False)

    async def _sync() -> None:
        conductor_wake.sync_subscriptions(svc)

    asyncio.run(_sync())
    assert conductor_wake._registry.boards == {}


def test_the_service_stop_clears_the_registry():
    conductor_wake._registry.observe(CONDUCTOR, [("it_a", "t1")])
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
        first.result(timeout=5)
        assert set(conductor_wake._registry.boards[CONDUCTOR]) == {"it_a"}
        moved = conductor_wake._observe_event(_event(CONDUCTOR, [("it_a", "t2")]))
        assert moved is not None
        moved.result(timeout=5)
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


def _report(unit: str, board: str, worker: str, stamp: str) -> None:
    _write(
        unit,
        slot=board,
        actor="worker",
        by=worker,
        action="report",
        item_id="it_p",
        status="done",
        summary="s",
        last_report_at=stamp,
    )


async def _until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


async def _sync_and_join(svc) -> None:
    """Sync the subscriptions, then wait for every join it started to land."""
    conductor_wake.sync_subscriptions(svc)
    await asyncio.gather(*list(conductor_wake._subscriptions.joining.values()))


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


# ── trigger two: a worker session closes ─────────────────────────────────────


def test_the_close_path_pushes_through_the_shared_lookup():
    """``close_slot``'s hook, driven directly: the close is already covered elsewhere.

    What is pinned is that the hook reaches the shared lookup with the closing slot's own
    name, and that it swallows a failure -- a close has rollback paths for its own four
    failure modes, and "the conductor heard late" is not one of them.
    """
    from kiro_crew.dashboard import chat_handlers

    seen: list[str] = []

    async def _drive(resolver) -> None:
        original = conductor_wake.fire_for_worker_slot
        conductor_wake.fire_for_worker_slot = resolver  # type: ignore[assignment]
        try:
            await chat_handlers._wake_conductor_for_closed_worker(WORKER)
        finally:
            conductor_wake.fire_for_worker_slot = original  # type: ignore[assignment]

    async def _record(slot_key: str) -> str:
        seen.append(slot_key)
        return LOOP_ID

    asyncio.run(_drive(_record))
    assert seen == [WORKER]

    async def _boom(_slot_key: str) -> str:
        raise RuntimeError("ledger store on fire")

    # Does not propagate: the close must finish.
    asyncio.run(_drive(_boom))


# ── trigger three: a worker turn ends ────────────────────────────────────────


def test_a_turn_end_from_a_bound_worker_schedules_a_push():
    """Outcome-blind, and that is what this trigger adds.

    A turn that raised, or ended without reporting, writes no ``work/recorded`` entry at
    all, so trigger one never sees it. This hook is called for every turn end.
    """
    from kiro_crew.autonudge_service import timers

    scheduled: list[str] = []

    class _Service:
        _inflight_adds: set = set()

    async def _drive() -> None:
        original = conductor_wake.fire_for_worker_slot

        async def _record(slot_key: str) -> str:
            scheduled.append(slot_key)
            return LOOP_ID

        conductor_wake.fire_for_worker_slot = _record  # type: ignore[assignment]
        try:
            timers._wake_bound_conductor(_Service(), WORKER)
            await asyncio.sleep(0)
        finally:
            conductor_wake.fire_for_worker_slot = original  # type: ignore[assignment]

    asyncio.run(_drive())
    assert scheduled == [WORKER]


def test_a_turn_end_with_no_running_event_loop_is_a_no_op():
    """A synchronous driver or a shutdown path has nothing to schedule onto."""
    from kiro_crew.autonudge_service import timers

    class _Service:
        _inflight_adds: set = set()

    timers._wake_bound_conductor(_Service(), WORKER)


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

    async def _save(_state, _slot, *_a, **kw) -> bool:
        if kw.get("closed"):
            events.append("save")
            if save_fails:
                raise OSError("disk full")
        # True: this double models the durable write itself; a bare None reads
        # as the save's own refusal, which the close answers rather than ignores.
        return True

    async def _wake(name: str) -> None:
        events.append(f"wake:{name}")

    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)
    monkeypatch.setattr(chat_handlers, "_wake_conductor_for_closed_worker", _wake)
    return events


def test_a_close_whose_archival_fails_fires_no_wake(tmp_path, monkeypatch):
    """The failure arm restores the slot, so the conductor must never have been told.

    A tick fired before the save would read the popped slot as closed and persist a
    ``worker_closed`` stall that the restore cannot retract.
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


def test_a_committed_close_fires_exactly_one_wake_after_the_save(tmp_path, monkeypatch):
    from kiro_crew.dashboard import chat_handlers

    events = _record_close(monkeypatch, save_fails=False)
    state, slot = _closing_state(tmp_path)

    async def _drive() -> None:
        state.sessions.remove = _noop_remove  # type: ignore[assignment]
        await chat_handlers.close_slot(state, slot, WORKER)

    asyncio.run(_drive())
    assert events == ["save", f"wake:{WORKER}"]
    assert WORKER not in state._slots


async def _noop_remove(_key) -> None:
    return None


# ── a push the service cannot take now is not lost ───────────────────────────


def _service(base_dir, **kwargs):
    from kiro_crew.autonudge import AutoNudgeService

    return AutoNudgeService(base_dir=base_dir, **kwargs)


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
            pushed.append(await conductor_wake.fire_for_worker_slot(WORKER))
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
            await asyncio.wait_for(asyncio.shield(svc._timers[loop_id]), timeout=10)
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
            deadline = time.time() + 100
            loop.next_due_ts = deadline
            svc._pushed_ticks.add(loop.id)
            await svc._timer(loop, delay=0.0)
            kept = loop.next_due_ts

            loop.next_due_ts = deadline
            before = time.time()
            await svc._timer(loop, delay=0.0)
            return deadline, kept, loop.next_due_ts - before
        finally:
            svc.stop()

    deadline, kept, control_gap = asyncio.run(_drive())
    assert kept == deadline
    assert control_gap >= 3600 - 1, "control: an unpushed quiet tick re-arms a full interval"


WORKER_B = "chat-worker-b"
WORKER_C = "chat-worker-c"


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
    await conductor_wake.fire_for_worker_slot(worker)
    for _ in range(5):
        task = svc._timers.get(loop_id)
        if task is None or task.done():
            return
        if loop_id not in svc._pushed_ticks and loop_id not in svc._pushed_running:
            # Only an armed deadline tick is left, hours away: nothing more to settle.
            return
        await asyncio.wait_for(asyncio.shield(task), timeout=10)


def test_a_close_after_a_done_wake_buys_no_second_turn(tmp_path, monkeypatch):
    """The done report wakes once; the close lands on the follow-up window and does not.

    The follow-up allowance is the loop's own second turn and survives for its next
    scheduled tick. A fresh ``question`` on another item in the same window is news, and
    buys exactly one turn.
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
            assert loop.monitor.followup_ticks == 1, "precondition: the wake left its follow-up"

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
    assert followups == 1, "the scheduled cadence still owns the free follow-up"


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
                    await conductor_wake.fire_for_worker_slot(worker)

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
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
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
                fired.append(await conductor_wake.fire_for_worker_slot(WORKER))
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
                await conductor_wake.fire_for_worker_slot(worker)
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
            await conductor_wake.fire_for_worker_slot(WORKER)
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
            await conductor_wake.fire_for_worker_slot(WORKER)
            await svc._timer(loop, delay=0.0)
            assert loop.id not in svc._pushed_ticks, "the tick that ran closed its window"
            work_ledger.apply_worker_report(CONDUCTOR, late, status="question", summary="s")
            await conductor_wake.fire_for_worker_slot(WORKER_B)
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
                await conductor_wake.fire_for_worker_slot(WORKER)
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
                await conductor_wake.fire_for_worker_slot(worker)
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
            await conductor_wake.fire_for_worker_slot(WORKER)
            assert conductor_wake._window_open(svc, loop.id), "precondition: the window opened"

            # What a user message does: the task is cancelled and popped, the mark stays.
            svc._cancel_timer(loop.id)
            marked = loop.id in svc._pushed_ticks
            still_open = conductor_wake._window_open(svc, loop.id)

            work_ledger.apply_worker_report(
                CONDUCTOR, second, status="question", summary="RULING: a -- b -- a"
            )
            await conductor_wake.fire_for_worker_slot(WORKER_B)
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
