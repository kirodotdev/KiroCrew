"""Tests for kiro_crew.signal_cli.transport_dispatch (SignalDispatcher)."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.signal_cli.client import KIND_DATA, KIND_NOTE_TO_SELF, SignalInbound
from kiro_crew.signal_cli.commands import HELP_TEXT, parse_command
from kiro_crew.signal_cli.renderer import SignalRenderer
from kiro_crew.signal_cli.transport_dispatch import SignalDispatcher

ACCOUNT = "+447700900001"
OWNER = "+447700900002"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, recipient: str, text: str) -> str:
        self.sent.append((recipient, text))
        return "1"

    async def send_typing(self, recipient: str) -> None:
        return None

    async def mark_read(self, recipient: str, timestamp: int) -> None:
        return None


class FakeProvider:
    def __init__(self) -> None:
        self.supports_steer = True
        self.steered: list[str] = []
        self.compacted = 0
        self.compact_result: dict[str, str] = {"type": "completed", "summary": ""}

    def has_active_turn(self) -> bool:
        return True

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def compact(self) -> None:
        self.compacted += 1

    async def wait_for_compaction(self, timeout: float = 300.0) -> dict[str, str]:
        return self.compact_result


class MirrorStore:
    """The session map's mirror/origin slice the dispatcher binds on every turn."""

    def mirror_opt_out(self, key: str) -> bool:
        return False

    def get_mirror_link(self, key: str) -> Any:
        return vars(self).setdefault("mirror_links", {}).get(key)

    def set_mirror_link(self, key: str, link: Any, **_kw: Any) -> None:
        vars(self).setdefault("mirror_links", {})[key] = link

    def set_origin_link(self, key: str, link: Any) -> None:
        vars(self).setdefault("origin_links", {})[key] = link


class FakeSessions(MirrorStore):
    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.providers: dict[str, Any] = {}
        self.sessions: set[str] = set()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.usage_pct = 0.0
        self.reserved_generations: list[str] = []

    def compact_wait_budget_secs(self) -> float:
        return 300.0

    def is_busy(self, key: str) -> bool:
        return key in self.busy

    def get_provider(self, key: str) -> Any:
        return self.providers.get(key)

    def has_session(self, key: str) -> bool:
        return key in self.sessions

    async def try_acquire(self, key: str) -> bool:
        self.acquired.append(key)
        return True

    def release(self, key: str) -> None:
        self.released.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return self.usage_pct

    def list_sessions(self) -> list[str]:
        return sorted(self.sessions)

    def reserve_generation(self, session_key: str) -> None:
        self.reserved_generations.append(session_key)

    async def aflush(self) -> None:
        return None

    def max_generation(self, *_args: object, **_kwargs: object) -> int:
        return 0


def _dispatcher() -> tuple[SignalDispatcher, FakeClient, FakeSessions]:
    from kiro_crew.config.loader import KiroCrewConfig

    sessions = FakeSessions()
    dispatcher = SignalDispatcher(
        sessions=sessions,  # type: ignore[arg-type]
        ctx_builder=object(),  # type: ignore[arg-type]
        cfg=KiroCrewConfig(),
        conv_log=None,
    )
    client = FakeClient()
    dispatcher.client = client  # type: ignore[assignment]
    return dispatcher, client, sessions


def _inbound(text: str, *, kind: str = KIND_DATA) -> SignalInbound:
    inbound = SignalInbound(
        kind=kind,
        text=text,
        source_number=ACCOUNT if kind == KIND_NOTE_TO_SELF else OWNER,
        timestamp=1700000000100,
        account=ACCOUNT,
    )
    inbound.principal = ACCOUNT if kind == KIND_NOTE_TO_SELF else OWNER
    # The transport's operator verdict (SignalTransport.is_operator).
    inbound.operator = True
    return inbound


@pytest.fixture(autouse=True)
def _permit(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    async def _yes(_channel: str) -> bool:
        return True

    _floor_monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _yes)


class TestCommands:
    @pytest.mark.parametrize(
        ("text", "cmd"),
        [
            ("/new", "new"),
            ("/START now", "new"),
            ("/compact", "compact"),
            ("/help", "help"),
            ("/commands", "help"),
            ("/status", "status"),
            ("/stop", "stop"),
            ("/Cancel", "stop"),
        ],
    )
    def test_parse(self, text: str, cmd: str) -> None:
        assert parse_command(text) == cmd

    @pytest.mark.parametrize("text", ["hello", "/unknown", "", "  "])
    def test_not_a_command(self, text: str) -> None:
        assert parse_command(text) is None

    def test_help_text_is_plain(self) -> None:
        assert "**" not in HELP_TEXT and "`" not in HELP_TEXT

    @pytest.mark.asyncio
    async def test_help_is_answered_to_the_sender_without_a_session(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        await dispatcher.handle_message(_inbound("/help"))
        assert client.sent == [(OWNER, HELP_TEXT)]
        assert sessions.acquired == []

    @pytest.mark.asyncio
    async def test_note_to_self_commands_are_answered_in_note_to_self(self) -> None:
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/help", kind=KIND_NOTE_TO_SELF))
        assert client.sent == [(ACCOUNT, HELP_TEXT)]

    @pytest.mark.asyncio
    async def test_new_bumps_the_generation(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        before = dispatcher._session_key(OWNER)
        await dispatcher.handle_message(_inbound("/new"))
        after = dispatcher._session_key(OWNER)
        assert after != before
        assert sessions.reserved_generations == [after]
        assert "fresh conversation" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_compact_runs_and_releases(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(OWNER)
        provider = FakeProvider()
        sessions.providers[key] = provider
        sessions.sessions.add(key)
        await dispatcher.handle_message(_inbound("/compact"))
        assert provider.compacted == 1
        assert sessions.released == [key]
        assert "compacted" in client.sent[0][1]

    @pytest.mark.asyncio
    async def test_a_governance_deny_drops_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _no(_channel: str) -> bool:
            return False

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.inbound_permitted", _no)
        dispatcher, client, _ = _dispatcher()
        await dispatcher.handle_message(_inbound("/help"))
        assert client.sent == []


class TestTurn:
    @pytest.mark.asyncio
    async def test_a_message_drives_one_turn_on_a_signal_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, _, _ = _dispatcher()
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(_inbound("what's up"))
        assert len(turns) == 1
        turn = turns[0]
        assert turn.channel_type == "signal"
        assert turn.session_key.startswith("signal:")
        assert turn.conversation_id == f"signal:{OWNER}"
        assert turn.user_text == "what's up"
        # Interactive by default: the typed-reply ladder, bound to this session.
        assert turn.decider is not None and turn.decider.session_key == turn.session_key
        assert turn.renderer._approval_session_key == turn.session_key
        assert isinstance(turn.renderer, SignalRenderer)
        assert turn.renderer._handle == OWNER
        assert turn.renderer._reply_timestamp == 1700000000100

    @pytest.mark.asyncio
    async def test_a_note_to_self_turn_replies_to_the_account_with_no_receipt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dispatcher, _, _ = _dispatcher()
        turns: list[Any] = []

        async def fake_drive_turn(turn: Any, **_kwargs: Any) -> None:
            turns.append(turn)

        monkeypatch.setattr("kiro_crew.signal_cli.transport_dispatch.drive_turn", fake_drive_turn)
        await dispatcher.handle_message(_inbound("todo", kind=KIND_NOTE_TO_SELF))
        assert turns[0].renderer._handle == ACCOUNT
        assert turns[0].renderer._reply_timestamp == 0

    @pytest.mark.asyncio
    async def test_a_mid_turn_message_is_steered(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        key = dispatcher._session_key(OWNER)
        sessions.busy.add(key)
        provider = FakeProvider()
        sessions.providers[key] = provider
        await dispatcher.handle_message(_inbound("also this"))
        assert provider.steered == ["also this"]
        assert "Folded into" in client.sent[0][1]


class TestThresholds:
    @pytest.mark.asyncio
    async def test_the_soft_threshold_nudges_once(self) -> None:
        dispatcher, client, sessions = _dispatcher()
        sessions.usage_pct = 85.0
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        await dispatcher._maybe_notice(_inbound("x"), "k", FakeProvider())
        assert len(client.sent) == 1 and "/compact" in client.sent[0][1]

    def test_thresholds_fall_back_to_defaults_without_a_signal_section(self) -> None:
        dispatcher, _, _ = _dispatcher()
        assert dispatcher._thresholds() == (80, 95)

    def test_session_identity(self) -> None:
        dispatcher, _, _ = _dispatcher()
        assert dispatcher._session_key(OWNER).startswith("signal:")
        assert dispatcher._resolve_agent() == "kirocrew"


class TestNoticeRedaction:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("split", ["", "**", "`", "_"])
    async def test_an_advisory_notice_is_screened_before_it_is_sent(self, split: str) -> None:
        # _notify bypasses the renderer, so it screens on its own: a notice can
        # quote a status line or an error the channel did not author.
        key = "AKIA" + "IOSFODNN7EXAMPLE"
        dispatcher, client, _ = _dispatcher()
        spelled = f"{key[:4]}{split}{key[4:]}{split}"
        assert await dispatcher._notify(OWNER, f"compaction failed: {spelled}") is True
        ((_, sent),) = client.sent
        assert key not in sent
        assert key not in sent.replace("*", "").replace("`", "").replace("_", "")
