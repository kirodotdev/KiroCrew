"""Regression: async remove() must offload persistence, not fsync on the loop (#425).

``remove()`` called ``remove_sync(persist=True)`` -> ``_save()`` -> ``_write_state``
which does a blocking ``os.fsync`` directly on the event loop. It must instead
snapshot under the lock and offload the write to an executor (as update() does).
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeAdmissionRefused
from kiro_crew.monitoring.models import MonitorBudgets


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


@pytest.mark.asyncio
async def test_remove_offloads_persist_without_blocking_save(svc, monkeypatch):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)

    save_called = {"v": False}
    monkeypatch.setattr(svc, "_save", lambda: save_called.__setitem__("v", True))

    writes: list[dict] = []
    orig_write = svc._write_state

    def _record_write(payload: dict) -> None:
        writes.append(payload)
        orig_write(payload)

    monkeypatch.setattr(svc, "_write_state", _record_write)

    await svc.remove(loop.id)

    assert loop.id not in svc._loops
    assert save_called["v"] is False, "blocking _save() must not run on the event loop"
    assert writes, "removal must still be persisted (via the offloaded write)"


@pytest.mark.asyncio
async def test_remove_missing_loop_is_noop(svc, monkeypatch):
    await svc.start()
    writes: list[dict] = []
    monkeypatch.setattr(svc, "_write_state", lambda payload: writes.append(payload))
    await svc.remove("does-not-exist")
    assert writes == []


@pytest.mark.asyncio
async def test_failed_remove_can_retry_after_in_memory_removal(svc, monkeypatch, tmp_path):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
    original_write = svc._write_state
    failed = False

    def _fail_once(payload: dict) -> None:
        nonlocal failed
        if not payload["loops"] and not failed:
            failed = True
            raise OSError("store unavailable")
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", _fail_once)

    with pytest.raises(OSError, match="store unavailable"):
        await svc.remove(loop.id)
    assert svc._loops[loop.id] is loop

    await svc.remove(loop.id)
    svc.stop()

    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("chat-1-123") is None
    restored.stop()


@pytest.mark.asyncio
async def test_failed_update_restores_the_durable_live_view(svc, monkeypatch):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="original", idle_secs=15)

    def _fail_write(_payload: dict) -> None:
        raise OSError("store unavailable")

    monkeypatch.setattr(svc, "_write_state", _fail_write)
    with pytest.raises(OSError, match="store unavailable"):
        await svc.update(loop.id, message="replacement", active=False)

    assert loop.message == "original"
    assert loop.active is True
    stored = json.loads(svc._path.read_text())
    assert stored["loops"][0]["message"] == "original"
    assert stored["loops"][0]["active"] is True
    svc.stop()


@pytest.mark.asyncio
async def test_failed_add_restores_the_replaced_live_loop(svc, monkeypatch):
    await svc.start()
    original = await svc.add(slot_key="chat-1-123", message="original", idle_secs=15)

    def _fail_write(_payload: dict) -> None:
        raise OSError("store unavailable")

    monkeypatch.setattr(svc, "_write_state", _fail_write)
    with pytest.raises(OSError, match="store unavailable"):
        await svc.add(slot_key="chat-1-123", message="replacement", idle_secs=15)

    assert svc.get_by_slot("chat-1-123") is original
    assert original.id in svc._timers
    stored = json.loads(svc._path.read_text())
    assert [item["id"] for item in stored["loops"]] == [original.id]
    svc.stop()


@pytest.mark.asyncio
async def test_second_cancellation_cannot_release_a_live_removal_write(svc, monkeypatch, tmp_path):
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="old", idle_secs=15)
    original_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()

    def _block_removal(payload: dict) -> None:
        if not payload["loops"]:
            write_started.set()
            release_write.wait(timeout=5)
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", _block_removal)
    removal = asyncio.create_task(svc.remove(loop.id))
    addition = None
    try:
        assert await asyncio.to_thread(write_started.wait, 2)
        removal.cancel()
        await asyncio.sleep(0)
        removal.cancel()
        await asyncio.sleep(0)
        assert not removal.done()

        addition = asyncio.create_task(
            svc.add(slot_key="chat-new-123", message="new", idle_secs=15)
        )
        await asyncio.sleep(0)
        assert not addition.done()
        release_write.set()
        with pytest.raises(asyncio.CancelledError):
            await removal
        added = await addition

        stored = json.loads((tmp_path / "autonudge.json").read_text())
        assert [item["id"] for item in stored["loops"]] == [added.id]
    finally:
        release_write.set()
        svc.stop()
        for task in (removal, addition):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_add_revalidates_its_session_inside_the_transaction(svc):
    await svc.start()
    slot = object()
    slots = {"chat-1-123": slot}
    addition = None
    async with AutoNudgeService.maintenance_service(base_dir=svc._base_dir):
        addition = asyncio.create_task(
            svc.add(
                slot_key="chat-1-123",
                message="new",
                idle_secs=15,
                admission_check=lambda: slots.get("chat-1-123") is slot,
            )
        )
        await asyncio.sleep(0)
        assert not addition.done()
        slots.clear()

    with pytest.raises(NudgeAdmissionRefused):
        await addition
    assert svc.get_by_slot("chat-1-123") is None
    svc.stop()


@pytest.mark.asyncio
async def test_queued_add_revalidates_after_waiting_for_the_service_lock(svc):
    from kiro_crew import autonudge as an

    await svc.start()
    slot = object()
    slots = {"chat-1-123": slot}
    await svc._lock.acquire()
    addition = asyncio.create_task(
        svc.add(
            slot_key="chat-1-123",
            message="new",
            idle_secs=15,
            admission_check=lambda: slots.get("chat-1-123") is slot,
        )
    )
    try:
        maintenance_lock = an._maintenance_lock(svc._base_dir)
        for _ in range(10):
            if maintenance_lock.locked():
                break
            await asyncio.sleep(0)
        assert maintenance_lock.locked()
        assert svc.get_by_slot("chat-1-123") is None

        slots.clear()
        svc._lock.release()
        with pytest.raises(NudgeAdmissionRefused):
            await addition
        assert svc.get_by_slot("chat-1-123") is None
    finally:
        if svc._lock.locked():
            svc._lock.release()
        if not addition.done():
            addition.cancel()
            await asyncio.gather(addition, return_exceptions=True)
        svc.stop()


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_leak_the_maintenance_lock(svc, monkeypatch):
    from kiro_crew import autonudge as an

    wait_returned = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    never = asyncio.Future()

    class SlowCancellationEvent:
        async def wait(self):
            try:
                await never
            finally:
                cleanup_started.set()
                await release_cleanup.wait()

    loop_id = "loop-under-cancellation"
    svc._maintenance_quiesce_events[loop_id] = SlowCancellationEvent()
    original_wait = asyncio.wait

    async def _pause_after_lock_acquisition(*args, **kwargs):
        result = await original_wait(*args, **kwargs)
        wait_returned.set()
        await asyncio.Future()
        return result

    monkeypatch.setattr(an.asyncio, "wait", _pause_after_lock_acquisition)
    maintenance_lock = an._maintenance_lock(svc._base_dir)
    await maintenance_lock.acquire()
    acquisition = asyncio.create_task(svc._acquire_mutation_lock(loop_id))
    try:
        await asyncio.sleep(0)
        maintenance_lock.release()
        await asyncio.wait_for(wait_returned.wait(), 1)
        assert maintenance_lock.locked()

        acquisition.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        acquisition.cancel()
        await asyncio.sleep(0)
        assert not acquisition.done()

        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await acquisition
        assert not maintenance_lock.locked()
    finally:
        release_cleanup.set()
        if not acquisition.done():
            acquisition.cancel()
            try:
                await acquisition
            except asyncio.CancelledError:
                pass
        if maintenance_lock.locked():
            maintenance_lock.release()


@pytest.mark.asyncio
async def test_maintenance_quiesce_wakes_a_firing_remove_waiter(tmp_path, monkeypatch):
    from kiro_crew import autonudge as an

    entered_fire = asyncio.Event()
    release_remove = asyncio.Event()
    svc = None

    async def _fire(loop):
        entered_fire.set()
        await release_remove.wait()
        assert svc is not None
        await svc.remove(loop.id)
        return False

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=_fire)
    loop = await svc.add(slot_key="chat-1-123", message="old", idle_secs=15)
    original_timer = svc._timers.pop(loop.id)
    original_timer.cancel()
    await original_timer
    timer = asyncio.create_task(svc._timer(loop, delay=0))
    svc._timers[loop.id] = timer
    monkeypatch.setattr(an, "_INSTANCE", svc)
    await asyncio.wait_for(entered_fire.wait(), 1)

    try:
        async with AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            release_remove.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not timer.done()
            assert await asyncio.wait_for(view.deactivate_and_wait(loop.id), 1)
            assert timer.done()
            assert loop.active is False
            await view.remove(loop.id)
    finally:
        release_remove.set()
        svc.stop()
        if not timer.done():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)


@pytest.mark.asyncio
async def test_offline_maintenance_does_not_deactivate_an_active_monitor(tmp_path):
    writer = AutoNudgeService(base_dir=tmp_path)
    runtime = None
    try:
        legacy = await writer.add(slot_key="chat-legacy", message="old", idle_secs=15)
        monitor = await writer.add_monitor(
            slot_key="chat-monitor",
            kind="github_pull_request",
            target="https://github.com/acme/widgets/pull/7",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(max_runtime_secs=600),
            now=100.0,
        )

        async with AutoNudgeService.maintenance_service(base_dir=tmp_path) as view:
            await view.remove(legacy.id)

        runtime = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
        runtime._load()
        restored = runtime._loops[monitor.id]

        assert restored.active
        assert restored.monitor is not None
        assert restored.monitor.outcome is None
    finally:
        writer.stop()
        if runtime is not None:
            runtime.stop()
