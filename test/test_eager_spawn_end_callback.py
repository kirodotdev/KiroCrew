"""How a speculative start reports its end to the signal that armed it.

A reload passes ``on_ended`` to ``schedule_eager_spawn`` and closes its notice
from it (``session_relaunch._report_reload_respawn``). These drive the real
scheduler and ``_eager_spawn`` with only the allocation stubbed: the callback
runs exactly once with ``None`` when the start ended without failing, once with
the exception when the start failed, and never when the start was cancelled by
a newer slot signal or the gateway shutdown.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eager_spawn_helpers import (  # noqa: F401 -- fixtures are used by name
    _isolate_armed_registry,
    _mock_state,
    _native_resume_cfg,
    _pin_prewarm_allowance,
    _prime,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.session import SpeculativeResumeRefused
from kiro_crew.testing.wait import await_until


@pytest.fixture(autouse=True)
def _no_debounce(_floor_monkeypatch):
    _floor_monkeypatch.setattr(chat_runner, "_EAGER_SPAWN_DEBOUNCE_SECS", 0)


def _arm_respawn(state, slot) -> tuple[asyncio.Task, list[Exception | None]]:
    """Arm a reload-shaped respawn (a resume start) and collect its end reports."""
    ended: list[Exception | None] = []
    _prime(True)
    task = chat_runner.schedule_eager_spawn(state, slot, allow_resume=True, on_ended=ended.append)
    assert task is not None
    return task, ended


async def _drop_prefetch_ttl(slot: _ChatSlot) -> None:
    ttl = getattr(slot, "_prefetch_ttl_task", None)
    if ttl is not None:
        ttl.cancel()
        await asyncio.gather(ttl, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allocation",
    [
        pytest.param({"return_value": (MagicMock(), True, True)}, id="registered"),
        # Another creator (a turn that joined the start) registered the session.
        pytest.param({"return_value": (MagicMock(), False, True)}, id="joined"),
        # Refused: the load fell back, or a person's claim took the start over.
        pytest.param({"side_effect": SpeculativeResumeRefused("dashboard:t1")}, id="stood-down"),
    ],
)
async def test_a_start_that_did_not_fail_reports_none_once(allocation):
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    state.sessions.get_or_create = AsyncMock(**allocation)
    with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
        task, ended = _arm_respawn(state, slot)
        try:
            await await_until(task, what="the respawn")
        finally:
            await _drop_prefetch_ttl(slot)
    state.sessions.get_or_create.assert_awaited_once()
    assert ended == [None]


@pytest.mark.asyncio
async def test_a_start_that_failed_reports_its_exception_once():
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    failure = RuntimeError("the agent's initialize handshake failed")
    state.sessions.get_or_create = AsyncMock(side_effect=failure)
    with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
        task, ended = _arm_respawn(state, slot)
        await await_until(task, what="the respawn")
    state.sessions.get_or_create.assert_awaited_once()
    assert len(ended) == 1
    assert ended[0] is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled_by", ["newer-signal", "shutdown"])
async def test_a_cancelled_start_reports_nothing(cancelled_by):
    slot = _ChatSlot("t1")
    state = _mock_state(slot)
    state._slots = {slot.key: slot}
    allocating = asyncio.Event()

    async def _held_until_cancelled(*_args, **_kwargs):
        allocating.set()
        await asyncio.Event().wait()

    state.sessions.get_or_create = AsyncMock(side_effect=_held_until_cancelled)
    with patch.object(chat_runner.KiroCrewConfig, "load", _native_resume_cfg()):
        task, ended = _arm_respawn(state, slot)
        newer: asyncio.Task | None = None
        try:
            await await_until(allocating, what="the respawn reaching its allocation")
            if cancelled_by == "newer-signal":
                # The newer signal's own start completes; only the cancelled
                # respawn's report is under test.
                state.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
                newer = chat_runner.schedule_eager_spawn(state, slot)
                assert newer is not None and newer is not task
            else:
                assert chat_runner.stop_eager_spawns(state) == [task]
            await await_until(
                asyncio.gather(task, return_exceptions=True), what="the cancelled respawn"
            )
            if newer is not None:
                await await_until(newer, what="the newer signal's start")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert ended == []
