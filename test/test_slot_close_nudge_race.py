"""Close-vs-expiry race on the dashboard slot-delete handler.

The defect these pin: ``api_chat_slot_delete`` retired the slot's auto-nudge
loop only AFTER it had persisted the closure and torn down the session. Both of
those are awaits, and the slot is already out of ``state._slots`` by then — so a
loop whose idle timer expired inside one of them found no live slot, took the
fire path's rehydrate branch (``adopt_closed=True``) and restored the transcript
the persist had just marked closed. The session the user dismissed came back,
carrying a transcript that outlived its own closure.

Retiring the loop before the first await also closes a second door: cancelling
``slot.task`` runs ``_run_chat``'s finally, which re-arms the timer through
``notify_turn_complete``. Disarming without removing is not enough.

The mirror bug matters as much: the close can still FAIL (the history persist
raises, and the handler restores the slot and answers 500). A loop retired for a
close that never happened would leave an unattended worker running with no clock
at all, so the failure path puts a replacement back — carrying the REMAINING
budget, never a fresh one.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import autonudge
from kiro_crew.apps.builtins.issue_radar.backend import crew_runtime
from kiro_crew.autonudge import AutoNudgeService, NudgeAdmissionRefused
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

    async def _persist(*_a, **_kw) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

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
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=15)

    order: list[str] = []
    svc.subscribe(lambda event, _lp: order.append(f"loop:{event}"))

    async def _persist(*_a, **_kw) -> None:
        order.append("persist")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    state.sessions.remove = AsyncMock(side_effect=lambda *_a: order.append("session_teardown"))

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 200
    assert order == ["loop:removed", "persist", "session_teardown"], (
        "the loop must be gone before the closure is persisted, not after"
    )
    svc.stop()


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
    assert survivor is not None and survivor.id == other.id, (
        "closing one tab retired another tab's loop"
    )
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
    svc = await _service(tmp_path, monkeypatch)
    loop = await svc.add(
        NAME, "check the PR", idle_secs=300, max_cycles=24, max_runtime_secs=3600
    )
    loop.cycle_count = 3
    loop.created_ts = time.time() - 600

    async def _persist(*_a, **_kw) -> None:
        raise RuntimeError("disk wedged")

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)

    resp = await handlers.api_chat_slot_delete(_Req(state, NAME))

    assert resp.status == 500
    assert NAME in state._slots, "the transcript was dropped on a failed close"
    replacement = svc.get_by_slot(NAME)
    assert replacement is not None, "the restored session was left with no clock"
    assert replacement.message == "check the PR"
    assert replacement.idle_secs == 300
    # Remaining budget, never a fresh one: 3 cycles and 600s are already spent.
    assert replacement.max_cycles == 21
    assert 2900 < replacement.max_runtime_secs <= 3000
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

    REGRESSION: the close mutates three independent stores — the slot table, the
    history file, and whatever the app writes — and no ordering makes that atomic.
    With the notify last, a failed persist left a live worker behind a dismissed
    tab. Moving it first fixed that and created the mirror: the crew was durably
    PAUSED, then the persist failed, and the handler restored the tab and returned
    500 while the worker stayed silently disabled. Only a compensating call closes
    both, unwound in reverse order of commitment.

    Asserts the undo actually fired with this slot, alongside the other two
    restores — a fix that only logged would leave the same disabled worker.
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

    REGRESSION: the slot-close hook's failure was logged and swallowed, and it
    fired AFTER the slot was popped and history was written. For a crew that hook
    is the write that PAUSES the worker, so a failure left the crew live and
    auto-approved behind a tab the user believed was gone — and its watchdog then
    relaunched that tab, the exact resurrection the hook exists to prevent.

    Asserts the close is refused, the transcript survives, history was never
    written as closed, and the nudge loop is armed again — the tab is fully
    operational, which is a state the user can see and retry.
    """
    state = _state_with_slot(tmp_path)
    state._slots[NAME]._app = "issue-radar"
    svc = await _service(tmp_path, monkeypatch)
    await svc.add(NAME, "check the PR", idle_secs=300, max_cycles=24)

    saved: list[bool] = []

    async def _persist(*_a, **kw) -> None:
        saved.append(bool(kw.get("closed")))

    async def _hook_fails(_app: str, _slot_key: str) -> bool:
        return False

    monkeypatch.setattr(handlers, "save_slot_off_loop", _persist)
    monkeypatch.setattr("kiro_crew.apps.teardown.notify_slot_closed", _hook_fails)

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
async def test_app_close_retires_the_latest_queued_arm_generation(
    tmp_path, monkeypatch
) -> None:
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
async def test_issue_radar_arm_queued_behind_final_retirement_is_refused(
    tmp_path, monkeypatch
) -> None:
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
    monkeypatch.setattr(
        crew_runtime, "ensure_crew_session", AsyncMock(return_value=slot)
    )
    monkeypatch.setattr(
        crew_runtime,
        "compose_turn_prompt_async",
        AsyncMock(return_value="replacement arm"),
    )
    write_entered = threading.Event()
    release_write = threading.Event()
    original_write = svc._write_state

    def _stall_empty_write(payload: dict[str, object]) -> None:
        if not payload["loops"]:
            write_entered.set()
            assert release_write.wait(timeout=5)
        original_write(payload)

    monkeypatch.setattr(svc, "_write_state", _stall_empty_write)
    close = asyncio.create_task(handlers.api_chat_slot_delete(_Req(state, NAME)))
    await hook_entered.wait()
    await svc.add(NAME, "mid-hook arm", idle_secs=300)
    release_hook.set()
    assert await asyncio.to_thread(write_entered.wait, 5)
    launch = asyncio.create_task(
        crew_runtime.launch_crew(
            state,
            "owner",
            "repo",
            {"id": "crew-1"},
            tmp_path,
        )
    )
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if svc._inflight_adds:
                break
        assert svc._inflight_adds
        assert not launch.done()
        release_write.set()
        response = await close
        launch_result = await asyncio.gather(launch, return_exceptions=True)

        assert response.status == 200
        assert NAME not in state._slots
        assert isinstance(launch_result[0], NudgeAdmissionRefused)
        assert svc.get_by_slot(NAME) is None
    finally:
        release_hook.set()
        release_write.set()
        for task in (launch, close):
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        svc.stop()


@pytest.mark.asyncio
async def test_a_retirement_that_cannot_persist_aborts_the_close(tmp_path, monkeypatch) -> None:
    """A half-applied retirement must stop the close, not ride along with it.

    REGRESSION: ``remove()`` drops the loop from memory and only THEN writes the
    registry, so a write that raises left memory retired while the disk still
    listed the loop — and the failure was logged and swallowed. The close then
    persisted the slot as closed, and the next start read the surviving record
    back; because the fire path answers a missing slot with ``adopt_closed=True``,
    the loop rebuilt the dismissed session and ran an unattended turn in it.

    Asserts BOTH halves of the repair: the close is refused, and the loop is
    armed again in memory so it agrees with the disk that still holds it. A fix
    that only re-raised would leave the tab open with no clock — an abandoned
    babysit, which is the defect `_restore_slot_nudge_loop` exists to prevent.
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
