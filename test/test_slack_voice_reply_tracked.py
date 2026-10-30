"""The fire-and-forget voice reply is held and cancelled like every other background task.

``_reply_by_voice`` hands synthesis to a task it never awaits. The event loop keeps only
a weak reference to a task, so an untracked one can be collected mid-synthesis, and
``cancel_background_tasks`` cannot stop it at gateway shutdown. Both tests park the reply
on an event so the in-flight state is observable without a sleep; every wait the test
must unblock is bounded by the shared lost-run ceiling, so a reply that never starts
fails by name instead of hanging the worker.
"""

from __future__ import annotations

import asyncio

import pytest
from slack_handler_helpers import _within_lost_run

from kiro_crew.slack import handler as h

_LONG_ANSWER = "This reply is comfortably longer than the fifty-character voice threshold."


@pytest.fixture
def parked_reply(monkeypatch):
    """Route ``_reply_by_voice`` into a reply that waits on *gate* until released."""
    monkeypatch.setattr(h, "_vc", h._VoiceConfig(global_enabled=True, provider="polly"))
    monkeypatch.setattr(h, "_tts_available", lambda **kw: True)
    # A private set, so tasks other tests leave behind do not count here.
    monkeypatch.setattr(h, "_background_tasks", set())
    gate = asyncio.Event()
    started = asyncio.Event()
    replies: list[asyncio.Task] = []

    async def _parked(slack, channel, reply_ts, text, **kw):
        task = asyncio.current_task()
        assert task is not None
        replies.append(task)
        started.set()
        await gate.wait()

    monkeypatch.setattr(h, "_safe_voice_reply", _parked)
    return gate, started, replies


async def _speak() -> None:
    await h._reply_by_voice(
        object(), "C1", "1.0", "U1", "session-1", _LONG_ANSWER, _LONG_ANSWER, False
    )


async def _started(started: asyncio.Event) -> None:
    await _within_lost_run(started.wait(), "the voice reply's start")


async def _drain(gate: asyncio.Event, replies: list[asyncio.Task]) -> None:
    gate.set()
    await _within_lost_run(
        asyncio.gather(*replies, return_exceptions=True), "the released voice reply"
    )


@pytest.mark.asyncio
async def test_in_flight_voice_reply_is_tracked_until_it_finishes(parked_reply):
    gate, started, replies = parked_reply
    try:
        await _speak()
        await _started(started)
        assert len(replies) == 1
        assert h._background_tasks == {replies[0]}
    finally:
        await _drain(gate, replies)
    assert h._background_tasks == set()


@pytest.mark.asyncio
async def test_shutdown_cancels_an_in_flight_voice_reply(parked_reply):
    gate, started, replies = parked_reply
    try:
        await _speak()
        await _started(started)
        h.cancel_background_tasks()
        # Read the request before awaiting: an uncancelled reply would park forever.
        assert replies[0].cancelling() == 1
    finally:
        await _drain(gate, replies)
    assert replies[0].cancelled()
