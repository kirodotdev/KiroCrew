"""Tests for kiro_crew.wecom.transport_dispatch (WeComDispatcher) + commands."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
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

    def build_message(self, text, is_new, key, *, channel_id, agent, resumed):
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.replies: list[tuple[str, str]] = []

    async def send_stream(self, req_id, sid, content, *, finish) -> bool:
        self.frames.append({"sid": sid, "content": content, "finish": finish})
        return True

    async def send_reply(self, url, content) -> None:
        self.replies.append((url, content))


class FakeConvLog:
    def __init__(self) -> None:
        self.appended: list[tuple[str, str, str]] = []
        self.titles: dict[str, str] = {}

    def append(self, key, role, text) -> None:
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
    return WeComInbound(
        userid=userid, text=text, response_url="https://r", req_id="rq1", chatid=""
    )


# ------------------------------------------------------------------
# Tests: full turn
# ------------------------------------------------------------------


class TestTurn:
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

        # Notice surfaced as a SEPARATE bubble (its own stream_id, finish=True).
        assert any("对话上下文已较长" in f["content"] for f in client.frames)
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

        assert client.replies == [("https://r", "✅ 已开始新对话")]
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
        assert client.replies == [("https://r", "🗜️ 已压缩上下文。")]

    @pytest.mark.asyncio
    async def test_compact_refused_while_turn_busy(self) -> None:
        provider = FakeProvider([])
        sessions = FakeSessions(provider, acquire=False, has_session=True)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert provider.compacted is False
        assert sessions.released == []
        assert client.replies == [("https://r", "⏳ 正在处理上一条消息，请稍后再试 /compact。")]

    @pytest.mark.asyncio
    async def test_compact_without_active_session(self) -> None:
        sessions = FakeSessions(None, acquire=False, has_session=False)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/compact"))

        assert sessions.released == []
        assert client.replies == [("https://r", "ℹ️ 当前没有可压缩的对话。")]

    @pytest.mark.asyncio
    async def test_link_rejected_on_wecom(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/link"))

        assert len(client.replies) == 1
        assert "/link" in client.replies[0][1]  # explains it's unavailable here
        assert sessions.successes == []  # no LLM turn

    def test_parse_command(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("新对话") == "new"
        assert parse_command("清空") == "new"
        assert parse_command("/compact") == "compact"
        assert parse_command("/link") == "link"
        assert parse_command("/unlink") == "unlink"
        assert parse_command("hello") is None

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
        assert any("合并" in content for _url, content in client.replies)
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

        assert any("重发" in content for _url, content in client.replies)
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
        assert not any("合并" in content for _url, content in client.replies)
        assert any("重发" in content for _url, content in client.replies)
        assert sessions.successes == []
