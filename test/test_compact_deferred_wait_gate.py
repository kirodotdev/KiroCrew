"""A user-typed ``/compact`` holds the chat turn gate through its deferred wait.

On a shared kiro-cli chat runtime (``chat_runtime_sharing`` preview) a
``/compact`` whose terminal arrives late runs its ``wait_for_compaction`` receipt
AFTER the ``stream_command`` turn has released the per-process chat turn gate. In
that window a co-tenant chat can start a turn and receive this session's
OWNERLESS compaction terminal (the status names no issuer and is fanned
``runtime_global`` to every co-tenant runner), clearing the co-tenant's
already-streamed answer. ``chat_runner._run_chat`` therefore enters
``runtime_ownership.chat_turn_gate_hold(client)`` onto a turn-scoped exit stack
before the dispatch gates of every turn and closes it in the turn's ``finally``,
so the gate is held from the command turn through ``_turn_compact_command``'s
deferred wait. ``CompactionCoordinator`` holds the same gate for automatic ones.

The behavioural tests drive the real hold and a real ``AcpSessionProvider`` gate
and prove a co-tenant cannot start a turn while the hold is in place, and that the
real ``_run_chat`` streams the command inside it.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from kiro_crew.runtime_ownership import chat_turn_gate_hold


def _gate_backed_provider(shared: bool = True):
    """An AcpSessionProvider whose per-process gate is a real asyncio lock."""
    from unittest.mock import MagicMock

    from kiro_crew.acp.session_provider import AcpSessionProvider

    gate_lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def chat_turn_gate():
        async with gate_lock:
            yield

    runtime = MagicMock()
    runtime.chat_turn_gate = chat_turn_gate
    provider = AcpSessionProvider(MagicMock(), runtime, shared_runtime=shared)
    return provider, gate_lock


@pytest.mark.asyncio
async def test_a_co_tenant_cannot_start_a_turn_during_the_deferred_wait() -> None:
    provider, gate_lock = _gate_backed_provider()

    wait_entered = asyncio.Event()
    release_wait = asyncio.Event()

    async def deferred_compact():
        # The call-site shape: the hold is entered, the command turn and the
        # deferred receipt run inside it (the inner gate re-entry is a no-op).
        async with chat_turn_gate_hold(provider):
            async with provider._chat_turn_gate():
                pass
            wait_entered.set()
            await release_wait.wait()
            return "completed"

    compact_task = asyncio.create_task(deferred_compact())
    await asyncio.wait_for(
        wait_entered.wait(), timeout=30.0
    )  # flake-ok: awaits the Event the deferred-compact task sets on entry, bounded only to name a hang
    assert gate_lock.locked()

    cotenant_started = asyncio.Event()

    async def cotenant_turn():
        async with provider._chat_turn_gate():
            cotenant_started.set()

    # A second task is a different holder, so it contends for the lock.
    cotenant_task = asyncio.create_task(cotenant_turn())
    for _ in range(50):
        await asyncio.sleep(0)
    assert not cotenant_started.is_set(), "a co-tenant started a turn during the deferred wait"

    release_wait.set()
    assert (
        await asyncio.wait_for(compact_task, timeout=30.0) == "completed"
    )  # flake-ok: awaits the task release_wait.set() just unblocked, bounded only to name a hang
    await asyncio.wait_for(
        cotenant_task, timeout=30.0
    )  # flake-ok: awaits the co-tenant that proceeds once the gate frees, bounded only to name a hang
    assert cotenant_started.is_set()


@pytest.mark.asyncio
async def test_an_unshared_provider_gets_a_null_hold() -> None:
    # The preview off: the provider is not shared, so the hold takes no lock and
    # the /compact turn runs exactly as before.
    provider, gate_lock = _gate_backed_provider(shared=False)
    async with chat_turn_gate_hold(provider):
        assert not gate_lock.locked()

    class _NoHook:
        pass

    async with chat_turn_gate_hold(_NoHook()):
        pass  # does not raise


@pytest.mark.asyncio
async def test_a_turn_streams_its_command_inside_the_hold(tmp_path, monkeypatch) -> None:
    """The real ``_run_chat`` holds the gate while the command streams, and frees it after."""
    from test_todo_pill_resync import _runner_state, _runner_turn

    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    state, _sent = _runner_state(tmp_path, monkeypatch, is_new=False, resumed=True)
    slot = state.get_or_create_slot("compact-chat")
    provider = (await state.sessions.get_or_create())[0]
    provider._shared_runtime = True
    gate = asyncio.Lock()
    held_while_streaming: list[bool] = []

    @contextlib.asynccontextmanager
    async def hold():
        async with gate:
            yield

    async def stream_command(message, *args, **kwargs):
        held_while_streaming.append(gate.locked())
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.hold_chat_turn_gate = hold
    provider.stream_command = stream_command
    await _runner_turn(state, slot, "/compact")
    assert held_while_streaming == [True]
    assert not gate.locked()
