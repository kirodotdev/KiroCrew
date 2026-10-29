"""Admission refuses unsaved sessions and cancels children without blocking the loop."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions, task_record, wait_taskq_open
from test_shared_crew_queued_admission import (  # noqa: F401 -- shared fixtures
    BOB,
    _client,
    clock,
    dash_state,
    installed,
)

from kiro_crew.dashboard import chat_handlers
from kiro_crew.shared_crew import admission_entry as entry
from kiro_crew.shared_crew import host_hooks
from kiro_crew.shared_crew.admission import OperationKind, OperationState
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

BOUND = 30


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_unsaved_session_is_refused_before_admission(
    dash_state, installed, monkeypatch, failure  # noqa: F811 -- imported fixtures
):
    slot = dash_state.get_or_create_slot("unsaved")
    await installed.admit(
        budget_key=BOB.id,
        session_key="dashboard:occupied",
        operation=OperationKind.CHAT_TURN,
        idempotency_key="occupy-capacity",
    )
    if failure == "false":
        monkeypatch.setattr(host_hooks, "ensure_slot_recorded", AsyncMock(return_value=False))
    else:
        monkeypatch.setattr(
            dash_state.conversation_log,
            "update_metadata_if",
            MagicMock(side_effect=OSError("metadata unavailable")),
        )
    run = AsyncMock()
    monkeypatch.setattr(chat_handlers, "_run_chat", run)
    monkeypatch.setattr(chat_handlers, "_maybe_auto_title", AsyncMock())
    async with _client(dash_state, BOB) as client:
        response = await asyncio.wait_for(
            client.post("/api/chat?ws=1", json={"slot": slot.key, "message": "keep me"}), BOUND
        )
        assert response.status == 503
        assert (await response.json())["code"] == "task_store_unavailable"
    assert await installed.list_work(session_key="dashboard:unsaved") == []
    assert installed.capacity(BOB.id).principal.queued == 0
    assert not slot._turn_admission_reserved and not slot._has_reader
    assert not slot.messages and slot.task is None
    run.assert_not_called()


@pytest.mark.asyncio
async def test_queued_child_cancel_keeps_store_off_loop(monkeypatch, close_subagent_managers):
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    manager = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
    await wait_taskq_open(manager, ceiling=BOUND)
    monkeypatch.setattr(manager, "_drain_queue", lambda: None)
    store = manager._admission.taskq_store()
    params = {"task": "queued child", "parent_session_key": "dashboard:parent"}
    await store.run(store.accept_one, task_record("queued-child", params=params))
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    original = store.cancel
    calls = []

    def cancel_row(*args, **kwargs):
        assert threading.get_ident() != loop_thread, "SQLite cancel ran on the event loop"
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(BOUND), "store cancel was not released"
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "cancel", cancel_row)
    tasks = []

    def create_task(coro):
        task = loop.create_task(coro)
        tasks.append(task)
        return task

    work = SimpleNamespace(extra={"execution_id": "queued-child"}, session_key="dashboard:parent")
    entry.subagent_cancel(manager, SimpleNamespace(create_task=create_task))(work)
    entered_task = loop.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait(
            [tasks[0], entered_task],
            timeout=BOUND,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if tasks[0] in done:
            await tasks[0]
        assert entered.is_set(), "cancel did not reach the store"
        assert manager._stopping_parents == {"dashboard:parent": 1}
        assert not tasks[0].done()
        release.set()
        await asyncio.wait_for(tasks[0], BOUND)
        assert calls == ["queued-child"]
        assert (await store.run(store.get, "queued-child")).state == "cancelled"
        assert not manager._stopping_parents
    finally:
        release.set()
        entered_task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, entered_task, return_exceptions=True), BOUND)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "starting", "done", "missing", "queued", "error"])
async def test_child_cancel_preserves_live_and_failure_paths(state):
    info = SimpleNamespace(done=state == "done", queued=False)
    queued = AsyncMock(return_value=state == "queued")
    if state == "error":
        queued.side_effect = OSError("store unavailable")
    manager = SimpleNamespace(
        cancel_queued=queued,
        cancel=AsyncMock(return_value=True),
        _agents={"child": info} if state in {"running", "done"} else {},
        _admission=SimpleNamespace(
            starting_claim_of=lambda _id: object() if state == "starting" else None
        ),
    )
    tasks = []
    loop = SimpleNamespace(create_task=lambda coro: tasks.append(asyncio.create_task(coro)))
    work = SimpleNamespace(extra={"execution_id": "child"}, session_key="dashboard:parent")
    entry.subagent_cancel(manager, loop)(work)
    if state == "error":
        with pytest.raises(OSError, match="store unavailable"):
            await asyncio.wait_for(tasks[0], BOUND)
    else:
        await asyncio.wait_for(tasks[0], BOUND)
    queued.assert_awaited_once_with("child", "dashboard:parent")
    assert manager.cancel.await_count == (1 if state in {"running", "starting"} else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["", "partial error"])
async def test_stopped_child_is_persisted_as_cancelled(installed, error):  # noqa: F811
    info = SimpleNamespace(done=False, user_stopped=True, error=error)
    probe = entry.subagent_probe(SimpleNamespace(_agents={"child": info}))
    assert probe("child") is None
    installed.set_child_probe(probe)
    child = await installed.admit_child(
        parent_session_key="dashboard:parent", fallback_budget=BOB.id
    )
    await child.attach_child("child")
    work = await installed.current_work(child.work.work_id)
    await installed.cancel(work.work_id, expected_revision=work.revision)
    info.done = True
    await installed.tick()
    ended = await installed.current_work(work.work_id)
    assert ended.state is OperationState.CANCELLED
    assert installed.live_work(work.work_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "written,status,expected",
    [
        (True, ({}, True), True),
        (False, ({"title": "t"}, True), True),
        (False, ({}, True), False),
        (False, ({}, False), False),
    ],
    ids=["written", "already-recorded", "skipped-empty", "unreadable"],
)
async def test_slot_recorded_only_when_a_line_exists(tmp_path, written, status, expected):
    log = MagicMock()
    log.update_metadata_if.return_value = written
    log.get_metadata_status.return_value = status
    slot = SimpleNamespace(key=f"rec-{written}-{status[1]}-{bool(status[0])}", created_at=1.0)
    host_hooks._RECORDED.discard(f"dashboard:{slot.key}")
    state = SimpleNamespace(conversation_log=log)
    from unittest.mock import patch

    from kiro_crew.dashboard.slot_persistence import metadata_codec

    with patch.object(metadata_codec, "encode", return_value={}):
        assert await host_hooks.ensure_slot_recorded(state, slot) is expected
        # A failed write is not cached: the next call writes again.
        assert await host_hooks.ensure_slot_recorded(state, slot) is expected
    assert log.update_metadata_if.call_count == (1 if expected else 2)
