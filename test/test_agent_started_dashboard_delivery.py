from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.dashboard.chat_turn.agent_started import wire_agent_started_turn


def _setup_slot_and_client(backend: str = "pi", key: str = "dashboard:probe"):
    acp = AcpClient(acp_backend=backend)
    acp._install_idle_reader()
    reader = asyncio.StreamReader()
    process = MagicMock()
    process.stdout = reader
    process.stdin = MagicMock()
    process.stdin.drain = AsyncMock()
    process.returncode = None
    acp._process = process
    acp._turn_done.set()
    provider = SimpleNamespace(client=acp)
    slot = SimpleNamespace(
        key=key,
        linked_session_key=None,
        is_closing=False,
        _stop_generation=0,
        _agent_started_pending=False,
        _turn_admission_reserved=False,
        running=False,
        task=None,
    )
    sessions = SimpleNamespace(
        final_drain_started=False,
        get_provider=lambda k: provider if k == key else None,
    )
    state = SimpleNamespace(
        _slots={slot.key: slot},
        sessions=sessions,
        _background_tasks=set(),
        run_background_turn=None,
        push_slots_update=MagicMock(),
    )
    return acp, reader, provider, slot, state


def _feed(reader: asyncio.StreamReader, update: dict) -> None:
    frame = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "probe-session", "update": update},
    }
    reader.feed_data((json.dumps(frame) + "\n").encode())


@pytest.mark.asyncio
async def test_pi_agent_started_callback_runs_guarded_promptless_turn(monkeypatch) -> None:
    acp, reader, provider, slot, state = _setup_slot_and_client()

    launched: list[asyncio.Task] = []
    run_seen = asyncio.Event()
    run_args: list[tuple] = []

    async def run_chat(*args, **kwargs):
        run_args.append((args, kwargs))
        run_seen.set()

    async def run_background_turn(_slot, coro):
        await coro

    state.run_background_turn = run_background_turn

    def spawn_guarded_turn(_state, _slot, coro):
        task = asyncio.create_task(run_background_turn(_slot, coro))
        launched.append(task)
        return task

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", run_chat, raising=False)
    monkeypatch.setattr("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn", spawn_guarded_turn)

    wire_agent_started_turn(state, slot, provider, slot.key)
    assert acp.on_agent_turn is not None

    _feed(reader, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}})
    _feed(reader, {"sessionUpdate": "session_info_update", "_meta": {"piAcp": {"running": False}}})
    acp._maybe_start_idle_reader()

    await asyncio.wait_for(run_seen.wait(), timeout=2)
    await asyncio.gather(*launched)
    assert len(launched) == 1
    assert run_args[0][0][2] == ""
    assert run_args[0][1] == {"_agent_started": True, "_turn_actor": "agent"}
    assert slot._agent_started_pending is False
    assert slot._turn_admission_reserved is False
    state.push_slots_update.assert_called_once_with()


@pytest.mark.asyncio
async def test_pi_agent_started_callback_rejects_stale_provider() -> None:
    acp, _reader, provider, slot, state = _setup_slot_and_client(key="dashboard:stale")
    state.sessions.get_provider = lambda _key: SimpleNamespace()

    wire_agent_started_turn(state, slot, provider, slot.key)

    assert acp.on_agent_turn is not None
    acp.on_agent_turn(acp)
    await asyncio.sleep(0)

    assert slot._agent_started_pending is False
    assert slot._turn_admission_reserved is False
    assert not state._background_tasks


@pytest.mark.asyncio
async def test_pi_agent_started_callback_waits_for_running_slot_task(monkeypatch) -> None:
    acp, _reader, provider, slot, state = _setup_slot_and_client()

    owner_release = asyncio.Event()
    owner_completed = asyncio.Event()

    async def active_owner():
        await owner_release.wait()
        owner_completed.set()
        slot.running = False
        slot.task = None

    slot.running = True
    owner_task = asyncio.create_task(active_owner())
    slot.task = owner_task

    launched: list[asyncio.Task] = []
    run_seen = asyncio.Event()

    async def run_chat(*args, **kwargs):
        run_seen.set()

    async def run_background_turn(_slot, coro):
        await coro

    state.run_background_turn = run_background_turn

    def spawn_guarded_turn(_state, _slot, coro):
        task = asyncio.create_task(run_background_turn(_slot, coro))
        launched.append(task)
        return task

    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", run_chat, raising=False)
    monkeypatch.setattr("kiro_crew.dashboard.turn_dispatch.spawn_guarded_turn", spawn_guarded_turn)

    wire_agent_started_turn(state, slot, provider, slot.key)
    acp.on_agent_turn(acp)

    await asyncio.sleep(0.01)
    # The wake is pending but waits for the active owner task
    assert slot._agent_started_pending is True
    assert not run_seen.is_set()

    # Finish owner task
    owner_release.set()
    await owner_completed.wait()
    await asyncio.wait_for(run_seen.wait(), timeout=2)
    await asyncio.gather(*launched)
    assert len(launched) == 1
    assert slot._agent_started_pending is False


@pytest.mark.asyncio
async def test_pi_agent_started_callback_cancelled_by_stop_generation() -> None:
    acp, _reader, provider, slot, state = _setup_slot_and_client()
    slot.running = True
    never_end = asyncio.Event()

    async def blocking_owner():
        await never_end.wait()

    owner_task = asyncio.create_task(blocking_owner())
    slot.task = owner_task

    wire_agent_started_turn(state, slot, provider, slot.key)
    acp.on_agent_turn(acp)
    await asyncio.sleep(0.01)
    assert slot._agent_started_pending is True

    # User presses stop -> increment stop generation
    slot._stop_generation += 1
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task
    slot.running = False
    slot.task = None

    await asyncio.sleep(0.05)
    # The pending turn must be cancelled and not delivered
    assert slot._agent_started_pending is False
    assert slot._turn_admission_reserved is False


@pytest.mark.asyncio
async def test_pi_agent_started_callback_aborts_on_gateway_shutdown() -> None:
    acp, _reader, provider, slot, state = _setup_slot_and_client()
    state.sessions.final_drain_started = True

    wire_agent_started_turn(state, slot, provider, slot.key)
    acp.on_agent_turn(acp)
    await asyncio.sleep(0)

    assert slot._agent_started_pending is False
    assert slot._turn_admission_reserved is False
