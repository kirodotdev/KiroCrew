"""The dashboard runner hands the MCP sign-in trigger only what the person typed.

A KAS session offers an unsigned MCP server's Authorize card when the user's
message names it (``AcpSessionHandle.want_mcp_sign_in_named``). The runner calls
``LLMProvider.note_user_message`` before the prompt. These tests drive the REAL
runner with a mocked provider and read what it was handed: the person's words,
with no prepend the runner authors (a drained sub-agent failure here), and
nothing on a turn the person did not write.
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

SLOT_NAME = "sign-in-chat"
TEXT = "Pull the latest frame from Figma."


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
    return state, provider


async def _asks(tmp_path, monkeypatch, *, message=TEXT, failures=(), **run_kwargs) -> list[str]:
    state, provider = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    slot._pending_subagent_failures = list(failures)
    slot.append("user", message)
    run_kwargs.setdefault("_directive_user_origin", True)
    await asyncio.wait_for(chat_runner._run_chat(state, slot, message, **run_kwargs), 30)
    await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)
    return [call.args[0] for call in provider.note_user_message.call_args_list]


@pytest.mark.asyncio
async def test_a_generated_turn_without_an_actor_hands_over_nothing(tmp_path, monkeypatch):
    asks = await _asks(
        tmp_path,
        monkeypatch,
        message="Spec documents are already written at /workspace/notion/copy.",
        _directive_user_origin=False,
    )
    assert asks == []


@pytest.mark.asyncio
async def test_a_provider_without_note_user_message_can_complete_a_user_turn(tmp_path, monkeypatch):
    state, provider = _turn_state(tmp_path, monkeypatch)
    del provider.note_user_message
    assert not hasattr(provider, "note_user_message")
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.append("user", TEXT)

    await asyncio.wait_for(
        chat_runner._run_chat(state, slot, TEXT, _directive_user_origin=True), 30
    )
    await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)

    assert not [row for row in slot.messages if row.get("role") == "error"]
    assert [row["content"] for row in slot.messages if row.get("role") == "assistant"] == ["Done."]


@pytest.mark.asyncio
async def test_a_user_turn_hands_over_the_typed_message(tmp_path, monkeypatch):
    assert await _asks(tmp_path, monkeypatch) == [TEXT]


@pytest.mark.asyncio
async def test_an_expanded_prompt_server_name_is_not_handed_over(tmp_path, monkeypatch):
    typed = "@release-checklist"
    expand = AsyncMock(return_value=("Read the release checklist from Notion.", "ok"))
    monkeypatch.setattr(chat_runner, "_expand_prompt_mention_off_loop", expand)

    asks = await _asks(tmp_path, monkeypatch, message=typed)

    expand.assert_awaited_once()
    assert expand.await_args.args[0] == typed
    assert asks == [typed]


@pytest.mark.asyncio
async def test_a_prepended_sub_agent_failure_is_not_handed_over(tmp_path, monkeypatch):
    asks = await _asks(
        tmp_path, monkeypatch, failures=["Sub-agent failed: notion requires authentication"]
    )
    assert asks == [TEXT]


@pytest.mark.asyncio
async def test_a_self_wake_turn_hands_over_nothing(tmp_path, monkeypatch):
    asks = await _asks(
        tmp_path,
        monkeypatch,
        message="Read the release checklist from Notion.",
        _directive_self_wake=True,
    )
    assert asks == []


@pytest.mark.asyncio
async def test_a_reentered_prompt_turn_hands_over_nothing(tmp_path, monkeypatch):
    asks = await _asks(
        tmp_path,
        monkeypatch,
        message="Read the release checklist from Notion.",
        _prompt_depth=1,
    )
    assert asks == []


@pytest.mark.parametrize("actor", ["subagent", "cron"])
@pytest.mark.asyncio
async def test_a_turn_the_person_did_not_write_hands_over_nothing(tmp_path, monkeypatch, actor):
    asks = await _asks(tmp_path, monkeypatch, _directive_user_origin=False, _turn_actor=actor)
    assert asks == []
