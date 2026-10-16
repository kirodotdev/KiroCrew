"""``drive_turn``, the ChannelTurn entry, field for field, and the exchange record adapter.

``drive_turn`` hands every ``ChannelTurn`` field to the pipeline it builds (persist,
provider binding, mute, origin bind, start priority, directives, the grant), and
``exchange_writer``, the record adapter every exchange-only channel uses, writes
answers off the loop and drops failures. The pipeline itself is in
``test_channel_turns.py``; the shared keys, autouse stubs and builders are in
``channel_turns_helpers.py``.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest
from channel_turn_fakes import (
    FakeSessions,
    RecordingCtxBuilder,
    RecordingRenderer,
    ScriptedProvider,
    answer,
    complete,
    directive,
    permission,
    text,
)
from channel_turns_helpers import governance_fixture  # noqa: F401
from channel_turns_helpers import published_fixture  # noqa: F401
from channel_turns_helpers import spooled_fixture  # noqa: F401
from channel_turns_helpers import _ROOM, CONV, KEY, ROUTE, _bound_as_room

from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.messaging.dispatch import (
    TOOLLESS_TURN_AGENT,
    ChannelTurn,
    TurnRecord,
    drive_turn,
    exchange_writer,
)
from kiro_crew.start_priority import StartPriority

# ── the record adapter every exchange-only channel uses ──────────────────────


def test_exchange_writer_writes_answers_off_loop_and_drops_failures() -> None:
    written: list[tuple[Any, ...]] = []
    threads: list[int] = []

    def _persist(*args: Any) -> None:
        threads.append(threading.get_ident())
        written.append(args)

    record = exchange_writer(_persist)

    async def scenario() -> None:
        await record(TurnRecord("answered", KEY, "agentA", "hi", "the reply", True))
        await record(TurnRecord("hook_reply", KEY, "agentA", "ping", "pong", False))
        await record(TurnRecord("failed", KEY, "agentA", "hi", "", False, error=RuntimeError()))

    asyncio.run(scenario())
    assert written == [
        (KEY, "hi", "the reply", True, "agentA"),
        (KEY, "ping", "pong", False, "agentA"),
    ]
    assert threading.get_ident() not in threads


# ── drive_turn: the ChannelTurn entry, field for field ───────────────────────


def _channel_turn(renderer: Any, **kw: Any) -> ChannelTurn:
    kw.setdefault("session_key", KEY)
    kw.setdefault("approval_mode", "auto")
    return ChannelTurn(
        channel_type="weixin",
        conversation_id=CONV,
        agent="agentA",
        user_text="hi",
        renderer=renderer,
        **kw,
    )


class TestDriveTurn:
    def test_persists_the_raw_reply_with_the_first_acquires_newness(self) -> None:
        persisted: list[tuple[str, str, bool]] = []
        sessions = FakeSessions(ScriptedProvider([text("  "), complete()]), is_new=True)
        turn = _channel_turn(
            RecordingRenderer(), persist=lambda u, r, n: persisted.append((u, r, n))
        )

        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))

        assert persisted == [("hi", "  ", True)]

    def test_hands_the_live_provider_on_before_the_driver_and_degrades_on_failure(self) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        seen: list[Any] = []

        def _bind(p: Any) -> None:
            seen.append(p)
            sessions.ledger.append(("bind_provider", ""))
            raise RuntimeError("no cwd")

        turn = _channel_turn(RecordingRenderer(), bind_provider=_bind)
        asyncio.run(
            drive_turn(
                turn, sessions=sessions, ctx_builder=RecordingCtxBuilder(ledger=sessions.ledger)
            )
        )

        assert seen == [provider]
        names = sessions.names()
        assert names.index("bind_provider") < names.index("build_message")
        assert sessions.count("record_success") == 1

    def test_an_unprompted_refusal_ends_silently(self) -> None:
        renderer = RecordingRenderer()
        turn = _channel_turn(
            renderer,
            session_key=f"weixin:{TOOLLESS_TURN_AGENT}:direct:peerB",
            deny_all_tools=True,
            unprompted=True,
        )
        sessions = FakeSessions(ScriptedProvider(backend=ACP_BACKEND_CLAUDE))
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        assert renderer.shown == [] and renderer.closed == 1

    def test_a_disconnected_conversation_is_silenced(self) -> None:
        provider = ScriptedProvider(answer())
        sessions = FakeSessions(provider)
        sessions.muted.add((KEY, True))  # a channel-born key reads the origin flag
        renderer = RecordingRenderer()
        asyncio.run(
            drive_turn(
                _channel_turn(renderer), sessions=sessions, ctx_builder=RecordingCtxBuilder()
            )
        )
        assert provider.prompts == ["hi"], "the message still lands in the session"
        assert renderer.shown == [] and renderer.started == 0 and renderer.closed == 0

    def test_the_origin_conversation_is_bound_off_the_loop(self) -> None:
        threads: list[int] = []

        class _Threaded(FakeSessions):
            def set_mirror_link(self, key: str, link: Any) -> None:
                threads.append(threading.get_ident())
                super().set_mirror_link(key, link)

        sessions = _Threaded()
        turn = _channel_turn(RecordingRenderer(), origin_conversation=_ROOM)
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        assert sessions.origin_links[KEY] is _ROOM and _bound_as_room(sessions.mirror_links[KEY])
        assert threads and threading.get_ident() not in threads

    def test_a_restricted_shutdown_refusal_never_spools(self, spooled) -> None:
        turn = _channel_turn(RecordingRenderer(), inbound_route=ROUTE, inbound_restricted=True)
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(closing=True), ctx_builder=RecordingCtxBuilder())
        )
        assert spooled == []

    def test_an_unprompted_turn_is_still_recorded(self) -> None:
        """``unprompted`` only silences the tool-less refusal; whether the exchange is
        written is the channel's ``persist`` alone."""
        persisted: list[tuple[str, str, bool]] = []
        turn = _channel_turn(
            RecordingRenderer(),
            unprompted=True,
            persist=lambda u, r, n: persisted.append((u, r, n)),
        )
        asyncio.run(
            drive_turn(
                turn,
                sessions=FakeSessions(ScriptedProvider(answer("the reply"))),
                ctx_builder=RecordingCtxBuilder(),
            )
        )
        assert [(u, "".join(r), n) for u, r, n in persisted] == [("hi", "the reply", False)]

    def test_an_unprompted_hook_reply_is_still_recorded_after_the_gap(self) -> None:
        persisted: list[tuple[str, str, bool]] = []
        sessions = FakeSessions()
        turn = _channel_turn(
            RecordingRenderer(),
            unprompted=True,
            persist=lambda u, r, n: persisted.append((u, r, n)),
        )
        asyncio.run(
            drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder(hook_reply="pong"))
        )
        assert persisted == [("hi", "pong", False)]
        assert sessions.count("await_replay_gap") == 1

    @pytest.mark.parametrize(
        "priority", [None, StartPriority.BACKGROUND, StartPriority.FOREGROUND], ids=str
    )
    def test_the_turns_priority_reaches_the_claim(self, priority) -> None:
        """An automation or unprompted turn (the ``ChannelTurn`` default) cold-starts
        in the background; only a person's message starts at person priority."""
        sessions = FakeSessions()
        kw = {} if priority is None else {"start_priority": priority}
        turn = _channel_turn(RecordingRenderer(), **kw)
        asyncio.run(drive_turn(turn, sessions=sessions, ctx_builder=RecordingCtxBuilder()))
        expected = StartPriority.BACKGROUND if priority is None else priority
        assert sessions.calls("get_or_create")[0][2]["start_priority"] is expected

    def test_the_turns_own_fields_reach_the_driver(self) -> None:
        provider = ScriptedProvider([permission("r1"), complete()])
        consumed: list[Any] = []

        async def _directives(kind: str, args: dict[str, Any]) -> None:
            consumed.append(kind)

        ctx = RecordingCtxBuilder()
        turn = _channel_turn(
            RecordingRenderer(),
            approval_mode="interactive",
            auto_approve_session=lambda: True,
            minimal_context=True,
            directive_consumer=_directives,
            user_display_name="Ann",
        )
        asyncio.run(drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=ctx))
        assert provider.approved == ["r1"]
        assert ctx.builds[0]["minimal_context"] is True
        assert ctx.builds[0]["user_display_name"] == "Ann"

    def test_the_turns_directive_consumer_receives_its_directives(self) -> None:
        """The per-turn consumer the channel built is the one the driver hands a
        session directive to, not a default one."""
        provider = ScriptedProvider([*directive("autonudge_stop"), complete()])
        consumed: list[Any] = []

        async def _directives(kind: str, args: dict[str, Any]) -> None:
            consumed.append(kind)

        turn = _channel_turn(RecordingRenderer(), directive_consumer=_directives)
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=RecordingCtxBuilder())
        )
        assert consumed == ["autonudge_stop"], "the turn's own consumer was never handed it"

    def test_without_a_grant_an_interactive_turn_denies_what_no_one_answers(self) -> None:
        """No ``auto_approve_session`` and no decider: a permission request is
        rejected, never approved (the grant is opt-in per channel)."""
        provider = ScriptedProvider([permission("r1"), complete()])
        turn = _channel_turn(RecordingRenderer(), approval_mode="interactive")
        asyncio.run(
            drive_turn(turn, sessions=FakeSessions(provider), ctx_builder=RecordingCtxBuilder())
        )
        assert provider.rejected == ["r1"] and provider.approved == []
