"""Tests for kiro_crew.feishu.renderer (FeishuRenderer, Layer 2b)."""

from __future__ import annotations

import pytest

from kiro_crew.feishu.renderer import FeishuRenderer
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
    async def test_empty_buffer_sends_ellipsis(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_done()
        assert len(c.replies) == 1
        assert c.replies[0] == ("msg1", "…")


class TestOptionsTrailer:
    """Feishu renders no tappable chip, so the trailer degrades to numbered text.

    Deleting it left the user unable to learn the choices existed at all. The
    grammar itself is pinned once in ``test_options_cap_contract.py`` against the
    shared helper; what these drive is that ``text()`` — the string that both
    reaches the user and is persisted to history — actually goes through it.
    """

    @pytest.mark.asyncio
    async def test_a_complete_trailer_becomes_a_numbered_list(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("Hi\n\n[OPTIONS: a | b]")
        await r.on_done()
        assert c.replies[0][1] == "Hi\n\n1. a\n2. b"

    @pytest.mark.asyncio
    async def test_an_unfinished_marker_is_kept(self) -> None:
        """Feishu buffers the whole turn and replies once, so it never streams.

        There is no partial frame for a half-arrived marker to flash in: text
        reaches the user only from ``on_done``. So a dangling ``[OPTIONS`` is the
        assistant's own prose, and cutting it is silent, permanent data loss.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("see the [OPTIONS section")
        await r.on_done()
        assert c.replies[0][1] == "see the [OPTIONS section"

    @pytest.mark.asyncio
    async def test_plain_text_passes_through(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("plain text")
        await r.on_done()
        assert c.replies[0][1] == "plain text"


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


class TestRedactionNotice:
    """A rewritten answer is followed by one notice reply; clean answers are not.

    Feishu delivers the TurnDriver's already-redacted stream verbatim, so the
    tests feed the placeholder tag the driver writes. The tally counts the
    delivered reply body; shared wording is pinned in
    ``test_credential_redaction_notice.py``.
    """

    @pytest.mark.asyncio
    async def test_redacted_answer_is_followed_by_one_notice(self) -> None:
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk(f"Run: psql {CREDENTIAL_REDACTION_TAGS[0]}")
        await r.on_done()
        assert len(c.replies) == 2
        assert c.replies[0][1].startswith("Run: psql ")
        assert "Security notice" in c.replies[1][1]

    @pytest.mark.asyncio
    async def test_clean_answer_sends_no_notice(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("All green, deploy finished.")
        await r.on_done()
        assert len(c.replies) == 1

    @pytest.mark.asyncio
    async def test_notice_send_failure_does_not_fail_a_delivered_turn(self) -> None:
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        c = FakeClient()
        real_send = c.send_reply

        async def send_but_fail_the_notice(message_id: str, text: str) -> bool:
            if "Security notice" in text:
                raise RuntimeError("feishu down after the answer")
            return await real_send(message_id, text)

        c.send_reply = send_but_fail_the_notice  # type: ignore[method-assign]
        r = _renderer(c)
        await r.on_text_chunk(f"Run: psql {CREDENTIAL_REDACTION_TAGS[0]}")
        await r.on_done()  # must not raise
        assert len(c.replies) == 1  # the answer itself was delivered

    @pytest.mark.asyncio
    async def test_a_false_notice_verdict_is_contained_too(self) -> None:
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        c = FakeClient()
        real_send = c.send_reply

        async def refuse_the_notice(message_id: str, text: str) -> bool:
            await real_send(message_id, text)
            return "Security notice" not in text

        c.send_reply = refuse_the_notice  # type: ignore[method-assign]
        r = _renderer(c)
        await r.on_text_chunk(f"Run: psql {CREDENTIAL_REDACTION_TAGS[0]}")
        await r.on_done()  # a False verdict on the notice is logged, not raised


# ---------------------------------------------------------------------------
# Streaming card mode (opt-in; the buffered path above stays the default)
# ---------------------------------------------------------------------------


class FakeCard:
    """Stands in for StreamingCardSession, recording the frames pushed."""

    def __init__(self, client: object, message_id: str) -> None:
        self.client = client
        self.message_id = message_id
        self.frames: list[str] = []
        self.final: str | None = None
        # Knobs the tests flip to drive each branch.
        self.start_ok = True
        self.live = True
        self.delivered_final = True
        self.anchor_gone = False
        self.push_raises = False

    async def start(self) -> bool:
        return self.start_ok

    async def push(self, text: str, *, force: bool = False) -> None:
        if self.push_raises:
            raise RuntimeError("push exploded")
        self.frames.append(text)

    async def finish(self, text: str) -> bool:
        self.final = text
        return self.delivered_final


def _streaming_renderer(
    client: FakeClient, monkeypatch: object, card: FakeCard | None = None
) -> tuple[FeishuRenderer, list[FakeCard]]:
    """Build a streaming renderer whose card session is a FakeCard."""
    import kiro_crew.feishu.renderer as mod

    made: list[FakeCard] = []

    def factory(cl: object, mid: str) -> FakeCard:
        made.append(card if card is not None else FakeCard(cl, mid))
        return made[-1]

    monkeypatch.setattr(mod, "StreamingCardSession", factory)  # type: ignore[attr-defined]
    return FeishuRenderer(client, "msg1", _CAPS, streaming=True), made


class TestStreamingTurnStart:
    @pytest.mark.asyncio
    async def test_a_card_is_opened_and_no_text_is_sent(self, monkeypatch) -> None:
        c = FakeClient()
        r, made = _streaming_renderer(c, monkeypatch)

        await r.on_turn_start()

        assert len(made) == 1
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_a_second_turn_start_does_not_open_a_second_card(self, monkeypatch) -> None:
        """on_turn_start is called twice per turn, so it has to be idempotent."""
        c = FakeClient()
        r, made = _streaming_renderer(c, monkeypatch)

        await r.on_turn_start()
        await r.on_turn_start()

        assert len(made) == 1

    @pytest.mark.asyncio
    async def test_a_failed_start_leaves_the_buffered_path_intact(self, monkeypatch) -> None:
        c = FakeClient()
        card = FakeCard(c, "msg1")
        card.start_ok = False
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("hello")
        await r.on_done()

        assert card.frames == []
        assert c.replies == [("msg1", "hello")]

    @pytest.mark.asyncio
    async def test_a_start_that_found_the_anchor_gone_suppresses_the_fallback(
        self, monkeypatch
    ) -> None:
        """A recall between card-create and card-reply must not raise.

        ``start`` records the gone anchor on the session and returns False. That
        verdict is the only thing left that knows a text reply cannot land
        either, so the session is kept even though it never went live: dropping
        it turned a recall the user performed on purpose into a delivery error.
        """
        c = FakeClient()
        # A reply to a recalled anchor is rejected, which the renderer turns
        # into a RuntimeError -- the harm this suppression exists to avoid.
        c.send_ok = False
        card = FakeCard(c, "msg1")
        card.start_ok = False
        card.live = False
        card.delivered_final = False
        card.anchor_gone = True
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("answer")
        await r.on_done()

        assert card.frames == []
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_a_start_that_failed_for_another_reason_still_replies(self, monkeypatch) -> None:
        """Only the gone-anchor verdict is worth keeping a dead session for."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        card.start_ok = False
        card.live = False
        card.delivered_final = False
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("answer")
        await r.on_done()

        assert c.replies == [("msg1", "answer")]


class TestStreamingPushes:
    @pytest.mark.asyncio
    async def test_frames_carry_the_cumulative_text(self, monkeypatch) -> None:
        c = FakeClient()
        r, made = _streaming_renderer(c, monkeypatch)

        await r.on_turn_start()
        await r.on_text_chunk("Hel")
        await r.on_text_chunk("lo")

        assert made[0].frames[-1] == "Hello"

    @pytest.mark.asyncio
    async def test_a_tool_call_is_shown_then_cleared(self, monkeypatch) -> None:
        c = FakeClient()
        r, made = _streaming_renderer(c, monkeypatch)

        await r.on_turn_start()
        await r.on_text_chunk("working")
        await r.on_tool_call("call-1", "grep")

        assert any("grep" in f for f in made[0].frames)

    @pytest.mark.asyncio
    async def test_a_dead_card_stops_receiving_frames(self, monkeypatch) -> None:
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        card.live = False
        await r.on_text_chunk("ignored")

        assert card.frames == []

    @pytest.mark.asyncio
    async def test_a_raising_push_never_breaks_the_turn(self, monkeypatch) -> None:
        """A rendering nicety must not cost the user the reply."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        card.push_raises = True
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("still fine")
        card.delivered_final = False
        await r.on_done()

        assert c.replies == [("msg1", "still fine")]


class TestStreamingDone:
    @pytest.mark.asyncio
    async def test_a_delivered_card_suppresses_the_text_reply(self, monkeypatch) -> None:
        """The user already has the answer; a text reply would duplicate it."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("done")
        await r.on_done()

        assert card.final == "done"
        assert c.replies == []

    @pytest.mark.asyncio
    async def test_a_delivered_card_still_gets_the_redaction_notice(self, monkeypatch) -> None:
        """A rewritten answer inside the card is followed by the same notice reply.

        The card body is the delivered answer, so the tally is counted over it;
        the notice itself is a plain reply on the anchor, as in buffered mode.
        """
        from kiro_crew.security import CREDENTIAL_REDACTION_TAGS

        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk(f"Run: psql {CREDENTIAL_REDACTION_TAGS[0]}")
        await r.on_done()

        assert card.final is not None and card.final.startswith("Run: psql ")
        assert len(c.replies) == 1
        assert "Security notice" in c.replies[0][1]

    @pytest.mark.asyncio
    async def test_an_undelivered_card_falls_back_to_text(self, monkeypatch) -> None:
        c = FakeClient()
        card = FakeCard(c, "msg1")
        card.delivered_final = False
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("payload")
        await r.on_done()

        assert c.replies == [("msg1", "payload")]

    @pytest.mark.asyncio
    async def test_a_recalled_anchor_suppresses_the_fallback_too(self, monkeypatch) -> None:
        """Replying to a recalled anchor fails as well, so do not try."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        card.delivered_final = False
        card.anchor_gone = True
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("lost")
        await r.on_done()

        assert c.replies == []

    @pytest.mark.asyncio
    async def test_a_live_frame_is_redacted_before_it_reaches_the_card(self, monkeypatch) -> None:
        """A live frame is on screen before ``text()`` scrubs at the send boundary.

        The channel-neutral stream pass upstream is a literal byte scan, so a
        credential split by markdown emphasis survives it and the card's own
        markdown rendering puts it back together. Without a scrub here the
        credential is displayed for as long as it takes the next frame to arrive.
        """
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("key AKIA**IOSFODNN7EXAMPLE** here")

        assert card.frames, "the chunk should have produced a frame"
        assert all("IOSFODNN7EXAMPLE" not in f for f in card.frames)
        assert "[REDACTED: credential]" in card.frames[-1]

    @pytest.mark.asyncio
    async def test_the_tool_footer_frame_is_redacted_too(self, monkeypatch) -> None:
        """The forced frame around a tool call carries the same body."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("key AKIAIOSFODNN7EXAMPLE")
        await r.on_tool_call("call-1", "grep")

        assert all("AKIAIOSFODNN7EXAMPLE" not in f for f in card.frames)
        assert "grep" in card.frames[-1]

    @pytest.mark.asyncio
    async def test_redaction_leaves_ordinary_frames_untouched(self, monkeypatch) -> None:
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("just prose, nothing secret")

        assert card.frames[-1] == "just prose, nothing secret"

    @pytest.mark.asyncio
    async def test_an_options_trailer_is_withheld_from_live_frames(self, monkeypatch) -> None:
        """A half-arrived marker must not flash; the final text still carries it."""
        c = FakeClient()
        card = FakeCard(c, "msg1")
        r, _made = _streaming_renderer(c, monkeypatch, card)

        await r.on_turn_start()
        await r.on_text_chunk("pick one\n[OPTIONS: a | b]")
        await r.on_done()

        assert all("[OPTIONS:" not in f for f in card.frames)
        assert card.final is not None and "a" in card.final
