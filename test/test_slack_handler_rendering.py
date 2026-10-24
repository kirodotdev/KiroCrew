"""Slack handler tests: how a reply renders and streams to Slack.

Message splitting, mrkdwn conversion, footers, tool timers, task cards and condensed
thinking.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager

from conftest import MockSlackClient
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.format import CONTINUATION, SLACK_MSG_LIMIT, split_message
from kiro_crew.slack.handler import _build_phase_emojis, _condense_thinking, handle_message


class TestSplitMessage:
    """Tests for split_message — splitting long text into Slack-safe chunks."""

    def test_short_text_returns_single_part(self):
        text = "hello world"
        assert split_message(text) == [text]

    def test_text_at_exact_limit_returns_single_part(self):
        text = "x" * SLACK_MSG_LIMIT
        assert split_message(text) == [text]

    def test_text_over_limit_splits_into_two(self):
        text = "a" * (SLACK_MSG_LIMIT + 100)
        parts = split_message(text)
        assert len(parts) == 2
        assert parts[0].endswith(CONTINUATION)
        assert not parts[1].endswith(CONTINUATION)

    def test_splits_at_newline_boundary(self):
        # Build text with a newline near the limit so it splits cleanly there
        line_a = "a" * (SLACK_MSG_LIMIT - len(CONTINUATION) - 50)
        line_b = "b" * 200
        text = line_a + "\n" + line_b
        parts = split_message(text)
        assert len(parts) == 2
        assert parts[0] == line_a + CONTINUATION
        assert parts[1] == line_b

    def test_hard_cut_when_no_newline(self):
        text = "x" * (SLACK_MSG_LIMIT + 500)  # no newlines at all
        parts = split_message(text)
        assert len(parts) == 2
        chunk_limit = SLACK_MSG_LIMIT - len(CONTINUATION)
        assert parts[0] == "x" * chunk_limit + CONTINUATION
        assert parts[1] == "x" * (SLACK_MSG_LIMIT + 500 - chunk_limit)

    def test_very_long_text_produces_multiple_parts(self):
        text = "x" * (SLACK_MSG_LIMIT * 3)
        parts = split_message(text)
        assert len(parts) >= 3
        # All non-final parts have continuation marker
        for part in parts[:-1]:
            assert part.endswith(CONTINUATION)
        # Final part does not
        assert not parts[-1].endswith(CONTINUATION)

    def test_all_parts_within_limit(self):
        text = "word " * 2000  # ~10000 chars with newline-free content
        parts = split_message(text)
        for part in parts:
            assert len(part) <= SLACK_MSG_LIMIT

    def test_empty_string_returns_single_part(self):
        assert split_message("") == [""]

    def _split_with_timeout(self, text, limit, seconds=5.0):
        """Run split_message in a daemon thread; return (parts, timed_out).

        Guards against the historical infinite loop: when ``limit`` was <= the
        length of the continuation marker, ``chunk_limit`` went <= 0, ``cut``
        became 0, the remainder never shrank, and the loop ran forever. We use a
        bounded join instead of relying on the global pytest timeout so the
        failure is a clear assertion rather than a 120s hang.
        """
        import threading

        box = {}

        def run():
            box["parts"] = split_message(text, limit)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=seconds)
        return box.get("parts"), t.is_alive()

    def test_limit_equal_to_continuation_len_terminates(self):
        # limit == len(CONTINUATION) drove chunk_limit to 0 -> infinite loop.
        parts, timed_out = self._split_with_timeout("hello world " * 5, len(CONTINUATION))
        assert not timed_out, "split_message did not terminate when limit == len(CONTINUATION)"
        assert parts is not None

    def test_tiny_and_zero_limits_terminate(self):
        for limit in (0, 1, 5, len(CONTINUATION) - 1, len(CONTINUATION) + 1):
            parts, timed_out = self._split_with_timeout("the quick brown fox " * 4, limit)
            assert not timed_out, f"split_message hung for limit={limit}"
            assert parts is not None

    def test_small_limit_preserves_all_content(self):
        # Even with a tiny limit the function must not silently drop characters.
        text = "alpha beta gamma delta epsilon"
        parts, timed_out = self._split_with_timeout(text, 5)
        assert not timed_out
        rejoined = "".join(
            p[: -len(CONTINUATION)] if p.endswith(CONTINUATION) else p for p in parts
        )
        # Non-whitespace content is preserved in order (newlines/markers aside).
        assert "".join(text.split()) == "".join(rejoined.split())

    def test_normal_limits_still_terminate_quickly(self):
        # Guard: the fix must not regress the common path.
        parts, timed_out = self._split_with_timeout("x " * 5000, SLACK_MSG_LIMIT)
        assert not timed_out
        assert len(parts) >= 2

    def test_no_continuation_when_remainder_is_only_newlines(self):
        # Remainder after cut is only newlines — should not get CONTINUATION marker
        chunk_limit = SLACK_MSG_LIMIT - len(CONTINUATION)
        text = "a" * chunk_limit + "\n" * 20
        parts = split_message(text)
        assert len(parts) == 1
        assert not parts[-1].endswith(CONTINUATION)


class TestCronMessageSplitting:
    """Tests for cron message splitting — long cron output sent as multiple messages."""

    @pytest.mark.asyncio
    async def test_short_cron_result_sends_single_block_message(self):
        """Short cron output posts one Block Kit message with ack button."""
        from kiro_crew.slack.format import build_cron_ack_block, to_slack_mrkdwn
        from kiro_crew.slack.gateway import _CRON_MSG_LIMIT

        slack = MockSlackClient()
        result_text = "All systems healthy."
        post_text = f"⏰ *Cron: health-check*\n\n{to_slack_mrkdwn(result_text)}"
        parts = split_message(post_text, limit=_CRON_MSG_LIMIT)

        assert len(parts) == 1

        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": parts[0]}},
        ] + build_cron_ack_block("job-1")
        await slack.post_blocks("C1", blocks, parts[0])

        assert len(slack.actions) == 1
        assert slack.actions[0][0] == "blocks"
        assert "health-check" in slack.actions[0][1]["text"]

    @pytest.mark.asyncio
    async def test_long_cron_result_splits_into_multiple_messages(self):
        """Long cron output splits: first as Block Kit, overflow as threaded messages."""
        from kiro_crew.slack.format import build_cron_ack_block, to_slack_mrkdwn
        from kiro_crew.slack.gateway import _CRON_MSG_LIMIT

        slack = MockSlackClient()
        # Generate text that exceeds the 3000-char Block Kit section limit
        result_text = "line of text\n" * 500  # ~6500 chars
        post_text = f"⏰ *Cron: big-report*\n\n{to_slack_mrkdwn(result_text)}"
        parts = split_message(post_text, limit=_CRON_MSG_LIMIT)

        assert len(parts) >= 2

        # First part: Block Kit with ack button
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": parts[0]}},
        ] + build_cron_ack_block("job-2")
        parent_ts = await slack.post_blocks("C1", blocks, parts[0])
        # Overflow parts: threaded under the first message
        for part in parts[1:]:
            await slack.post_message("C1", part, parent_ts)

        total_messages = len(slack.actions)
        assert total_messages == len(parts)
        # First message is Block Kit
        assert slack.actions[0][0] == "blocks"
        assert any(b.get("type") == "actions" for b in slack.actions[0][1]["blocks"])
        # Remaining messages are plain text threaded under the first
        for action in slack.actions[1:]:
            assert action[0] == "post"
            assert action[1]["thread_ts"] == parent_ts

    @pytest.mark.asyncio
    async def test_all_cron_parts_within_block_kit_limit(self):
        """Every split part fits within the Block Kit section text limit."""
        from kiro_crew.slack.format import to_slack_mrkdwn
        from kiro_crew.slack.gateway import _CRON_MSG_LIMIT

        result_text = "x" * 10000
        post_text = f"⏰ *Cron: stress*\n\n{to_slack_mrkdwn(result_text)}"
        parts = split_message(post_text, limit=_CRON_MSG_LIMIT)

        for part in parts:
            assert len(part) <= _CRON_MSG_LIMIT

    @pytest.mark.asyncio
    async def test_cron_split_preserves_full_content(self):
        """All original content is present across the split parts (no data loss)."""
        from kiro_crew.slack.format import to_slack_mrkdwn
        from kiro_crew.slack.gateway import _CRON_MSG_LIMIT

        result_text = "unique_token_abc\n" * 400
        post_text = f"⏰ *Cron: check*\n\n{to_slack_mrkdwn(result_text)}"
        parts = split_message(post_text, limit=_CRON_MSG_LIMIT)

        # Strip continuation markers and rejoin
        joined = "".join(p.replace(CONTINUATION, "") for p in parts)
        assert "unique_token_abc" in joined
        assert joined.count("unique_token_abc") == post_text.count("unique_token_abc")


class TestStreamingAPI:
    """Tests for the Slack streaming API path (startStream/appendStream/stopStream)."""

    def _streaming_client(self):
        c = MockSlackClient()
        c._stream_enabled = True
        return c

    @pytest.mark.asyncio
    async def test_uses_start_stream(self):
        """When streaming is available, start_stream is called instead of post_message for initial."""
        slack = self._streaming_client()
        sessions = FakeSessionManager()
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        starts = [a for a in slack.actions if a[0] == "start_stream"]
        assert len(starts) == 1
        assert starts[0][1]["text"] is None

    @pytest.mark.asyncio
    async def test_stop_stream_with_final_text(self):
        """stop_stream is called with the final formatted text."""
        slack = self._streaming_client()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="hello world")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        stops = [a for a in slack.actions if a[0] == "stop_stream"]
        assert len(stops) == 1
        assert "hello world" in stops[0][1]["text"]

    @pytest.mark.asyncio
    async def test_no_update_message_on_streaming_path(self):
        """After streaming, chat.update must NOT fire — stop_stream preserves the rich AI renderer."""
        slack = self._streaming_client()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="streamed")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        updates = [a for a in slack.actions if a[0] == "update"]
        assert len(updates) == 0

    @pytest.mark.asyncio
    async def test_tool_call_appended_to_stream(self):
        """Tool call status is appended via append_task."""
        slack = self._streaming_client()
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "read", None, "msg1", "U1")

        tasks = [a for a in slack.actions if a[0] == "append_task"]
        assert any("Read File" in a[1]["title"] for a in tasks)

    @pytest.mark.asyncio
    async def test_fallback_when_stream_unavailable(self):
        """When start_stream returns None, falls back to post+update."""
        slack = MockSlackClient()  # _stream_enabled defaults to False
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="fallback")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        posts = [a for a in slack.actions if a[0] == "post"]
        assert any("Thinking" in p[1]["text"] for p in posts)
        stops = [a for a in slack.actions if a[0] == "stop_stream"]
        assert len(stops) == 0


# ── Reaction emoji config override tests ──


class TestReactionOverrides:
    """Tests for _build_phase_emojis."""

    def test_defaults_without_overrides(self):
        result, unknown = _build_phase_emojis({})
        assert result["done"] == "lobster"
        assert result["queued"] == "eyes"
        assert unknown == []

    def test_override_applies(self):
        result, unknown = _build_phase_emojis({"done": "sparkle"})
        assert result["done"] == "sparkle"
        assert result["queued"] == "eyes"  # others unchanged
        assert unknown == []

    def test_unknown_key_returned(self):
        result, unknown = _build_phase_emojis({"bogus": "emoji"})
        assert "bogus" in unknown
        assert "bogus" not in result


class TestContextFooter:
    """Tests for context usage percentage in the timing footer."""

    # ── Helper ──

    async def _get_footer(self, pct_value=None, pct_side_effect=None):
        """Run handle_message and return the last blocks call's text + blocks."""
        slack = MockSlackClient()
        provider = FakeProvider()
        if pct_side_effect is not None:
            provider.context_usage_pct = pct_side_effect
        elif pct_value is not None:
            provider.context_usage_pct = lambda: pct_value
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")
        blocks_calls = [a for a in slack.actions if a[0] == "blocks"]
        assert blocks_calls, "Expected at least one post_blocks call"
        last = blocks_calls[-1][1]
        return last["text"], last["blocks"]

    # ── Threshold boundary tests ──

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("pct", "icon", "shown"),
        [
            pytest.param(0.0, "🟢", "ctx 0%", id="test_green_at_zero"),
            # 29.9 rounds to 30 → 🟡 (not green). Icon and display are consistent.
            pytest.param(29.9, "🟡", "ctx 30%", id="test_boundary_29_rounds_to_yellow"),
            pytest.param(29.4, "🟢", "ctx 29%", id="test_green_at_29_exact"),
            pytest.param(30.0, "🟡", "ctx 30%", id="test_yellow_at_exactly_30"),
            # 49.9 rounds to 50 → 🟠. Icon and display are consistent.
            pytest.param(49.9, "🟠", "ctx 50%", id="test_boundary_49_rounds_to_orange"),
            pytest.param(49.4, "🟡", "ctx 49%", id="test_yellow_at_49_exact"),
            pytest.param(50.0, "🟠", "ctx 50%", id="test_orange_at_exactly_50"),
            # 69.9 rounds to 70 → 🔴. Icon and display are consistent.
            pytest.param(69.9, "🔴", "ctx 70%", id="test_boundary_69_rounds_to_red"),
            pytest.param(69.4, "🟠", "ctx 69%", id="test_orange_at_69_exact"),
            pytest.param(70.0, "🔴", "ctx 70%", id="test_red_at_exactly_70"),
            pytest.param(99.0, "🔴", "ctx 99%", id="test_red_at_99"),
            pytest.param(100.0, "🔴", "ctx 100%", id="test_red_at_100"),
        ],
    )
    async def test_threshold_icon_and_display(self, pct, icon, shown):
        text, _ = await self._get_footer(pct)
        assert icon in text
        assert shown in text

    # ── Format and structure ──

    @pytest.mark.asyncio
    async def test_footer_format_structure(self):
        """Footer text should match 'Finished in Xs · ICON ctx NN%'."""
        text, blocks = await self._get_footer(42.0)
        assert text.startswith("Finished in ")
        assert " · " in text
        assert "ctx 42%" in text
        # Block structure: context block with mrkdwn element
        assert blocks[0]["type"] == "context"
        assert blocks[0]["elements"][0]["type"] == "mrkdwn"
        assert blocks[0]["elements"][0]["text"] == text

    @pytest.mark.asyncio
    async def test_fallback_text_matches_blocks(self):
        """The fallback text arg to post_blocks should equal the block text."""
        slack = MockSlackClient()
        provider = FakeProvider()
        provider.context_usage_pct = lambda: 55.0
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")
        blocks_calls = [a for a in slack.actions if a[0] == "blocks"]
        last = blocks_calls[-1][1]
        block_text = last["blocks"][0]["elements"][0]["text"]
        assert last["text"] == block_text

    @pytest.mark.asyncio
    async def test_pct_rounded_no_decimals(self):
        """Percentage should be displayed as integer, no decimal point."""
        text, _ = await self._get_footer(42.7)
        assert "ctx 43%" in text
        assert "." not in text.split("ctx")[1]
        # round() returns int, so no format specifier needed
        assert "🟡" in text

    # ── Error / fallback paths ──

    @pytest.mark.asyncio
    async def test_fallback_on_runtime_error(self):
        def _raise():
            raise RuntimeError("no data")

        text, _ = await self._get_footer(pct_side_effect=_raise)
        assert "Finished in" in text
        assert "ctx" not in text

    @pytest.mark.asyncio
    async def test_fallback_on_attribute_error(self):
        def _raise():
            raise AttributeError("missing method")

        text, _ = await self._get_footer(pct_side_effect=_raise)
        assert "Finished in" in text
        assert "ctx" not in text

    @pytest.mark.asyncio
    async def test_fallback_on_type_error(self):
        """Provider returning None would cause TypeError in formatting."""
        text, _ = await self._get_footer(pct_side_effect=lambda: None)
        # None >= 70 raises TypeError; should fall back gracefully
        assert "Finished in" in text

    @pytest.mark.asyncio
    async def test_fallback_still_has_duration(self):
        """Even on error, the duration portion must be present."""

        def _raise():
            raise RuntimeError("boom")

        text, _ = await self._get_footer(pct_side_effect=_raise)
        assert text.startswith("Finished in ")
        assert "s" in text  # duration always ends with 's'
        assert "ctx" not in text


class TestToSlackMrkdwnTruncation:
    """The >SLACK_MAX_TEXT truncation used `rfind("\\n") or SLACK_MAX_TEXT`,
    which misreads rfind's int return: -1 (no newline) is truthy so text[:-1]
    keeps ~39000 chars and Slack rejects the message; a newline at index 0
    falls through to the cap. The result must always be safely under the limit."""

    def test_no_newline_long_text_truncates_under_limit(self):
        from kiro_crew.slack.format import SLACK_MAX_TEXT, to_slack_mrkdwn

        # A single long line with NO newline (minified JSON / base64 / long URL).
        text = "x" * (SLACK_MAX_TEXT + 5000)
        out = to_slack_mrkdwn(text)
        # Pre-fix: rfind -> -1, text[:-1] leaves ~SLACK_MAX_TEXT+4999 chars.
        assert len(out) < SLACK_MAX_TEXT + 200  # body cut to cap + short notice
        assert "truncated" in out

    def test_leading_newline_does_not_truncate_to_empty(self):
        from kiro_crew.slack.format import SLACK_MAX_TEXT, to_slack_mrkdwn

        # Newline only at index 0 of the window: rfind -> 0, `0 or CAP` -> CAP
        # pre-fix (accidentally correct), but the explicit check keeps it robust.
        text = "\n" + "y" * (SLACK_MAX_TEXT + 1000)
        out = to_slack_mrkdwn(text)
        assert len(out) < SLACK_MAX_TEXT + 200
        assert "truncated" in out

    def test_short_text_is_untouched(self):
        from kiro_crew.slack.format import to_slack_mrkdwn

        assert to_slack_mrkdwn("hello world") == "hello world"


class TestToSlackMrkdwnKeepTables:
    """Tests for the keep_tables parameter in to_slack_mrkdwn."""

    TABLE = "| Model | Cost |\n" "|-------|------|\n" "| GPT-4 | $30  |\n" "| Claude | $15 |"

    def test_tables_converted_by_default(self):
        from kiro_crew.slack.format import to_slack_mrkdwn

        result = to_slack_mrkdwn(self.TABLE)
        assert "| Model | Cost |" not in result
        assert "•" in result

    def test_tables_preserved_with_keep_tables(self):
        from kiro_crew.slack.format import to_slack_mrkdwn

        result = to_slack_mrkdwn(self.TABLE, keep_tables=True)
        assert "| Model | Cost |" in result
        assert "•" not in result

    def test_keep_tables_still_converts_headings(self):
        from kiro_crew.slack.format import to_slack_mrkdwn

        text = "## Heading\n\n" + self.TABLE
        result = to_slack_mrkdwn(text, keep_tables=True)
        assert "*Heading*" in result
        assert "| Model | Cost |" in result

    def test_keep_tables_still_converts_mermaid(self):
        from kiro_crew.slack.format import to_slack_mrkdwn

        text = self.TABLE + "\n\n```mermaid\ngraph TD\nA[Start] --> B[End]\n```"
        result = to_slack_mrkdwn(text, keep_tables=True)
        assert "| Model | Cost |" in result
        assert "```mermaid" not in result


class TestToSlackMrkdwnImages:
    """Markdown IMAGE syntax must survive conversion untouched.

    Slack mrkdwn has no image form, so running an image through the
    ``[text](url)`` -> ``<url|text>`` rewrite emits a stray ``!`` in front of a
    clickable link to a destination Slack cannot open (a local path is not even
    reachable from Slack's servers). Raw passthrough is what the reader can act
    on, and it matches Discord, whose renderer rewrites no links at all.
    """

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            pytest.param(
                "![shot](/tmp/x.png)",
                "![shot](/tmp/x.png)",
                id="test_image_with_local_path_is_untouched",
            ),
            pytest.param(
                "![diagram](https://example.com/d.png)",
                "![diagram](https://example.com/d.png)",
                id="test_image_with_http_url_is_untouched",
            ),
            pytest.param(
                "[docs](https://example.com)",
                "<https://example.com|docs>",
                id="test_regular_link_still_converts",
            ),
            pytest.param(
                "see ![a](/x.png) and [b](https://y)",
                "see ![a](/x.png) and <https://y|b>",
                id="test_mixed_line_converts_only_the_link",
            ),
            # CommonMark reads ``\![x](p)`` as an escaped literal ``!`` followed by a
            # LINK, so a strict reader would convert it. This converter has no
            # backslash-unescaping layer, so honouring that in detection alone would
            # emit ``\!<p|x>`` -- a stray literal backslash in place of a stray ``!``.
            # Passing it through raw also matches ``image_artifacts._IMAGE_MD_RE``,
            # which registers ``\![x](p)`` as an image on the dashboard side.
            pytest.param(
                r"\![x](p)", r"\![x](p)", id="test_escaped_bang_is_still_treated_as_an_image"
            ),
            # No space between ``!`` and ``[``: CommonMark binds the ``!`` into image
            # syntax, so this is an image, not "exclamation mark, then link".
            pytest.param(
                "Wow![click](https://x)",
                "Wow![click](https://x)",
                id="test_sentence_bang_before_a_link_is_an_image_per_commonmark",
            ),
            pytest.param(
                "```\n![a](/x.png)\n```",
                "```\n![a](/x.png)\n```",
                id="test_image_inside_a_code_fence_is_untouched",
            ),
        ],
    )
    def test_image_passthrough_and_link_conversion(self, text, expected):
        from kiro_crew.slack.format import to_slack_mrkdwn

        assert to_slack_mrkdwn(text) == expected


class TestMermaidSequenceArrows:
    """Regression: dashed sequence-diagram arrows were rendered by a dead branch.

    In ``_mermaid_sequence`` the dashed-arrow line was::

        arrow = "⇠" if ">>" in arrow_type else "⇠"   # both branches identical

    so (1) a dashed reply ``-->>`` and a dashed open ``-->`` rendered identically
    (the message-type distinction Mermaid encodes was silently lost), and (2) both
    pointed LEFT (``⇠``) while ``src``/``dst`` are laid out left-to-right, so the
    arrow pointed back at the source — the opposite direction from the solid arrows
    ``->>`` (``→``) and ``->`` (``⇢``) on the same diagram.
    """

    @staticmethod
    def _seq(line: str) -> str:
        from kiro_crew.slack.format import _mermaid_sequence

        # _mermaid_sequence skips the first line ("sequenceDiagram")
        return _mermaid_sequence("sequenceDiagram\n" + line)

    def test_dashed_arrows_point_same_direction_as_solid(self):
        # All four arrow types describe A talking to B; none should point back at A.
        for at in ("->>", "-->>", "->", "-->"):
            out = self._seq(f"A {at} B: msg")
            assert "⇠" not in out, (
                f"arrow_type {at!r} rendered a left-pointing '⇠' though layout is "
                f"left-to-right (A→B): {out!r}"
            )

    def test_dashed_reply_distinct_from_dashed_open(self):
        # '-->>' (dashed reply, has '>>') must not render identically to '-->'.
        reply = self._seq("A -->> B: ok")
        open_ = self._seq("A --> B: ping")
        reply_glyph = reply.split("A ", 1)[1].split(" B", 1)[0]
        open_glyph = open_.split("A ", 1)[1].split(" B", 1)[0]
        assert reply_glyph != open_glyph, (
            f"dashed reply '-->>' and dashed open '-->' render the same glyph "
            f"{reply_glyph!r} — the '>>' distinction is lost"
        )

    def test_solid_arrows_unchanged(self):
        # The fix must not alter the already-correct solid-arrow rendering.
        assert "→" in self._seq("A ->> B: call")
        assert "⇢" in self._seq("A -> B: note")


class TestStreamingTablePreservation:
    """Tests that tables are preserved when using the streaming API path."""

    TABLE_RESPONSE = (
        "| Name | Value |\n" "|------|-------|\n" "| foo  | 42    |\n" "| bar  | 99    |"
    )

    def _streaming_client(self):
        c = MockSlackClient()
        c._stream_enabled = True
        return c

    @pytest.mark.asyncio
    async def test_streaming_no_update_message_with_options(self):
        """When streaming + OPTIONS, chat.update must NOT fire — rich renderer is preserved."""
        slack = self._streaming_client()
        text_with_options = self.TABLE_RESPONSE + "\n\n[OPTIONS: A | B]"
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=text_with_options)])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        updates = [a for a in slack.actions if a[0] == "update"]
        assert (
            len(updates) == 0
        ), "chat.update must not fire on the streaming path without redaction"

    @pytest.mark.asyncio
    async def test_stop_stream_text_preserves_tables(self):
        """The final text passed to stop_stream should still contain pipe tables."""
        slack = self._streaming_client()
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=self.TABLE_RESPONSE)])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        stops = [a for a in slack.actions if a[0] == "stop_stream"]
        assert len(stops) == 1
        final = stops[0][1]["text"]
        assert "| Name | Value |" in final
        assert "•" not in final

    @pytest.mark.asyncio
    async def test_non_streaming_converts_tables(self):
        """Without streaming, tables should be converted to bullet lists."""
        slack = MockSlackClient()  # _stream_enabled = False
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=self.TABLE_RESPONSE)])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        updates = [a for a in slack.actions if a[0] == "update"]
        assert any("•" in a[1]["text"] for a in updates)

    @pytest.mark.asyncio
    async def test_stream_failed_to_start_converts_tables(self):
        """If streaming is enabled but stream_ts is None (failed to start),
        tables should be converted to bullets since post_message uses mrkdwn."""
        slack = self._streaming_client()
        slack._start_stream_fails = True  # simulate stream failing to start
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=self.TABLE_RESPONSE)])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        # Fallback path uses update or post — either way tables must be bullets
        all_text_actions = [a for a in slack.actions if a[0] in ("post", "update")]
        assert any(
            "•" in a[1]["text"] for a in all_text_actions
        ), "Tables should be converted to bullets when stream fails to start"

    @pytest.mark.asyncio
    async def test_streaming_redaction_triggers_update_with_converted_tables(self):
        """When exfiltration URLs are detected in streaming mode,
        chat.update must fire with tables converted to bullets."""
        slack = self._streaming_client()
        # URL with long query string triggers exfiltration redaction
        exfil_url = "https://evil.com/steal?data=" + "A" * 200
        text = self.TABLE_RESPONSE + f"\n\nSee {exfil_url}"
        provider = FakeProvider([LLMEvent(kind="text_chunk", text=text)])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        updates = [a for a in slack.actions if a[0] == "update"]
        assert len(updates) >= 1, "chat.update must fire when redaction occurs"
        update_text = updates[-1][1]["text"]
        assert "REDACTED" in update_text, "Redacted URL should appear in update"
        assert "•" in update_text, "Tables should be converted to bullets in update"


class TestBuildTimingFooter:
    """Unit tests for the build_timing_footer helper."""

    def test_duration_seconds(self):
        from kiro_crew.slack.handler import build_timing_footer

        blocks, text = build_timing_footer(5.0)
        assert text == "Finished in 5s"
        assert blocks[0]["type"] == "context"

    def test_duration_minutes(self):
        from kiro_crew.slack.handler import build_timing_footer

        blocks, text = build_timing_footer(125.0)
        assert text == "Finished in 2m 5s"

    def test_with_client_ctx(self):
        from kiro_crew.slack.handler import build_timing_footer

        provider = FakeProvider()
        provider.context_usage_pct = lambda: 42.0
        blocks, text = build_timing_footer(3.0, provider)
        assert "🟡" in text
        assert "ctx 42%" in text

    def test_no_client_no_ctx(self):
        from kiro_crew.slack.handler import build_timing_footer

        blocks, text = build_timing_footer(10.0, None)
        assert "ctx" not in text
        assert text == "Finished in 10s"

    def test_client_error_falls_back(self):
        from kiro_crew.slack.handler import build_timing_footer

        provider = FakeProvider()
        provider.context_usage_pct = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        blocks, text = build_timing_footer(7.0, provider)
        assert text == "Finished in 7s"
        assert "ctx" not in text


def _handler_clock(monkeypatch, monotonic) -> None:
    """Give the handler and its composed owners *monotonic*, and nothing else.

    ``handler.time`` IS the stdlib ``time`` module, so patching an attribute on it
    rebinds ``time.monotonic`` for the whole worker: asyncio's loop clock and every
    other thread read the fake too, and a frozen fake strands every timer the turn
    awaits. The owners read ``time`` from the handler's globals
    (``_facade_compose.compose``), so rebinding that NAME reaches each elapsed-time
    read the turn makes while the rest of the process keeps the real clock.
    """
    from kiro_crew.slack import handler

    monkeypatch.setattr(handler, "time", SimpleNamespace(monotonic=monotonic))


class _HandlerAsyncio:
    """``asyncio`` as the handler's namespace sees it, with *overrides* swapped in.

    Same reason as :func:`_handler_clock`: ``monkeypatch.setattr(asyncio, ...)``
    would replace the attribute for every coroutine in the worker.
    """

    def __init__(self, **overrides):
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(asyncio, name)


def _completed_tasks(slack) -> list[dict]:
    """The ``complete`` task cards a turn appended, in order."""
    return [
        a[1] for a in slack.actions if a[0] == "append_task" and a[1].get("status") == "complete"
    ]


class TestToolElapsedTimer:
    """Tests for tool elapsed time display on task cards."""

    @pytest.mark.asyncio
    async def test_tool_completion_shows_elapsed_time(self, monkeypatch):
        """Tool taking >1s shows elapsed time in completion card at end of stream."""
        slack = MockSlackClient()
        slack._stream_enabled = True

        # Every handler clock read is 5.5 s after the previous one, so the
        # completion (read after the tool's timer start) is at least 5.5 s.
        calls = [0]

        def fake_monotonic():
            calls[0] += 1
            return 1_000.0 + calls[0] * 5.5

        _handler_clock(monkeypatch, fake_monotonic)
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="file contents"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "read it", None, "msg1", "U1")

        complete_tasks = _completed_tasks(slack)
        assert complete_tasks, f"No complete tasks found. actions={slack.actions}"
        titles = [str(t.get("title", "")) for t in complete_tasks]
        assert any(re.search(r"⏱ \d+(\.\d+)?s", t) for t in titles), titles
        assert all("⏱" not in str(t.get("details", "")) for t in complete_tasks)

    @pytest.mark.asyncio
    async def test_fast_tool_no_elapsed_time(self, monkeypatch):
        """Tool taking <1s shows no elapsed time."""
        slack = MockSlackClient()
        slack._stream_enabled = True

        # Every handler clock read returns the same instant (0 elapsed).
        _handler_clock(monkeypatch, lambda: 1_000.0)

        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "read it", None, "msg1", "U1")

        # Elapsed time is written into the completion card's TITLE, so that is
        # where a fast tool must show none.
        complete_tasks = _completed_tasks(slack)
        assert complete_tasks, f"No complete tasks found. actions={slack.actions}"
        for t in complete_tasks:
            assert "⏱" not in str(t.get("title", "")), t
            assert "⏱" not in str(t.get("details", "")), t

    @pytest.mark.asyncio
    async def test_tool_timer_cancelled_on_completion(self):
        """Tool timer task is properly cancelled when tool completes."""
        slack = MockSlackClient()
        slack._stream_enabled = True
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)

        # Just verify the handler completes without hanging (timer not blocking)
        await handle_message(slack, sessions, "C1", "read it", None, "msg1", "U1")

        task_appends = [a for a in slack.actions if a[0] == "append_task"]
        complete_tasks = [t for t in task_appends if t[1].get("status") == "complete"]
        assert len(complete_tasks) >= 1

    @pytest.mark.asyncio
    async def test_elapsed_time_shows_minutes_format(self, monkeypatch):
        """Tool taking >60s shows minutes+seconds format."""
        slack = MockSlackClient()
        slack._stream_enabled = True

        # Advance the clock by >60s on EVERY monotonic() call. The elapsed
        # calc always runs at least one call after _start_tool_timer, so the
        # computed elapsed is >= one step regardless of how many unrelated
        # monotonic() calls precede the timer start. (A fixed call-count
        # threshold here was order-dependent: module caches touched by earlier
        # tests in this file change the pre-timer call count, so the test
        # broke whenever a pytest-split shard boundary landed inside this
        # class.)
        calls = [0]

        def fake_monotonic():
            calls[0] += 1
            return 1_000.0 + calls[0] * 75.5

        _handler_clock(monkeypatch, fake_monotonic)
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "read it", None, "msg1", "U1")

        task_appends = [a for a in slack.actions if a[0] == "append_task"]
        complete_tasks = [t for t in task_appends if t[1].get("status") == "complete"]
        # Elapsed time is shown in the TITLE (Slack replaces title on same
        # task_id; details would APPEND and accumulate ⏱ stamps). Elapsed is
        # N*75.5s for some N>=1, so assert the minutes FORMAT rather than a
        # specific minute count.
        title_list = [str(t[1].get("title", "")) for t in complete_tasks]
        assert any(
            re.search(r"⏱ \d+m \d+(\.\d+)?s", t) for t in title_list
        ), f"Expected minutes-format elapsed in title but got: {title_list}"
        # Regression guard: elapsed must NOT be in details (append bug)
        details_list = [str(t[1].get("details", "")) for t in complete_tasks]
        assert all("⏱" not in d for d in details_list), f"⏱ leaked into details: {details_list}"

    @pytest.mark.asyncio
    async def test_elapsed_updater_fires_after_30s(self, monkeypatch):
        """Periodic updater updates task card after 30s."""
        from kiro_crew.slack import handler

        slack = MockSlackClient()
        slack._stream_enabled = True

        real_sleep = asyncio.sleep
        sleep_calls = []

        async def fake_sleep(secs):
            sleep_calls.append(secs)
            if secs == 30:
                # Updater sleep - let it run once then abort
                await real_sleep(0)
                raise asyncio.CancelledError()
            await real_sleep(0)

        monkeypatch.setattr(handler, "asyncio", _HandlerAsyncio(sleep=fake_sleep))

        call_count = [0]

        def fake_monotonic():
            call_count[0] += 1
            # Simulate 45 seconds elapsed when updater checks
            if call_count[0] <= 2:
                return 1_000.0
            return 1_045.0

        _handler_clock(monkeypatch, fake_monotonic)
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "read it", None, "msg1", "U1")

        # The 30s sleep is the updater's: seeing it proves the updater started.
        assert 30 in sleep_calls

    @pytest.mark.asyncio
    async def test_tool_transition_completion_shows_elapsed_in_title(self, monkeypatch):
        """When a new tool starts, the previous tool's task card is marked
        complete with elapsed time in the TITLE (not details)."""
        slack = MockSlackClient()
        slack._stream_enabled = True

        calls = [0]

        def fake_monotonic():
            calls[0] += 1
            # Advance >1s per call: the transition's elapsed calc always runs
            # after the first tool's timer start, so elapsed >= one 42s step.
            # (A fixed call-count threshold was order-dependent — see
            # test_elapsed_time_shows_minutes_format.)
            return 1_000.0 + calls[0] * 42.0

        _handler_clock(monkeypatch, fake_monotonic)
        provider = FakeProvider(
            [
                LLMEvent(kind="tool_call", title="Read File", tool_kind="read"),
                LLMEvent(kind="tool_call", title="Run Shell", tool_kind="execute"),
                LLMEvent(kind="text_chunk", text="done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "do two things", None, "msg1", "U1")

        task_appends = [a for a in slack.actions if a[0] == "append_task"]
        complete_tasks = [t for t in task_appends if t[1].get("status") == "complete"]
        # The first tool's completion (at transition) should carry elapsed in title
        title_list = [str(t[1].get("title", "")) for t in complete_tasks]
        assert any("⏱" in t for t in title_list), f"No elapsed in any title: {title_list}"
        # Regression guard: elapsed never leaks into details
        details_list = [str(t[1].get("details", "")) for t in complete_tasks]
        assert all("⏱" not in d for d in details_list), f"⏱ leaked into details: {details_list}"


class _TaskCardRefusedSlack(MockSlackClient):
    """append_task is refused; every other call is healthy.

    The shape of a long tool phase meeting a Slack rate limit: the elapsed-time
    refresh is the only thing touching the stream and Slack turns it down.
    """

    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        await super().append_task(
            channel, ts, task_id, title, status, details=details, output=output
        )
        return False


class _StreamAppendRefusedOnceSlack(MockSlackClient):
    """The first append_stream is refused, so real text forces one rotation."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._n_append = 0

    async def append_stream(self, channel, ts, text):
        self._n_append += 1
        await super().append_stream(channel, ts, text)
        return self._n_append > 1


class TestTaskCardNeverAbandonsTheStream:
    """A refused task card must not cost the reader their in-progress message.

    Rotating on a task-card failure stops the stream the reader is watching and
    continues the same answer in a NEW message, so the thread reads as a reply
    that failed followed minutes later by an unexplained second reply. A task
    card is decoration, so skipping it withholds no answer text;
    ``_append_stream`` still rotates when real text is refused.
    """

    @pytest.mark.asyncio
    async def test_refused_task_card_does_not_open_a_second_stream(self):
        slack = _TaskCardRefusedSlack()
        slack._stream_enabled = True
        provider = FakeProvider(
            [
                LLMEvent(kind="text_chunk", text="looking"),
                LLMEvent(kind="tool_call", title="Run Shell", tool_kind="execute"),
                LLMEvent(kind="text_chunk", text=" done"),
            ]
        )
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "do it", None, "msg1", "U1")

        starts = [a for a in slack.actions if a[0] == "start_stream"]
        assert len(starts) == 1, slack.actions
        assert [a for a in slack.actions if a[0] == "append_task"], slack.actions
        # One stream opened, one stopped: the answer stayed in a single message.
        stops = [a for a in slack.actions if a[0] == "stop_stream"]
        assert len(stops) == 1, slack.actions
        assert stops[0][1]["ts"] == starts[0][1]["ts"], slack.actions

    @pytest.mark.asyncio
    async def test_refused_real_text_still_rotates_and_says_it_continues(self):
        """The branch that protects answer delivery is untouched, and the
        replacement stream opens with the continuation marker so the two
        messages read as one answer."""
        slack = _StreamAppendRefusedOnceSlack()
        slack._stream_enabled = True
        # Trailing space: StreamRedactor withholds a trailing credential-class
        # run, so a chunk with no separator never reaches append_stream at all.
        provider = FakeProvider([LLMEvent(kind="text_chunk", text="hello ")])
        sessions = FakeSessionManager(provider)
        await handle_message(slack, sessions, "C1", "hi", None, "msg1", "U1")

        starts = [a for a in slack.actions if a[0] == "start_stream"]
        assert len(starts) == 2, slack.actions
        assert starts[0][1]["text"] is None, starts
        # The literal, not the constant: a test that imports the constant still
        # passes when the marker is emptied out.
        assert "continued" in (starts[1][1]["text"] or ""), starts


class TestCondenseThinking:
    """Unit tests for the _condense_thinking blockquote/truncation helper."""

    def test_short_text_blockquoted_no_truncation(self):
        out = _condense_thinking("first line\nsecond line", limit=600)
        assert out.startswith("💭 *Thinking*\n")
        assert "> first line" in out
        assert "> second line" in out
        assert "full reasoning in dashboard" not in out

    def test_long_text_truncated_with_pointer(self):
        text = "word " * 400  # ~2000 chars, well over the limit
        out = _condense_thinking(text, limit=600)
        assert "full reasoning in dashboard Activity" in out
        # Body stays near the limit (plus header/quote/suffix overhead).
        assert len(out) < 800

    def test_truncates_on_word_boundary(self):
        text = "alpha beta gamma delta epsilon zeta"
        out = _condense_thinking(text, limit=18)
        # Should not cut in the middle of a word.
        body = (
            out.split("\n", 1)[1]
            .replace("> ", "")
            .replace("_…full reasoning in dashboard Activity_", "")
            .strip()
        )
        assert not body.endswith("gam")
        assert "alpha" in body

    def test_blank_lines_become_bare_quote_markers(self):
        out = _condense_thinking("a\n\nb", limit=600)
        lines = out.splitlines()
        assert ">" in lines  # the blank middle line renders as a bare ">"

    def test_hard_cut_when_no_word_boundary(self):
        """A long run with no early whitespace falls back to a hard cut at the
        limit (— the `cut < limit // 2` guard)."""
        text = "x" * 700  # 700 chars, no spaces → rfind returns -1 → hard cut
        out = _condense_thinking(text, limit=600)
        assert "full reasoning in dashboard Activity" in out
        body = out.split("\n", 1)[1]
        # Hard-cut keeps exactly `limit` chars of the run (quoted).
        assert body.count("x") == 600

    def test_truncates_on_newline_when_no_space_in_window(self):
        """When the only whitespace in the truncation window is a newline (no
        literal space), the cut still breaks cleanly at the line rather than
        falling through to the hard cut (quansea review — match any \\s)."""
        text = "a" * 300 + "\n" + "b" * 400  # only break in window is the \n at 300
        out = _condense_thinking(text, limit=600)
        # The reasoning quote line is the line after the header, before the
        # truncation-pointer suffix (which itself contains a/b letters).
        reasoning_line = out.splitlines()[1]
        assert reasoning_line == "> " + "a" * 300  # broke cleanly at the newline
        assert "b" not in reasoning_line  # nothing past the boundary leaked in
        assert "full reasoning in dashboard Activity" in out
