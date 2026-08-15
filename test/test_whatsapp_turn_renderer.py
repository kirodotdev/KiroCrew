"""WhatsApp turn-renderer transcript tests (buffered emit, sentinel)."""

from __future__ import annotations

import pytest

from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.whatsapp.group_gate import SILENCE_SENTINEL
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

CAPS = TransportCapabilities(max_message_chars=4096, max_buttons=0)


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, jid: str, content: str) -> str:
        self.sent.append((jid, content))
        return "ID1"


class FakeClient:
    def __init__(self) -> None:
        self.typing: list[bool] = []

    async def send_typing(self, jid: str, active: bool) -> None:
        self.typing.append(active)


def make(unprompted: bool = False):
    transport, client = FakeTransport(), FakeClient()
    r = WhatsAppRenderer(
        transport, client, "chat@s.whatsapp.net", CAPS, unprompted=unprompted
    )
    return r, transport, client


@pytest.mark.asyncio
class TestBufferedEmit:
    async def test_nothing_sent_before_on_done(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("part one ")
        await r.on_text_chunk("part two")
        assert transport.sent == []
        await r.on_done()
        assert len(transport.sent) == 1
        assert transport.sent[0][1] == "part one part two"

    async def test_options_trailer_is_stripped(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("Pick one.\n[OPTIONS: a | b]")
        await r.on_done()
        assert transport.sent[0][1] == "Pick one."

    async def test_error_turn_sends_apology(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert "went wrong" in transport.sent[0][1]

    async def test_close_finalizes_an_unfinished_turn(self):
        r, transport, _ = make()
        await r.on_turn_start()
        await r.on_text_chunk("answer")
        await r.close()
        assert len(transport.sent) == 1


@pytest.mark.asyncio
class TestSilenceSentinel:
    async def test_sentinel_reply_is_suppressed_entirely(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk(SILENCE_SENTINEL)
        await r.on_done()
        assert transport.sent == []
        assert r.suppressed is True

    async def test_empty_unprompted_reply_is_suppressed(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_done()
        assert transport.sent == [] and r.suppressed

    async def test_real_unprompted_answer_is_delivered(self):
        r, transport, _ = make(unprompted=True)
        await r.on_turn_start()
        await r.on_text_chunk("Actually, the answer is 42.")
        await r.on_done()
        assert len(transport.sent) == 1
        assert not r.suppressed

    async def test_prompted_turn_never_suppresses_sentinel_text(self):
        r, transport, _ = make(unprompted=False)
        await r.on_turn_start()
        await r.on_text_chunk(SILENCE_SENTINEL)
        await r.on_done()
        assert len(transport.sent) == 1


@pytest.mark.asyncio
class TestTyping:
    async def test_typing_stops_by_on_done(self):
        r, _, client = make()
        await r.on_turn_start()
        await r.on_text_chunk("x")
        await r.on_done()
        assert client.typing and client.typing[-1] is False
