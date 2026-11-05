"""Headless turn builders mark the turn non-interactive and size it to the model.

The task runner's step turn, the planner's decomposition turn and a workflow
worker's turn have no user reading them live. Each must call ``build_message``
with ``interactive=False`` so the chat-only [REPLY FORMAT RULES] and [OPTIONS:]
guidance stays out, and with the provider's ``model_window`` so a step pinned
to a smaller model gets context caps sized for that model instead of the 1M
reference.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_background_loops_compaction_reinjection import (
    _run_one_task,
    _task_ctx,
    _task_provider,
    _task_sessions,
)
from test_task_planner_decompose_gate import (
    _ctx_with_hook,
    _provider_requesting_tool,
    _sessions_with,
)

from kiro_crew.hooks import TOOL_ALLOW
from kiro_crew.task_planner import decompose
from kiro_crew.workflow_memory import WorkflowScope

_WINDOW = 200_000


def _with_window(provider: MagicMock) -> MagicMock:
    provider.context_window_tokens = MagicMock(return_value=_WINDOW)
    return provider


def _headless_kwargs(build_message: MagicMock) -> dict:
    call = build_message.call_args
    assert call is not None, "build_message was never called"
    return {k: call.kwargs.get(k) for k in ("interactive", "model_window")}


@pytest.mark.asyncio
async def test_task_step_turn_is_non_interactive_and_window_sized(tmp_path):
    sessions = _task_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_with_window(_task_provider()), False, False))
    ctx = _task_ctx()

    await _run_one_task(sessions, ctx, tmp_path)

    assert _headless_kwargs(ctx.build_message) == {
        "interactive": False,
        "model_window": _WINDOW,
    }


def test_decomposition_turn_is_non_interactive_and_window_sized():
    provider = _with_window(_provider_requesting_tool())
    ctx = _ctx_with_hook(TOOL_ALLOW)

    asyncio.run(decompose("spec", _sessions_with(provider), ctx=ctx, task_id="t1"))

    assert _headless_kwargs(ctx.build_message) == {
        "interactive": False,
        "model_window": _WINDOW,
    }


@pytest.mark.asyncio
async def test_workflow_worker_turn_is_non_interactive_and_window_sized():
    scope = WorkflowScope("run-1", "global", "origin", "temporary", MagicMock(member_id=None))
    context = MagicMock()
    context.build_message = MagicMock(return_value=("full", None))
    provider = _with_window(MagicMock())

    with patch("kiro_crew.workflow_memory.bind_session_execution"):
        await scope.prompt(
            context,
            scope.worker_key("w"),
            "do it",
            is_new=True,
            agent=None,
            cwd=None,
            provider=provider,
        )

    assert _headless_kwargs(context.build_message) == {
        "interactive": False,
        "model_window": _WINDOW,
    }
