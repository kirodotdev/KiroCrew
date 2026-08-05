"""Tests for the SEC-006 decomposition-phase tool gate in task_planner.decompose.

The decomposition phase must run tool-permission requests through the same
deny-list/hook check used during execution, instead of unconditionally
approving every request (which a prompt-injection in the spec could abuse).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, ToolHookResult
from kiro_crew.providers.base import LLMEvent
from kiro_crew.task_planner import decompose


def _sessions_with(provider: MagicMock) -> MagicMock:
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await sessions.get_or_create(session_key, agent=agent, cwd=cwd)

    sessions.open_task_session = _open_task_session
    sessions.release_subagent_runtime = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    return sessions


def _provider_requesting_tool() -> MagicMock:
    provider = MagicMock()

    async def _stream(message: str):
        yield LLMEvent(kind="permission_request", title="execute_bash", request_id="r1")
        yield LLMEvent(kind="text_chunk", text='{"steps": []}')
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    return provider


def _ctx_with_hook(action: str) -> MagicMock:
    ctx = MagicMock()
    ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=action))
    ctx.build_message = MagicMock(return_value=("prompt", None))
    return ctx


def test_denied_tool_is_rejected_during_decomposition():
    provider = _provider_requesting_tool()
    sessions = _sessions_with(provider)
    ctx = _ctx_with_hook(TOOL_DENY)

    asyncio.run(decompose("spec", sessions, ctx=ctx, task_id="t1"))

    provider.reject_tool.assert_awaited_once_with("r1")
    provider.approve_tool.assert_not_awaited()


def test_allowed_tool_is_approved_during_decomposition():
    provider = _provider_requesting_tool()
    sessions = _sessions_with(provider)
    ctx = _ctx_with_hook(TOOL_ALLOW)

    asyncio.run(decompose("spec", sessions, ctx=ctx, task_id="t1"))

    provider.approve_tool.assert_awaited_once_with("r1")
    provider.reject_tool.assert_not_awaited()


def test_no_ctx_denies_by_default():
    """Without a ContextBuilder there is no hook store to gate the request, so
    deny by default rather than approving (CSE SEC-006, review-bot deny-by-default)."""
    provider = _provider_requesting_tool()
    sessions = _sessions_with(provider)

    asyncio.run(decompose("spec", sessions, ctx=None, task_id="t1"))

    provider.reject_tool.assert_awaited_once_with("r1")
    provider.approve_tool.assert_not_awaited()
