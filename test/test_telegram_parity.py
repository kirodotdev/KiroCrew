"""Telegram's command and capability parity with the Slack channel.

Covers what this channel gained: the commands a user can now reach from chat, the
outbound image upload, the reasoning post, the stall marks on the live bubble, the
reaction allow-list, the durable getUpdates cursor — and, first, the credential
that Telegram's own markdown→HTML conversion can REASSEMBLE after the
byte-level redactor had already looked at it.

The doubles come from ``test_telegram`` so there is one FakeClient, not two that
drift.

This module keeps the rendering and delivery rows: the display-form redaction, outbound
images and the refusals an upload names, the reasoning post, the stall marks, the reaction
allow-list, the durable getUpdates cursor and the multipart upload shape. The commands
named above live in ``test_telegram_parity_commands.py``, session trust and the approval
prompt in ``test_telegram_parity_approvals.py``, the privacy modes in
``test_telegram_parity_privacy.py``, what a turn sends back in
``test_telegram_parity_turns.py``, and what the polling loop takes in and which messages
reach a turn in ``test_telegram_parity_inbound.py``; the shared messages, renderer and
fixture in ``telegram_parity_helpers.py``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _AWS_KEY,
    _drain_offset_writes,
    _drop_live_config_snapshot,
    _renderer,
)
from test_telegram import _dispatcher

from conftest import host_abs
from kiro_crew.messaging.outbound_files import OutboundFile
from kiro_crew.telegram.client import (
    REACTION_EMOJI,
    TelegramClient,
    TelegramInbound,
    normalize_reaction_emoji,
)
from kiro_crew.telegram.renderer import _display_safe, _utf16_chunks, _utf16_cut, _utf16_len

#: The authorized upload root. ``authorize_upload_root`` keeps only an ABSOLUTE
#: root (a string gate; extraction is faked below), and from Python 3.13
#: ``ntpath.isabs("/tmp")`` is False, so the POSIX literal left uploads disabled
#: on Windows and every "the picture must be uploaded" assertion failed there.
_UPLOAD_ROOT = host_abs("tmp")


def _png(name: str = "chart.png") -> OutboundFile:
    return OutboundFile(
        path=f"/tmp/{name}", data=b"\x89PNG\r\n\x1a\n", alt="chart", mime="image/png"
    )


# ---------------------------------------------------------------------------
# The security row: a credential markup reassembles after byte-level redaction
# ---------------------------------------------------------------------------


class TestDisplayFormRedaction:
    """``redact_credentials`` sees BYTES; Telegram's reader sees the RENDER."""

    @pytest.mark.parametrize(
        "markup",
        [
            # Bold splits the key, so the byte pattern never matches — and
            # _md_to_telegram_html then emits <b> tags Telegram hides.
            f"key {_AWS_KEY[:4]}**{_AWS_KEY[4:]}** done",
            # A link does the same through a different rule.
            f"tok [{_AWS_KEY[:4]}](https://x.example.com){_AWS_KEY[4:]} end",
            # Italic, single-underscore form.
            f"id {_AWS_KEY[:4]}__{_AWS_KEY[4:]}__ x",
            # No markup at all: a zero-width joiner between the halves.
            f"raw {_AWS_KEY[:4]}​{_AWS_KEY[4:]} x",
        ],
    )
    def test_a_render_reassembled_credential_is_redacted(self, markup: str) -> None:
        from kiro_crew.security import redact_credentials

        # The premise: the byte-level pass leaves the markup untouched, so the
        # halves are still there for the platform to rejoin.
        assert redact_credentials(markup)[0] == markup
        # The guarantee, asserted POSITIVELY. "the contiguous key is absent" is
        # true of the input too — it was never contiguous — so it holds with the
        # redactor deleted. What must be true is that the redactor FIRED and the
        # surviving text carries neither half of the key.
        out = _display_safe(markup)
        assert "[REDACTED" in out
        assert _AWS_KEY[4:] not in out and _AWS_KEY not in out

    @pytest.mark.asyncio
    async def test_the_seal_redacts_before_it_renders_to_html(self) -> None:
        renderer, client = _renderer()
        renderer._buf = [f"key {_AWS_KEY[:4]}**{_AWS_KEY[4:]}** done"]
        await renderer.on_done()
        landed = "".join(text for text, _ in client.sent) + "".join(
            text for _, text, _ in client.edits
        )
        assert _AWS_KEY not in landed
        # And the tag pair that would have rebuilt it is gone with it.
        assert f"{_AWS_KEY[:4]}<b>" not in landed

    @pytest.mark.asyncio
    async def test_a_live_frame_redacts_too(self) -> None:
        # _strip_md removes the ** on the live plaintext frame, which reassembles
        # the key just as surely as the HTML path does.
        renderer, client = _renderer()
        renderer._last_edit = -1e9  # defeat the throttle deterministically
        await renderer.on_text_chunk(f"key {_AWS_KEY[:4]}**{_AWS_KEY[4:]}** done")
        landed = "".join(text for text, _ in client.sent)
        assert landed and _AWS_KEY not in landed

    @pytest.mark.asyncio
    async def test_a_heading_secret_is_redacted_before_the_plaintext_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import redact_credentials

        payload = "SecretAccessKey\n# : wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        redacted, warnings = redact_credentials(payload)
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in redacted
        assert warnings == ["Redacted bare secret key (40 chars)"]
        renderer, client = _renderer()
        original_send = client.send_message

        async def reject_html(chat_id, text, **kwargs):
            if kwargs.get("parse_mode") == "HTML":
                return None
            return await original_send(chat_id, text, **kwargs)

        monkeypatch.setattr(client, "send_message", reject_html)
        renderer._buf = [payload]
        await renderer.on_done()

        landed = "".join(text for text, _ in client.sent)
        assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in landed
        assert "[REDACTED" in landed

    @pytest.mark.asyncio
    async def test_a_heading_cannot_reveal_a_named_short_secret_in_the_plaintext_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.security import redact_credentials

        secret = "short-secret-value"
        payload = f"SecretAccessKey\n# : {secret}"
        assert redact_credentials(payload)[0] == payload
        renderer, client = _renderer()
        original_send = client.send_message

        async def reject_html(chat_id, text, **kwargs):
            if kwargs.get("parse_mode") == "HTML":
                return None
            return await original_send(chat_id, text, **kwargs)

        monkeypatch.setattr(client, "send_message", reject_html)
        renderer._buf = [payload]
        await renderer.on_done()

        landed = "".join(text for text, _ in client.sent)
        assert secret not in landed
        assert "[REDACTED" in landed

    @pytest.mark.asyncio
    async def test_the_canonical_scan_screens_the_heading_join_without_the_telegram_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # With the Telegram-specific heading pass removed, the always-on canonical
        # display-form scan still screens this join: it collapses a line-leading
        # ``# `` marker (``_HEADING_MARKER``) too, so ``SecretAccessKey`` above and
        # ``# : <value>`` below read as one credential on screen and are redacted
        # before delivery. The screen is defence-in-depth, not the one Telegram
        # pass alone.
        from kiro_crew.messaging import display_safety

        heading = display_safety.TELEGRAM_FALLBACK_HEADING
        monkeypatch.setattr(
            display_safety,
            "TELEGRAM_FALLBACK_PASSES",
            tuple(
                (pattern, replacement)
                for pattern, replacement in display_safety.TELEGRAM_FALLBACK_PASSES
                if pattern is not heading
            ),
        )
        secret = "short-secret-value"
        payload = f"SecretAccessKey\n# : {secret}"
        renderer, client = _renderer()
        original_send = client.send_message

        async def reject_html(chat_id, text, **kwargs):
            if kwargs.get("parse_mode") == "HTML":
                return None
            return await original_send(chat_id, text, **kwargs)

        monkeypatch.setattr(client, "send_message", reject_html)
        renderer._buf = [payload]
        await renderer.on_done()

        landed = "".join(text for text, _ in client.sent)
        assert secret not in landed
        assert "[REDACTED" in landed

    @pytest.mark.asyncio
    async def test_the_rich_table_path_redacts_too(self) -> None:
        renderer, client = _renderer()
        renderer._buf = [f"| a | b |\n| --- | --- |\n| {_AWS_KEY[:4]}**{_AWS_KEY[4:]}** | y |"]
        await renderer.on_done()
        assert client.rich_sent, "a conforming table must take the rich path"
        # Positive again: the rich payload is raw markdown, so the halves would
        # survive verbatim without the redactor.
        payload = client.rich_sent[0][0]
        assert "[REDACTED" in payload
        assert _AWS_KEY[4:] not in payload

    def test_ordinary_text_is_untouched(self) -> None:
        # The trade is formatting for a rendered secret; text with no secret
        # keeps every character.
        clean = "**bold** and [a link](https://example.com) and `code`"
        assert _display_safe(clean) == clean

    @pytest.mark.asyncio
    async def test_a_command_reply_redacts_before_it_renders_to_html(self) -> None:
        # The renderer's two sinks are not the only ones: the dispatcher renders
        # markdown too, for the shared command replies. Those carry text an LLM
        # wrote (a cron job's name, a task's title), so this leg is a redaction
        # sink for the same reason and by the same mechanism.
        d, client, _ = _dispatcher({7})
        await d._reply_markdown(7, f"*Your cron jobs:* {_AWS_KEY[:4]}**{_AWS_KEY[4:]}**")

        landed = "".join(text for text, _ in client.sent)
        assert "[REDACTED" in landed, "the redactor must have fired, not merely not-matched"
        assert _AWS_KEY not in landed and _AWS_KEY[4:] not in landed
        # The tag pair that would have rebuilt it is gone with it. The reply's own
        # bold goes too: canonicalizing to the rendered form is what finds the key,
        # and that is the documented trade this leg now shares with the renderer's.
        assert f"{_AWS_KEY[:4]}<b>" not in landed

    @pytest.mark.asyncio
    async def test_a_clean_command_reply_still_renders_its_markdown(self) -> None:
        # The other half of the trade: redacting at this sink must not cost the
        # rendering it exists for. Without this, the leg could "pass" the test
        # above by never rendering at all.
        d, client, _ = _dispatcher({7})
        await d._reply_markdown(7, "*Your cron jobs:* `nightly`")

        landed = "".join(text for text, _ in client.sent)
        # Emphasis, as a tag rather than as literal asterisks. `<i>` and not `<b>`
        # because the shared replies are written in Slack's grammar, where a single
        # `*` is bold, and this translator reads it as markdown's italic. The
        # heading is still emphasized, which is what the leg is for; unifying the
        # two grammars would change every other Telegram render.
        assert "<i>Your cron jobs:</i>" in landed
        assert "<code>nightly</code>" in landed

    def test_the_raw_translator_has_no_sink_outside_the_renderer(self) -> None:
        """The pairing is enforced by keeping the translator private, not by memory.

        ``_md_to_telegram_html`` is the vector ``_display_safe`` closes, so a call
        site that reaches it directly is a sink that skipped the redaction. The
        renderer itself is exempt: it redacts at its own boundaries and its
        ``_html_len``/split budget MEASURE the render, where redacting would size a
        segment against text that is not what gets sent.
        """
        import ast

        from source_corpus import parsed_candidates
        from source_corpus import src_root as corpus_root

        src_root = corpus_root()
        offenders: list[str] = []
        # A sink spells the translator's name, so only the files whose text holds it
        # are parsed.
        for path, _text, tree in parsed_candidates(
            require_any=("_md_to_telegram_html",), skip_syntax_errors=False
        ):
            if path.name == "renderer.py" and path.parent.name == "telegram":
                continue
            for node in ast.walk(tree):
                name = ""
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                elif isinstance(node, ast.ImportFrom):
                    name = next(
                        (a.name for a in node.names if a.name == "_md_to_telegram_html"), ""
                    )
                if name == "_md_to_telegram_html":
                    offenders.append(f"{path.relative_to(src_root)}:{node.lineno}")
        assert not offenders, (
            "these reach the raw markdown translator instead of "
            f"md_to_telegram_html_safe: {offenders}"
        )


# ---------------------------------------------------------------------------
# Outbound images
# ---------------------------------------------------------------------------


class TestOutboundImages:
    @pytest.mark.asyncio
    async def test_an_image_ships_as_an_attachment_after_the_text(self) -> None:
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("Here is the chart.", [_png()])
        )
        renderer._buf = ["Here is the chart. ![c](/tmp/chart.png)"]
        await renderer.on_done()
        assert client.media_sent, "the picture must be uploaded"
        files, thread, silent = client.media_sent[0]
        assert [f.path for f in files] == ["/tmp/chart.png"]
        # The answer bubble already pinged; the picture must not ping again.
        assert silent is True

    @pytest.mark.asyncio
    async def test_uploads_are_off_without_an_absolute_root(self) -> None:
        renderer, client = _renderer()
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("x", [_png()])
        )
        for root in ("", "relative/dir", None, 7):
            renderer.authorize_upload_root(root)  # type: ignore[arg-type]
            assert renderer._uploads_enabled() is False
        renderer._buf = ["Here is the chart. ![c](/tmp/chart.png)"]
        await renderer.on_done()
        assert client.media_sent == []
        # The honest degradation: the path stays visible, never silently dropped.
        assert "/tmp/chart.png" in "".join(text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_a_restricted_session_keeps_uploads_off(self) -> None:
        renderer, _ = _renderer(uploads_allowed=False)
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        assert renderer._uploads_enabled() is False

    @pytest.mark.asyncio
    async def test_a_failed_upload_names_the_picture_without_repeating_the_answer(
        self,
    ) -> None:
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        client.media_fails = True
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("Here it is.", [_png()])
        )
        renderer._buf = ["Here it is. ![c](/tmp/chart.png)"]
        await renderer.on_done()
        landed = [text for text, _ in client.sent]
        recovery = [text for text in landed if "Couldn't upload" in text]
        assert len(recovery) == 1 and "/tmp/chart.png" in recovery[0]
        # The text bubble has already landed, so recovery must not repeat it —
        # this is where re-posting the whole source doubled the answer.
        assert sum(text.count("Here it is.") for text in landed) == 1

    @pytest.mark.asyncio
    async def test_a_failed_upload_keeps_a_concealed_credential_redacted(self) -> None:
        # Markup that hid a secret loses its formatting rather than its
        # redaction: that is the documented direction of the trade.
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        client.media_fails = True
        leaky = OutboundFile(
            path="/tmp/chart.png",
            data=b"\x89PNG\r\n\x1a\n",
            alt=f"{_AWS_KEY[:4]}**{_AWS_KEY[4:]}**",
            mime="image/png",
        )
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("Here it is.", [leaky])
        )
        renderer._buf = ["Here it is."]
        await renderer.on_done()
        assert _AWS_KEY not in "".join(text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_recovery_of_many_failed_uploads_drops_no_reference(self) -> None:
        # A truncated bubble must not keep only what fits under the cap: with
        # enough failed images, every reference past it vanished silently.
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        client.media_fails = True
        files = [_png(f"chart-{i:03d}.png") for i in range(200)]
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("Here they are.", files)
        )
        renderer._buf = ["Here they are."]
        await renderer.on_done()
        landed = "\n".join(text for text, _ in client.sent)
        for item in files:
            assert item.path in landed, f"recovery dropped {item.path}"
        # Every recovery bubble stays within the channel budget, measured in
        # Telegram's unit.
        for text, _ in client.sent:
            assert _utf16_len(text) <= renderer._limit()

    @pytest.mark.asyncio
    async def test_recovery_respects_telegram_utf16_budget(self) -> None:
        # Telegram's cap counts UTF-16 code units; the old slice counted code
        # points. An astral char costs 2 units, so emoji-dense alt text passed
        # the slice while overflowing the real limit, and the send bounced.
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        client.media_fails = True
        dense = OutboundFile(
            path="/tmp/chart.png",
            data=b"\x89PNG\r\n\x1a\n",
            alt="🚀" * 3000,
            mime="image/png",
        )
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("Here.", [dense])
        )
        renderer._buf = ["Here."]
        await renderer.on_done()
        assert client.sent, "recovery bubbles must land"
        for text, _ in client.sent:
            assert _utf16_len(text) <= renderer._limit(), "bubble exceeds the API cap"
        # Chunked, not truncated: no part of the alt text was dropped.
        assert sum(text.count("🚀") for text, _ in client.sent) == 3000

    def test_utf16_cut_floor_prevents_zero_progress(self) -> None:
        # limit=1 with a leading astral char would otherwise cut at index 0
        # forever; the floor of 2 guarantees any single char makes progress.
        assert _utf16_cut("🚀abc", 1) >= 1
        chunks = _utf16_chunks("🚀" * 5, 1)
        assert "".join(chunks) == "🚀" * 5
        assert all(_utf16_len(chunk) <= 2 for chunk in chunks)

    def test_utf16_chunks_pack_lines_and_lose_nothing(self) -> None:
        text = "\n".join(f"![a](/tmp/{i}.png)" for i in range(40))
        chunks = _utf16_chunks(text, 100)
        assert len(chunks) > 1, "must actually split for this to test packing"
        assert all(_utf16_len(chunk) <= 100 for chunk in chunks)
        # Newline-boundary packing: reassembled lines are exactly the input
        # lines — nothing dropped, no line bisected.
        lines = [line for chunk in chunks for line in chunk.split("\n")]
        assert lines == text.split("\n")

    @pytest.mark.asyncio
    async def test_a_length_rotation_holds_a_STRADDLING_reference(self) -> None:
        # The reference has to straddle the cut the splitter would actually take,
        # or the test passes with the guard removed: a reference that already sits
        # in the trailing chunk survives either way. With no newline to prefer,
        # _split_text hard-cuts at the render budget, so the markup is placed
        # across that offset on purpose.
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        ref = "![c](/tmp/chart.png)"
        cut = renderer._rendered_limit()
        renderer._buf = ["A" * (cut - 10) + ref]
        await renderer._rotate_on_length()
        held = "".join(renderer._buf)
        assert ref in held, "the whole reference must stay in the live tail"
        # And no sealed message may carry a fragment of it.
        sealed = "".join(text for text, _ in client.sent) + "".join(
            text for _, text, _ in client.edits
        )
        assert "![c](/tmp/ch" not in sealed and "art.png)" not in sealed

    @pytest.mark.asyncio
    async def test_live_frames_hide_the_markup_so_no_path_flashes(self) -> None:
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        renderer._last_edit = -1e9
        await renderer.on_text_chunk("Look: ![c](/tmp/secret-dir/chart.png)")
        assert "/tmp/secret-dir/chart.png" not in "".join(text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_an_image_only_reply_leaves_no_transient_footer_above_it(self) -> None:
        # Extraction consumes the whole body, so there is no text to seal — but a
        # live bubble may already carry a "🔧 {tool}…" footer or a stall mark, and
        # leaving it makes that transient frame the turn's FINAL text message,
        # sitting above the picture forever.
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        renderer._buf = ["![c](/tmp/chart.png)"]
        renderer._last_edit = -1e9
        await renderer.on_tool_call("t1", "render_chart")
        live = renderer._stream_mid
        assert live is not None and "render_chart" in client.sent[-1][0]
        renderer._extract_uploads = AsyncMock(  # type: ignore[method-assign]
            return_value=("", [_png()])
        )
        await renderer.on_done()
        assert live in client.deleted, "the transient footer bubble must be retired"
        assert client.media_sent, "and the picture must still ship"

    @pytest.mark.asyncio
    async def test_an_extraction_failure_costs_the_picture_not_the_answer(self) -> None:
        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        renderer._buf = ["The answer. ![c](/tmp/chart.png)"]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "kiro_crew.telegram.renderer.extract_local_refs_off_loop",
                AsyncMock(side_effect=OSError("disk gone")),
            )
            await renderer.on_done()
        assert "The answer." in "".join(text for text, _ in client.sent) + "".join(
            text for _, text, _ in client.edits
        )


# ---------------------------------------------------------------------------
# Reasoning
# ---------------------------------------------------------------------------


class TestUploadRejections:
    """Every refusal is surfaced: a picture that vanishes with no explanation is
    the defect the extractor's rejection list exists to prevent."""

    def _rejection(self, reason: str = "not_a_raster"):
        from kiro_crew.messaging.outbound_files import Rejection

        return Rejection(dest="/tmp/x.png", reason=reason, detail="")

    def test_each_refusal_is_named_in_the_answer(self) -> None:
        renderer, _ = _renderer()
        out = renderer._append_rejections("The answer.", [self._rejection()])
        assert out.startswith("The answer.")
        assert "⚠️" in out and out != "The answer."

    def test_past_the_line_budget_the_rest_collapse_into_a_tally(self) -> None:
        renderer, _ = _renderer()
        out = renderer._append_rejections("A.", [self._rejection() for _ in range(7)])
        assert out.count("⚠️") == 4  # 3 named + 1 tally
        assert "and 4 more" in out

    def test_the_note_is_dropped_rather_than_pushing_the_answer_over_budget(self) -> None:
        # The answer is what the user asked for; a refusal note that displaces it
        # would trade the content for the explanation.
        renderer, _ = _renderer()
        body = "A" * renderer._limit()
        assert renderer._append_rejections(body, [self._rejection()]) == body

    @pytest.mark.asyncio
    async def test_a_refused_image_keeps_its_markup_and_says_why(self) -> None:
        from kiro_crew.messaging.outbound_files import ExtractResult

        renderer, client = _renderer()
        renderer.authorize_upload_root(_UPLOAD_ROOT)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "kiro_crew.telegram.renderer.extract_local_refs_off_loop",
                AsyncMock(
                    return_value=ExtractResult(
                        rewritten_text="See it. ![c](/tmp/chart.png)",
                        files=[],
                        rejections=[self._rejection()],
                    )
                ),
            )
            renderer._buf = ["See it. ![c](/tmp/chart.png)"]
            await renderer.on_done()
        landed = "".join(t for t, _ in client.sent) + "".join(t for _, t, _ in client.edits)
        assert "/tmp/chart.png" in landed and "⚠️" in landed
        assert client.media_sent == []


class TestThinking:
    @pytest.mark.asyncio
    async def test_reasoning_is_dropped_when_the_operator_has_not_opted_in(self) -> None:
        renderer, client = _renderer(show_thinking=False)
        await renderer.on_thinking("because of X")
        renderer._buf = ["The answer."]
        await renderer.on_done()
        assert "because of X" not in "".join(text for text, _ in client.sent)

    @pytest.mark.asyncio
    async def test_reasoning_posts_once_as_an_expandable_quote_after_the_answer(self) -> None:
        renderer, client = _renderer(show_thinking=True)
        await renderer.on_thinking("first, ")
        await renderer.on_thinking("then second")
        renderer._buf = ["The answer."]
        await renderer.on_done()
        quotes = [t for t, _ in client.sent if "blockquote expandable" in t]
        assert len(quotes) == 1
        assert "first, then second" in quotes[0]
        # Silent: the answer already notified.
        assert client.send_silent[-1] is True

    @pytest.mark.asyncio
    async def test_reasoning_is_redacted_against_the_rendered_form(self) -> None:
        renderer, client = _renderer(show_thinking=True)
        await renderer.on_thinking(f"key {_AWS_KEY[:4]}**{_AWS_KEY[4:]}**")
        renderer._buf = ["ok"]
        await renderer.on_done()
        assert _AWS_KEY not in "".join(t for t, _ in client.sent)

    @pytest.mark.asyncio
    async def test_empty_reasoning_posts_nothing(self) -> None:
        renderer, client = _renderer(show_thinking=True)
        await renderer.on_thinking("   ")
        renderer._buf = ["ok"]
        await renderer.on_done()
        assert not [t for t, _ in client.sent if "blockquote" in t]

    @pytest.mark.asyncio
    async def test_a_silent_turn_still_publishes_its_reasoning(self) -> None:
        # Earlier segments carried the turn, so on_done posts no tail message —
        # the reasoning must not be lost with it.
        renderer, client = _renderer(show_thinking=True)
        renderer._seal_count = 1
        await renderer.on_thinking("thought")
        renderer._buf = []
        await renderer.on_done()
        assert [t for t, _ in client.sent if "blockquote expandable" in t]


# ---------------------------------------------------------------------------
# Stall marks
# ---------------------------------------------------------------------------


class TestStallMarks:
    @pytest.mark.parametrize(
        "idle,expect",
        [(0.0, ""), (14.9, ""), (15.0, "🥱"), (44.9, "🥱"), (45.0, "😨"), (600.0, "😨")],
    )
    def test_the_mark_is_read_from_the_clock_not_latched(
        self, monkeypatch: pytest.MonkeyPatch, idle: float, expect: str
    ) -> None:
        # The clock is pinned rather than sampled twice: 14.9 against a 15.0
        # threshold leaves 100 ms of real elapsed time to flip the verdict.
        renderer, _ = _renderer()
        renderer._last_progress = 1000.0
        monkeypatch.setattr("kiro_crew.telegram.renderer.time.monotonic", lambda: 1000.0 + idle)
        mark = renderer._stall_mark()
        assert (expect in mark) if expect else (mark == "")

    @pytest.mark.asyncio
    async def test_progress_clears_a_mark_that_was_showing(self) -> None:
        import time as _time

        renderer, _ = _renderer()
        renderer._last_progress = _time.monotonic() - 60
        assert renderer._stall_mark() != ""
        await renderer.on_text_chunk("a token")
        assert renderer._stall_mark() == ""

    @pytest.mark.asyncio
    async def test_a_tool_footer_outranks_the_stall_mark(self) -> None:
        # on_tool_call RESETS the stall clock as its first statement, so driving
        # this through on_tool_call can never observe the precedence — the mark is
        # already "" by the time the footer is chosen. Set both states directly and
        # render one frame.
        import time as _time

        renderer, client = _renderer()
        renderer._buf = ["working"]
        renderer._tool = "grep"
        renderer._last_progress = _time.monotonic() - 60
        assert renderer._stall_mark() != "", "the stall state must be live"
        renderer._last_edit = -1e9
        await renderer._stream_live()
        frame = ([t for t, _ in client.sent] + [t for _, t, _ in client.edits])[-1]
        # Naming what is happening beats reporting that nothing is.
        assert "grep" in frame and "🥱" not in frame

    @pytest.mark.asyncio
    async def test_a_late_frame_cannot_overwrite_a_sealed_message(self) -> None:
        # The typing loop publishes from its OWN task, so a frame computed before
        # a seal could edit the message the seal had just finalized — replacing
        # the formatted answer with a stale plaintext draft. The seal retires the
        # live id, so the worst a late frame can do is post a new bubble.
        import time as _time

        renderer, client = _renderer()
        renderer._last_edit = -1e9
        renderer._buf = ["the formatted answer"]
        await renderer._stream_live()
        sealed_mid = renderer._stream_mid
        assert sealed_mid is not None
        await renderer.on_done()
        assert renderer._stream_mid is None

        # Now let a stall frame run as the typing tick would.
        renderer._closed = False
        renderer._last_edit = -1e9
        renderer._last_progress = _time.monotonic() - 60
        renderer._buf = ["a later token"]
        await renderer._stream_live()
        assert all(
            mid != sealed_mid for mid, _text, _kb in client.edits[1:]
        ), "no edit after the seal may target the sealed message"

    @pytest.mark.asyncio
    async def test_the_mark_never_reaches_a_sealed_message(self) -> None:
        import time as _time

        renderer, client = _renderer()
        renderer._last_progress = _time.monotonic() - 60
        renderer._last_edit = -1e9
        renderer._buf = ["the answer"]
        await renderer._stream_live()
        await renderer.on_done()
        final = ([t for _, t, _ in client.edits] or [t for t, _ in client.sent])[-1]
        assert "🥱" not in final and "😨" not in final


# ---------------------------------------------------------------------------
# The reaction allow-list
# ---------------------------------------------------------------------------


class TestReactionAllowList:
    def test_the_set_is_exactly_the_documented_seventy_three(self) -> None:
        assert len(REACTION_EMOJI) == 73

    @pytest.mark.parametrize("emoji", ["🫡", "👀", "🤔", "👨‍💻", "🥱", "😨", "👍", "⚡"])
    def test_every_emoji_this_channel_uses_is_on_the_list(self, emoji: str) -> None:
        assert normalize_reaction_emoji(emoji) in REACTION_EMOJI

    @pytest.mark.parametrize("emoji", ["✅", "🚀", "⏳", "🤖", "🌐", "🔧", "🦞"])
    def test_the_plausible_status_marks_that_are_NOT_on_the_list(self, emoji: str) -> None:
        # Each of these reads like an obvious progress mark and is a hard 400.
        assert normalize_reaction_emoji(emoji) not in REACTION_EMOJI

    @pytest.mark.parametrize("emoji", ["❤️", "🕊️", "✍️", "☃️", "🤷‍♂️", "❤️‍🔥"])
    def test_the_variation_selector_forms_a_keyboard_emits_are_accepted(self, emoji: str) -> None:
        # Seven members are documented WITHOUT U+FE0F while every keyboard adds
        # it, and the two major Python libraries disagree about which. Membership
        # is tested on the stripped form so neither spelling is rejected.
        assert "️" in emoji
        assert normalize_reaction_emoji(emoji) in REACTION_EMOJI

    @pytest.mark.asyncio
    async def test_an_off_list_emoji_is_refused_here_not_at_the_wire(self) -> None:
        client = TelegramClient(token="t:1")
        calls: list[Any] = []
        client._api = AsyncMock(side_effect=lambda *a, **k: calls.append(a))  # type: ignore[method-assign]
        assert await client.set_message_reaction(1, 2, "✅") is False
        assert calls == [], "an off-list emoji must cost no round-trip"

    @pytest.mark.asyncio
    async def test_an_allowed_emoji_is_sent_in_its_documented_spelling(self) -> None:
        client = TelegramClient(token="t:1")
        seen: list[Any] = []

        async def _api(method: str, params: dict, *a: Any, **k: Any) -> Any:
            seen.append((method, params))
            return {}

        client._api = _api  # type: ignore[method-assign]
        assert await client.set_message_reaction(1, 2, "❤️") is True
        assert seen[0][1]["reaction"] == [{"type": "emoji", "emoji": "❤"}]


# ---------------------------------------------------------------------------
# The durable getUpdates cursor
# ---------------------------------------------------------------------------


class TestOffsetDurability:
    """The persisted cursor is a LOW-water mark, not what the poll observed.

    Two failure modes have to be avoided at once. Persisting what was observed
    loses a message: the process can die between the poll and the turn finishing,
    and the restart resumes past an update nobody handled, so the user sees no
    answer and no error. Persisting only what COMPLETED replays forever, because an
    update the gateway deliberately never turns into a turn (a sticker, an
    unauthorized sender) has no completion to wait for.
    """

    @pytest.mark.asyncio
    async def test_observing_a_batch_does_not_persist_it(self, tmp_path: Any) -> None:
        # The poll advances the in-memory cursor because the next getUpdates call
        # needs it, and writes nothing: the batch has not been dispatched yet, so a
        # crash here must replay it.
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._api = AsyncMock(return_value=[{"update_id": 7}, {"update_id": 9}])  # type: ignore[method-assign]
        await client._get_updates()
        assert client._offset == 10
        assert not path.exists(), "an observed-but-undispatched batch must not be acked"

    @pytest.mark.asyncio
    async def test_an_in_flight_turn_holds_the_cursor_behind_it(self, tmp_path: Any) -> None:
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._offset = 10
        client._in_flight = {7, 9}
        assert client._persistable_offset() == 7, "the cursor must hold at the OLDEST"
        client._resolve_updates((7,))
        assert client._persistable_offset() == 9
        client._resolve_updates((9,))
        assert client._persistable_offset() == 10, "nothing in flight means fully acked"

    @pytest.mark.asyncio
    async def test_a_finished_turn_persists_and_a_crash_would_replay_the_rest(
        self, tmp_path: Any
    ) -> None:
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._offset = 10
        client._in_flight = {7, 9}
        client._resolve_updates((7,))
        await _drain_offset_writes(client)
        assert (
            json.loads(path.read_text(encoding="utf-8"))["offset"] == 9
        ), "update 9 is still running, so a restart must resume AT it"

    @pytest.mark.asyncio
    async def test_a_raising_handler_still_resolves_its_update(self, tmp_path: Any) -> None:
        # A turn that crashed will crash again on replay, so holding the cursor on it
        # would wedge every later message behind it forever.
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)

        async def _boom(_inbound: Any) -> None:
            raise RuntimeError("handler exploded")

        client._on_message = _boom  # type: ignore[assignment]
        client._offset = 8
        client._in_flight = {7}
        await client._invoke_message(
            TelegramInbound(chat_id=1, user_id=1, text="x", message_id=1), (7,)
        )
        assert client._in_flight == set()
        assert client._persistable_offset() == 8

    @pytest.mark.asyncio
    async def test_an_update_with_no_handler_is_resolved_not_held(self, tmp_path: Any) -> None:
        # With no handler installed there is nothing this update could ever become.
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._on_message = None
        client._offset = 8
        client._in_flight = {7}
        await client._invoke_message(
            TelegramInbound(chat_id=1, user_id=1, text="x", message_id=1), (7,)
        )
        assert client._in_flight == set()

    @pytest.mark.asyncio
    async def test_a_button_press_holds_the_cursor_until_it_resolves(self, tmp_path: Any) -> None:
        # A press is an approval or an option choice, so losing one leaves a turn
        # waiting on a decision that never arrives.
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._offset = 8
        seen: list[Any] = []

        async def _cb(c: Any) -> None:
            seen.append(c)
            assert client._persistable_offset() == 7, "held while the press is running"

        client._on_callback = _cb  # type: ignore[assignment]
        client._in_flight = {7}
        await client._invoke_callback(
            SimpleNamespace(callback_query_id="q", data="a:1:1"),  # type: ignore[arg-type]
            (7,),
        )
        assert seen and client._in_flight == set()
        assert client._persistable_offset() == 8

    @pytest.mark.asyncio
    async def test_an_emptied_album_group_does_not_pin_the_cursor_forever(
        self, tmp_path: Any
    ) -> None:
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._offset = 12
        client._in_flight = {11}
        client._album_updates["c:g"] = [11]
        # No members: the group was evicted or double-flushed. Its ids still have to
        # be released, or the cursor never advances again.
        client._flush_album("c:g")
        assert client._in_flight == set()
        assert client._persistable_offset() == 12

    @pytest.mark.asyncio
    async def test_start_resumes_the_persisted_cursor(self, tmp_path: Any) -> None:
        path = tmp_path / "offset.json"
        path.write_text(json.dumps({"bot_id": "t", "offset": 55}), encoding="utf-8")
        client = TelegramClient(token="t:1", offset_path=path)
        # Stub the wire before start(): start() creates the polling task, and this
        # test's hermeticity must not rest on close() winning a race with its first
        # step.
        client._api = AsyncMock(return_value=[])  # type: ignore[method-assign]
        try:
            await client.start()
            assert client._offset == 55
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_an_idle_poll_writes_nothing(self, tmp_path: Any) -> None:
        path = tmp_path / "offset.json"
        client = TelegramClient(token="t:1", offset_path=path)
        client._api = AsyncMock(return_value=[])  # type: ignore[method-assign]
        await client._get_updates()
        assert not path.exists()

    @pytest.mark.parametrize(
        "raw",
        [
            '{"bot_id": "t", "offset": -1}',
            '{"bot_id": "t", "offset": true}',
            '{"bot_id": "t", "offset": "9"}',
            # A cursor from a DIFFERENT bot: update_id sequences are per-bot, so
            # applying it would either skip everything below a higher foreign
            # offset or replay from a lower one.
            '{"bot_id": "other", "offset": 500}',
            # An older file with no recorded bot — we cannot tell whose it is.
            '{"offset": 500}',
            "[]",
            "not json",
            "",
        ],
    )
    def test_every_unusable_cursor_reads_as_zero(self, tmp_path: Any, raw: str) -> None:
        # Zero is exactly the pre-persistence behaviour: a bounded replay the
        # operator can see, never a wrong cursor that silently skips messages.
        path = tmp_path / "offset.json"
        path.write_text(raw, encoding="utf-8")
        assert TelegramClient(token="t:1", offset_path=path)._load_offset() == 0

    def test_an_absent_cursor_reads_as_zero(self, tmp_path: Any) -> None:
        client = TelegramClient(token="t:1", offset_path=tmp_path / "none.json")
        assert client._load_offset() == 0

    def test_an_unwritable_home_does_not_stop_delivery(self, tmp_path: Any) -> None:
        # A read-only or full data home costs one replay window, not the channel.
        client = TelegramClient(token="t:1", offset_path=tmp_path / "f" / "offset.json")
        client._offset_path.parent.write_text("i am a file", encoding="utf-8")
        client._save_offset(3)  # must not raise


# ---------------------------------------------------------------------------
# Multipart upload shape
# ---------------------------------------------------------------------------


class TestMultipartShape:
    @pytest.mark.asyncio
    async def test_one_image_takes_sendPhoto(self) -> None:
        client = TelegramClient(token="t:1")
        seen: list[Any] = []

        async def _mp(method: str, params: dict, files: Any, **kw: Any) -> Any:
            seen.append((method, params, list(files), kw["field_names"]))
            return {"message_id": 5}

        client._api_multipart = _mp  # type: ignore[method-assign]
        assert await client.send_media_group(1, [_png()]) == [5]
        assert seen[0][0] == "sendPhoto" and seen[0][3] == ["photo"]

    @pytest.mark.asyncio
    async def test_several_images_take_sendMediaGroup_with_matching_descriptors(self) -> None:
        client = TelegramClient(token="t:1")
        seen: list[Any] = []

        async def _mp(method: str, params: dict, files: Any, **kw: Any) -> Any:
            seen.append((method, params, list(files), kw["field_names"]))
            return [{"message_id": 5}, {"message_id": 6}]

        client._api_multipart = _mp  # type: ignore[method-assign]
        assert await client.send_media_group(1, [_png("a.png"), _png("b.png")]) == [5, 6]
        method, params, files, names = seen[0]
        assert method == "sendMediaGroup"
        # Every descriptor must name a part that is actually in the body.
        attached = [item["media"].removeprefix("attach://") for item in params["media"]]
        assert attached == names and len(files) == len(names)

    @pytest.mark.asyncio
    async def test_an_empty_set_sends_nothing(self) -> None:
        client = TelegramClient(token="t:1")
        client._api_multipart = AsyncMock()  # type: ignore[method-assign]
        assert await client.send_media_group(1, []) == []
        client._api_multipart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_document_takes_sendDocument_with_its_real_name(self) -> None:
        client = TelegramClient(token="t:1")
        seen: list[Any] = []

        async def _mp(method: str, params: dict, files: Any, **kw: Any) -> Any:
            seen.append((method, params, list(files), kw["field_names"], kw.get("filenames")))
            return {"message_id": 9}

        client._api_multipart = _mp  # type: ignore[method-assign]
        doc = OutboundFile(
            path="/tmp/box/report.pdf", data=b"%PDF-1.4", alt="", mime="application/pdf"
        )
        mid = await client.send_document(7, doc, caption="x" * 2000, message_thread_id=3)
        assert mid == 9
        method, params, files, names, filenames = seen[0]
        assert method == "sendDocument" and names == ["document"]
        # The real filename is pinned: the multipart sanitizer is aimed at
        # LLM-authored reference paths, and rewriting this already-gated name's
        # extension would break the receiver's file-type association.
        assert filenames == ["report.pdf"]
        assert params["chat_id"] == 7 and params["message_thread_id"] == 3
        # Caption capped at Telegram's 1024; the send stays silent (the text
        # bubble for the same turn already pinged).
        assert len(params["caption"]) == 1024
        assert params["disable_notification"] is True
        assert files == [doc]

    @pytest.mark.asyncio
    async def test_the_transport_document_verb_converts_ids_and_returns_str(self) -> None:
        # The endpoint hands the transport a str conversation id off a
        # ChannelLink; the Bot API wants ints. The verb owns that conversion,
        # like send_message beside it.
        from kiro_crew.telegram.transport import TelegramTransport

        client = TelegramClient(token="t:1")
        seen: list[Any] = []

        async def _send_document(chat_id: int, document: Any, **kw: Any) -> int:
            seen.append((chat_id, document, kw))
            return 44

        client.send_document = _send_document  # type: ignore[method-assign]
        transport = TelegramTransport(client)
        doc = _png("report.png")
        mid = await transport.send_document("42", doc, caption="here", thread_id="7")
        assert mid == "44"
        chat_id, document, kw = seen[0]
        assert chat_id == 42 and document is doc
        assert kw["caption"] == "here" and kw["message_thread_id"] == 7

    @pytest.mark.asyncio
    async def test_the_album_cap_bounds_one_call(self) -> None:
        client = TelegramClient(token="t:1")
        seen: list[int] = []

        async def _mp(method: str, params: dict, files: Any, **kw: Any) -> Any:
            seen.append(len(list(files)))
            return []

        client._api_multipart = _mp  # type: ignore[method-assign]
        await client.send_media_group(1, [_png(f"{i}.png") for i in range(25)])
        assert seen == [10]

    @pytest.mark.asyncio
    async def test_a_malformed_result_is_not_read_as_success(self) -> None:
        client = TelegramClient(token="t:1")
        client._api_multipart = AsyncMock(return_value=None)  # type: ignore[method-assign]
        assert await client.send_media_group(1, [_png("a.png"), _png("b.png")]) == []

    @pytest.mark.asyncio
    async def test_the_body_is_rebuilt_per_attempt(self) -> None:
        # An aiohttp form is consumed as it is written; replaying one sends an
        # empty body, which Telegram answers with a 400 that reads like a
        # payload-shape bug.
        client = TelegramClient(token="t:1")
        built: list[int] = []

        async def _request(method: str, body: Any, *a: Any, **k: Any) -> Any:
            body()
            body()
            built.append(2)
            return {"message_id": 1}

        client._api_request = _request  # type: ignore[method-assign]
        await client.send_photo(1, _png())
        assert built == [2]
