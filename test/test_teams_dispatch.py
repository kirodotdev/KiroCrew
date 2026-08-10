"""Tests for the Teams transport dispatch (turn bookkeeping, commands,
threshold notices) against the shared TurnDriver, with fully mocked
sessions/provider/context."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport_dispatch import TeamsDispatcher


class FakeProvider:
    supports_steer = True

    def __init__(self, events: list) -> None:
        self._events = events
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
        pass

    async def reject_tool(self, rid) -> None:
        pass

    async def compact(self) -> None:
        self.compacted = True

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": ""}


class FakeSessions:
    def __init__(self, provider, *, is_new=True, raise_on_get=None, ctx_pct=0.0, acquire=True):
        self._p = provider
        self._is_new = is_new
        self._raise = raise_on_get
        self._ctx_pct = ctx_pct
        self._acquire = acquire
        self.released: list = []
        self.successes: list = []
        self.failures: list = []
        self.acquired: list = []
        self.channels: list = []
        self.last_agent = None
        self._busy = False

    async def get_or_create(self, key, *, agent, channel_id, **_kw):
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
        return self._p is not None

    def is_busy(self, key) -> bool:
        return self._busy

    def max_generation(self, bucket: str) -> int:
        return -1


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

    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source):
        assert runtime_source == "teams"
        return (text, None)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.typing: list[tuple[str, str]] = []
        self._n = 0

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        self.sent.append((conversation_id, content, service_url))
        self._n += 1
        return f"MSG{self._n}"

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        self.typing.append((conversation_id, service_url))


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
        teams=SimpleNamespace(hard_threshold_pct=95.0, soft_threshold_pct=80.0),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(sessions, ctx, client, *, conv_log=None, agent=None, cfg=None):
    d = TeamsDispatcher(
        sessions=sessions,
        ctx_builder=ctx,
        cfg=cfg or _cfg(),
        agent=agent,
        conv_log=conv_log,
        approval_mode="interactive",
    )
    d.client = client
    return d


_EMAIL = "kyle@example.com"
_SVC = "https://smba.example.com/"


def _inbound(text: str = "hello", email: str = _EMAIL) -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=email,
        aad_object_id="aad-1",
        activity_id="act-1",
    )


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

        key = d._session_key(_EMAIL)
        assert any(content == "hi there" for (_, content, _) in client.sent)
        assert client.typing == [("CONV", _SVC)]  # typing indicator at start
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

        await d.handle_message(_inbound("hi"))  # must not raise

        assert sessions.released == []
        assert sessions.failures == []

    @pytest.mark.asyncio
    async def test_soft_threshold_notice_separate_and_unpersisted(self) -> None:
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        conv = FakeConvLog()
        d = _dispatcher(sessions, FakeCtx(), client, conv_log=conv)

        await d.handle_message(_inbound("hello"))

        assert any("/compact" in content for (_, content, _) in client.sent)
        assistant_texts = [t for (_, role, t) in conv.appended if role == "assistant"]
        assert assistant_texts == ["answer"]

    @pytest.mark.asyncio
    async def test_hard_threshold_forces_compaction(self) -> None:
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        sessions = FakeSessions(provider, ctx_pct=96.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert provider.compacted is True
        assert any("compacted" in content for (_, content, _) in client.sent)


class TestCommands:
    @pytest.mark.asyncio
    async def test_new_bumps_gen_and_acks(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/new"))

        assert client.sent == [("CONV", "✅ Started a fresh conversation.", _SVC)]
        assert d._conv.current_gen(_EMAIL) == 1
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_help_command(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("/help"))

        assert len(client.sent) == 1
        assert "/compact" in client.sent[0][1]
        assert sessions.successes == []

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
        assert client.sent == [("CONV", "🗜️ Context compacted.", _SVC)]

    @pytest.mark.asyncio
    async def test_stable_session_key_per_user(self) -> None:
        sessions = FakeSessions(FakeProvider([]))
        d = _dispatcher(sessions, FakeCtx(), FakeClient())
        assert d._session_key(_EMAIL) == d._session_key(_EMAIL)


class TestInboundGovernance:
    @pytest.mark.asyncio
    async def test_inbound_dropped_when_channels_policy_denies(self, monkeypatch) -> None:
        # A host-profile deny of the `channels` member `teams` must drop the
        # message before any session work or reply — the per-message recheck
        # that catches a policy tightened after connect (startup gate only
        # blocks CONNECTING).
        async def _deny(_member: str) -> bool:
            return False

        # Patched on messaging.dispatch: the gate moved into the shared
        # pipeline, and the teams dispatcher's own early check calls the
        # ``inbound_permitted`` wrapper, which resolves
        # ``channel_inbound_permitted`` from dispatch's globals at call time --
        # so this one patch covers both the channel-side and pipeline gates.
        monkeypatch.setattr(
            "kiro_crew.messaging.dispatch.channel_inbound_permitted", _deny
        )
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert client.sent == []
        assert sessions.acquired == []
        assert sessions.successes == []
        assert sessions.released == []
