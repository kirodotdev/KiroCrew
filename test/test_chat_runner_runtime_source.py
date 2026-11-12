"""A dashboard slot's turn names the interface its words were typed in.

A message typed in a linked Slack thread runs in the dashboard slot, but its reader
is in Slack. ``_turn_build_prompt`` must hand ``build_message`` ``runtime_source``
``slack`` for it, so ``[RUNTIME]`` and the source-keyed rules match the surface
that reads the reply; a turn typed in the dashboard keeps ``dashboard``.

Tests drive the REAL runner and read the keyword ``build_message`` received: a
helper test cannot see a hard-coded value at the call site.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard import chat_runner
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import finished

SLOT_NAME = "linked-thread-chat"


def _turn_state(tmp_path, monkeypatch):
    """A DashboardState whose only mocked seams are the provider and the builder."""
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


async def _runtime_source_of_turn(state, **dispatch) -> str:
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.append("user", "What changed?")
    await finished(chat_runner._run_chat(state, slot, "What changed?", **dispatch), "the turn")
    await finished(drain_background_tasks(state), "draining the background tasks")
    state.context_builder.build_message.assert_called()
    return state.context_builder.build_message.call_args.kwargs["runtime_source"]


@pytest.mark.asyncio
async def test_a_message_typed_in_a_linked_slack_thread_runs_as_slack(tmp_path, monkeypatch):
    state = _turn_state(tmp_path, monkeypatch)
    source = await _runtime_source_of_turn(
        state,
        _directive_user_origin=True,
        _directive_channel_origin=True,
        _directive_source_channels=("C0LINKED",),
    )
    assert source == "slack"


@pytest.mark.asyncio
async def test_a_message_typed_in_the_dashboard_runs_as_dashboard(tmp_path, monkeypatch):
    state = _turn_state(tmp_path, monkeypatch)
    source = await _runtime_source_of_turn(state, _directive_user_origin=True)
    assert source == "dashboard"


@pytest.mark.asyncio
async def test_a_channel_hand_off_without_a_slack_channel_stays_dashboard(tmp_path, monkeypatch):
    # The other channel hand-offs stamp no channel type on the turn, so the
    # runner does not guess one.
    state = _turn_state(tmp_path, monkeypatch)
    source = await _runtime_source_of_turn(
        state, _directive_user_origin=True, _directive_channel_origin=True
    )
    assert source == "dashboard"
