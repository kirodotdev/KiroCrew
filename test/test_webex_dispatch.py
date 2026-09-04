"""Tests for kiro_crew.webex.transport_dispatch (WebexDispatcher) + commands."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.webex import cards
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
# Fakes
# ------------------------------------------------------------------


class FakeProvider:
    supports_steer = True

    def __init__(self, events: list) -> None:
        self._events = events
        self.compacted = False
        self.steered: list = []
        self.active_turn = True
        self.cancelled: list = []

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def stream(self, message: str):
        for ev in self._events:
            yield ev

    async def approve_tool(self, rid) -> None:
        pass

    async def reject_tool(self, rid) -> None:
        pass

    async def cancel(self, wait_ack_timeout: float = 0.0) -> None:
        self.cancelled.append(wait_ack_timeout)

    async def compact(self) -> None:
        self.compacted = True

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": ""}


class FakeSessions:
    def __init__(
        self,
        provider,
        *,
        is_new=True,
        raise_on_get=None,
        ctx_pct=0.0,
        acquire=True,
        has_session=None,
    ) -> None:
        self._p = provider
        self._is_new = is_new
        self._raise = raise_on_get
        self._ctx_pct = ctx_pct
        self._acquire = acquire
        self._has_session = provider is not None if has_session is None else has_session
        self.acquired: list = []
        self.released: list = []
        self.successes: list = []
        self.failures: list = []
        self.channels: list = []
        self.last_agent = None
        self.queued: list = []
        self.cleared: list = []
        self.origin_links: dict = {}
        self.mirror_links: dict = {}
        self.opt_out: dict = {}
        self.batched = 0
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0

    async def get_or_create(self, key, *, agent, channel_id):
        self.last_agent = agent
        if self._raise is not None:
            raise self._raise
        return self._p, self._is_new, False

    def begin_turn(self, key):
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    async def set_channel(self, key, cid) -> None:
        self.channels.append((key, cid))

    def release(self, key) -> None:
        self.released.append(key)

    def record_success(self, key) -> None:
        self.successes.append(key)

    async def record_failure(self, key) -> None:
        self.failures.append(key)

    def check_context_usage(self, key, provider) -> float:
        return self._ctx_pct

    def get_provider(self, key):
        return self._p

    async def try_acquire(self, key) -> bool:
        self.acquired.append(key)
        return self._acquire

    def has_session(self, key) -> bool:
        return self._has_session

    def is_busy(self, key) -> bool:
        return getattr(self, "_busy", False)

    def max_generation(self, bucket: str) -> int:
        return -1

    # -- mid-turn queue (drive_turn's drain + /stop) --
    def enqueue(self, key, ts, text, *, force=False, **kw) -> bool:
        if not force and not self.is_busy(key):
            return False
        self.queued.append((str(ts), text, dict(kw)))
        return True

    def dequeue(self, key):
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key) -> None:
        self.cleared.append(key)
        self.queued.clear()

    # -- origin / mirror binding (drive_turn binds on every turn) --
    def set_origin_link(self, key, link) -> None:
        self.origin_links[key] = link

    def mirror_opt_out(self, key) -> bool:
        return self.opt_out.get(key, False)

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_out[key] = bool(value)

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> int:
        # Returns a COUNT, matching SessionMap: release_conversation_location
        # sums these to decide between "Unlinked" and "wasn't linked".
        return 1 if self.mirror_links.pop(key, None) is not None else 0

    def clear_mirror_links_at(self, location, *, reason="") -> list[str]:
        swept = [k for k, v in self.mirror_links.items() if v == location]
        for k in swept:
            self.mirror_links.pop(k, None)
        return swept

    @contextmanager
    def batched_save(self):
        self.batched += 1
        yield


class _GateResult:
    def __init__(self, action: str = "") -> None:
        self.action = action


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, title, **kw):
        return _GateResult("")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = FakeHooks()

    # Names only the kwargs it asserts on and takes ``**kw`` for the rest: the
    # shared pipeline owns the call shape, so a fake that pins every kwarg
    # breaks on any field the pipeline later forwards, without that field
    # having anything to do with this channel. Mirrors the pipeline's own fake.
    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source, **kw):
        assert runtime_source == "webex"
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[str, str, str]] = []
        self.deleted: list[str] = []
        self._next_id = 0

    async def send_message(self, conversation_id: str, markdown: str, **kw) -> str:
        self.sent.append((conversation_id, markdown))
        self._next_id += 1
        return f"MSG{self._next_id}"

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        self.edits.append((message_id, room_id, markdown))
        return True

    async def delete_message(self, message_id: str) -> None:
        self.deleted.append(message_id)


class FakeConvLog:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self.titles: dict[str, str] = {}

    def append(self, key, role, text, agent=None, mid=None) -> None:
        self.appended.append((key, role, text))

    def set_title(self, key, title) -> None:
        self.titles[key] = title


def _cfg(default_agent: str = "", approval_mode: str = "interactive"):
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent=default_agent, approval_mode=approval_mode),
        webex=SimpleNamespace(
            hard_threshold_pct=95.0,
            soft_threshold_pct=80.0,
            allowed_emails=[_EMAIL],
            allow_group_rooms=False,
            allowed_room_ids=[],
            reply_in_thread=True,
            wdm_base="",
        ),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(sessions, ctx, client, *, conv_log=None, agent=None, cfg=None):
    d = WebexDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=cfg or _cfg(),
        agent=agent,
        conv_log=conv_log,
        approval_mode="interactive",
    )
    d.client = client
    return d


def _cfg_queue():
    cfg = _cfg()
    cfg.messaging.queue_mode = "queue"
    return cfg


async def _spin(predicate, timeout: float = 1.0) -> None:
    """Poll *predicate* until true. Polling, not sleeping — a fixed sleep long
    enough to be reliable on a loaded box also dominates the suite."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


_EMAIL = "kyle@example.com"


def _inbound(text: str = "hello", email: str = _EMAIL) -> WebexInbound:
    return WebexInbound(person_email=email, room_id="ROOM", text=text, room_type="direct")


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
        # HIGH (GPT round-4 #2): a channels DENY must stop handle_message from
        # driving a turn. Regression-locks the Webex inbound chokepoint.
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
        # own as context fills (#8156).
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
        # on its own, so there is nothing for the user to act on (#8156).
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
        # compact() is NEVER dispatched (#8156).
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
            webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("1"))

        assert await task is True
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
            webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("2"))

        assert await task is False
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
            webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        await d.handle_message(_inbound("actually use ripgrep"))

        assert provider.steered == ["actually use ripgrep"]
        assert task.done() is False
        webex_dispatch._APPROVALS.resolve(key, False)
        await task

    @pytest.mark.asyncio
    async def test_a_reply_that_lost_the_race_is_told_the_prompt_expired(self) -> None:
        """Reporting "Approved" for a prompt that is no longer pending would tell
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
                webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=1))
            )
            await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
            await d.handle_message(_inbound("1"))
            assert approve_task.done() is False  # approve dropped

            await d.handle_message(_inbound("2"))
            assert await approve_task is False  # deny got through

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
            webex_dispatch._APPROVALS.decide(key_a, SimpleNamespace(request_id=1))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key_a))

        # User B answers "1" in THEIR DM — it must not resolve user A's prompt.
        await d.handle_message(_inbound("1", email=email_b))
        assert task.done() is False, "user B resolved user A's pending approval"

        # User A's own "1" resolves it.
        await d.handle_message(_inbound("1", email=email_a))
        assert await task is True


# ------------------------------------------------------------------
# Mid-turn queue + drain
# ------------------------------------------------------------------


class TestQueueAndDrain:
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
        sessions.queued = [("1", "first", {}), ("2", "second", {})]
        prompts: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            prompts.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

        assert prompts == ["first\n\nsecond"]

    @pytest.mark.asyncio
    async def test_the_drain_defers_past_the_collapse_cap_in_order(self) -> None:
        # Once one message no longer fits, it AND everything behind it are
        # deferred, so queue order stays exact rather than being reordered.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [(str(i), f"m{i}", {}) for i in range(_MAX_COLLAPSE + 2)]
        prompts: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            prompts.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

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
            ("1", "a-one", {"webex_room_id": "ROOM_A", "webex_person_email": "a@example.com"}),
            ("2", "a-two", {"webex_room_id": "ROOM_A", "webex_person_email": "a@example.com"}),
            ("3", "b-one", {"webex_room_id": "ROOM_B", "webex_person_email": "b@example.com"}),
        ]
        turns: list[tuple[str, str]] = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append((turn.user_text, turn.conversation_id))

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

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
            ("1", "t-a", {"webex_room_id": "ROOM", "webex_parent_id": "THREAD_A"}),
            ("2", "t-a2", {"webex_room_id": "ROOM", "webex_parent_id": "THREAD_A"}),
            ("3", "t-b", {"webex_room_id": "ROOM", "webex_parent_id": "THREAD_B"}),
        ]
        turns: list[str] = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

        assert turns[0] == "t-a\n\nt-a2"
        assert turns[1] == "t-b"

    @pytest.mark.asyncio
    async def test_an_empty_queue_drains_to_nothing(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

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


# ------------------------------------------------------------------
# /yolo — the global auto-approve grant
# ------------------------------------------------------------------


class TestYolo:
    @staticmethod
    def _grant(active: bool = False):
        so = mock.MagicMock()
        so.is_active.return_value = active
        so.activate.return_value = SimpleNamespace(active=True)
        so.renew.return_value = SimpleNamespace(renewed=True)
        return so

    @pytest.mark.asyncio
    async def test_a_bare_yolo_reports_status_and_usage(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=self._grant()):
            await d.handle_message(_inbound("/yolo"))
        reply = d.client.sent[-1][1]
        assert "OFF" in reply and "on | off | renew" in reply

    @pytest.mark.asyncio
    async def test_yolo_on_activates_and_audits(self) -> None:
        """Credential-shaped grants MUST be audited.

        A grant that auto-approves every tool is exactly the event an incident
        review needs to find, and the channel is where it was taken.
        """
        so = self._grant()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/yolo on"))
        so.activate.assert_called_once_with("webex")
        assert "YOLO ON" in d.client.sent[-1][1]
        op = sel_mock.return_value.log_api_access.call_args.kwargs
        assert op["operation"] == "webex.yolo_mode"
        assert op["outcome"] == "allowed"

    @pytest.mark.asyncio
    async def test_yolo_on_when_already_active_does_not_re_activate(self) -> None:
        so = self._grant(active=True)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo on"))
        so.activate.assert_not_called()
        assert "already ON" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_refused_activation_is_reported_as_denied(self) -> None:
        so = self._grant()
        so.activate.return_value = SimpleNamespace(active=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/yolo on"))
        assert "Couldn't turn YOLO on" in d.client.sent[-1][1]
        assert sel_mock.return_value.log_api_access.call_args.kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_yolo_off_is_unconditional(self) -> None:
        """Deactivate runs even when the grant already lapsed.

        It also zeroes a lapsed grant's deadline, which closes the renew grace
        window so a later "/yolo renew" cannot resurrect it — and it records the
        operator's decision either way.
        """
        so = self._grant(active=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo off"))
        so.deactivate.assert_called_once_with("webex")
        assert "YOLO OFF" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_renewing_an_inactive_grant_says_so(self) -> None:
        so = self._grant()
        so.renew.return_value = SimpleNamespace(renewed=False)
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        with mock.patch.object(webex_dispatch, "safety_override", return_value=so):
            await d.handle_message(_inbound("/yolo renew"))
        assert "not active" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# /kirocrew dashboard — presigned login link
# ------------------------------------------------------------------


class TestDashboardLink:
    #: A stub token at the REAL length. A live token is ~218 characters (base64url
    #: payload + HMAC signature), and the exfiltration redactor fires on a query
    #: string past 200 — so a short stub asserts the link is delivered while every
    #: real link is redacted into an unusable one.
    TOKEN = "a1b2c3d4" * 27

    @pytest.mark.parametrize("origin", ["http://localhost:8765", "http://127.0.0.1:8765"])
    @pytest.mark.asyncio
    async def test_a_link_is_minted_capped_and_audited(self, origin: str) -> None:
        """Both origins, because only one of them exposed the bug.

        The exfiltration redactor flags an IP-literal host and not ``localhost``,
        so a test pinned to the default fallback origin passed while every
        operator who set ``dashboard.url`` to an IP got an unusable link.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url=origin)

        with mock.patch(
            "kiro_crew.dashboard.token_auth.generate_token", return_value=self.TOKEN
        ) as gen:
            with mock.patch.object(webex_dispatch, "sel") as sel_mock:
                await d.handle_message(_inbound("/kirocrew dashboard 2h"))

        assert gen.call_args.kwargs["ttl_seconds"] == 7200
        # The WHOLE token, not a prefix: a redacted link would still contain
        # "token=" and the failure is that it no longer authenticates.
        assert f"token={self.TOKEN}" in d.client.sent[-1][1]
        op = sel_mock.return_value.log_api_access.call_args.kwargs
        assert op["operation"] == "webex.dashboard_token"
        assert op["outcome"] == "ok"

    @pytest.mark.asyncio
    async def test_a_non_direct_room_is_refused(self) -> None:
        """A presigned link is a credential.

        The direct-room assertion is kept local rather than inherited from the
        transport's gate: that gate is a property of another layer, which a future
        group capability could relax.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        inbound = WebexInbound(
            person_email=_EMAIL, room_id="ROOM", text="/kirocrew dashboard", room_type="group"
        )

        with mock.patch("kiro_crew.dashboard.token_auth.generate_token") as gen:
            await d._handle_dashboard(inbound)

        gen.assert_not_called()
        assert "direct message" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_bare_kirocrew_answers_with_usage(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/kirocrew"))
        assert "Usage:" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_token_failure_reports_the_type_not_the_message(self) -> None:
        # The exception message can embed the dashboard URL or a response body.
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url="http://localhost:8765")

        with mock.patch(
            "kiro_crew.dashboard.token_auth.generate_token",
            side_effect=RuntimeError("secret=abc"),
        ):
            await d.handle_message(_inbound("/kirocrew dashboard"))

        reply = d.client.sent[-1][1]
        assert "RuntimeError" in reply and "secret=abc" not in reply

    @pytest.mark.parametrize(
        "arg,secs",
        [("dashboard 2h", 7200), ("dashboard 30m", 1800), ("dashboard 90m", 5400)],
    )
    def test_a_valid_duration_comes_from_the_shared_grammar(self, arg: str, secs: int) -> None:
        """The grammar belongs to ``token_auth.parse_duration``, not this channel.

        Only the token SPLITTING is local, so a unit added to the shared parser
        reaches this command instead of diverging silently.
        """
        from kiro_crew.dashboard.token_auth import parse_duration

        assert parse_duration(webex_dispatch._ttl_spec(arg)) == secs

    @pytest.mark.parametrize(
        "arg",
        ["dashboard", "dashboard xx", "dashboard -1h", "dashboard 5d", "dashboard 2hh"],
    )
    @pytest.mark.asyncio
    async def test_an_unusable_duration_falls_back_to_the_default(self, arg: str) -> None:
        """The user asked for a link; refusing one over a typo trades a working
        link for a lecture. The shared parser returns None and the call site
        supplies the default."""
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.cfg.dashboard = SimpleNamespace(url="http://localhost:8765")

        with mock.patch("kiro_crew.dashboard.token_auth.generate_token", return_value="TOK") as gen:
            await d.handle_message(_inbound(f"/kirocrew {arg}"))

        assert gen.call_args.kwargs["ttl_seconds"] == 3600

    def test_the_ttl_is_clamped_to_the_server_maximum(self) -> None:
        # A user-supplied duration must not be able to widen the login window.
        from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, parse_duration

        assert parse_duration(webex_dispatch._ttl_spec("dashboard 9999h")) == MAX_SESSION_TTL_SECS

    @pytest.mark.parametrize(
        "secs,shown", [(3600, "1h"), (7200, "2h"), (1800, "30m"), (5400, "1h 30m"), (30, "1m")]
    )
    def test_ttl_display(self, secs: int, shown: str) -> None:
        assert webex_dispatch._format_ttl(secs) == shown


class TestDrainIsFlat:
    @pytest.mark.asyncio
    async def test_the_replayed_turn_does_not_start_its_own_drain(self) -> None:
        """The drain loop is the pump, and it must stay FLAT.

        Letting each replayed turn drain too would nest one Python frame per
        burst, so a sustained burst grows the stack without bound — and the
        nested call would re-enter with the queue in a state its caller has
        already read.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [(str(i), f"m{i}", {}) for i in range(_MAX_COLLAPSE + 2)]
        depth = {"now": 0, "max": 0}
        real_drain = d._drain_queue

        async def _counting(inbound, session_key):
            depth["now"] += 1
            depth["max"] = max(depth["max"], depth["now"])
            try:
                await real_drain(inbound, session_key)
            finally:
                depth["now"] -= 1

        with mock.patch.object(d, "_drain_queue", _counting):
            with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _noop_turn):
                await d._drain_queue(_inbound("x"), d._session_key(_EMAIL))

        assert depth["max"] == 1, "the drain re-entered itself"
        assert sessions.queued == []  # everything still drained


async def _noop_turn(turn, *, sessions, ctx_builder):
    """A drive_turn stand-in: the drain's behaviour is what is under test."""
    return None


# ------------------------------------------------------------------
# Tests: Adaptive Card presses
# ------------------------------------------------------------------


def _press(
    inputs: dict,
    *,
    email: str = _EMAIL,
    room_type: str = "direct",
    room_id: str = "ROOM",
) -> WebexInbound:
    """A card-press envelope.

    Shaped exactly like a message envelope — including ``room_type``, which the
    client resolves before dispatch because an attachment-action record carries
    none and the room gate is a type decision.
    """
    return WebexInbound(
        person_email=email,
        room_id=room_id,
        text="",
        room_type=room_type,
        card_inputs=inputs,
    )


def _options_press(index: str, nonce: str, **kw) -> WebexInbound:
    return _press(
        {
            cards.KEY_KIND: cards.KIND_OPTIONS,
            cards.KEY_CHOICE: index,
            cards.KEY_NONCE: nonce,
        },
        **kw,
    )


def _approval_press(choice: str, nonce: str, request_id: str = "1", **kw) -> WebexInbound:
    return _press(
        {
            cards.KEY_KIND: cards.KIND_APPROVAL,
            cards.KEY_CHOICE: choice,
            cards.KEY_NONCE: nonce,
            cards.KEY_REQUEST: request_id,
        },
        **kw,
    )


class TestOptionsCardPress:
    @pytest.mark.asyncio
    async def test_a_press_runs_the_choice_as_a_turn_and_echoes_it(self) -> None:
        """The card is only useful if a press reaches the model.

        It is published by a renderer that is gone by the time the press arrives —
        the card is the LAST thing a turn sends — so the store has to outlive the
        turn or every press answers "no longer current".
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["Keep going", "Stop"])
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        assert turns == ["Keep going"]
        # Echoed: a press leaves no trace in the room, so without this the answer
        # arrives with nothing above it saying which option it answers.
        assert client.sent[0][1] == "> Keep going"

    @pytest.mark.asyncio
    async def test_a_choice_that_looks_like_a_command_is_not_executed(self) -> None:
        """A choice LABEL is model-authored text.

        `[OPTIONS: Keep going | /yolo on]` would otherwise render a button whose
        single press takes the process-global auto-approve grant, and any label
        starting with `/` would be answered with the unknown-command card and the
        choice dropped.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["/yolo on"])
        before = d._conv.current_gen(_EMAIL)
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        assert turns == ["/yolo on"]
        assert d._conv.current_gen(_EMAIL) == before
        assert not any("YOLO" in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_a_stale_press_is_told_so_and_runs_nothing(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        turns: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            turns.append(turn.user_text)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "NOPE"))

        assert turns == []
        assert "no longer current" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_unauthorized_press_is_dropped_and_audited(self) -> None:
        """A card lives in a room and Webex lets anyone in that room press it.

        The transport's ROOM gate has already run; this is the sender check, which
        for a press has to resolve a person id to an email first.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        key = d._session_key("intruder@example.com")
        d._choices.publish(key, "N1", ["Keep going"])

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_options_press("0", "N1", email="intruder@example.com"))

        assert client.sent == []
        outcomes = [c.kwargs["outcome"] for c in fake_sel.return_value.log_api_access.mock_calls]
        assert "denied" in outcomes

    @pytest.mark.asyncio
    async def test_a_press_that_is_not_ours_is_ignored(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        await d.handle_message(_press({"unrelated": "x"}))
        assert client.sent == []


#: A presigned dashboard URL at its REAL length. A live token is ~218 characters
#: (base64url payload + HMAC signature), well past the exfiltration redactor's
#: 200-character query threshold — so a token short enough to slip under it would
#: make these tests pass while every real link stayed broken.
_DASHBOARD_LINK = "http://127.0.0.1:8765/?token=" + ("a1b2c3d4" * 27)


class TestOutboundRedaction:
    """Not everything the dispatcher sends is its own copy.

    A card press echoes a MODEL-authored label, `/sessions` prints titles that are
    the opening words of user messages, and a queue receipt quotes the message it
    queued — so the display scan sits at the choke point rather than at whichever
    call site remembered.
    """

    @pytest.mark.asyncio
    async def test_an_option_echo_cannot_reassemble_a_credential(self) -> None:
        """The choice text is model-authored.

        `AKIA**IOSF**ODNN7EXAMPLE` does not match a credential pattern as written,
        and Webex's own renderer removes the emphasis and shows the intact key.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        client = FakeClient()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["AKIA**IOSF**ODNN7EXAMPLE"])

        async def _capture(turn, *, sessions, ctx_builder):
            pass

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_options_press("0", "N1"))

        echo = client.sent[0][1]
        assert "AKIAIOSFODNN7EXAMPLE" not in echo.replace("*", "")

    @pytest.mark.asyncio
    async def test_our_own_markdown_survives_the_scan(self) -> None:
        # The scan only downgrades markup when the canonical form actually reveals
        # a credential, so /help keeps its backticks.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d.handle_message(_inbound("/help"))

        assert "`/new`" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_minted_dashboard_link_is_not_redacted(self) -> None:
        """The scan is RIGHT about this text, which is why the exemption exists.

        A presigned dashboard URL looks exactly like the credential-bearing link
        the exfiltration redactor is built to catch, so scanning it would deliver a
        login link that cannot log in.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d._reply(_inbound("x"), _DASHBOARD_LINK, self_minted=True)

        assert client.sent[-1][1] == _DASHBOARD_LINK

    @pytest.mark.asyncio
    async def test_the_exemption_is_not_the_default(self) -> None:
        # The same URL from an unexempt caller is still redacted, so the exemption
        # cannot be inherited by anything that forgot to think about it.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)

        await d._reply(_inbound("x"), _DASHBOARD_LINK)

        assert client.sent[-1][1] != _DASHBOARD_LINK
        assert "REDACTED" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_queue_receipt_scans_the_text_it_quotes(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        sessions.queued = []
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        cfg = _cfg_queue()
        d.cfg = cfg

        await d.handle_message(_inbound("also AKIA**IOSF**ODNN7EXAMPLE"))

        bodies = " ".join(m for (_c, m) in client.sent)
        assert "AKIAIOSFODNN7EXAMPLE" not in bodies.replace("*", "")


class TestApprovalCardPress:
    @staticmethod
    async def _pending(d: WebexDispatcher, key: str, request_id: int = 1):
        task = asyncio.create_task(
            webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=request_id))
        )
        await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
        return task

    @pytest.mark.asyncio
    async def test_a_press_with_the_minted_nonce_resolves_the_decision(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", nonce))

        assert await task is True
        assert "Approved" in client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_forged_nonce_cannot_resolve_the_decision(self) -> None:
        """The guard runs INSIDE resolve, as a precondition.

        Checking it around the resolve call would approve the tool first and only
        then discover the press was stale — by which point the only thing left to
        suppress is the confirmation message.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", "FORGED"))

        assert webex_dispatch._APPROVALS.has_pending(key)
        assert "no longer current" in client.sent[-1][1]
        assert webex_dispatch._APPROVALS.resolve(key, False) is True
        assert await task is False

    @pytest.mark.asyncio
    async def test_a_press_carrying_no_nonce_fails_closed(self) -> None:
        # Every real press echoes the nonce we minted, so its absence is either a
        # forgery or a card from a build that predates it. An empty nonce means
        # "typed answer" to the registry and would skip the guard entirely.
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", ""))

        assert webex_dispatch._APPROVALS.has_pending(key)
        webex_dispatch._APPROVALS.resolve(key, False)
        assert await task is False

    @pytest.mark.asyncio
    async def test_a_second_press_reports_neutrally_rather_than_denied(self) -> None:
        """Webex cannot retire a card that carries an attachment.

        So the buttons stay clickable after the decision resolved. Telling the user
        their APPROVED tool was denied is worse than telling them nothing changed.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        await d.handle_message(_approval_press("approve", nonce))
        assert await task is True
        await d.handle_message(_approval_press("approve", nonce))

        assert "already answered or timed out" in client.sent[-1][1]
        assert "denied" not in client.sent[-1][1].lower()

    @pytest.mark.asyncio
    async def test_every_press_outcome_is_audited(self) -> None:
        # A forged or replayed press leaves no other trace: the reply it draws is
        # indistinguishable from the one a genuinely expired card draws.
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_approval_press("deny", "FORGED"))
            await d.handle_message(_approval_press("deny", nonce))

        assert await task is False
        operations = [
            c.kwargs["operation"] for c in fake_sel.return_value.log_api_access.mock_calls
        ]
        assert operations.count("webex.tool_approval") == 2

    @pytest.mark.asyncio
    async def test_a_refused_press_never_audits_as_an_approval(self) -> None:
        """The audit LABEL follows the resolve, not the button that was pressed.

        A press the registry refused — stale nonce, wrong request id — decided
        nothing, so recording the decision it ASKED for would put an approval in
        the SEL log that no tool ever received. That record is the only trace a
        forged or replayed press leaves, and the reply it draws is the same one a
        genuinely expired card draws, so nothing else would contradict it.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        with mock.patch("kiro_crew.webex.transport_dispatch.sel") as fake_sel:
            await d.handle_message(_approval_press("approve", "FORGED"))
            # Still open: the refused press must not have spent the decision.
            assert webex_dispatch._APPROVALS.has_pending(key)
            await d.handle_message(_approval_press("approve", nonce))

        assert await task is True
        outcomes = [
            c.kwargs["outcome"]
            for c in fake_sel.return_value.log_api_access.mock_calls
            if c.kwargs.get("operation") == "webex.tool_approval"
        ]
        assert outcomes == ["denied", "approved"]

    @pytest.mark.asyncio
    async def test_governance_blocks_an_approve_but_still_resolves_a_deny(self) -> None:
        """A policy that forbids this channel has no interest in keeping a tool
        request alive for its whole window."""
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        task = await self._pending(d, key)
        nonce = webex_dispatch._APPROVALS.reserve(key, "1")

        async def _denied(_channel: str) -> bool:
            return False

        with mock.patch("kiro_crew.webex.transport_dispatch.inbound_permitted", _denied):
            await d.handle_message(_approval_press("approve", nonce))
            assert webex_dispatch._APPROVALS.has_pending(key)
            await d.handle_message(_approval_press("deny", nonce))

        assert await task is False


# ------------------------------------------------------------------
# Tests: group spaces
# ------------------------------------------------------------------


def _space(text: str = "hello", email: str = _EMAIL, room_id: str = "SPACE1") -> WebexInbound:
    return WebexInbound(
        person_email=email,
        room_id=room_id,
        text=text,
        room_type="group",
        mentioned_people=("BOTID",),
    )


def _cfg_group():
    cfg = _cfg()
    cfg.webex.allow_group_rooms = True
    cfg.webex.allowed_room_ids = ["SPACE1"]
    return cfg


class TestGroupSpaceRouting:
    @pytest.mark.asyncio
    async def test_a_space_turn_does_not_run_in_the_senders_dm(self) -> None:
        """The single most consequential routing property on this channel.

        A space keyed by the sender answers their PRIVATE history into a room, is
        the target a mid-turn DM steers into, and is what their `/new` resets.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=_cfg_group())
        keys: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            keys.append(turn.session_key)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        space_key, dm_key = keys
        assert space_key != dm_key
        assert "SPACE1" in space_key
        assert _EMAIL not in space_key

    @pytest.mark.asyncio
    async def test_a_space_is_namespaced_as_a_forum_so_unified_cannot_collapse_it(self) -> None:
        """``dm_scope=unified`` collapses DIRECT DMs into one cross-surface bucket.

        A shared space merging into one person's private bucket would answer their
        DM history into the room, so the space is namespaced `forum`, which that
        collapse skips.
        """
        cfg = _cfg_group()
        cfg.messaging.dm_scope = "unified"
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=cfg)
        keys: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            keys.append(turn.session_key)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        space_key, dm_key = keys
        assert dm_key.startswith("unified:")
        assert not space_key.startswith("unified:")
        assert ":forum:" in space_key

    @pytest.mark.asyncio
    async def test_new_in_a_space_does_not_reset_the_senders_dm(self) -> None:
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client, cfg=_cfg_group())
        dm_before = d._session_key(_EMAIL)

        with mock.patch.object(type(d), "_bot_name", lambda self: "Kiro"):
            await d.handle_message(_space("Kiro /new"))

        assert "fresh conversation" in client.sent[-1][1]
        assert d._session_key(_EMAIL) == dm_before

    @pytest.mark.asyncio
    async def test_a_mention_is_stripped_before_the_command_and_approval_paths(self) -> None:
        """In a space Webex delivers only @mentions and does NOT strip the name.

        So every path that READS the text has to run after the strip: "@Kiro 1" is
        an approval answer and "@Kiro /new" is a command, and matching either
        against the raw text fails in exactly the room where the mention is
        mandatory.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg_group())
        with mock.patch.object(type(d), "_bot_name", lambda self: "Kiro"):
            key = d._session_key(webex_dispatch._route_of(_space()))
            task = asyncio.create_task(
                webex_dispatch._APPROVALS.decide(key, SimpleNamespace(request_id=1))
            )
            await _spin(lambda: webex_dispatch._APPROVALS.has_pending(key))
            await d.handle_message(_space("Kiro 1"))

        assert await task is True
        assert provider.steered == []

    @pytest.mark.asyncio
    async def test_uploads_are_off_in_a_space(self) -> None:
        """A file posted into a space is readable by every member of it, including
        people the email allow-list excludes."""
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=_cfg_group())
        seen: list = []

        real = webex_dispatch.WebexRenderer

        def _spy(*a, **kw):
            seen.append(kw.get("uploads_allowed"))
            return real(*a, **kw)

        async def _noop(turn, *, sessions, ctx_builder):
            pass

        with mock.patch("kiro_crew.webex.transport_dispatch.WebexRenderer", _spy):
            with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _noop):
                await d.handle_message(_space("hi"))
                await d.handle_message(_inbound("hi"))

        assert seen == [False, True]

    @pytest.mark.asyncio
    async def test_a_space_turn_is_built_without_the_operators_private_context(self) -> None:
        """A space is a shared audience the allow-list does not cover.

        The operator's memory, lessons, skills and history are injected into the
        PROMPT before any tool runs, so a full-context turn could quote the
        operator's private notes into a room they do not control. A space turn is
        built with ``minimal_context``; a direct DM keeps the full context.
        """
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient(), cfg=_cfg_group())
        seen: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            seen.append(turn.minimal_context)

        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_space("hi"))
            await d.handle_message(_inbound("hi"))

        assert seen == [True, False]


# ------------------------------------------------------------------
# Tests: /sessions
# ------------------------------------------------------------------


class FakeListingLog(FakeConvLog):
    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self._rows = rows

    def list_sessions(self) -> list[dict]:
        return list(self._rows)


class TestSessionsCommand:
    @staticmethod
    def _stem(d: WebexDispatcher, route: str, gen: int = 0) -> str:
        from kiro_crew.history import transcript_stem

        key = d._session_key(route)
        return transcript_stem(f"{key}:gen{gen}" if gen else key)

    @pytest.mark.asyncio
    async def test_it_lists_this_conversations_generations(self) -> None:
        """Regression: ``list_sessions`` reports a FILENAME STEM.

        Every character outside ``[\\w\\-.]`` is folded to ``_``, so a raw prefix
        test against a colon-bearing session key matches nothing at all — and the
        command silently reports "no conversations" forever.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        rows = [
            {"key": self._stem(d, _EMAIL, 2), "title": "newer"},
            {"key": self._stem(d, _EMAIL), "title": "first"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_inbound("/sessions"))

        body = d.client.sent[-1][1]
        assert "newer" in body and "first" in body

    @pytest.mark.asyncio
    async def test_another_users_conversations_are_not_listed(self) -> None:
        """A title is the opening words of a message.

        Scoping to the caller's own bucket is what makes the audience of the list
        exactly the audience of the conversations in it.
        """
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        rows = [
            {"key": self._stem(d, _EMAIL), "title": "mine"},
            {"key": self._stem(d, "other@example.com"), "title": "theirs"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_inbound("/sessions"))

        body = d.client.sent[-1][1]
        assert "mine" in body and "theirs" not in body

    @pytest.mark.asyncio
    async def test_a_space_lists_the_spaces_own_conversations(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient(), cfg=_cfg_group())
        route = webex_dispatch._route_of(_space())
        rows = [
            {"key": self._stem(d, route), "title": "in the space"},
            {"key": self._stem(d, _EMAIL), "title": "my private dm"},
        ]
        d.conv_log = FakeListingLog(rows)

        await d.handle_message(_space("/sessions"))

        body = d.client.sent[-1][1]
        assert "in the space" in body and "my private dm" not in body

    @pytest.mark.asyncio
    async def test_the_current_conversation_is_marked(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = FakeListingLog([{"key": self._stem(d, _EMAIL), "title": "here"}])

        await d.handle_message(_inbound("/sessions"))

        assert "current" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_empty_list_says_so(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = FakeListingLog([])
        await d.handle_message(_inbound("/sessions"))
        assert "No earlier conversations" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_no_history_store_degrades_with_a_notice(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/sessions"))
        assert "not available" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_listing_failure_is_reported_rather_than_raised(self) -> None:
        class Boom(FakeConvLog):
            def list_sessions(self):
                raise OSError("disk")

        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        d.conv_log = Boom()
        await d.handle_message(_inbound("/sessions"))
        assert "Couldn't read" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_long_list_is_capped_with_a_remainder_line(self) -> None:
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), FakeClient())
        total = webex_dispatch._SESSIONS_LIST_MAX + 3
        d.conv_log = FakeListingLog(
            [{"key": self._stem(d, _EMAIL, i), "title": f"t{i}"} for i in range(1, total + 1)]
        )

        await d.handle_message(_inbound("/sessions"))

        assert "and 3 older" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# Tests: /model
# ------------------------------------------------------------------


class FakeAcpClient:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.set_calls: list[str] = []
        self._raises = raises

    async def set_model(self, model_id: str) -> None:
        if self._raises is not None:
            raise self._raises
        self.set_calls.append(model_id)


class OlderAcpClient:
    """A backend whose client predates ``session/set_model``.

    A separate class rather than a flag, because the code probes for the METHOD:
    a stub that exists and refuses would not exercise the branch.
    """

    set_calls: list[str] = []


class ModelProvider(FakeProvider):
    def __init__(
        self,
        models: list[dict] | None = None,
        *,
        has_set_model: bool = True,
        raises: Exception | None = None,
    ) -> None:
        super().__init__([AcpEvent(kind=EVENT_COMPLETE)])
        self._models = models if models is not None else [{"modelId": "m-1", "name": "Model One"}]
        self.client: object = FakeAcpClient(raises=raises) if has_set_model else OlderAcpClient()

    def available_models(self) -> list[dict]:
        return list(self._models)


class TestModelCommand:
    @pytest.mark.asyncio
    async def test_the_list_comes_from_what_the_backend_advertised(self) -> None:
        """Never a static catalogue.

        Accounts differ in entitlement, so a hardcoded list offers models the
        account cannot reach — which surfaces as a refusal mid-conversation.
        """
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        body = d.client.sent[-1][1]
        assert "Auto" in body and "Model One" in body

    @pytest.mark.asyncio
    async def test_auto_is_offered_once(self) -> None:
        provider = ModelProvider([{"modelId": "auto", "name": "Auto"}, {"modelId": "m-1"}])
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert d.client.sent[-1][1].count("Auto") == 2  # the header line + one row

    @pytest.mark.asyncio
    async def test_no_session_yet_says_to_send_a_message_first(self) -> None:
        d = _dispatcher(FakeSessions(None, has_session=False), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert "No model list yet" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_an_out_of_range_pick_is_refused(self) -> None:
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 99"))
        assert "Pick a number between 1 and 2" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_non_numeric_pick_is_refused(self) -> None:
        d = _dispatcher(FakeSessions(ModelProvider()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model sonnet"))
        assert "Pick a number" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_pick_switches_the_live_conversation(self) -> None:
        """``session/set_model`` carries the conversation across.

        Recording the preference and telling the user to `/new` would make the
        command useless for the case people actually use it in: mid-conversation.
        """
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert provider.client.set_calls == ["m-1"]
        assert "Now using" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_the_switch_takes_the_session_semaphore(self) -> None:
        # The switch and a turn share one stdio channel, so interleaving JSON-RPC
        # on it would corrupt both.
        provider = ModelProvider()
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        key = d._session_key(_EMAIL)
        await d.handle_message(_inbound("/model 2"))
        assert sessions.acquired == [key] and sessions.released == [key]

    @pytest.mark.asyncio
    async def test_a_busy_session_defers_rather_than_claiming_a_switch(self) -> None:
        provider = ModelProvider()
        sessions = FakeSessions(provider, acquire=False)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert provider.client.set_calls == []
        assert "a reply is still running" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_backend_without_set_model_defers(self) -> None:
        provider = ModelProvider(has_set_model=False)
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        assert "applies to your next one" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_a_failed_switch_is_reported_honestly(self) -> None:
        provider = ModelProvider(raises=RuntimeError("nope"))
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 2"))
        body = d.client.sent[-1][1]
        assert "Couldn't switch" in body and "RuntimeError" in body

    @pytest.mark.asyncio
    async def test_picking_auto_records_without_claiming_a_live_switch(self) -> None:
        """ "Auto" has no ACP id meaning "let the backend choose"."""
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model 1"))
        assert provider.client.set_calls == []
        assert "applies to your next one" in d.client.sent[-1][1]

    @pytest.mark.asyncio
    async def test_the_preference_reaches_the_next_turn(self) -> None:
        provider = ModelProvider()
        d = _dispatcher(FakeSessions(provider), FakeCtx(), FakeClient())
        models: list = []

        async def _capture(turn, *, sessions, ctx_builder):
            models.append(turn.model)

        await d.handle_message(_inbound("/model 2"))
        with mock.patch("kiro_crew.webex.transport_dispatch.drive_turn", _capture):
            await d.handle_message(_inbound("hello"))

        assert models == ["m-1"]

    @pytest.mark.asyncio
    async def test_a_backend_that_advertises_nothing_still_offers_auto(self) -> None:
        class Broken(ModelProvider):
            def available_models(self):
                raise RuntimeError("no")

        d = _dispatcher(FakeSessions(Broken()), FakeCtx(), FakeClient())
        await d.handle_message(_inbound("/model"))
        assert "Auto" in d.client.sent[-1][1]


# ------------------------------------------------------------------
# Tests: mid-turn attachments
# ------------------------------------------------------------------


def _with_files(text: str = "", urls: tuple[str, ...] = ("https://webexapis.com/v1/c/A",)):
    return WebexInbound(
        person_email=_EMAIL,
        room_id="ROOM",
        text=text,
        room_type="direct",
        file_urls=urls,
    )


class TestMidTurnAttachments:
    @pytest.mark.asyncio
    async def test_a_message_with_files_is_queued_even_in_steer_mode(self) -> None:
        """Steer forwards TEXT ONLY.

        Steering a message that carries files acknowledges a fold while silently
        dropping every attachment, so the files reach the agent one turn later
        rather than never.
        """
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        await d.handle_message(_with_files("look at this"))

        assert provider.steered == []
        assert sessions.queued[0][1] == "look at this"
        assert sessions.queued[0][2]["webex_file_urls"] == ["https://webexapis.com/v1/c/A"]

    @pytest.mark.asyncio
    async def test_an_uncaptioned_attachment_still_gets_a_receipt(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_with_files(""))

        assert any(webex_dispatch._QUEUED_ATTACHMENT_LABEL in m for (_, m) in client.sent)

    @pytest.mark.asyncio
    async def test_the_drain_replays_the_queued_files_and_not_the_finished_turns(self) -> None:
        """*inbound* is the message that OPENED the finished turn.

        Inheriting its attachments would re-download and re-summarize files the
        agent has already been shown, once per drain iteration.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [("1", "and this", {"webex_file_urls": ["QUEUED-URL"]})]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append((inbound.text, inbound.file_urls))

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(_with_files("first", ("OPENING-URL",)), "KEY")

        assert seen == [("and this", ("QUEUED-URL",))]

    @pytest.mark.asyncio
    async def test_a_deferred_entry_keeps_its_attachments(self) -> None:
        """A burst past the collapse cap re-enqueues the surplus IN ORDER.

        Re-enqueueing without the kwargs would silently lose the files of
        everything after the cap — and the deferred entry drains in THIS pump, so
        the loss would land one iteration later in the same call.
        """
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        sessions.queued = [
            (str(i), f"m{i}", {"webex_file_urls": [f"U{i}"]}) for i in range(_MAX_COLLAPSE + 1)
        ]
        seen: list = []

        async def _replay(self, inbound, *, interpret_commands=True, drain=True):
            seen.append(inbound.file_urls)

        with mock.patch.object(type(d), "handle_message", _replay):
            await d._drain_queue(_inbound("first"), "KEY")

        # Two iterations: the capped burst, then the one deferred entry — which
        # still carries the file it was queued with.
        assert len(seen[0]) == _MAX_COLLAPSE
        assert seen[1] == (f"U{_MAX_COLLAPSE}",)


class TestGovernanceOnPresses:
    @pytest.mark.asyncio
    async def test_a_policy_denied_channel_drops_a_choice_whole(self) -> None:
        """A choice is turn CONTENT.

        `drive_turn` would drop the turn anyway, so echoing the press would claim
        the channel is answering when nothing will.
        """
        client = FakeClient()
        d = _dispatcher(FakeSessions(FakeProvider([])), FakeCtx(), client)
        key = d._session_key(_EMAIL)
        d._choices.publish(key, "N1", ["Keep going"])

        async def _denied(_channel: str) -> bool:
            return False

        with mock.patch("kiro_crew.webex.transport_dispatch.inbound_permitted", _denied):
            await d.handle_message(_options_press("0", "N1"))

        assert client.sent == []
