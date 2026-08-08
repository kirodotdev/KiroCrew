"""``stream_and_collect`` must surface the backend's ``steering_consumed`` echo.

Every caller that steers fakes this helper in its own tests, so without a test
against the REAL implementation the hook could be dead at runtime while all of
them stayed green — and a steer whose echo is never observed is requeued as a
duplicate question on every turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from kiro_crew.acp.types import EVENT_STEER_CONSUMED
from kiro_crew.llm_helpers import stream_and_collect
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent


class _ScriptedProvider:
    """Yields a fixed event script, like a backend mid-turn."""

    def __init__(self, events: list[LLMEvent]) -> None:
        self._events = events

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        for event in self._events:
            yield event


@pytest.mark.asyncio
async def test_the_steer_consumed_echo_reaches_the_callback():
    echo = "<user_message>\nuse QUIC\n</user_message>"
    provider = _ScriptedProvider(
        [
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="thinking"),
            LLMEvent(kind=EVENT_STEER_CONSUMED, text=echo),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text=" done"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[str] = []

    text = await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        retry_transient=False,
        on_steer_consumed=seen.append,
    )

    assert seen == [echo], "the steering_consumed echo never reached the caller"
    # The echo must not contaminate the collected reply text.
    assert text == "thinking done"


@pytest.mark.asyncio
async def test_an_echo_without_text_still_notifies():
    """An empty echo is meaningful — it means "settle everything" downstream — so
    it must be delivered rather than filtered out as falsy."""
    provider = _ScriptedProvider(
        [
            LLMEvent(kind=EVENT_STEER_CONSUMED, text=""),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )
    seen: list[str] = []

    await stream_and_collect(
        provider,  # type: ignore[arg-type]
        "q",
        retry_transient=False,
        on_steer_consumed=seen.append,
    )

    assert seen == [""]


@pytest.mark.asyncio
async def test_a_caller_that_passes_no_callback_is_unaffected():
    """The hook is optional: existing callers (cron, subagents, titling) pass
    nothing and must not trip over the event."""
    provider = _ScriptedProvider(
        [
            LLMEvent(kind=EVENT_STEER_CONSUMED, text="whatever"),
            LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
            LLMEvent(kind=EVENT_COMPLETE, text=""),
        ]
    )

    assert await stream_and_collect(provider, "q", retry_transient=False) == "ok"  # type: ignore[arg-type]
