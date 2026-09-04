"""Tests for kiro_crew.feishu.transport_dispatch (FeishuDispatcher)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.feishu.client import LarkInbound
from kiro_crew.feishu.transport_dispatch import FeishuDispatcher
from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    build_dm_session_key,
)
from kiro_crew.session_allocation import SessionClosingError

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
        self._max_gen: dict[str, int] = {}
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
        return self._max_gen.get(bucket, -1)


class _GateResult:
    def __init__(self, action: str = "") -> None:
        self.action = action


class FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(
        self,
        title,
        *,
        session_key,
        agent,
        tool_kind,
        raw_params=None,
        command=None,
        is_shell=False,
    ):
        return _GateResult("")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = FakeHooks()
        #: ``minimal_context`` as the dispatcher actually passed it, per call.
        self.minimal: list[bool] = []

    def build_message(
        self,
        text,
        is_new,
        key,
        *,
        channel_id,
        agent,
        resumed,
        runtime_source,
        **kw,
    ):
        assert runtime_source == "feishu"
        self.minimal.append(bool(kw.get("minimal_context", False)))
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []

    async def send_reply(self, message_id: str, content: str) -> bool:
        # Mirrors the real LarkClient contract: True on delivery.
        self.replies.append((message_id, content))
        return True


class FakeConvLog:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        # Recorded separately so the existing 3-tuple assertions stay readable.
        # The real ConversationLog.append takes agent= and writes it into the
        # record, so a fake that did not accept it would hide a dropped agent.
        self.agents: list[str | None] = []
        self.titles: dict[str, str] = {}

    def append(self, key, role, text, *, agent=None, mid=None) -> None:
        self.appended.append((key, role, text))
        self.agents.append(agent)

    def set_title(self, key, title) -> None:
        self.titles[key] = title


def _cfg(default_agent: str = "", approval_mode: str = "interactive", **kw):
    dm_scope = kw.get("dm_scope", "per-channel-peer")
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent=default_agent, approval_mode=approval_mode),
        feishu=SimpleNamespace(hard_threshold_pct=95.0, soft_threshold_pct=80.0),
        messaging=SimpleNamespace(
            dm_scope=dm_scope,
            idle_reset_minutes=kw.get("idle_reset_minutes", 0),
            daily_reset_hour=kw.get("daily_reset_hour", -1),
            queue_mode="steer",
        ),
    )


def _dispatcher(sessions, ctx, client, *, conv_log=None, agent=None, cfg=None):
    d = FeishuDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=cfg or _cfg(),
        agent=agent,
        conv_log=conv_log,
        approval_mode="interactive",
    )
    d.client = client
    return d


def _inbound(
    text: str = "hello",
    open_id: str = "ou_abc123",
    message_id: str = "msg1",
    chat_type: str = "p2p",
    chat_id: str = "",
    command_text: str = "",
) -> LarkInbound:
    return LarkInbound(
        open_id=open_id,
        text=text,
        message_id=message_id,
        chat_type=chat_type,
        chat_id=chat_id,
        command_text=command_text,
    )


# ------------------------------------------------------------------
# Tests: full turn
# ------------------------------------------------------------------


class TestTurn:
    @pytest.mark.asyncio
    async def test_text_turn_bookkeeping(self) -> None:
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="hi there"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider)
        client = FakeClient()
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), client, conv_log=conv)

        await d.handle_message(_inbound("hello"))

        key = d._session_key(d._route(_inbound("hello")))
        # Final answer sent as a reply.
        assert any(content == "hi there" for _, content in client.replies)
        # Bookkeeping: success recorded, semaphore released, turn persisted.
        assert sessions.successes == [key]
        assert sessions.released == [key]
        assert (key, "user", "hello") in conv.appended
        assert (key, "assistant", "hi there") in conv.appended

    @pytest.mark.asyncio
    async def test_persistence_carries_the_resolved_agent(self) -> None:
        """A custom-agent turn must persist WITH that agent.

        Without it the transcript has no agent metadata and the dashboard
        attributes the conversation to the default agent instead.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider)
        conv = FakeConvLog()
        d = _dispatcher(
            sessions,
            FakeCtx(),
            FakeClient(),
            conv_log=conv,
            cfg=_cfg(default_agent="kirocrew"),
            agent="my-custom-agent",
        )

        await d.handle_message(_inbound("hello"))

        # The turn ran on the custom agent...
        assert sessions.last_agent == "my-custom-agent"
        # ...and every persisted message carries it, not the default.
        assert conv.appended, "expected the turn to be persisted"
        assert set(conv.agents) == {"my-custom-agent"}

    @pytest.mark.asyncio
    async def test_agent_resolves_to_kirocrew_when_unset(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=_cfg(default_agent=""))
        await d.handle_message(_inbound("hi"))
        assert sessions.last_agent == "kirocrew"

    @pytest.mark.asyncio
    async def test_cold_start_failure_finalizes_renderer(self) -> None:
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider, raise_on_get=RuntimeError("boom"))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        # Must not raise -- the dispatcher swallows and finalizes.
        await d.handle_message(_inbound("hi"))

        # Renderer finalized (error fallback text sent).
        assert len(client.replies) >= 1
        # Never held the semaphore -> never release it.
        assert sessions.released == []

    @pytest.mark.asyncio
    async def test_soft_threshold_notice_post_turn(self) -> None:
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), client, conv_log=conv)

        await d.handle_message(_inbound("hello"))

        # Notice surfaced as a separate reply.
        assert any("对话上下文已较长" in content for _, content in client.replies)
        # The real answer is also persisted.
        assistant_texts = [t for (_, role, t) in conv.appended if role == "assistant"]
        assert assistant_texts == ["answer"]

    @pytest.mark.asyncio
    async def test_soft_threshold_notice_fires_once_not_every_turn(self) -> None:
        """The soft notice is latched: a second turn in the band stays quiet."""
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("one", message_id="m1"))
        await d.handle_message(_inbound("two", message_id="m2"))

        notices = [c for _, c in client.replies if "对话上下文已较长" in c]
        assert len(notices) == 1

    @pytest.mark.asyncio
    async def test_compaction_clears_the_latch_so_it_can_warn_again(self) -> None:
        """Crossing the hard threshold re-arms the soft notice."""
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("one", message_id="m1"))
        sessions._ctx_pct = 96.0
        await d.handle_message(_inbound("two", message_id="m2"))
        sessions._ctx_pct = 85.0
        await d.handle_message(_inbound("three", message_id="m3"))

        notices = [c for _, c in client.replies if "对话上下文已较长" in c]
        assert len(notices) == 2

    @pytest.mark.asyncio
    async def test_manual_compact_clears_the_latch_so_it_can_warn_again(self) -> None:
        """A user who complies with the nudge must be nudged again next cycle.

        The hard-threshold path already released the latch; the manual
        ``/compact`` path did not, so after complying once the user was never
        warned again until the hard threshold -- the notice silently degraded
        from a per-growth-cycle nudge into a once-ever one.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("one", message_id="m1"))
        await d.handle_message(_inbound("/compact", message_id="m2"))
        await d.handle_message(_inbound("three", message_id="m3"))

        notices = [c for _, c in client.replies if "对话上下文已较长" in c]
        assert len(notices) == 2

    @pytest.mark.asyncio
    async def test_idle_rotation_starts_a_new_generation(self) -> None:
        """``messaging.idle_reset_minutes`` must actually rotate the conversation.

        Without a ``maybe_rotate`` call the dispatcher never records activity, so
        ``last_active`` stays frozen and BOTH ``idle_reset_minutes`` and
        ``daily_reset_hour`` are silently inert -- a stale Feishu conversation
        would be resumed across the idle boundary however the operator
        configured it.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(idle_reset_minutes=30))

        await d.handle_message(_inbound("one", message_id="m1"))

        # Backdate the recorded activity past the idle window; the next inbound
        # must land on a NEW generation rather than resuming the stale one.
        route = d._route(_inbound("two", message_id="m2"))
        d._conv._get(route).last_active -= 31 * 60

        await d.handle_message(_inbound("two", message_id="m2"))

        assert len(sessions.successes) == 2
        assert (
            sessions.successes[0] != sessions.successes[1]
        ), f"idle rotation did not mint a new key ({sessions.successes[0]})"

    @pytest.mark.asyncio
    async def test_no_rotation_keeps_the_same_generation(self) -> None:
        """The complement: inside the idle window the conversation is RESUMED.

        Without this, a rotation bug that fired on every message would still pass
        the test above.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(idle_reset_minutes=30))

        await d.handle_message(_inbound("one", message_id="m1"))
        await d.handle_message(_inbound("two", message_id="m2"))

        assert len(sessions.successes) == 2
        assert sessions.successes[0] == sessions.successes[1]

    @pytest.mark.asyncio
    async def test_rotation_runs_after_the_busy_check(self) -> None:
        """Order matters: rotating BEFORE the busy check would mint a new key and
        miss the in-flight turn on the current one, turning a steer into a second
        concurrent turn. Pinned because the fix's correctness is its placement."""
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client, cfg=_cfg(idle_reset_minutes=30))

        await d.handle_message(_inbound("one", message_id="m1"))
        route = d._route(_inbound("two", message_id="m2"))
        gen_before = d._conv.current_gen(route)

        # Idle-eligible AND busy: the busy branch must win, leaving the
        # generation untouched so the steer reaches the running turn.
        d._conv._get(route).last_active -= 31 * 60
        sessions._busy = True
        await d.handle_message(_inbound("two", message_id="m2"))

        assert d._conv.current_gen(route) == gen_before

    @pytest.mark.asyncio
    async def test_turn_honours_an_out_of_band_approval_grant(self, monkeypatch) -> None:
        """A button-less channel needs the process-global grant or it cannot act.

        With ``decider=None`` there is no in-band approve/deny, so if the turn does
        not read the out-of-band grant the INTERACTIVE ladder denies EVERY tool and
        the agent can only talk -- even while the operator has the dashboard YOLO
        toggle active. 8/9 channels pass this, including every other button-less
        one; Feishu was the exception.

        Asserted as a live predicate rather than a captured bool, because reading it
        per request is what makes arming (or lapsing) the grant take effect on the
        next tool instead of after a gateway restart.
        """
        captured: list = []

        async def _capture(turn, **kwargs):
            captured.append(turn)

        monkeypatch.setattr("kiro_crew.feishu.transport_dispatch.drive_turn", _capture)

        sessions = FakeSessions(FakeProvider([]), ctx_pct=10.0)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        await d.handle_message(_inbound("hello", message_id="m1"))

        assert len(captured) == 1
        predicate = captured[0].auto_approve_session
        assert callable(predicate), "grant must be a predicate, not a captured value"

        import kiro_crew.feishu.transport_dispatch as mod

        monkeypatch.setattr(mod, "safety_override", lambda: SimpleNamespace(is_active=lambda: True))
        assert predicate() is True
        monkeypatch.setattr(
            mod, "safety_override", lambda: SimpleNamespace(is_active=lambda: False)
        )
        assert predicate() is False

    @pytest.mark.asyncio
    async def test_turn_carries_a_session_directive_consumer(self, monkeypatch) -> None:
        """``monitor_start`` & co. must actually apply on a Feishu session.

        These tools are stateless: they return a marker that ``TurnDriver``
        decodes and hands to the turn's ``directive_consumer``. With no consumer
        the driver leaves the marker inert *while the tool still reports success
        to the model* -- a silent no-op, which is strictly worse than a refusal
        because nothing surfaces the failure. Nine sibling dispatchers pass one;
        this pins that Feishu does too.
        """
        captured: list = []

        async def _capture(turn, **kwargs):
            captured.append(turn)

        monkeypatch.setattr("kiro_crew.feishu.transport_dispatch.drive_turn", _capture)

        sessions = FakeSessions(FakeProvider([]), ctx_pct=10.0)
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        await d.handle_message(_inbound("hello", message_id="m1"))

        assert len(captured) == 1
        consumer = captured[0].directive_consumer
        assert consumer is not None, "Feishu turn built without a directive consumer"
        assert inspect.iscoroutinefunction(consumer)

    @pytest.mark.asyncio
    async def test_latch_is_per_route_so_a_group_warns_independently(self) -> None:
        """A group's latch is its own; a DM latch must not silence it."""
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("dm", message_id="m1"))
        await d.handle_message(
            _inbound("group", message_id="m2", chat_type="group", chat_id="oc_grp")
        )

        notices = [c for _, c in client.replies if "对话上下文已较长" in c]
        assert len(notices) == 2

    @pytest.mark.asyncio
    async def test_hard_threshold_auto_compacts(self) -> None:
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=96.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        # Hard threshold triggers auto-compaction.
        assert provider.compacted is True
        assert any("自动压缩" in content for _, content in client.replies)


# ------------------------------------------------------------------
# Tests: commands (/new, /reset, /compact)
# ------------------------------------------------------------------


class TestCommands:
    @pytest.mark.asyncio
    async def test_new_bumps_gen_and_acks(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/new"))

        assert client.replies == [("msg1", "✅ 已开始新对话")]
        route = d._route(_inbound("/new"))
        assert d._conv.current_gen(route) == 1
        assert sessions.successes == []  # no LLM turn

    @pytest.mark.asyncio
    async def test_group_command_intercepts_despite_the_bot_mention(self) -> None:
        """`@Bot /new` in a group must reset, not become a prompt.

        Feishu requires the bot to be @-mentioned in a group, so the resolved
        body reads "@FeishuBot /new". Matching the whole string against "/new"
        never fires, and the command was silently dispatched to the model as a
        prompt -- the user's reset appeared to be ignored. The mention-free form
        travels on the frame precisely so this match can work.
        """
        sessions = FakeSessions(FakeProvider([]), ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(
            _inbound(
                "@FeishuBot /new",
                command_text="/new",
                chat_type="group",
                chat_id="oc_grp",
                message_id="m1",
            )
        )

        assert any("已开始新对话" in c for _, c in client.replies)
        # Intercepted means no turn ran at all.
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_group_compact_intercepts_despite_the_bot_mention(self) -> None:
        """Same for `/compact`; it shares the intercept block."""
        sessions = FakeSessions(FakeProvider([]), ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(
            _inbound(
                "@FeishuBot /compact",
                command_text="/compact",
                chat_type="group",
                chat_id="oc_grp",
                message_id="m1",
            )
        )

        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_a_second_mention_does_not_reset_the_conversation(self) -> None:
        """The end-to-end consequence of the command-body rule.

        With no command body the dispatcher falls back to the resolved text,
        which still carries "@FeishuBot", so nothing matches "/new" and the turn
        runs as an ordinary prompt. Asserted on the generation because the harm
        being prevented is a silent conversation reset, not a missing reply.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        inbound = _inbound(
            "@FeishuBot /new @Alice",
            command_text="",
            chat_type="group",
            chat_id="oc_grp",
            message_id="m1",
        )
        gen_before = d._conv.current_gen(d._route(inbound))
        await d.handle_message(inbound)

        assert d._conv.current_gen(d._route(inbound)) == gen_before
        assert len(sessions.successes) == 1
        assert not any("已开始新对话" in c for _, c in client.replies)

    @pytest.mark.asyncio
    async def test_a_mention_plus_prose_is_still_a_prompt(self) -> None:
        """The complement: only a bare command intercepts.

        Without this, "strip mentions then match" could be over-eager and start
        swallowing ordinary sentences that merely contain a command word.
        """
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider, ctx_pct=10.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(
            _inbound(
                "@FeishuBot please run /new later",
                command_text="please run /new later",
                chat_type="group",
                chat_id="oc_grp",
                message_id="m1",
            )
        )

        assert len(sessions.successes) == 1

    @pytest.mark.asyncio
    async def test_reset_is_alias_for_new(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/reset"))

        assert client.replies == [("msg1", "✅ 已开始新对话")]
        route = d._route(_inbound("/reset"))
        assert d._conv.current_gen(route) == 1
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_compact_command(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        key = d._session_key(d._route(_inbound("/compact")))
        assert provider.compacted is True
        assert sessions.acquired == [key]
        assert sessions.released == [key]
        assert client.replies == [("msg1", "🗜️ 已压缩上下文。")]

    @pytest.mark.asyncio
    async def test_compact_refused_while_turn_busy(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider, acquire=False, has_session=True)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert provider.compacted is False
        assert sessions.released == []
        assert client.replies == [("msg1", "⏳ 正在处理上一条消息，请稍后再试 /compact。")]

    @pytest.mark.asyncio
    async def test_compact_without_active_session(self) -> None:
        sessions = FakeSessions(None, acquire=False, has_session=False)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert sessions.released == []
        assert client.replies == [("msg1", "ℹ️ 当前没有可压缩的对话。")]


# ------------------------------------------------------------------
# Tests: mid-turn busy
# ------------------------------------------------------------------


class TestMidTurn:
    @pytest.mark.asyncio
    async def test_busy_steers_and_acknowledges(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("and also this"))

        assert provider.steered == ["and also this"]
        assert any("合并" in content for _, content in client.replies)
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_busy_but_turn_finished_runs_fresh(self) -> None:
        # is_busy is False by the time _handle_busy rechecks -> fresh turn.
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)  # _busy defaults False
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        route = d._route(_inbound("later"))
        key = d._session_key(route)
        await d._handle_busy(_inbound("later"), key)

        assert sessions.successes  # a real turn ran
        assert provider.steered == []

    @pytest.mark.asyncio
    async def test_busy_steer_unavailable_asks_resend(self) -> None:
        provider = FakeProvider([])
        provider.supports_steer = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        route = d._route(_inbound("later"))
        key = d._session_key(route)
        await d._handle_busy(_inbound("later"), key)

        assert any("重发" in content for _, content in client.replies)
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_busy_no_active_turn_does_not_steer(self) -> None:
        provider = FakeProvider([])
        provider.active_turn = False
        sessions = FakeSessions(provider)
        sessions._busy = True
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        route = d._route(_inbound("later"))
        key = d._session_key(route)
        await d._handle_busy(_inbound("later"), key)

        assert provider.steered == []
        assert not any("合并" in content for _, content in client.replies)
        assert any("重发" in content for _, content in client.replies)
        assert sessions.successes == []


# ------------------------------------------------------------------
# Tests: group isolation (review finding a)
# ------------------------------------------------------------------


class TestGroupIsolation:
    """A group message must never resume the sender's private DM session."""

    @pytest.mark.asyncio
    async def test_group_key_differs_from_dm_key(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        group = _inbound(
            text="hi",
            open_id="ou_abc123",
            chat_type="group",
            chat_id="oc_group1",
        )
        dm = _inbound(text="hi", open_id="ou_abc123", chat_type="p2p")

        group_route = d._route(group)
        dm_route = d._route(dm)

        group_key = d._session_key(group_route)
        dm_key = d._session_key(dm_route)

        # Keys must differ -- group never resumes DM.
        assert group_key != dm_key

        # Group route uses CHAT_TYPE_FORUM, not CHAT_TYPE_DIRECT.
        assert group_route[0] == CHAT_TYPE_FORUM
        assert dm_route[0] == CHAT_TYPE_DIRECT

        # Group key carries the chat_id, not the sender's open_id in scope.
        assert "oc_group1" in group_key
        assert "ou_abc123" not in group_key

        # Group key's chat_type segment is forum, not direct.
        assert f":{CHAT_TYPE_FORUM}:" in group_key
        assert f":{CHAT_TYPE_DIRECT}:" not in group_key

    @pytest.mark.asyncio
    async def test_group_isolation_survives_unified_scope(self) -> None:
        """Under dm_scope=unified, direct keys collapse but group keys must not."""
        sessions = FakeSessions(FakeProvider([]))
        cfg = _cfg(dm_scope="unified")
        d = _dispatcher(sessions, FakeCtx(), FakeClient(), cfg=cfg)

        group = _inbound(
            text="hi",
            open_id="ou_abc123",
            chat_type="group",
            chat_id="oc_group1",
        )
        dm = _inbound(text="hi", open_id="ou_abc123", chat_type="p2p")

        group_key = d._session_key(d._route(group))
        dm_key = d._session_key(d._route(dm))

        # Even under unified scope, group and DM remain isolated.
        assert group_key != dm_key

        # The unified DM key uses the unified bucket.
        assert dm_key.startswith("unified:")

        # The group key retains its full per-channel-peer shape.
        assert f":{CHAT_TYPE_FORUM}:" in group_key
        assert "oc_group1" in group_key


# ------------------------------------------------------------------
# Tests: restart seeding (review finding b)
# ------------------------------------------------------------------


class TestRestartSeeding:
    """/new must advance past a generation left on disk."""

    @pytest.mark.asyncio
    async def test_new_advances_past_persisted_generation(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        # Compute the bucket that _seed_gen will query, the same way production
        # does: build_dm_session_key with gen=0 for the DM route.
        dm = _inbound(text="/new", open_id="ou_abc123")
        route = d._route(dm)
        bucket = build_dm_session_key(
            "feishu",
            "kirocrew",
            route[1],
            gen=0,
            dm_scope="per-channel-peer",
            chat_type=route[0],
        )

        # Seed the fake: pretend generation 3 is persisted on disk.
        sessions._max_gen[bucket] = 3

        # Fresh dispatcher picks up the seed.
        d2 = _dispatcher(sessions, FakeCtx(), client)
        assert d2._conv.current_gen(route) == 3

        # /new advances PAST the persisted generation.
        await d2.handle_message(_inbound("/new"))
        assert d2._conv.current_gen(route) == 4

    @pytest.mark.asyncio
    async def test_no_persisted_entry_starts_at_zero(self) -> None:
        """With no persisted entry (max_generation -> -1), gen starts at 0."""
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())

        dm = _inbound(text="hi", open_id="ou_abc123")
        route = d._route(dm)

        # No entry seeded -> max_generation returns -1 by default.
        # ConversationState clamps with max(0, ...) so gen is 0, not -1.
        assert d._conv.current_gen(route) == 0


class TestSharedSessionContext:
    """A group session is shared, so private context must not be assembled
    into a reply the whole group reads. `_route` isolates the turn HISTORY;
    `minimal_context` is what withholds memory / lessons / skills."""

    @pytest.mark.asyncio
    async def test_group_turn_withholds_private_context(self) -> None:
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider)
        ctx = FakeCtx()
        d = _dispatcher(sessions, ctx, FakeClient())

        await d.handle_message(
            _inbound("hello", chat_type="group", chat_id="oc_grp", message_id="m1")
        )

        assert ctx.minimal == [True], ctx.minimal

    @pytest.mark.asyncio
    async def test_dm_turn_keeps_full_context(self) -> None:
        """The DM is where context-rich work belongs, so the same rule must NOT
        strip a p2p turn -- otherwise the channel is useless for real work."""
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        sessions = FakeSessions(provider)
        ctx = FakeCtx()
        d = _dispatcher(sessions, ctx, FakeClient())

        await d.handle_message(
            _inbound("hello", chat_type="p2p", open_id="ou_abc123", message_id="m1")
        )

        assert ctx.minimal == [False], ctx.minimal


# ------------------------------------------------------------------
# Tests: the /compact capability gate (#8156)
# ------------------------------------------------------------------


class TestCompactCapabilityGate:
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

        assert provider.compacted is False
        assert any("自动压缩上下文" in c for _, c in client.replies)

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
    async def test_thresholds_decline_silently_on_auto_managed_backend(self) -> None:
        # Hard: no forced compaction; soft: no /compact nudge — the backend
        # compacts on its own as context fills (#8156).
        provider = FakeProvider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider, ctx_pct=96.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("one", message_id="m1"))
        assert provider.compacted is False

        sessions._ctx_pct = 85.0
        await d.handle_message(_inbound("two", message_id="m2"))
        assert not any("已自动压缩" in c for _, c in client.replies)
        assert not any("对话上下文已较长" in c for _, c in client.replies)
