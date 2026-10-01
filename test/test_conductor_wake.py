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
    """Own data home per test, so no binding file outlives its own test."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))


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
        self._reconciler = None
        self._timers: dict[str, object] = {}

    def get_by_slot(self, slot_key: str) -> "_Loop | None":
        return self._loop if self._loop is not None and slot_key == CONDUCTOR else None

    async def fire_now(self, loop_id, *, defer_if_firing=False):
        if self._refuse:
            if defer_if_firing:
                self.deferred.append(loop_id)
            return None, self._refuse, 409
        self.fired.append(loop_id)
        return self._loop, "", 200


def _bind(worker: str = WORKER, *, status: str | None = None, verdict: str = "") -> str:
    """A real conductor ledger with one item bound to *worker*. Returns the item id.

    A real store rather than a fake one, because the binding file IS what the lookup
    reads: a stubbed resolver here would pin this test against itself.
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
    """The whole no-op case, and the reason the binding is the gate rather than the board.

    A conductor's OWN ``work/recorded`` entries take this path too; a conductor slot has
    no binding as a worker, so it resolves nothing without this module knowing what a
    conductor is.
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


# ── trigger one: the crew-log eager drain ────────────────────────────────────


def test_a_work_recorded_entry_from_a_bound_worker_pushes_on_the_drain(monkeypatch):
    """The second consumer on the drain, and it keys on the WRITER's slot.

    Driven through ``_push_conductor_wakes`` with a stubbed slot resolver rather than
    through a live crew log: what this pins is which wakes the consumer selects and whose
    slot it resolves them to, and a real log would make a selection defect look like a
    projection defect.
    """
    from kiro_crew.crew_log import eager

    pushed: list[str] = []
    monkeypatch.setattr(eager, "_slot_of", lambda unit_id: f"chat-{unit_id}")
    monkeypatch.setattr(
        conductor_wake, "fire_for_worker_slot_from_thread", lambda slot: pushed.append(slot) or True
    )
    batch = {
        ("u1", CONDUCTOR): eager._Wake("u1", "work/recorded", 4, CONDUCTOR),
        ("u2", ""): eager._Wake("u2", "panel/published", 2, ""),
        ("u3", ""): eager._Wake("u3", "session/closed", 7, ""),
    }
    eager._push_conductor_wakes(batch)
    assert pushed == ["chat-u1"]


def test_one_workers_several_entries_in_a_batch_push_once(monkeypatch):
    """The push is a deadline move, so two of them for one batch arm one tick twice."""
    from kiro_crew.crew_log import eager

    pushed: list[str] = []
    monkeypatch.setattr(eager, "_slot_of", lambda unit_id: WORKER)
    monkeypatch.setattr(
        conductor_wake, "fire_for_worker_slot_from_thread", lambda slot: pushed.append(slot) or True
    )
    batch = {
        ("u1", CONDUCTOR): eager._Wake("u1", "work/recorded", 4, CONDUCTOR),
        ("u1", "chat-other"): eager._Wake("u1", "work/recorded", 5, "chat-other"),
    }
    eager._push_conductor_wakes(batch)
    assert pushed == [WORKER]


def test_a_raising_push_neither_blocks_the_drain_nor_skips_the_fold(monkeypatch):
    """Driven through the REAL worker thread, because the guard is in the loop body.

    Two independent consumers share one batch and one ``finally`` that settles the
    counters. So a raising wake must not take the fold with it, and must not strand a
    waiter on ``drain`` -- which is what asserting ``drain`` returns ``True`` proves,
    since it waits for settled to reach queued rather than for the queue to empty.

    The control is the fold call: if it never ran, this test would pass for the wrong
    reason (nothing was consumed at all).
    """
    from kiro_crew.crew_log import eager

    folded: list[int] = []

    def _boom(_slot):
        raise RuntimeError("binding store on fire")

    monkeypatch.setattr(eager, "_fold_batch", lambda batch, closers=None: folded.append(len(batch)))
    monkeypatch.setattr(conductor_wake, "fire_for_worker_slot_from_thread", _boom)
    monkeypatch.setattr(eager, "_slot_of", lambda unit_id: WORKER)
    eager.resume_for_tests()
    try:
        eager.note_commit("u1", "work/recorded", 4, CONDUCTOR)
        assert eager.drain(timeout=10.0) is True
    finally:
        eager.stop_for_tests()
    assert folded == [1], "the fold consumer must still have run"


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

    async def _save(_state, _slot, *_a, **kw) -> None:
        if kw.get("closed"):
            events.append("save")
            if save_fails:
                raise OSError("disk full")

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


def _ledger_service(tmp_path, monkeypatch, *, closed: "set[str]"):
    """A real service whose real gate reads the real ledger, counting delivered turns."""
    import kiro_crew.autonudge as autonudge

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
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
    assert arms == [0.0] * cap
    assert active is True, "the loop and its scheduled tick stay armed"
    assert _item(item_id).summary == str(cap), "the thirteenth report still landed"
    tripped = [r for r in caplog.records if "pull-forwards of loop" in r.getMessage()]
    assert len(tripped) == 1 and tripped[0].levelno == logging.INFO
