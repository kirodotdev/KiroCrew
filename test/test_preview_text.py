"""Tests for `preview_text.strip_markdown_preview` and its two consumers.

Session-list previews (sidebar `last_message` from `_ChatSlot.to_dict()` and
archived-session previews from `HistoryLog.last_message_preview`) are rendered
as a single plain-text truncated line — raw markdown markers there read as
noise (the bug: previews showing literally ``**GitHub Triage**`` and
`` ```diff --- /Users/... ``). The helper strips markdown to readable plain
text without rendering it; both producers must apply it.
"""

from __future__ import annotations

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.preview_text import strip_markdown_preview


class TestStripMarkdownPreview:
    def test_plain_text_passes_through(self):
        assert strip_markdown_preview("hello there") == "hello there"

    def test_bold_markers_stripped(self):
        assert (
            strip_markdown_preview("Compacted the **GitHub Triage** rail header")
            == "Compacted the GitHub Triage rail header"
        )

    def test_diff_fence_becomes_placeholder(self):
        src = "```diff\n--- /Users/user/.kirocrew/workspace/H.tsx\n+++ b\n@@ -1 +1 @@\n```"
        assert strip_markdown_preview(src) == "(diff)"

    def test_code_fence_becomes_placeholder(self):
        assert strip_markdown_preview("Done:\n```python\nprint(1)\n```") == "Done: (code)"

    def test_unterminated_fence_still_stripped(self):
        # A tail-window preview can slice a message mid-code-block.
        assert strip_markdown_preview("```diff\n--- /a/b.tsx\n+++") == "(diff)"

    def test_inline_code_keeps_literal_text(self):
        assert strip_markdown_preview("run `npm ci` first") == "run npm ci first"

    def test_keep_visible_marker_stripped(self):
        # #7948: the collapse-exemption marker is emitted "as its final line"
        # (prompt contract) — a trailing tag LINE is stripped from previews.
        assert (
            strip_markdown_preview("Fleet synthesis banked.\n<!-- keep-visible -->")
            == "Fleet synthesis banked."
        )

    def test_html_comment_control_tags_stripped(self):
        # Heartbeat delivery routing tags are HTML comments too — appended as
        # trailing lines. Mid-body occurrences are rendered content (the
        # tail-anchored grammar shared with the frontend recognizer): they
        # are what makes tags quoted in ANY code dialect untouchable.
        assert strip_markdown_preview("done\n<!-- deliver:dashboard -->") == "done"
        assert "deliver" in strip_markdown_preview("before <!-- deliver:dashboard --> after")

    def test_unterminated_control_tag_no_longer_swallows_text(self):
        # The old generic strip treated an unclosed <!-- as a comment to EOF,
        # which silently deleted everything after it — the data-loss shape
        # the scoped strip exists to prevent. A truncated control tag now
        # leaks literally (visible garbage beats silent loss).
        assert (
            strip_markdown_preview("visible tail <!-- keep-visible")
            == "visible tail <!-- keep-visible"
        )

    def test_ordinary_html_comments_are_preserved(self):
        # Only recognized control tags are stripped: an ordinary comment the
        # assistant quotes is visible rendered content in inline code, and
        # inline code is NOT fence-placeholdered — a generic strip deleted it.
        assert (
            strip_markdown_preview("use `<!-- ordinary -->` here") == "use <!-- ordinary --> here"
        )

    def test_html_comment_inside_code_fence_stays_placeholdered(self):
        # Fences are placeholder-protected BEFORE the comment strip runs.
        assert strip_markdown_preview("```html\n<!-- x -->\n```") == "(code)"

    def test_recognized_tag_quoted_in_inline_code_is_preserved(self):
        # A RECOGNIZED control tag quoted in inline code renders literally in
        # the dashboard — visible content the preview must keep. Round-5
        # review: the old anywhere-strip deleted it while search/copy kept
        # it, a grammar divergence between the projections.
        assert (
            strip_markdown_preview("the `<!-- keep-visible -->` marker")
            == "the <!-- keep-visible --> marker"
        )

    def test_plan_task_id_tag_is_stripped(self):
        # Third control-tag family (task planner anchors) — same leak class
        # as deliver: routing tags; covered by the shared constant.
        assert strip_markdown_preview("plan ready\n<!-- plan_task_id:abc123 -->") == "plan ready"

    def test_link_keeps_label(self):
        assert strip_markdown_preview("see [the docs](https://x.y/z)") == "see the docs"

    def test_image_uses_alt_or_placeholder(self):
        assert strip_markdown_preview("![screenshot](/tmp/a.png)") == "screenshot"
        assert strip_markdown_preview("![](/tmp/a.png)") == "(image)"

    def test_headers_quotes_bullets_stripped(self):
        src = "## Summary\n> quoted\n- item one\n2. item two"
        assert strip_markdown_preview(src) == "Summary quoted item one item two"

    def test_options_block_removed(self):
        assert strip_markdown_preview("pick one [OPTIONS: A | B]") == "pick one"

    def test_snake_case_survives(self):
        # Single underscores are NOT emphasis in intra-word positions; the
        # stripper must not mangle identifiers.
        assert strip_markdown_preview("check last_message field") == "check last_message field"

    def test_emoji_kept(self):
        assert strip_markdown_preview("✅ done") == "✅ done"

    def test_mcwidget_becomes_placeholder(self):
        assert (
            strip_markdown_preview('<mcwidget title="T"><div>x</div></mcwidget> saved')
            == "(widget) saved"
        )

    def test_whitespace_collapsed(self):
        assert strip_markdown_preview("a\n\n\nb   c") == "a b c"

    def test_zwsp_only_message_yields_empty(self):
        # Quiet monitor-loop cycles reply with a bare U+200B. ``str.split()``
        # does not treat Cf format chars as whitespace, so without the drop
        # the preview is truthy-but-invisible and the downstream
        # empty-preview fallbacks never fire.
        assert strip_markdown_preview("\u200b") == ""

    def test_invisible_only_mix_yields_empty(self):
        # ZWSP, ZWNJ, ZWJ, word joiner, BOM and soft hyphen with plain
        # whitespace between: the whole Cf class collapses, not one codepoint.
        assert strip_markdown_preview("\u200b\u200c \u200d\u2060\t\ufeff\u00ad") == ""

    def test_format_chars_dropped_from_mixed_content(self):
        # Dropped, not turned into spaces: visible text stays intact.
        assert strip_markdown_preview("a\u200bb c") == "ab c"


class TestSlotLastMessageStripsMarkdown:
    def _slot_with(self, *messages) -> _ChatSlot:
        s = _ChatSlot("chat-1")
        for role, content in messages:
            s.append(role, content, "msg msg-a", broadcast=False)
        return s

    def test_sidebar_preview_is_plain_text(self):
        s = self._slot_with(
            ("user", "do the thing"),
            ("assistant", "Compacted the **GitHub Triage** rail header"),
        )
        assert s.to_dict()["last_message"] == "Compacted the GitHub Triage rail header"

    def test_sidebar_preview_replaces_diff_fence(self):
        s = self._slot_with(
            ("assistant", "```diff\n--- /Users/user/.kirocrew/workspace/H.tsx\n+++ b\n```"),
        )
        assert s.to_dict()["last_message"] == "(diff)"

    def test_options_still_parsed_from_raw_text(self):
        # Stripping applies to the preview only — options come from raw text.
        s = self._slot_with(("assistant", "pick one [OPTIONS: A | B]"))
        d = s.to_dict()
        assert d["options"] == ["A", "B"]
        assert d["last_message"] == "pick one"

    def test_syntax_only_message_falls_back_to_older_preview(self):
        # Codex PR-243 finding: a latest message that is ONLY stripped syntax
        # must not blank the preview — fall back to the previous visible
        # message (mirrors history.last_message_preview's skip-empty scan).
        s = self._slot_with(
            ("assistant", "here is the real answer"),
            ("assistant", "[OPTIONS: A | B]"),
        )
        d = s.to_dict()
        assert d["last_message"] == "here is the real answer"
        # Role/options state still comes from the NEWEST conversational message.
        assert d["options"] == ["A", "B"]
        assert d["has_options"] is True

    def test_all_syntax_only_messages_yield_empty_preview(self):
        s = self._slot_with(("assistant", "---"))
        assert s.to_dict()["last_message"] == ""

    def test_zwsp_only_message_falls_back_to_older_preview(self):
        # A quiet monitor-loop cycle's say-nothing reply (bare U+200B) must
        # not blank the sidebar subtitle: with format chars dropped the
        # preview is empty, so the walk lands on the last real message.
        s = self._slot_with(
            ("assistant", "here is the real answer"),
            ("assistant", "\u200b"),
        )
        assert s.to_dict()["last_message"] == "here is the real answer"

    def test_credential_split_by_markdown_is_still_redacted(self):
        # Codex PR-243 HIGH finding: stripping must run BEFORE redaction.
        # Markdown markers inside a secret split the credential signature past
        # the scanner; stripping afterwards would rejoin the fragments into a
        # valid credential in the broadcast preview.
        s = self._slot_with(("assistant", "key is AKIA**IOSFODNN7EXAMPLE** ok"))
        msg = s.to_dict()["last_message"]
        assert "AKIAIOSFODNN7EXAMPLE" not in msg
        assert msg.startswith("key is ")

    def test_credential_split_by_zwsp_is_still_redacted(self):
        # The format-char drop can REASSEMBLE a credential that zero-width
        # characters had split, so strip-before-redact is load-bearing for
        # this class too: stripping after redaction would rejoin the halves
        # into a live key in the broadcast preview.
        s = self._slot_with(("assistant", "key is AKIA\u200bIOSFODNN7EXAMPLE ok"))
        msg = s.to_dict()["last_message"]
        assert "AKIAIOSFODNN7EXAMPLE" not in msg
        assert msg.startswith("key is ")


class TestHistoryLastMessagePreviewStripsMarkdown:
    def test_archived_preview_is_plain_text(self, tmp_path):
        import json

        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.init()
        key = "dash-test"
        lines = [
            json.dumps({"_type": "metadata", "title": "t"}),
            json.dumps({"role": "assistant", "content": "Compacted the **GitHub Triage** rail"}),
        ]
        log._path(key).write_text("\n".join(lines) + "\n")
        assert log.last_message_preview(key) == "Compacted the GitHub Triage rail"

    def test_last_message_info_falls_back_past_zwsp_only_row(self, tmp_path):
        import json
        from datetime import datetime

        from kiro_crew.history import ConversationLog

        log = ConversationLog(base_dir=tmp_path)
        log.init()
        key = "dash-test"
        lines = [
            json.dumps(
                {
                    "role": "assistant",
                    "content": "the real answer",
                    "ts": "2026-01-01T00:00:00Z",
                }
            ),
            # Newest row is a quiet monitor-cycle reply: a bare U+200B. Its
            # preview must come out empty so the skip-empty scan passes it.
            json.dumps({"role": "assistant", "content": "\u200b", "ts": "2026-01-02T00:00:00Z"}),
        ]
        log._path(key).write_text("\n".join(lines) + "\n")
        preview, epoch = log.last_message_info(key)
        assert preview == "the real answer"
        # The timestamp travels with the row the preview came from.
        assert epoch == datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()
