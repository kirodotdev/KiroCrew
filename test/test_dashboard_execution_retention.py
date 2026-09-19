"""Canonical retention survives stale slots and ends with the final live consumer."""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import close_slot
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.execution_context import (
    ExecutionContext,
    MemoryStoreRef,
    bind_session_execution,
    read_session_execution,
)
from kiro_crew.history import ConversationLog


def _state(tmp_path):
    state = _make_state(tmp_path)
    state.conversation_log = ConversationLog()
    state.sessions.get_provider.return_value = None
    state.sessions.remove = AsyncMock()
    slot = state.get_or_create_slot("retention")
    return state, slot, slot_history_key(slot)


def _execution(mode):
    return ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew", mode)


@pytest.mark.parametrize("mode", ["incognito", "temporary"])
@pytest.mark.parametrize("flags", [{}, {"force": True}, {"rows_only": True}, {"rewrite": True}])
def test_canonical_mode_blocks_stale_slot_rows_and_queue(tmp_path, mode, flags):
    state, slot, key = _state(tmp_path)
    bind_session_execution(key, _execution(mode))
    assert slot.memory_mode == "persistent"
    slot.append("user", "restricted row sentinel")
    slot._queue.append({"id": "q1", "content": "restricted queue sentinel", "kind": ""})
    assert len(slot.durable_queue_entries()) == 1
    assert _save_slot_to_history(state, slot, **flags)
    assert not state.conversation_log.has_log(key)
    assert len(slot._queue) == 1


def test_mode_tightening_at_commit_blocks_prepared_body(tmp_path, monkeypatch):
    state, slot, key = _state(tmp_path)
    execution = _execution("persistent")
    bind_session_execution(key, execution)
    slot.append("user", "late restricted row sentinel")
    original = state.conversation_log._locked

    @contextmanager
    def tighten_before_lock(requested):
        bind_session_execution(key, execution.with_mode("temporary"), replace_existing=True)
        with original(requested):
            yield

    monkeypatch.setattr(state.conversation_log, "_locked", tighten_before_lock)
    assert _save_slot_to_history(state, slot, force=True)
    assert "late restricted row sentinel" not in state.conversation_log._path(key).read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_close_releases_only_own_live_execution(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state, slot, key = _state(tmp_path)
    slot.memory_mode = mode
    execution = _execution(mode)
    bind_session_execution(key, execution)
    bind_session_execution("dashboard:retained-child", execution)
    await close_slot(state, slot, slot.key)
    assert read_session_execution(key) is None
    assert read_session_execution("dashboard:retained-child") == execution
    replacement = state.get_or_create_slot(slot.key)
    assert replacement.memory_mode == "persistent"
    bind_session_execution(key, _execution("persistent"))
    assert read_session_execution(key).memory_mode == "persistent"


@pytest.mark.asyncio
async def test_close_keeps_identity_until_cancelled_consumer_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state, slot, key = _state(tmp_path)
    slot.memory_mode = "temporary"
    execution = _execution("temporary")
    bind_session_execution(key, execution)
    entered, release = asyncio.Event(), asyncio.Event()

    async def consumer():
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    slot.task = asyncio.create_task(consumer())
    await entered.wait()
    try:
        await close_slot(state, slot, slot.key)
        assert read_session_execution(key) == execution
        release.set()
        await slot.task
        await asyncio.sleep(0)
        assert read_session_execution(key) is None
    finally:
        release.set()
        await asyncio.gather(slot.task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_idle_cleanup_releases_closed_restricted_identity(tmp_path, monkeypatch, mode):
    from test_slot_close_recreation_race import _Req

    from kiro_crew.dashboard.chat_handlers import api_chat_slots_cleanup

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state, slot, key = _state(tmp_path)
    slot.memory_mode = mode
    slot.created_at = "2020-01-01T00:00:00+00:00"
    bind_session_execution(key, _execution(mode))
    response = await api_chat_slots_cleanup(_Req(state, slot.key))
    assert response.status == 200
    assert state.get_slot(slot.key) is None
    assert read_session_execution(key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_late_queue_flush_keeps_closed_slot_restricted(tmp_path, monkeypatch, mode):
    from kiro_crew.dashboard.chat_delivery import start_queue_persist

    monkeypatch.setattr("kiro_crew.autonudge._INSTANCE", None)
    state, slot, key = _state(tmp_path)
    bind_session_execution(key, _execution(mode))
    assert slot.memory_mode == "persistent"
    slot._queue.append({"id": "q1", "content": "closed restricted queue sentinel", "kind": ""})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = state.flush_slot_now

    def delayed_flush(target):
        entered.set()
        try:
            assert release.wait(5)
            original(target)
        finally:
            finished.set()

    monkeypatch.setattr(state, "flush_slot_now", delayed_flush)
    start_queue_persist(state, slot)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        await close_slot(state, slot, slot.key)
        assert read_session_execution(key) is None
        assert slot.memory_mode == mode
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0)
        assert not state.conversation_log.has_log(key)
        assert len(slot.durable_queue_entries()) == 1
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 5)
