"""The keys, autouse stubs and builders the channel turn test modules share.

The session key, conversation and inbound route every case uses; the three
autouse stubs ``governance``, ``published`` and ``spooled`` (declared
``@pytest.fixture(..., name=...)`` and imported under their function names, so a
module that imports them gets them); the ``ChannelTurns`` builder ``_turns``, the
``Asker`` builder ``_asker``, ``_answer``, which runs one turn to completion, and
the room link ``_ROOM`` with ``_bound_as_room``, which checks the origin bind
stored it signed for the session's key. ``test_channel_turns.py``,
``test_channel_turns_drive_turn.py`` and ``test_channel_turns_discord_leg.py``
import them.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from channel_turn_fakes import (
    FakeSessions,
    Recorder,
    RecordingCtxBuilder,
    RecordingDispatcher,
    RecordingRenderer,
)

from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import Approvals, Asker, ChannelTurns, Drift
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.start_priority import StartPriority

KEY = "weixin:agentA:direct:userA"
CONV = "weixin:userA"
ROUTE = InboundRoute(conversation_id="ROOM", text="what the user typed", user_id="userA")


class _Governance:
    """What the channels policy answers, and every channel it was asked about."""

    def __init__(self) -> None:
        self.permitted = True
        self.asked: list[str] = []

    async def __call__(self, channel_type: str) -> bool:
        self.asked.append(channel_type)
        return self.permitted


# The three autouse stubs patch through the isolation floor's own MonkeyPatch
# (testing-conventions D11), so a test's own ``monkeypatch.undo()`` never strips them.
@pytest.fixture(autouse=True, name="governance")
def governance_fixture(_floor_monkeypatch: pytest.MonkeyPatch) -> _Governance:
    gate = _Governance()
    _floor_monkeypatch.setattr(D, "channel_inbound_permitted", gate)
    return gate


@pytest.fixture(autouse=True, name="published")
def published_fixture(_floor_monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Identity publication, recorded into the session ledger it ran against."""
    keys: list[str] = []

    async def _publish(sessions: Any, session_key: str) -> None:
        keys.append(session_key)
        ledger = getattr(sessions, "ledger", None)
        if ledger is not None:
            ledger.append(("publish_turn_identity", session_key))

    _floor_monkeypatch.setattr(D, "publish_turn_identity", _publish)
    return keys


@pytest.fixture(autouse=True, name="spooled")
def spooled_fixture(_floor_monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Every durable refusal the pipeline spooled, as ``(channel_type, route)``."""
    calls: list[tuple[str, Any]] = []

    async def _spool(*, channel_type: str, route: Any) -> bool:
        calls.append((channel_type, route))
        return True

    _floor_monkeypatch.setattr(D, "spool_refused_turn", _spool)
    return calls


def _turns(
    sessions: FakeSessions,
    *,
    ctx: RecordingCtxBuilder | None = None,
    recorder: Recorder | None = None,
    drift: frozenset[Drift] = frozenset(),
    approval_mode: str = "auto",
    approvals: Approvals | None = None,
    restricted: Any = None,
    conv_log: Any = None,
    dispatcher: Any = None,
    agent: str = "agentA",
    channel: str = "weixin",
) -> ChannelTurns:
    recorder = recorder if recorder is not None else Recorder()
    return ChannelTurns(
        channel,
        sessions=sessions,
        ctx_builder=ctx if ctx is not None else RecordingCtxBuilder(ledger=sessions.ledger),
        dispatcher=dispatcher if dispatcher is not None else RecordingDispatcher(approval_mode),
        agent=lambda: agent,
        record=recorder.record,
        notice=recorder.notice,
        surface=recorder.surface,
        restricted=restricted,
        conv_log=conv_log,
        approvals=approvals if approvals is not None else Approvals(grant=None),
        drift=drift,
    )


def _asker(session_key: str = KEY, **kw: Any) -> Asker:
    kw.setdefault("route", ROUTE)
    kw.setdefault("start_priority", StartPriority.FOREGROUND)
    kw.setdefault("audit_caller", "weixin:userA")
    kw.setdefault("reply_to", "ROOM")
    return Asker(session_key, CONV, **kw)


def _answer(turns: ChannelTurns, *, asker: Asker | None = None, message: str = "hi", **kw: Any):
    renderer = kw.pop("renderer", None)
    return asyncio.run(
        turns.answer(
            asker if asker is not None else _asker(),
            message,
            renderer if renderer is not None else RecordingRenderer(),
            **kw,
        )
    )


_ROOM = ChannelLink("weixin", channel_id="ROOM", thread_id=None)


def _bound_as_room(stored: Any) -> bool:
    """The origin bind stores this location, signed for the session's key."""
    from kiro_crew.mirror_admission import verify_mirror_admission

    return stored == _ROOM and verify_mirror_admission(KEY, stored)
