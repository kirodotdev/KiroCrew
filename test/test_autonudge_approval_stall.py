from __future__ import annotations

import asyncio

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import APPROVAL_STALL_REASON, AutoNudgeService, NudgeLoop


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


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
async def test_stall_stops_loop_before_the_next_cycle(svc, _nosleep):
    """Recorded stall evidence deactivates the loop instead of firing again.

    Same terminal treatment as the other bounds — deactivate (not remove) plus
    ``expired``, so the loop stays inspectable and the operator is told it
    stopped rather than finished.
    """
    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    events: list[tuple[str, str]] = []
    svc.subscribe(lambda ev, lp: events.append((ev, lp.id if lp else "")))
    loop = await _armed(svc)
    svc._on_fire = on_fire

    svc.notify_approval_stalled("chat-1-123")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    assert ("expired", loop.id) in events, f"no expired event emitted; got {events}"
    refreshed = svc._loops[loop.id]
    assert refreshed.active is False
    assert refreshed.stopped_reason == APPROVAL_STALL_REASON
    assert fired == [], "a loop proved unable to act must not burn another cycle"


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
    """A loop also out of cycles reports the bound it historically would have.

    The stall check is evaluated last precisely so it cannot relabel an
    existing terminal outcome.
    """
    loop = await _armed(svc, max_cycles=1)
    loop.cycle_count = loop.max_cycles  # cap reached

    svc.notify_approval_stalled("chat-1-123")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    assert svc._loops[loop.id].stopped_reason == "cycle_cap"


@pytest.mark.asyncio
async def test_runtime_budget_wins_over_stall(svc, _nosleep):
    """Same precedence for the wall-clock budget."""
    loop = await _armed(svc, max_runtime_secs=60)
    loop.created_ts = loop.created_ts - 120  # backdate: budget already spent

    svc.notify_approval_stalled("chat-1-123")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)

    assert svc._loops[loop.id].stopped_reason == "runtime_budget"


@pytest.mark.asyncio
async def test_stall_hook_records_without_stopping(svc, _nosleep):
    """The hook writes evidence and returns; _timer owns the stop.

    Stopping inline would cancel a possibly-mid-fire timer and race the very
    turn that produced the evidence, so the loop must still be active (and its
    timer intact) immediately after the signal.
    """
    loop = await _armed(svc)

    svc.notify_approval_stalled("chat-1-123")

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

    svc.notify_approval_stalled("chat-1-123")

    assert svc._loops[loop.id].approval_stalled is False
    assert svc._loops[loop.id].stopped_reason == "manual", "a manual pause must not be relabelled"


@pytest.mark.asyncio
async def test_a_settings_save_on_an_active_loop_keeps_the_evidence(svc, _nosleep):
    """Only an actual revival spends the evidence, not any ``active=True``.

    The goal popover sends ``active: true`` on every edit of an existing loop, so
    a save landing between the stall and the next wake would otherwise erase
    evidence recorded moments earlier and let one more doomed cycle fire.
    """
    loop = await _armed(svc)
    svc.notify_approval_stalled("chat-1-123")

    # An ordinary settings edit on a loop that is still active.
    await svc.update(loop.id, message="revised", active=True)

    assert svc._loops[loop.id].approval_stalled is True, (
        "a settings save erased the stall evidence; the next cycle would fire "
        "and be declined again"
    )
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc._loops[loop.id].stopped_reason == APPROVAL_STALL_REASON


@pytest.mark.asyncio
async def test_revival_clears_stall_evidence(svc, _nosleep):
    """Resuming spends the evidence, so the resumed loop gets to try again.

    A retained flag would stop the loop on its first wake — before it ever
    tested whether approval is available again.
    """
    loop = await _armed(svc)
    svc.notify_approval_stalled("chat-1-123")
    svc._cancel_timer(loop.id)
    await svc._timer(loop)
    assert svc._loops[loop.id].stopped_reason == APPROVAL_STALL_REASON

    await svc.update(loop.id, active=True)

    refreshed = svc._loops[loop.id]
    assert refreshed.approval_stalled is False
    assert refreshed.stopped_reason == ""


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

    restored = svc2.get_by_slot("chat-1-123")
    assert restored is not None
    assert restored.approval_stalled is True


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
