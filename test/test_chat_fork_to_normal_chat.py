"""A person's fork of a conductor mode root is a normal chat; the root is left as it was.

``POST /api/chat/slots/{slot}/fork`` on a conductor mode root copies its
transcript into a new top-level chat on the default template, with conductor
mode off. The conductor chat keeps its agent and its workers keep their
``crew_root``; nothing is switched in place, so the conductor mode lock is never
asked. Every other fork keeps the parent's agent, as it always did.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew import crew_mode

_CONDUCTOR = crew_mode.DEFAULT_CREW_MODE_AGENT
_DEFAULT = "kirocrew"


@pytest.fixture(autouse=True)
def _crew_mode_in_memory(_floor_monkeypatch, tmp_path):
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    _floor_monkeypatch.setattr(crew_mode, "_configured", _CONDUCTOR)
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_fork_conductor._default_chat_template", lambda: _DEFAULT
    )
    yield


def _seed(slot) -> None:
    slot.append("user", "plan the release", "msg msg-u")
    slot.append("assistant", "dispatched two workers", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False


def _template(state, key: str, agent: str, **kw):
    slot = state.get_or_create_slot(key, agent=agent, **kw)
    slot.agent_kind = "template"
    return slot


def _conductor_state(tmp_path, *, memory_mode: str = "persistent"):
    state = _make_state(tmp_path)
    root = _template(state, "root", _CONDUCTOR, memory_mode=memory_mode)
    root.crew_mode_agent = _CONDUCTOR
    _seed(root)
    worker = _template(state, "worker", "kirocrew-worker")
    worker._created_by = "root"
    _seed(worker)
    return state


async def _fork(state, key: str, payload: dict | None = None) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{key}/fork", json=payload or {})
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_fork_of_a_conductor_root_is_a_top_level_default_chat(tmp_path) -> None:
    state = _conductor_state(tmp_path)
    assert crew_mode.live_stamps(state._slots)["worker"] == ("root", False)

    status, body = await _fork(state, "root")

    assert status == 200, body
    child = state._slots[body["key"]]
    assert (child.agent, child.agent_kind, child.crew_mode_agent) == (_DEFAULT, "template", "")
    assert [m["content"] for m in child.messages] == ["plan the release", "dispatched two workers"]
    stamps = crew_mode.live_stamps(state._slots)
    # Conductor mode off, top level: no conductor root, no worker of the original.
    assert stamps[child.key] == ("", False)
    # The original stays a conductor root on its own agent, with its worker.
    root = state._slots["root"]
    assert (root.agent, root.agent_kind, root.crew_mode_agent) == (
        _CONDUCTOR,
        "template",
        _CONDUCTOR,
    )
    assert [m["content"] for m in root.messages] == ["plan the release", "dispatched two workers"]
    assert stamps["root"] == ("", True)
    assert stamps["worker"] == ("root", False)


@pytest.mark.asyncio
async def test_a_fork_at_a_message_lands_on_the_default_too(tmp_path) -> None:
    state = _conductor_state(tmp_path)

    status, body = await _fork(state, "root", {"at_message_index": 0})

    assert status == 200, body
    child = state._slots[body["key"]]
    assert child.agent == _DEFAULT
    assert [m["content"] for m in child.messages] == ["plan the release"]


@pytest.mark.asyncio
async def test_a_running_worker_does_not_block_the_fork(tmp_path) -> None:
    state = _conductor_state(tmp_path)
    state._slots["worker"]._turn_admission_reserved = True

    status, body = await _fork(state, "root")

    assert status == 200, body
    assert state._slots[body["key"]].agent == _DEFAULT
    assert state._slots["worker"].running is True
    assert crew_mode.live_stamps(state._slots)["worker"] == ("root", False)


@pytest.mark.asyncio
async def test_the_forked_chat_can_turn_conductor_mode_on(tmp_path, _floor_monkeypatch) -> None:
    """The lock (``crew_mode_locked``) holds only on a conductor mode root, so the
    fork, a plain chat with messages, may switch onto the conductor; it is then a
    root of its own, beside the original."""
    from kiro_crew.dashboard import chat_handlers

    state = _conductor_state(tmp_path)
    status, body = await _fork(state, "root")
    assert status == 200, body
    child = state._slots[body["key"]]

    caller = chat_handlers.SlotAgentSwitchCaller(app="", remote="t", owner=True)
    assert chat_handlers._crew_mode_lock_refusal(child, _CONDUCTOR, "template", caller) is None
    assert crew_mode.stamp_for_switch(_CONDUCTOR, "template") == _CONDUCTOR
    # What the committed switch writes onto the chat.
    child.agent, child.crew_mode_agent = _CONDUCTOR, _CONDUCTOR

    stamps = crew_mode.live_stamps(state._slots)
    assert stamps[child.key] == ("", True)
    assert stamps["root"] == ("", True)
    assert stamps["worker"] == ("root", False)


@pytest.mark.asyncio
async def test_a_worker_fork_keeps_its_agent(tmp_path) -> None:
    state = _conductor_state(tmp_path)

    status, body = await _fork(state, "worker")

    assert status == 200, body
    assert state._slots[body["key"]].agent == "kirocrew-worker"


@pytest.mark.asyncio
async def test_a_plain_fork_keeps_its_agent(tmp_path) -> None:
    state = _conductor_state(tmp_path)
    _seed(_template(state, "plain", "research"))

    status, body = await _fork(state, "plain")

    assert status == 200, body
    assert state._slots[body["key"]].agent == "research"


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["incognito", "temporary"])
async def test_an_ephemeral_conductor_chat_forks_as_before(tmp_path, memory_mode) -> None:
    # An incognito or temporary chat is never a conductor mode root.
    state = _conductor_state(tmp_path, memory_mode=memory_mode)

    status, body = await _fork(state, "root")

    assert status == 200, body
    child = state._slots[body["key"]]
    assert (child.agent, child.memory_mode) == (_CONDUCTOR, memory_mode)


@pytest.mark.asyncio
async def test_a_crewmate_on_a_conductor_name_forks_as_before(tmp_path) -> None:
    state = _conductor_state(tmp_path)
    mate = state.get_or_create_slot("mate", agent=_CONDUCTOR)
    mate.agent_kind = "member"
    _seed(mate)

    status, body = await _fork(state, "mate")

    assert status == 200, body
    assert state._slots[body["key"]].agent == _CONDUCTOR


@pytest.mark.asyncio
async def test_a_conductor_default_keeps_the_fork_on_the_conductor(
    tmp_path, _floor_monkeypatch
) -> None:
    # With a conductor as the default agent there is no normal chat to land on.
    _floor_monkeypatch.setattr(
        "kiro_crew.dashboard.chat_fork_conductor._default_chat_template", lambda: _CONDUCTOR
    )
    state = _conductor_state(tmp_path)

    status, body = await _fork(state, "root")

    assert status == 200, body
    assert state._slots[body["key"]].agent == _CONDUCTOR
