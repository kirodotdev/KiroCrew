"""One consolidation pass at a time per gateway process.

Each pass spends minutes on embeds and store writes in the same process that
serves HTTP and the websocket. Passes for different sessions running side by
side starve the event loop through the GIL. These tests pin the gate:
two sessions' passes never overlap, the second one still runs, and a pass that
raises or is cancelled never strands the gate.
"""

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import consolidation_gate
from kiro_crew import history as history_mod
from kiro_crew.consolidation_gate import consolidation_pass_gate
from kiro_crew.history import ConversationLog, HistoryConsolidator

KEYS = ("dashboard:chat-gate-a", "dashboard:chat-gate-b")


def _consolidator(tmp_path) -> HistoryConsolidator:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for key in KEYS:
            for i in range(3):
                log.append(key, "user", f"{key} m{i}")
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    return HistoryConsolidator(
        log=log, memory=memory, migrated=True, history_idle_secs=0, sessions=None
    )


class _RecordingGate(asyncio.Semaphore):
    """The real one-slot gate, plus an event set when a pass has to queue on it."""

    def __init__(self) -> None:
        super().__init__(1)
        self.queued = asyncio.Event()

    async def acquire(self) -> bool:
        if self.locked():
            self.queued.set()
        return await super().acquire()


@pytest.mark.asyncio
async def test_two_sessions_passes_never_overlap_and_both_run(tmp_path, monkeypatch) -> None:
    c = _consolidator(tmp_path)
    gate = _RecordingGate()
    monkeypatch.setattr(consolidation_gate, "consolidation_pass_gate", lambda: gate)
    in_flight = 0
    peak = 0
    ran: list[str] = []
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()

    async def _turn(_prompt: str, session_key: str = "", **_kw: Any) -> dict:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        ran.append(session_key)
        if len(ran) == 1:
            first_entered.set()
            # Hold the first pass until the second either queues on the gate
            # or, with no gate, reaches its own provider turn beside this one.
            waiters = [
                asyncio.ensure_future(gate.queued.wait()),
                asyncio.ensure_future(second_entered.wait()),
            ]
            await asyncio.wait(waiters, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
        else:
            second_entered.set()
        in_flight -= 1
        return {"history_entry": "e"}

    with patch.object(c, "_call_llm", _turn):
        first = asyncio.create_task(c._consolidate(KEYS[0]))
        await asyncio.wait_for(first_entered.wait(), timeout=30)
        second = asyncio.create_task(c._consolidate(KEYS[1]))
        await asyncio.wait_for(asyncio.gather(first, second), timeout=30)

    assert peak == 1
    assert sorted(ran) == sorted(KEYS)


@pytest.mark.asyncio
async def test_a_raising_pass_releases_the_gate(tmp_path) -> None:
    c = _consolidator(tmp_path)

    async def _boom(*_a: Any, **_kw: Any) -> dict:
        raise RuntimeError("boom")

    with patch.object(c, "_call_llm", _boom):
        with pytest.raises(RuntimeError):
            await c._consolidate(KEYS[0])

    assert not consolidation_pass_gate().locked()


@pytest.mark.asyncio
async def test_a_pass_cancelled_while_queued_leaves_the_gate_to_the_holder(tmp_path) -> None:
    c = _consolidator(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _turn(*_a: Any, **_kw: Any) -> dict:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=30)
        return {"history_entry": "e"}

    with patch.object(c, "_call_llm", _turn):
        holder = asyncio.create_task(c._consolidate(KEYS[0]))
        await asyncio.wait_for(entered.wait(), timeout=30)
        queued = asyncio.create_task(c._consolidate(KEYS[1]))
        for _ in range(20):
            await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert consolidation_pass_gate().locked()
        release.set()
        await asyncio.wait_for(holder, timeout=30)

    assert not consolidation_pass_gate().locked()
