"""Route tests: the build loop behind an executing spec.

The served status is reconciled against the live autonudge loop and turn, and
the loop's maintenance transactions (pause, remove, service startup, a peer's
commit) persist durably and serialize against a live removal or reactivation.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading

import pytest

from kiro_crew.apps.builtins.spec_builder.tests.routes_facade import routes

# _never_touch_the_real_state is an autouse fixture: pytest finds it by the name bound here.
from kiro_crew.apps.builtins.spec_builder.tests.routes_helpers import (  # noqa: F401
    _never_touch_the_real_state,
    _redirect_state,
)
from kiro_crew.testing.fixtures import one_pool_thread  # noqa: F401 (fixture)
from kiro_crew.testing.wait import async_wait_until, default_timeout

# ── abort cleanup spares replacements; status is served reconciled ───────────


@pytest.mark.asyncio
async def test_stale_executing_status_settles_back_to_planning(tmp_path, monkeypatch):
    """The reported defect: the nudge loop is CAPPED, and when it runs out of
    cycles the service deactivates it without telling this app — so `executing`
    stayed in the index forever and the UI showed 'building' plus a Pause button
    for a run that had already finished."""
    _redirect_state(monkeypatch, tmp_path)
    spec_dir = tmp_path / "wd" / ".kiro" / "specs" / "done"
    routes._save_index(
        {
            "done": {
                "spec_dir": str(spec_dir),
                "working_dir": str(tmp_path / "wd"),
                "status": "executing",
            }
        }
    )
    meta = routes._load_index()["done"]

    class _Idle:
        running = False

    # No loop at all (the capped loop was removed) -> settles.
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)
    assert await routes._effective_status("done", meta, _Idle()) == "planning"
    assert routes._load_index()["done"]["status"] == "planning", "status not persisted"


@pytest.mark.asyncio
async def test_live_execution_is_not_settled(tmp_path, monkeypatch):
    """Non-vacuous: an execution whose loop is still active must keep reporting
    executing, and a deactivated loop must not."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"live": {"spec_dir": "/s/live", "status": "executing"}})
    meta = routes._load_index()["live"]

    class _Idle:
        running = False

    class _Loop:
        def __init__(self, active):
            self.id = "l1"
            self.active = active

    def _svc(active):
        class _Svc:
            def get_by_slot(self, key):
                return _Loop(active)

        return lambda: _Svc()

    monkeypatch.setattr(routes, "_autonudge_instance", _svc(True))
    assert await routes._effective_status("live", meta, _Idle()) == "executing"
    assert routes._load_index()["live"]["status"] == "executing"

    monkeypatch.setattr(routes, "_autonudge_instance", _svc(False))
    assert await routes._effective_status("live", meta, _Idle()) == "planning"


@pytest.mark.asyncio
async def test_running_turn_keeps_executing(tmp_path, monkeypatch):
    """A single dispatched turn with no loop (the degraded path) still counts as
    executing while the slot is actually running."""
    _redirect_state(monkeypatch, tmp_path)
    routes._save_index({"one": {"spec_dir": "/s/one", "status": "executing"}})
    meta = routes._load_index()["one"]
    monkeypatch.setattr(routes, "_autonudge_instance", lambda: None)

    class _Busy:
        running = True

    assert await routes._effective_status("one", meta, _Busy()) == "executing"


def test_status_is_served_reconciled_not_raw():
    """Source guard: both read endpoints must serve the reconciled status."""

    for handler in (routes._handle_list, routes._handle_get):
        src = inspect.getsource(handler)
        assert "_effective_status(" in src, f"{handler.__name__} serves the raw status"
        assert 'meta.get("status", "planning")' not in src


# ── cancelled persistence cannot be overtaken ────────────────────────────────


@pytest.mark.asyncio
async def test_cancelled_persistence_cannot_be_overtaken(tmp_path):
    """The reported inversion, in my own round-24 change: run_in_executor hands
    back a future whose worker keeps writing after cancellation, so bailing out on
    CancelledError released the service lock mid-write. A later add() could then
    acquire the lock and persist FIRST, leaving the older payload to land last and
    erase the new loop across a restart. The drain keeps the lock until the write
    settles."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    doomed = await svc.add(slot_key="dashboard:a", message="a", idle_secs=60, max_cycles=1)

    order: list[str] = []
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_write = svc._write_state

    def _slow_write(payload):
        order.append("write-start")
        loop.call_soon_threadsafe(started.set)
        # A wedge backstop, not an automatic successful release of the writer.
        if not release.wait(default_timeout()):
            raise TimeoutError("test did not release the persistence worker")
        real_write(payload)
        order.append("write-done")

    svc._write_state = _slow_write  # type: ignore[method-assign]

    remover = asyncio.create_task(svc.remove(doomed.id))
    try:
        # Removal has asynchronous prerequisites before persistence. Cancelling
        # during those is not cancellation of an in-flight write.
        await asyncio.wait_for(started.wait(), 10)
        remover.cancel()
        await asyncio.sleep(0)  # deliver cancellation, without guessing elapsed time

        # The lock must still be held: a competing writer cannot get in yet.
        assert svc._lock.locked(), "service lock released while the write was in flight"
        assert not remover.done(), "cancellation did not wait for the persistence worker"
    finally:
        release.set()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(remover, 10)
        finally:
            svc._cancel_timer(doomed.id)

    assert order == ["write-start", "write-done"], order
    assert remover.cancelled()  # cancellation still propagates after the drain


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_pool_thread")
async def test_nudge_maintenance_pause_is_durable_and_waits_for_a_firing_timer(
    tmp_path,
):
    """Orphan cleanup gets a restart marker before it snapshots the worker slot."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    original_timer = svc._timers.pop(loop.id)
    original_timer.cancel()
    await original_timer

    write_done = threading.Event()
    real_write = svc._write_state

    def _record_write(payload):
        real_write(payload)
        write_done.set()

    svc._write_state = _record_write  # type: ignore[method-assign]
    release = asyncio.Event()
    firing_timer = asyncio.create_task(release.wait())
    svc._timers[loop.id] = firing_timer
    svc._firing.add(loop.id)
    pause = asyncio.create_task(svc.deactivate_and_wait(loop.id))
    await async_wait_until(
        write_done.is_set, describe=lambda: "the paused loop's store write never finished"
    )

    assert loop.active is False
    assert not pause.done()
    reloaded = await an.AutoNudgeService.load_for_maintenance(base_dir=tmp_path)
    persisted = reloaded.get_by_slot(loop.slot_key)
    assert persisted is not None and persisted.active is False

    release.set()
    assert await pause is True
    svc._firing.discard(loop.id)


@pytest.mark.asyncio
@pytest.mark.usefixtures("one_pool_thread")
async def test_nudge_maintenance_pause_waits_for_a_replacement_timer(
    tmp_path,
):
    """A turn-complete rearm during the persisted pause cannot escape cleanup."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    original_timer = svc._timers.pop(loop.id)
    original_timer.cancel()
    await original_timer

    write_done = threading.Event()
    real_write = svc._write_state

    def _record_write(payload):
        real_write(payload)
        write_done.set()

    svc._write_state = _record_write  # type: ignore[method-assign]
    await svc._lock.acquire()
    pause = asyncio.create_task(svc.deactivate_and_wait(loop.id))
    await asyncio.sleep(0)

    release = asyncio.Event()
    replacement_timer = asyncio.create_task(release.wait())
    svc._timers[loop.id] = replacement_timer
    svc._firing.add(loop.id)
    svc._lock.release()
    await async_wait_until(
        write_done.is_set, describe=lambda: "the paused loop's store write never finished"
    )

    assert loop.active is False
    assert not pause.done()
    release.set()
    assert await pause is True
    svc._firing.discard(loop.id)


@pytest.mark.asyncio
async def test_nudge_remove_failure_restores_the_retryable_loop(tmp_path, monkeypatch):
    """A failed durable delete cannot make its retained row invisible in memory."""
    from kiro_crew import autonudge as an

    svc = an.AutoNudgeService(base_dir=tmp_path)
    loop = await svc.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await svc.update(loop.id, active=False)
    events: list[str] = []
    svc.subscribe(lambda event, _loop: events.append(event))
    real_write = svc._write_state

    def _fail_write(_payload):
        raise OSError("store unavailable")

    monkeypatch.setattr(svc, "_write_state", _fail_write)
    with pytest.raises(OSError, match="store unavailable"):
        await svc.remove(loop.id)

    assert svc.get_by_slot(loop.slot_key) is loop
    assert loop in svc.list_all()
    assert "removed" not in events

    monkeypatch.setattr(svc, "_write_state", real_write)
    await svc.remove(loop.id)
    assert svc.get_by_slot(loop.slot_key) is None
    assert events == ["removed"]


@pytest.mark.asyncio
async def test_nudge_maintenance_transaction_serializes_service_startup(tmp_path, monkeypatch):
    """Startup loads only after an offline cleanup commits its authoritative view."""
    from kiro_crew import autonudge as an

    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")
    monkeypatch.setattr(an, "_INSTANCE", None)
    seed = an.AutoNudgeService(base_dir=tmp_path)
    loop = await seed.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await seed.update(loop.id, active=False)

    live = an.AutoNudgeService(base_dir=tmp_path)
    load_started = threading.Event()
    real_load = live._load

    def _record_load():
        load_started.set()
        real_load()

    monkeypatch.setattr(live, "_load", _record_load)
    starter = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as offline:
            starter = asyncio.create_task(live.start())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not load_started.is_set()
            await offline.remove(loop.id)

        await starter
        assert load_started.is_set()
        assert live.list_all() == []
        assert live._timers == {}
    finally:
        live.stop()
        if starter is not None and not starter.done():
            starter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await starter


@pytest.mark.asyncio
async def test_nudge_maintenance_transactions_reload_after_a_peer_commit(tmp_path, monkeypatch):
    """Concurrent offline cleanup cannot write from a pre-transaction snapshot."""
    from kiro_crew import autonudge as an

    monkeypatch.setattr(an, "_INSTANCE", None)
    seed = an.AutoNudgeService(base_dir=tmp_path)
    first_loop = await seed.add(
        slot_key="spec-builder-s-1234abcd",
        message="first",
        idle_secs=60,
        max_cycles=1,
    )
    second_loop = await seed.add(
        slot_key="spec-builder-t-deadbeef",
        message="second",
        idle_secs=60,
        max_cycles=1,
    )
    await seed.update(first_loop.id, active=False)
    await seed.update(second_loop.id, active=False)

    peer_entered = asyncio.Event()

    async def _peer_view() -> set[str]:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as peer:
            peer_entered.set()
            return {loop.id for loop in peer.list_all()}

    peer_task = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as first:
            peer_task = asyncio.create_task(_peer_view())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not peer_entered.is_set()
            await first.remove(first_loop.id)

        assert await peer_task == {second_loop.id}
    finally:
        if peer_task is not None and not peer_task.done():
            peer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await peer_task


@pytest.mark.asyncio
async def test_nudge_maintenance_excludes_a_failing_live_remove(tmp_path, monkeypatch):
    """A rolled-back live removal stays visible to the maintenance scan."""
    from kiro_crew import autonudge as an

    live = an.AutoNudgeService(base_dir=tmp_path)
    loop = await live.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await live.update(loop.id, active=False)
    monkeypatch.setattr(an, "_INSTANCE", live)
    write_started = threading.Event()
    release_write = threading.Event()
    real_write = live._write_state

    def _fail_removal(payload):
        if not payload["loops"]:
            write_started.set()
            assert release_write.wait(
                timeout=default_timeout()
            ), "the test never released the removal write"
            raise OSError("disk unavailable")
        real_write(payload)

    monkeypatch.setattr(live, "_write_state", _fail_removal)
    maintenance_entered = asyncio.Event()

    async def _scan() -> set[str]:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            maintenance_entered.set()
            return {item.id for item in view.list_all()}

    removal = asyncio.create_task(live.remove(loop.id))
    scan = None
    try:
        await async_wait_until(
            write_started.is_set, describe=lambda: "the removal's store write never started"
        )
        scan = asyncio.create_task(_scan())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not maintenance_entered.is_set()
        release_write.set()
        with pytest.raises(OSError, match="disk unavailable"):
            await removal
        assert await scan == {loop.id}
    finally:
        release_write.set()
        live.stop()
        for task in (removal, scan):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_nudge_maintenance_excludes_a_live_reactivation(tmp_path, monkeypatch):
    """An external update cannot revive a row during administrative removal."""
    from kiro_crew import autonudge as an

    live = an.AutoNudgeService(base_dir=tmp_path)
    loop = await live.add(
        slot_key="spec-builder-s-1234abcd",
        message="continue",
        idle_secs=60,
        max_cycles=1,
    )
    await live.update(loop.id, active=False)
    monkeypatch.setattr(an, "_INSTANCE", live)
    updater = None
    try:
        async with an.AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            updater = asyncio.create_task(live.update(loop.id, active=True))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not updater.done()
            await view.remove(loop.id)

        assert await updater is None
        assert live.list_all() == []
        assert live._timers == {}
    finally:
        live.stop()
        if updater is not None and not updater.done():
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater
