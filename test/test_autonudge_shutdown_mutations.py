"""AutoNudgeService shutdown-drain tests: admitted-mutation commit boundary.

One of the sibling topic modules split from the original
``test_autonudge_shutdown_drain.py``. These cover the mutation-admission lease:
an admitted add/update/remove/monitor mutation commits even as shutdown closes
the section, a cancelled caller's write still settles, and the boundary blocks
rearm and new mutations.

``_enable`` is autouse (imported from ``autonudge_helpers``); ``svc_fixture``
registers the ``svc`` fixture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from unittest.mock import AsyncMock

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401
from autonudge_shutdown_drain_helpers import _LOST_RUN_SECS, _wait_until

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorUpdateConflict,
    NudgeAdmissionRefused,
    NudgeLoop,
)
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorOutcome,
)
from kiro_crew.testing.wait import async_wait_until, until_parked


@pytest.mark.asyncio
async def test_MUTATION_shutdown_boundary_blocks_rearm_and_new_mutations(tmp_path, monkeypatch):
    """A drained add cannot repopulate timers or submit work after closure."""
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()

    def blocking_write(payload):
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    add_task = asyncio.create_task(svc.add(slot_key="slot-boundary", message="later", idle_secs=60))
    await async_wait_until(write_started.is_set)
    shutdown_task = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    assert not svc._accepting_mutations

    release_write.set()
    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)

    assert loop.id not in svc._timers
    assert svc._inflight_adds == set()
    assert svc._inflight_persistence == set()
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.add(slot_key="late-add", message="no", idle_secs=60)
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.update(loop.id, message="late-update")
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.remove(loop.id)
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.add_monitor(
            slot_key="late-monitor",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.update_monitor("missing-monitor", wake_instructions="late")
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        await svc.stop_monitor("missing-monitor")
    assert svc.get_by_slot("slot-boundary") is loop
    with pytest.raises(NudgeAdmissionRefused, match="gateway is restarting"):
        svc._start_persistence(svc._serialize_state())
    assert svc._inflight_persistence == set()


@pytest.mark.asyncio
async def test_shutdown_boundary_blocks_inflight_update_rearm(tmp_path, monkeypatch):
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    loop = await svc.add(slot_key="slot-update", message="one", idle_secs=60)
    await svc.update(loop.id, active=False)
    assert loop.id not in svc._timers

    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()

    def blocking_write(payload):
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    update_task = asyncio.create_task(svc.update(loop.id, active=True))
    await async_wait_until(write_started.is_set)
    shutdown_task = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    release_write.set()

    assert await asyncio.wait_for(update_task, timeout=_LOST_RUN_SECS) is loop
    await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)
    assert loop.active
    assert loop.id not in svc._timers
    assert svc._inflight_adds == set()
    assert svc._inflight_persistence == set()


async def _wait_for_mutation_owner(svc: AutoNudgeService) -> None:
    for _ in range(100):
        if svc._inflight_adds:
            return
        await asyncio.sleep(0)
    raise AssertionError("mutation owner was not registered")


@pytest.mark.asyncio
async def test_MUTATION_admission_lease_is_single_use(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    admission = svc._admit_mutation()
    await svc._start_persistence(svc._serialize_state(), admission=admission)
    with pytest.raises(NudgeAdmissionRefused, match="already consumed"):
        svc._start_persistence(svc._serialize_state(), admission=admission)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["add", "update", "remove", "monitor"])
async def test_MUTATION_admitted_mutation_commits_after_shutdown_closes(tmp_path, operation):
    svc = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    await svc.start()
    existing = None
    if operation in {"update", "remove"}:
        existing = await svc.add(slot_key=f"slot-{operation}", message="before", idle_secs=60)
        svc._cancel_timer(existing.id)
    elif operation == "monitor":
        existing = await svc.add_monitor(
            slot_key="slot-monitor",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
        svc._cancel_timer(existing.id)

    gate = svc._lock if operation in {"add", "monitor"} else _an._maintenance_lock(svc._base_dir)
    await asyncio.wait_for(gate.acquire(), timeout=_LOST_RUN_SECS)
    if operation == "add":
        mutation = asyncio.create_task(svc.add("slot-add", "after", idle_secs=60))
    elif operation == "update":
        mutation = asyncio.create_task(svc.update(existing.id, message="after"))
    elif operation == "remove":
        mutation = asyncio.create_task(svc.remove(existing.id))
    else:
        mutation = asyncio.create_task(svc.update_monitor(existing.id, wake_instructions="after"))
    try:
        await _wait_for_mutation_owner(svc)
        shutdown = asyncio.create_task(svc.shutdown())
        await asyncio.sleep(0)
        gate.release()
        await asyncio.wait_for(mutation, timeout=_LOST_RUN_SECS)
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        if gate.locked():
            gate.release()

    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    try:
        slot = restored.get_by_slot(f"slot-{operation}")
        if operation == "remove":
            assert slot is None
        elif operation == "monitor":
            assert slot is not None and slot.monitor is not None
            assert slot.monitor.wake_instructions == "after"
        else:
            assert slot is not None and slot.message == "after"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove", "monitor"])
async def test_MUTATION_shutdown_drains_the_section_not_the_callers_remaining_task(
    tmp_path, monkeypatch, operation
):
    svc = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    await svc.start()
    if operation == "remove":
        loop = await svc.add(slot_key="slot-remove", message="before", idle_secs=60)
    else:
        loop = await svc.add_monitor(
            slot_key="slot-monitor",
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
    svc._cancel_timer(loop.id)
    committed = asyncio.Event()
    release_section = asyncio.Event()
    drain_snapshot = asyncio.Event()
    caller_tail = asyncio.Event()

    real_drain = _an._DurabilityDrain

    class ObservedDrain(real_drain):
        async def settle(self, *awaitables):
            if any(awaitable in svc._inflight_adds for awaitable in awaitables):
                drain_snapshot.set()
            return await asyncio.wait_for(super().settle(*awaitables), timeout=_LOST_RUN_SECS)

    monkeypatch.setattr(_an, "_DurabilityDrain", ObservedDrain)

    if operation == "remove":
        real_mutation = svc._remove_unserialized

        async def mutation_after_commit(*args, **kwargs):
            result = await real_mutation(*args, **kwargs)
            committed.set()
            await asyncio.wait_for(release_section.wait(), timeout=_LOST_RUN_SECS)
            return result

        monkeypatch.setattr(svc, "_remove_unserialized", mutation_after_commit)
    else:
        real_mutation = svc._update_monitor_admitted

        async def mutation_after_commit(*args, **kwargs):
            result = await real_mutation(*args, **kwargs)
            committed.set()
            await asyncio.wait_for(release_section.wait(), timeout=_LOST_RUN_SECS)
            return result

        monkeypatch.setattr(svc, "_update_monitor_admitted", mutation_after_commit)

    async def mutation_then_wait_forever():
        if operation == "remove":
            assert await svc.remove(loop.id)
        else:
            updated = await svc.update_monitor(loop.id, wake_instructions="after")
            assert updated is loop
        await asyncio.wait_for(caller_tail.wait(), timeout=_LOST_RUN_SECS)

    caller = asyncio.create_task(mutation_then_wait_forever())
    shutdown = None
    try:
        await asyncio.wait_for(committed.wait(), timeout=_LOST_RUN_SECS)
        shutdown = asyncio.create_task(svc.shutdown())
        await asyncio.wait_for(drain_snapshot.wait(), timeout=_LOST_RUN_SECS)
        release_section.set()
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
        assert not caller.done(), "shutdown cancelled or drained work after the mutation section"
    finally:
        release_section.set()
        caller.cancel()
        await asyncio.wait_for(
            asyncio.gather(caller, return_exceptions=True), timeout=_LOST_RUN_SECS
        )
        if shutdown is not None and not shutdown.done():
            shutdown.cancel()
            await asyncio.wait_for(
                asyncio.gather(shutdown, return_exceptions=True), timeout=_LOST_RUN_SECS
            )

    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    try:
        persisted = restored.get_by_id(loop.id)
        if operation == "remove":
            assert persisted is None
        else:
            assert persisted is not None and persisted.monitor is not None
            assert persisted.monitor.wake_instructions == "after"
    finally:
        await restored.shutdown()


@pytest.mark.asyncio
async def test_MUTATION_cancelled_remove_waiting_on_lock_still_persists(tmp_path, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-remove-cancelled", message="stop", idle_secs=60)
    service._cancel_timer(loop.id)
    lock = _an._maintenance_lock(service._base_dir)
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    acquire_started = asyncio.Event()
    real_acquire = service._acquire_mutation_lock

    async def observed_acquire(*args, **kwargs):
        acquire_started.set()
        return await real_acquire(*args, **kwargs)

    monkeypatch.setattr(service, "_acquire_mutation_lock", observed_acquire)
    caller = asyncio.create_task(service.remove(loop.id))
    registered: set[asyncio.Future] = set()
    try:
        await asyncio.wait_for(acquire_started.wait(), timeout=_LOST_RUN_SECS)
        registered = set(service._inflight_adds)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
        assert service.get_by_slot("slot-remove-cancelled") is loop
    finally:
        if lock.locked():
            lock.release()

    await asyncio.wait_for(
        asyncio.gather(*registered, return_exceptions=True), timeout=_LOST_RUN_SECS
    )
    await _wait_until(lambda: not service._inflight_adds, "cancelled remove settlement")
    assert service.get_by_slot("slot-remove-cancelled") is None
    stored = json.loads(service._path.read_text(encoding="utf-8"))
    assert stored["loops"] == [], "the cancelled handler left the removed row on disk"
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ["stop_monitor", "clear_terminal_monitor", "update_monitor"],
)
async def test_MUTATION_cancelled_monitor_mutation_waiting_on_lock_still_persists(
    tmp_path, operation
):
    service = AutoNudgeService(base_dir=tmp_path / "home", on_monitor_tick=AsyncMock())
    await service.start()
    loop = await service.add_monitor(
        slot_key=f"slot-{operation}",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    service._cancel_timer(loop.id)
    if operation == "clear_terminal_monitor":
        assert await service.stop_monitor(loop.id) is loop
        service._cancel_timer(loop.id)
        gate = _an._maintenance_lock(service._base_dir)
    else:
        gate = service._lock
    await asyncio.wait_for(gate.acquire(), timeout=_LOST_RUN_SECS)
    if operation == "stop_monitor":
        caller = asyncio.create_task(service.stop_monitor(loop.id))
    elif operation == "clear_terminal_monitor":
        caller = asyncio.create_task(service.clear_terminal_monitor(loop.id))
    else:
        caller = asyncio.create_task(
            service.update_monitor(loop.id, wake_instructions="persist after cancel")
        )
    registered: set[asyncio.Future] = set()
    try:
        await _wait_for_mutation_owner(service)
        registered = set(service._inflight_adds)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
    finally:
        if gate.locked():
            gate.release()

    await asyncio.wait_for(
        asyncio.gather(*registered, return_exceptions=True), timeout=_LOST_RUN_SECS
    )
    await _wait_until(lambda: not service._inflight_adds, f"cancelled {operation} settlement")
    current = service.get_by_id(loop.id)
    if operation == "clear_terminal_monitor":
        assert current is None
    elif operation == "stop_monitor":
        assert current is loop and loop.monitor is not None
        assert loop.monitor.outcome is MonitorOutcome.USER_STOP
    else:
        assert current is loop and loop.monitor is not None
        assert loop.monitor.wake_instructions == "persist after cancel"

    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    persisted = restored.get_by_id(loop.id)
    if operation == "clear_terminal_monitor":
        assert persisted is None
    elif operation == "stop_monitor":
        assert persisted is not None and persisted.monitor is not None
        assert persisted.monitor.outcome is MonitorOutcome.USER_STOP
    else:
        assert persisted is not None and persisted.monitor is not None
        assert persisted.monitor.wake_instructions == "persist after cancel"
    await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "remove",
        "stop_monitor",
        "clear_terminal_monitor",
        "update_monitor",
        "release_approval_hold",
    ],
)
async def test_MUTATION_caller_cancelled_mid_write_is_held_until_the_write_settles(
    tmp_path, monkeypatch, operation
):
    """Once an admitted mutation's write is in flight, its caller waits it out.

    The child completes the mutation either way; what this pins is the caller's
    side: cancelled (twice) while the write runs, it stays attached until the write
    settles and only then receives ``CancelledError``.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home", on_monitor_tick=AsyncMock())
    await service.start()
    slot = f"slot-mid-write-{operation}"
    if operation in {"remove", "release_approval_hold"}:
        loop = await service.add(slot_key=slot, message="go", idle_secs=60)
    else:
        loop = await service.add_monitor(
            slot_key=slot,
            kind="github_pull_request",
            target="owner/repo#123",
            objective="review_ready",
            cadence_secs=60,
            budgets=MonitorBudgets(),
        )
    service._cancel_timer(loop.id)
    if operation == "clear_terminal_monitor":
        assert await service.stop_monitor(loop.id) is loop
        service._cancel_timer(loop.id)
    elif operation == "release_approval_hold":
        service.notify_approval_stalled(slot)
        await _wait_until(lambda: loop.approval_stalled, "the approval hold record")
        await _wait_until(lambda: not service._inflight_adds, "the approval hold settlement")
        service._cancel_timer(loop.id)

    real_write = service._write_state
    armed = threading.Event()
    write_started = threading.Event()
    release_write = threading.Event()
    order: list[str] = []

    def _blocking_write(payload: dict) -> None:
        if armed.is_set():
            write_started.set()
            if not release_write.wait(_LOST_RUN_SECS):
                raise AssertionError("the test did not release the writer")
        real_write(payload)
        order.append("write-done")

    monkeypatch.setattr(service, "_write_state", _blocking_write)
    armed.set()
    if operation == "remove":
        caller = asyncio.create_task(service.remove(loop.id))
    elif operation == "stop_monitor":
        caller = asyncio.create_task(service.stop_monitor(loop.id))
    elif operation == "clear_terminal_monitor":
        caller = asyncio.create_task(service.clear_terminal_monitor(loop.id))
    elif operation == "update_monitor":
        caller = asyncio.create_task(
            service.update_monitor(loop.id, wake_instructions="held through the write")
        )
    else:
        caller = asyncio.create_task(service.release_approval_hold(slot, why="test", arm=False))
    try:
        await async_wait_until(write_started.is_set)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done(), "the cancelled caller left before its write settled"
    finally:
        release_write.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
    assert order == ["write-done"], "the caller was released before the write landed"

    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    persisted = restored.get_by_id(loop.id)
    if operation in {"remove", "clear_terminal_monitor"}:
        assert persisted is None
    elif operation == "stop_monitor":
        assert persisted is not None and persisted.monitor is not None
        assert persisted.monitor.outcome is MonitorOutcome.USER_STOP
    elif operation == "update_monitor":
        assert persisted is not None and persisted.monitor is not None
        assert persisted.monitor.wake_instructions == "held through the write"
    else:
        assert persisted is not None and persisted.approval_stalled is False
    await service.shutdown()


@pytest.mark.asyncio
async def test_MUTATION_shutdown_drains_cancelled_caller_remove(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-remove-shutdown", message="stop", idle_secs=60)
    service._cancel_timer(loop.id)
    lock = _an._maintenance_lock(service._base_dir)
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    caller = asyncio.create_task(service.remove(loop.id))
    shutdown: asyncio.Task[None] | None = None
    try:
        await _wait_for_mutation_owner(service)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=_LOST_RUN_SECS)
        shutdown = asyncio.create_task(service.shutdown())
        await _wait_until(lambda: not service.accepting_mutations, "shutdown admission closure")
        assert not shutdown.done(), "shutdown abandoned the admitted removal"
        lock.release()
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        if lock.locked():
            lock.release()
        if shutdown is not None and not shutdown.done():
            shutdown.cancel()
            await asyncio.wait_for(
                asyncio.gather(shutdown, return_exceptions=True), timeout=_LOST_RUN_SECS
            )

    assert service.get_by_slot("slot-remove-shutdown") is None
    stored = json.loads(service._path.read_text(encoding="utf-8"))
    assert stored["loops"] == [], "shutdown returned before the removal reached disk"


@pytest.mark.asyncio
async def test_MUTATION_running_callback_is_cancelled_after_remove_settles(tmp_path, monkeypatch):
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-callback-remove", message="stop", idle_secs=60)
    service._cancel_timer(loop.id)
    lock = _an._maintenance_lock(service._base_dir)
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    entered = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def remove_from_callback(callback_loop):
        entered.set()
        try:
            await service.remove(callback_loop.id)
        except asyncio.CancelledError:
            callback_cancelled.set()
            raise

    monkeypatch.setattr(service, "_run_timer_callback", remove_from_callback)
    service._arm_timer(loop, delay=0.0)
    timer = service._timers[loop.id]
    shutdown: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=_LOST_RUN_SECS)
        await _wait_until(
            lambda: timer in _an._timers._drain_registered_owners(service),
            "remove transaction callback ownership",
        )
        shutdown = asyncio.create_task(service.shutdown())
        await _wait_until(lambda: not service.accepting_mutations, "shutdown admission closure")
        assert not timer.done(), "shutdown cancelled the callback before removal settled"
        assert not callback_cancelled.is_set()
        lock.release()
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        if lock.locked():
            lock.release()
        if shutdown is not None and not shutdown.done():
            shutdown.cancel()
            await asyncio.wait_for(
                asyncio.gather(shutdown, return_exceptions=True), timeout=_LOST_RUN_SECS
            )

    assert callback_cancelled.is_set()
    assert timer.done() and timer.cancelled()
    assert service.get_by_slot("slot-callback-remove") is None
    stored = json.loads(service._path.read_text(encoding="utf-8"))
    assert stored["loops"] == []


@pytest.mark.asyncio
async def test_admitted_transaction_does_not_recancel_owner_after_its_wait_ended(tmp_path):
    service = AutoNudgeService(base_dir=tmp_path / "home")
    transaction_entered = asyncio.Event()
    release_transaction = asyncio.Event()
    owner_left_wait = asyncio.Event()
    release_owner = asyncio.Event()
    second_cancellation = asyncio.Event()

    async def transaction() -> None:
        transaction_entered.set()
        await asyncio.wait_for(release_transaction.wait(), timeout=_LOST_RUN_SECS)

    async def owner() -> None:
        task = asyncio.current_task()
        assert task is not None
        service._running_callbacks.add(task)
        try:
            try:
                await _an._timers._run_admitted_transaction(
                    service,
                    transaction(),
                    cancel_callback_on_exit=True,
                    on_failure_log="unexpected transaction failure",
                )
            except asyncio.CancelledError:
                owner_left_wait.set()
            try:
                await asyncio.wait_for(release_owner.wait(), timeout=_LOST_RUN_SECS)
            except asyncio.CancelledError:
                second_cancellation.set()
                raise
        finally:
            service._running_callbacks.discard(task)

    callback = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(transaction_entered.wait(), timeout=_LOST_RUN_SECS)
        callback.cancel()
        await asyncio.wait_for(owner_left_wait.wait(), timeout=_LOST_RUN_SECS)
        service._accepting_mutations = False
        release_transaction.set()
        await _wait_until(
            lambda: not service._inflight_adds,
            "detached transaction settlement",
        )
        assert not second_cancellation.is_set()
        assert not callback.done()
        release_owner.set()
        await asyncio.wait_for(callback, timeout=_LOST_RUN_SECS)
    finally:
        release_transaction.set()
        release_owner.set()
        if not callback.done():
            callback.cancel()
        await asyncio.wait_for(
            asyncio.gather(callback, return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )


@pytest.mark.asyncio
async def test_MUTATION_prior_lease_write_does_not_hold_a_lock_waiting_transaction(
    tmp_path, monkeypatch
):
    """Only the current transaction's submission can hold its cancelled caller."""
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-prior-write", message="stop", idle_secs=60)
    service._cancel_timer(loop.id)
    lock = _an._maintenance_lock(service._base_dir)
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    prior_write_landed = asyncio.Event()
    caller_released = asyncio.Event()
    hold_started = asyncio.Event()
    real_drain = _an._timers._DurabilityDrain

    class ObservedDrain(real_drain):
        def note_cancelled(self) -> None:
            hold_started.set()
            super().note_cancelled()

    monkeypatch.setattr(_an._timers, "_DurabilityDrain", ObservedDrain)

    async def remove_after_prior_write(_loop) -> None:
        await service._persist_locked()
        prior_write_landed.set()
        try:
            await service.remove(loop.id)
        except asyncio.CancelledError:
            caller_released.set()
            raise

    monkeypatch.setattr(service, "_run_timer_callback", remove_after_prior_write)
    service._arm_timer(loop, delay=0.0)
    timer = service._timers[loop.id]
    released_wait = asyncio.create_task(caller_released.wait())
    held_wait = asyncio.create_task(hold_started.wait())
    try:
        await asyncio.wait_for(prior_write_landed.wait(), timeout=_LOST_RUN_SECS)
        await until_parked(lock, timeout=_LOST_RUN_SECS)
        timer.cancel()
        done, _ = await asyncio.wait(
            {released_wait, held_wait},
            timeout=_LOST_RUN_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert released_wait in done, "a prior lease write held the later transaction's caller"
        assert held_wait not in done
    finally:
        lock.release()
        for waiter in (released_wait, held_wait):
            if not waiter.done():
                waiter.cancel()
        await asyncio.wait_for(
            asyncio.gather(timer, released_wait, held_wait, return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )
        await _wait_until(lambda: not service._inflight_adds, "the removal child settlement")

    assert service.get_by_id(loop.id) is None
    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    assert restored.get_by_id(loop.id) is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_MUTATION_a_detached_lease_persist_does_not_hold_a_lock_waiting_transaction(
    tmp_path, monkeypatch
):
    """A ``_persist_soon`` under the callback's lease is not this transaction's write.

    The gate schedules the detached persist and the callback reaches ``remove()``
    with no yield between, so that write lands only once the transaction waits for
    its lock. Counting it as the transaction's own holds the cancelled caller for
    the whole post-cancellation window. The transaction's token stays at zero, the
    caller leaves at once, and the registered child completes the removal.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-detached-persist", message="stop", idle_secs=60)
    service._cancel_timer(loop.id)
    lock = _an._maintenance_lock(service._base_dir)
    await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
    detached_write_submitted = asyncio.Event()
    caller_released = asyncio.Event()
    hold_started = asyncio.Event()
    real_drain = _an._timers._DurabilityDrain

    class ObservedDrain(real_drain):
        def note_cancelled(self) -> None:
            hold_started.set()
            super().note_cancelled()

    monkeypatch.setattr(_an._timers, "_DurabilityDrain", ObservedDrain)
    real_start = service._start_persistence

    def _observed_start(payload, *, admission=None):
        future = real_start(payload, admission=admission)
        detached_write_submitted.set()
        return future

    monkeypatch.setattr(service, "_start_persistence", _observed_start)

    async def remove_after_scheduling_a_persist(_loop) -> None:
        service._persist_soon()
        try:
            await service.remove(loop.id)
        except asyncio.CancelledError:
            caller_released.set()
            raise

    monkeypatch.setattr(service, "_run_timer_callback", remove_after_scheduling_a_persist)
    service._arm_timer(loop, delay=0.0)
    timer = service._timers[loop.id]
    released_wait = asyncio.create_task(caller_released.wait())
    held_wait = asyncio.create_task(hold_started.wait())
    try:
        await asyncio.wait_for(detached_write_submitted.wait(), timeout=_LOST_RUN_SECS)
        await until_parked(lock, timeout=_LOST_RUN_SECS)
        timer.cancel()
        done, _ = await asyncio.wait(
            {released_wait, held_wait},
            timeout=_LOST_RUN_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert released_wait in done, "a detached lease write held the transaction's caller"
        assert held_wait not in done
    finally:
        lock.release()
        for waiter in (released_wait, held_wait):
            if not waiter.done():
                waiter.cancel()
        await asyncio.wait_for(
            asyncio.gather(timer, released_wait, held_wait, return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )
        await _wait_until(lambda: not service._inflight_adds, "the removal child settlement")

    assert service.get_by_id(loop.id) is None
    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    assert restored.get_by_id(loop.id) is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_an_observer_task_cannot_count_toward_the_transactions_submissions(tmp_path):
    """A write an observer's task starts from inside a child is not the child's.

    ``_emit`` clears the submission token for the observers it calls, so a task an
    observer creates copies a context without it. Only the write the transaction
    itself starts moves its count.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home")
    admission = service._admit_mutation(allow_multiple_persistence=True)
    seen: list[tuple[str, object]] = []
    observer_writes: list[asyncio.Task[None]] = []

    async def _write_from_the_observer() -> None:
        seen.append(("observer task", service._admitted_transaction_submissions.get()))
        await service._start_persistence(service._serialize_state())

    def observer(event: str, _loop: NudgeLoop | None) -> None:
        seen.append(("observer", service._admitted_transaction_submissions.get()))
        observer_writes.append(asyncio.ensure_future(_write_from_the_observer()))

    service.subscribe(observer)

    async def transaction() -> tuple[int, int]:
        submissions = service._admitted_transaction_submissions.get()
        assert submissions is not None, "the child must carry its own submission token"
        service._emit("probe", None)
        await asyncio.wait_for(asyncio.gather(*observer_writes), timeout=_LOST_RUN_SECS)
        after_observer = submissions.count
        await service._start_persistence(service._serialize_state(), admission=admission)
        return after_observer, submissions.count

    counts = await asyncio.wait_for(
        _an._timers._run_admitted_transaction(
            service,
            transaction(),
            on_failure_log="unexpected transaction failure",
        ),
        timeout=_LOST_RUN_SECS,
    )

    assert seen == [("observer", None), ("observer task", None)]
    assert counts == (0, 1), "only the transaction's own write may move its count"


@pytest.mark.asyncio
async def test_an_observer_task_cannot_reach_a_monitor_updates_post_commit_handle(tmp_path):
    """A task an observer creates from inside an update cannot see its continuation.

    A detached monitor-update child carries its post-commit handle, and through it
    the credential grant callback, in ``_MONITOR_UPDATE_POST_COMMIT``. ``_emit`` clears
    it for the observers it calls, so a task an observer creates copies a context
    without it, and the emitter gets its own handle back afterwards.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home")
    seen: list[tuple[str, object]] = []
    observer_tasks: list[asyncio.Task[None]] = []

    async def _observer_task() -> None:
        seen.append(("observer task", _an._monitor_records._MONITOR_UPDATE_POST_COMMIT.get()))

    def observer(event: str, _loop: NudgeLoop | None) -> None:
        seen.append(("observer", _an._monitor_records._MONITOR_UPDATE_POST_COMMIT.get()))
        observer_tasks.append(asyncio.ensure_future(_observer_task()))

    service.subscribe(observer)

    async def _grant(*_args: object) -> None:
        return None

    with _an._monitor_records._monitor_update_post_commit(_grant) as handle:
        service._emit("updated", None)
        assert _an._monitor_records._MONITOR_UPDATE_POST_COMMIT.get() is handle
    await asyncio.wait_for(asyncio.gather(*observer_tasks), timeout=_LOST_RUN_SECS)

    assert seen == [("observer", None), ("observer task", None)]


@pytest.mark.asyncio
async def test_MUTATION_lifecycle_persist_queued_before_shutdown_keeps_its_write(
    tmp_path, monkeypatch
) -> None:
    """A sync lifecycle mutation owns admission before its persist task runs."""
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    loop = await service.add(slot_key="slot-lifecycle", message="check", idle_secs=60)
    service._cancel_timer(loop.id)

    real_start = service._start_persistence
    first_write_entered = asyncio.Event()
    release_first_write = asyncio.Event()
    block_first_write = True

    async def _start_after_release(payload, *, admission=None):
        nonlocal block_first_write
        if block_first_write:
            block_first_write = False
            first_write_entered.set()
            await asyncio.wait_for(release_first_write.wait(), timeout=_LOST_RUN_SECS)
        return await real_start(payload, admission=admission)

    monkeypatch.setattr(service, "_start_persistence", _start_after_release)
    blocker = asyncio.create_task(service._persist_locked(admission=service._admit_mutation()))
    shutdown: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(first_write_entered.wait(), timeout=_LOST_RUN_SECS)
        service.notify_cycle_start_failed(loop.slot_key)
        assert loop.consecutive_start_failures == 1
        await until_parked(service._lock, timeout=_LOST_RUN_SECS)

        shutdown = asyncio.create_task(service.shutdown())
        await _wait_until(
            lambda: not service.accepting_mutations,
            "mutation admission closure",
        )
        assert not shutdown.done(), "shutdown skipped the queued lifecycle persist"
        release_first_write.set()
        await asyncio.wait_for(blocker, timeout=_LOST_RUN_SECS)
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        release_first_write.set()
        pending = [task for task in (blocker, shutdown) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=_LOST_RUN_SECS,
            )

    restored = AutoNudgeService(base_dir=tmp_path / "home")
    await asyncio.to_thread(restored._load)
    restored_loop = restored.get_by_id(loop.id)
    assert restored_loop is not None
    assert restored_loop.consecutive_start_failures == 1


@pytest.mark.asyncio
async def test_persist_soon_after_shutdown_schedules_nothing(tmp_path) -> None:
    """A fresh lifecycle persist cannot enter after admission closes."""
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    await service.shutdown()
    persisted = service._path.read_bytes()

    service._persist_soon()

    assert service._inflight_adds == set()
    assert service._inflight_persistence == set()
    assert service._path.read_bytes() == persisted


@pytest.mark.asyncio
async def test_a_persist_soon_task_cannot_count_toward_the_transactions_submissions(
    tmp_path, monkeypatch
):
    """A persist task created inside the child is not the child's own write."""
    service = AutoNudgeService(base_dir=tmp_path / "home")
    admission = service._admit_mutation(allow_multiple_persistence=True)
    detached_write_submitted = asyncio.Event()
    real_start = service._start_persistence

    def _observe_start(payload, *, admission=None):
        future = real_start(payload, admission=admission)
        detached_write_submitted.set()
        return future

    monkeypatch.setattr(service, "_start_persistence", _observe_start)

    async def transaction() -> tuple[int, int]:
        submissions = service._admitted_transaction_submissions.get()
        assert submissions is not None, "the child must carry its own submission token"
        service._persist_soon()
        await asyncio.wait_for(detached_write_submitted.wait(), timeout=_LOST_RUN_SECS)
        after_detached = submissions.count
        await service._start_persistence(service._serialize_state(), admission=admission)
        return after_detached, submissions.count

    counts = await asyncio.wait_for(
        _an._timers._run_admitted_transaction(
            service,
            transaction(),
            on_failure_log="unexpected transaction failure",
        ),
        timeout=_LOST_RUN_SECS,
    )
    await _wait_until(lambda: not service._inflight_adds, "the detached persist")

    assert counts == (0, 1), "only the transaction's own write may move its count"


@pytest.mark.asyncio
async def test_a_callback_held_through_its_write_takes_the_closure_cancel_as_one(tmp_path):
    """The hold absorbs shutdown's cancel of the owning callback.

    A callback cancelled once its transaction's write was submitted stays attached
    until the transaction settles. Admission closing in that window makes the
    helper cancel the callback again; the hold absorbs that, the callback sees
    exactly one ``CancelledError``, and the companion record resolves only after.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home")
    admission = service._admit_mutation()
    written = asyncio.Event()
    finish_write = asyncio.Event()
    seen: list[str] = []
    requests: list[int] = []

    async def transaction() -> None:
        admission.persistence_submitted = True
        submissions = service._admitted_transaction_submissions.get()
        assert submissions is not None, "the child must carry its own submission token"
        submissions.count += 1
        written.set()
        await asyncio.wait_for(finish_write.wait(), timeout=_LOST_RUN_SECS)

    async def owner() -> None:
        task = asyncio.current_task()
        assert task is not None
        service._running_callbacks.add(task)
        try:
            await _an._timers._run_admitted_transaction(
                service,
                transaction(),
                cancel_callback_on_exit=True,
                on_failure_log="unexpected transaction failure",
            )
        except asyncio.CancelledError:
            seen.append("cancelled")
            for _ in range(5):
                await asyncio.sleep(0)
            seen.append("no second cancel")
            # Two requests reached this task, this test's and the helper's closure
            # cancel, and the hold delivered them as the one error caught above.
            requests.append(asyncio.current_task().cancelling())  # type: ignore[union-attr]
        finally:
            service._running_callbacks.discard(task)

    callback = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(written.wait(), timeout=_LOST_RUN_SECS)
        callback.cancel()
        await asyncio.sleep(0)
        assert not callback.done(), "the callback left before its write settled"
        service._accepting_mutations = False
        finish_write.set()
        await asyncio.wait_for(callback, timeout=_LOST_RUN_SECS)
        await _wait_until(lambda: not service._inflight_adds, "the transaction's settlement")
    finally:
        finish_write.set()
        if not callback.done():
            callback.cancel()
        await asyncio.wait_for(
            asyncio.gather(callback, return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )

    assert seen == ["cancelled", "no second cancel"]
    assert requests == [2], "the closure cancel was not issued during the hold"


@pytest.mark.asyncio
async def test_expected_admitted_transaction_refusal_has_no_warning_traceback(tmp_path, caplog):
    service = AutoNudgeService(base_dir=tmp_path / "home")

    async def refuse() -> None:
        raise MonitorUpdateConflict("caller-facing conflict")

    caplog.set_level(logging.WARNING, logger="kiro_crew.autonudge")
    with pytest.raises(MonitorUpdateConflict, match="caller-facing conflict"):
        await _an._timers._run_admitted_transaction(
            service,
            refuse(),
            on_failure_log="unexpected detached warning",
            expected=(MonitorUpdateConflict,),
        )

    assert not [
        record
        for record in caplog.records
        if record.name == "kiro_crew.autonudge"
        and record.getMessage() == "unexpected detached warning"
    ]


@pytest.mark.asyncio
async def test_MUTATION_a_cancelled_guard_exit_still_releases_its_user_count(tmp_path):
    """The per-monitor guard's exit bookkeeping cannot be skipped by a cancellation.

    The exit runs while the service lock is held elsewhere and the user is then
    cancelled. A count left behind would pin the lock entry of a row that is gone.
    """
    from kiro_crew.autonudge_service.maintenance import _monitor_mutation_guard

    service = AutoNudgeService(base_dir=tmp_path / "home")
    entered = asyncio.Event()
    leave_body = asyncio.Event()

    async def _user() -> None:
        async with _monitor_mutation_guard(service, "gone-monitor"):
            entered.set()
            await asyncio.wait_for(leave_body.wait(), timeout=_LOST_RUN_SECS)

    user = asyncio.create_task(_user())
    parked: asyncio.Task[int] | None = None
    await asyncio.wait_for(entered.wait(), timeout=_LOST_RUN_SECS)
    assert service._monitor_mutation_lock_users == {"gone-monitor": 1}
    await asyncio.wait_for(service._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        leave_body.set()
        # The exit either finishes at once or parks on the held service lock.
        parked = asyncio.create_task(until_parked(service._lock, timeout=_LOST_RUN_SECS))
        await asyncio.wait(
            {user, parked}, timeout=_LOST_RUN_SECS, return_when=asyncio.FIRST_COMPLETED
        )
        user.cancel()
        await asyncio.wait_for(asyncio.gather(user, return_exceptions=True), _LOST_RUN_SECS)
    finally:
        service._lock.release()
        if parked is not None and not parked.done():
            parked.cancel()
        await asyncio.gather(*(t for t in (user, parked) if t is not None), return_exceptions=True)

    assert "gone-monitor" not in service._monitor_mutation_lock_users
    assert "gone-monitor" not in service._monitor_mutation_locks


@pytest.mark.asyncio
async def test_MUTATION_a_task_an_admitted_child_spawns_from_its_emit_does_not_inherit_its_owner(
    tmp_path,
):
    """An observer's follow-up task must not pass for the owner's timer callback.

    The owner here is the live timer task of ``keep`` and runs ``remove(target)``
    through the detached child. The observer of that child's ``removed`` event
    spawns a task which later retires ``keep``'s timer, and that cancel must reach
    the owner instead of being taken for the owner cancelling itself.
    """
    service = AutoNudgeService(base_dir=tmp_path / "home")
    await service.start()
    keep = await service.add(slot_key="chat-keep-1", message="keep", idle_secs=600)
    target = await service.add(slot_key="chat-target-1", message="go", idle_secs=600)
    removed_returned = asyncio.Event()
    hold_owner = asyncio.Event()
    spawned: list[asyncio.Task[None]] = []
    inherited: list[object] = []

    async def _observer_followup() -> None:
        inherited.append(service._admitted_transaction_owner.get())
        await asyncio.wait_for(removed_returned.wait(), timeout=_LOST_RUN_SECS)
        service._cancel_timer(keep.id)

    def _observer(event: str, loop: NudgeLoop | None) -> None:
        if event == "removed" and loop is not None and loop.id == target.id:
            spawned.append(asyncio.ensure_future(_observer_followup()))

    service.subscribe(_observer)

    async def _owner() -> None:
        await service.remove(target.id)
        removed_returned.set()
        await asyncio.wait_for(hold_owner.wait(), timeout=_LOST_RUN_SECS)

    service._cancel_timer(keep.id)
    owner = asyncio.create_task(_owner())
    service._timers[keep.id] = owner
    try:
        await asyncio.wait_for(removed_returned.wait(), timeout=_LOST_RUN_SECS)
        await asyncio.wait_for(asyncio.gather(*spawned), timeout=_LOST_RUN_SECS)
        assert inherited == [None], "the observer's task inherited the owner"
        await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), _LOST_RUN_SECS)
        assert owner.cancelled(), "the follow-up's cancel was taken for self-cancellation"
    finally:
        hold_owner.set()
        await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), _LOST_RUN_SECS)
        await service.shutdown()


@pytest.mark.asyncio
async def test_add_update_remove_remain_admitted_before_shutdown(tmp_path):
    """Opposite mode: the admission boundary changes no live-service behavior."""
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    loop = await svc.add(slot_key="slot-live", message="one", idle_secs=60)
    assert loop.id in svc._timers
    updated = await svc.update(loop.id, message="two")
    assert updated is loop
    assert loop.message == "two"
    await svc.remove(loop.id)
    assert svc.get_by_slot("slot-live") is None
    await svc.shutdown()


@pytest.mark.asyncio
async def test_a_turn_hook_after_shutdown_arms_nothing(tmp_path):
    """Sessions close AFTER AutoNudge drains, so a turn can still end on a slot whose
    loop is active. Its completion hook must not arm a timer, or schedule the
    deadline persist that comes with one, on a service that has already shut down."""
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    loop = await svc.add(slot_key="chat-1-555", message="go", idle_secs=60)
    await svc.shutdown()
    assert svc._timers == {}
    persists: list[int] = []
    svc._persist_soon = lambda: persists.append(1)  # type: ignore[method-assign]

    loop.next_due_ts = 0.0
    svc.notify_turn_complete("chat-1-555")
    svc._arm_timer(loop, delay=0.0)

    assert svc._timers == {}
    assert persists == []
    assert svc._inflight_persistence == set()
    # The hook's conductor push is not a timer; let it settle so nothing leaks.
    if svc._inflight_adds:
        await asyncio.wait_for(
            asyncio.gather(*list(svc._inflight_adds), return_exceptions=True),
            timeout=_LOST_RUN_SECS,
        )


@pytest.mark.asyncio
async def test_shutdown_never_awaits_its_current_task(tmp_path):
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    current = asyncio.current_task()
    assert current is not None
    svc._inflight_adds.add(current)

    await asyncio.wait_for(svc.shutdown(), timeout=_LOST_RUN_SECS)

    assert current in svc._inflight_adds
    svc._inflight_adds.discard(current)
