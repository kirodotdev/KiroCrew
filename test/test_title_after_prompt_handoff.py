"""A first turn claims its session before its title reaches the background runtime.

The send that starts a session's first turn also starts the title attempt. The
attempt runs on the shared background runtime, whose start holds a lock the turn's
admission waits on, so a title that got there first held the turn back for a
process start. These tests run the real chat runner and the real title attempt
together and read the order off one journal: the turn's session claim, the title's
request to the background runtime, and the first step of the turn's stream.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from turn_harness import Do, TurnContext, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.dashboard import chat_title
from kiro_crew.testing.wait import await_until

_REPLY = AcpEvent(kind=EVENT_TEXT_CHUNK, text="the reply")
_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)


class _Send:
    """What the send handler does beside starting the turn, and one journal of the order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.journal: list[str] = []
        self.titled = asyncio.Event()

        async def _generate(_state: Any, _messages: Any, *, session_key: str = "") -> str:
            self.journal.append("title asks the background runtime")
            self.titled.set()
            return "Named"

        monkeypatch.setattr(chat_title, "_generate_title_via_kiro", _generate)
        monkeypatch.setattr(chat_title, "_persist_title", AsyncMock())
        monkeypatch.setattr(chat_title, "maybe_suggest_folder", AsyncMock())
        monkeypatch.setattr(chat_title, "_ticket_key_if_enabled", AsyncMock(return_value=""))
        monkeypatch.setattr(chat_title, "_reveal_title", AsyncMock())

    def setup(self, ctx: TurnContext) -> None:
        # The harness hands over an already-titled slot; this one is brand new.
        ctx.slot._titled = False
        claim = ctx.state.sessions.get_or_create

        async def _claim(*args: Any, **kwargs: Any) -> Any:
            self.journal.append("turn claims its session")
            return await claim(*args, **kwargs)

        ctx.state.sessions.get_or_create = _claim
        # Runs after the harness publishes the turn's task and before the turn's
        # first step: where the send handler starts the title attempt.
        asyncio.get_running_loop().call_soon(self._start_title, ctx)

    def _start_title(self, ctx: TurnContext) -> None:
        task = asyncio.ensure_future(chat_title._maybe_auto_title(ctx.state, ctx.slot))
        ctx.slot._title_task = task
        ctx.state._background_tasks.add(task)
        task.add_done_callback(ctx.state._background_tasks.discard)

    async def title_lands_during_the_stream(self, _ctx: TurnContext) -> None:
        # Bounded: a title that never starts raises here by name. The turn records that
        # as its error, so the test then fails on the journal, with this message in the log.
        await await_until(self.titled, what="the title's request to the background runtime")
        self.journal.append("turn's first stream step")


@pytest.mark.asyncio
async def test_the_title_asks_after_the_claim_and_lands_during_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    send = _Send(monkeypatch)

    record = await run_turn(
        TurnScript(
            events=[Do(send.title_lands_during_the_stream), _REPLY, _DONE],
            setup=send.setup,
        )
    )

    assert send.journal == [
        "turn claims its session",
        "title asks the background runtime",
        "turn's first stream step",
    ]
    assert [row["content"] for row in record.rows("assistant")] == ["the reply"]
    assert record.window[0]["role"] == "user"


@pytest.mark.asyncio
async def test_a_turn_whose_session_cannot_start_is_titled_once_it_has_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No handoff ever comes, and the session is still named from the person's message."""
    send = _Send(monkeypatch)

    await run_turn(
        TurnScript(
            events=[_REPLY, _DONE],
            setup=send.setup,
            allocation_error=RuntimeError("the session could not start"),
        )
    )

    assert send.journal == ["turn claims its session", "title asks the background runtime"]
