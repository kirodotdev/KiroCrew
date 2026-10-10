from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import APPROVAL_STALL_REASON, AutoNudgeService, NudgeLoop
from kiro_crew.monitoring.models import MonitorOutcome, MonitorState


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _no_published_service_outlives_the_test():
    """Unpublish the singleton and bound leftover work after every test.

    ``start()`` publishes the service as the module singleton and only ``stop()``
    clears it, so a test that starts one and returns leaves ``get_instance()``
    handing a later test a service bound to a store it is finished with -- and the
    approval-stall hook reaches the service exactly that way, so the residue
    lands on the path under test. That is what this fixture is FOR.

    Cancelling the in-flight persists is housekeeping, not the safety property:
    it stops work nothing is waiting for. It is explicitly NOT what keeps a late
    write from landing in a deleted directory -- ``_write_state`` runs on an
    executor thread, and a thread cannot be cancelled, so no teardown inside the
    test could promise that. ``store_dir`` owns that guarantee by giving the
    store a directory no individual test deletes.

    Deliberately a SYNC fixture: this suite pins pytest-asyncio 0.20.3, whose
    async-fixture wrapper reads a ``fixturedef`` attribute pytest 8.1 removed, so
    an async-generator fixture errors at setup on CI. The repo avoids the
    decorator by convention. The unpublish is in a ``finally`` because ``stop()``
    cancels timer tasks first: if that raises against a loop already torn down,
    the singleton must still be cleared, or one failing teardown poisons every
    later test in the worker.
    """
    yield
    svc = _an.get_instance()
    if svc is None:
        return
    try:
        # Covers both producers: the stall hook's ``_persist_soon`` write and
        # ``update()``'s shielded inner task, which register in the same set.
        inflight = getattr(svc, "_inflight_adds", None)
        if inflight is not None:
            for task in list(inflight):
                task.cancel()
            inflight.clear()
        svc.stop()
    finally:
        _an._INSTANCE = None


@pytest.fixture
def store_dir(tmp_path_factory):
    """A loop-store directory owned by the SESSION, not by one test.

    The persist path is executor-backed: ``_persist_locked`` hands
    ``_write_state`` to a thread, and a thread cannot be cancelled, so no
    teardown running inside the test can guarantee the write is finished. Since
    ``_write_state`` opens with ``mkdir(parents=True, exist_ok=True)``, a write
    that lands late against a per-test ``tmp_path`` re-creates a directory pytest
    already removed.

    Cancelling the task narrows that window but cannot close it, so the fix is
    not a better teardown: it is giving the store a directory no individual test
    deletes. ``tmp_path_factory`` is session-scoped, so each test still gets its
    own isolated store (no cross-test interference) while nothing is removed
    until the session ends and every task is dead. This is the same remedy the
    repo's testing guidance prescribes for a background writer that re-creates
    its directory.
    """
    return tmp_path_factory.mktemp("autonudge-store")


@pytest.fixture
def svc(store_dir):
    return AutoNudgeService(base_dir=store_dir)


@pytest.fixture
def _nosleep(monkeypatch):
    """Collapse the timer's idle wait so _timer runs synchronously."""

    async def _noop(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _noop)


async def _armed(svc, **kwargs) -> NudgeLoop:
    """A started service with one loop whose initial armed timer has drained."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15, **kwargs)
    await svc._timers[loop.id]
    return loop


async def _stall(svc: AutoNudgeService, slot_key: str = "chat-1-123") -> None:
    """Record an unanswered approval and wait for its durable write."""
    svc.notify_approval_stalled(slot_key)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight)


async def _stop_and_drain(svc: AutoNudgeService) -> None:
    """Stop work this test started before pytest closes its event loop."""
    timers = list(svc._timers.values())
    svc.stop()
    if timers:
        await asyncio.gather(*timers, return_exceptions=True)
    inflight = list(svc._inflight_adds)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)


@pytest.mark.asyncio
async def test_starting_publishes_the_service_and_stopping_unpublishes_it(store_dir):
    """The contract the teardown above depends on.

    The stall hook has no service reference of its own -- it reaches the running
    service through ``get_instance()`` -- so what that returns is part of this
    feature's wiring, not an incidental detail.
    """
    svc = AutoNudgeService(base_dir=store_dir)
    await svc.start()
    assert _an.get_instance() is svc

    svc.stop()

    assert _an.get_instance() is None


@pytest.mark.asyncio
async def test_stall_holds_loop_instead_of_firing(svc, _nosleep):
    """Recorded stall evidence PAUSES the loop: it stays active and fires nothing.

    A hold, not a stop -- no ``expired``, no ``stopped_reason``, the cycle count
    untouched -- so a patrol nobody was awake to answer is still there in the
    morning, inspectable as paused for approval.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    loop = await _armed(svc)
    svc._on_fire = on_fire
    cycles = loop.cycle_count

    await _stall(svc)
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    refreshed = svc._loops[loop.id]
    assert refreshed.active is True
    assert refreshed.approval_stalled is True
    assert refreshed.approval_stalled_at > 0
    assert refreshed.stopped_reason == ""
    assert refreshed.cycle_count == cycles, "a held loop must not spend its cycle cap"
    assert fired == [], "a loop proved unable to act must not burn another cycle"
    assert ("expired", loop.id) not in events, "a hold is not a stop"
    assert ("updated", loop.id) in events, "the hold must reach the popover"
    assert events.count(("held", loop.id)) == 1, "the hold notice is sent once"
    timer = svc._timers.get(loop.id)
    assert timer is None or timer.done(), "a held tick must not arm another one"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_loop_without_stall_evidence_fires_normally(svc, _nosleep):
    """The stop is reactive: no recorded stall, no behaviour change.

    This is the false-positive guard. A loop whose cycles only touch
    auto-approved tools never reaches an interactive approval wait, so nothing
    ever records evidence for it and it must keep running even with no grant in
    force.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    await svc.start()
    svc._on_fire = on_fire
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    await svc._timers[loop.id]

    assert len(fired) == 1
    assert "expired" not in events
    assert svc._loops[loop.id].active is True
    assert svc._loops[loop.id].approval_stalled is False


@pytest.mark.asyncio
async def test_cycle_cap_wins_over_stall(svc, _nosleep):
    """A loop also out of cycles reports its cycle-cap bound, not the stall.

    The stall check is evaluated last precisely so it cannot relabel an
    existing terminal outcome.
    """
    loop = await _armed(svc, max_cycles=1)
    loop.cycle_count = loop.max_cycles  # cap reached

    await _stall(svc)
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    assert svc._loops[loop.id].stopped_reason == "cycle_cap"


@pytest.mark.asyncio
async def test_hold_does_not_spend_the_runtime_budget(svc, _nosleep):
    """The wall clock runs while nobody answers, and that time is handed back.

    Checked BEFORE the runtime budget, so a hold longer than the budget does not
    quietly end the loop it exists to keep; on release the held time is added to
    ``created_ts`` (the budget clock), so the loop resumes with what it had left.
    """
    loop = await _armed(svc, max_runtime_secs=60)
    await _stall(svc)
    # Armed 130s ago and held for the last 120s: measured naively, the 60s budget
    # is spent; measured without the hold, 10s of it are.
    now = _an.time.time()
    loop.created_ts = now - 130
    held_since = now - 120
    loop.approval_stalled_at = held_since
    created_before = loop.created_ts
    assert _an.runtime_budget_exceeded(loop)
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    assert svc._loops[loop.id].active is True
    assert svc._loops[loop.id].stopped_reason == ""

    before = _an.time.time()
    assert await svc.release_approval_hold("chat-1-123", why="test", arm=False) is True
    after = _an.time.time()

    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is False
    assert refreshed.approval_stalled_at == 0.0
    shift = refreshed.created_ts - created_before
    assert before - held_since <= shift <= after - held_since
    assert not _an.runtime_budget_exceeded(refreshed)
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_person_typing_releases_the_hold_and_an_app_does_not(svc, _nosleep):
    """A human message proves someone is back; an app's send proves nothing."""
    loop = await _armed(svc)
    await _stall(svc)

    svc.notify_user_input("chat-1-123")  # app / unknown origin
    assert svc._loops[loop.id].approval_stalled is True

    svc.notify_user_input("chat-1-123", human=True)
    await asyncio.gather(*list(svc._inflight_adds))
    assert svc._loops[loop.id].approval_stalled is False
    assert svc._loops[loop.id].active is True
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_release_rearms_and_the_next_cycle_fires(svc, _nosleep):
    """No user re-arm: the release itself arms the loop and the cycle goes out."""
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    loop = await _armed(svc)
    svc._on_fire = on_fire
    await _stall(svc)
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert fired == []

    assert await svc.release_approval_hold("chat-1-123", why="an approval was answered") is True
    await svc._timers[loop.id]

    assert len(fired) == 1, "the released loop did not fire its next cycle"
    assert svc._loops[loop.id].approval_stalled is False
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_release_is_inert_without_a_hold(svc, _nosleep):
    """No hold, no loop, or an inactive loop: nothing changes, nothing is armed."""
    assert await svc.release_approval_hold("chat-nope-000", why="test") is False
    loop = await _armed(svc)
    assert await svc.release_approval_hold("chat-1-123", why="test") is False
    await svc.update(loop.id, active=False)
    assert await svc.release_approval_hold("chat-1-123", why="test") is False
    assert svc._loops[loop.id].active is False
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_reconciler_leaves_a_held_loop_alone(svc, _nosleep):
    """No live timer is the intended state of a hold, not a stranding."""
    loop = await _armed(svc)
    await _stall(svc)
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    svc._reconcile_once()
    svc._reconcile_once()

    timer = svc._timers.get(loop.id)
    assert timer is None or timer.done(), "the reconciler re-armed a held loop"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_release_approval_hold_for_is_best_effort(svc, _nosleep, monkeypatch):
    """The approval paths' one call: inert with no service, and never raises."""
    monkeypatch.setattr(_an, "_INSTANCE", None)
    _an.release_approval_hold_for("chat-1-123", why="test")  # no service: no-op

    loop = await _armed(svc)
    await _stall(svc)
    _an.release_approval_hold_for(None, why="test")  # unbound key: no-op
    assert svc._loops[loop.id].approval_stalled is True
    _an.release_approval_hold_for("chat-1-123", why="test")
    await asyncio.gather(*list(svc._inflight_adds))
    assert svc._loops[loop.id].approval_stalled is False

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(_an._timers, "_schedule_release", _boom)
    _an.release_approval_hold_for("chat-1-123", why="test")  # swallowed
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_stall_hook_records_without_stopping(svc, _nosleep):
    """The hook writes evidence and returns; _timer owns the stop.

    Stopping inline would cancel a possibly-mid-fire timer and race the very
    turn that produced the evidence, so the loop must still be active (and its
    timer intact) immediately after the signal.
    """
    loop = await _armed(svc)

    await _stall(svc)

    assert svc._loops[loop.id].approval_stalled is True
    assert svc._loops[loop.id].active is True
    assert svc._loops[loop.id].stopped_reason == ""


@pytest.mark.asyncio
async def test_stall_hook_ignores_unknown_and_inactive_loops(svc, _nosleep):
    """An unbound slot is a no-op, and a paused loop is not re-tagged.

    The approval path calls this on every unanswered prompt, including in
    sessions that have no loop at all, so it must be inert there.
    """
    svc.notify_approval_stalled("chat-nope-000")  # must not raise

    loop = await _armed(svc)
    await svc.update(loop.id, active=False)
    assert svc._loops[loop.id].stopped_reason == "manual"

    await _stall(svc)

    assert svc._loops[loop.id].approval_stalled is False
    assert svc._loops[loop.id].stopped_reason == "manual", "a manual pause must not be relabelled"


@pytest.mark.asyncio
async def test_a_settings_save_on_an_active_loop_keeps_the_evidence(svc, _nosleep):
    """Only an actual revival spends the evidence, not any ``active=True``.

    A caller may repeat ``active: true`` while revising an existing active loop,
    so a settings edit landing between the stall and the next wake must not erase
    evidence recorded moments earlier and let one more doomed cycle fire.
    """
    loop = await _armed(svc)
    await _stall(svc)

    # An ordinary settings edit on a loop that is still active.
    await svc.update(loop.id, message="revised", active=True)

    assert svc._loops[loop.id].approval_stalled is True, (
        "a settings save erased the stall evidence; the next cycle would fire "
        "and be declined again"
    )
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc._loops[loop.id].approval_stalled is True
    assert svc._loops[loop.id].active is True


@pytest.mark.asyncio
async def test_revival_clears_stall_evidence(svc, _nosleep):
    """A pause-and-resume by hand spends the evidence too.

    A retained flag would hold the resumed loop on its first wake -- before it
    ever tested whether approval is available again.
    """
    loop = await _armed(svc)
    await _stall(svc)
    await svc.update(loop.id, active=False)

    await svc.update(loop.id, active=True)

    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is False
    assert refreshed.stopped_reason == ""
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_stall_evidence_persists_across_restart(store_dir, _nosleep):
    """The hook itself must reach the store, not just the in-memory loop.

    The lapsed grant that caused the stall usually outlives a restart, so losing
    the flag would spend a fresh cycle re-discovering the same stall on every
    gateway start. Awaits the hook's own supervised background write rather than
    forcing a persist, so this fails if the hook stops scheduling one.
    """
    svc1 = AutoNudgeService(base_dir=store_dir)
    await svc1.start()
    loop = await svc1.add(slot_key="chat-1-123", message="go", idle_secs=15)
    await svc1._timers[loop.id]

    svc1.notify_approval_stalled("chat-1-123")
    assert svc1._inflight_adds, "the hook scheduled no persist"
    await asyncio.gather(*list(svc1._inflight_adds))
    svc1.stop()

    svc2 = AutoNudgeService(base_dir=store_dir)
    await svc2.start()
    try:
        restored = svc2.get_by_slot("chat-1-123")
        assert restored is not None
        assert restored.approval_stalled is True
    finally:
        await _stop_and_drain(svc2)


@pytest.mark.asyncio
async def test_stall_reason_is_a_terminal_bound(svc, _nosleep):
    """A user pause that lands first is not overwritten by the stall tag.

    Same terminal-transition atomicity the cap and budget have: both
    transitions serialize on the service lock, and the bound's deactivation
    degrades to a no-op when the loop is already inactive.
    """
    assert APPROVAL_STALL_REASON in _an._TERMINAL_BOUND_REASONS

    loop = await _armed(svc)
    await svc.update(loop.id, active=False)  # user pauses first -> "manual"

    await svc.update(loop.id, active=False, stopped_reason=APPROVAL_STALL_REASON)

    assert svc._loops[loop.id].stopped_reason == "manual"


def test_monitor_inspect_reading_shows_the_hold():
    """``monitor_inspect`` reads this projection; a held loop must say so."""
    from kiro_crew.dashboard.handlers.autonudge import _autonudge_loop_reading

    loop = NudgeLoop(id="abcd1234", slot_key="chat-1-123", message="go")
    assert _autonudge_loop_reading(loop)["paused_for_approval"] is False
    loop.approval_stalled = True
    reading = _autonudge_loop_reading(loop)
    assert reading["paused_for_approval"] is True
    assert reading["active"] is True


@pytest.mark.asyncio
async def test_MUTATION_cancelled_release_waiting_on_lock_still_persists(svc, store_dir):
    class _ObservedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.contended = asyncio.Event()

        async def acquire(self) -> bool:
            if self.locked():
                self.contended.set()
            return await super().acquire()

    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=600)
    await _stall(svc)
    svc._cancel_timer(loop.id)
    lock = _ObservedLock()
    svc._lock = lock
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    caller = asyncio.create_task(svc.release_approval_hold("chat-1-123", why="test", arm=False))
    registered: set[asyncio.Future] = set()
    try:
        await asyncio.wait_for(lock.contended.wait(), timeout=_LOST_RUN_SECS)
        registered = set(svc._inflight_adds)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
        assert loop.approval_stalled is True
    finally:
        if lock.locked():
            lock.release()

    await asyncio.wait_for(
        asyncio.gather(*registered, return_exceptions=True),
        timeout=_LOST_RUN_SECS,
    )
    await _wait_until(lambda: not svc._inflight_adds, "cancelled release settlement")
    assert loop.approval_stalled is False
    assert loop.approval_stalled_at == 0.0
    stored = await _stored(store_dir)
    assert stored[loop.slot_key].approval_stalled is False
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_failed_release_write_keeps_the_hold_and_publishes_nothing(
    svc, _nosleep, monkeypatch
):
    """Persist before publishing: a release the store refused is undone in full."""
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop = await _armed(svc)
    await _stall(svc)
    await asyncio.gather(*list(svc._inflight_adds))
    svc._cancel_timer(loop.id)
    held_at = loop.approval_stalled_at
    created = loop.created_ts
    events.clear()

    async def _refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _refuse)
    with pytest.raises(OSError):
        await svc.release_approval_hold("chat-1-123", why="test")

    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is True
    assert refreshed.approval_stalled_at == held_at
    assert refreshed.created_ts == created
    assert events == [], "a refused release must not be announced"
    timer = svc._timers.get(loop.id)
    assert timer is None or timer.done(), "a refused release must not re-arm"
    await _stop_and_drain(svc)


def _observing_writer(svc, loop_id: str, seen: list):
    """Wrap the snapshot writer: record the live loop and the written row mid-write."""
    real = svc._write_monitor_snapshot_locked

    async def _write(payload=None, *, admission=None):
        live = svc._loops[loop_id]
        row = None
        if payload is not None:
            row = next(r for r in payload["loops"] if r.get("id") == loop_id)
        seen.append(
            (
                (live.approval_stalled, live.approval_stalled_at, live.created_ts),
                None if row is None else (row["approval_stalled"], row["approval_stalled_at"]),
            )
        )
        await real(payload, admission=admission)

    return _write


@pytest.mark.asyncio
async def test_the_hold_reaches_the_live_loop_only_after_its_write(svc, _nosleep, monkeypatch):
    """Readers must not see a hold the store has not accepted yet, even mid-write."""
    loop = await _armed(svc)
    seen: list = []
    monkeypatch.setattr(
        svc, "_write_monitor_snapshot_locked", _observing_writer(svc, loop.id, seen)
    )
    await _stall(svc)

    ((live, row),) = seen
    assert live[0] is False and live[1] == 0.0, "the live loop published the hold before the write"
    assert row is not None and row[0] is True and row[1] > 0, "the write must carry the staged hold"
    assert svc._loops[loop.id].approval_stalled is True
    assert svc._loops[loop.id].approval_stalled_at == row[1]
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_the_release_reaches_the_live_loop_only_after_its_write(svc, _nosleep, monkeypatch):
    """The release side of the same rule: the cleared hold and moved clock wait for the store."""
    loop = await _armed(svc)
    await _stall(svc)
    svc._cancel_timer(loop.id)
    held_at = loop.approval_stalled_at
    created = loop.created_ts
    seen: list = []
    monkeypatch.setattr(
        svc, "_write_monitor_snapshot_locked", _observing_writer(svc, loop.id, seen)
    )

    assert await svc.release_approval_hold("chat-1-123", why="test", arm=False) is True

    ((live, row),) = seen
    assert live == (True, held_at, created), "the live loop published the release before the write"
    assert row == (False, 0.0), "the write must carry the staged release"
    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is False
    assert refreshed.created_ts >= created
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_failed_hold_write_leaves_the_loop_running_and_unannounced(
    svc, _nosleep, monkeypatch
):
    """Persist before publishing, for the hold itself: no hold the store refused."""
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop = await _armed(svc)
    events.clear()

    async def _refuse(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(svc, "_write_monitor_snapshot_locked", _refuse)
    svc.notify_approval_stalled("chat-1-123")
    await asyncio.gather(*list(svc._inflight_adds), return_exceptions=True)

    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is False
    assert refreshed.approval_stalled_at == 0.0
    assert events == [], "a refused hold must not be announced"
    await _stop_and_drain(svc)


@pytest.mark.asyncio
async def test_a_structured_monitor_records_the_stall_but_never_holds(svc, _nosleep, monkeypatch):
    """Its tick never reads the flag, so a hold there would be a false notice."""
    events: list[str] = []
    svc.subscribe(lambda ev, lp: events.append(ev))
    loop = await _armed(svc)
    events.clear()
    monkeypatch.setattr(_an._timers, "is_structured_monitor_loop", lambda _loop: True)

    svc.notify_approval_stalled("chat-1-123")
    await asyncio.gather(*list(svc._inflight_adds))

    # The flag is still recorded: the monitor's action-completion path reads it
    # to settle the monitor as an approval_stall stop.
    assert svc._loops[loop.id].approval_stalled is True
    assert svc._loops[loop.id].approval_stalled_at == 0.0
    assert "held" not in events and "updated" not in events
    await _stop_and_drain(svc)


#: Lost-run ceiling for synchronization the test itself must release. It is a
#: hang guard whose expiry fails at the awaited line, never a performance
#: assertion, and is at most half the repository's ``--timeout=120``.
_LOST_RUN_SECS = 10.0


async def _wait_until(predicate, what: str) -> None:
    """Wait until *predicate* is true, failing by name if the run is wedged."""

    async def _spin() -> None:
        while not predicate():
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_spin(), timeout=_LOST_RUN_SECS)
    except asyncio.TimeoutError:
        pytest.fail(f"{what} did not happen within the lost-run ceiling")


async def _stored(store_dir) -> dict[str, NudgeLoop]:
    """The loops the store holds, read by a fresh service off the event loop."""
    reader = AutoNudgeService(base_dir=store_dir)
    await asyncio.to_thread(reader._load)
    return {loop.slot_key: loop for loop in reader.list_all()}


def _record_arms_after_closure(svc: AutoNudgeService, monkeypatch) -> list[str]:
    """Record every timer coroutine created once shutdown has closed admission.

    Read at creation rather than off the timer table, because ``shutdown()``
    ends with ``stop()``, which empties that table whatever was armed into it.
    """
    real_timer = svc._timer
    armed: list[str] = []

    def _recording_timer(loop, delay=None):
        if not svc._accepting_mutations:
            armed.append(loop.id)
        return real_timer(loop, delay)

    monkeypatch.setattr(svc, "_timer", _recording_timer)
    return armed


@pytest.mark.asyncio
async def test_MUTATION_shutdown_drains_a_hold_registered_before_its_first_step(store_dir):
    """The notifier registers its task before shutdown can take its first snapshot."""
    svc = AutoNudgeService(base_dir=store_dir)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=600)
    controls = [
        task for task in (svc._reconciler, *svc._timers.values()) if isinstance(task, asyncio.Task)
    ]
    svc.stop(preserve_admitted=True)
    await asyncio.wait_for(
        asyncio.gather(*controls, return_exceptions=True),
        timeout=_LOST_RUN_SECS,
    )
    tasks_before = asyncio.all_tasks()

    try:
        svc.notify_approval_stalled("chat-1-123")
        await svc.shutdown()

        assert loop.approval_stalled is True
        assert loop.approval_stalled_at > 0
        stored = await _stored(store_dir)
        assert stored[loop.slot_key].approval_stalled is True
        assert stored[loop.slot_key].approval_stalled_at == loop.approval_stalled_at
    finally:
        unregistered = [
            task
            for task in asyncio.all_tasks() - tasks_before
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in unregistered:
            task.cancel()
        if unregistered:
            await asyncio.wait_for(
                asyncio.gather(*unregistered, return_exceptions=True),
                timeout=_LOST_RUN_SECS,
            )


@pytest.mark.asyncio
async def test_a_hold_admitted_before_shutdown_lands_and_one_after_is_dropped(store_dir, caplog):
    """The hold is evidence about the NEXT wake, so it is admitted where it arrives.

    One recorded before shutdown closes admission is drained to disk, because after
    a shutdown the only next wake is the restart that reads the row. The lock is held
    so that hold parks BEFORE it submits its write, which is the one place admission
    decides whether the write may land. One that arrives after closure has no wake
    left to hold: it schedules nothing, writes nothing, announces nothing, and is
    logged at DEBUG rather than raised into the approval path.
    """
    caplog.set_level(logging.DEBUG, logger="kiro_crew.autonudge")
    svc = AutoNudgeService(base_dir=store_dir)
    await svc.start()
    before = await svc.add(slot_key="chat-1-123", message="go", idle_secs=600)
    after = await svc.add(slot_key="chat-2-456", message="go", idle_secs=600)
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))

    await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        svc.notify_approval_stalled("chat-1-123")
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        assert not shutdown.done(), "shutdown must drain the hold admitted before closure"

        scheduled = set(svc._inflight_adds)
        svc.notify_approval_stalled("chat-2-456")
        assert set(svc._inflight_adds) == scheduled, "a hold after closure schedules nothing"
    finally:
        svc._lock.release()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    rows = await _stored(store_dir)
    assert rows["chat-1-123"].approval_stalled is True, "the admitted hold reached the store"
    assert rows["chat-2-456"].approval_stalled is False
    assert after.approval_stalled is False, "nor did it change the loop in memory"
    assert ("held", before.id) in events
    assert ("held", after.id) not in events and ("updated", after.id) not in events
    assert any(
        r.levelno == logging.DEBUG
        and "not recording the approval hold for chat-2-456" in r.getMessage()
        for r in caplog.records
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]


@pytest.mark.asyncio
async def test_a_release_admitted_before_shutdown_lands_and_one_after_is_dropped(store_dir, caplog):
    """The release takes its lease where the person's action arrives.

    A release scheduled before closure is drained to disk, so a restart does not
    hold a loop a person already answered for. One scheduled after closure is
    dropped at DEBUG and leaves the hold exactly as the store has it.
    """
    caplog.set_level(logging.DEBUG, logger="kiro_crew.autonudge")
    svc = AutoNudgeService(base_dir=store_dir)
    await svc.start()
    released = await svc.add(slot_key="chat-1-123", message="go", idle_secs=600)
    kept = await svc.add(slot_key="chat-2-456", message="go", idle_secs=600)
    await _stall(svc, "chat-1-123")
    await _stall(svc, "chat-2-456")
    created = released.created_ts

    await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        _an.release_approval_hold_for("chat-1-123", why="an approval was answered")
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        assert not shutdown.done(), "shutdown must drain the release admitted before closure"

        scheduled = set(svc._inflight_adds)
        _an.release_approval_hold_for("chat-2-456", why="an approval was answered")
        assert set(svc._inflight_adds) == scheduled, "a release after closure schedules nothing"
    finally:
        svc._lock.release()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    rows = await _stored(store_dir)
    assert rows["chat-1-123"].approval_stalled is False, "the admitted release reached the store"
    assert rows["chat-1-123"].created_ts >= created
    assert rows["chat-2-456"].approval_stalled is True
    assert kept.approval_stalled is True
    assert any(
        r.levelno == logging.DEBUG
        and "not releasing the approval hold for chat-2-456" in r.getMessage()
        for r in caplog.records
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and r.exc_info]


@pytest.mark.asyncio
async def test_a_release_that_lands_after_closure_arms_nothing(store_dir, monkeypatch):
    """A drained release ends the hold durably and stops there.

    ``release_approval_hold_for`` asks for the re-arm, and the release reaches its
    arm step only after shutdown closed admission. Arming then would leave a timer
    behind the teardown, so the arm guard refuses it.
    """
    svc = AutoNudgeService(base_dir=store_dir)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=600)
    await _stall(svc)
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    armed_after_closure = _record_arms_after_closure(svc, monkeypatch)

    await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        _an.release_approval_hold_for("chat-1-123", why="an approval was answered")
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
    finally:
        svc._lock.release()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    assert loop.approval_stalled is False
    assert ("updated", loop.id) in events, "the release landed and was announced"
    assert armed_after_closure == [], "nothing may be armed after admission closed"
    assert loop.id not in svc._rearm_pending


@pytest.mark.asyncio
async def test_a_held_delivered_terminal_settles_once_after_release_without_a_turn(
    store_dir, monkeypatch
):
    """The hold sits ahead of the delivered-marker settlement, and they never mix.

    A loop held while its owed terminal turn is already DELIVERED neither fires nor
    re-probes on the hold tick. The release moves only the hold fields and the
    budget clock (``created_ts``): the delivered marker and its re-probe counter are
    separate monitor fields it never writes. The first tick after the release then
    settles the terminal through the delivered path, with one re-probe and no
    second model turn.
    """
    on_fire = AsyncMock(return_value=True)
    svc = AutoNudgeService(base_dir=store_dir, on_fire=on_fire)
    reprobe = AsyncMock(return_value="holds")
    monkeypatch.setattr(svc, "_terminal_still_holds", reprobe)
    monitor = MonitorState(
        kind="gh-pr",
        target="acme/widgets#42",
        objective="review_ready",
        created_ts=1_000.0,
    )
    monitor.terminal_delivered = "success"
    monitor.terminal_reprobe_unknowns = 1
    loop = NudgeLoop(
        id="held-delivered",
        slot_key="chat-1-123",
        message="watch https://github.com/acme/widgets/pull/42 until green",
        idle_secs=600,
        cycle_count=1,
        created_ts=1_000.0,
        approval_stalled=True,
        approval_stalled_at=1_000.0,
        monitor=monitor,
        gate=True,
    )
    svc._loops[loop.id] = loop

    try:
        await asyncio.wait_for(svc._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
        assert loop.active is True and loop.approval_stalled is True
        reprobe.assert_not_awaited()
        on_fire.assert_not_awaited()
        assert monitor.terminal_delivered == "success"

        assert await svc.release_approval_hold("chat-1-123", why="test", arm=False) is True
        assert loop.approval_stalled is False
        assert loop.created_ts > 1_000.0, "the held time was handed back to the budget"
        assert monitor.terminal_delivered == "success", "the release leaves the marker"
        assert monitor.terminal_reprobe_unknowns == 1, "and its re-probe counter"
        stored = (await _stored(store_dir))["chat-1-123"]
        assert stored.monitor is not None
        assert stored.monitor.terminal_delivered == "success"
        assert stored.monitor.terminal_reprobe_unknowns == 1

        await asyncio.wait_for(svc._timer(loop, delay=0.0), timeout=_LOST_RUN_SECS)
    finally:
        svc.stop()

    reprobe.assert_awaited_once_with(loop, monitor)
    on_fire.assert_not_awaited()
    assert loop.active is False
    assert monitor.outcome is MonitorOutcome.SUCCESS
    assert monitor.terminal_delivered == ""
    assert monitor.terminal_reprobe_unknowns == 0
