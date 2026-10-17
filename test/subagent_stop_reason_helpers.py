"""Run harness for the sub-agent stop-reason tests.

A ``SubagentManager`` whose ACP session provider is faked, a single-turn
stream, and the waits that end only once a run and its detached state writers
have finished.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete(stop_reason: str, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_COMPLETE,
        stop_reason=stop_reason,
        text=text,
        title="execute_bash",
        tool_input="pytest -q > run.log 2>&1",
        runtime_global=False,
        refusal=None,
    )


def _mock_sessions(stream_factory) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions._provider = provider
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _manager(sessions: MagicMock) -> SubagentManager:
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr


def _spy_events(mgr: SubagentManager) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    _orig = mgr._fire_event

    async def _spy(etype, info, extra=None):
        events.append((etype, dict(extra or {})))
        await _orig(etype, info, extra)

    mgr._fire_event = _spy
    return events


def _spy_taskq_marks(mgr: SubagentManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    marks: list[str] = []
    real = type(mgr._admission).taskq_mark

    def _spy(self, info, state):
        marks.append(state)
        real(self, info, state)

    monkeypatch.setattr(type(mgr._admission), "taskq_mark", _spy)
    return marks


#: How long a test waits for a run it started before failing by name: far past
#: any healthy run here, and well under the suite's per-test timeout.
_RUN_CEILING = 60.0


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
    await _wait_writers_landed(mgr, info)
    return info


async def _wait_writers_landed(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Wait for every state writer the run left detached to land.

    The finished-result write is bounded by ``_STATE_DRAIN_TIMEOUT`` even when
    nothing cancels it, and past the bound the run ends while its worker is
    still writing. ``state.json`` then lacks ``result_complete`` until that
    worker lands, so a test that reads it right after the run ends must wait
    for the hold the worker keeps on the run."""
    await async_wait_until(
        lambda: info.id not in mgr._abandoned_state_writers,
        describe=lambda: f"waiting for run {info.id}'s detached state writers to land",
    )


def _single_turn(
    stop_reason: str | None, evidence: str = "", *, chunks: tuple[str, ...] = ("partial output ",)
):
    """Every stream call: *chunks*, then the same completion. ``None`` is no
    completion at all: the generator just ends, as when the transport dies."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            for chunk in chunks:
                yield _text(chunk)
            if stop_reason is not None:
                yield _complete(stop_reason, evidence)

        return _gen()

    return stream_factory, calls


def _done_event(events: list[tuple[str, dict]]) -> dict:
    done = [e for k, e in events if k == "subagent_done"]
    assert len(done) == 1, events
    return done[0]
