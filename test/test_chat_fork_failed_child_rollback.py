"""A fork child that fails before its first frame leaves no identity behind.

``fork_slot`` mints the child, binds the parent's memory identity to it, then
shapes it and runs the caller's ``stamp``. The bind publishes a live execution
carrier, a restricted child adds its key to ``state._restricted_keys``, and a
persistent child gets a transcript record holding that identity. When the shape
or the stamp raises, the fork answers with an error and never reveals the
child's key, so anything left under that key is an orphan: hidden memory
routing that history and cleanup code can still see. These tests force each
failure and require that no slots frame, live slot, execution, restricted key
or transcript record remains for the child.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import execution_context as execution
from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.history import ConversationLog

PARENT_FOLDER = "fold0000par1"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch):
    patch = _floor_monkeypatch
    patch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    patch.setattr(chat_fork, "sel", lambda: MagicMock())
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


async def _seeded_state(tmp_path, memory_mode: str):
    state = _make_state(tmp_path / "sessions")
    # The bind writes through a default ``ConversationLog``; read the same one.
    state.conversation_log = ConversationLog()
    state._folders.append({"id": PARENT_FOLDER, "name": "Parent", "parent_id": ""})
    parent = state.get_or_create_slot("parent")
    parent.memory_mode = memory_mode
    parent.append("user", "hello", "msg msg-u")
    parent.append("assistant", "hi", "msg msg-a")
    parent.drain()
    await save_slot_off_loop(state, parent)
    parent.folder_id = PARENT_FOLDER
    parent.title = "Design notes"
    parent._titled = True
    return state, parent


def _record_frames(state) -> list[list[dict]]:
    seen: list[list[dict]] = []

    def capture(payload):
        if payload.get("_type") == "slots":
            seen.append(json.loads(payload["slots"]))

    state._broadcast = capture  # type: ignore[method-assign]
    # Seeding pushed; reopen the window so the mint's push is sent, not absorbed.
    state._slots_broadcast_last = float("-inf")
    return seen


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("failing", ["shape", "stamp"])
async def test_a_child_that_fails_after_its_bind_leaves_nothing_behind(
    tmp_path, monkeypatch, memory_mode, failing
) -> None:
    state, parent = await _seeded_state(tmp_path, memory_mode)
    slots_before = set(state._slots)
    restricted_before = set(state._restricted_keys)
    minted: list = []
    bound: list[str] = []
    real_bind = chat_fork._bind_fork_execution

    def recording_bind(source, child_key, inherited):
        real_bind(source, child_key, inherited)
        bound.append(child_key)

    def failing_shape(new_slot, *_args, **_kwargs):
        minted.append(new_slot)
        raise RuntimeError("shape failed")

    def failing_stamp_for(_source_slot):
        def stamp(child):
            minted.append(child)
            raise RuntimeError("stamp failed")

        return stamp

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", recording_bind)
    if failing == "shape":
        monkeypatch.setattr(chat_fork, "_shape_fork_child", failing_shape)
    else:
        monkeypatch.setattr(chat_fork, "_inherit_pin", failing_stamp_for)
    frames = _record_frames(state)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{parent.key}/fork", json={})
        assert resp.status == 500

    assert len(minted) == 1
    child = minted[0]
    child_key = effective_session_key(child)
    # Positive control: the bind really ran, so there was something to undo.
    assert bound == [child_key]
    announced = [row for frame in frames for row in frame if row["key"] == child.key]
    assert announced == [], "a slots frame announced a child that failed before it was answered"
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert execution.read_session_execution(child_key) is None
    record_left = ConversationLog().has_log(child_key)
    assert not record_left, "the bind's transcript record survived the failed fork"


def _assert_child_withdrawn(state, child, slots_before, restricted_before) -> None:
    child_key = effective_session_key(child)
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert execution.read_session_execution(child_key) is None
    assert execution.read_live_session_execution(child_key) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("failing", ["save", "source_deleted"])
async def test_a_child_that_fails_after_it_was_announced_leaves_nothing_behind(
    tmp_path, monkeypatch, memory_mode, failing
) -> None:
    """The destination save and the source-deleted check run after the mint
    block announced the child. Their rollbacks take the same withdrawal as the
    mint block's, and push the slot list again to take the row back."""
    state, parent = await _seeded_state(tmp_path, memory_mode)
    slots_before = set(state._slots)
    restricted_before = set(state._restricted_keys)
    bound: list[str] = []
    saved: list = []
    real_bind = chat_fork._bind_fork_execution
    real_save = chat_fork.save_slot_off_loop

    def recording_bind(source, child_key, inherited):
        real_bind(source, child_key, inherited)
        bound.append(child_key)

    async def child_save(st, target, *args, **kwargs):
        if target is parent:
            return await real_save(st, target, *args, **kwargs)
        saved.append(target)
        # The bind ran and the child is live and restricted, so the rollback
        # has a carrier and a restricted key to withdraw.
        assert execution.read_session_execution(effective_session_key(target)) is not None
        # Reopen the coalescer window so the withdrawal's push is sent at once.
        st._slots_broadcast_last = float("-inf")
        if failing == "save":
            raise OSError("disk full")
        result = await real_save(st, target, *args, **kwargs)
        assert st.conversation_log.delete_session(slot_history_key(parent)) is True
        return result

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", recording_bind)
    monkeypatch.setattr(chat_fork, "save_slot_off_loop", child_save)
    frames = _record_frames(state)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{parent.key}/fork", json={})
        if failing == "save":
            assert resp.status == 500
        else:
            assert resp.status == 409
            assert (await resp.json())["code"] == "fork_source_deleted"

    assert len(saved) == 1
    child = saved[0]
    child_key = effective_session_key(child)
    assert bound == [child_key]
    assert frames, "the mint block's flush sent no frame"
    assert all(row["key"] != child.key for row in frames[-1]), "the withdrawal was not pushed"
    _assert_child_withdrawn(state, child, slots_before, restricted_before)
    assert not ConversationLog().has_log(child_key), "the child's record survived"


def _failing_record_read(child_holder: list[str]):
    real_has_messages = ConversationLog.has_messages

    def has_messages(self, key):
        if child_holder and key == child_holder[0]:
            raise OSError("record unreadable")
        return real_has_messages(self, key)

    return has_messages


@pytest.mark.asyncio
async def test_a_raising_cleanup_still_answers_store_unavailable(
    tmp_path, monkeypatch, caplog
) -> None:
    """A second IO fault inside the bind rollback is logged, and the client
    still gets the structured store-unavailable answer, not an unhandled 500.
    A persistent child, so the bind wrote a record the cleanup has to read."""
    state, parent = await _seeded_state(tmp_path, "persistent")
    slots_before = set(state._slots)
    restricted_before = set(state._restricted_keys)
    bound: list[str] = []
    real_bind = chat_fork._bind_fork_execution

    def failing_bind(source, child_key, inherited):
        real_bind(source, child_key, inherited)
        bound.append(child_key)
        raise OSError("bind write failed")

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", failing_bind)
    monkeypatch.setattr(ConversationLog, "has_messages", _failing_record_read(bound))
    caplog.set_level(logging.WARNING, logger=chat_fork.logger.name)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{parent.key}/fork", json={})
        payload = await resp.json()

    assert resp.status == 503, payload
    assert payload["code"] == "store_unavailable"
    assert len(bound) == 1
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert execution.read_live_session_execution(bound[0]) is None
    assert any(
        "removing the failed fork child's record" in r.getMessage() for r in caplog.records
    ), "the cleanup failure was not logged"


@pytest.mark.asyncio
async def test_a_raising_cleanup_still_records_the_failed_fork(tmp_path, monkeypatch) -> None:
    """A cleanup that raises during the shape rollback does not skip the audit."""
    state, parent = await _seeded_state(tmp_path, "persistent")
    bound: list[str] = []
    real_bind = chat_fork._bind_fork_execution
    audit = MagicMock()

    def recording_bind(source, child_key, inherited):
        real_bind(source, child_key, inherited)
        bound.append(child_key)

    def failing_shape(*_args, **_kwargs):
        raise RuntimeError("shape failed")

    monkeypatch.setattr(chat_fork, "_bind_fork_execution", recording_bind)
    monkeypatch.setattr(chat_fork, "_shape_fork_child", failing_shape)
    monkeypatch.setattr(ConversationLog, "has_messages", _failing_record_read(bound))
    monkeypatch.setattr(chat_fork, "sel", lambda: audit)

    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{parent.key}/fork", json={})
        assert resp.status == 500

    assert len(bound) == 1
    outcomes = [c.kwargs.get("outcome") for c in audit.log_api_access.call_args_list]
    assert outcomes == ["error"], outcomes
