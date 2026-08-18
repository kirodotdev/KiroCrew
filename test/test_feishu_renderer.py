"""Tests for kiro_crew.feishu.renderer (FeishuRenderer, Layer 2b)."""

from __future__ import annotations

import pytest

from kiro_crew.feishu.renderer import FeishuRenderer, _strip_options
from kiro_crew.messaging.transport import TransportCapabilities


class FakeClient:
    """Records send_reply calls without requiring lark_oapi."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        # Flip to False to exercise the dropped-reply path.
        self.send_ok = True

    async def send_reply(self, message_id: str, text: str) -> bool:
        # Mirrors the real LarkClient contract: True on delivery. The
        # renderer treats a falsy return as a dropped reply, so a fake
        # returning None would silently exercise the failure path.
        self.replies.append((message_id, text))
        return self.send_ok


# Feishu v1 declares max_buttons=0 (no interactive widgets).
_CAPS = TransportCapabilities(max_buttons=0, max_message_chars=0)


def _renderer(client: FakeClient, message_id: str = "msg1") -> FeishuRenderer:
    return FeishuRenderer(client, message_id, _CAPS)


class TestTurnStart:
    @pytest.mark.asyncio
    async def test_on_turn_start_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        assert c.replies == []


class TestTextAccumulation:
    @pytest.mark.asyncio
    async def test_chunks_accumulated_and_sent_once_on_done(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        assert c.replies == []  # nothing sent yet
        await r.on_done()
        assert len(c.replies) == 1
        assert c.replies[0] == ("msg1", "Hello world")

    @pytest.mark.asyncio
    async def test_options_trailer_stripped(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B | C]")
        await r.on_done()
        assert c.replies[0][1] == "Pick one"

    @pytest.mark.asyncio
    async def test_unterminated_options_stripped(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("Answer text\n\n[OPTIONS: A | B")
        await r.on_done()
        assert c.replies[0][1] == "Answer text"

    @pytest.mark.asyncio
    async def test_empty_buffer_sends_ellipsis(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_done()
        assert len(c.replies) == 1
        assert c.replies[0] == ("msg1", "…")


class TestStripOptions:
    def test_complete_options_tag(self) -> None:
        assert _strip_options("Hi\n[OPTIONS: a | b]") == "Hi"

    def test_unterminated_options_tag(self) -> None:
        assert _strip_options("Hi\n[OPTIONS: a | b") == "Hi"

    def test_no_options_passthrough(self) -> None:
        assert _strip_options("plain text") == "plain text"


class TestErrorDone:
    @pytest.mark.asyncio
    async def test_error_with_buffer_sends_buffer(self) -> None:
        """Even on error, if there's accumulated text it is sent."""
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("partial answer")
        await r.on_done(stop_reason="error")
        # text() returns truthy -> it wins over the error fallback
        assert c.replies[0][1] == "partial answer"

    @pytest.mark.asyncio
    async def test_error_with_empty_buffer(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_done(stop_reason="error")
        assert c.replies[0][1] == "⚠️ 出错了，请重试"


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_on_done_idempotent(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("response")
        await r.on_done()
        await r.on_done()  # second call is a no-op
        assert len(c.replies) == 1


class TestClose:
    @pytest.mark.asyncio
    async def test_close_without_done_finalizes(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("partial")
        await r.close()
        assert len(c.replies) == 1
        # close() calls on_done(stop_reason="error") but text() is truthy
        assert c.replies[0][1] == "partial"

    @pytest.mark.asyncio
    async def test_close_after_done_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("answer")
        await r.on_done()
        await r.close()
        assert len(c.replies) == 1


class TestDeliveryFailure:
    """A reply the transport did not deliver must not pass as a finished turn."""

    @pytest.mark.asyncio
    async def test_on_done_raises_when_the_reply_is_dropped(self) -> None:
        c = FakeClient()
        c.send_ok = False
        r = _renderer(c)
        await r.on_text_chunk("an answer the user never sees")
        with pytest.raises(RuntimeError, match="not delivered"):
            await r.on_done()

    @pytest.mark.asyncio
    async def test_close_swallows_a_dropped_error_reply(self) -> None:
        """close() runs in the driver's finally, so it must not raise and
        replace the error that actually brought the turn down."""
        c = FakeClient()
        c.send_ok = False
        r = _renderer(c)
        await r.close()  # must not raise
        assert len(c.replies) == 1


class TestNoOpHandlers:
    @pytest.mark.asyncio
    async def test_on_tool_call_no_send(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_tool_call("t1", "fs_read", tool_kind="read", tool_purpose="read")
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_on_prompt_choice_no_send(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_prompt_choice([{"label": "yes"}], "rq")
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_on_thinking_no_send(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_thinking("reasoning step")
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_on_compaction_no_send(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_compaction(75.0)
        assert c.replies == []
