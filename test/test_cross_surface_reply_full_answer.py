"""The channel-neutral reply mirror sends the turn's WHOLE answer.

A dashboard slot linked to a non-Slack channel (Telegram, Discord, Teams...) gets
each completed reply through ``_deliver_cross_surface_reply``. A tool boundary
flushes ``turn.assistant_text`` to a finalized reply row and resets it, so a turn
that answers BEFORE its last tool call keeps only the fragment after that call in
it, or nothing. Handing the leg that accumulator dropped the answer on the linked
channel while the dashboard kept it. These drive a REAL turn through
``turn_harness.run_turn`` and record the text the leg is handed, then check the
OPTIONS rule through the real egress composition.
"""

from __future__ import annotations

from typing import Any

import pytest
from turn_harness import TurnScript, run_turn

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import chat_runner
from kiro_crew.messaging.egress import channel_egress_parts
from kiro_crew.messaging.transport import TransportCapabilities

_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_ANSWER = "Here is the full analysis the user asked for."


def _tool(n: int) -> AcpEvent:
    return AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=f"t{n}", title=f"read_{n}", tool_kind="read")


async def _mirrored_text(monkeypatch: pytest.MonkeyPatch, events: list[AcpEvent]) -> list[str]:
    """Run one turn and return every text the channel-neutral leg was handed."""
    handed: list[str] = []

    async def _record(state: Any, session_key: str, text: str, *, slot: Any = None) -> None:
        handed.append(text)

    monkeypatch.setattr(chat_runner, "_deliver_cross_surface_reply", _record)
    await run_turn(TurnScript(events=[*events, _DONE]))
    return handed


class TestTheLegIsHandedTheWholeAnswer:
    @pytest.mark.asyncio
    async def test_an_answer_before_the_last_tool_call_reaches_the_leg(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handed = await _mirrored_text(
            monkeypatch,
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER),
                _tool(1),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
            ],
        )
        assert len(handed) == 1
        assert _ANSWER in handed[0]
        assert "Done." in handed[0]

    @pytest.mark.asyncio
    async def test_an_answer_wholly_before_a_textless_last_tool_still_reaches_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # assistant_text is empty at the end of this turn, and the leg sends
        # nothing for empty text: the answer has to come from the reply rows.
        handed = await _mirrored_text(
            monkeypatch, [AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER), _tool(1)]
        )
        assert handed and _ANSWER in handed[0]

    @pytest.mark.asyncio
    async def test_a_single_segment_answer_is_handed_over_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handed = await _mirrored_text(
            monkeypatch, [_tool(1), AcpEvent(kind=EVENT_TEXT_CHUNK, text=_ANSWER)]
        )
        assert handed == [_ANSWER]


class TestOnlyTheFinalAskIsOffered:
    def test_an_earlier_segments_trailer_is_stripped_and_the_final_one_kept(self) -> None:
        text = chat_runner._cross_surface_reply_text(
            ["Which one?\n\n[OPTIONS: Alpha | Beta]", "Final.\n\n[OPTIONS: Gamma | Delta]"],
            "Final.\n\n[OPTIONS: Gamma | Delta]",
        )
        assert text == "Which one?\n\nFinal.\n\n[OPTIONS: Gamma | Delta]"

    def test_a_segment_holding_only_a_trailer_leaves_no_blank_gap(self) -> None:
        text = chat_runner._cross_surface_reply_text(["[OPTIONS: Alpha | Beta]", "Done."], "")
        assert text == "Done."

    def test_with_no_row_named_the_trailing_text_is_used(self) -> None:
        assert chat_runner._cross_surface_reply_text([], "tail") == "tail"

    @pytest.mark.asyncio
    async def test_the_egress_numbers_the_final_choices_and_never_the_earlier_ones(self) -> None:
        text = chat_runner._cross_surface_reply_text(
            ["Which one?\n\n[OPTIONS: Alpha | Beta]", "Final.\n\n[OPTIONS: Gamma | Delta]"],
            "",
        )
        parts = await channel_egress_parts(text, TransportCapabilities(), number_options=True)
        sent = "\n".join(parts)
        assert "[OPTIONS:" not in sent
        assert "Gamma" in sent and "Delta" in sent
        assert "Alpha" not in sent and "Beta" not in sent
        assert "Which one?" in sent
