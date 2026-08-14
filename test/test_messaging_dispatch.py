"""Finalization contract of the shared channel turn pipeline.

``drive_turn`` owns the semaphore lifetime for every adopted channel, so a bug
in its ``finally`` is a bug in all of them at once. These tests pin the part
that is invisible on the happy path: what happens to ``release()`` when
finalization itself fails.
"""

from __future__ import annotations

import asyncio
from typing import Any

from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn
from kiro_crew.messaging.renderer import SilentRenderer


class _Sessions:
    """Minimal stand-in that counts the calls this contract is about."""

    def __init__(self, raise_on_acquire: bool = False):
        self.released = 0
        self.successes = 0
        self.failures = 0
        self._raise_on_acquire = raise_on_acquire

    async def get_or_create(self, key, agent=None, channel_id=None):
        if self._raise_on_acquire:
            raise RuntimeError("cold start failed")
        return object(), False, False

    async def set_channel(self, key, channel_id):
        pass

    def record_success(self, key):
        self.successes += 1

    async def record_failure(self, key):
        self.failures += 1

    def release(self, key):
        self.released += 1

    def get_provider(self, key):
        return object()


class _Renderer:
    """Renderer whose ``close`` can fail the way a real one can mid-flush."""

    def __init__(self, close_raises: bool = False):
        self.close_raises = close_raises
        self.closed = 0

    async def on_turn_start(self):
        pass

    async def close(self):
        self.closed += 1
        if self.close_raises:
            raise RuntimeError("renderer finalization failed")


class _CtxBuilder:
    def build_message(self, text, is_new, session_key, **kw):
        return text, None


class _Driver:
    def __init__(self, *a, **kw):
        pass

    async def run(self, message):
        return "the reply"


def _turn(renderer: Any) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
    )


def _patch_pipeline(monkeypatch, *, permitted: bool = True):
    """Stub everything drive_turn touches except the finalization under test."""

    async def _permitted(_channel_type):
        return permitted

    async def _publish(_sessions, _key):
        pass

    async def _embed(fn, *args, **kw):
        return fn(*args, **kw)

    monkeypatch.setattr(D, "inbound_permitted", _permitted)
    monkeypatch.setattr(D, "publish_turn_identity", _publish)
    monkeypatch.setattr(D, "run_in_embed_pool", _embed)
    monkeypatch.setattr(D, "TurnDriver", _Driver)


def test_release_still_runs_when_renderer_close_fails(monkeypatch) -> None:
    """A failed renderer.close must NOT strand the session semaphore.

    The semaphore is keyed by SESSION, so leaking it does not merely lose this
    turn -- every later message for that conversation blocks forever and any
    queued turn never drains, until the gateway restarts.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    renderer = _Renderer(close_raises=True)

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1, "close should still be attempted"
    assert sessions.released == 1, (
        "renderer.close raised and the session was never released -- the "
        "conversation is now permanently busy"
    )


def test_a_failing_close_does_not_escape_drive_turn(monkeypatch) -> None:
    """The failure is logged and swallowed, not raised at the caller.

    Adopters call drive_turn from a per-message task; letting finalization
    raise would surface as an unhandled task exception for a turn that already
    delivered its reply.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()

    # asyncio.run re-raises anything drive_turn lets escape.
    asyncio.run(
        drive_turn(
            _turn(_Renderer(close_raises=True)),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert sessions.successes == 1, "the turn itself succeeded"


def test_release_is_not_called_when_the_semaphore_was_never_acquired(monkeypatch) -> None:
    """The _acquired gate must survive the new guard.

    A cold-start failure raises before get_or_create returns, so nothing was
    ever held -- releasing here would hand back a permit that does not exist.
    """
    _patch_pipeline(monkeypatch)
    sessions = _Sessions(raise_on_acquire=True)
    renderer = _Renderer(close_raises=True)

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1, "finalization still runs on the failure path"
    assert sessions.released == 0, "nothing was acquired, so nothing may be released"
    assert sessions.failures == 0, "record_failure is also gated on _acquired"


def test_the_happy_path_releases_exactly_once(monkeypatch) -> None:
    """Guard rail: the new try/except must not double-release."""
    _patch_pipeline(monkeypatch)
    sessions = _Sessions()
    renderer = _Renderer()

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 1
    assert sessions.released == 1
    assert sessions.successes == 1


def test_a_denied_turn_neither_renders_nor_releases(monkeypatch) -> None:
    """Governance backstop returns before any side effect."""
    _patch_pipeline(monkeypatch, permitted=False)
    sessions = _Sessions()
    renderer = _Renderer()

    asyncio.run(drive_turn(_turn(renderer), sessions=sessions, ctx_builder=_CtxBuilder()))

    assert renderer.closed == 0
    assert sessions.released == 0
    assert sessions.successes == 0


class _PauseSessions(_Sessions):
    """Interface parity with the real SessionManager for the pause lookup.

    Extended here rather than leaning on production's fail-open: that fallback
    exists for the bare ``MagicMock`` managers elsewhere in the suite, and a test
    about the gate must not be silently exercising the fallback instead.
    """

    def __init__(self, paused: bool = False):
        super().__init__()
        self.paused = paused
        self.pause_calls: list[tuple[str, bool]] = []

    def is_mirror_paused(self, key, *, origin=False):
        self.pause_calls.append((key, origin))
        return self.paused


class _CountingRenderer(_Renderer):
    """Records the turn-start the user would SEE as a typing indicator."""

    def __init__(self):
        super().__init__()
        self.started = 0

    async def on_turn_start(self):
        self.started += 1


def _capture_driver(box: list) -> type:
    class _Capturing(_Driver):
        def __init__(self, provider, renderer, **kw):
            super().__init__()
            box.append(renderer)

    return _Capturing


def _turn_with_key(renderer: Any, session_key: str) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key=session_key,
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        approval_mode="auto",
    )


def test_a_disconnected_conversation_is_silenced(monkeypatch) -> None:
    """Disconnect stops the replies, which for a non-Slack channel happens HERE.

    Slack enforces a disconnect on its own streaming mirror. Every other channel
    answers through this pipeline, so before this gate a disconnected channel
    kept replying and the dashboard control changed nothing but its own label.

    The turn still runs and the semaphore is still released: the binding is
    retained by design, so the inbound message must still land in the session.
    """
    box: list[Any] = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver(box))
    sessions = _PauseSessions(paused=True)
    renderer = _CountingRenderer()

    asyncio.run(
        drive_turn(
            _turn_with_key(renderer, "weixin:agentA:direct:userA"),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert isinstance(box[0], SilentRenderer), "the driver must stream into the silent one"
    assert renderer.started == 0, "a disconnected conversation must not even show typing"
    assert renderer.closed == 0, "the real renderer was never used, so it has nothing to close"
    assert sessions.successes == 1, "the turn still ran"
    assert sessions.released == 1, "and the session semaphore was still released"


def test_a_connected_conversation_keeps_its_real_renderer(monkeypatch) -> None:
    """The non-vacuity half: without it, deleting the gate would still pass above."""
    box: list[Any] = []
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr(D, "TurnDriver", _capture_driver(box))
    sessions = _PauseSessions(paused=False)
    renderer = _CountingRenderer()

    asyncio.run(
        drive_turn(
            _turn_with_key(renderer, "weixin:agentA:direct:userA"),
            sessions=sessions,
            ctx_builder=_CtxBuilder(),
        )
    )

    assert box[0] is renderer
    assert renderer.started == 1
    assert renderer.closed == 1


def test_the_pause_is_read_for_the_role_the_turn_arrived_on(monkeypatch) -> None:
    """Two non-Slack deliveries mute independently, so the ROLE decides the flag.

    A channel-BORN session's key IS its conversation, so a turn arriving in that
    namespace is the origin. Anything else reaching this pipeline came over a
    mirror/resume binding. Reading the wrong flag would let one row's disconnect
    silence the other's conversation.
    """
    _patch_pipeline(monkeypatch)

    born = _PauseSessions(paused=False)
    asyncio.run(
        drive_turn(
            _turn_with_key(_CountingRenderer(), "weixin:agentA:direct:userA"),
            sessions=born,
            ctx_builder=_CtxBuilder(),
        )
    )
    assert born.pause_calls == [("weixin:agentA:direct:userA", True)], "born-in reads origin"

    mirrored = _PauseSessions(paused=False)
    asyncio.run(
        drive_turn(
            _turn_with_key(_CountingRenderer(), "dashboard:chat-1"),
            sessions=mirrored,
            ctx_builder=_CtxBuilder(),
        )
    )
    assert mirrored.pause_calls == [("dashboard:chat-1", False)], "a mirror reads the mirror flag"
