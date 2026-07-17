"""Tests for kiro_crew.wechat.renderer (WeComRenderer, Layer 2b)."""

from __future__ import annotations

import pytest

from kiro_crew.wechat.renderer import WeComRenderer
from kiro_crew.wechat.transport import WECOM_CAPABILITIES


class FakeClient:
    """Records WS stream frames and response_url fallback POSTs."""

    def __init__(self, stream_ok: bool = True) -> None:
        self.frames: list[dict] = []
        self.replies: list[tuple[str, str]] = []
        self._stream_ok = stream_ok

    async def send_stream(
        self, req_id: str, stream_id: str, content: str, *, finish: bool
    ) -> bool:
        self.frames.append({"req_id": req_id, "content": content, "finish": finish})
        return self._stream_ok

    async def send_reply(self, url: str, content: str) -> None:
        self.replies.append((url, content))


def _renderer(client: FakeClient, req_id: str = "rq1") -> WeComRenderer:
    return WeComRenderer(client, req_id, "https://resp.url", WECOM_CAPABILITIES)


class TestStreaming:
    @pytest.mark.asyncio
    async def test_turn_start_sends_placeholder(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        assert c.frames[0]["content"] == "🤔 …"
        assert c.frames[0]["finish"] is False

    @pytest.mark.asyncio
    async def test_turn_start_idempotent(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_turn_start()  # second call no-ops
        assert len(c.frames) == 1

    @pytest.mark.asyncio
    async def test_final_answer_is_accumulated_text(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        await r.on_done()
        final = c.frames[-1]
        assert final["content"] == "Hello world"
        assert final["finish"] is True

    @pytest.mark.asyncio
    async def test_options_trailer_stripped(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B | C]")
        await r.on_done()
        assert c.frames[-1]["content"] == "Pick one"

    @pytest.mark.asyncio
    async def test_tool_footer_pushed(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_read", tool_kind="read")
        # force-pushed frame carries the transient footer
        assert any("🔧 正在运行：fs_read" in f["content"] for f in c.frames)

    @pytest.mark.asyncio
    async def test_error_done_shows_error_text(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert c.frames[-1]["content"] == "⚠️ 出错了，请重试"
        assert c.frames[-1]["finish"] is True


class TestFallback:
    @pytest.mark.asyncio
    async def test_no_req_id_uses_response_url(self) -> None:
        c = FakeClient()
        r = WeComRenderer(c, "", "https://resp.url", WECOM_CAPABILITIES)
        await r.on_turn_start()  # no stream (no req_id)
        await r.on_text_chunk("reply text")
        await r.on_done()
        assert c.frames == []  # never streamed
        assert c.replies == [("https://resp.url", "reply text")]

    @pytest.mark.asyncio
    async def test_stream_died_falls_back_to_response_url(self) -> None:
        c = FakeClient(stream_ok=False)  # every send_stream reports failure
        r = _renderer(c)
        await r.on_turn_start()  # placeholder send reports False -> stream_ok flips off
        await r.on_text_chunk("answer")
        await r.on_done()
        assert c.replies == [("https://resp.url", "answer")]


class TestClose:
    @pytest.mark.asyncio
    async def test_close_after_done_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("done text")
        await r.on_done()
        finish_frames_before = sum(1 for f in c.frames if f["finish"])
        await r.close()
        finish_frames_after = sum(1 for f in c.frames if f["finish"])
        assert finish_frames_before == finish_frames_after == 1

    @pytest.mark.asyncio
    async def test_close_without_done_finalizes(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("partial")
        await r.close()  # turn never reached on_done (e.g. cold-start failure)
        assert any(f["finish"] for f in c.frames)


class TestPromptChoice:
    @pytest.mark.asyncio
    async def test_prompt_choice_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        before = len(c.frames)
        await r.on_prompt_choice([{"label": "yes"}], "rq")  # WeCom has no buttons
        assert len(c.frames) == before  # nothing rendered, no raise
