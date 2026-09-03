"""Tests for kiro_crew.wecom.transport_dispatch (WeComDispatcher) + commands."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.wecom.client import WeComInbound
from kiro_crew.wecom.commands import ConversationState, parse_command
from kiro_crew.wecom.transport_dispatch import WeComDispatcher

# ------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------


class FakeProvider:
    supports_steer = True

    def __init__(self, events: list) -> None:
        self._events = events
        self.approved: list = []
        self.rejected: list = []
        self.compacted = False
        self.steered: list = []
        self.active_turn = True

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def stream(self, message: str):
        for ev in self._events:
            yield ev

    async def approve_tool(self, rid) -> None:
        self.approved.append(rid)

    async def reject_tool(self, rid) -> None:
        self.rejected.append(rid)

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
        # Dashboard-mirror surface. WeCom binds its origin mirror on every turn
        # now that aibot_send_msg gives a mirrored reply somewhere to land, so the
        # fake owes the same API the real SessionMap exposes.
        self.mirror_links: dict = {}
        self.opted_out: dict = {}
        self.cleared_mirror: list = []
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0

    @contextlib.contextmanager
    def batched_save(self):
        """One whole-map write per user action, as the real one does."""
        yield

    def mirror_opt_out(self, key) -> bool:
        """Predicate, matching the real SessionMap -- not a plain mapping."""
        return bool(self.opted_out.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opted_out[key] = value

    def set_mirror_link(self, key, location, *, reason="") -> None:
        self.mirror_links[key] = location

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def clear_mirror_link(self, key, *, reason="") -> bool:
        """Returns whether a binding was actually cleared, like the real one --
        ``release_conversation_location`` counts the return value."""
        self.cleared_mirror.append(key)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(self, location, *, reason="") -> list:
        """Sweep every binding pointing at *location* (none, in these tests)."""
        return []

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

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


class _GateResult:
    def __init__(self, action: str = "") -> None:
        self.action = action


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, title, *, session_key, agent, tool_kind, raw_params=None):
        return _GateResult("")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = FakeHooks()

    # Names only the kwargs it asserts on and takes ``**kw`` for the rest: the
    # shared pipeline owns the call shape, so a fake that pins every kwarg
    # breaks on any field the pipeline later forwards, without that field
    # having anything to do with this channel. Mirrors the pipeline's own fake.
    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source, **kw):
        assert runtime_source == "wecom"
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.replies: list[tuple[str, str]] = []
        self.said: list[str] = []
        #: (chat_id, content) of every confirmed push. A threshold notice goes out
        #: this way rather than as a stream frame, so it cannot retarget the ACK
        #: attribution of the answer's sealing frame (which shares the inbound
        #: req_id).
        self.pushed: list[tuple[str, str]] = []

    async def send_stream(self, req_id, sid, content, *, finish, await_ack=False) -> bool:
        self.frames.append({"sid": sid, "content": content, "finish": finish})
        return True

    async def say(self, inbound, content) -> bool:
        """Out-of-band ack. Recorded as a sealed one-frame bubble, like the real
        client, so a test can tell an ack apart from the answer's own frames."""
        self.said.append(content)
        self.frames.append({"sid": "say", "content": content, "finish": True})
        return True

    async def send_proactive(self, chat_id, content) -> bool:
        self.pushed.append((chat_id, content))
        return True

    def stream_is_dead(self, stream_id) -> bool:
        # The renderer checks bubble liveness before every frame; bubbles stay
        # live here, so dispatch tests exercise the ordinary path.
        return False

    def stream_had_rejection(self, stream_id) -> bool:
        return False

    async def send_reply(self, url, content) -> None:
        self.replies.append((url, content))


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
        wecom=SimpleNamespace(hard_threshold_pct=95.0, soft_threshold_pct=80.0),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(sessions, ctx, client, *, conv_log=None, agent=None, cfg=None):
    d = WeComDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=cfg or _cfg(),
        owner_id="Wei",
        agent=agent,
        conv_log=conv_log,
        approval_mode="interactive",
    )
    d.client = client
    return d


def _inbound(text: str = "hello", userid: str = "Wei") -> WeComInbound:
    return WeComInbound(userid=userid, text=text, response_url="https://r", req_id="rq1", chatid="")


# ------------------------------------------------------------------
# Tests: full turn
# ------------------------------------------------------------------


def _deny_wecom_profile(monkeypatch, tmp_path):
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
        # driving a turn. Regression-locks the WeCom inbound chokepoint.
        from kiro_crew.platform import governance_profiles as gp

        _deny_wecom_profile(monkeypatch, tmp_path)
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

        key = d._session_key("Wei")
        # Final answer streamed with finish=True.
        assert any(f["finish"] and f["content"] == "hi there" for f in client.frames)
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

        # Placeholder finalized (no perma-"🤔 …") even though get_or_create failed.
        assert any(f["finish"] for f in client.frames)
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

        # Notice surfaced as a separate message, delivered as a confirmed PUSH so
        # it carries its own req_id: sent through `say` it would replace the
        # answer's tracked stream on the shared inbound req_id, and a refusal ACK
        # for the answer's sealing frame would then be recorded against the notice.
        assert any("对话上下文已较长" in c for _chat, c in client.pushed)
        assert not any(
            "对话上下文已较长" in f["content"] for f in client.frames
        ), "the notice went out on the answer's req_id"
        # ...but NOT persisted into the assistant turn (only the real answer is).
        assistant_texts = [t for (_, role, t) in conv.appended if role == "assistant"]
        assert assistant_texts == ["answer"]


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

        assert client.said == ["✅ 已开始新对话"]
        assert d._conv.current_gen("Wei") == 1  # generation bumped
        assert sessions.successes == []  # no LLM turn

    @pytest.mark.asyncio
    async def test_compact_command(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        key = d._session_key("Wei")
        assert provider.compacted is True
        assert sessions.acquired == [key]
        assert sessions.released == [key]
        assert client.said == ["🗜️ 已压缩上下文。"]

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

        key = d._session_key("Wei")
        assert provider.compacted is False
        assert sessions.released == [key]  # the acquired semaphore is handed back
        assert any("自动压缩上下文" in s for s in client.said)

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
        assert not any("已自动压缩" in c for _chat, c in client.pushed)

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

        assert not any("对话上下文已较长" in c for _chat, c in client.pushed)

    @pytest.mark.asyncio
    async def test_compact_refused_while_turn_busy(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider, acquire=False, has_session=True)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert provider.compacted is False
        assert sessions.released == []
        assert client.said == ["⏳ 正在处理上一条消息，请稍后再试 /compact。"]

    @pytest.mark.asyncio
    async def test_compact_without_active_session(self) -> None:
        sessions = FakeSessions(None, acquire=False, has_session=False)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert sessions.released == []
        assert client.said == ["ℹ️ 当前没有可压缩的对话。"]

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_link_binds_the_dashboard_mirror(self) -> None:
        # /link used to be refused outright, on the belief that WeCom could never
        # push. aibot_send_msg makes the mirror deliverable, so the command now
        # does what it says.
        client = FakeClient()
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), client)
        await d.handle_message(_inbound("/link"))

        key = "wecom:kirocrew:direct:Wei"
        assert sessions.mirror_links[key].channel_type == "wecom"
        assert sessions.mirror_links[key].channel_id == "Wei"
        assert sessions.opted_out[key] is False
        assert any("已连接" in said for said in client.said)

    @pytest.mark.asyncio
    async def test_unlink_opts_out_so_the_next_turn_does_not_rebind(self) -> None:
        # The opt-out is the load-bearing half: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the next message.
        client = FakeClient()
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), client)
        await d.handle_message(_inbound("/unlink"))

        key = "wecom:kirocrew:direct:Wei"
        assert sessions.opted_out[key] is True
        assert sessions.successes == []  # a command runs no LLM turn

        # /link withdraws the opt-out, so the next turn rebinds again.
        await d.handle_message(_inbound("/link"))
        assert sessions.opted_out[key] is False
        assert sessions.mirror_links[key].channel_id == "Wei"

    @pytest.mark.asyncio
    async def test_an_attachment_sent_mid_turn_is_refused_not_dropped(self) -> None:
        # _session/steer carries TEXT ONLY, so folding this into the running turn
        # would send the caption and lose the picture with no sign of it.
        sessions = FakeSessions(FakeProvider([]), acquire=False)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)
        inbound = _inbound("look at this")
        inbound.attachments = [("att", "key")]

        await d.handle_message(inbound)

        assert inbound.attachments == [], "the refused attachment must be cleared"
        assert any("附件" in said for said in client.said), "the user must be told"

    def test_parse_command(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("新对话") == "new"
        assert parse_command("清空") == "new"
        assert parse_command("/compact") == "compact"
        assert parse_command("/link") == "link"
        assert parse_command("/unlink") == "unlink"
        assert parse_command("hello") is None

    def test_parse_command_after_a_group_mention(self) -> None:
        """A group chat requires @-mentioning the bot, so the command arrives prefixed."""
        assert parse_command("@Kiro /new") == "new"
        assert parse_command("@Kiro /compact") == "compact"
        assert parse_command("@Kiro 新对话") == "new"
        assert parse_command("@Kiro 清空") == "new"
        assert parse_command("@Kiro /link") == "link"
        assert parse_command("@Kiro /unlink") == "unlink"

    def test_mention_does_not_loosen_command_matching(self) -> None:
        """Only ONE leading mention token in front of an EXACT command counts.

        Everything here must keep reaching the model as ordinary text. The bot's
        display name is not known to the parser, so the mention is recognized
        structurally -- which is exactly why the rest of the match has to stay
        exact rather than becoming a prefix or substring test.
        """
        for text in (
            "@Kiro hello",  # mentioned prose
            "@Kiro",  # bare mention, no remainder
            "@Kiro please /new",  # command embedded in prose
            "@Kiro /new extra",  # trailing argument
            "@someone ordinary text",  # a mention of somebody else
            "@a @b /new",  # only one mention token is consumed
            "hello /new",  # command not at the start
            "/new @Kiro",  # trailing mention
            "/new@Kiro",  # Telegram's suffix syntax, not WeCom's
            "@Kiro /bogus",  # unknown command stays unknown
            "@Kiro/new",  # no separator -- not a mention token
        ):
            assert parse_command(text) is None, text

    @pytest.mark.asyncio
    async def test_group_mention_new_bumps_gen_and_acks(self) -> None:
        """The reported shape: '@Kiro /new' must reset, not steer into the turn."""
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("@Kiro /new"))

        assert client.said == ["✅ 已开始新对话"]
        assert d._conv.current_gen("Wei") == 1
        assert sessions.successes == []  # no LLM turn

    @pytest.mark.asyncio
    async def test_group_mention_compact_reaches_compaction(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("@Kiro /compact"))

        assert provider.compacted is True
        assert client.said == ["🗜️ 已压缩上下文。"]

    @pytest.mark.asyncio
    async def test_mentioned_prose_still_runs_a_turn_with_text_intact(self) -> None:
        """Ordinary inbound text is never rewritten -- the model sees the mention."""
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="sure"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider)
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), conv_log=conv)

        await d.handle_message(_inbound("@Kiro explain this stack trace"))

        key = d._session_key("Wei")
        assert sessions.successes == [key]
        assert (key, "user", "@Kiro explain this stack trace") in conv.appended
        assert d._conv.current_gen("Wei") == 0  # not treated as /new

    @pytest.mark.asyncio
    async def test_mentioned_unknown_command_still_runs_a_turn(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        await d.handle_message(_inbound("@Kiro /bogus"))

        assert sessions.successes == [d._session_key("Wei")]
        assert d._conv.current_gen("Wei") == 0

    def test_conversation_state(self) -> None:
        s = ConversationState()
        assert s.current_gen("u") == 0
        assert s.bump_gen("u") == 1
        s.set_awaiting("u")
        assert s.is_awaiting("u") is True
        s.clear_awaiting("u")
        assert s.is_awaiting("u") is False


class TestWeComMidTurn:
    @pytest.mark.asyncio
    async def test_busy_steers_and_acknowledges(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("and also this"))

        assert provider.steered == ["and also this"]
        assert any("合并" in content for content in client.said)
        assert sessions.successes == []  # no full turn ran while busy

    @pytest.mark.asyncio
    async def test_busy_but_turn_finished_runs_fresh(self) -> None:
        # is_busy is False by the time _handle_busy runs (turn finished in the
        # window) -> run the message as a fresh turn instead of a false ack.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)  # _busy defaults False
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key("Wei"))

        assert sessions.successes  # a real turn ran
        assert provider.steered == []  # not steered

    @pytest.mark.asyncio
    async def test_busy_steer_unavailable_asks_resend(self) -> None:
        # A turn is genuinely in flight but steer isn't possible (cold start):
        # ask the user to resend rather than looping or dropping the message.
        provider = FakeProvider([])
        provider.supports_steer = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key("Wei"))

        assert any("重发" in content for content in client.said)
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_busy_no_active_turn_does_not_steer(self) -> None:
        # Semaphore held (post-turn bookkeeping) but no turn is live: steer must
        # not be attempted and must not falsely acknowledge a merge -- fall
        # through to the resend prompt instead.
        provider = FakeProvider([])
        provider.active_turn = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d._handle_busy(_inbound("later"), d._session_key("Wei"))

        assert provider.steered == []
        assert not any("合并" in content for content in client.said)
        assert any("重发" in content for content in client.said)
        assert sessions.successes == []
