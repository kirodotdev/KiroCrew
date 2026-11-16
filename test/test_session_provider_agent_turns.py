"""``AcpSessionProvider``'s agent-started turn calls (``session_provider_agent_turns``).

The watch is kept on the provider and put back on a replacement handle, and an
unsolicited turn is streamed under the chat's shared-turn claim, which is ended
whether the stream finishes or is closed early.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.acp.session_provider import AcpSessionProvider


class _Handle:
    def __init__(self, events: list[Any] | None = None, pending: bool = False) -> None:
        self.watched: list[Any] = []
        self._events = events or []
        self._pending = pending
        self.timeouts: list[Any] = []

    def watch_agent_turns(self, callback: Any) -> bool:
        self.watched.append(callback)
        return True

    def agent_turn_pending(self) -> bool:
        return self._pending

    async def stream_unsolicited(self, timeout: Any = None):
        self.timeouts.append(timeout)
        for event in self._events:
            yield event


def _provider(handle: _Handle) -> Any:
    provider = object.__new__(AcpSessionProvider)
    provider._handle = handle
    provider.claims = []
    provider.ended = []

    def _claim() -> str:
        provider.claims.append("claim")
        return "claim"

    async def _end(claim: Any) -> None:
        provider.ended.append(claim)

    provider._claim_shared_turn = _claim
    provider._end_shared_turn = _end
    return provider


def _callback() -> None:
    return None


def test_the_watch_follows_a_replacement_handle():
    first = _Handle()
    provider = _provider(first)
    assert provider.watch_agent_turns(_callback) is True
    assert first.watched == [_callback]
    second = _Handle()
    provider._handle = second
    provider._rewatch_agent_turns()
    assert second.watched == [_callback]


def test_no_watch_is_put_back_when_none_was_registered():
    handle = _Handle()
    provider = _provider(handle)
    provider._rewatch_agent_turns()
    assert handle.watched == []


def test_pending_is_the_handles_answer():
    assert _provider(_Handle(pending=True)).agent_turn_pending() is True
    assert _provider(_Handle(pending=False)).agent_turn_pending() is False


def test_an_unsolicited_turn_streams_under_the_claim_and_ends_it():
    handle = _Handle(events=["a", "b"])
    provider = _provider(handle)

    async def _go() -> list[Any]:
        return [event async for event in provider.stream_unsolicited(7)]

    assert asyncio.run(_go()) == ["a", "b"]
    assert handle.timeouts == [7]
    assert provider.claims == ["claim"]
    assert provider.ended == ["claim"]


def test_a_stream_closed_early_still_ends_the_claim():
    provider = _provider(_Handle(events=["a", "b"]))

    async def _go() -> None:
        stream = provider.stream_unsolicited()
        assert await stream.__anext__() == "a"
        await stream.aclose()

    asyncio.run(_go())
    assert provider.ended == ["claim"]
