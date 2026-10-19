"""Helpers shared by the three ``test_subagent_turn_resilience*.py`` modules.

``_mock_sessions`` builds a sessions mock whose provider streams from the given factory,
``_manager`` a ``SubagentManager`` over it on the dedicated-process path, and
``_spawn_and_wait`` spawns one run and awaits its task. ``_text_event`` and
``_complete_event`` are the stream events, and ``_TransientError``, ``_FatalError`` and
``_context_overflow_error`` the failures the tests inject. Each of the three modules
imports the autouse ``_close_subagent_managers``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.subagent import SubagentInfo, SubagentManager


@pytest.fixture(autouse=True)
def _close_subagent_managers(close_subagent_managers) -> None:
    """Every manager built here opens tasks.db; close it at teardown, not at GC."""


class _TransientError(Exception):
    """Duck-typed AcpError carrying the structured transient verdict."""

    transient = True


class _FatalError(Exception):
    transient = False


def _context_overflow_error():
    from kiro_crew.acp.client import AcpError

    error = AcpError("The agent context exceeded the model window", transient=False)
    error.structural_terminal = True
    error.context_overflow = True
    return error


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete_event() -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)


def _mock_sessions(stream_factory) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    # Read synchronously after every turn; as AsyncMock children they
    # would hand back coroutines nobody awaits.
    provider.context_window_tokens = lambda: 0
    provider.context_used_tokens = lambda: 0
    provider.mcp_session_report = MagicMock(return_value=None)
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions.forget_conversation_if_sid = MagicMock(return_value=(False, None))
    sessions.aflush = AsyncMock()
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
    # Force the dedicated-process path (deterministic under MagicMock sessions).
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        # flake-ok: pre-existing unbounded await of a run's task, moved verbatim
        await mgr._tasks[info.id]
    return info
