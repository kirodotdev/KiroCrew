"""A sub-agent completion turn triggers no skills; a person's turn still does.

The unit under test is the ``skill_trigger_text`` keyword that ``_turn_build_prompt``
(``chat_turn/prompt_assembly.py``) hands ``build_message``. A completion event
repeats the sub-agent's task and result, which is the parent's own work coming
back, so matching skills on it injected full skill bodies on every completion.
Each test drives the REAL runner with a mocked ``build_message`` and reads the
keyword it received.
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
from kiro_crew.testing.wait import finished

SLOT_NAME = "parent-chat"
TEXT = "Review the coder changes and restart the gateway."


def _turn_state(tmp_path, monkeypatch):
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
    state.sessions.get_or_create = AsyncMock(return_value=(provider, False, True))
    state.sessions.consume_replay_suppression = MagicMock(return_value=False)
    state.sessions.consume_needs_reinjection = MagicMock(return_value=False)
    state.sessions.record_failure = AsyncMock()
    monkeypatch.setattr(chat_runner, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(chat_runner, "generate_session_summary", AsyncMock())
    return state


async def _trigger_text(tmp_path, monkeypatch, **run_kwargs) -> str:
    state = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.append("user", TEXT)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, TEXT, **run_kwargs), 30)
    await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)
    state.context_builder.build_message.assert_called()
    return state.context_builder.build_message.call_args.kwargs["skill_trigger_text"]


@pytest.mark.asyncio
async def test_subagent_completion_turn_passes_empty_trigger_text(tmp_path, monkeypatch):
    text = await _trigger_text(
        tmp_path, monkeypatch, _directive_user_origin=False, _turn_actor="subagent"
    )
    assert text == ""


@pytest.mark.asyncio
async def test_user_turn_still_passes_its_text(tmp_path, monkeypatch):
    text = await _trigger_text(tmp_path, monkeypatch)
    assert "restart the gateway" in text
