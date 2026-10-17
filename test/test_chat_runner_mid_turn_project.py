"""A project picked while a reply runs does not reach that reply.

``chat_runner._run_chat`` releases the session switch lock once its session is
registered, and from then on the project route may write ``slot.project``. The
running turn must keep describing the directory its kiro-cli process was
allocated in: the prompt context built by ``build_message`` and the crew log's
``session/opened`` cwd read the turn's start-of-turn project, not the live field.

Each test drives the REAL runner with a mocked provider and ``build_message``,
and simulates the pick by writing ``slot.project`` from inside the session
claim, which is the last await before the lock is released.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

SLOT_NAME = "mid-turn-project"


def _turn_state(tmp_path, monkeypatch, slot_ref: dict, picked: str):
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.context_builder.build_message = MagicMock(return_value=("task", None))
    state.context_builder.ensure_store = AsyncMock(return_value=object())
    provider = MagicMock()

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    provider.stream = stream

    async def claim(*args, **kwargs):
        # The pick lands as the session registers; the runner releases the
        # switch lock right after this returns, which is when the project
        # route's write would go through.
        slot_ref["slot"].project = picked
        return provider, True, False

    state.sessions.get_or_create = AsyncMock(side_effect=claim)
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


@pytest.mark.asyncio
async def test_pick_after_registration_does_not_reach_the_running_prompt(tmp_path, monkeypatch):
    start = tmp_path / "start-of-turn"
    picked = tmp_path / "picked-mid-turn"
    start.mkdir()
    picked.mkdir()
    slot_ref: dict = {}
    state = _turn_state(tmp_path, monkeypatch, slot_ref, str(picked))
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.project = str(start)
    slot_ref["slot"] = slot
    opened = MagicMock()
    monkeypatch.setattr(chat_runner.crew_log_emit, "on_session_opened", opened)

    slot.append("user", "Look at the repo.")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Look at the repo."), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    # The pick itself stands: the next turn starts in the new project.
    assert slot.project == str(picked)
    state.context_builder.build_message.assert_called()
    assert state.context_builder.build_message.call_args.kwargs["project"] == str(start)
    opened.assert_called()
    assert opened.call_args.kwargs["cwd"] == str(start)


@pytest.mark.asyncio
async def test_pick_after_registration_does_not_reach_prompt_or_skill_expansion(
    tmp_path, monkeypatch
):
    """``@prompt`` and ``$skill`` expansion resolve against the start-of-turn
    project, so a mid-reply pick cannot load another project's local prompt
    file, skill bodies or skill allow-globs into this reply."""
    start = tmp_path / "start-of-turn"
    picked = tmp_path / "picked-mid-turn"
    start.mkdir()
    picked.mkdir()
    slot_ref: dict = {}
    state = _turn_state(tmp_path, monkeypatch, slot_ref, str(picked))
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.project = str(start)
    slot_ref["slot"] = slot
    seen: dict = {}

    async def _prompt(message, _state, _slot, *, project=None):
        seen["prompt"] = (project, _slot.project)
        return message, "not_found"

    def _skills(message, _state, _slot, _key, project=None):
        seen["skills"] = (project, _slot.project)
        return message, 0

    monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", _prompt)
    monkeypatch.setattr(chat_runner, "_expand_dollar_skills", _skills)

    message = "@local-sop use $local-skill"
    slot.append("user", message)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, message), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    # Both expansions ran after the pick landed, and both were handed the
    # project the turn started in rather than the live field.
    assert seen["prompt"] == (str(start), str(picked))
    assert seen["skills"] == (str(start), str(picked))


def test_explicit_project_overrides_the_live_slot_project(tmp_path):
    slot = MagicMock()
    slot.project = str(tmp_path / "live")
    assert chat_runner._slot_prompt_project(slot) == tmp_path / "live"
    assert chat_runner._slot_prompt_project(slot, str(tmp_path / "turn")) == tmp_path / "turn"
    # An empty snapshot means the turn started with no project: no local root.
    assert chat_runner._slot_prompt_project(slot, "") is None


def test_dollar_skills_resolve_against_the_explicit_project(tmp_path, monkeypatch):
    slot = MagicMock()
    slot.project = str(tmp_path / "live")
    slot.agent = "kirocrew"
    slot.key = SLOT_NAME
    seen: dict = {}

    def _globs(_key, _agent, *, project_dir=None, provider_type=None):
        seen["globs"] = project_dir
        return None

    skills = MagicMock()

    def _resolve(_message, project_dir, only=None):
        seen["resolve"] = project_dir
        return []

    skills.resolve_dollar_skills = _resolve
    skills.has_dollar_candidate = MagicMock(return_value=False)
    monkeypatch.setattr(chat_runner, "session_skill_globs", _globs)
    monkeypatch.setattr(chat_runner, "_get_skills", lambda _state: skills)

    turn = str(tmp_path / "turn")
    chat_runner._expand_dollar_skills("use $x", MagicMock(), slot, "dashboard:x", turn)
    assert seen == {"globs": turn, "resolve": turn}
    chat_runner._expand_dollar_skills("use $x", MagicMock(), slot, "dashboard:x", "")
    assert seen == {"globs": None, "resolve": None}


@pytest.mark.asyncio
async def test_deferred_reset_turn_describes_the_reused_process_directory(tmp_path, monkeypatch):
    """A pick staged while sub-agents are attached leaves the project reset
    armed into the next turn, which reuses the process spawned in the old
    directory. That turn's prompt and crew log name the process's directory,
    not the staged pick."""
    old = tmp_path / "process-dir"
    picked = tmp_path / "staged-pick"
    old.mkdir()
    picked.mkdir()
    slot_ref: dict = {}
    state = _turn_state(tmp_path, monkeypatch, slot_ref, str(picked))
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.project = str(picked)
    slot_ref["slot"] = slot
    key = chat_runner.effective_session_key(slot)
    slot._pending_reset_history_key = key
    monkeypatch.setattr(chat_runner, "subagents_attached_async", AsyncMock(return_value=True))
    monkeypatch.setattr(chat_runner, "_arm_pending_reset_retry", MagicMock())
    reused = MagicMock()
    reused.cwd = str(old)

    async def stream(*args, **kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Done.")
        yield LLMEvent(kind=EVENT_COMPLETE)

    reused.stream = stream
    state.sessions.get_or_create = AsyncMock(return_value=(reused, False, False))
    opened = MagicMock()
    monkeypatch.setattr(chat_runner.crew_log_emit, "on_session_opened", opened)

    slot.append("user", "Carry on.")
    await asyncio.wait_for(chat_runner._run_chat(state, slot, "Carry on."), 30)
    await asyncio.wait_for(drain_background_tasks(state), 30)

    # The reset is still owed, and the turn described the reused process.
    assert slot._pending_reset_history_key == key
    state.context_builder.build_message.assert_called()
    assert state.context_builder.build_message.call_args.kwargs["project"] == str(old)
    opened.assert_called()
    assert opened.call_args.kwargs["cwd"] == str(old)
