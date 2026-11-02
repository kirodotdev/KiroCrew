"""The dashboard runner notes a turn that ran to its end, as every engine does.

A person typing in a cron job's or a channel conversation's tab runs the dashboard
runner on that parent's session, and a ``spawn_sub_agents`` result that turn reads
back settles when the turn gives the session back (``kiro_crew.parent_turns``). The
runner notes the landing only for a turn that ended on ``end_turn``, from the task
that holds the session's permit: that release confirms the marks, so the children's
completions play no second turn. Any other end notes nothing, so the release revokes
the marks and the completions are delivered.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from chat_runner_coverage_helpers import _complete, _drive, _runner_state, _set_stream, _slot

from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.providers.base import LLMEvent


def _answer() -> LLMEvent:
    return LLMEvent(kind=EVENT_TEXT_CHUNK, text="the answer")


def _record(state: Any, client: Any) -> dict[str, list[Any]]:
    """Record each claim and each landing note with the task that made it and the
    number of prompts sent so far, so a note is tied to the prompt it follows."""
    seen: dict[str, list[Any]] = {"claims": [], "notes": []}
    claimed = state.sessions.get_or_create.return_value

    async def _get_or_create(key: str, *_a: Any, **_kw: Any) -> Any:
        seen["claims"].append((key, asyncio.current_task()))
        return claimed

    def _note(key: str) -> None:
        seen["notes"].append((key, asyncio.current_task(), client.stream.call_count))

    state.sessions.get_or_create.side_effect = _get_or_create
    state.sessions.note_turn_landed.side_effect = _note
    return seen


@pytest.mark.asyncio
async def test_a_turn_that_ends_on_end_turn_notes_its_landing_from_the_claiming_task(
    tmp_path: Any,
) -> None:
    state, client = _runner_state(tmp_path)
    seen = _record(state, client)
    _set_stream(client, [_answer(), _complete("end_turn")])

    await _drive(state, _slot("chat-landed"))

    key, claimed_by = seen["claims"][0]
    first_prompt = [(k, task) for k, task, prompts in seen["notes"] if prompts == 1]
    assert first_prompt == [(key, claimed_by)], (
        f"the runner did not note the landing once, from the task holding {key!r}: "
        f"{seen['notes']!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "events",
    [
        [_answer(), _complete("cancelled")],
        [_answer(), _complete("refusal")],
        [_answer(), _complete("max_tokens")],
        [_answer(), _complete("error: tool stall")],
        [_answer()],
    ],
    ids=["cancelled", "refusal", "max_tokens", "tool_stall", "no_completion"],
)
async def test_a_turn_that_did_not_end_on_end_turn_notes_nothing(
    tmp_path: Any, events: list[LLMEvent]
) -> None:
    state, client = _runner_state(tmp_path)
    seen = _record(state, client)
    _set_stream(client, events)

    await _drive(state, _slot("chat-unlanded"))

    assert seen["claims"], "the turn never claimed its session"
    first_prompt = [note for note in seen["notes"] if note[2] == 1]
    assert (
        first_prompt == []
    ), f"a turn that ended on {events[-1].stop_reason or 'no completion'!r} noted a landing"
