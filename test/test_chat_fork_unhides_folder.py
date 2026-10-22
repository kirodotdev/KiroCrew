"""A fork filed into a hidden folder un-hides that folder.

A fork inherits its parent's folder. When that folder is hidden, the child is
filed somewhere the sidebar does not draw, so it disappears as soon as the user
moves away from it. Create and resume follow Model-B semantics: a session that
lands in a hidden folder un-hides it (``_unhide_folder``). The fork core does
the same for the dashboard Fork button, and for the agent ``session_fork`` tool
only when the agent names the folder, as agent ``session_create`` does. It runs
only once the fork has been acknowledged: a fork refused at its acknowledgment
boundary leaves the user's choice alone.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import chat_fork, create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
from kiro_crew.dashboard.chat_utils import slot_history_key

FOLDER_ID = "fold00000001"


@pytest.fixture(autouse=True)
def _isolated(_floor_monkeypatch, tmp_path):
    mp = _floor_monkeypatch
    mp.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    mp.setattr(sc, "session_control_enabled", lambda: True)
    mp.setattr(chat_fork, "sel", lambda: MagicMock())
    mp.setattr(sc, "sel", lambda: MagicMock())
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _seeded(tmp_path, *, folder_hidden: bool | None = True):
    """A settled two-turn parent, filed in a folder that is hidden by default.

    ``folder_hidden=None`` leaves the parent unfiled and adds no folder.
    """
    state = _make_state(tmp_path)
    row = None
    if folder_hidden is not None:
        row = {"id": FOLDER_ID, "name": "Parked", "parent_id": "", "hidden": folder_hidden}
        state._folders.append(row)
    slot = state.get_or_create_slot("parent")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot.drain()
    _save_slot_to_history(state, slot, closed=False)
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    if row is not None:
        slot.folder_id = FOLDER_ID
    return state, slot, row


async def _dashboard_fork(state, payload=None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/parent/fork", json=payload or {})
        return resp.status, await resp.json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, {"at_message_index": 0, "direction": "tail"}],
    ids=["head", "tail"],
)
async def test_a_dashboard_fork_unhides_the_inherited_folder(tmp_path, payload) -> None:
    state, _parent, row = _seeded(tmp_path)

    status, body = await _dashboard_fork(state, payload)

    assert status == 200
    assert state._slots[body["key"]].folder_id == FOLDER_ID
    assert row["hidden"] is False, "a fork filed into a hidden folder must un-hide it"


@pytest.mark.asyncio
async def test_the_pushed_frame_carries_the_visible_folder(tmp_path) -> None:
    """The last slots frame the fork broadcasts already has the folder visible,
    so an open sidebar draws the child without waiting for another push."""
    state, _parent, _row = _seeded(tmp_path)
    frames: list[list[dict]] = []
    real_push = state.push_slots_update

    def _spy(*args, **kwargs):
        frames.append([dict(f) for f in state._folders])
        return real_push(*args, **kwargs)

    state.push_slots_update = _spy

    status, _body = await _dashboard_fork(state)

    assert status == 200
    assert frames, "the fork must push a slots update"
    pushed = next(f for f in frames[-1] if f["id"] == FOLDER_ID)
    assert pushed.get("hidden") is False


@pytest.mark.asyncio
async def test_an_unfiled_fork_touches_no_folder(tmp_path, monkeypatch) -> None:
    state, _parent, _row = _seeded(tmp_path, folder_hidden=None)
    calls: list[str] = []

    async def _never(*_args, **_kwargs):
        calls.append("unhide")
        return True

    monkeypatch.setattr(chat_fork, "_unhide_folder", _never)

    status, body = await _dashboard_fork(state)

    assert status == 200
    assert not state._slots[body["key"]].folder_id
    assert calls == []


@pytest.mark.asyncio
async def test_a_fork_refused_at_acknowledgment_leaves_the_folder_hidden(
    tmp_path, monkeypatch
) -> None:
    """A source deleted during the destination save rolls the child back with a
    409. The un-hide comes after that boundary, so the folder stays hidden."""
    state, _parent, row = _seeded(tmp_path)
    monkeypatch.setattr(chat_fork, "session_was_deleted", lambda *_a, **_k: True)

    status, body = await _dashboard_fork(state)

    assert status == 409
    assert body["code"] == "fork_source_deleted"
    assert row["hidden"] is True, "a refused fork must not un-hide the folder"


@pytest.mark.asyncio
async def test_an_unhide_failure_does_not_fail_a_committed_fork(tmp_path, monkeypatch) -> None:
    """The child is saved and acknowledged before the un-hide runs. A folder-store
    failure then must not report failure for a session that exists, or the
    caller's retry would make a duplicate."""
    state, _parent, row = _seeded(tmp_path)

    async def _boom(*_args, **_kwargs):
        raise OSError("folder store unwritable")

    monkeypatch.setattr(chat_fork, "_unhide_folder", _boom)

    status, body = await _dashboard_fork(state)

    assert status == 200
    assert body["key"] in state._slots
    assert row["hidden"] is True


def _agent_fork(state, caller, **kwargs):
    return asyncio.run(
        sc.fork_session(state, caller_session_key=slot_history_key(caller), **kwargs)
    )


def test_an_agent_fork_leaves_an_inherited_hidden_folder_hidden(tmp_path) -> None:
    """Agent ``session_create`` un-hides only a folder the agent names. An agent
    fork that inherits a folder the user hid matches it: no person acted, so the
    user's hide stands."""
    state, parent, row = _seeded(tmp_path)

    result = _agent_fork(state, parent)

    assert state.get_slot(result["target"]).folder_id == FOLDER_ID
    assert row["hidden"] is True, "an agent fork must not un-hide a folder it only inherited"


def test_an_agent_fork_naming_its_inherited_folder_unhides_it(tmp_path) -> None:
    state, parent, row = _seeded(tmp_path)

    result = _agent_fork(state, parent, folder_id=FOLDER_ID)

    assert state.get_slot(result["target"]).folder_id == FOLDER_ID
    assert row["hidden"] is False


def test_an_agent_fork_unhides_a_named_folder(tmp_path) -> None:
    state, parent, _row = _seeded(tmp_path, folder_hidden=None)
    other = {"id": "fold00000002", "name": "Elsewhere", "parent_id": "", "hidden": True}
    state._folders.append(other)

    result = _agent_fork(state, parent, folder_id="fold00000002")

    assert state.get_slot(result["target"]).folder_id == "fold00000002"
    assert other["hidden"] is False
