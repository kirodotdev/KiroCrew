"""A dashboard turn on a V2 content-filter refusal fallback.

On a refusal, kiro-cli streams the refused model's text, then a ``model_fallback``
hop (``cause: refused``), then the fallback model's answer
(``test/fixtures/acp_frames/kiro/refusal-fallback-live.jsonl``). The refused text must
reach neither the saved reply nor the ``chat_message`` frame that overwrites the
tab's streamed copy. When no model answers after the hop, the provider's own words are kept.
The native Slack turn follows the same rule (``test_slack_refusal_fallback.py``).
These drive the real ``_run_chat`` through ``turn_harness.run_turn``.
"""

from __future__ import annotations

import pytest
from turn_harness import Do, TurnContext, TurnScript, run_turn

from kiro_crew.acp.engine_fallback import ModelFallbackNotice
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_MODEL_FALLBACK,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    AcpEvent,
)

_REFUSED = "I can't help with that."
_ANSWER = "Here is the answer."


def _hop(*, promoted: bool = False) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_MODEL_FALLBACK,
        model_fallback=ModelFallbackNotice(
            "model-a", "model-b", "refused", promoted=promoted, engine="v2"
        ),
    )


def _tool() -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="read_1", tool_kind="read")


@pytest.mark.asyncio
async def test_the_refused_text_leaves_the_reply_and_the_fallback_answer_stays():
    # The window a tab reloading mid-turn is served, read right after the hop.
    window_after_hop: list[str] = []

    def _read_window(ctx: TurnContext) -> None:
        window_after_hop.extend(str(row.get("content", "")) for row in ctx.slot.messages)

    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REFUSED),
                _hop(),
                Do(_read_window),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
                # The promotion follows the answer on the wire; it drops nothing.
                _hop(promoted=True),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
            ]
        )
    )

    assert [row["content"] for row in record.rows("assistant")] == [_ANSWER]
    # The frame the tab replaces its streamed copy with.
    finals = [
        frame.payload["content"]
        for frame in record.frames("chat_message")
        if frame.payload.get("role") == "assistant"
    ]
    assert finals == [_ANSWER], finals
    assert not [content for content in window_after_hop if _REFUSED in content]


@pytest.mark.asyncio
async def test_a_refusal_the_fallback_did_not_recover_keeps_the_providers_words():
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REFUSED),
                _hop(),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_REFUSAL),
            ]
        )
    )

    assert [row["content"] for row in record.rows("assistant")] == [_REFUSED]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "after_hop, stop_reason",
    [
        # The fallback wrote, called a tool, then its next call was refused bare.
        (
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me check the file."), _tool()],
            STOP_REASON_REFUSAL,
        ),
        # The fallback answered with tool work alone.
        ([_tool()], STOP_REASON_END_TURN),
    ],
    ids=["answered_then_refused", "tool_only_answer"],
)
async def test_a_refusal_hop_the_fallback_answered_does_not_put_the_refused_text_back(
    after_hop: list[AcpEvent], stop_reason: str
):
    record = await run_turn(
        TurnScript(
            events=[
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_REFUSED),
                _hop(),
                *after_hop,
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
            ]
        )
    )

    assert not [row for row in record.rows("assistant") if _REFUSED in row["content"]]
