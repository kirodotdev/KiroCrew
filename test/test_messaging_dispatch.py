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
