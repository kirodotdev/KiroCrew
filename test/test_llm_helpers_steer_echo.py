"""``stream_and_collect`` must surface the backend's ``steering_consumed`` echo.

Every caller that steers fakes this helper in its own tests, so without a test
against the REAL implementation the hook could be dead at runtime while all of
them stayed green — and a steer whose echo is never observed is requeued as a
duplicate question on every turn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from kiro_crew.acp.client import AcpError, AcpPromptBusy
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


class TestSteerConsumptionAcrossRetries:
    """A retry re-sends the original message WITHOUT the steer.

    Committing consumption mid-stream would mark a steer delivered that the model never saw —
    and consumption is what suppresses the requeue, so the question would be neither answered
    nor handed back.
    """

    @pytest.mark.asyncio
    async def test_a_transient_retry_does_not_commit_consumption(self, monkeypatch) -> None:
        consumed: list[str] = []

        class _Flaky:
            def __init__(self) -> None:
                self.attempts = 0

            async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
                self.attempts += 1
                if self.attempts == 1:
                    # The backend acknowledges the steer, then the stream dies retryably.
                    yield LLMEvent(kind=EVENT_STEER_CONSUMED, text="the steered question")
                    raise AcpError("503 service unavailable, please retry")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answer")
                yield LLMEvent(kind=EVENT_COMPLETE, text="")

            async def cancel(self) -> None:
                return None

        async def _instant(_seconds: float) -> None:
            return None

        monkeypatch.setattr("kiro_crew.llm_helpers.asyncio.sleep", _instant)

        provider = _Flaky()
        out = await stream_and_collect(
            provider, "original prompt", on_steer_consumed=consumed.append
        )

        assert provider.attempts == 2, provider.attempts
        assert out == "answer"
        # The steer was never replayed, so it must NOT count as consumed — staying unconsumed
        # is what lets the requeue return the question.
        assert consumed == [], consumed

    @pytest.mark.asyncio
    async def test_an_attempt_that_settles_does_commit_consumption(self) -> None:
        # Buffering must not swallow the normal case.
        consumed: list[str] = []

        class _Clean:
            async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
                yield LLMEvent(kind=EVENT_STEER_CONSUMED, text="the steered question")
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="answer")
                yield LLMEvent(kind=EVENT_COMPLETE, text="")

            async def cancel(self) -> None:
                return None

        out = await stream_and_collect(
            _Clean(), "original prompt", on_steer_consumed=consumed.append
        )

        assert out == "answer"
        assert consumed == ["the steered question"], consumed


@pytest.mark.asyncio
async def test_fatal_error_still_commits_a_consumed_steer() -> None:
    """A non-retried failure is terminal for the steer: the backend already consumed it.

    Discarding the acknowledgement makes the side cleanup requeue an already-delivered
    question, so the user is asked the same thing twice.
    """
    consumed: list[str] = []

    class FatalAfterConsume:
        async def stream(self, message: str):  # noqa: ANN202 - test double
            yield LLMEvent(kind=EVENT_STEER_CONSUMED, text="the steered question")
            raise AcpError("validation failed: bad request")

    with pytest.raises(AcpError):
        await stream_and_collect(
            FatalAfterConsume(),  # type: ignore[arg-type]
            "prompt",
            on_steer_consumed=consumed.append,
        )

    assert consumed == ["the steered question"]


@pytest.mark.asyncio
async def test_a_retry_still_discards_the_consumption() -> None:
    """The other half of the discriminator: a retry re-sends the prompt WITHOUT the steer.

    Committing there would mark a steer delivered that the retried attempt never carried.
    """
    consumed: list[str] = []

    class ConsumeThenBusyThenSucceed:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, message: str):  # noqa: ANN202 - test double
            self.calls += 1
            if self.calls == 1:
                yield LLMEvent(kind=EVENT_STEER_CONSUMED, text="dropped by the retry")
                raise AcpPromptBusy("prompt already in progress")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="second attempt")
            yield LLMEvent(kind=EVENT_COMPLETE, text="")

        async def cancel(self) -> None:
            return None

    out = await stream_and_collect(
        ConsumeThenBusyThenSucceed(),  # type: ignore[arg-type]
        "prompt",
        on_steer_consumed=consumed.append,
    )

    assert out == "second attempt"
    assert consumed == []
