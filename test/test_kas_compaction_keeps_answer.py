"""A harness that compacts on its own after answering keeps that answer.

KAS summarizes mid-turn once the context crosses its threshold, and it can do so
AFTER the model already answered the prompt: the live capture
(``acp_frames/kas/auto-compaction-live.jsonl``) streams "pear", then
``summarization_started`` / ``summarization_completed``, then the engine's own
continuation ("There's nothing left to continue..."). The answer that streamed
before the compaction is the reply to the user's prompt and must reach the
transcript; dropping it leaves the prompt looking unanswered.
"""

from __future__ import annotations

import pathlib

import pytest
from test_post_compaction_continuation import _assistant_transcript, _drive_compaction_turn

from kiro_crew.acp.types import (
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)

_ANSWER = "FILL7"
_AFTER = "There's nothing pending. The last request was FILL7, and I've already answered it."


@pytest.mark.asyncio
async def test_an_answer_streamed_before_a_real_compaction_is_kept(
    tmp_path: pathlib.Path,
) -> None:
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=_AFTER),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]
    _state, slot, continued = await _drive_compaction_turn(tmp_path, events)

    transcript = _assistant_transcript(slot)
    assert _ANSWER in transcript.replace(_AFTER, ""), slot.messages
    assert _AFTER in transcript
    assert not continued


@pytest.mark.asyncio
async def test_kiro_cli_order_saves_each_text_once(tmp_path: pathlib.Path) -> None:
    """kiro-cli compacts when a request overflows, which is the request after a
    tool result: the narration before the tool was already cut at the tool call,
    so nothing is pending at the terminal and the answer kiro-cli produces after
    re-sending is saved once, with no fragment row ahead of it."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL, EVENT_TOOL_RESULT

    narration = "Let me read the notes first."
    answer = "The notes say the deploy is on Friday."
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=narration),
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            title="Reading notes",
            tool_kind="read",
            tool_input="{}",
            tool_call_id="read-1",
            tool_name="fs_read",
            tool_identity_trusted=True,
        ),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="read-1", tool_output="ok", tool_final=True),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="started"),
        AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="summary"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=answer),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
    ]
    _state, slot, continued = await _drive_compaction_turn(tmp_path, events)

    transcript = _assistant_transcript(slot)
    assert transcript.count(narration) == 1, slot.messages
    assert transcript.count(answer) == 1, slot.messages
    assert transcript.index(narration) < transcript.index(answer)
    assert not continued
