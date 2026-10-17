"""A recycle backend is warned before Crew restarts its session.

Every messaging surface holds its own soft-threshold nudge, and every one of them
stays silent for a harness-managed backend (kas), whose context shrinks on its
own. A backend in ``ACP_BACKENDS_CONTEXT_RECYCLE`` (deepseek) is different: Crew restarts that
session at ``session.autocompact_pct`` and the agent forgets the conversation.

Each surface's ``_maybe_notice`` is driven directly against a minimal stand-in for
its dispatcher, so the matrix below covers all nine surfaces with one set of
assertions:

* inside the band ``[threshold - margin, threshold)`` a recycle backend gets ONE
  warning that offers ``/new`` (``!new`` on Discord) and never ``/compact``;
* a harness-managed backend at the same reading still gets nothing;
* at or past the threshold the recycle is already running, so nothing is promised;
* leaving the band re-arms the warning, so the next fill after a restart warns.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.config.sections import CONTEXT_WARN_MARGIN_PCT
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.feishu.transport_dispatch import FeishuDispatcher
from kiro_crew.imessage.transport_dispatch import IMessageDispatcher
from kiro_crew.messaging import commands
from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.teams.transport_dispatch import TeamsDispatcher
from kiro_crew.telegram.transport_dispatch import TelegramDispatcher
from kiro_crew.webex.transport_dispatch import WebexDispatcher
from kiro_crew.wecom.transport_dispatch import WeComDispatcher
from kiro_crew.weixin.transport_dispatch import WeixinDispatcher
from kiro_crew.whatsapp import transport_dispatch as whatsapp_dispatch
from kiro_crew.whatsapp.transport_dispatch import WhatsAppDispatcher

THRESHOLD = 70.0
IN_BAND = THRESHOLD - CONTEXT_WARN_MARGIN_PCT / 2  # 65: warn
BELOW_BAND = THRESHOLD - CONTEXT_WARN_MARGIN_PCT - 1  # 59: quiet, re-arms
AT_THRESHOLD = THRESHOLD  # recycle already fired: quiet


class _Sessions:
    """The two reads ``_maybe_notice`` makes of the session manager."""

    def __init__(self) -> None:
        self.pct = 0.0

    def check_context_usage(self, key: str, provider: Any) -> float:
        return self.pct

    def effective_autocompact_pct(self, key: str) -> float:
        return THRESHOLD

    def compact_wait_budget_secs(self) -> float:
        return 1.0


class _Provider:
    """A live provider that refuses ``/compact`` as *backend* and never compacts."""

    def __init__(self, backend: str) -> None:
        self.manual_compact_unsupported_backend = backend
        self.compacted = 0

    async def compact(self) -> None:  # pragma: no cover - must never be reached
        self.compacted += 1

    async def wait_for_compaction(self, timeout: float) -> dict:  # pragma: no cover
        return {"type": "completed"}


def _fake(sent: list[str], **extra: Any) -> SimpleNamespace:
    """A dispatcher stand-in: sessions, conversation state, both threshold reads."""

    async def _record(*args: Any, **_kw: Any) -> bool:
        sent.append(next(a for a in reversed(args) if isinstance(a, str)))
        return True

    ns = SimpleNamespace(
        sessions=_Sessions(),
        _conv=ConversationState(),
        _thresholds=lambda: (80, 95),
        _soft_threshold=lambda: 80,
    )
    ns.record = _record
    for name, value in extra.items():
        setattr(ns, name, value)
    return ns


def _discord(sent):
    fake = _fake(sent)
    fake.client = SimpleNamespace(send_message=fake.record)
    return fake, lambda p: DiscordDispatcher._maybe_notice(fake, "chan", "scope", "k", p)


def _telegram(sent):
    fake = _fake(sent)
    fake.client = object()
    fake._reply = fake.record
    fake._route_thread = lambda route: None
    route = ("1", "")
    return fake, lambda p: TelegramDispatcher._maybe_notice(fake, 1, route, "k", p)


def _feishu(sent):
    fake = _fake(sent)
    fake.client = SimpleNamespace(send_reply=fake.record)
    fake._route = lambda inbound: ("ou", "")
    inbound = SimpleNamespace(message_id="m1")
    return fake, lambda p: FeishuDispatcher._maybe_notice(fake, inbound, "k", p)


def _imessage(sent):
    fake = _fake(sent)
    fake.client = object()
    fake._notify = fake.record
    inbound = SimpleNamespace(handle="+1555")
    return fake, lambda p: IMessageDispatcher._maybe_notice(fake, inbound, "k", p)


def _teams(sent):
    fake = _fake(sent)
    fake.client = object()
    fake._reply = fake.record
    fake._identity = lambda inbound: "a@example.com"
    inbound = SimpleNamespace()
    return fake, lambda p: TeamsDispatcher._maybe_notice(fake, inbound, "k", p)


def _webex(sent):
    fake = _fake(sent)
    fake._reply = fake.record
    inbound = SimpleNamespace(room_type="direct", person_email="a@example.com", room_id="r")
    return fake, lambda p: WebexDispatcher._maybe_notice(fake, inbound, "k", p)


def _wecom(sent):
    fake = _fake(sent)
    fake._notice_bubble = fake.record
    inbound = SimpleNamespace(userid="u1")
    return fake, lambda p: WeComDispatcher._maybe_notice(fake, inbound, "k", p)


def _weixin(sent):
    fake = _fake(sent)
    fake._say = fake.record
    return fake, lambda p: WeixinDispatcher._maybe_notice(fake, "u1", "k", p)


def _whatsapp(sent):
    fake = _fake(sent)
    fake._say = fake.record
    return fake, lambda p: WhatsAppDispatcher._maybe_notice(fake, "jid", "k", p, unprompted=False)


SURFACES = {
    "discord": (_discord, "!new"),
    "telegram": (_telegram, "/new"),
    "feishu": (_feishu, "/new"),
    "imessage": (_imessage, "/new"),
    "teams": (_teams, "/new"),
    "webex": (_webex, "/new"),
    "wecom": (_wecom, "/new"),
    "weixin": (_weixin, "/new"),
    "whatsapp": (_whatsapp, "/new"),
}


@pytest.fixture(autouse=True)
def _unmuted(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    # WhatsApp also asks whether delivery is muted; the stand-in has no link store.
    _floor_monkeypatch.setattr(whatsapp_dispatch, "delivery_is_muted", lambda *a, **k: False)


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_a_recycle_backend_is_warned_once_inside_the_band(surface: str) -> None:
    build, new_cmd = SURFACES[surface]
    sent: list[str] = []
    fake, notice = build(sent)
    provider = _Provider("deepseek")
    fake.sessions.pct = IN_BAND

    await notice(provider)
    await notice(provider)

    assert len(sent) == 1, sent
    assert new_cmd in sent[0]
    assert "compact" not in sent[0].replace("can't compact", "")
    assert "无法压缩" in sent[0] or "can't compact" in sent[0]
    assert provider.compacted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_a_harness_managed_backend_still_gets_nothing(surface: str) -> None:
    build, _ = SURFACES[surface]
    sent: list[str] = []
    fake, notice = build(sent)
    provider = _Provider("kas")
    for pct in (IN_BAND, 85.0, 99.0):
        fake.sessions.pct = pct
        await notice(provider)

    assert sent == []
    assert provider.compacted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_no_warning_once_the_recycle_threshold_is_reached(surface: str) -> None:
    build, _ = SURFACES[surface]
    sent: list[str] = []
    fake, notice = build(sent)
    provider = _Provider("deepseek")
    for pct in (AT_THRESHOLD, 85.0, 99.0):
        fake.sessions.pct = pct
        await notice(provider)

    assert sent == []
    assert provider.compacted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", sorted(SURFACES))
async def test_the_next_fill_after_a_restart_warns_again(surface: str) -> None:
    build, _ = SURFACES[surface]
    sent: list[str] = []
    fake, notice = build(sent)
    provider = _Provider("deepseek")
    for pct in (IN_BAND, AT_THRESHOLD, BELOW_BAND, IN_BAND):
        fake.sessions.pct = pct
        await notice(provider)

    assert len(sent) == 2, sent


@pytest.mark.asyncio
async def test_a_muted_whatsapp_conversation_is_not_warned_and_keeps_its_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    fake, notice = _whatsapp(sent)
    provider = _Provider("deepseek")
    fake.sessions.pct = IN_BAND
    monkeypatch.setattr(whatsapp_dispatch, "delivery_is_muted", lambda *a, **k: True)
    await notice(provider)
    assert sent == []
    monkeypatch.setattr(whatsapp_dispatch, "delivery_is_muted", lambda *a, **k: False)
    await notice(provider)
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("threshold", "pct", "due"),
    [
        (70.0, 59.9, False),
        (70.0, 60.0, True),
        (70.0, 69.9, True),
        (70.0, 70.0, False),
        (50.0, 45.0, True),
    ],
)
def test_the_band_follows_the_sessions_live_threshold(
    threshold: float, pct: float, due: bool
) -> None:
    sessions = SimpleNamespace(effective_autocompact_pct=lambda key: threshold)
    assert commands.recycle_warning_due(sessions, "k", pct) is due


def test_a_threshold_read_that_raises_never_warns() -> None:
    sessions = SimpleNamespace(effective_autocompact_pct=lambda key: 1 / 0)
    assert commands.recycle_warning_due(sessions, "k", 65.0) is False


@pytest.mark.parametrize(
    ("backend", "expected"),
    [("deepseek", "deepseek"), ("kas", None), ("unheard-of", None), (None, None)],
)
def test_only_a_recycle_member_is_a_recycle_backend(backend: Any, expected: Any) -> None:
    provider = SimpleNamespace(manual_compact_unsupported_backend=backend)
    assert commands.recycle_backend(provider) == expected
