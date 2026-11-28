"""A Stop on a fresh chat's first turn: the next prompt holds the stopped request once.

``_run_chat`` runs on a real ``SessionManager``, ``ConversationLog``, ``ContextBuilder``,
history replay and cancelled-turn restore. Only the ACP child is a script (``fake_provider``
with a scripted ``stream`` and a ``cancel`` that acks). The Stop is the dashboard's own
``stop_slot_turn``, fired while the turn streams, so the first-turn history debt, the
``prev_turn_cancelled`` flag and the transcript rows are what a real soft Stop leaves.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, run_as_slot_task
from real_session_manager import fake_provider, real_sessions

from kiro_crew.acp.types import STOP_REASON_CANCELLED
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_handlers import stop_slot_turn
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.history import ConversationLog
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader

REQUEST = "Please summarise ZZFIRSTREQ-71 for me"
PARTIAL = "Starting the summary"
STOP_NOTE = "[User stopped the previous turn mid-execution.]"
RESTORE_MARKER = "[PREVIOUS TURN WAS CANCELLED BY THE USER"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    """A fresh dashboard chat on a real session manager whose ACP child acks a cancel."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.title_then_refresh", AsyncMock())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.generate_session_summary", AsyncMock())
    log = ConversationLog(base_dir=tmp_path / "history")
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "memory"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        conversation_log=log,
    )
    state = _make_state(tmp_path, context_builder=builder)
    state.conversation_log = log
    hook_store = MagicMock()
    hook_store.fire = AsyncMock(return_value=[])
    state._hook_store = hook_store
    client = fake_provider()
    client.cancel = AsyncMock(return_value="acked")
    state.sessions = real_sessions(client)
    slot = state.get_or_create_slot("first-cancel")
    return SimpleNamespace(state=state, slot=slot, client=client)


async def _turn(chat, text: str, stream) -> None:
    """Send *text* the way the chat route does (the user row first), then run its turn."""
    chat.client.stream = stream
    chat.slot.append("user", text, "msg msg-u")
    await run_as_slot_task(chat.slot, _run_chat(chat.state, chat.slot, text))
    await asyncio.gather(*chat.state._background_tasks)


async def _stopped_turn(chat, text: str = REQUEST, partial: str = PARTIAL) -> None:
    """Run *text* as a turn the user stops once *partial* has streamed."""

    async def stream(_prompt, *_args, **_kwargs):
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=partial)
        await stop_slot_turn(chat.state, chat.slot)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)

    await _turn(chat, text, stream)
    key = effective_session_key(chat.slot)
    assert chat.state.sessions._sessions[key].prev_turn_cancelled is True, "the Stop did not land"


async def _answered_turn(chat, text: str, answer: str = "done") -> str:
    """Run *text* to a normal answer and return the prompt the provider received."""
    prompts: list[str] = []

    async def stream(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=answer)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    await _turn(chat, text, stream)
    assert len(prompts) == 1, "the turn must dispatch exactly one prompt"
    return str(prompts[0])


def _carriers(prompt: str) -> tuple[int, int]:
    """How often *prompt* carries the stopped request: as a replay row and as the restore.

    The prompt names the request a third time in the thread's own recent-activity line,
    which is neither of the two.
    """
    return prompt.count(f"\nUser: {REQUEST}"), prompt.count(f"Cancelled user request:\n{REQUEST}")


@pytest.mark.asyncio
async def test_a_stopped_first_turn_reaches_the_next_prompt_once(chat):
    """The replay a cold start rebuilds already carries the stopped turn."""
    await _stopped_turn(chat)
    key = effective_session_key(chat.slot)
    sessions = chat.state.sessions
    # The first-turn debt stays armed after the Stop, so the next prompt rebuilds
    # the replay from the transcript.
    assert sessions.first_turn_history_owed_pending(key) is True

    prompt = await _answered_turn(chat, "next")

    assert _carriers(prompt) == (1, 0), "the stopped request is sent twice"
    assert RESTORE_MARKER not in prompt
    assert prompt.count(STOP_NOTE) == 1, "the model is still told the turn was stopped"
    assert sessions._sessions[key].prev_turn_cancelled is False


@pytest.mark.asyncio
async def test_a_replay_that_lost_the_stopped_request_keeps_the_restore(chat):
    """A partial reply longer than the replay budget pushes the request row out of it.

    Nothing else names the request then, so the restore stays and names it once.
    """
    from kiro_crew.context_assembly.replay import _REPLAY_BUDGET_CHARS

    await _stopped_turn(chat, partial="x" * (_REPLAY_BUDGET_CHARS + 1))

    prompt = await _answered_turn(chat, "next")

    assert _carriers(prompt) == (0, 1)


@pytest.mark.asyncio
async def test_a_warm_stop_still_restores_the_stopped_turn(chat):
    """With no replay in the prompt, the restore is the only copy of the stopped turn."""
    await _answered_turn(chat, "an earlier question", "an earlier answer")
    key = effective_session_key(chat.slot)
    assert chat.state.sessions.first_turn_history_owed_pending(key) is False
    await _stopped_turn(chat)

    prompt = await _answered_turn(chat, "next")

    assert _carriers(prompt) == (0, 1)
    assert RESTORE_MARKER in prompt
