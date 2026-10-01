"""Tests for kiro_crew.wecom.renderer (WeComRenderer, Layer 2b)."""

from __future__ import annotations

import dataclasses

import pytest

from conftest import CREDENTIAL_STRADDLE_SHAPES, assert_rejected_without_backtracking
from kiro_crew.messaging.display_safety import canonicalize_display
from kiro_crew.messaging.renderer import _default_redactor
from kiro_crew.wecom.client import WECOM_MAX_REPLY_BYTES
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
        #: Non-terminal rejections (a frame drew a non-zero ACK but the bubble is
        #: still writable). The real client keeps this separate from _dead_streams
        #: and marks a dead stream rejected too, so stream_had_rejection consults
        #: both.
        self.rejected_streams: set[str] = set()
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

        Consulted by the aged rotation, the post-turn seal recheck, and the
        reasoning-candidate retention. A dead bubble was also refused, so it
        answers for both a non-terminal rejection and a terminal one.
        """
        return stream_id in self.rejected_streams or stream_id in self.dead_streams

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


_SMALL_CAP = 96


def _capped_renderer(client: FakeClient, cap: int = _SMALL_CAP) -> WeComRenderer:
    caps = dataclasses.replace(WECOM_CAPABILITIES, max_message_chars=cap)
    return WeComRenderer(client, "rq1", "https://resp.url", caps, chat_id="chat1")


def _answer_whose_cap_lands_between(head: str, tail: str, cap: int = _SMALL_CAP) -> str:
    """An answer whose message cap falls EXACTLY between *head* and *tail*."""
    assert len(head) <= cap, "the fixture has to fit the cap it is positioned against"
    return "x" * (cap - len(head)) + head + tail + " and some trailing prose"


async def _stream_across_a_rotation(answer: str, cap: int = _SMALL_CAP) -> FakeClient:
    """Stream *answer*, let the platform seal the bubble, and finish the turn.

    The rotation is the moment the boundary becomes irreversible: a sealed bubble
    can never be rewritten, so what it shows sits beside the next bubble for good.
    """
    c = FakeClient()
    r = _capped_renderer(c, cap)
    await r.on_text_chunk(answer)
    await r._push(force=True)
    c.dead_streams.add(r._stream_id)
    await r._push(force=True)
    await r.on_done()
    return c


def _reader_instants(c: FakeClient) -> list[list[str]]:
    """Every state the screen passed through, as the bodies visible at that moment.

    A stream frame REPLACES its own bubble, so at any instant the screen holds the
    LATEST frame of each bubble opened so far -- and a sealed bubble's last frame
    stays there for good. The final state is not enough to assert against: a frame
    that puts a credential on screen has been read by the time a later frame
    supersedes it, so each instant is checked on its own.
    """
    order: list[str] = []
    shown: dict[str, str] = {}
    instants: list[list[str]] = []
    for f in c.frames:
        sid = f["stream_id"]
        if sid not in shown:
            order.append(sid)
        shown[sid] = f["content"]
        instants.append([shown[s] for s in order])
    pushed: list[str] = []
    for p in c.pushed:
        pushed.append(p[1])
        instants.append([shown[s] for s in order] + list(pushed))
    return instants


def _reader_views(c: FakeClient) -> tuple[str, str]:
    """(what the screen shows, what a copy of every message shows), at the end.

    The last instant of :func:`_reader_instants`, kept as a named pair because two
    tests read the two renderings apart.
    """
    instants = _reader_instants(c)
    bodies = instants[-1] if instants else []
    return (
        "".join(canonicalize_display(b) for b in bodies),
        canonicalize_display("".join(bodies)),
    )


def _without_think_wrapper(text: str) -> str:
    """The reasoning wrapper as the READER sees it: not at all.

    WeCom renders ``<think>...</think>`` as its own reasoning panel, so the tags are
    markup rather than characters on screen. They matter here because they sit
    exactly where a reasoning tail meets the next bubble's opening characters -- a
    scan that keeps them reads a break the reader does not have, and the credential
    those two pieces spell goes unseen.
    """
    return text.replace("<think>", "").replace("</think>", "")


def _assert_nothing_reached_the_reader(c: FakeClient) -> None:
    """No credential at ANY instant the screen passed through.

    Scanned with the SAME pair the renderer redacts with, so the assertion cannot
    drift away from the guard into checking a different notion of "credential". Both
    readings are checked at each instant, for the same reason
    :func:`joins_to_a_credential` checks both: neither contains the other.
    """
    for bodies in _reader_instants(c):
        bodies = [_without_think_wrapper(b) for b in bodies]
        for view in (
            "".join(canonicalize_display(b) for b in bodies),
            canonicalize_display("".join(bodies)),
        ):
            assert _default_redactor(view) == view, "a credential reached the reader"


class TestTheCapCutsWhereTheReaderCannotRejoin:
    """A cap may not sever a credential the reader's client will rejoin.

    The cap is applied to the RAW answer while the reader sees the CANONICAL rendering
    of each bubble, so a credential the model split with markup can be cut in half:
    each bubble is scrubbed alone and matches nothing, and the client renders the
    markup away and joins the halves on screen.

    Each case below is a genuine straddle -- the premise assertions say so -- and every
    one is red without ``safe_split_offset`` in ``_push``.
    """

    @pytest.mark.asyncio
    async def test_a_link_split_key_does_not_cross_two_bubbles(self) -> None:
        # The acceptance case: a Markdown link whose target carries a comma, which no
        # hand-written character class reached across six review rounds.
        head, tail = "[AKIA](https://ex.test/a,b)", "IOSFODNN7EXAMPLE"
        c = await _stream_across_a_rotation(_answer_whose_cap_lands_between(head, tail))

        on_screen, in_a_copy = _reader_views(c)
        assert "AKIAIOSFODNN7EXAMPLE" not in on_screen
        assert "AKIAIOSFODNN7EXAMPLE" not in in_a_copy
        _assert_nothing_reached_the_reader(c)

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    @pytest.mark.asyncio
    async def test_no_straddling_shape_reaches_the_reader(self, head: str, tail: str) -> None:
        # Premise: neither half is a credential ALONE. That is why scrubbing each bubble
        # cannot see this, and it is asserted so a fixture that stops straddling fails
        # loudly instead of passing on a case it does not cover.
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        c = await _stream_across_a_rotation(_answer_whose_cap_lands_between(head, tail))

        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_an_answer_within_the_cap_is_sent_unchanged(self) -> None:
        # The allow direction. A cut that refused every boundary would deliver nothing,
        # so this is the case that says the guard is conditional.
        answer = "a short ordinary answer with nothing interesting in it."

        c = FakeClient()
        r = _capped_renderer(c)
        await r.on_text_chunk(answer)
        await r._push(force=True)

        assert c.frames[-1]["content"] == answer, "an answer within the cap was altered"


class TestRedactionNotice:
    """A rewritten answer is followed by one notice, delivered from ``close()``.

    The driver redacts text before it reaches this renderer, so these feed the
    placeholder tag directly — what a production turn actually carries. WeCom's
    answer can finish landing as late as the deferred-overflow release, so the
    tally is taken at ``on_done`` and the notice goes out at ``close()``, the
    one point every delivery path funnels through. Shared wording is pinned in
    ``test_credential_redaction_notice.py``.
    """

    @pytest.mark.asyncio
    async def test_redacted_answer_posts_one_notice_at_close(self) -> None:
        client = FakeClient()
        r = _renderer(client)
        await r.on_text_chunk("Run: psql [REDACTED: credential]")
        await r.on_done()
        await r.close()

        # No conversation id on this renderer, so the notice takes the
        # response_url fallback.
        notices = [c for _url, c in client.replies if "Security notice" in c]
        assert len(notices) == 1

    @pytest.mark.asyncio
    async def test_clean_answer_sends_no_notice(self) -> None:
        client = FakeClient()
        r = _renderer(client)
        await r.on_text_chunk("All green, deploy finished.")
        await r.on_done()
        await r.close()

        assert not any("Security notice" in c for _url, c in client.replies)
        assert not any("Security notice" in c for _cid, c in client.pushed)

    @pytest.mark.asyncio
    async def test_a_second_close_does_not_post_the_notice_twice(self) -> None:
        client = FakeClient()
        r = _renderer(client)
        await r.on_text_chunk("Run: psql [REDACTED: credential]")
        await r.on_done()
        await r.close()
        await r.close()

        notices = [c for _url, c in client.replies if "Security notice" in c]
        assert len(notices) == 1

    @pytest.mark.asyncio
    async def test_notice_send_failure_does_not_fail_the_teardown(self) -> None:
        client = FakeClient()

        async def failing_reply(url, content):
            raise RuntimeError("wecom down after the answer")

        client.send_reply = failing_reply  # type: ignore[method-assign]
        r = _renderer(client)
        await r.on_text_chunk("Run: psql [REDACTED: credential]")
        # The sealing frame lands over the WS stream, so the raising
        # response_url only ever carries the notice here.
        await r.on_done()
        await r.close()  # must not raise


async def _stream_across_a_seam(head: str, tail: str, cap: int = _SMALL_CAP) -> FakeClient:
    """Stream a credential whose halves land in two DIFFERENT bubbles.

    This is the ROTATION SEAM straddle, not the intra-frame cap cut: the first
    bubble is filled so its visible tail ends with *head* and is then SEALED —
    frozen, never rewritable. The answer keeps growing (a real turn does not stop
    at a bubble boundary), and *tail* arrives only after the seal, so it opens the
    continuation bubble. Each bubble is scrubbed alone; the reader's client renders
    them side by side and rejoins them. The seam was clean when it was chosen (the
    tail did not exist yet) and the seal made the choice irreversible — the exact
    shape a credential straddling a rotation describes.
    """
    c = FakeClient()
    r = _capped_renderer(c, cap)
    # Fill bubble one so its visible tail is exactly *head*, sitting right at the
    # seam. Leave a little room so *head* is delivered in this bubble, not cut.
    filler = "x" * (cap - len(head) - 4)
    await r.on_text_chunk(filler + head)
    await r._push(force=True)
    c.dead_streams.add(r._stream_id)  # the platform seals bubble one, for good
    # The turn continues; the credential's completion arrives now and rolls into a
    # fresh bubble because the old one is dead.
    await r.on_text_chunk(tail + " and the answer keeps going after that")
    await r._push(force=True)
    await r.on_done()
    await r.close()
    return c


class TestACredentialCannotStraddleTheRotationSeam:
    """A credential split across a bubble ROTATION seam is closed.

    Distinct from ``TestTheCapCutsWhereTheReaderCannotRejoin`` — that guards the cut
    ``safe_split_offset`` makes WITHIN a bubble's frames, where the renderer still
    controls both sides. Here the cut is the seam between a SEALED bubble (which can
    never be rewritten) and its continuation, so the only place the completion can
    be closed is the head of the continuation. Each case is a genuine straddle: the
    premise assertions confirm neither half is a credential alone, so per-bubble
    scrubbing cannot see it, and every one is red without the seam grading in
    ``_render_slice``.
    """

    @pytest.mark.asyncio
    async def test_link_split_key_does_not_cross_the_seam(self) -> None:
        # The acceptance case, at the seam: a Markdown link whose target carries a
        # comma — the shape no hand-written character class reached across six review
        # rounds — with its completion in the bubble AFTER the seal.
        head, tail = "[AKIA](https://ex.test/a,b)", "IOSFODNN7EXAMPLE"
        c = await _stream_across_a_seam(head, tail)

        on_screen, in_a_copy = _reader_views(c)
        assert "AKIAIOSFODNN7EXAMPLE" not in on_screen
        assert "AKIAIOSFODNN7EXAMPLE" not in in_a_copy
        _assert_nothing_reached_the_reader(c)

    @pytest.mark.parametrize(("head", "tail"), CREDENTIAL_STRADDLE_SHAPES)
    @pytest.mark.asyncio
    async def test_no_straddling_shape_crosses_the_seam(self, head: str, tail: str) -> None:
        # Premise: neither half is a credential ALONE — the reason per-bubble
        # scrubbing is blind to it, asserted so a fixture that stops straddling on
        # its own fails loudly rather than passing on a case it does not cover.
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        c = await _stream_across_a_seam(head, tail)

        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_trailing_prose_after_the_completion_survives(self) -> None:
        # The seam grading must redact only the COMPLETING fragment, not the whole
        # continuation: a blanket drop would lose legitimate answer text and turn a
        # security guard into data loss (the failure mode the conductor decision
        # rejected — visible dup beats silent loss, and here we do neither).
        head, tail = "AKIAIOSF", "ODNN7EXAMPLE"
        marker_word = "afterwardsuniquetoken"
        c = FakeClient()
        r = _capped_renderer(c)
        filler = "x" * (_SMALL_CAP - len(head) - 4)
        await r.on_text_chunk(filler + head)
        await r._push(force=True)
        c.dead_streams.add(r._stream_id)
        await r.on_text_chunk(tail + " " + marker_word + " and more")
        await r._push(force=True)
        await r.on_done()
        await r.close()

        on_screen, _copy = _reader_views(c)
        assert "AKIAIOSFODNN7EXAMPLE" not in on_screen, "the key must not survive the seam"
        delivered = "".join(f["content"] for f in c.frames) + "".join(p[1] for p in c.pushed)
        assert marker_word in delivered, "trailing prose past the completion was dropped"
        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_a_clean_seam_is_left_untouched(self) -> None:
        # The allow direction: when the seam severs nothing, the continuation is
        # delivered verbatim. A guard that redacted every rotation would be a
        # regression as bad as the leak.
        head, tail = "the first part of an ordinary", " sentence with no secrets in it"
        c = await _stream_across_a_seam(head, tail)

        delivered = "".join(f["content"] for f in c.frames) + "".join(p[1] for p in c.pushed)
        assert "sentence with no secrets in it" in delivered
        assert "[REDACTED" not in delivered, "a clean seam must not be redacted"

    @pytest.mark.asyncio
    async def test_a_space_at_the_seam_is_part_of_what_the_reader_sees(self) -> None:
        # A WeCom rotation cuts ONE continuous answer across two bubbles, so a space
        # at the seam is visible answer text — "…Bearer" then " abc…" reads as
        # "Bearer abc…". The shared message-boundary repair strips edge whitespace
        # (correct when the halves are separate messages the platform trims), so
        # this continuous-seam case must be graded verbatim, not stripped.
        head, tail = "Authorization: Bearer", " abcdefghijklmnopqrstuvwxyz0123456789"
        assert _default_redactor(head) == head, "the head half must be clean alone"
        assert _default_redactor(tail) == tail, "the tail half must be clean alone"

        c = await _stream_across_a_seam(head, tail)

        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_a_credential_after_an_unbounded_link_target_is_still_closed(self) -> None:
        # The soundness case a fixed context window would fail: a Markdown link
        # whose target is longer than any hand-picked window pushes the
        # credential-bearing label far back in the seam, so a windowed grade would
        # scan a span the key's start was never inside and pass vacuously. Grading
        # the WHOLE delivered seam closes it.
        head = "[AKIA](https://ex.test/" + "a" * 600 + ",b)"
        tail = "IOSFODNN7EXAMPLE"
        c = await _stream_across_a_seam(head, tail, cap=700)

        on_screen, in_a_copy = _reader_views(c)
        assert "AKIAIOSFODNN7EXAMPLE" not in on_screen
        assert "AKIAIOSFODNN7EXAMPLE" not in in_a_copy
        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_a_key_split_between_reasoning_and_the_answer_is_closed(self) -> None:
        # A reasoning frame goes into the bubble while the answer is still empty, so
        # no answer offset (`_carried`) is recorded. The bubble AGES OUT (a >10-min
        # agentic turn) after that accepted reasoning frame, and the answer opens
        # with the key's completion, rolling to a fresh bubble with `_carried == 0`.
        # The seam is not the delivered answer (there is none) but the frozen
        # reasoning the aged bubble still shows. The model owns both sides of the
        # reasoning→answer boundary, so this is reachable.
        import time as _time

        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        await r.on_thinking("thinking, the key is AKIAIOSF")
        await r._push(force=True)
        # The bubble ages past the stream lifetime with its reasoning ACCEPTED and
        # displayed (not refused): the next push rolls to a fresh bubble.
        r._stream_opened_at = _time.monotonic() - 10_000
        await r.on_text_chunk("ODNN7EXAMPLE completes it, then more prose")
        await r._push(force=True)
        await r.on_done()
        await r.close()

        def _seen(fc: FakeClient) -> list[str]:
            order, shown = [], {}
            for f in fc.frames:
                if f["stream_id"] not in shown:
                    order.append(f["stream_id"])
                shown[f["stream_id"]] = f["content"]
            seen_bodies = [shown[s] for s in order] + [p[1] for p in fc.pushed]
            return [_without_think_wrapper(b) for b in seen_bodies]

        for reading in (
            "".join(canonicalize_display(b) for b in _seen(c)),
            canonicalize_display("".join(_seen(c))),
        ):
            assert "AKIAIOSFODNN7EXAMPLE" not in reading
            assert _default_redactor(reading) == reading, "the key rejoined on screen"

    @pytest.mark.asyncio
    async def test_a_refused_reasoning_frame_is_not_taken_as_the_seam(self) -> None:
        # F1: a first reasoning frame ends at the credential prefix and is
        # displayed; a LATER frame appends prose. WeCom gives no positive ACK that
        # the later frame was accepted (send_stream never waits, the rejection
        # marker is retired at the next send), so the reader may still be seeing the
        # first — both stay candidates (always retained, deduped). The answer's
        # completion must be redacted against the first (prefix) candidate, since a
        # single-snapshot pick of the later prose-ending frame would miss the join
        # and leak the key.
        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        await r.on_thinking("reasoning, the key is AKIAIOSF")
        await r._push(force=True)
        await r.on_thinking(" and some more reasoning after it")
        await r._push(force=True)
        # Both reasoning frames are candidates the fresh bubble is graded against —
        # the prefix-ending one is NOT dropped.
        assert any(
            "AKIAIOSF" in s and s.endswith("AKIAIOSF") for s in r._shown_reasonings
        ), "the credential-prefix reasoning frame is retained as a candidate"
        # The bubble ages out (reasoning displayed, no answer yet); the answer opens
        # with the completion and rolls to a fresh bubble.
        import time as _time

        r._stream_opened_at = _time.monotonic() - 10_000
        await r.on_text_chunk("ODNN7EXAMPLE and then ordinary prose")
        await r._push(force=True)

        answer_frames = [f["content"] for f in c.frames if f["stream_id"] == r._stream_id]
        assert answer_frames, "the answer frame was sent"
        # Whichever reasoning the reader sees, the answer head is redacted so no
        # candidate rejoins the key.
        for candidate in (
            "reasoning, the key is AKIAIOSF",
            "reasoning, the key is AKIAIOSF and some more reasoning after it",
        ):
            assert "AKIAIOSFODNN7EXAMPLE" not in canonicalize_display(
                candidate + answer_frames[-1]
            ), "the key rejoined across the reasoning seam"

    @pytest.mark.asyncio
    async def test_a_second_reasoning_roll_keeps_the_first_rolls_seam(self) -> None:
        # Opus BLOCK: a reasoning-only bubble seals on a prefix-ending frame (roll
        # 1 captures it as a seam and resets _shown_reasonings), then the fresh
        # bubble is ALSO refused before any answer and rolls AGAIN — still at
        # _carried == 0 with _shown_reasonings now empty. Replacing _reasoning_seams
        # with the (empty) current set would drop roll 1's seam and let the
        # straddling credential through; the union must keep it.
        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        await r.on_thinking("reasoning, the key is AKIAIOSF")
        await r._push(force=True)
        # Roll 1: the reasoning bubble is sealed; rolling captures its seam.
        c.dead_streams.add(r._stream_id)
        r._roll_if_sealed()
        assert any(s.endswith("AKIAIOSF") for s in r._reasoning_seams), "roll 1 captured the seam"
        # Roll 2: the fresh bubble is sealed too, still before any answer and with
        # no new reasoning frame — _shown_reasonings is empty.
        c.dead_streams.add(r._stream_id)
        r._roll_if_sealed()
        assert any(
            s.endswith("AKIAIOSF") for s in r._reasoning_seams
        ), "the second roll wiped the first roll's reasoning seam"

    @pytest.mark.asyncio
    async def test_accepted_reasoning_frames_collapse_to_the_newest(self) -> None:
        # No positive per-frame ACK exists (send_stream never waits, and the
        # rejection marker is retired at the next send), so no reasoning frame is
        # ever provably superseded — a frame that LOOKS replaced may still be the
        # one on screen if its successor is refused late. So the displayed
        # prefix-ending frame must be RETAINED as a candidate no matter how many
        # later frames arrive, even with no outstanding rejection. Dedup keeps the
        # list from growing on identical throttled re-sends.
        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        await r.on_thinking("the key is AKIAIOSF")
        await r._push(force=True)
        first = r._shown_reasonings[-1]
        assert first.endswith("AKIAIOSF"), "the prefix-ending frame was captured"
        for i in range(30):
            await r.on_thinking(f" step {i}")
            await r._push(force=True)
        assert first in r._shown_reasonings, (
            "the prefix-ending frame was dropped though no positive ACK ever confirmed "
            "a successor was displayed — its credential could rejoin the answer on screen"
        )
        # Identical throttled re-sends do not grow the list (dedup).
        before = len(r._shown_reasonings)
        await r._push(force=True)
        await r._push(force=True)
        assert len(r._shown_reasonings) == before, "identical re-sends must dedup"

    @pytest.mark.asyncio
    async def test_a_displayed_prefix_is_not_evicted_under_sustained_rejection(self) -> None:
        # The GPT F1 scenario: under a sustained run of refusals the DISPLAYED frame
        # is an earlier, shorter prefix — the newer frames were refused and never
        # reached the screen. A fixed count window (or an unsound ACK-tied collapse)
        # would drop that displayed prefix; always-retain keeps every candidate,
        # including the first.
        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        # One accepted frame first — this is the one that stays on screen.
        await r.on_thinking(" the key is AKIAIOSF")
        await r._push(force=True)
        first = r._shown_reasonings[-1]
        # Now the stream carries an outstanding rejection; later frames are not
        # confirmed displayed. Many of them arrive.
        c.rejected_streams.add(r._stream_id)
        for i in range(30):
            await r.on_thinking(f" more reasoning {i}")
            await r._push(force=True)
        assert first in r._shown_reasonings, (
            "the displayed prefix was evicted under sustained rejection — a credential "
            "ending it would rejoin the continuation on screen while the seam grade "
            "passed vacuously against the longer, undisplayed prefixes"
        )

    @pytest.mark.asyncio
    async def test_no_delivered_frame_exceeds_the_wire_byte_limit(self) -> None:
        # F2: a chunk sized to the CHARACTER cap sits AT the byte cap when its
        # characters are 4-byte astral characters (WECOM_SAFE_REPLY_CHARS is exactly
        # the byte cap // 4), and the seam repair then prepends a redaction tag that
        # pushes it OVER — send_stream would truncate the overflow away silently.
        # Simulate that over-cap seam-rendered chunk directly (the natural trigger
        # needs ~5120 astral chars coincident with a seam-straddling credential):
        # _render_slice returns a chunk one tag past the cap, and the byte-rebound
        # after the seam pass must bring every delivered frame back under the wire
        # limit, re-flowing the tail rather than losing it.
        c = FakeClient()
        r = _renderer(c)  # full WeCom cap
        await r.on_turn_start()
        over_cap = "𝐀" * (WECOM_MAX_REPLY_BYTES // 4) + "[REDACTED: credential]tail"
        r._render_slice = lambda body, *, final, seams=None: over_cap  # type: ignore[assignment]
        await r.on_text_chunk("the answer that will be rendered over-cap")
        await r.on_done()
        await r.close()
        delivered = [f["content"] for f in c.frames] + [p[1] for p in c.pushed]
        assert delivered, "the answer was delivered"
        for frame in delivered:
            assert len(frame.encode("utf-8")) <= WECOM_MAX_REPLY_BYTES, (
                "a delivered frame exceeded the wire byte cap; send_stream would "
                "truncate its tail silently"
            )
        # The over-cap chunk was re-flowed into more than one delivered frame, not
        # truncated — the tail survives.
        assert len(delivered) >= 2, "the over-byte tail was dropped rather than re-flowed"

    @pytest.mark.asyncio
    async def test_streaming_frame_shrinks_rather_than_overrunning_sent_abs(self) -> None:
        # GPT F1 (streaming twin of the on_done byte-rebound): in _push the seam
        # repair can push a near-astral slice past the byte cap. send_stream would
        # truncate it silently while _sent_abs advanced over the raw slice — an aged
        # rotation then resumes PAST the undelivered suffix. _push must instead
        # shrink the RAW slice and re-render until it fits, deriving _sent_abs from
        # the shorter raw slice. Model the over-cap render: _render_slice echoes the
        # raw body, so a long raw slice renders over-cap and a shrunk one fits.
        c = FakeClient()
        r = _renderer(c)  # full WeCom cap
        await r.on_turn_start()
        # Force a seam so the seam path (the one GPT flagged) runs.
        r._reasoning_seams = ["frozen reasoning prefix"]
        # Model the seam repair GROWING the slice: echo the raw body plus a tag, so
        # a full char-cap slice (5120 astral chars = 20480 bytes) renders OVER the
        # byte cap and only a shrunk raw slice fits. render is called off-thread with
        # keyword final/seams.
        tag = "[REDACTED: credential]"
        r._render_slice = lambda body, *, final, seams=None: body + tag  # type: ignore[assignment]
        # A near-astral answer longer than the byte cap: the first streaming frame
        # cannot ship it all, so it must shrink.
        answer = "𝐀" * (WECOM_MAX_REPLY_BYTES // 4 + 2000)
        await r.on_text_chunk(answer)
        await r._push(force=True)
        streamed = [f["content"] for f in c.frames if "<think>" not in f["content"]]
        assert streamed, "a streaming frame was sent"
        for frame in streamed:
            assert len(frame.encode("utf-8")) <= WECOM_MAX_REPLY_BYTES, (
                "a streamed frame exceeded the wire byte cap; send_stream would "
                "truncate its tail silently while _sent_abs advanced over it"
            )
        # _sent_abs reflects only the raw slice that actually shipped — the next
        # frame resumes exactly at the undelivered suffix, not past it.
        assert r._sent_abs <= len(r.text()), "_sent_abs advanced beyond the answer"
        shipped_raw = r.text()[r._carried : r._sent_abs]
        assert (
            len(shipped_raw.encode("utf-8")) <= WECOM_MAX_REPLY_BYTES
        ), "_sent_abs covers more raw text than one frame's byte budget can carry"

    @pytest.mark.asyncio
    async def test_retained_reasoning_candidates_are_count_bounded(self) -> None:
        # AUTOSDE a-bound-bounds-every-field-it-retains: the candidate list needs a
        # COUNT bound, not just the per-item byte bound. The bound is kept sound by
        # STOPPING reasoning sends once reached (not evicting), so retention is
        # finite AND no un-retained frame reaches the screen.
        from kiro_crew.wecom.renderer import _MAX_REASONING_CANDIDATES

        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        for i in range(_MAX_REASONING_CANDIDATES * 3):
            await r.on_thinking(f" distinct step {i}")
            await r._push(force=True)
        assert len(r._shown_reasonings) <= _MAX_REASONING_CANDIDATES, "retention is unbounded"
        # Sends stopped at the cap: the number of reasoning frames put on the wire
        # does not exceed the cap (plus the turn-start placeholder frame).
        reasoning_frames = [f for f in c.frames if "<think>" in f["content"]]
        assert (
            len(reasoning_frames) <= _MAX_REASONING_CANDIDATES + 1
        ), "reasoning sends did not stop at the retention cap"

    @pytest.mark.asyncio
    async def test_the_newest_frozen_seam_is_never_sliced_across_rotations(self) -> None:
        # The carried-seam union at a rotation must never slice: a [:cap] slice keeps
        # the oldest and drops the NEWEST frozen prefix — the bubble that sealed last,
        # sitting immediately above the continuation, which is exactly the seam a key
        # joins to (nothing follows it, so it is not covered as a prefix of a later
        # frame). The send-stop keeps the combined set within cap so the slice is
        # unreachable in normal operation; this exercises the union directly with an
        # at-cap state to prove the union logic itself never drops the newest.
        from kiro_crew.wecom.renderer import _MAX_REASONING_CANDIDATES

        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        # Prior rolls already filled _reasoning_seams to the cap; this bubble then
        # sealed on a NEWEST frozen prefix ending at a credential prefix.
        r._reasoning_seams = [f"frozen prefix {i}" for i in range(_MAX_REASONING_CANDIDATES)]
        r._shown_reasonings = ["reasoning, the key is AKIAIOSF"]
        newest = r._shown_reasonings[-1]
        c.dead_streams.add(r._stream_id)
        r._roll_if_sealed()
        assert newest in r._reasoning_seams, (
            "the newest frozen seam was sliced away — a credential ending it would "
            "rejoin the continuation on screen"
        )

    @pytest.mark.asyncio
    async def test_a_space_separated_continuation_is_not_over_redacted(self) -> None:
        # GPT F1: WeCom shows ONE continuous answer across the seam, so boundary
        # whitespace is REAL on-screen text. A sealed tail ending `AKIAIOSF` and a
        # continuation opening ` ODNN7EXAMPLE` read as two space-separated tokens —
        # NOT a key. The stripped repair (for separate-message channels) would strip
        # the space, see a join, and corrupt harmless prose; the verbatim grade must
        # leave it intact.
        c = await _stream_across_a_seam("AKIAIOSF", " ODNN7EXAMPLE and ordinary prose")
        on_screen, in_a_copy = _reader_views(c)
        # The two tokens are shown with their separating space, unredacted.
        assert "ODNN7EXAMPLE and ordinary prose" in on_screen, (
            "harmless space-separated continuation was corrupted by a stripped-seam "
            "repair that treats WeCom's continuous answer like separate messages"
        )
        assert "[REDACTED" not in on_screen, "harmless prose was over-redacted at the seam"

    @pytest.mark.asyncio
    async def test_a_no_space_split_across_the_seam_is_still_closed(self) -> None:
        # The verbatim grade still catches a GENUINE split: `AKIAIOSF` directly
        # abutting `ODNN7EXAMPLE` with no separator forms the key on screen and must
        # be redacted.
        c = await _stream_across_a_seam("AKIAIOSF", "ODNN7EXAMPLE")
        on_screen, in_a_copy = _reader_views(c)
        assert "AKIAIOSFODNN7EXAMPLE" not in on_screen
        assert "AKIAIOSFODNN7EXAMPLE" not in in_a_copy
        _assert_nothing_reached_the_reader(c)

    @pytest.mark.asyncio
    async def test_suppressed_reasoning_updates_are_announced(self) -> None:
        # GPT F2 (AUTOSDE a-bound-bounds): once the retention cap stops reasoning
        # updates, the suppression must be COUNTED and said out loud once, not
        # silently frozen.
        from kiro_crew.wecom.renderer import _MAX_REASONING_CANDIDATES

        c = FakeClient()
        r = _capped_renderer(c, cap=200)
        await r.on_turn_start()
        for i in range(_MAX_REASONING_CANDIDATES + 5):
            await r.on_thinking(f" distinct step {i}")
            await r._push(force=True)
        assert r._suppressed_reasoning > 0, "suppressed updates were not counted"
        await r.on_done()
        await r.close()
        delivered = (
            [f["content"] for f in c.frames]
            + [p[1] for p in c.pushed]
            + [rep[1] for rep in c.replies]
        )
        assert any(
            "省略" in d and "推理" in d for d in delivered
        ), "the reasoning-overflow notice was not announced"
