# SPDX-License-Identifier: Apache-2.0
"""A crewmate's workers run under its Profile Permissions, however they started.

Slot trust lives in memory only. A worker that got trust solely by copying its
creator's live slot at ``session_create`` would, once brought back after a
gateway restart, have none and ask for every tool until each prompt timed out.
Each case here reads the mode from the crewmate's record (``approval_mode``),
the one setting its Profile tab writes.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_chat_agent_selection import _finished, _turn_state
from test_mate_worker_dispatch import _create, _fresh_create_budget, crewmate  # noqa: F401

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.crewmate_approval import apply_to_live_sessions
from kiro_crew.execution_context import clear_session_execution
from kiro_crew.history import ConversationLog
from kiro_crew.mate_worker import MATE_WORKER_AGENT


def _set_mode(tmp_path, mode: str) -> None:
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"]["approval_mode"] = mode
    path.write_text(json.dumps(config), encoding="utf-8")
    from kiro_crew.config.loader import _invalidate_config_cache

    _invalidate_config_cache()


@pytest.fixture
def home(crewmate, tmp_path):  # noqa: F811
    return (*crewmate, tmp_path)


def test_a_new_worker_takes_the_crewmates_profile_mode_not_its_threads(home):
    state, caller, _execution, tmp_path = home
    _set_mode(tmp_path, "trust_reads")
    caller._trust = True  # the thread was trusted by hand; the Profile says reads only
    worker = state.get_slot(_create(state, caller)["target"])
    assert worker.agent == MATE_WORKER_AGENT
    assert (worker._trust, worker._trust_reads) == (False, True)


@pytest.mark.parametrize(
    "before, after, trusted", [("trust", "normal", False), ("normal", "trust", True)]
)
def test_a_profile_change_during_the_workers_creation_reaches_it(
    home, monkeypatch, before, after, trusted
):
    """The Profile is saved after the create read its config but before the worker
    has a record, so the save's own apply cannot see the worker."""
    from kiro_crew.dashboard.session_control_parts import session_create

    state, caller, _execution, tmp_path = home
    _set_mode(tmp_path, before)
    bind = session_create.bind_session_execution

    def _bind_after_the_profile_save(key, execution, **kwargs):
        if execution.template_id == MATE_WORKER_AGENT:
            _set_mode(tmp_path, after)
        return bind(key, execution, **kwargs)

    monkeypatch.setattr(session_create, "bind_session_execution", _bind_after_the_profile_save)
    worker = state.get_slot(_create(state, caller)["target"])
    assert worker._trust is trusted
    assert not worker._trust_reads


@pytest.mark.parametrize(
    "before, after, trusted", [("trust", "normal", False), ("normal", "trust", True)]
)
@pytest.mark.asyncio
async def test_a_profile_save_applied_during_the_seeds_read_wins(
    home, monkeypatch, before, after, trusted
):
    """The seed's config read is stale: the save and its live apply land between that
    read and the seed. The apply marks the slot seeded, so the stale mode is not used."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard import crewmate_approval
    from kiro_crew.execution_context import read_session_execution

    state, caller, _execution, tmp_path = home
    await asyncio.to_thread(_set_mode, tmp_path, before)
    seed = crewmate_approval.seed_new_worker

    async def _not_yet(*_args, **_kwargs):
        return False

    monkeypatch.setattr(crewmate_approval, "seed_new_worker", _not_yet)
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    assert not (worker._trust or worker._trust_reads or worker._member_approval_seeded)

    stale = await asyncio.to_thread(KiroCrewConfig.load)  # the seed's read
    assert stale.agents["pixel"].approval_mode == before
    await asyncio.to_thread(_set_mode, tmp_path, after)
    assert await apply_to_live_sessions(state, "pixel", after) == 1
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: stale)
    record = await asyncio.to_thread(
        read_session_execution, slot_history_key(worker), required=True
    )
    await seed(state, worker, record)

    assert worker._trust is trusted
    assert not worker._trust_reads


@pytest.mark.asyncio
async def test_a_worker_restored_after_a_restart_keeps_the_crewmates_mode(home, monkeypatch):
    state, caller, _execution, tmp_path = home
    await asyncio.to_thread(_set_mode, tmp_path, "trust")
    created = await asyncio.to_thread(_create, state, caller)
    worker = state.get_slot(created["target"])
    assert worker._trust

    # The gateway restarts: a new process, the same data home, no live carrier.
    clear_session_execution(slot_history_key(worker))
    restored_state = _turn_state(tmp_path, monkeypatch)
    restored_state.conversation_log = ConversationLog()
    restored = restored_state.get_or_create_slot(worker.key)
    restored.agent = worker.agent
    restored.agent_kind = worker.agent_kind
    restored.workspace = worker.workspace
    restored.memory_store = worker.memory_store
    restored.project = worker.project
    assert not restored._trust

    seen = {}
    claim = restored_state.sessions.get_or_create

    async def _claim(*args, **kwargs):
        seen["trust"] = restored._trust
        return await claim(*args, **kwargs)

    restored_state.sessions.get_or_create = _claim
    await _finished(chat_runner._run_chat(restored_state, restored, "resume"), "the resumed turn")
    assert seen.get("trust") is True, "the restored worker claimed its session untrusted"


@pytest.mark.asyncio
async def test_a_profile_change_reaches_a_live_worker(home):
    state, caller, _execution, tmp_path = home
    await asyncio.to_thread(_set_mode, tmp_path, "normal")
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    assert not (worker._trust or worker._trust_reads)
    await asyncio.to_thread(_set_mode, tmp_path, "trust")
    assert await apply_to_live_sessions(state, "pixel", "trust") == 1
    assert worker._trust
    await asyncio.to_thread(_set_mode, tmp_path, "normal")
    assert await apply_to_live_sessions(state, "pixel", "normal") == 1
    assert not (worker._trust or worker._trust_reads)


@pytest.mark.asyncio
async def test_saving_the_profile_permission_reaches_a_live_worker(home, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from test_member_memory_creation_capability import _app

    state, caller, _execution, tmp_path = home
    await asyncio.to_thread(_set_mode, tmp_path, "normal")
    worker = state.get_slot((await asyncio.to_thread(_create, state, caller))["target"])
    assert not worker._trust
    app = _app(monkeypatch)
    app["state"] = state
    async with TestClient(TestServer(app)) as client:
        response = await client.put(
            "/api/agents/pixel", json={"approval_mode": "trust", "expected_approval_mode": "normal"}
        )
        assert response.status == 200, await response.text()
    assert worker._trust


def test_the_crewmates_own_chat_takes_the_mode_at_its_turn_start(home):
    """A thread restored after a restart is seeded before its first turn, not only on open."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.crewmate_approval import seed_for_turn

    state, caller, execution, tmp_path = home
    _set_mode(tmp_path, "trust_reads")
    assert seed_for_turn(state, caller, KiroCrewConfig.load(), execution)
    assert (caller._trust, caller._trust_reads) == (False, True)
