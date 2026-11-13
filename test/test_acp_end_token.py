"""The quiet-end token is dropped where the model's text enters Kiro Crew.

After ``nothing_to_do`` the model replies with exactly
``session_directive.QUIET_END_TOKEN``, so the backend's history ends on an
assistant message. ``acp.end_token.drop_end_token`` takes that reply out of the
event stream; these tests pin which text it drops, which it releases untouched,
and that ``AcpClient`` reads its turn loop through it (the session handle is
pinned in ``test_acp_runtime_dispatch_tools.py``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.end_token import drop_end_token
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.session_directive import QUIET_END_TOKEN


def _text(text: str, message_id: str = "m1") -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text, message_id=message_id)


def _filtered(events: list[AcpEvent]) -> list[AcpEvent]:
    async def _source():
        for event in events:
            yield event

    async def _collect():
        return [event async for event in drop_end_token(_source())]

    return asyncio.run(_collect())


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param([QUIET_END_TOKEN], id="whole"),
        pytest.param([QUIET_END_TOKEN[:3], QUIET_END_TOKEN[3:]], id="split"),
        pytest.param(["\n", QUIET_END_TOKEN, " \n"], id="padded"),
    ],
)
def test_a_segment_that_is_the_token_is_dropped(chunks):
    complete = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")
    assert _filtered([*(_text(c) for c in chunks), complete]) == [complete]


def test_text_that_only_starts_like_the_token_is_released_unchanged():
    """A reply that begins with the token's first characters is held only
    until it differs, then every chunk goes out as it came, in order, with its
    message id; text later in that segment is not held again."""
    events = [
        _text(QUIET_END_TOKEN[:3], "m1"),
        _text("ick note", "m2"),
        _text(QUIET_END_TOKEN, "m3"),
        AcpEvent(kind=EVENT_COMPLETE),
    ]
    assert _filtered(events) == events


def test_each_segment_between_other_events_is_judged_on_its_own():
    """A token before a tool call is dropped and the call passes; a segment that
    stopped matching does not keep the next one from matching; a token prefix
    left at a boundary is released; a token at the end of the stream is dropped."""
    call = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="tc1")
    checking, prefix = _text("checking", "m1"), _text(QUIET_END_TOKEN[:3], "m3")
    events = [checking, call, _text(QUIET_END_TOKEN, "m2"), call, prefix, call]
    assert _filtered([*events, _text(QUIET_END_TOKEN, "m4")]) == [
        checking,
        call,
        call,
        prefix,
        call,
    ]


@pytest.mark.asyncio
async def test_a_token_only_reply_reaches_no_consumer_of_the_client_loop():
    """``AcpClient`` reads its turn loop through the drop: a prompt turn whose
    reply is only the token yields no text."""
    client = AcpClient()
    chunk = JsonRpcMessage(
        method="session/update",
        params={
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": QUIET_END_TOKEN},
            }
        },
    )

    async def fake_prompt_loop(req_id, timeout):
        yield "update", chunk
        yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

    client.ensure_ready = AsyncMock()
    client._send_prompt = AsyncMock(return_value=1)
    client._prompt_loop = fake_prompt_loop
    client._read_new_tool_results_sync = lambda: []

    kinds = [ev.kind async for ev in client.stream_events("patrol")]

    assert kinds == [EVENT_COMPLETE]
