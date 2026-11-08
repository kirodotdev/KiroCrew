"""Tests for kiro_crew.feishu.renderer (FeishuRenderer, Layer 2b)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from kiro_crew.feishu.renderer import FeishuRenderer
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.messaging.transport import TransportCapabilities

#: A real 2x2 PNG: extraction admits a file on its byte signature.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


class FakeClient:
    """Records send_reply / send_image_reply calls without requiring lark_oapi."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.images: list[tuple[str, bytes]] = []
        # Flip to False to exercise the dropped-reply path.
        self.send_ok = True
        self.image_ok = True

    async def send_reply(self, message_id: str, text: str) -> bool:
        # Mirrors the real LarkClient contract: True on delivery. The
        # renderer treats a falsy return as a dropped reply, so a fake
        # returning None would silently exercise the failure path.
        self.replies.append((message_id, text))
        return self.send_ok

    async def send_image_reply(self, message_id: str, data: bytes) -> bool:
        self.images.append((message_id, data))
        return self.image_ok


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
        answer, notice = (text for _, text in c.replies)
        assert answer.startswith("Run: psql ")
        assert "Security notice" in notice


class TestOutboundImages:
    """A local image the answer references goes out as its own image reply."""

    @staticmethod
    def _image_renderer(c: FakeClient, root: Path) -> FeishuRenderer:
        r = FeishuRenderer(c, "msg1", FEISHU_CAPABILITIES, uploads_allowed=True)
        r.authorize_upload_root(str(root))
        return r

    @pytest.mark.asyncio
    async def test_the_text_goes_first_then_the_image_without_its_path(
        self, tmp_path: Path
    ) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(_PNG)
        c = FakeClient()
        r = self._image_renderer(c, tmp_path)
        await r.on_text_chunk(f"Here is the chart.\n\n![chart]({chart})")
        await r.on_done()
        assert c.replies == [("msg1", "Here is the chart.")]
        assert c.images == [("msg1", _PNG)]

    @pytest.mark.asyncio
    async def test_an_image_outside_the_root_keeps_its_path_and_says_why(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        outside = tmp_path / "elsewhere.png"
        outside.write_bytes(_PNG)
        c = FakeClient()
        r = self._image_renderer(c, root)
        await r.on_text_chunk(f"![chart]({outside})")
        await r.on_done()
        assert c.images == []
        ((_, text),) = c.replies
        assert f"![chart]({outside})" in text
        assert "⚠️" in text and "not sent" in text

    @pytest.mark.asyncio
    async def test_past_three_refusals_the_rest_collapse_into_a_count(self, tmp_path: Path) -> None:
        c = FakeClient()
        r = self._image_renderer(c, tmp_path)
        await r.on_text_chunk(" ".join(f"![x{i}](rel{i}.png)" for i in range(5)))
        await r.on_done()
        ((_, text),) = c.replies
        assert text.count("not sent") == 3
        assert text.endswith("⚠️ …and 2 more")

    @pytest.mark.asyncio
    async def test_a_failed_upload_is_named_by_its_path(self, tmp_path: Path) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(_PNG)
        c = FakeClient()
        c.image_ok = False
        r = self._image_renderer(c, tmp_path)
        await r.on_text_chunk(f"Done.\n\n![chart]({chart})")
        await r.on_done()  # the answer landed, so a failed picture is not a failed turn
        answer, follow_up = (text for _, text in c.replies)
        assert answer == "Done."
        assert follow_up == f"⚠️ Couldn't upload:\n{chart}"

    @pytest.mark.asyncio
    async def test_an_image_only_answer_whose_upload_fails_still_fails_the_turn(
        self, tmp_path: Path
    ) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(_PNG)
        c = FakeClient()
        c.image_ok = False
        c.send_ok = False
        r = self._image_renderer(c, tmp_path)
        await r.on_text_chunk(f"![chart]({chart})")
        with pytest.raises(RuntimeError, match="not delivered"):
            await r.on_done()

    @pytest.mark.asyncio
    async def test_an_answer_that_is_only_an_image_sends_only_the_image(
        self, tmp_path: Path
    ) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(_PNG)
        c = FakeClient()
        r = self._image_renderer(c, tmp_path)
        await r.on_text_chunk(f"![chart]({chart})")
        await r.on_done()
        assert c.replies == []
        assert c.images == [("msg1", _PNG)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("allowed, root", [(False, True), (True, False)])
    async def test_a_restricted_or_unrooted_session_keeps_printing_the_path(
        self, tmp_path: Path, allowed: bool, root: bool
    ) -> None:
        chart = tmp_path / "chart.png"
        chart.write_bytes(_PNG)
        c = FakeClient()
        r = FeishuRenderer(c, "msg1", FEISHU_CAPABILITIES, uploads_allowed=allowed)
        if root:
            r.authorize_upload_root(str(tmp_path))
        await r.on_text_chunk(f"![chart]({chart})")
        await r.on_done()
        assert c.images == []
        assert c.replies == [("msg1", f"![chart]({chart})")]
