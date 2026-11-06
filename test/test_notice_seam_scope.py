"""Only a provider-notice seam takes the notice-boundary grade.

``repaired_after_a_sent_tail`` grades every cross-message seam a channel renderer
delivers. A reply-to-reply seam keeps the adjacent-join grade alone. A seam with a
delivered notice on either side also reads the two messages with a space between
them, so ``Authorization: Bearer`` at the end of a notice cannot acquire its token
from the answer below it, and its repair widens to the display-redacted join. A
renderer that keeps one predecessor slot for notices and reply chunks records a
notice as ``NoticeTail``, and only after the notice lands.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest
from split_credential_boundary_helpers import HEAD, TAIL

from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.feishu.renderer import FeishuRenderer
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.imessage.renderer import IMessageRenderer
from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
from kiro_crew.messaging.renderer import (
    CREDENTIAL_REDACTION_TAGS,
    NoticeTail,
    _default_redactor,
    repaired_after_a_sent_tail,
)
from kiro_crew.messaging.split import offset_clear_of_a_sent_tail
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES
from kiro_crew.telegram.renderer import _fresh_seam_safe
from kiro_crew.webex.renderer import WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES
from kiro_crew.wecom.renderer import WeComRenderer
from kiro_crew.wecom.transport import WECOM_CAPABILITIES
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

TOKEN = "opaque-scope-token"
SCHEME = "Authorization: Bearer"
PROSE = " and following prose survives."
ANSWER = TOKEN + PROSE

#: Reply-to-reply pairs: two whose adjacent join completes a key, and the
#: scheme-then-token pair only a space between the messages would complete.
REPLY_PAIRS = [
    ("Authorization: Bear", f"er {TOKEN}{PROSE}"),
    ("Authorization: Bear", f"er **{TOKEN}**{PROSE}"),
    (f"{'w ' * 20}{HEAD}", f"{TAIL} and then more ordinary trailing prose."),
    (SCHEME, ANSWER),
]


@pytest.mark.parametrize(("sent", "later"), REPLY_PAIRS)
def test_a_reply_seam_keeps_the_adjacent_join_grade(sent: str, later: str) -> None:
    offset = offset_clear_of_a_sent_tail(sent, later, _default_redactor)
    expected = CREDENTIAL_REDACTION_TAGS[0] + later[offset:] if offset else None
    assert repaired_after_a_sent_tail(sent, later, _default_redactor) == expected


@pytest.mark.parametrize("wrapper", ["", "**", "`"])
def test_a_reply_ending_in_a_scheme_is_not_read_across_a_space(wrapper: str) -> None:
    later = f"{wrapper}{TOKEN}{wrapper}{PROSE}"
    assert repaired_after_a_sent_tail(SCHEME, later, _default_redactor) is None


@pytest.mark.parametrize("wrapper", ["", "**", "`"])
@pytest.mark.parametrize("marked_by", ["flag", "notice_tail"])
def test_a_notice_seam_is_read_across_a_space(wrapper: str, marked_by: str) -> None:
    later = f"{wrapper}{TOKEN}{wrapper}{PROSE}"
    if marked_by == "flag":
        repaired = repaired_after_a_sent_tail(
            SCHEME, later, _default_redactor, across_message_space=True
        )
    else:
        repaired = repaired_after_a_sent_tail(NoticeTail(SCHEME), later, _default_redactor)
    assert repaired is not None
    assert TOKEN not in repaired
    assert repaired.endswith(PROSE)


def test_a_notice_tail_is_the_text_it_wraps() -> None:
    tail = NoticeTail("⚠️ Model fallback")
    assert tail == "⚠️ Model fallback"
    assert isinstance(tail, str)


def test_discord_grades_by_its_predecessor() -> None:
    assert DiscordRenderer._seam_against(SCHEME, ANSWER) == ANSWER
    graded = DiscordRenderer._seam_against(NoticeTail(SCHEME), ANSWER)
    assert TOKEN not in graded
    assert graded.endswith(PROSE)


@pytest.mark.parametrize("slot", ["_sent_tail", "_note_below_stream"])
def test_telegram_fresh_send_grades_by_its_predecessor(slot: str) -> None:
    def graded(predecessor: str, **kwargs: Any) -> str:
        renderer = SimpleNamespace(_sent_tail="", _note_below_stream=None)
        setattr(renderer, slot, predecessor)
        return _fresh_seam_safe(renderer, ANSWER, **kwargs)

    assert TOKEN in graded(SCHEME)
    assert TOKEN not in graded(NoticeTail(SCHEME))
    assert TOKEN not in graded(SCHEME, notice=True)


def test_whatsapp_hands_a_notice_below_the_bubble_on_as_a_notice() -> None:
    renderer = SimpleNamespace(_note_below_live=NoticeTail(" ⚠️ notice "), _sent_tail="")
    WhatsAppRenderer._record_sent(renderer, "the sealed reply")
    assert renderer._sent_tail == "⚠️ notice"
    assert isinstance(renderer._sent_tail, NoticeTail)
    assert renderer._note_below_live is None
    WhatsAppRenderer._record_sent(renderer, " the next reply ")
    assert renderer._sent_tail == "the next reply"
    assert not isinstance(renderer._sent_tail, NoticeTail)


class _Client:
    """Every channel's send call a notice reaches, recording what landed."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.ok = True

    def _land(self, text: str) -> bool:
        if self.ok:
            self.sent.append(text)
        return self.ok

    async def send_reply(self, _target: str, text: str) -> bool:
        return self._land(text)

    async def send_proactive(self, _target: str, text: str) -> bool:
        return self._land(text)

    async def send(self, _target: str, text: str) -> None:
        self._land(text)

    async def send_message(self, _target: str, text: str, *_args: Any, **_kw: Any) -> Any:
        return "M1" if self._land(text) else None


_SHARED_SLOT_RENDERERS: dict[str, Callable[[_Client], Any]] = {
    "feishu": lambda c: FeishuRenderer(c, "om_1", FEISHU_CAPABILITIES),
    "imessage": lambda c: IMessageRenderer(c, "+1", IMESSAGE_CAPABILITIES),
    "teams": lambda c: TeamsRenderer(c, "conv", "https://smba.example", TEAMS_CAPABILITIES),
    "webex": lambda c: WebexRenderer(c, "ROOM", WEBEX_CAPABILITIES),
    "wecom": lambda c: WeComRenderer(c, "rq1", "https://resp.url", WECOM_CAPABILITIES, chat_id="CHAT"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", sorted(_SHARED_SLOT_RENDERERS))
async def test_a_delivered_notice_is_recorded_as_a_notice(channel: str) -> None:
    client = _Client()
    renderer = _SHARED_SLOT_RENDERERS[channel](client)
    await renderer.on_notice("⚠️ " + SCHEME)
    assert client.sent
    assert isinstance(renderer._sent_tail, NoticeTail)
    assert renderer._sent_tail.strip() == client.sent[-1].strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["feishu", "webex", "wecom"])
async def test_an_undelivered_notice_leaves_the_reply_predecessor(channel: str) -> None:
    client = _Client()
    renderer = _SHARED_SLOT_RENDERERS[channel](client)
    renderer._sent_tail = "the reply above"
    client.ok = False
    with pytest.raises(RuntimeError, match="refused provider notice"):
        await renderer.on_notice("⚠️ " + SCHEME)
    assert renderer._sent_tail == "the reply above"
    assert not isinstance(renderer._sent_tail, NoticeTail)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["webex", "wecom"])
async def test_a_reply_chunk_after_a_notice_is_recorded_as_a_reply(channel: str) -> None:
    client = _Client()
    renderer = _SHARED_SLOT_RENDERERS[channel](client)
    await renderer.on_notice("⚠️ " + SCHEME)
    await renderer._send_fresh(ANSWER)
    assert TOKEN not in client.sent[-1]
    assert not isinstance(renderer._sent_tail, NoticeTail)
    await renderer._send_fresh(SCHEME)
    await renderer._send_fresh(ANSWER)
    assert TOKEN in client.sent[-1]
