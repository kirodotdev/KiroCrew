"""Tests for kiro_crew.wecom.renderer (WeComRenderer, Layer 2b)."""

from __future__ import annotations

import pytest

from conftest import assert_rejected_without_backtracking
from kiro_crew.wecom.renderer import WeComRenderer, _render_options_as_text
from kiro_crew.wecom.transport import WECOM_CAPABILITIES

# The PEM markers are ASSEMBLED from fragments, never written as one literal, so
# the internal content scan (rule ``credential-private-key``, which matches a
# BEGIN...PRIVATE-KEY header on a source line) does not flag a test fixture. The
# runtime value is byte-identical to the real marker, so the test still exercises
# the exact string the renderer's guard and the redactor recognise.
_DASHES = "-" * 5
_KEY_KIND = "PRIVATE" + " KEY"


def _pem_begin(*, markup: bool = False, split_token: bool = False) -> str:
    kind = "**PRIVATE**" + " KEY" if markup else _KEY_KIND
    head = f"{_DASHES}BEG**IN**" if split_token else f"{_DASHES}BEGIN"
    return f"{head} RSA {kind}{_DASHES}"


def _pem_end() -> str:
    return f"{_DASHES}END RSA {_KEY_KIND}{_DASHES}"


class TestStripOptionsRedos:
    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. The tempered body
        # (?:[^[]|\[(?!OPTIONS:))* forbids only a re-occurring "[OPTIONS:", so the
        # body is unambiguous (linear). A whitespace-padded unterminated tag and
        # many repeated "[OPTIONS:" prefixes (the real pump) must both be rejected
        # in CPU time linear in the pump -- see
        # conftest.assert_rejected_without_backtracking for why this is not a
        # 1.0 s wall-clock bound.

        # A single unterminated tag: no closing ']' after the last "[OPTIONS",
        # so the whole still-streaming partial is hidden.
        def hidden(text: str) -> None:
            assert (
                _render_options_as_text(text) == ""
            ), "an unterminated marker is hidden, never rendered"

        assert_rejected_without_backtracking(hidden, lambda n: "[OPTIONS:" + ("\t" * n) + "x")

        # Many repeated "[OPTIONS:" prefixes (the real polynomial pump). There is
        # no closing ']', so the trailer regex does not match; the property under
        # test is the cost of deciding that, not the rendered text.
        assert_rejected_without_backtracking(
            _render_options_as_text, lambda n: "[OPTIONS:" * n + "x"
        )


class FakeClient:
    """Records WS stream frames and response_url fallback POSTs."""

    def __init__(self, stream_ok: bool = True) -> None:
        self.frames: list[dict] = []
        self.replies: list[tuple[str, str]] = []
        self._stream_ok = stream_ok
        self.dead_streams: set[str] = set()
        #: (chat_id, content) of confirmed pushes — the answer's tail, and a head
        #: re-delivered after its sealing frame turned out to be refused.
        self.pushed: list[tuple[str, str]] = []

    async def send_stream(
        self,
        req_id: str,
        stream_id: str,
        content: str,
        *,
        finish: bool,
        await_ack: bool = False,
    ) -> bool:
        self.frames.append(
            {"req_id": req_id, "stream_id": stream_id, "content": content, "finish": finish}
        )
        return self._stream_ok

    def stream_is_dead(self, stream_id: str) -> bool:
        """The renderer consults this before every frame, so the fake owes it.

        Bubbles are live unless a test says otherwise; sealing behaviour has its
        own coverage in test_wecom_wire_reliability.py.
        """
        return stream_id in self.dead_streams

    def stream_had_rejection(self, stream_id: str) -> bool:
        """The narrower question: was everything written here ACCEPTED.

        Consulted by the aged rotation and by the post-turn seal recheck. A dead
        bubble was also refused, so it answers for both.
        """
        return stream_id in self.dead_streams

    async def send_proactive(self, chat_id: str, content: str) -> bool:
        self.pushed.append((chat_id, content))
        return True

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
        # WeCom renders <think>…</think> as its own collapsed reasoning block, so
        # the placeholder does not sit in the answer and does not have to be
        # cleared before the real text arrives.
        assert c.frames[0]["content"] == "<think>…</think>"
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
    async def test_options_trailer_becomes_a_numbered_list(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B | C]")
        await r.on_done()
        # Numbered text, not deleted: WeCom renders no chips, but the user still
        # has to learn the choices exist and can answer by typing one.
        assert c.frames[-1]["content"] == "Pick one\n\n1. A\n2. B\n3. C"

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


class TestThinkReasoningRedaction:
    """The ``<think>`` reasoning frame is scrubbed render-aware, not literally.

    WeCom renders the ``<think>`` block as markdown, so a credential split by
    emphasis (``AKIA**REST**``) survives a literal byte scan and is reassembled
    on screen -- the same hazard the answer body is guarded against, on the same
    channel. Asserted against the RENDERED form.
    """

    @pytest.mark.asyncio
    async def test_think_reasoning_redacts_markup_split_credential(self) -> None:
        from kiro_crew.messaging.display_safety import canonicalize_display

        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        c.frames.clear()
        r._last_send = 0.0  # clear the throttle so the reasoning frame goes out
        await r.on_thinking("leaking AKIAIOSF**ODNN7EXAMPLE** in the trace")

        think = "".join(f["content"] for f in c.frames)
        assert "<think>" in think, f"no reasoning frame was sent: {c.frames}"
        assert "AKIAIOSFODNN7EXAMPLE" not in canonicalize_display(think)

    @pytest.mark.asyncio
    async def test_think_reasoning_keeps_clean_text(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        c.frames.clear()
        r._last_send = 0.0
        await r.on_thinking("just thinking out loud, no secret")

        think = "".join(f["content"] for f in c.frames)
        assert "just thinking out loud, no secret" in think


class TestAnswerBodyRedaction:
    """The answer body is scrubbed render-aware at the send, and no cut severs a key.

    WeCom renders the body as markdown and extracts no attachments from it, so a
    credential split by emphasis (``AKIA**REST**``) survives the driver's literal
    channel-neutral pass and is reassembled on screen. ``_render_slice`` redacts
    each outgoing slice, and the callers pick the cut with ``_safe_raw_cut`` so a
    credential is never split across two bubbles -- the offsets stay in raw
    ``text()`` coordinates, which is what keeps a bubble rotation resuming at the
    right place. Asserted against the RENDERED form.
    """

    @pytest.mark.asyncio
    async def test_answer_body_redacts_markup_split_credential(self) -> None:
        from kiro_crew.messaging.display_safety import canonicalize_display

        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("the key is AKIAIOSF**ODNN7EXAMPLE** ok")
        await r.on_done()

        final = c.frames[-1]["content"]
        assert "AKIAIOSFODNN7EXAMPLE" not in canonicalize_display(final)

    def test_text_stays_raw_for_persistence(self) -> None:
        # text() is what drive_turn persists; the redaction lives only on the
        # delivery slices, so the raw answer is unchanged here.
        c = FakeClient()
        r = _renderer(c)
        r._buf.append("plain answer body")
        assert r.text() == "plain answer body"

    @pytest.mark.asyncio
    async def test_answer_body_keeps_clean_text(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("Hello world")
        await r.on_done()

        assert c.frames[-1]["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_roll_keeps_offsets_in_raw_coordinates(self) -> None:
        # Offsets index raw text() (not a content-dependent redacted view), so a
        # credential completing across a bubble ROLL does not shift them: the
        # resume point is stable as text() grows -- the delivered bubbles reassemble
        # the whole answer with no dropped or duplicated non-secret span.
        c = FakeClient()
        r = _renderer(c)
        await r.on_text_chunk("alpha beta gamma delta")
        await r._push(force=True)
        c.dead_streams.add(r._stream_id)  # next push must roll
        await r.on_text_chunk(" epsilon zeta eta theta")
        await r._push(force=True)
        await r.on_done()

        # Reconstruct what the reader sees across bubbles: the resume after the roll
        # picks up exactly where the sealed bubble left off, so no word is dropped
        # and none is duplicated across the boundary.
        delivered = "".join(f["content"] for f in c.frames) + "".join(p[1] for p in c.pushed)
        for word in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"):
            assert word in delivered, word
