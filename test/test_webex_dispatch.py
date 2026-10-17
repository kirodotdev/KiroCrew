"""Tests for kiro_crew.webex.transport_dispatch (WebexDispatcher) + commands."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import pytest
from webex_dispatch_helpers import (
    _EMAIL,
    FakeClient,
    FakeConvLog,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _cfg,
    _cfg_queue,
    _dispatcher,
    _entry,
    _inbound,
    _spin,
)

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.webex import client as webex_client
from kiro_crew.webex import transport_dispatch as webex_dispatch
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.commands import (
    COMMAND_SPEC,
    ConversationState,
    build_help_text,
    is_bare_mid_turn_override,
    is_unknown_command,
    parse_command,
    parse_command_argument,
    parse_mid_turn_override,
)
from kiro_crew.webex.transport_dispatch import _MAX_COLLAPSE, WebexDispatcher

# ------------------------------------------------------------------
# Tests: full turn
# ------------------------------------------------------------------


def _deny_webex_profile(monkeypatch, tmp_path):
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir(exist_ok=True)
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()
    (pdir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": ["slack"]}},
            }
        )
    )


class TestTurn:
    @pytest.mark.asyncio
    async def test_channels_deny_drops_inbound_message(self, tmp_path, monkeypatch) -> None:
        # A channels DENY must stop handle_message from driving a turn. This
        # locks the Webex inbound chokepoint.
        from kiro_crew.platform import governance_profiles as gp

        _deny_webex_profile(monkeypatch, tmp_path)
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        try:
            await d.handle_message(_inbound("hello"))
            assert sessions.successes == []  # no turn ran
        finally:
            gp.reset_store()

    @pytest.mark.asyncio
    async def test_text_turn_bookkeeping(self) -> None:
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi there"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider)
        client = FakeClient()
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), client, conv_log=conv)

        await d.handle_message(_inbound("hello"))

        key = d._session_key(_EMAIL)
        # Final answer landed via placeholder edit.
        assert any(m == "hi there" for (_, _, m) in client.edits)
        # Bookkeeping: success recorded, semaphore released, turn persisted.
        assert sessions.successes == [key]
        assert sessions.released == [key]
        assert (key, "user", "hello") in conv.appended
        assert (key, "assistant", "hi there") in conv.appended

    @pytest.mark.asyncio
    async def test_agent_resolves_to_kirocrew_when_unset(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=_cfg(default_agent=""))
        await d.handle_message(_inbound("hi"))
        assert sessions.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_cold_start_failure_finalizes_and_skips_release(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider, raise_on_get=RuntimeError("boom"))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        # Must not raise — the dispatcher swallows and finalizes.
        await d.handle_message(_inbound("hi"))

        # Placeholder finalized (no perma-"🤔") even though get_or_create failed.
        assert client.edits  # close() drove on_done -> placeholder edited
        # Never held the semaphore -> never release / record_failure it.
        assert sessions.released == []
        assert sessions.failures == []

    @pytest.mark.asyncio
    async def test_soft_threshold_notice_is_separate_and_unpersisted(self) -> None:
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)  # >= soft (80), < hard (95)
        client = FakeClient()
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), client, conv_log=conv)

        await d.handle_message(_inbound("hello"))

        # Notice surfaced as a SEPARATE message.
        assert any("/compact" in m for (_, m) in client.sent)
        # ...but NOT persisted into the assistant turn (only the real answer is).
        assistant_texts = [t for (_, role, t) in conv.appended if role == "assistant"]
        assert assistant_texts == ["answer"]

    @pytest.mark.asyncio
    async def test_hard_threshold_forces_compaction(self) -> None:
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider, ctx_pct=96.0)  # >= hard (95)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert provider.compacted is True
        assert any("compacted" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_hard_threshold_declines_silently_on_auto_managed_backend(self) -> None:
        # No /compact to dispatch and no notice: the backend compacts on its
        # own as context fills.
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider, ctx_pct=96.0)  # >= hard (95)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert provider.compacted is False
        assert not any("compacted" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_soft_nudge_suppressed_on_auto_managed_backend(self) -> None:
        # The nudge advises /compact, which this backend refuses — it compacts
        # on its own, so there is nothing for the user to act on.
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider, ctx_pct=85.0)  # >= soft (80), < hard (95)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert not any("/compact" in m for (_, m) in client.sent)


# ------------------------------------------------------------------
# Tests: commands
# ------------------------------------------------------------------


class TestCommands:
    @pytest.mark.asyncio
    async def test_new_bumps_gen_and_acks(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/new"))

        assert client.sent == [("ROOM", "✅ Started a fresh conversation.")]
        assert d._conv.current_gen(_EMAIL) == 1  # generation bumped
        assert sessions.successes == []  # no LLM turn

    @pytest.mark.asyncio
    async def test_help_command(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/help"))

        assert len(client.sent) == 1
        assert "/compact" in client.sent[0][1]
        assert sessions.successes == []  # no LLM turn

    @pytest.mark.asyncio
    async def test_compact_command(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        key = d._session_key(_EMAIL)
        assert provider.compacted is True
        assert sessions.acquired == [key]
        assert sessions.released == [key]
        assert client.sent == [("ROOM", "🗜️ Context compacted.")]

    @pytest.mark.asyncio
    async def test_compact_declined_on_auto_managed_backend(self) -> None:
        # A backend that cannot serve /compact gets the informational reply and
        # compact() is NEVER dispatched.
        provider = FakeProvider([])
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        key = d._session_key(_EMAIL)
        assert provider.compacted is False
        assert sessions.released == [key]  # the acquired semaphore is handed back
        assert any("manages compaction automatically" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_compact_none_capability_preserves_dispatch(self) -> None:
        # The ABC's None (supported) default keeps the existing dispatch.
        provider = FakeProvider([])
        provider.manual_compact_unsupported_backend = None
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert provider.compacted is True

    @pytest.mark.asyncio
    async def test_compact_refused_while_turn_busy(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider, acquire=False, has_session=True)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert provider.compacted is False
        assert sessions.released == []
        assert any("/compact" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_compact_without_active_session(self) -> None:
        sessions = FakeSessions(None, acquire=False, has_session=False)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert sessions.released == []
        assert any("no conversation" in m for (_, m) in client.sent)

    def test_parse_command(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("/start") == "new"
        assert parse_command("/compact") == "compact"
        assert parse_command("/help") == "help"
        assert parse_command("hello") is None
        assert parse_command("say /new please") is None

    def test_conversation_state(self) -> None:
        s = ConversationState()
        assert s.current_gen("u") == 0
        assert s.bump_gen("u") == 1
        s.set_awaiting("u")
        assert s.is_awaiting("u") is True
        s.clear_awaiting("u")
        assert s.is_awaiting("u") is False


# ------------------------------------------------------------------
# Tests: mid-turn
# ------------------------------------------------------------------


class TestWebexMidTurn:
    @pytest.mark.asyncio
    async def test_busy_steers_and_acknowledges(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("and also this"))

        assert provider.steered == ["and also this"]
        assert any("Folded" in m for (_, m) in client.sent)
        assert sessions.successes == []  # no full turn ran while busy

    @pytest.mark.asyncio
    async def test_busy_but_turn_finished_runs_fresh(self) -> None:
        # is_busy is False by the time _handle_busy runs (turn finished in the
        # window) -> run the message as a fresh turn instead of a false ack.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)  # _busy defaults False
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key(_EMAIL), "later")

        assert sessions.successes  # a real turn ran
        assert provider.steered == []  # not steered

    @pytest.mark.asyncio
    async def test_busy_steer_unavailable_queues_instead_of_refusing(self) -> None:
        """A mid-turn message that cannot be steered is QUEUED, never bounced.

        Telling the user to resend puts the burden of not losing their own
        message on them, and they have no way to know when the turn ends.
        """
        provider = FakeProvider([])
        provider.supports_steer = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key(_EMAIL), "later")

        assert [t for _ts, t, _kw in sessions.queued] == ["later"]
        assert not any("resend" in m for (_, m) in client.sent)
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_busy_no_active_turn_does_not_steer(self) -> None:
        # Semaphore held (post-turn bookkeeping) but no turn is live: steer must
        # not be attempted and must not falsely acknowledge a merge.
        provider = FakeProvider([])
        provider.active_turn = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key(_EMAIL), "later")

        assert provider.steered == []
        assert not any("Folded" in m for (_, m) in client.sent)
        assert [t for _ts, t, _kw in sessions.queued] == ["later"]
        assert sessions.successes == []


# ------------------------------------------------------------------
# Command surface
# ------------------------------------------------------------------


class TestCommandParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/new", "new"),
            ("/start", "new"),
            ("/compact", "compact"),
            ("/help", "help"),
            ("/stop", "stop"),
            ("/cancel", "stop"),
            ("/link", "link"),
            ("/unlink", "unlink"),
            ("/yolo", "yolo"),
            ("/yolo on", "yolo"),
            ("/kirocrew dashboard", "dashboard"),
            ("  /HELP  ", "help"),
            ("hello", None),
            ("", None),
            ("/nope", None),
        ],
    )
    def test_parse_command(self, text: str, expected: str | None) -> None:
        assert parse_command(text) == expected

    def test_queue_and_steer_are_not_standalone_commands(self) -> None:
        """They are PREFIXES carrying a message body.

        Resolving them as commands here would swallow the body, so the message
        the user actually wanted queued would never run.
        """
        assert parse_command("/queue also check the logs") is None
        assert parse_command("/steer use ripgrep") is None

    def test_help_text_is_generated_from_the_spec(self) -> None:
        # A hand-written card drifts from the parser silently, and the user is
        # the one who finds out. Every spec row must appear, and every row must
        # actually parse.
        text = build_help_text()
        for name, desc in COMMAND_SPEC:
            assert f"/{name}" in text
            assert desc in text
            assert parse_command(f"/{name}") is not None

    @pytest.mark.parametrize(
        "text,mode,rest",
        [
            ("/queue check the logs", "queue", "check the logs"),
            ("/steer use ripgrep", "steer", "use ripgrep"),
            ("/QUEUE shout", "queue", "shout"),
            ("/queue", None, "/queue"),
            ("plain text", None, "plain text"),
        ],
    )
    def test_parse_mid_turn_override(self, text: str, mode: str | None, rest: str) -> None:
        assert parse_mid_turn_override(text) == (mode, rest)

    @pytest.mark.parametrize("text", ["/queue", "/steer", " /QUEUE "])
    def test_bare_directive_is_detected(self, text: str) -> None:
        assert is_bare_mid_turn_override(text) is True

    @pytest.mark.parametrize("text", ["/queue x", "/new", "hello", ""])
    def test_a_directive_with_a_body_is_not_bare(self, text: str) -> None:
        assert is_bare_mid_turn_override(text) is False

    @pytest.mark.parametrize("text", ["/nope", "/mdoel", "/quue x"])
    def test_unknown_slash_commands_are_recognised_as_such(self, text: str) -> None:
        assert is_unknown_command(text) is True

    @pytest.mark.parametrize("text", ["/new", "/queue x", "hello", "", "1/2 done"])
    def test_known_commands_and_prose_are_not_unknown(self, text: str) -> None:
        assert is_unknown_command(text) is False

    @pytest.mark.parametrize(
        "text",
        [
            "/usr/bin/python3 -V",
            "/home/me/foo.py: fix this",
            "/etc/hosts",
            "/tmp/a.log has the trace",
        ],
    )
    def test_a_pasted_path_is_a_message_not_a_command(self, text: str) -> None:
        """A leading slash is not enough to call something a command.

        Absolute paths are extremely common in what a user sends an agent.
        Answering them with the help card would swallow the actual request, and
        the user would have no idea why.
        """
        assert is_unknown_command(text) is False
        assert parse_command(text) is None

    @pytest.mark.parametrize(
        "text,arg", [("/yolo on", "on"), ("/yolo", ""), ("/kirocrew dashboard 2h", "dashboard 2h")]
    )
    def test_parse_command_argument(self, text: str, arg: str) -> None:
        assert parse_command_argument(text) == arg


class TestCommandDispatch:
    @pytest.mark.asyncio
    async def test_help_answers_with_the_generated_card(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/help"))

        assert client.sent[-1][1] == build_help_text()
        assert sessions.successes == []  # no LLM turn spent on a command

    @pytest.mark.asyncio
    async def test_a_bare_queue_directive_answers_with_usage(self) -> None:
        # Otherwise the literal string "/queue" reaches the model and the user
        # gets an answer to it instead of being told they left the message off.
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/queue"))

        assert "need a message after them" in client.sent[-1][1]
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_an_unknown_command_answers_with_the_card(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/nwe"))

        assert "Unknown command" in client.sent[-1][1]
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_a_drained_command_reaches_the_model_as_text(self) -> None:
        """``interpret_commands=False`` is what makes the drain safe.

        A "/new" the user typed mid-turn is turn CONTENT. Executing it on replay
        would wipe the conversation they were waiting on an answer from.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/new"), interpret_commands=False)

        assert sessions.successes  # a real turn ran
        assert not any("fresh conversation" in m for (_, m) in client.sent)


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_cancels_the_running_turn_and_clears_the_queue(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.queued = [("1", "queued", {})]
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/stop"))

        assert provider.cancelled == [0]
        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert "🛑 Stopped." in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_decline_webex_could_not_post_leaves_the_next_press_a_first_press(
        self,
    ) -> None:
        """``_reply`` hands back the id of what it posted, and ``None`` when there
        was nothing to post with. A member who saw no answer presses again within
        seconds, and that press must be declined again, never a silent reset."""
        from kiro_crew import session_lifecycle as sl

        sl._stop_declined_markers.clear()
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.is_compacting = lambda key: True
        forced: list[dict] = []

        async def stop_turn(key, **kw):
            forced.append(kw)
            return "hard"

        sessions.stop_turn = stop_turn
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        posted = d._reply
        down = True

        async def _flaky(inbound, text, **kw):
            if down:
                return None
            return await posted(inbound, text, **kw)

        d._reply = _flaky  # type: ignore[method-assign]
        await d.handle_message(_inbound("/stop"))
        assert sl._stop_declined_markers == {}, "an undelivered warning arms nothing"
        down = False
        await d.handle_message(_inbound("/stop"))
        assert forced == [], "no force on a press the member was never warned about"
        assert "nothing was stopped" in client.sent[-1][1]
        sl._stop_declined_markers.clear()

    @pytest.mark.asyncio
    async def test_stop_is_declined_while_the_session_compacts(self) -> None:
        """An automatic compaction holds the session: nothing is cancelled, nothing
        is cleared, and the caller is told nothing was stopped."""
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.queued = [("1", "queued", {})]
        sessions.is_compacting = lambda key: True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/stop"))

        assert provider.cancelled == []
        assert sessions.cleared == []
        assert "nothing was stopped" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_another_members_declined_stop_does_not_force_mine(self) -> None:
        """A group space shares one session key. The decline marker belongs to the
        member who was declined, so the next member's first press stays a first
        press, and the declined member's own repeat is the one that forces."""
        from kiro_crew import session_lifecycle as sl

        sl._stop_declined_markers.clear()
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.is_compacting = lambda key: True
        forced: list[dict] = []

        async def stop_turn(key, **kw):
            forced.append(kw)
            return "hard"

        sessions.stop_turn = stop_turn
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        def group(email: str) -> WebexInbound:
            return WebexInbound(person_email=email, room_id="ROOM", text="/stop", room_type="group")

        await d.handle_message(group("alice@example.com"))
        await d.handle_message(group("bob@example.com"))
        assert forced == [], "bob's first press is a first press"
        assert all("nothing was stopped" in m for (_, m) in client.sent[-2:])
        await d.handle_message(group("alice@example.com"))
        assert forced == [{"force": True, "preserve_queue": True}]
        sl._stop_declined_markers.clear()

    @pytest.mark.asyncio
    async def test_the_forced_stop_keeps_other_members_queued_work(self) -> None:
        """The reset is this member's; the queue is the whole space's. The queue is
        detached before the hard stop, this member's entries are dropped from the
        handles, and the other members' are handed to the successor."""
        from kiro_crew import session_lifecycle as sl
        from kiro_crew.messaging.queue_drain import QUEUED_OWNER_KEY
        from kiro_crew.webex.transport_dispatch import _entry_owner

        sl._stop_declined_markers.clear()
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.is_compacting = lambda key: True
        mine = _entry_owner(_inbound("/stop"))
        own = ("1", "mine", {QUEUED_OWNER_KEY: mine})
        theirs = ("2", "theirs", {QUEUED_OWNER_KEY: "someone-else"})
        sessions.queued = [own, theirs]
        calls: list[dict] = []
        only_cleared: list = []
        restored: list = []

        async def stop_turn(key, **kw):
            calls.append(kw)
            sessions.queued.clear()  # the hard reset pops the session and its queue
            return "hard"

        def detach_queue(key):
            taken = tuple(sessions.queued)
            sessions.queued.clear()
            return taken

        owner_clears: list = []

        def clear_queue(key, owned_by=None, *, only=None):
            if only is not None:
                only_cleared.extend(only)
            else:
                owner_clears.append(owned_by)

        def restore_queue(key, entries):
            restored.extend(entries)
            sessions.queued[:0] = list(entries)

        sessions.stop_turn = stop_turn
        sessions.detach_queue = detach_queue
        sessions.clear_queue = clear_queue
        sessions.restore_queue = restore_queue
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/stop"))
        await d.handle_message(_inbound("/stop"))
        assert calls == [{"force": True, "preserve_queue": True}]
        assert only_cleared == [own], "only this member's entry dropped"
        assert owner_clears == [], "no second owner-scoped clear after the force"
        assert restored == [theirs], "the other member's entry reached the successor"
        assert sessions.queued == [theirs]
        assert sessions.released[-1] == d._session_key(_EMAIL)
        assert "🛑 Stopped." in client.sent[-1][1]
        sl._stop_declined_markers.clear()

    @pytest.mark.asyncio
    async def test_stop_with_nothing_running_still_clears(self) -> None:
        sessions = FakeSessions(FakeProvider([]))  # not busy
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/cancel"))

        assert sessions.cleared == [d._session_key(_EMAIL)]
        assert "Nothing was running" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_stop_survives_a_session_with_no_provider(self) -> None:
        # ``cancel`` is declared on the provider ABC, so the guard is for a
        # session whose provider is gone, not a provider missing the method.
        sessions = FakeSessions(None)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/stop"))

        assert "Nothing was running" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_failing_cancel_does_not_prevent_the_clear(self) -> None:
        class Stubborn(FakeProvider):
            async def cancel(self, wait_ack_timeout: float = 0.0) -> None:
                raise RuntimeError("no")

        sessions = FakeSessions(Stubborn([]))
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/stop"))

        assert sessions.cleared == [d._session_key(_EMAIL)]


class TestMirrorBinding:
    @pytest.mark.asyncio
    async def test_a_turn_binds_the_room_as_origin_and_mirror(self) -> None:
        """The ROOM, not the ``webex:{email}`` attribution bucket.

        The room is what a send actually addresses; binding the bucket would
        record a target no client can post to.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        await d.handle_message(_inbound("hello"))

        assert sessions.origin_links[key].channel_id == "ROOM"
        assert sessions.origin_links[key].channel_type == "webex"
        assert sessions.mirror_links[key].channel_id == "ROOM"

    @pytest.mark.asyncio
    async def test_an_existing_binding_is_not_repointed(self) -> None:
        # The dashboard can aim a session's mirror anywhere; overwriting it would
        # silently redirect the user's replies into this room.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)
        elsewhere = ChannelLink("discord", channel_id="99", thread_id=None)
        sessions.mirror_links[key] = elsewhere

        await d.handle_message(_inbound("hello"))

        assert sessions.mirror_links[key] is elsewhere

    @pytest.mark.asyncio
    async def test_unlink_then_link_round_trips(self) -> None:
        """Unlink must PERSIST, or the next message re-binds it.

        The bind is re-asserted every turn, so a release without the opt-out
        would last exactly until the user typed again.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        await d.handle_message(_inbound("/unlink"))
        assert sessions.opt_out[key] is True

        await d.handle_message(_inbound("hello"))
        assert key not in sessions.mirror_links  # the opt-out held

        await d.handle_message(_inbound("/link"))
        assert sessions.opt_out[key] is False
        assert sessions.mirror_links[key].channel_id == "ROOM"

    @pytest.mark.asyncio
    async def test_link_and_unlink_use_one_spelling_of_this_room(self) -> None:
        # A release matches an occupied location by VALUE, so a second spelling
        # would let the unlink miss the binding the bind wrote.
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        assert d._origin_mirror_link("ROOM") == ChannelLink(
            "webex", channel_id="ROOM", thread_id=None
        )

    @pytest.mark.asyncio
    async def test_a_bind_failure_does_not_drop_the_turn(self) -> None:
        """The widest call site in the codebase: five channels route through it.

        Losing the mirror costs the user a dashboard convenience; raising here
        costs them the answer they are waiting for.
        """

        class Hostile(FakeSessions):
            def mirror_opt_out(self, key) -> bool:
                raise RuntimeError("session map unavailable")

        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = Hostile(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert sessions.successes  # the turn still completed


# ------------------------------------------------------------------
# Interactive approvals (typed reply)
# ------------------------------------------------------------------


class TestApprovals:
    @pytest.mark.asyncio
    async def test_interactive_mode_builds_a_decider(self) -> None:
        """Without one the driver denies by default.

        That is what made this channel effectively read-only: every tool the
        PreToolUse gate did not auto-approve was refused with no prompt.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        captured: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            captured.append(turn)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_inbound("hello"))

        assert captured[0].decider is not None

    @pytest.mark.asyncio
    async def test_auto_mode_builds_no_decider(self) -> None:
        # In auto the driver's own ladder approves without asking, so a prompt
        # would be noise nobody needs to answer.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = WebexDispatcher(
            sessions=sessions,
            ctx_builder=FakeCtx(),
            cfg=_cfg(),
            approval_mode="auto",
        )
        d.client = FakeClient()
        captured: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            captured.append(turn)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_inbound("hello"))

        assert captured[0].decider is None

    @pytest.mark.asyncio
    async def test_the_session_auto_approve_predicate_is_forwarded(self) -> None:
        """Without it, /yolo and per-session Trust are silently inert here.

        The predicate is read PER REQUEST rather than captured at turn start, so
        a grant taken or revoked mid-turn takes effect on the next tool.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        captured: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            captured.append(turn)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_inbound("hello"))

        predicate = captured[0].auto_approve_session
        assert predicate is not None
        with mock.patch("kiro_crew.webex.transport_dispatch.safety_override") as so:
            so.return_value.is_active.return_value = True
            assert predicate() is True
            so.return_value.is_active.return_value = False
            assert predicate() is False

    @pytest.mark.asyncio
    async def test_an_approve_reply_resolves_the_prompt_and_does_not_steer(self) -> None:
        """The intercept runs BEFORE the steer path.

        The session semaphore is held for the whole turn, so an approval answer
        necessarily arrives while the session is busy. Without the intercept, "1"
        is folded into the running turn as a mid-turn instruction and the tool
        request is left to time out.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("1"))

        assert (await task)[0] is True
        assert provider.steered == []
        assert "Approved" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_deny_reply_resolves_the_prompt(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("2"))

        assert (await task)[0] is False
        assert "Denied" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_unrecognised_reply_still_steers(self) -> None:
        """A user who ignores the prompt must not lose their message.

        Consuming anything that arrives while a prompt is open would swallow a
        real instruction, which is worse than not recognising it: the user gets
        no signal at all.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("actually use ripgrep"))

        assert provider.steered == ["actually use ripgrep"]
        assert task.done() is False
        webex_dispatch._APPROVALS.resolve(key, False)
        await task

    @pytest.mark.asyncio
    async def test_a_reply_that_lost_the_race_is_told_the_prompt_expired(self) -> None:
        """Reporting "Approved" for a prompt that is not pending would tell
        the user a tool ran when it did not.

        And the report is deliberately NEUTRAL rather than "denied": an unmatched
        answer means the window timed out (denied) OR the other affordance already
        answered (possibly approved), and with buttons Webex cannot retire the
        second is the common case.

        Reached only by a race on the typed path: ``has_pending`` gates the
        intercept, so the prompt has to be retired between that check and the
        resolve. Exercised directly, because a scheduling race is not something a
        test should try to provoke.
        """
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        consumed = await d._maybe_answer_approval(_inbound("1"), key, permitted=True)

        assert consumed is True  # the answer is not re-read as chat
        body = client.sent[-1][1]
        assert "already answered or timed out" in body
        assert "denied" not in body.lower()

    @pytest.mark.asyncio
    async def test_a_reply_arriving_with_no_prompt_open_is_ordinary_chat(self) -> None:
        # The intercept is gated on a pending prompt, so a message of "1" with
        # nothing waiting must reach the model, not be eaten as an approval.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("1"))

        assert sessions.successes  # a real turn ran
        assert not any("Approved" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_a_governance_deny_blocks_approve_but_still_resolves_deny(self) -> None:
        """A policy that forbids this channel has no interest in keeping a tool
        request alive.

        Dropping the deny too would strand the provider's permission request for
        the whole approval window with the turn holding the semaphore.
        """
        sessions = FakeSessions(FakeProvider([]))
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)

        with mock.patch(
            "kiro_crew.webex.transport_dispatch.inbound_permitted",
            new=mock.AsyncMock(return_value=False),
        ):
            approve_task = asyncio.create_task(
                webex_dispatch._APPROVALS.decide_with_cause(key, SimpleNamespace(request_id=1))
            )
            await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
            await d.handle_message(_inbound("1"))
            assert approve_task.done() is False  # approve dropped

            await d.handle_message(_inbound("2"))
            assert (await approve_task)[0] is False  # deny got through

    @pytest.mark.asyncio
    async def test_unified_scope_does_not_let_one_user_answer_anothers_prompt(self) -> None:
        """Under ``dm_scope=unified`` every allowed user's DM collapses onto one
        session key, so keying the approval registry on it would let user B's
        ``1`` resolve user A's pending tool prompt. The registry is keyed per
        CONVERSATION (route), which does not collapse, so it does not.
        """
        cfg = _cfg()
        cfg.messaging.dm_scope = "unified"
        sessions = FakeSessions(FakeProvider([]))
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=cfg)

        email_a, email_b = "a@example.com", "b@example.com"
        # The two DMs share ONE session key under unified...
        assert d._session_key(email_a) == d._session_key(email_b)
        # ...but the approval key is per conversation, so they do not.
        key_a = d._approval_key(email_a)
        assert d._approval_key(email_b) != key_a

        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide_with_cause(key_a, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key_a))

        # User B answers "1" in THEIR DM — it must not resolve user A's prompt.
        await d.handle_message(_inbound("1", email=email_b))
        assert task.done() is False, "user B resolved user A's pending approval"

        # User A's own "1" resolves it.
        await d.handle_message(_inbound("1", email=email_a))
        assert (await task)[0] is True


# ------------------------------------------------------------------
# Mid-turn queue + drain
# ------------------------------------------------------------------


class TestAReceiptAddressNamesTheThreadItLivesIn:
    """Two threads of one room share a session key, so the ADDRESS must part them.

    A group space routes as ``space:{room_id}``, so every thread in it lands on one
    queue entry and one bubble. The bubble hangs under the thread its first send
    threaded into, and it may only ever show what arrived there -- a room-only
    address answers "same conversation" for a sibling thread and renders that
    thread's text where the other thread's readers are.
    """

    def _surface(self, *, parent: str, email: str = _EMAIL, threaded: bool = True):
        cfg = _cfg_queue()
        cfg.webex.reply_in_thread = threaded
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient(), cfg=cfg)
        return d._receipt_surface(
            WebexInbound(
                person_email=email, room_id="ROOM", text="x", room_type="group", parent_id=parent
            )
        )

    def test_two_threads_of_one_room_are_two_addresses(self) -> None:
        """The fixed disclosure: thread B must not be addressed to thread A's bubble."""
        a = self._surface(parent="THREAD_A")
        b = self._surface(parent="THREAD_B")
        assert a.address_key != b.address_key

    def test_one_thread_is_one_address_for_every_member(self) -> None:
        """Still SHARED within a thread, which is the whole point of a space bubble.

        A second member's mid-turn message must keep updating the one bubble, so the
        address may not narrow to the sender.
        """
        mine = self._surface(parent="THREAD_A")
        theirs = self._surface(parent="THREAD_A", email="other@example.com")
        assert mine.address_key == theirs.address_key
        assert mine.address_key

    def test_the_room_root_is_a_named_address_not_an_unknown_one(self) -> None:
        """A receipt outside any thread still needs an address, or no bubble opens.

        ``receipt_address_key`` reads an empty part as UNKNOWN and returns ``""``, and
        a surface with no address opens nothing -- so the root case would have lost
        its receipts entirely rather than merely sharing one.
        """
        root = self._surface(parent="")
        assert root.address_key
        assert root.address_key != self._surface(parent="THREAD_A").address_key

    def test_without_threaded_replies_the_whole_room_is_one_address(self) -> None:
        """With ``reply_in_thread`` off every send lands in the room root.

        The address tracks where the bubble actually IS, so two threads then share
        one address -- the same place their receipts share.
        """
        a = self._surface(parent="THREAD_A", threaded=False)
        b = self._surface(parent="THREAD_B", threaded=False)
        assert a.address_key == b.address_key
        assert a.address_key == self._surface(parent="", threaded=False).address_key

    def test_the_root_stand_in_cannot_be_mistaken_for_a_thread(self) -> None:
        """A real thread root is an opaque base64 id, which has no colon in it."""
        assert webex_dispatch._ROOT_THREAD
        assert ":" in webex_dispatch._ROOT_THREAD
        real = webex_client.hydra_id("2f1e6d5c-4b3a-2918-0706-f5e4d3c2b1a0")
        assert ":" not in real
        assert self._surface(parent=real).address_key != self._surface(parent="").address_key


class TestQueueAndDrain:
    @pytest.mark.asyncio
    async def test_the_surface_reports_whether_the_edit_landed(self) -> None:
        """Webex documents a per-message edit cap, so a refusal here is ordinary. The
        registry can only keep such a transition retryable if the wrapper reports it
        rather than swallowing the client's answer."""
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client, cfg=_cfg_queue())
        surface = d._receipt_surface(_inbound("x"))

        client.edit_ok = True
        assert await surface.edit_receipt("m1", "body") is True
        client.edit_ok = False
        assert await surface.edit_receipt("m1", "body") is False

    @pytest.mark.asyncio
    async def test_queue_mode_enqueues_with_a_receipt(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg_queue())

        await d._handle_busy(_inbound("later"), d._session_key(_EMAIL), "later")

        assert [t for _ts, t, _kw in sessions.queued] == ["later"]
        assert provider.steered == []
        assert any("later" in m for (_, m) in client.sent)  # the receipt bubble

    @pytest.mark.asyncio
    async def test_a_queue_override_beats_steer_mode(self) -> None:
        # ``/queue <msg>`` overrides messaging.queue_mode for THIS message only.
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient())  # cfg is steer mode

        await d.handle_message(_inbound("/queue check the logs"))

        assert [t for _ts, t, _kw in sessions.queued] == ["check the logs"]
        assert provider.steered == []

    @pytest.mark.asyncio
    async def test_a_steer_override_beats_queue_mode(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=_cfg_queue())

        await d.handle_message(_inbound("/steer use ripgrep"))

        assert provider.steered == ["use ripgrep"]
        assert sessions.queued == []

    @pytest.mark.asyncio
    async def test_the_drain_collapses_a_burst_in_order(self) -> None:
        """One combined turn, order preserved, rather than a turn each.

        Replaying each separately would make the agent answer a burst of related
        messages as if they were unrelated.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        sessions.queued = [_entry("1", "first"), _entry("2", "second")]
        prompts: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            prompts.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        assert prompts == ["first\n\nsecond"]

    @pytest.mark.asyncio
    async def test_the_drain_defers_past_the_collapse_cap_in_order(self) -> None:
        # Once one message does not fit, it AND everything behind it are
        # deferred, so queue order stays exact rather than being reordered.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [_entry(str(i), f"m{i}") for i in range(_MAX_COLLAPSE + 2)]
        prompts: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            prompts.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        expected_first = "\n\n".join(f"m{i}" for i in range(_MAX_COLLAPSE))
        assert prompts[0] == expected_first
        assert prompts[1] == f"m{_MAX_COLLAPSE}\n\nm{_MAX_COLLAPSE + 1}"

    @pytest.mark.asyncio
    async def test_the_drain_never_answers_one_room_into_another(self) -> None:
        """Under a unified key two allowed people share ONE queue.

        So a message B queued during A's turn must not be collapsed into A's turn
        and answered into A's room: that discloses B's text to A and delivers B's
        answer nowhere. Same-room messages still collapse; a different room defers
        itself and everything behind it, and drains next as its own turn in its own
        envelope.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE), AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry("1", "a-one", room="ROOM_A", email="a@example.com"),
            _entry("2", "a-two", room="ROOM_A", email="a@example.com"),
            _entry("3", "b-one", room="ROOM_B", email="b@example.com"),
        ]
        turns: list[tuple[str, str]] = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append((turn.user_text, turn.conversation_id))

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        # A's two collapse together; B's is a separate turn, never merged into A's.
        assert turns[0][0] == "a-one\n\na-two"
        assert turns[1][0] == "b-one"
        assert "b-one" not in turns[0][0]

    @pytest.mark.asyncio
    async def test_the_drain_never_answers_one_thread_into_another(self) -> None:
        """Two threads share ONE room id, so the room alone is not the conversation.

        With ``reply_in_thread`` the reply parent IS the inbound's ``parent_id``, so
        a message queued in thread B during thread A's turn must not be collapsed
        into A's turn — that answers B inside A's thread, where Webex either
        misplaces it or rejects the parent outright.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE), AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            _entry("1", "t-a", parent="THREAD_A"),
            _entry("2", "t-a2", parent="THREAD_A"),
            _entry("3", "t-b", parent="THREAD_B"),
        ]
        turns: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        assert turns[0] == "t-a\n\nt-a2"
        assert turns[1] == "t-b"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("queued_person", "opener_person", "expected"),
        [
            # A person's queued message drains FOREGROUND whoever opened the turn.
            (True, False, "fg"),
            # A gateway-built wake that reached the queue stays BACKGROUND, even
            # behind a person's turn.
            (False, True, "bg"),
        ],
    )
    async def test_a_drained_turn_takes_the_queued_entrys_own_person_flag(
        self, queued_person, opener_person, expected
    ) -> None:
        """The replay is built ON the finished turn's inbound, so the opener's flag is
        the wrong source both ways: the entry records its own at enqueue time."""
        from kiro_crew.start_priority import StartPriority

        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        queued = replace(_inbound("queued"), person_origin=queued_person)
        assert await d._enqueue_with_receipt(d._session_key(_EMAIL), "queued", queued)
        sessions._busy = False
        priorities: list[StartPriority] = []

        async def _capture(turn, *, sessions, ctx_builder):
            priorities.append(turn.start_priority)

        opener = replace(_inbound("opener"), person_origin=opener_person)
        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(d._session_key(_EMAIL), opener)

        assert priorities == [StartPriority(expected)]

    @pytest.mark.asyncio
    async def test_an_empty_queue_drains_to_nothing(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        await d._drain_queue(d._session_key(_EMAIL), _inbound("x"))

        assert sessions.successes == []


# ------------------------------------------------------------------
# Compaction honesty
# ------------------------------------------------------------------


class TestCompactionHonesty:
    @pytest.mark.asyncio
    async def test_a_timeout_result_is_not_reported_as_success(self) -> None:
        """The defect this replaces needed no hang at all.

        The ACP client synthesizes a completion whenever text streamed, so
        ``compact()`` returns normally having compacted nothing and
        ``wait_for_compaction()`` reports a timeout. Announcing success there
        tells the user their context shrank, and they stop doing the one thing
        that would have helped.
        """

        class Silent(FakeProvider):
            async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
                return {"type": "timeout"}

        sessions = FakeSessions(Silent([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        reply = client.sent[-1][1]
        assert "timed out" in reply and "/new" in reply
        assert "🗜️ Context compacted." not in reply

    @pytest.mark.asyncio
    async def test_a_failed_result_is_not_reported_as_success(self) -> None:
        class Failing(FakeProvider):
            async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
                return {"type": "failed"}

        sessions = FakeSessions(Failing([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert "failed" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_completed_result_reports_success(self) -> None:
        sessions = FakeSessions(FakeProvider([]))  # returns {"type": "completed"}
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert "🗜️ Context compacted." in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_unrecognised_result_type_reports_success(self) -> None:
        # Enumerating FAILURES rather than allow-listing success: an unknown type
        # is far more likely a renamed success than a new failure, and claiming
        # failure would tell a user whose context did shrink to start over.
        class Odd(FakeProvider):
            async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
                return {"type": "compacted-v2"}

        sessions = FakeSessions(Odd([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert "🗜️ Context compacted." in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_compaction_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The provider's own prompt deadline is measured in HOURS.

        The caller holds the session semaphore for the whole wait, so an
        unbounded compaction makes the conversation look permanently busy.
        """
        monkeypatch.setattr(webex_dispatch, "_COMPACT_TIMEOUT_S", 0.01)

        class Hanging(FakeProvider):
            async def compact(self) -> None:
                await asyncio.sleep(60)

        sessions = FakeSessions(Hanging([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert "timed out" in client.sent[-1][1]
        assert sessions.released  # the semaphore came back
