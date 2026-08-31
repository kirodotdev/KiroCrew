"""A prompt-busy channel session is replaced, not papered over with a card.

When the ACP backend still holds an in-flight prompt it rejects every later
prompt on that session with ``already in progress``. Answering that with a
generic ``An error occurred`` card and returning without touching the session
leaves the wedge in place: the agent then rejects every subsequent @mention
identically, and the only recovery is a manual per-agent clear-context.

``AcpPromptBusy``'s own contract is that callers reset the session so the next
message cold-starts. The dashboard (``chat_runner``) and Slack (``handler``)
paths honour it, and this file pins the channel loop onto the same contract.

Bounded failure matters as much as the recovery: if the wedge survives the
reset the agent is reported ONCE and stops consuming its inbox, and the
abandoned replacement session is torn back down -- ``channel:``-keyed sessions
are exempt from both session reapers, and ``api_channel_wake_agent`` would
otherwise re-acquire the same wedged session out of the registry.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpError, AcpPromptBusy
from kiro_crew.channel import (
    Channel,
    _reacquire_cleared_session,
    _recover_busy_agent,
    _reset_busy_session,
    _stream_task,
    run_channel_agent,
)
from kiro_crew.llm_helpers import is_prompt_busy
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:c1:a1",
        approval_policy=SimpleNamespace(value="writes"),
        _approval_future=None,
    )


def _make_channel():
    ch = SimpleNamespace(id="c1", trusted=False, members={})
    ch._broadcast = MagicMock()
    ch.post = AsyncMock()
    return ch


def _raising_client(exc: BaseException, entered: asyncio.Event | None = None):
    client = SimpleNamespace()

    async def _stream(message):
        if entered is not None:
            entered.set()
        raise exc
        yield  # pragma: no cover - makes _stream an async generator

    client.stream = _stream
    return client


def _wedged_client(entered: asyncio.Event):
    """A client that signals ``entered``, then rejects the prompt as still busy."""
    return _raising_client(AcpPromptBusy("prompt already in progress"), entered=entered)


def _text_client(text: str, seen: list[str] | None = None):
    client = SimpleNamespace()

    async def _stream(message):
        if seen is not None:
            seen.append(message)
        yield SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text)
        yield SimpleNamespace(kind=EVENT_COMPLETE)

    client.stream = _stream
    return client


class FakeSessions:
    """Minimal ``SessionManager`` stand-in for the channel agent loop."""

    def __init__(
        self,
        clients: list[Any],
        reset_exc: BaseException | None = None,
        reset_exc_after: int = 0,
    ):
        self._sessions: dict[str, Any] = {}
        self._clients = list(clients)
        self.resets: list[tuple[str, Any]] = []
        self.acquires: list[str] = []
        self.released: list[str] = []
        self.lifecycle_marked: list[str] = []
        self.turn_active_calls: list[tuple[str, bool]] = []
        self._reset_exc = reset_exc
        # Number of resets that succeed before ``reset_exc`` starts firing.
        # 0 (the default) means the very first reset raises; 1 lets the swap
        # through and fails only the teardown of the abandoned replacement.
        self._reset_exc_after = reset_exc_after

    async def get_or_create(self, key, agent=None, approval_policy="", **kwargs):
        self.acquires.append(key)
        client = self._clients.pop(0)
        self._sessions[key] = SimpleNamespace(provider=client)
        return client, True, False

    async def reset(self, key, *, expect_session=None, **kwargs):
        self.resets.append((key, expect_session))
        if self._reset_exc is not None and len(self.resets) > self._reset_exc_after:
            raise self._reset_exc
        return self._sessions.pop(key, None) is not None

    def release(self, key, **kwargs):
        self.released.append(key)

    def has_session(self, key):
        return key in self._sessions

    def get_provider(self, key):
        """The provider registered under this key, or ``None``.

        Distinct from ``has_session`` on purpose: the channel member compares this against the
        provider it cached at spawn, so a replacement registered under the same key must be
        visible as a DIFFERENT object rather than merely as presence.
        """
        entry = self._sessions.get(key)
        return entry.provider if entry is not None else None

    def mark_lifecycle_lease(self, key):
        """A lease this loop keeps for the member's life, as the real manager records it."""
        session = self._sessions.get(key)
        if session is None:
            return False
        session.lifecycle_lease = True
        self.lifecycle_marked.append(key)
        return True

    def set_lifecycle_turn_active(self, key, active):
        """Records the member's own turn window, as the real manager does."""
        session = self._sessions.get(key)
        if session is None:
            return False
        session.lifecycle_turn_active = active
        self.turn_active_calls.append((key, active))
        return True


# ── Detection ──


@pytest.mark.parametrize(
    "exc",
    [
        AcpPromptBusy("prompt already in progress"),
        # _format_acp_error rewrites the marker away, so the structural arm has
        # to carry this one on its own.
        AcpPromptBusy("The agent is still working on your previous request."),
        AcpError("session/prompt failed: a prompt is already in progress"),
    ],
)
def test_prompt_busy_is_detected(exc):
    assert is_prompt_busy(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        AcpError("InternalServerError"),
        RuntimeError("a prompt is already in progress"),
    ],
)
def test_unrelated_errors_are_not_prompt_busy(exc):
    assert is_prompt_busy(exc) is False


# ── _stream_task reports the wedge instead of posting a dead-end card ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        AcpPromptBusy("prompt already in progress"),
        AcpError("session/prompt failed: a prompt is already in progress"),
    ],
)
async def test_stream_task_reports_busy_and_posts_no_card(exc):
    agent, ch = _make_agent(), _make_channel()
    busy = await _stream_task(agent, ch, _raising_client(exc), "hi")
    assert busy is True
    ch.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_stream_task_still_posts_a_card_for_an_unrelated_error():
    agent, ch = _make_agent(), _make_channel()
    busy = await _stream_task(agent, ch, _raising_client(AcpError("InternalServerError")), "hi")
    assert busy is False
    ch.post.assert_awaited_once()
    assert "error occurred" in ch.post.await_args.args[1]


@pytest.mark.asyncio
async def test_stream_task_returns_false_on_success():
    agent, ch = _make_agent(), _make_channel()
    busy = await _stream_task(agent, ch, _text_client("done"), "hi")
    assert busy is False
    ch.post.assert_awaited_once()
    assert ch.post.await_args.args[1] == "done"


# ── _reset_busy_session ──


@pytest.mark.asyncio
async def test_reset_busy_session_returns_a_cold_client():
    agent = _make_agent()
    cold = _text_client("cold")
    sessions = FakeSessions([_raising_client(AcpPromptBusy("busy")), cold])
    await sessions.get_or_create(agent.session_key)
    wedged_entry = sessions._sessions[agent.session_key]

    client = await _reset_busy_session(sessions, agent)

    assert client is cold
    # Compare-and-swap: the reset is bound to the entry that was actually
    # observed as wedged, so a concurrent clear-context cannot make this
    # discard a session someone else just cold-started.
    assert sessions.resets == [(agent.session_key, wedged_entry)]


@pytest.mark.asyncio
async def test_reset_busy_session_gives_up_without_re_acquiring_when_reset_raises():
    agent = _make_agent()
    sessions = FakeSessions([_text_client("cold")], reset_exc=RuntimeError("shutdown blew up"))
    assert await _reset_busy_session(sessions, agent) is None
    assert sessions.acquires == []


@pytest.mark.asyncio
async def test_reset_busy_session_returns_none_when_the_re_acquire_fails():
    agent = _make_agent()
    sessions = FakeSessions([])
    sessions.get_or_create = AsyncMock(side_effect=RuntimeError("no capacity"))
    assert await _reset_busy_session(sessions, agent) is None


# ── _recover_busy_agent ──


@pytest.mark.asyncio
async def test_recover_replays_the_message_on_the_cold_session():
    agent, ch = _make_agent(), _make_channel()
    replayed: list[str] = []
    cold = _text_client("recovered", seen=replayed)
    sessions = FakeSessions([cold])

    client = await _recover_busy_agent(agent, ch, sessions, "please retry")

    assert client is cold
    assert replayed == ["please retry"]
    # One reset only: the replacement worked, so it must NOT be torn down.
    assert len(sessions.resets) == 1


@pytest.mark.asyncio
async def test_recover_is_unrecoverable_when_the_reset_fails():
    agent, ch = _make_agent(), _make_channel()
    sessions = FakeSessions([], reset_exc=RuntimeError("shutdown blew up"))
    assert await _recover_busy_agent(agent, ch, sessions, "hi") is None


@pytest.mark.asyncio
async def test_recover_tears_down_a_replacement_that_is_still_busy():
    agent, ch = _make_agent(), _make_channel()
    sessions = FakeSessions([_raising_client(AcpPromptBusy("prompt already in progress"))])

    assert await _recover_busy_agent(agent, ch, sessions, "hi") is None

    # Two resets: the swap, then the teardown of the abandoned replacement.
    # channel:-keyed sessions are exempt from both reapers, so leaving it
    # registered would leak it AND let wake re-acquire the wedge.
    assert len(sessions.resets) == 2
    assert agent.session_key not in sessions._sessions


@pytest.mark.asyncio
async def test_recover_swallows_a_failed_teardown_of_the_abandoned_replacement():
    agent, ch = _make_agent(), _make_channel()
    sessions = FakeSessions(
        [_raising_client(AcpPromptBusy("prompt already in progress"))],
        reset_exc=RuntimeError("teardown blew up"),
        reset_exc_after=1,
    )

    assert await _recover_busy_agent(agent, ch, sessions, "hi") is None
    # The swap landed; the teardown was attempted and blew up. Reporting the
    # dead end is the caller's job, so the failure must not escape.
    assert len(sessions.resets) == 2


# ── run_channel_agent end to end ──


async def _run_until(agent, ch, sessions, done: asyncio.Event, text: str) -> asyncio.Task:
    task = asyncio.create_task(run_channel_agent(agent, ch, sessions))
    await ch.post("human", text)
    await asyncio.wait_for(done.wait(), timeout=5)
    return task


async def _wait_for(predicate) -> bool:
    for _ in range(100):
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.mark.asyncio
async def test_loop_recovers_a_wedged_agent_and_keeps_listening():
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    replayed: list[str] = []
    sessions = FakeSessions(
        [
            _raising_client(AcpPromptBusy("prompt already in progress")),
            _text_client("back on my feet", seen=replayed),
        ]
    )

    task = asyncio.create_task(run_channel_agent(agent, ch, sessions))
    try:
        await ch.post("human", "start the review")
        # The agent is usable again: the replay landed AND the loop went back to
        # listening for the next message instead of terminating.
        assert await _wait_for(
            lambda: any("back on my feet" in m.content for m in ch.messages)
            and agent.state == "listening"
        )
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert replayed and "start the review" in replayed[0]
    assert len(sessions.resets) == 1
    posted = [m.content for m in ch.messages if m.from_id == agent.id]
    assert not any("could not be recovered" in p for p in posted)
    assert not any("error occurred" in p for p in posted)


@pytest.mark.asyncio
async def test_loop_reports_an_unrecoverable_agent_once_and_stops():
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    done = asyncio.Event()
    sessions = FakeSessions([_wedged_client(done), _wedged_client(done)])

    task = await _run_until(agent, ch, sessions, done, "start the review")
    await asyncio.wait_for(task, timeout=5)

    assert agent.state == "failed"
    stuck = [m.content for m in ch.messages if "could not be recovered" in m.content]
    assert len(stuck) == 1
    # No generic dead-end card, and the abandoned replacement is gone.
    assert not any("error occurred" in m.content for m in ch.messages)
    assert agent.session_key not in sessions._sessions
    assert sessions.released == [agent.session_key]


@pytest.mark.asyncio
async def test_an_unrecoverable_agent_stops_consuming_its_inbox():
    """The wedge is reported once, not once per message.

    The pre-fix loop posted a card per message and kept going; re-running the
    reset per message would have been strictly worse. Three queued messages
    must therefore produce exactly one report.
    """
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    done = asyncio.Event()
    sessions = FakeSessions([_wedged_client(done), _wedged_client(done)])
    # Queue all three BEFORE the loop starts, so the count cannot depend on how
    # fast the agent reaches its terminal state (a terminal agent is skipped by
    # Channel.post).
    for i in range(3):
        await ch.post("human", f"msg {i}")
    assert agent.inbox.qsize() == 3
    task = asyncio.create_task(run_channel_agent(agent, ch, sessions))
    await asyncio.wait_for(done.wait(), timeout=5)
    await asyncio.wait_for(task, timeout=5)

    assert agent.state == "failed"
    assert len([m for m in ch.messages if "could not be recovered" in m.content]) == 1
    # The other two are left unread rather than each producing its own report.
    assert agent.inbox.qsize() == 2


@pytest.mark.asyncio
async def test_the_stuck_card_still_lands_when_the_teardown_reset_fails():
    """A teardown failure must not cost the user the stuck report.

    Without the guard in ``_recover_busy_agent`` the reset error unwinds into
    ``run_channel_agent``'s generic handler: the agent still ends up ``failed``,
    but no card is posted, so the wedge is invisible in the channel.
    """
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None
    done = asyncio.Event()
    sessions = FakeSessions(
        [_wedged_client(done), _wedged_client(done)],
        reset_exc=RuntimeError("teardown blew up"),
        reset_exc_after=1,
    )

    task = await _run_until(agent, ch, sessions, done, "start the review")
    await asyncio.wait_for(task, timeout=5)

    assert agent.state == "failed"
    assert len([m for m in ch.messages if "could not be recovered" in m.content]) == 1
    assert sessions.released == [agent.session_key]


# ── a cleared member is re-acquired rather than left on a dead provider ──


@pytest.mark.asyncio
async def test_a_cleared_member_serves_its_next_message():
    """Clearing an idle member must not strand its listener on the shut-down provider.

    `api_channel_clear_context` discards the member's session, which pops the registry
    entry and shuts the provider down -- and that provider is the one this member cached
    when it spawned. Without a re-acquire the member streams a dead object for every later
    message, and only a restart recovers it.
    """
    agent = _make_agent()
    served: list[str] = []
    fresh = _text_client("served", seen=served)
    sessions = FakeSessions([_text_client("first"), fresh])

    # Spawn: the member takes its lease and caches the provider it was handed.
    cached = await _reacquire_cleared_session(sessions, agent)
    assert cached is not None, "precondition: the spawn acquire failed"
    assert sessions.has_session(agent.session_key), "precondition: no session was registered"

    # The clear: exactly what `discard_conversation` leaves behind -- key popped.
    sessions._sessions.pop(agent.session_key)
    assert not sessions.has_session(
        agent.session_key
    ), "precondition: the clear did not pop the key, so nothing is being tested"

    # The next message: the member must obtain a live provider rather than reuse the dead one.
    replacement = await _reacquire_cleared_session(sessions, agent)
    assert replacement is not None, (
        "the member could not re-acquire after its context was cleared, so every later "
        "message fails until it is restarted"
    )
    assert replacement is not cached, (
        "the member kept the provider the clear shut down, so its next message streams a "
        "dead session"
    )
    assert (
        await _stream_task(agent, _make_channel(), replacement, "hi") is False
    ), "the re-acquired provider did not serve the message"
    assert served == ["hi"], f"the message never reached the fresh provider; saw {served!r}"
    assert sessions.lifecycle_marked, (
        "the re-acquired lease was not declared lifecycle-scoped, so the next clear on this "
        "member is refused for as long as it lives"
    )


@pytest.mark.asyncio
async def test_a_replacement_registered_under_the_same_key_is_not_streamed_through():
    """A dequeued message must not reach the provider this member cached at spawn once the
    registry holds a DIFFERENT one under the same key.

    Presence cannot answer this. An idle clear pops the key and shuts the cached provider down;
    a surfaced dashboard tab can then be linked to the same key and register its own provider,
    at which point a presence probe says "a session exists" and is satisfied, while the
    object the member still holds is the dead one. Streaming into it loses the message.
    """
    ch = Channel(id="c1", topic="review")
    agent = ch.add_agent(role="dev", agent_name="dev")
    assert agent is not None

    stale_seen: list[str] = []
    fresh_seen: list[str] = []
    stale = _text_client("from the stale provider", seen=stale_seen)
    fresh = _text_client("from the replacement", seen=fresh_seen)
    sessions = FakeSessions([stale, fresh])

    task = asyncio.create_task(run_channel_agent(agent, ch, sessions))
    try:
        # Let the member spawn and cache `stale` under the key before anything is queued.
        assert await _wait_for(lambda: sessions.acquires and agent.state == "listening")

        # Exactly the shape the finding names: the entry is REPLACED, not removed, so the key is
        # still present and only the identity differs.
        key = sessions.acquires[0]
        replacement = _text_client("from the replacement", seen=fresh_seen)
        sessions._sessions[key] = SimpleNamespace(provider=replacement)
        assert sessions.has_session(key), "precondition: presence must still hold"
        assert sessions.get_provider(key) is not stale, "precondition: identity must differ"

        await ch.post("human", "please review this")
        assert await _wait_for(
            lambda: any(m.from_id == agent.id for m in ch.messages)
        ), "the member produced nothing at all"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert not stale_seen, (
        "the message was streamed through the provider cached at spawn, which the registry has "
        f"already replaced; saw {stale_seen!r}"
    )
