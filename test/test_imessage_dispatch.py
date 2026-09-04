"""Tests for kiro_crew.imessage.transport_dispatch (IMessageDispatcher)."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.imessage.client import IMessageInbound
from kiro_crew.imessage.transport_dispatch import IMessageDispatcher
from kiro_crew.session_allocation import SessionClosingError

HANDLE = "+15551234567"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, to: str, text: str) -> str:
        assert to == HANDLE
        self.sent.append(text)
        return "GUID"

    async def send_typing(self, selector: dict[str, Any]) -> None:
        return None

    async def mark_read(self, selector: dict[str, Any]) -> None:
        return None


class FakeProvider:
    def __init__(self, *, steerable: bool = True, active: bool = True) -> None:
        self.supports_steer = steerable
        self.steered: list[str] = []
        self._active = active
        self.compacted = 0

    def has_active_turn(self) -> bool:
        return self._active

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def compact(self) -> None:
        self.compacted += 1

    async def wait_for_compaction(self) -> None:
        return None


class FakeSessions:
    def __init__(self) -> None:
        self.busy: set[str] = set()
        # `closing` mirrors SessionManager._closing so begin_turn refuses the
        # dispatch the way the real gate does after close_all.
        self.closing = False
        self.begin_turns = 0
        self.providers: dict[str, Any] = {}
        self.sessions: set[str] = set()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.acquire_ok = True
        self.usage_pct = 0.0

    def is_busy(self, key: str) -> bool:
        return key in self.busy

    def get_provider(self, key: str) -> Any:
        return self.providers.get(key)

    def has_session(self, key: str) -> bool:
        return key in self.sessions

    async def try_acquire(self, key: str) -> bool:
        if not self.acquire_ok:
            return False
        self.acquired.append(key)
        return True

    def begin_turn(self, key: str) -> None:
        """The real manager's synchronous pre-dispatch closing gate."""
        self.begin_turns += 1
        if self.closing:
            raise SessionClosingError("SessionManager is closing")

    def release(self, key: str) -> None:
        self.released.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return self.usage_pct

    def list_sessions(self) -> list[str]:
        return sorted(self.sessions)

    def max_generation(self, *_args: object, **_kwargs: object) -> int:
        """No prior generation to seed from — a fresh install starts at 0."""
        return 0


def _cfg() -> Any:
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig()


def _dispatcher(
    sessions: FakeSessions | None = None,
) -> tuple[IMessageDispatcher, FakeClient, FakeSessions]:
    sess = sessions or FakeSessions()
    dispatcher = IMessageDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=object(),  # type: ignore[arg-type]
        cfg=_cfg(),
        conv_log=None,
    )
    client = FakeClient()
    dispatcher.client = client  # type: ignore[assignment]
    return dispatcher, client, sess


def _inbound(text: str) -> IMessageInbound:
    return IMessageInbound(
        handle=HANDLE,
        text=text,
        guid="G1",
        rowid=1,
        chat_guid="iMessage;-;+15551234567",
        chat_identifier=HANDLE,
        chat_id=7,
    )


@pytest.fixture(autouse=True)
def _permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the per-message governance gate to permit."""

    async def _yes(_channel: str) -> bool:
        return True

    monkeypatch.setattr("kiro_crew.imessage.transport_dispatch.inbound_permitted", _yes)


class TestCommandIntercept:
    @pytest.mark.asyncio
    async def test_help_is_answered_without_a_session(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        await dispatcher.handle_message(_inbound("/help"))
        assert len(client.sent) == 1
        assert "/compact" in client.sent[0]
        assert sessions.acquired == []

    @pytest.mark.asyncio
    async def test_help_text_carries_no_markdown(self) -> None:
        # iMessage renders none, so asterisks and backticks would arrive literal.
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/help"))
        assert "**" not in client.sent[0]
        assert "`" not in client.sent[0]

    @pytest.mark.asyncio
    async def test_new_bumps_the_generation_so_the_session_key_changes(self) -> None:
        dispatcher, client, _ = _dispatcher()
        before = dispatcher._session_key(HANDLE)
        await dispatcher.handle_message(_inbound("/new"))
        assert dispatcher._session_key(HANDLE) != before
        assert "fresh conversation" in client.sent[0]

    @pytest.mark.asyncio
    async def test_start_is_an_alias_for_new(self) -> None:
        dispatcher, client, _ = _dispatcher()
        before = dispatcher._session_key(HANDLE)
        await dispatcher.handle_message(_inbound("/start"))
        assert dispatcher._session_key(HANDLE) != before

    @pytest.mark.asyncio
    async def test_a_governance_deny_drops_the_message_silently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no(_channel: str) -> bool:
            return False

        monkeypatch.setattr("kiro_crew.imessage.transport_dispatch.inbound_permitted", _no)
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/help"))
        assert client.sent == []


class TestCompact:
    @pytest.mark.asyncio
    async def test_compact_with_no_session_says_so(self) -> None:
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/compact"))
        assert "no conversation to compact" in client.sent[0]

    @pytest.mark.asyncio
    async def test_compact_runs_and_releases_the_session(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        provider = FakeProvider()
        sessions.providers[key] = provider
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert provider.compacted == 1
        assert sessions.released == [key]
        assert "compacted" in client.sent[0]

    @pytest.mark.asyncio
    async def test_compact_declined_on_auto_managed_backend(self) -> None:
        # A backend that cannot serve /compact gets the informational reply and
        # compact() is NEVER dispatched (#8156).
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        provider = FakeProvider()
        provider.manual_compact_unsupported_backend = "kas"
        sessions.providers[key] = provider
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert provider.compacted == 0
        assert sessions.released == [key]
        assert "manages compaction automatically" in client.sent[0]
        assert "`" not in client.sent[0]  # iMessage speech carries no markdown

    @pytest.mark.asyncio
    async def test_compact_none_capability_preserves_dispatch(self) -> None:
        # The ABC's None (supported) default keeps the existing dispatch.
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        provider = FakeProvider()
        provider.manual_compact_unsupported_backend = None
        sessions.providers[key] = provider
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert provider.compacted == 1

    @pytest.mark.asyncio
    async def test_compact_on_a_busy_session_asks_the_user_to_retry(self) -> None:
        # Compacting while a turn is mutating the same session races the
        # transcript, so it must not proceed.
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        sessions.acquire_ok = False
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert "try /compact again" in client.sent[0]
        assert sessions.released == []

    @pytest.mark.asyncio
    async def test_a_failing_compaction_is_reported_and_still_releases(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)

        class Boom(FakeProvider):
            async def compact(self) -> None:
                raise RuntimeError("nope")

        sessions.providers[key] = Boom()
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert "Compaction failed" in client.sent[0]
        assert sessions.released == [key]


class TestBusyHandling:
    @pytest.mark.asyncio
    async def test_a_mid_turn_message_is_folded_in_via_steer(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        sessions.busy.add(key)
        provider = FakeProvider()
        sessions.providers[key] = provider
        await dispatcher.handle_message(_inbound("also check CI"))
        assert provider.steered == ["also check CI"]
        assert "Folded into" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_finished_turn_that_still_reads_busy_is_not_steered(self) -> None:
        # is_busy stays True through post-turn bookkeeping, so steering there
        # would falsely acknowledge a merge into a turn that already ended.
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        sessions.busy.add(key)
        provider = FakeProvider(active=False)
        sessions.providers[key] = provider
        await dispatcher.handle_message(_inbound("hello"))
        assert provider.steered == []
        assert "please resend" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_cold_provider_asks_the_user_to_resend(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        sessions.busy.add(key)
        await dispatcher.handle_message(_inbound("hello"))
        assert "please resend" in client.sent[0]

    @pytest.mark.asyncio
    async def test_busy_messages_carry_no_markdown(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(HANDLE)
        sessions.busy.add(key)
        await dispatcher.handle_message(_inbound("hello"))
        assert "`" not in client.sent[0]


class TestThresholdNotices:
    @pytest.mark.asyncio
    async def test_below_the_soft_threshold_says_nothing(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 10.0
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_the_soft_threshold_nudges_once(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 85.0
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        assert len(client.sent) == 1
        assert "/compact" in client.sent[0]

    @pytest.mark.asyncio
    async def test_the_nudge_carries_no_markdown(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 85.0
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        assert "`" not in client.sent[0]

    @pytest.mark.asyncio
    async def test_the_hard_threshold_compacts_automatically(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 99.0
        provider = FakeProvider()
        await dispatcher._maybe_notice(_inbound("x"), "k", provider)
        assert provider.compacted == 1
        assert "compacted automatically" in client.sent[0]

    @pytest.mark.asyncio
    async def test_thresholds_decline_silently_on_auto_managed_backend(self) -> None:
        # Hard: no forced compaction; soft: no /compact nudge — the backend
        # compacts on its own as context fills (#8156).
        dispatcher, client, sessions = _dispatcher()
        provider = FakeProvider()
        provider.manual_compact_unsupported_backend = "kas"
        sessions.usage_pct = 99.0
        await dispatcher._maybe_notice(_inbound("x"), "k", provider)
        assert provider.compacted == 0
        sessions.usage_pct = 85.0
        await dispatcher._maybe_notice(_inbound("x"), "k", provider)
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_a_failed_auto_compaction_is_not_announced_as_success(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 99.0

        class Boom(FakeProvider):
            async def compact(self) -> None:
                raise RuntimeError("nope")

        await dispatcher._maybe_notice(_inbound("x"), "k", Boom())
        assert client.sent == []


class TestSessionIdentity:
    def test_the_session_key_is_namespaced_to_the_channel(self) -> None:
        # The namespace is what makes these sessions visible to the sidebar and
        # attributable in the audit log.
        dispatcher, _, _ = _dispatcher()
        assert dispatcher._session_key(HANDLE).startswith("imessage:")

    def test_the_agent_falls_back_to_the_canonical_kirocrew_agent(self) -> None:
        # Otherwise the session loads kiro-cli's bare default and has no
        # spawn_run / cron tools.
        dispatcher, _, _ = _dispatcher()
        assert dispatcher._resolve_agent() == "kirocrew"
