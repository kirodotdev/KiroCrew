"""Headless turns match skill triggers on the request, not Kiro Crew's own text.

A task-runner step, a task decomposition and a workflow authoring turn each
wrap the request in text Kiro Crew writes: the step's role text, the plan's other
steps, the spec and the instructions; the decomposition instructions around the
spec; the authoring instructions and the reference workflows around the intent.
Unless ``build_message`` gets ``skill_trigger_text``, triggers match that whole
message, so a skill whose trigger is a phrase of the wrapper loads on every such
turn whatever was asked.

Each case drives the real caller with a mocked ``ContextBuilder`` to capture the
``build_message`` call, then renders the captured call through a real builder
with such a skill installed, so the assertion is on the assembled prompt text.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_background_loops_compaction_reinjection import _task_ctx, _task_provider, _task_sessions

from kiro_crew import task_planner
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.skills import SkillsLoader
from kiro_crew.task_executor import task_trigger_text
from kiro_crew.taskrunner import Step, TaskRun, TaskRunner
from kiro_crew.workflow_memory import WorkflowScope
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: The skill body; its presence in the prompt means the skill was selected.
_BODY = "WRAPPER SKILL PROCEDURE: follow these steps."


def _builder(tmp_path: Path, trigger: str) -> ContextBuilder:
    skills = tmp_path / "skills"
    target = skills / "wrapper-words"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: wrapper-words\ndescription: Matches wrapper text\n"
        f"triggers: {trigger}\n---\n{_BODY}\n",
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


def _render(builder: ContextBuilder, message: str, trigger_text: str | None) -> str:
    rendered, _ = builder.build_message(
        message, True, "task:headless-trigger", skill_trigger_text=trigger_text
    )
    return rendered


# ── task-runner step ─────────────────────────────────────────────────────────

#: Words of ``build_task_prompt``'s role text, absent from the step below.
_ROLE_TRIGGER = "autonomous execution agent pipeline"


async def _step_build_call(tmp_path: Path) -> tuple[str, dict]:
    sessions = _task_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_task_provider(), True, False))
    ctx = _task_ctx()
    runner = TaskRunner(sessions=sessions, context_builder=ctx, auto_test=False, work_dir=tmp_path)
    run = TaskRun(
        spec_path=str(tmp_path / "t.md"), spec_content="Ship the parser", status="running"
    )
    first = Step(index=1, title="Draft the grammar", description="Write the grammar file")
    second = Step(index=2, title="Write the parser", description="Parse the grammar into nodes")
    run.tasks = [first, second]
    with patch("kiro_crew.task_executor.self_review", return_value=True):
        await runner._execute_single_task(run, second)
    call = ctx.build_message.call_args
    return call.args[0], call.kwargs


@pytest.mark.asyncio
async def test_a_step_matches_triggers_on_its_own_title_and_description(tmp_path):
    message, kwargs = await _step_build_call(tmp_path)

    assert kwargs["skill_trigger_text"] == "Write the parser\n\nParse the grammar into nodes"
    # The message still carries the wrapper: the plan's other step and the spec.
    assert "Draft the grammar" in message and "Ship the parser" in message


@pytest.mark.asyncio
async def test_a_step_does_not_load_a_skill_its_role_text_triggers(tmp_path):
    message, kwargs = await _step_build_call(tmp_path)
    builder = _builder(tmp_path, _ROLE_TRIGGER)

    # The wrapper alone would select it: what the step's trigger text prevents.
    assert _BODY in _render(builder, message, None)
    assert _BODY not in _render(builder, message, kwargs["skill_trigger_text"])


def test_a_step_whose_description_repeats_its_title_matches_the_title_once():
    step = Step(index=1, title="Write the parser", description="Write the parser")
    assert task_trigger_text(step) == "Write the parser"
    assert task_trigger_text(Step(index=1, title="Write", description="")) == "Write"


# ── task decomposition ───────────────────────────────────────────────────────

#: Words of the decomposition instructions, absent from the spec below.
_PLANNER_TRIGGER = "destructive steps"
_SPEC = "Add a CSV export to the reports page"


def _plan_stream(*_a: object, **_k: object):
    async def _gen():
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text='{"steps": [{"title": "Export"}]}')
        yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn")

    return _gen()


async def _decompose_build_call() -> tuple[str, dict]:
    provider = MagicMock()
    provider.stream = MagicMock(side_effect=_plan_stream)
    sessions = MagicMock()
    sessions.open_task_session = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built", None))
    with patch("kiro_crew.context.inherit_session_memory", AsyncMock(return_value="")):
        await task_planner.decompose(_SPEC, sessions, ctx, task_id="t1")
    call = ctx.build_message.call_args
    return call.args[0], call.kwargs


@pytest.mark.asyncio
async def test_a_decomposition_matches_triggers_on_the_spec(tmp_path):
    message, kwargs = await _decompose_build_call()
    builder = _builder(tmp_path, _PLANNER_TRIGGER)

    assert kwargs["skill_trigger_text"] == _SPEC
    assert _BODY in _render(builder, message, None)
    assert _BODY not in _render(builder, message, kwargs["skill_trigger_text"])


# ── workflow authoring ───────────────────────────────────────────────────────

_GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    return {'ok': True}\n"
)


@pytest.mark.asyncio
async def test_an_authoring_turn_matches_triggers_on_the_intent(monkeypatch):
    import kiro_crew.workflows.service as svc_mod

    seen: list[dict] = []

    async def _prompt(self, context, key, text, **kwargs):
        seen.append(kwargs)
        return text

    async def _stream(provider, message, **kw):
        return _GOOD_SCRIPT

    provider = MagicMock()
    sessions = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.destroy = AsyncMock()
    monkeypatch.setattr(WorkflowScope, "prompt", _prompt)
    monkeypatch.setattr(svc_mod, "stream_and_collect", _stream)

    out = await WorkflowService(sessions=sessions).author("summarise yesterday's builds")

    assert out["ok"] is True
    assert [kw["skill_trigger_text"] for kw in seen] == ["summarise yesterday's builds"]


@pytest.mark.asyncio
async def test_the_workflow_scope_hands_the_trigger_text_to_build_message(monkeypatch):
    scope = WorkflowScope("r1", "default", "", memory_mode="temporary")
    monkeypatch.setattr(WorkflowScope, "prepare", AsyncMock(return_value="default"))
    context = MagicMock()
    context.build_message = MagicMock(return_value=("built", None))

    await scope.prompt(
        context,
        "wf-author:r1:a1",
        "wrapped",
        is_new=True,
        agent="kirocrew-lite",
        cwd=None,
        provider=None,
        skill_trigger_text="the intent",
    )
    await scope.prompt(
        context, "wf:r1:1", "a worker prompt", is_new=True, agent="", cwd=None, provider=None
    )

    kwargs = [call.kwargs["skill_trigger_text"] for call in context.build_message.call_args_list]
    # A worker turn passes nothing: its text is the script author's own.
    assert kwargs == ["the intent", None]
