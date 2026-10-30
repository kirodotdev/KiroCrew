"""A sub-agent's skill triggers match the task it was given, nothing else.

``run_turn`` prepends Kiro Crew's own text to the task: ``_SYSTEM_PREFIX`` for
an unnamed child, the cancel-resume preamble on a retry, and the MCP notice.
Unless ``build_message`` gets ``skill_trigger_text``, triggers match against
that whole message, so a skill whose trigger is a word of the prefix
("orchestration decisions") loads into every unnamed child.

This drives one real sub-agent turn with a mocked ``ContextBuilder`` to capture
the ``build_message`` call, then renders that call through a real builder with
such a skill installed, so the assertion is on the assembled prompt text.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.skills import SkillsLoader
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_parts.run_limits import _SYSTEM_PREFIX

# ``SubagentManager.spawn`` refuses while the host looks short of memory, and
# the real builder reads host resources; neither is this test's input.
pytestmark = pytest.mark.usefixtures(
    "healthy_host_memory", "ample_host_resources", "close_skills_loaders"
)

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "ORCHESTRATION REVIEW PROCEDURE: list every decision first."
#: A trigger made of words that appear in ``_SYSTEM_PREFIX`` only.
_PREFIX_TRIGGER = "orchestration decisions"

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


async def _child_build_call(task: str) -> tuple[tuple, dict]:
    """Spawn one unnamed sub-agent and return the ``build_message`` call it made."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=ctx)
    mgr._should_use_session_sharing = MagicMock(return_value=False)  # type: ignore[method-assign]
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
    assert ctx.build_message.called, "build_message was never reached"
    call = ctx.build_message.call_args
    return call.args, call.kwargs


def _builder(tmp_path: Path) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "orchestration-review"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: orchestration-review\ndescription: Review orchestration choices\n"
        f"triggers: {_PREFIX_TRIGGER}\n---\n{_BODY}\n",
        encoding="utf-8",
    )
    # The matcher is off at the shipped cap of 0; this case is about what it
    # matches once an operator turns it on.
    loader = SkillsLoader(
        skills_path=skills,
        install_builtins=False,
        config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
    )
    return ContextBuilder(memory=MemoryStore(workspace=tmp_path / "ws"), skills=loader)


def _render(builder: ContextBuilder, args: tuple, kwargs: dict) -> str:
    message, is_new, session_key = args
    rendered, _ = builder.build_message(
        message,
        is_new,
        session_key,
        skill_trigger_text=kwargs.get("skill_trigger_text"),
    )
    return rendered


def test_the_prefix_carries_the_trigger_words():
    # The case below is meaningful only while the prefix holds these words.
    for word in _PREFIX_TRIGGER.split():
        assert word in _SYSTEM_PREFIX


@pytest.mark.asyncio
async def test_child_triggers_read_the_raw_task():
    task = "pick the better of two designs"
    args, kwargs = await _child_build_call(task)
    assert args[0].startswith(_SYSTEM_PREFIX)
    assert kwargs.get("skill_trigger_text") == task


@pytest.mark.asyncio
async def test_spawn_prefix_loads_no_skill(tmp_path):
    args, kwargs = await _child_build_call("pick the better of two designs")
    assert _BODY not in _render(_builder(tmp_path), args, kwargs)


@pytest.mark.asyncio
async def test_task_naming_the_trigger_still_loads_the_skill(tmp_path):
    args, kwargs = await _child_build_call("audit the orchestration decisions in cron.py")
    assert _BODY in _render(_builder(tmp_path), args, kwargs)
