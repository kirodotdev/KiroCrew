"""The per-turn guidance offers ``suggest_followup`` only where the applier admits it.

The applier refuses ``suggest_followup`` on a turn no person sent (a cron, a
loop's wake, a sub-agent, a peer send such as a crewmate's hand-off), even with
the tab open. The guidance asked only whether a tab was open, so those turns
were told to call a tool that was then refused. Both now read one
predicate, ``kiro_crew.directive_origin.user_origin_gate_admits``, and a caller
that does not name the turn's provenance is treated as no person's turn.

The parity case runs the real applier and the real guidance on the same inputs.
The runner case drives the real dashboard runner with a real ``build_message``
and reads the prompt it built.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state, drain_background_tasks

from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import turn
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.usefixtures("close_skills_loaders")

#: Ceiling for a wait that only fails on a hang, so a regression fails by name.
_HANG_CEILING_S = 30
_REFUSAL = "only works from a turn a person sent"


def _offers_followup(session_key: str, *, person: bool) -> bool:
    paragraphs = turn.interactive_guidance(
        interactive=True,
        session_key=session_key,
        agent=None,
        minimal_context=True,
        producer_is_user_facing=person,
    )
    return any("suggest_followup" in p for p in paragraphs)


@pytest.mark.asyncio
@pytest.mark.parametrize("person", [True, False], ids=["person-turn", "automation-turn"])
async def test_guidance_matches_the_applier(person: bool) -> None:
    session_key = "dashboard:chat-1"
    slot = MagicMock()
    slot.key = session_key
    result = await apply_session_directive(
        None,
        slot,
        session_key,
        "suggest_followup",
        {"items": [{"title": "t", "prompt": "p"}]},
        producer_is_user_facing=person,
    )
    refused = _REFUSAL in str(result)
    assert refused is (not person)
    assert _offers_followup(session_key, person=person) is (not refused)


def test_automation_turn_keeps_the_other_dashboard_guidance() -> None:
    paragraphs = turn.interactive_guidance(
        interactive=True,
        session_key="dashboard:chat-1",
        agent=None,
        minimal_context=True,
        producer_is_user_facing=False,
    )
    assert any("ask_question" in p for p in paragraphs)
    assert any("[OPTIONS:" in p for p in paragraphs)
    assert not any("suggest_followup" in p for p in paragraphs)


def test_a_caller_that_does_not_say_is_not_offered_the_card(tmp_path, monkeypatch) -> None:
    # A channel turn on a session whose tab is open builds its prompt without
    # naming the turn's provenance. Its directive reaches the applier with no
    # slot and no person mark, so the card would be refused.
    from kiro_crew import session_surface

    monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset({"slack:C1:1.0"}))
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    msg, _ = builder.build_message(
        "done", is_new_session=False, interactive=True, session_key="slack:C1:1.0"
    )
    assert "ask_question" in msg
    assert "suggest_followup" not in msg


SLOT_NAME = "followup-gate"
TEXT = "finish the brief"


def _turn_state(tmp_path, monkeypatch) -> tuple[Any, list[str]]:
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "workspace"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )
    state = _make_state(tmp_path, context_builder=builder)
    built: list[str] = []
    real_build = builder.build_message

    def _build(*args: Any, **kwargs: Any) -> Any:
        out = real_build(*args, **kwargs)
        built.append(out[0])
        return out

    state.context_builder.build_message = MagicMock(side_effect=_build)
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
    return state, built


@pytest.mark.asyncio
@pytest.mark.parametrize("person", [True, False], ids=["person-turn", "automation-turn"])
async def test_runner_prompt_follows_who_sent_the_turn(tmp_path, monkeypatch, person: bool):
    state, built = _turn_state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot(SLOT_NAME)
    slot.append("user", TEXT)
    await asyncio.wait_for(
        chat_runner._run_chat(state, slot, TEXT, _directive_user_origin=person),
        _HANG_CEILING_S,
    )
    await finished(drain_background_tasks(state), "draining the background tasks", timeout=10)
    assert built, "the runner built no prompt"
    prompt = built[-1]
    assert "ask_question" in prompt
    assert ("suggest_followup" in prompt) is person
    # The runner hands the builder the same provenance the applier reads.
    call = state.context_builder.build_message.call_args
    assert call.kwargs.get("directive_user_origin") is person
