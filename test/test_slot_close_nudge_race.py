"""Close-vs-expiry race on the dashboard slot-delete handler.

``api_chat_slot_delete`` retires the auto-nudge loop before its first await: a timer
firing on a missing slot rehydrates with ``adopt_closed=True`` and restores the
dismissed session. Cancelling ``slot.task`` re-arms through ``notify_turn_complete``,
so the loop is removed, not just disarmed.

When the close fails (500), a replacement loop is put back with the remaining budget,
never a fresh one.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import autonudge
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.crew_log import emit as _emit_module
from kiro_crew.dashboard import chat_handlers as handlers
from kiro_crew.monitoring.models import MonitorBudgets, MonitorOutcome

NAME = "chat-1-1785"


class _Req:
    """Minimal stand-in for the aiohttp request the delete handler reads.

    The race tests drive the handler directly rather than through a client: the
    interleaving has to be scheduled deterministically, and the client's own
    awaits would let the expiring timer run before the handler even started.
    """

    def __init__(self, state, slot: str) -> None:
        self.app = {"state": state}
        self.match_info = {"slot": slot}

    def get(self, key: str, default: str = "") -> str:
        del key
        return default


def _state_with_slot(tmp_path, name: str = NAME):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(name)
    slot.append("user", "watch the PR")
    slot.append("assistant", "watching")
    slot.drain()
    return state


def test_slot_owns_its_close_admission_fence(tmp_path) -> None:
    slot = _state_with_slot(tmp_path).get_slot(NAME)
    assert slot is not None
    assert slot.is_closing is False

    slot.begin_close()
    assert slot.is_closing is True

    slot.cancel_close()
    assert slot.is_closing is False


async def _service(tmp_path, monkeypatch, on_fire=None) -> AutoNudgeService:
    """A real AutoNudgeService registered as the process-wide instance.

    Real, not a mock: the ordering this suite asserts lives in the service's own
    remove/timer bookkeeping, and a mock would assert nothing about it.
    """
    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    monkeypatch.setattr(autonudge, "_INSTANCE", svc)
    return svc


@pytest.mark.asyncio
async def test_nudge_expiring_during_the_persist_cannot_resurrect(tmp_path, monkeypatch) -> None:
    """The reported race, driven deterministically — no sleeps, no hoping."""
    state = _state_with_slot(tmp_path)
    fired: list[str] = []

    async def _fire(lp) -> bool:
        fired.append(lp.id)
        # What _fire_dashboard_nudge does on a get_slot miss: rehydrate the
        # persisted session (adopt_closed=True) and drive a turn in it.
        state.get_or_create_slot(NAME)
        return True

    svc = await _service(tmp_path, monkeypatch, on_fire=_fire)
    loop = await svc.add(NAME, "check the PR", idle_secs=15, max_cycles=24)

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _persist(*_a, **_kw) -> bool:
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    def refuse_durable_execution_read(*_args, **_kwargs):
        pytest.fail("close cleanup must snapshot live execution without a disk read")

    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution", refuse_durable_execution_read
    )

    # The loop's idle timer is armed and about to expire. Collapsing its delay
    # to 0 makes the expiry SCHEDULED rather than timing-dependent: this task is
    # queued before the close task, so the event loop gives it its turn first,
    # and it parks on a bare yield exactly while the close is running.
    svc._arm_timer(loop, delay=0)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await entered.wait()  # the close is now parked inside the persist
    for _ in range(4):  # every chance for the expiring timer to land
        await asyncio.sleep(0)
    release.set()
    resp = await close

    assert resp.status == 200
    assert NAME not in state._slots, "the closed session was resurrected by its own loop"
    assert fired == [], "a nudge fired into the session being closed"
    assert svc.get_by_slot(NAME) is None
    svc.stop()


@pytest.mark.asyncio
async def test_loop_is_retired_before_the_persist_begins(tmp_path, monkeypatch) -> None:
    """Order, not end state: the retirement must precede the closure persist."""
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=15)

    order: list[str] = []
    svc.subscribe(lambda event, _lp: order.append(f"loop:{event}"))
    original_retire = handlers._retire_slot_nudge_loop

    async def _retire(slot_key: str):
        assert slot.is_closing is True
        return await original_retire(slot_key)

    async def _persist(*_a, **_kw) -> bool:
        order.append("persist")
        assert slot.is_closing is True
        return True

    monkeypatch.setattr(handlers, "_retire_slot_nudge_loop", _retire)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = AsyncMock(side_effect=lambda *_a: order.append("session_teardown"))

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert order == [
        "loop:removed",
        "persist",
        "session_teardown",
    ], "the loop must be gone before the closure is persisted, not after"
    svc.stop()


@pytest.mark.asyncio
async def test_cancelled_close_releases_the_live_slot_admission_fence(
    tmp_path, monkeypatch
) -> None:
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    entered = asyncio.Event()
    parked = asyncio.Event()

    async def _retire(_slot_key: str):
        entered.set()
        await parked.wait()

    monkeypatch.setattr(handlers, "_retire_slot_nudge_loop", _retire)
    close = asyncio.create_task(handlers.close_slot(state, slot, NAME))
    await entered.wait()
    assert slot.is_closing is True

    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close

    assert state.get_slot(NAME) is slot
    assert slot.is_closing is False


@pytest.mark.asyncio
async def test_close_with_no_armed_loop_is_unaffected(tmp_path, monkeypatch) -> None:
    """The shared close path: no loop on this slot, and someone else's survives."""
    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    other = await svc.add("chat-2-9999", "watch the other PR", idle_secs=15)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.delete(f"/api/chat/slots/{NAME}")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True

    assert NAME not in state._slots
    saved = state.conversation_log.read_messages(f"dashboard:{NAME}")
    assert [m["role"] for m in saved] == ["user", "assistant"]
    survivor = svc.get_by_slot("chat-2-9999")
    assert (
        survivor is not None and survivor.id == other.id
    ), "closing one tab retired another tab's loop"
    svc.stop()


@pytest.mark.asyncio
async def test_close_with_no_nudge_service_still_closes(tmp_path, monkeypatch) -> None:
    """No service at all (--no-dashboard boot order, nudges never armed)."""
    state = _state_with_slot(tmp_path)
    monkeypatch.setattr(autonudge, "_INSTANCE", None)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.delete(f"/api/chat/slots/{NAME}")
        assert resp.status == 200

    assert NAME not in state._slots


@pytest.mark.asyncio
async def test_failed_persist_gives_the_session_its_clock_back(tmp_path, monkeypatch) -> None:
    """A close that cannot persist must not silently retire the babysit loop."""
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24, max_runtime_secs=3600)
    loop.cycle_count = 3
    loop.created_ts = time.time() - 600
    original_restore = handlers._restore_slot_nudge_loop

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    async def _restore(retired_loop, admission_check) -> None:
        assert slot.is_closing is True, "monitor admission reopened before rollback completed"
        await original_restore(retired_loop, admission_check)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    monkeypatch.setattr(handlers, "_restore_slot_nudge_loop", _restore)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert NAME in state._slots, "the transcript was dropped on a failed close"
    replacement = svc.get_by_slot(NAME)
    assert replacement is not None, "the restored session was left with no clock"
    assert replacement.message == "check the PR"
    assert replacement.idle_secs == 300
    assert state._slots[NAME].is_closing is False
    # Remaining budget, never a fresh one: 3 cycles and 600s are already spent.
    assert replacement.max_cycles == 21
    assert 2900 < replacement.max_runtime_secs <= 3000
    svc.stop()


class _SteppedTime:
    """The ``time`` module with ``time()`` stepped by *step* seconds; every other name is real.

    Bound as a module's own ``time``, never over the stdlib clock, so the event loop and
    every other module keep the real one.
    """

    def __init__(self, step: float) -> None:
        self._step = step

    def time(self) -> float:
        return time.time() + self._step

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.mark.parametrize("step", [0.0, 7200.0], ids=["no_step", "forward_step"])
@pytest.mark.asyncio
async def test_failed_persist_restores_the_budget_the_timer_reads_as_left(
    tmp_path, monkeypatch, step
) -> None:
    """The replacement carries the remaining budget as the timer reads it.

    The loop is 30 minutes into a 3 hour budget. Read as ``time.time() - created_ts``,
    a 2 hour forward step leaves the replacement 30 minutes instead of 2.5 hours.
    """
    from kiro_crew.autonudge_service.model import runtime_budget_exceeded

    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(NAME, "check the PR", idle_secs=300, max_runtime_secs=10800)
    # Every second that really passes from here on comes out of the budget the timer
    # reads as left, so the bound below is taken relative to the span measured here.
    started = time.monotonic()
    loop.created_ts = time.time() - 1800
    runtime_budget_exceeded(loop)  # the timer's read in this process

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    from kiro_crew.autonudge_service import model, mutations

    # The close path's components read ``time`` through the chat_handlers facade.
    for module in (handlers, model, mutations):
        monkeypatch.setattr(module, "time", _SteppedTime(step))
    try:
        resp = await handlers.api_chat_slot_delete(_Req(state, NAME))
        spent = time.monotonic() - started

        assert resp.status == 500
        replacement = svc.get_by_slot(NAME)
        assert replacement is not None, "the restored session was left with no clock"
        # 9000 s are left at the timer's read. The wall step may take none of them; the
        # seconds this test spent may, plus the one the restore's int() can drop.
        left = replacement.max_runtime_secs
        assert 9000 - spent - 1 <= left <= 9000, f"{left} s of the 9000 s left were restored"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_structured_rollback_rechecks_the_restored_slot_generation(
    tmp_path, monkeypatch
) -> None:
    """A superseded close cannot reactivate a monitor for a removed slot."""
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add_monitor(
        slot_key=NAME,
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    await svc.retire_monitor_for_session_close(loop.id, now=120.0)

    await handlers._restore_slot_nudge_loop(loop, lambda: False)

    assert loop.active is False
    assert loop.monitor is not None
    assert loop.monitor.outcome is MonitorOutcome.SESSION_CLOSE
    svc.stop()


@pytest.mark.asyncio
async def test_active_structured_rollback_does_not_enter_legacy_restore(
    tmp_path, monkeypatch
) -> None:
    """A failed retirement write already restores the active typed record."""
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add_monitor(
        slot_key=NAME,
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
        now=100.0,
    )
    legacy_add = AsyncMock()
    structured_restore = AsyncMock()
    monkeypatch.setattr(svc, "add", legacy_add)
    monkeypatch.setattr(svc, "restore_monitor_after_failed_session_close", structured_restore)

    await handlers._restore_slot_nudge_loop(loop, lambda: True)

    legacy_add.assert_not_awaited()
    structured_restore.assert_not_awaited()
    assert svc.get_by_slot(NAME) is loop
    assert loop.monitor is not None
    svc.stop()


@pytest.mark.asyncio
async def test_a_failed_persist_takes_the_app_dismissal_back(tmp_path, monkeypatch) -> None:
    """The notify SUCCEEDED and the persist then failed: the app must be told so.

    The slot table, history file and app store cannot change atomically, so a failed
    persist after the notify needs a compensating undo, in reverse order. Asserts the
    undo fired with this slot; a log alone would leave the worker paused.
    """
    state = _state_with_slot(tmp_path)
    state._slots[NAME]._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24)

    undone: list[str] = []

    async def _told(_app: str, _slot_key: str) -> bool:
        return True

    async def _undo(_app: str, slot_key: str) -> bool:
        undone.append(slot_key)
        return True

    async def _persist(*_a, **_kw) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _told)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_close_undone", _undo)
    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert undone == [NAME], "the app was left believing the tab was dismissed"
    assert NAME in state._slots, "the transcript was dropped on a failed close"
    assert svc.get_by_slot(NAME) is not None, "the restored session was left with no clock"
    svc.stop()


@pytest.mark.asyncio
async def test_an_app_that_cannot_be_told_aborts_the_close(tmp_path, monkeypatch) -> None:
    """A dismissal the app could not record must not remove the tab.

    For a crew the hook pauses the worker; swallowing its failure would leave it live
    behind a closed tab. The close is refused and the tab stays fully operational.
    """
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    slot._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24)
    original_restore = handlers._restore_slot_nudge_loop

    saved: list[bool] = []

    async def _persist(*_a, **kw) -> bool:
        saved.append(bool(kw.get("closed")))
        return True

    async def _hook_fails(_app: str, _slot_key: str) -> bool:
        return False

    async def _restore(retired_loop, admission_check) -> None:
        assert slot.is_closing is True, "monitor admission reopened before rollback completed"
        await original_restore(retired_loop, admission_check)

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook_fails)
    monkeypatch.setattr(handlers, "_restore_slot_nudge_loop", _restore)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500, "the tab was removed even though the app was never told"
    assert json.loads(resp.body)["code"] == "app_close_hook_failed"
    assert NAME in state._slots, "the transcript was dropped on a refused close"
    assert True not in saved, "history was written as closed despite the refusal"
    survivor = svc.get_by_slot(NAME)
    assert survivor is not None, "the refused close left the tab with no clock"
    assert survivor.message == "check the PR"
    svc.stop()


@pytest.mark.asyncio
async def test_app_close_retires_a_loop_armed_during_the_close_hook(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    slot = state._slots[NAME]
    slot._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    async def _hook(_app: str, _slot_key: str) -> bool:
        hook_entered.set()
        await release_hook.wait()
        return True

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await hook_entered.wait()

    armed = await svc.add(
        NAME,
        "check the PR",
        idle_secs=300,
        admission_check=lambda: state._slots.get(NAME) is slot,
    )
    assert svc.get_by_slot(NAME) is armed
    release_hook.set()
    response = await close

    assert response.status == 200
    assert NAME not in state._slots
    assert svc.get_by_slot(NAME) is None
    svc.stop()


@pytest.mark.asyncio
async def test_app_close_waits_for_a_queued_direct_arm(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    state._slots[NAME]._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    async def _hook(_app: str, _slot_key: str) -> bool:
        hook_entered.set()
        await release_hook.wait()
        return True

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await hook_entered.wait()

    await svc._lock.acquire()
    addition = asyncio.create_task(svc.add(NAME, "check the PR", idle_secs=300))
    maintenance_lock = autonudge._maintenance_lock(svc._base_dir)
    try:
        for _ in range(10):
            if maintenance_lock.locked():
                break
            await asyncio.sleep(0)
        assert maintenance_lock.locked()
        assert svc.get_by_slot(NAME) is None

        release_hook.set()
        await asyncio.sleep(0)
        assert not close.done()
        svc._lock.release()
        await addition
        response = await close

        assert response.status == 200
        assert NAME not in state._slots
        assert svc.get_by_slot(NAME) is None
    finally:
        release_hook.set()
        if svc._lock.locked():
            svc._lock.release()
        for task in (addition, close):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        svc.stop()


@pytest.mark.asyncio
async def test_app_close_retires_the_latest_queued_arm_generation(tmp_path, monkeypatch) -> None:
    state = _state_with_slot(tmp_path)
    state._slots[NAME]._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    async def _hook(_app: str, _slot_key: str) -> bool:
        hook_entered.set()
        await release_hook.wait()
        return True

    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await hook_entered.wait()
    first = await svc.add(NAME, "first arm", idle_secs=300)

    await svc._lock.acquire()
    replacement = asyncio.create_task(svc.add(NAME, "replacement arm", idle_secs=300))
    maintenance_lock = autonudge._maintenance_lock(svc._base_dir)
    try:
        for _ in range(10):
            if maintenance_lock.locked():
                break
            await asyncio.sleep(0)
        assert maintenance_lock.locked()
        assert svc.get_by_slot(NAME) is first

        release_hook.set()
        await asyncio.sleep(0)
        assert not close.done()
        svc._lock.release()
        second = await replacement
        assert second.id != first.id
        response = await close

        assert response.status == 200
        assert NAME not in state._slots
        assert svc.get_by_slot(NAME) is None
    finally:
        release_hook.set()
        if svc._lock.locked():
            svc._lock.release()
        for task in (replacement, close):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        svc.stop()


@pytest.mark.asyncio
async def test_a_retirement_that_cannot_persist_aborts_the_close(tmp_path, monkeypatch) -> None:
    """A half-applied retirement must stop the close, not ride along with it.

    ``remove()`` drops the loop from memory before writing the registry, so a failed
    write leaves the loop on disk to rebuild the dismissed session. The close is
    refused and the loop re-armed in memory to match disk (`_restore_slot_nudge_loop`).
    """
    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24)

    def _wedged(*_a, **_kw) -> None:
        raise OSError("no space left on device")

    # The registry WRITE fails, which is the only step that can split memory
    # from disk. Patched on the service instance so the in-memory drop that
    # precedes it still happens — reproducing the split rather than assuming it.
    monkeypatch.setattr(svc, "_write_state", _wedged)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500, "the close reported success despite an armed loop on disk"
    # The dashboard renders `error` verbatim into a localized UI, so the machine
    # -readable `code` is the part a caller can actually branch on.
    assert json.loads(resp.body)["code"] == "nudge_retire_failed"
    assert NAME in state._slots, "the session was dropped even though the close failed"
    survivor = svc.get_by_slot(NAME)
    assert survivor is not None, "memory stayed retired while the disk kept the loop"
    assert survivor.message == "check the PR"
    svc.stop()


@pytest.mark.asyncio
async def test_failed_persist_does_not_revive_a_paused_loop(tmp_path, monkeypatch) -> None:
    """Restoring the clock must not override an explicit stop."""
    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(NAME, "check the PR", idle_secs=300)
    await svc.update(loop.id, active=False)

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert svc.get_by_slot(NAME) is None, "a paused loop was resumed by a failed close"
    svc.stop()


@pytest.mark.asyncio
async def test_failed_persist_gives_the_session_its_banner_back(tmp_path, monkeypatch) -> None:
    """The replacement must carry the configured banner, not a blank one.

    ``add()`` defaults ``banner`` to ``""``, so dropping it fails silently and is
    persisted. The budget is asserted here too, so replaying the loop object with a
    fresh 24 cycles and 3600s fails.
    """
    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(
        NAME,
        "check the PR",
        idle_secs=300,
        max_cycles=24,
        max_runtime_secs=3600,
        banner="watching CI",
    )
    loop.cycle_count = 3
    loop.created_ts = time.time() - 600

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    replacement = svc.get_by_slot(NAME)
    assert replacement is not None, "the restored session was left with no clock"
    assert replacement.banner == "watching CI", "the restore discarded the configured banner"
    # NEGATIVE CONTROL: the remaining budget is still subtracted, exactly as the
    # sibling test pins. 3 cycles and 600s are already spent.
    assert replacement.max_cycles == 21, "threading the banner regressed the cycle budget"
    assert 2900 < replacement.max_runtime_secs <= 3000, "the runtime budget was reset"
    svc.stop()


@pytest.mark.asyncio
async def test_a_banner_loop_whose_cycles_are_spent_is_still_not_restored(
    tmp_path, monkeypatch
) -> None:
    """NEGATIVE CONTROL: a banner must not buy a terminal loop another life.

    A spent cycle cap or wall-clock budget still drops the loop, so a failed close
    grants no unauthorized cycles.
    """
    state = _state_with_slot(tmp_path)
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=4, banner="watching CI")
    loop.cycle_count = 4

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert svc.get_by_slot(NAME) is None, "a spent loop was revived to preserve its banner"
    svc.stop()


@pytest.mark.asyncio
async def test_a_deliberate_close_emits_a_removed_crew_log_edge(tmp_path, monkeypatch) -> None:
    """A deliberate close writes ``session/closed`` with ``END_REASON_REMOVED``.

    So ``session_status`` reads ``closed``, not ``lost``. The sid comes from
    ``mapped_sid``, which survives an idle-expiry ``reset``. ``SessionManager.remove``
    stays silent (``test_session_service_boundaries``).
    """
    from unittest.mock import patch

    state = _state_with_slot(tmp_path)
    state.sessions.mapped_sid = lambda _key: "sid-finished-worker"

    # The emit is awaited through ``awaiting_commit``; the stub settles it True so
    # the close proceeds. (The commit-before-wake ORDER is pinned separately below.)
    def _emit(session_id, reason, *, on_settled=None):
        if on_settled is not None:
            on_settled(True)

    with patch("kiro_crew.crew_log.emit.on_session_closed", side_effect=_emit) as on_closed:
        resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    from kiro_crew.metrics.sessions import END_REASON_REMOVED

    on_closed.assert_called_once()
    args, kwargs = on_closed.call_args
    assert args == ("sid-finished-worker", END_REASON_REMOVED)
    assert "on_settled" in kwargs  # awaited, not fire-and-forget


@pytest.mark.asyncio
async def test_a_deliberate_close_with_no_mapped_sid_emits_nothing(tmp_path, monkeypatch) -> None:
    """A slot the session map has no sid for (never ran a turn, or its mapping was
    already cleared) writes no close edge rather than a ``removed`` edge for an empty
    sid -- the emit is a no-op on a blank sid, so the close still succeeds and the
    gone-row split simply has no close to read (it defaults to ``lost``)."""
    from unittest.mock import patch

    state = _state_with_slot(tmp_path)
    state.sessions.mapped_sid = lambda _key: ""

    with patch("kiro_crew.crew_log.emit.on_session_closed") as on_closed:
        resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    on_closed.assert_not_called()


@pytest.mark.asyncio
async def test_a_deliberate_close_commits_the_removed_edge_before_waking_the_conductor(
    tmp_path, monkeypatch
) -> None:
    """The close blocks on the ``removed`` edge's commit via ``awaiting_commit``.

    Otherwise the woken conductor's fold reads the earlier ``reset`` as ``lost`` and
    re-dispatches a finished worker. ``on_settled`` must be passed and settled.
    """
    from unittest.mock import patch

    state = _state_with_slot(tmp_path)
    state.sessions.mapped_sid = lambda _key: "sid-finished-worker"

    settled: list[bool] = []

    def _emit(session_id, reason, *, on_settled=None):
        # A real committed write settles on a later tick; emulate that so a caller
        # that did not AWAIT the commit would return before the settle lands.
        assert on_settled is not None, "the removed edge must be awaited, not fire-and-forget"
        asyncio.get_running_loop().call_soon(on_settled, True)

    def _record_settle(wrote):
        settled.append(wrote)

    orig_awaiting = _emit_module.awaiting_commit

    async def _traced_awaiting(emit_one, *, what, timeout=5.0):
        result = await orig_awaiting(emit_one, what=what, timeout=timeout)
        _record_settle(result)
        return result

    with patch("kiro_crew.crew_log.emit.on_session_closed", side_effect=_emit):
        with patch("kiro_crew.crew_log.emit.awaiting_commit", side_effect=_traced_awaiting):
            resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    # The close awaited the commit and saw it settle True BEFORE returning -- so a
    # conductor woken after this point reads the edge, never the stale ``reset``.
    assert settled == [True], settled


@pytest.mark.asyncio
async def test_a_deliberate_close_still_succeeds_when_the_removed_edge_write_is_dropped(
    tmp_path, monkeypatch
) -> None:
    """Fail-safe direction: if ``awaiting_commit`` reports the edge was dropped (or
    timed out), the close still completes (slot removed, 200). The gone row then reads
    ``lost`` -- a transient redundant re-dispatch, never a lost worker -- which is the
    safe way to be wrong, so a drop must not block the close.
    """
    from unittest.mock import patch

    state = _state_with_slot(tmp_path)
    state.sessions.mapped_sid = lambda _key: "sid-finished-worker"

    def _emit(session_id, reason, *, on_settled=None):
        if on_settled is not None:
            on_settled(False)  # writer dropped it

    with patch("kiro_crew.crew_log.emit.on_session_closed", side_effect=_emit):
        resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert (
        state.get_slot(NAME) is None
    ), "the close must still remove the slot when the edge is dropped"


@pytest.mark.asyncio
async def test_close_after_drain_before_gateway_unpublish_aborts_with_active_row_intact(
    tmp_path, monkeypatch
) -> None:
    """A teardown-window close cannot mistake the active loop for no loop."""
    _LOST_RUN_SECS = 60.0

    async def bounded(awaitable, what: str):
        try:
            return await asyncio.wait_for(awaitable, timeout=_LOST_RUN_SECS)
        except asyncio.TimeoutError:
            pytest.fail(f"{what} did not finish within the {_LOST_RUN_SECS:.0f}s lost-run ceiling")

    state = _state_with_slot(tmp_path)
    slot = state.get_slot(NAME)
    assert slot is not None
    svc = await _service(tmp_path, monkeypatch)
    try:
        loop = await bounded(
            svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24),
            "the teardown-window loop arm",
        )
        await bounded(svc.shutdown(), "the AutoNudge durability drain")
        assert (
            autonudge.get_instance() is svc
        ), "shutdown withdrew the singleton while the dashboard can still close slots"

        response = await bounded(
            handlers.api_chat_slot_delete(_Req(state, NAME)),
            "the teardown-window slot close",
        )

        assert response.status == 500
        assert json.loads(response.body)["code"] == "nudge_retire_failed"
        assert state.get_slot(NAME) is slot, "the refused close persisted the slot as closed"
        assert slot.is_closing is False
        live = svc.get_by_slot(NAME)
        assert live is loop and live.active, "the refusal lost the active in-memory loop"
        metadata = await bounded(
            asyncio.to_thread(
                state.conversation_log.get_metadata,
                f"dashboard:{NAME}",
            ),
            "the open transcript metadata read",
        )
        assert not metadata.get(
            "closed"
        ), "the refused close wrote a closed transcript beside an active loop"
        restored = AutoNudgeService(base_dir=tmp_path)
        await bounded(asyncio.to_thread(restored._load), "the durable loop-row read")
        persisted = restored.get_by_slot(NAME)
        assert (
            persisted is not None and persisted.active
        ), "the shutdown refusal did not preserve the durable active loop row"
    finally:
        svc.stop()
