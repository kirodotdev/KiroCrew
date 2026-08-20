"""Tests for kiro_crew.feishu.transport_dispatch (FeishuDispatcher)."""

from __future__ import annotations

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

    async def get_or_create(self, key, *, agent, channel_id):
        self.last_agent = agent
        if self._raise is not None:
            raise self._raise
        return self._p, self._is_new, False

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
    ):
        assert runtime_source == "feishu"
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

    def append(self, key, role, text, *, agent=None) -> None:
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
            idle_reset_minutes=0,
            daily_reset_hour=-1,
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
) -> LarkInbound:
    return LarkInbound(
        open_id=open_id,
        text=text,
        message_id=message_id,
        chat_type=chat_type,
        chat_id=chat_id,
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
