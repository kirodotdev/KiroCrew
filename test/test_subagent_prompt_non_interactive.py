"""A sub-agent's prompt carries no ``[REPLY FORMAT RULES]`` block.

A sub-agent's reader is its parent agent, not a person at a dashboard, so the
per-turn ``[OPTIONS:]`` and dashboard-tool guidance has no surface to render
into. ``_SYSTEM_PREFIX`` tells an unnamed child not to emit ``[OPTIONS:]``; if
``build_message`` were called with its ``interactive=True`` default, the same
prompt would also tell it to end every choice with one, and a named child
(which gets no ``_SYSTEM_PREFIX``) would see only that instruction.

This drives one real sub-agent turn with a mocked ``ContextBuilder`` to capture
the ``build_message`` call, then renders that call through a real builder, so
the assertion is on the assembled prompt text.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.skills import SkillsLoader
from kiro_crew.subagent import SubagentManager

# ``SubagentManager.spawn`` refuses while the host looks short of memory, and
# the real builder reads host resources; neither is this test's input.
pytestmark = pytest.mark.usefixtures(
    "healthy_host_memory", "ample_host_resources", "close_skills_loaders"
)

_REPLY_FORMAT_MARKER = "[REPLY FORMAT RULES]"

#: How long a test waits for the run it started before failing by name: far past
#: any healthy run here, and well under the suite's per-test timeout.
_RUN_CEILING = 60.0


def _stream(*_a: object, **_k: object):
    async def _gen():
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="done", runtime_global=False)
        yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

    return _gen()


def _mock_sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=_stream)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    return sessions


async def _child_build_call() -> tuple[tuple, dict]:
    """Spawn one sub-agent and return the ``build_message`` call it made."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("pick the better of two designs")
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
    assert ctx.build_message.called, "build_message was never reached"
    call = ctx.build_message.call_args
    return call.args, call.kwargs


@pytest.mark.asyncio
async def test_child_turn_is_built_non_interactive():
    _args, kwargs = await _child_build_call()
    assert kwargs.get("interactive") is False


@pytest.mark.asyncio
async def test_assembled_child_prompt_has_no_reply_format_rules(tmp_path):
    args, kwargs = await _child_build_call()
    message, is_new, session_key = args
    assert session_key.startswith("subagent:")
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path),
    )
    # Render the captured call's turn-shape arguments; the default stands in
    # for an omitted ``interactive=``, which is how the call looked unfixed.
    rendered, _ = builder.build_message(
        message,
        is_new,
        session_key,
        interactive=kwargs.get("interactive", True),
    )
    assert _REPLY_FORMAT_MARKER not in rendered
    # The per-turn reminder itself. The agent system prompt's own mention of
    # [OPTIONS:] is a separate source and is not this call's to withhold.
    assert "(If presenting choices, end with [OPTIONS:" not in rendered
    # The guard is meaningful only if an interactive turn of the same shape
    # does carry the block.
    interactive, _ = builder.build_message(message, is_new, session_key, interactive=True)
    assert _REPLY_FORMAT_MARKER in interactive
