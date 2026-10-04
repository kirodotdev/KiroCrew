"""Eager config loading stays off-loop and preserves current allocation guards."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, ResolvedBindings
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import DashboardState, _ChatSlot

_GUARD_SECS = 30.0
_REAL_LOAD = KiroCrewConfig.load


@pytest.fixture
def eager(monkeypatch):
    slot = _ChatSlot("config-worker")
    state = MagicMock(spec=DashboardState)
    state.get_slot.return_value = slot
    state.sessions = MagicMock()
    cfg = KiroCrewConfig(agents={"default": KiroCrewAgentConfig()})
    bindings = ResolvedBindings(
        workspace_dir=Path("workspace"),
        effective_memory_config={},
        kiro_agent="kirocrew",
        model="",
        resolved_alias="default",
        requested_resolved=True,
        memory_store_name="default",
        selection_kind="member",
    )
    spawn = AsyncMock()
    monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(chat_runner, "_consume_pending_reset", AsyncMock(return_value=False))
    monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(chat_runner, "resolve_session_agent_bindings", lambda *args: bindings)
    monkeypatch.setattr(chat_runner, "_require_session_memory_assignment", lambda *args: None)
    monkeypatch.setattr(chat_runner, "_default_session_model", lambda *args: "")
    monkeypatch.setattr(chat_runner, "record_agent_selection", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_runner, "_prewarm_allowance", lambda: 1)
    monkeypatch.setattr(chat_runner, "_pressure_hold_blocks_prewarm", lambda state: False)
    monkeypatch.setattr(chat_runner, "_admit_prefetch", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_runner, "_spawn_admitted_prefetch", spawn)
    return state, slot, cfg, spawn


@pytest.mark.asyncio
async def test_config_load_runs_off_loop_and_normal_spawn_survives(eager, monkeypatch):
    state, slot, cfg, spawn = eager
    loop_thread = threading.get_ident()
    load_threads = []

    def load():
        load_threads.append(threading.get_ident())
        return cfg

    monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", load)
    await chat_runner._eager_spawn(state, slot)
    assert len(load_threads) == 1
    assert load_threads[0] != loop_thread
    spawn.assert_awaited_once()
    assert spawn.await_args.args[3] == "dashboard:config-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["replace", "agent", "model", "project", "memory_store", "running"]
)
async def test_superseded_load_never_reaches_allocation(eager, monkeypatch, mutation):
    state, slot, cfg, spawn = eager
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def load():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(_GUARD_SECS)
        return cfg

    monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", load)
    task = asyncio.create_task(chat_runner._eager_spawn(state, slot))
    running_turn = None
    try:
        await asyncio.wait_for(entered.wait(), _GUARD_SECS)
        if mutation == "replace":
            state.get_slot.return_value = _ChatSlot(slot.key)
        elif mutation == "running":
            running_turn = asyncio.create_task(asyncio.Event().wait())
            slot.task = running_turn
        else:
            setattr(slot, mutation, "changed")
        release.set()
        await asyncio.wait_for(task, _GUARD_SECS)
        spawn.assert_not_awaited()
        state.sessions.get_or_create.assert_not_called()
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if running_turn is not None:
            running_turn.cancel()
            await asyncio.gather(running_turn, return_exceptions=True)


@pytest.mark.asyncio
async def test_first_worker_load_migration_preserves_concurrent_config_write(eager, monkeypatch):
    state, slot, cfg, spawn = eager
    config_path = loader.config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"workspaces": {"default": {"dir": "workspace"}}}), encoding="utf-8"
    )
    loader._invalidate_config_cache()
    real_persist = loader._persist_config_migration
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    load_threads = []

    def parked_persist(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(_GUARD_SECS)
        return real_persist(*args, **kwargs)

    def real_load():
        load_threads.append(threading.get_ident())
        return _REAL_LOAD()

    def concurrent_update():
        def mutate(current):
            current.setdefault("session", {})["autocompact_pct"] = 42.0
            return current

        loader.update_config_locked(config_path, mutate=mutate)

    monkeypatch.setattr(loader, "_persist_config_migration", parked_persist)
    monkeypatch.setattr(chat_runner.KiroCrewConfig, "load", real_load)
    task = asyncio.create_task(chat_runner._eager_spawn(state, slot))
    try:
        await asyncio.wait_for(entered.wait(), _GUARD_SECS)
        await asyncio.to_thread(concurrent_update)
        release.set()
        await asyncio.wait_for(task, _GUARD_SECS)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    stored = json.loads(config_path.read_text(encoding="utf-8"))
    assert stored["session"]["autocompact_pct"] == 42.0
    assert "default" in stored["agents"]
    assert len(load_threads) == 1
    assert load_threads[0] != threading.get_ident()
    spawn.assert_awaited_once()
