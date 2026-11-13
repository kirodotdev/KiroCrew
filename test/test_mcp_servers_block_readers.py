"""The ``[MCP SERVERS]`` block reaches the model and nobody else.

It is context on the turn's rail, like ``[RESOURCES]``: built into the prompt the
provider is sent, never shown to a person and never stored as their words. One
real dashboard turn (``turn_harness.run_turn``) with a real ``ContextBuilder``,
then each reader of a turn is checked against what the turn left behind:

* the dashboard transcript and a Bot's chat draw the slot's rows and the frames
  pushed to the browser;
* history replay, session summaries, the title request and memory extraction
  read the rows the turn wrote to history;
* a channel mirror posts the slot's rows;
* the crew log records the turn's events.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from turn_harness import TurnRecord, TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly.mcp_servers import MCP_SERVERS_HEADER

pytestmark = pytest.mark.usefixtures("ample_host_resources")

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="GitHub is still starting, one moment.")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_ASK = "list my open pull requests on github"
#: Words only the block carries, so a leak is found whichever part of it leaked.
_BLOCK_WORDS = (
    MCP_SERVERS_HEADER,
    "MCP_SERVER_STATES",
    "still starting: github",
    "mcp_server_wait",
)


def _with_a_server_still_starting(ctx: Any) -> None:
    builder = ContextBuilder()
    builder.mcp_states_for_session = lambda key: {"connecting": ("github",)}
    ctx.state.context_builder = builder


async def _turn() -> TurnRecord:
    return await run_turn(
        TurnScript(events=[_REPLY, _DONE], message=_ASK, setup=_with_a_server_still_starting)
    )


def _leaks(value: Any) -> list[str]:
    text = json.dumps(value, default=str, ensure_ascii=False)
    return [word for word in _BLOCK_WORDS if word in text]


@pytest.mark.asyncio
async def test_the_model_is_told():
    turn = await _turn()
    [(prompt,)] = [call.args for call in turn.calls("stream")]

    assert prompt.index(MCP_SERVERS_HEADER) < prompt.index(_ASK)
    assert "still starting: github" in prompt
    assert prompt.rstrip().endswith(_ASK)


@pytest.mark.asyncio
async def test_the_dashboard_transcript_and_a_bots_chat_show_the_users_own_words():
    """Both draw the slot's rows: the user's row is exactly what they typed."""
    turn = await _turn()
    [user_row] = [row for row in turn.window if row.get("role") == "user"]

    assert user_row["content"] == _ASK
    assert _leaks(list(turn.window)) == []


@pytest.mark.asyncio
async def test_nothing_pushed_to_the_browser_carries_it():
    turn = await _turn()
    frames = [(frame.channel, frame.kind, frame.payload) for frame in turn.ws_frames]

    assert frames, "the turn pushed nothing, so this would pass for no reason"
    assert _leaks(frames) == []


@pytest.mark.asyncio
async def test_history_holds_the_users_words_only():
    """What history replay, a session summary, the title request and memory
    extraction read: the rows the turn wrote to history."""
    turn = await _turn()
    assert [row["content"] for row in turn.history_rows if row.get("role") == "user"] == [_ASK]
    assert _leaks(list(turn.history_rows)) == []


@pytest.mark.asyncio
async def test_a_channel_mirror_has_nothing_of_it_to_post():
    """A mirror posts the slot's user and assistant rows."""
    turn = await _turn()
    mirrored = [row for row in turn.window if row.get("role") in ("user", "assistant")]

    assert mirrored
    assert _leaks(mirrored) == []


@pytest.mark.asyncio
async def test_the_crew_log_and_the_audit_log_do_not_record_it():
    turn = await _turn()
    recorded = [(call.name, call.args, dict(call.kwargs)) for call in turn.crew_log]
    audited = [(call.name, call.args, dict(call.kwargs)) for call in turn.audit_events]

    assert _leaks(recorded) == []
    assert _leaks(audited) == []
