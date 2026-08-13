"""Coverage for the Teams dispatcher's mid-turn and /compact edge paths.

Companion to ``test_teams_dispatch.py``, which owns this module's main behaviour; this file
only closes the coverage gaps left at its edges. New behaviour cases belong
in the sibling, not here.

A message arriving while a turn is in flight must either fold into that turn or
tell the user to resend — never silently vanish, and never falsely claim a merge
for a turn that already ended. ``/compact`` has the mirror-image problem: it must
distinguish "busy" from "nothing to compact", and always release the session it
acquired. Those branches are what these tests pin.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport_dispatch import TeamsDispatcher

_EMAIL = "quinn@example.com"
_SVC = "https://smba.example.com/"


class _Provider:
    def __init__(
        self,
        *,
        supports_steer: bool = True,
        steer_result: bool = True,
        active_turn: bool = True,
        compact_exc: Exception | None = None,
        with_steer: bool = True,
    ) -> None:
        self.supports_steer = supports_steer
        self._steer_result = steer_result
        self._active = active_turn
        self._compact_exc = compact_exc
        self.compacted = False
        self.steered: list[str] = []
        if not with_steer:
            del self.steer

    def has_active_turn(self) -> bool:
        return self._active

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return self._steer_result

    async def stream(self, message: str):
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="reply text")
        yield AcpEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, rid) -> None:
        pass

    async def reject_tool(self, rid) -> None:
        pass

    async def compact(self) -> None:
        if self._compact_exc is not None:
            raise self._compact_exc
        self.compacted = True

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": ""}


class _Sessions:
    def __init__(
        self,
        provider,
        *,
        busy: list[bool] | None = None,
        acquire: bool = True,
        has_session: bool = True,
        ctx_pct: float = 0.0,
    ) -> None:
        self._p = provider
        self._busy = list(busy or [False])
        self._acquire = acquire
        self._has_session = has_session
        self._ctx_pct = ctx_pct
        self.released: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.acquired: list[str] = []
        self.busy_checks = 0

    async def get_or_create(self, key, *, agent, channel_id):
        return self._p, True, False

    async def set_channel(self, key, cid) -> None:
        pass

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
        self.busy_checks += 1
        idx = min(self.busy_checks - 1, len(self._busy) - 1)
        return self._busy[idx]

    def max_generation(self, bucket: str) -> int:
        return -1


class _Hooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, title, **kw):
        return SimpleNamespace(action="")


class _Ctx:
    def __init__(self) -> None:
        self.hooks = _Hooks()

    def build_message(self, text, is_new, key, *, channel_id, agent, resumed, runtime_source):
        return (text, None)


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, conversation_id: str, content: str, service_url: str) -> str:
        self.sent.append((conversation_id, content, service_url))
        return f"MSG{len(self.sent)}"

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        pass


def _cfg():
    return SimpleNamespace(
        agent=SimpleNamespace(default_agent="", approval_mode="interactive"),
        teams=SimpleNamespace(hard_threshold_pct=95.0, soft_threshold_pct=80.0),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(sessions, client):
    d = TeamsDispatcher(
        sessions=sessions,
        ctx_builder=_Ctx(),
        cfg=_cfg(),
        agent=None,
        conv_log=None,
        approval_mode="interactive",
    )
    d.client = client
    return d


def _inbound(text: str = "hello") -> TeamsInbound:
    return TeamsInbound(
        conversation_id="CONV",
        conversation_type="personal",
        service_url=_SVC,
        text=text,
        user_email=_EMAIL,
        aad_object_id="aad-1",
        activity_id="act-1",
    )


class TestMidTurnMessage:
    @pytest.mark.asyncio
    async def test_message_folds_into_the_running_turn(self) -> None:
        provider = _Provider()
        sessions = _Sessions(provider, busy=[True, True])
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("also check the logs"))

        assert provider.steered == ["also check the logs"]
        assert client.sent == [("CONV", "⏳ Folded into the reply in progress.", _SVC)]
        assert sessions.successes == []

    @pytest.mark.asyncio
    async def test_refused_steer_asks_the_user_to_resend(self) -> None:
        provider = _Provider(steer_result=False)
        sessions = _Sessions(provider, busy=[True, True])
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("second message"))

        assert "please resend" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_provider_without_steer_support_asks_to_resend(self) -> None:
        provider = _Provider(supports_steer=False)
        sessions = _Sessions(provider, busy=[True, True])
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("second message"))

        assert provider.steered == []
        assert "please resend" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_finished_turn_is_never_falsely_acknowledged(self) -> None:
        provider = _Provider(active_turn=False)
        sessions = _Sessions(provider, busy=[True, True])
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("second message"))

        assert provider.steered == []
        assert "please resend" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_turn_that_ended_mid_check_runs_as_a_fresh_turn(self) -> None:
        # busy on the dispatch check, idle by the time _handle_busy re-checks.
        provider = _Provider()
        sessions = _Sessions(provider, busy=[True, False, False])
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("hello"))

        assert provider.steered == []
        assert any(content == "reply text" for (_, content, _) in client.sent)
        assert sessions.successes == [d._session_key(_EMAIL)]


class TestCompactCommand:
    @pytest.mark.asyncio
    async def test_busy_session_defers_the_compaction(self) -> None:
        sessions = _Sessions(_Provider(), acquire=False, has_session=True)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/compact"))

        assert "try `/compact` again shortly" in client.sent[0][1]
        assert sessions.released == []

    @pytest.mark.asyncio
    async def test_no_session_reports_nothing_to_compact(self) -> None:
        sessions = _Sessions(_Provider(), acquire=False, has_session=False)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/compact"))

        assert "no conversation to compact" in client.sent[0][1]
        assert sessions.released == []

    @pytest.mark.asyncio
    async def test_acquired_but_providerless_session_still_releases(self) -> None:
        sessions = _Sessions(None)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/compact"))

        assert "no conversation to compact" in client.sent[0][1]
        assert sessions.released == [d._session_key(_EMAIL)]

    @pytest.mark.asyncio
    async def test_compaction_failure_is_reported_and_released(self) -> None:
        provider = _Provider(compact_exc=RuntimeError("provider died"))
        sessions = _Sessions(provider)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/compact"))

        assert "Compaction failed" in client.sent[0][1]
        assert sessions.released == [d._session_key(_EMAIL)]


class TestThresholdNotice:
    @pytest.mark.asyncio
    async def test_hard_threshold_compaction_failure_never_breaks_the_turn(self) -> None:
        provider = _Provider(compact_exc=RuntimeError("compaction unavailable"))
        sessions = _Sessions(provider, ctx_pct=99.0)
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("hello"))

        assert any(content == "reply text" for (_, content, _) in client.sent)
        assert not any("compacted automatically" in content for (_, content, _) in client.sent)
        assert sessions.released == [d._session_key(_EMAIL)]


class TestHelpAndAgentResolution:
    @pytest.mark.asyncio
    async def test_help_never_touches_a_session(self) -> None:
        sessions = _Sessions(_Provider())
        client = _Client()
        d = _dispatcher(sessions, client)

        await d.handle_message(_inbound("/help"))

        assert len(client.sent) == 1
        assert sessions.acquired == []

    def test_explicit_agent_wins_over_the_config_default(self) -> None:
        sessions = _Sessions(_Provider())
        d = _dispatcher(sessions, _Client())
        assert d._resolve_agent() == "kirocrew"
        d.agent = "custom-agent"
        assert d._resolve_agent() == "custom-agent"
