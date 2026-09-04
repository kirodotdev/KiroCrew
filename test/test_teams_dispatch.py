"""Tests for the Teams transport dispatch (turn bookkeeping, commands,
threshold notices) against the shared TurnDriver, with fully mocked
sessions/provider/context."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.session_allocation import SessionClosingError
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
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        # Mid-turn queue + dashboard-mirror surface the dispatcher now uses.
        self.queues: dict[str, list] = {}
        self.cleared: list = []
        self.mirror_links: dict = {}
        self.opt_outs: dict = {}
        self.locked = False

    # -- dashboard mirror -------------------------------------------------
    def mirror_opt_out(self, key) -> bool:
        return bool(self.opt_outs.get(key))

    def set_mirror_opt_out(self, key, value) -> None:
        self.opt_outs[key] = value

    def get_mirror_link(self, key):
        return self.mirror_links.get(key)

    def set_mirror_link(self, key, link, *, reason="") -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key, *, reason="") -> bool:
        return self.mirror_links.pop(key, None) is not None

    def is_mirror_paused(self, key, *, origin=False) -> bool:
        return False

    def batched_save(self):
        return contextlib.nullcontext()

    # -- mid-turn queue ---------------------------------------------------
    def enqueue(self, key, msg_ts, text, *, force=False, **kwargs) -> bool:
        if not force and not self._busy:
            return False
        self.queues.setdefault(key, []).append((msg_ts, text, kwargs))
        return True

    def dequeue(self, key):
        queue = self.queues.get(key) or []
        return queue.pop(0) if queue else None

    def clear_queue(self, key) -> None:
        self.cleared.append(key)
        self.queues.pop(key, None)

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
        return self._p is not None

    async def aflush(self) -> None:
        # The resume release flushes the session map before it reports success; a
        # double without this correctly surfaces as a release FAILURE.
        return None

    def clear_mirror_links_at(self, link, *, reason: str = "") -> list:
        return []

    def find_mirror_sessions(self, link, *, inbound_only: bool = False) -> list:
        # No resumed dashboard session in these tests, so routing is a no-op. Present
        # because Teams routes EVERY message through the resume resolver.
        return []

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

    # Names only the kwargs it asserts on and takes ``**kw`` for the rest: the
    # shared pipeline owns the call shape, so a fake that pins every kwarg
    # breaks on any field the pipeline later forwards, without that field
    # having anything to do with this channel. Mirrors the pipeline's own fake.
    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source, **kw):
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

    def append(self, key, role, text, agent=None, mid=None) -> None:
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
_SVC = "https://smba.trafficmanager.net/"


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

    @pytest.mark.asyncio
    async def test_hard_threshold_declines_silently_on_auto_managed_backend(self) -> None:
        # No /compact to dispatch and no notice: the backend compacts on its
        # own as context fills (#8156).
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider, ctx_pct=96.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert provider.compacted is False
        assert not any("compacted" in content for (_, content, _) in client.sent)

    @pytest.mark.asyncio
    async def test_soft_nudge_suppressed_on_auto_managed_backend(self) -> None:
        # The nudge advises /compact, which this backend refuses — it compacts
        # on its own, so there is nothing for the user to act on (#8156).
        provider = FakeProvider(
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="answer"), AcpEvent(kind=EVENT_COMPLETE)]
        )
        provider.manual_compact_unsupported_backend = "kas"
        sessions = FakeSessions(provider, ctx_pct=85.0)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert not any("/compact" in content for (_, content, _) in client.sent)


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
        assert any("manages compaction automatically" in content for (_, content, _) in client.sent)

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
        monkeypatch.setattr("kiro_crew.messaging.dispatch.channel_inbound_permitted", _deny)
        provider = FakeProvider([AcpEvent(kind=EVENT_COMPLETE)])
        sessions = FakeSessions(provider)
        client = FakeClient()
        d = _dispatcher(sessions, FakeCtx(), client)

        await d.handle_message(_inbound("hello"))

        assert client.sent == []
        assert sessions.acquired == []
        assert sessions.successes == []
        assert sessions.released == []
