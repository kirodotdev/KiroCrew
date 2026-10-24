from __future__ import annotations

import inspect
import pathlib
import re
import time

import pytest
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401

from kiro_crew.dashboard.handlers import source_providers as source

# ── Jira issue fetching tests ────────────────────────────────────────────────


class TestAdfToMarkdown:
    """The ADF converter emits markdown for Atlassian Document Format JSON."""

    @staticmethod
    def _doc(*content):
        return {"type": "doc", "version": 1, "content": list(content)}

    @staticmethod
    def _para(*content):
        return {"type": "paragraph", "content": list(content)}

    @staticmethod
    def _text(text, marks=None):
        node = {"type": "text", "text": text}
        if marks is not None:
            node["marks"] = marks
        return node

    def test_simple_paragraph(self):
        adf = self._doc(self._para(self._text("Hello world")))
        assert source._adf_to_markdown(adf) == "Hello world"

    def test_multiple_paragraphs_separated_by_blank_line(self):
        adf = self._doc(self._para(self._text("Line 1")), self._para(self._text("Line 2")))
        assert source._adf_to_markdown(adf) == "Line 1\n\nLine 2"

    def test_heading_becomes_hashes(self):
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 3}, "content": [self._text("Title")]}
        )
        assert source._adf_to_markdown(adf) == "### Title"

    def test_heading_level_is_clamped(self):
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 99}, "content": [self._text("Deep")]}
        )
        assert source._adf_to_markdown(adf) == "###### Deep"

    def test_heading_stays_on_one_line(self):
        """Only a heading's first line carries the `#`, so a hardBreak inside it
        would leave a second line whose `-` renders as a list."""
        adf = self._doc(
            {
                "type": "heading",
                "attrs": {"level": 3},
                "content": [self._text("Title"), {"type": "hardBreak"}, self._text("- x")],
            }
        )
        assert source._adf_to_markdown(adf) == "### Title - x"

    def test_emphasis_marks(self):
        adf = self._doc(
            self._para(
                self._text("bold", [{"type": "strong"}]),
                self._text(" "),
                self._text("italic", [{"type": "em"}]),
                self._text(" "),
                self._text("gone", [{"type": "strike"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**bold** *italic* ~~gone~~"

    def test_adjacent_identical_marks_are_merged(self):
        """`**a****b**` renders as a bold `a****b` -- the delimiters become
        content -- so equally marked neighbours must merge before wrapping."""
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                self._text("b", [{"type": "strong"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**ab**"

    def test_adjacent_code_marks_are_merged(self):
        """Worse than emphasis: `` `a``b` `` collapses into one span holding
        literal backticks."""
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "code"}]),
                self._text("b", [{"type": "code"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "`ab`"

    def test_adjacent_different_marks_are_not_merged(self):
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                self._text("b", [{"type": "em"}]),
            )
        )
        # `_` rather than `*` for the em: it abuts the strong's closing `**`, and
        # two asterisk runs that touch are re-lexed as one. `**a**_b_` parses as
        # <strong>a</strong><em>b</em>.
        assert source._adf_to_markdown(adf) == "**a**_b_"

    def test_an_italic_between_plain_neighbours_keeps_its_emphasis(self):
        """CommonMark will not open underscore emphasis intraword, so `a_b_c`
        would render with visible underscores and no italic."""
        adf = self._doc(
            self._para(
                self._text("a"),
                self._text("b", [{"type": "em"}]),
                self._text("c"),
            )
        )
        assert source._adf_to_markdown(adf) == "a*b*c"

    def test_a_hard_break_keeps_marked_neighbours_apart(self):
        adf = self._doc(
            self._para(
                self._text("a", [{"type": "strong"}]),
                {"type": "hardBreak"},
                self._text("b", [{"type": "strong"}]),
            )
        )
        assert source._adf_to_markdown(adf) == "**a**  \n**b**"

    def test_code_mark_is_literal_and_not_escaped(self):
        adf = self._doc(self._para(self._text("a_b*c", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`a_b*c`"

    def test_code_span_keeps_its_boundary_spaces(self):
        """CommonMark strips one space from each end of ` x `, so pad it."""
        adf = self._doc(self._para(self._text(" foo ", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`  foo  `"

    def test_all_whitespace_code_span_is_not_padded(self):
        """Whitespace-only content is exempt from the strip rule, so padding it
        would silently add two spaces."""
        adf = self._doc(self._para(self._text("   ", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "`   `"

    def test_one_sided_space_in_a_code_span_is_not_padded(self):
        adf = self._doc(self._para(self._text(" foo", [{"type": "code"}])))
        assert source._adf_to_markdown(adf) == "` foo`"

    def test_newline_in_a_code_span_cannot_break_out_of_the_fence(self):
        """A code span is inline, so a newline in its content ends the paragraph
        and everything after it is parsed as fresh markdown -- outside the fence,
        and so past the inline escape and the link-target scan."""
        payload = "safe\n\n# Injected\n[x](javascript:alert(1))"
        node = self._text(payload, [{"type": "code"}])
        out = source._adf_to_markdown(self._doc(self._para(node)))
        assert "\n" not in out
        assert out == "`safe  # Injected [x](javascript:alert(1))`"

    def test_carriage_return_in_a_code_span_is_collapsed_too(self):
        """CommonMark ends a line on a bare CR and on CRLF, not only on LF."""
        for raw in ("a\rb", "a\r\nb"):
            out = source._adf_to_markdown(
                self._doc(self._para(self._text(raw, [{"type": "code"}])))
            )
            assert out == "`a b`", raw

    def test_folding_a_long_whitespace_run_is_linear(self):
        """A provider-controlled newline-FREE whitespace run, bounded only by the
        8MiB fetch cap, must fold in linear time. A pattern whose whitespace runs
        and newline anchor compete for the same characters backtracks
        quadratically — 200k spaces cost seconds per heading and per table cell.
        The budget is ~100x the linear cost, so this fails only on a return to
        quadratic scanning."""
        payload = " " * 200_000 + "x"
        start = time.monotonic()
        assert source._md_one_line(payload) == "x"
        assert time.monotonic() - start < 2.0

    def test_empty_marked_text_emits_nothing(self):
        """A marked empty text node must not leave its bare delimiters behind."""
        for mark in ("strong", "em", "strike", "code"):
            adf = self._doc(self._para(self._text("", [{"type": mark}])))
            assert source._adf_to_markdown(adf) == "", mark

    def test_external_media_becomes_a_link_not_an_image(self):
        """A link keeps the URL recoverable without the panel auto-fetching it."""
        adf = self._doc(
            self._para(
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://ex.com/a.png", "alt": "chart"},
                }
            )
        )
        assert source._adf_to_markdown(adf) == "[chart](https://ex.com/a.png)"

    def test_media_without_a_url_contributes_nothing(self):
        """An attachment reference carries no fetchable address."""
        adf = self._doc(
            self._para({"type": "media", "attrs": {"type": "file", "id": "abc", "alt": "shot"}})
        )
        assert source._adf_to_markdown(adf) == ""

    def test_link_mark_keeps_the_url(self):
        adf = self._doc(
            self._para(
                self._text(
                    "the docs",
                    [{"type": "link", "attrs": {"href": "https://example.com/a"}}],
                )
            )
        )
        assert source._adf_to_markdown(adf) == "[the docs](https://example.com/a)"

    def test_link_with_parentheses_uses_the_angle_bracket_form(self):
        adf = self._doc(
            self._para(
                self._text(
                    "wiki",
                    [{"type": "link", "attrs": {"href": "https://ex.com/a(b)"}}],
                )
            )
        )
        assert source._adf_to_markdown(adf) == "[wiki](<https://ex.com/a(b)>)"

    def test_inline_card_becomes_a_link(self):
        adf = self._doc(
            self._para({"type": "inlineCard", "attrs": {"url": "https://example.com"}})
        )
        assert source._adf_to_markdown(adf) == "[https://example.com](https://example.com)"

    def test_code_block_is_fenced_with_its_language(self):
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [self._text("print(1)\nprint(2)")],
            }
        )
        assert source._adf_to_markdown(adf) == "```python\nprint(1)\nprint(2)\n```"

    def test_code_block_fence_widens_past_inner_backticks(self):
        adf = self._doc({"type": "codeBlock", "content": [self._text("a ``` b")]})
        assert source._adf_to_markdown(adf) == "````\na ``` b\n````"

    def test_code_block_language_cannot_leave_its_fence_line(self):
        """A fence info string runs to end of line, so a newline in the
        `language` attribute would close the fence and inject real markdown."""
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python\n\n![x](https://evil.example/beacon.png)\n\n```"},
                "content": [self._text("safe")],
            }
        )
        result = source._adf_to_markdown(adf)
        assert result == "```\nsafe\n```"
        assert "evil.example" not in result

    def test_code_block_keeps_a_real_language_token(self):
        adf = self._doc(
            {"type": "codeBlock", "attrs": {"language": "c++"}, "content": [self._text("x;")]}
        )
        assert source._adf_to_markdown(adf) == "```c++\nx;\n```"

    def test_code_block_drops_a_multi_token_language(self):
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python rm -rf"},
                "content": [self._text("x")],
            }
        )
        assert source._adf_to_markdown(adf) == "```\nx\n```"

    def test_expand_title_stays_on_one_line(self):
        adf = self._doc(
            {
                "type": "expand",
                "attrs": {"title": "Details\n\n# Injected"},
                "content": [self._para(self._text("inner"))],
            }
        )
        assert source._adf_to_markdown(adf) == "**Details # Injected**\n\ninner"

    def test_mention_name_stays_on_one_line(self):
        adf = self._doc(self._para({"type": "mention", "attrs": {"text": "Alice\n# Injected"}}))
        assert source._adf_to_markdown(adf) == "@Alice # Injected"

    def test_bullet_list_gets_markers(self):
        adf = self._doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("one"))]},
                    {"type": "listItem", "content": [self._para(self._text("two"))]},
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- one\n- two"

    def test_nested_list_is_indented_under_its_parent(self):
        adf = self._doc(
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            self._para(self._text("outer")),
                            {
                                "type": "bulletList",
                                "content": [
                                    {
                                        "type": "listItem",
                                        "content": [self._para(self._text("inner"))],
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- outer\n  - inner"

    def test_ordered_list_honours_its_start_number(self):
        adf = self._doc(
            {
                "type": "orderedList",
                "attrs": {"order": 3},
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("a"))]},
                    {"type": "listItem", "content": [self._para(self._text("b"))]},
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "3. a\n4. b"

    def test_task_list_becomes_a_checklist(self):
        adf = self._doc(
            {
                "type": "taskList",
                "content": [
                    {
                        "type": "taskItem",
                        "attrs": {"state": "DONE"},
                        "content": [self._text("shipped")],
                    },
                    {
                        "type": "taskItem",
                        "attrs": {"state": "TODO"},
                        "content": [self._text("pending")],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "- [x] shipped\n- [ ] pending"

    def test_blockquote_prefixes_every_line(self):
        adf = self._doc(
            {
                "type": "blockquote",
                "content": [self._para(self._text("first")), self._para(self._text("second"))],
            }
        )
        assert source._adf_to_markdown(adf) == "> first\n>\n> second"

    def test_panel_renders_as_a_blockquote(self):
        adf = self._doc(
            {
                "type": "panel",
                "attrs": {"panelType": "warning"},
                "content": [self._para(self._text("careful"))],
            }
        )
        assert source._adf_to_markdown(adf) == "> careful"

    def test_rule_becomes_a_thematic_break(self):
        adf = self._doc(self._para(self._text("a")), {"type": "rule"}, self._para(self._text("b")))
        assert source._adf_to_markdown(adf) == "a\n\n---\n\nb"

    def test_table_becomes_gfm(self):
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("H1"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("H2"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("a"))]},
                            {"type": "tableCell", "content": [self._para(self._text("b"))]},
                        ],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "| H1 | H2 |\n| --- | --- |\n| a | b |"

    def test_short_table_row_emits_only_its_own_cells(self):
        """GFM fills a short row itself, so padding it here buys nothing."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("H1"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("H2"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("only"))]}
                        ],
                    },
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "| H1 | H2 |\n| --- | --- |\n| only |"

    def test_a_ragged_table_does_not_amplify_its_output(self):
        """A wide header plus many narrow rows must stay linear in cell count."""
        cells = 200
        wide = {
            "type": "tableRow",
            "content": [
                {"type": "tableHeader", "content": [self._para(self._text("h"))]}
                for _ in range(cells)
            ],
        }
        narrow = [
            {
                "type": "tableRow",
                "content": [{"type": "tableCell", "content": [self._para(self._text("c"))]}],
            }
            for _ in range(cells)
        ]
        rendered = source._adf_to_markdown(self._doc({"type": "table", "content": [wide, *narrow]}))
        # 400 real cells. Padding every short row to the widest emits 200*200.
        assert rendered.count("|") < 2000

    def test_mention_gets_an_at_prefix_without_doubling_it(self):
        adf = self._doc(
            self._para(
                {"type": "mention", "attrs": {"text": "Alice"}},
                self._text(" and "),
                {"type": "mention", "attrs": {"text": "@Bob"}},
            )
        )
        assert source._adf_to_markdown(adf) == "@Alice and @Bob"

    def test_hard_break_is_a_markdown_line_break(self):
        adf = self._doc(self._para(self._text("a"), {"type": "hardBreak"}, self._text("b")))
        assert source._adf_to_markdown(adf) == "a  \nb"

    def test_literal_markdown_in_text_is_escaped(self):
        adf = self._doc(self._para(self._text("**not bold** and <b>tag</b> and _u_")))
        result = source._adf_to_markdown(adf)
        assert result == r"\*\*not bold\*\* and \<b\>tag\</b\> and \_u\_"

    def test_literal_html_entity_in_text_is_escaped(self):
        """rehypeRaw would otherwise decode `&copy;` to a copyright sign."""
        adf = self._doc(self._para(self._text("&copy; 2026 &amp; friends")))
        assert source._adf_to_markdown(adf) == r"\&copy; 2026 \&amp; friends"

    def test_a_credential_in_text_is_redacted_by_the_converter_itself(self):
        secret = "Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(self._para(self._text(f"use ghp_{secret} to clone")))
        assert secret not in source._adf_to_markdown(adf)

    def test_a_credential_in_an_attribute_is_redacted_inside_the_bounded_walk(self):
        """`_adf_attr_label` redacts before it escapes.

        Escaping would insert a backslash into `ghp_...` and hide it from the
        payload-level redactor that runs afterwards, and doing this inside the
        converter's depth-capped traversal is what avoids an unbounded pre-pass
        over a provider-controlled tree.
        """
        secret = "Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                {
                    "type": "media",
                    "attrs": {
                        "type": "external",
                        "url": "https://ex.com/a.png",
                        "alt": f"use ghp_{secret}",
                    },
                }
            )
        )
        assert secret not in source._adf_to_markdown(adf)

    def test_a_credential_split_across_marked_siblings_is_still_redacted(self):
        """A plain-text walk joins sibling text nodes seamlessly, so the payload
        redactor catches a credential spanning them. Marks would put delimiters
        between the halves and hide it, so an inline run whose own raw text
        carries a credential is emitted as one redacted string."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(self._text(head), self._text(tail, [{"type": "strong"}]))
        )
        rendered = source._adf_to_markdown(source._redact_provider_data(adf))
        assert tail not in rendered
        assert head not in rendered

    def test_redacting_a_run_keeps_every_node_s_text(self):
        """The fallback emits the plain rendition of the WHOLE run, so a mention
        or card in the same paragraph keeps its text instead of disappearing."""
        secret = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                self._text(f"token {secret} for "),
                {"type": "mention", "attrs": {"text": "Alice"}},
                self._text(" see "),
                {"type": "inlineCard", "attrs": {"url": "https://example.com/doc"}},
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert secret not in rendered
        assert "Alice" in rendered
        assert "https://example.com/doc" in rendered

    def test_a_credential_split_across_a_label_boundary_is_redacted(self):
        """An emoji label contributes text with no delimiter of its own, so a
        secret continued inside one is contiguous in the rendered output."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(self._text(head), {"type": "emoji", "attrs": {"text": tail}})
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_a_credential_split_through_an_unknown_container_is_redacted(self):
        """An unrecognised inline container emits nothing of its own, so a
        plain-text walk joined the halves either side of it seamlessly. It has to
        stay inside the span the credential check reads."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                self._text(head),
                {"type": "someFutureInline", "content": [self._text(tail)]},
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_literal_math_syntax_is_escaped(self):
        """The same renderer runs remark-math, so a literal `$$x$$` would
        otherwise render as KaTeX instead of as the characters typed."""
        adf = self._doc(self._para(self._text("costs $$5 and $x$ too")))
        assert source._adf_to_markdown(adf) == r"costs \$\$5 and \$x\$ too"

    def test_a_credential_split_inside_an_unknown_container_is_redacted(self):
        """Both halves inside the container, the second one marked."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        adf = self._doc(
            self._para(
                {
                    "type": "someFutureInline",
                    "content": [self._text(head), self._text(tail, [{"type": "strong"}])],
                }
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert head not in rendered
        assert tail not in rendered

    def test_adjacent_identical_marks_inside_an_unknown_container_are_merged(self):
        adf = self._doc(
            self._para(
                {
                    "type": "someFutureInline",
                    "content": [
                        self._text("a", [{"type": "strong"}]),
                        self._text("b", [{"type": "strong"}]),
                    ],
                }
            )
        )
        assert source._adf_to_markdown(adf) == "**ab**"

    def test_a_literal_bang_cannot_splice_an_image_onto_a_link(self):
        """`!` before an emitted `[` would form image syntax, and an image
        auto-fetches the URL -- the exact beacon the media-as-link form avoids."""
        adf = self._doc(
            self._para(
                self._text("!"),
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://evil.example/beacon.png"},
                },
            )
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == r"\![https://evil.example/beacon.png](https://evil.example/beacon.png)"

    def test_a_wide_run_of_text_nodes_merges_in_one_pass(self):
        """Only traversal DEPTH is capped, so a provider can put hundreds of
        thousands of adjacent text nodes in one paragraph. The run's text is
        joined once rather than rebuilt per node."""
        n = 100000
        adf = self._doc(
            {"type": "paragraph", "content": [{"type": "text", "text": "a"} for _ in range(n)]}
        )
        assert source._adf_to_markdown(adf) == "a" * n

    def test_a_credential_split_deep_inside_nested_containers_is_redacted(self):
        """The scan runs once at the outermost run and inner containers reuse it,
        so the guarantee has to hold at depth, not just at the top level."""
        head, tail = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4", "Yz7Bc0Ef3Gh6"
        node = {
            "type": "someFutureInline",
            "content": [self._text(head), self._text(tail, [{"type": "strong"}])],
        }
        for _ in range(3):
            node = {"type": "someFutureInline", "content": [node]}
        rendered = source._adf_to_markdown(self._doc(self._para(node)))
        assert head not in rendered
        assert tail not in rendered

    def test_a_link_whose_href_fails_the_scan_emits_no_destination(self):
        """`_URL_RE` stops at `)`, so a paren in the path puts the whole query
        outside every exfiltration check. The href is scanned paren-encoded and
        the link is dropped rather than emitted partly redacted."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        href = f"https://evil.example.com/a)b?data={blob}"
        adf = self._doc(
            self._para(self._text("click", [{"type": "link", "attrs": {"href": href}}]))
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == "click"
        assert blob not in rendered
        assert "evil.example.com" not in rendered

    def test_a_benign_url_with_parentheses_keeps_its_link(self):
        """The scan must not cost legitimate links: wiki and Confluence URLs
        carry parentheses routinely, and one is not evidence of anything."""
        for href in (
            "https://en.wikipedia.org/wiki/Salt_(chemistry)",
            "https://co.atlassian.net/wiki/spaces/X/pages/1/Plan_(v2)?focus=1",
        ):
            adf = self._doc(
                self._para(self._text("doc", [{"type": "link", "attrs": {"href": href}}]))
            )
            assert source._adf_to_markdown(adf) == f"[doc](<{href}>)"

    def test_a_layout_keeps_its_columns_as_blocks(self):
        """Markdown has no columns, so a layout flattens -- but its children are
        BLOCKS. Reaching the inline path concatenated them into `firstsecond`,
        losing both the separator and the heading's `##`."""
        adf = {
            "type": "doc",
            "content": [
                {
                    "type": "layoutSection",
                    "content": [
                        {
                            "type": "layoutColumn",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "first"}]}
                            ],
                        },
                        {
                            "type": "layoutColumn",
                            "content": [
                                {
                                    "type": "heading",
                                    "attrs": {"level": 2},
                                    "content": [{"type": "text", "text": "second"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }
        assert source._adf_to_markdown(adf) == "first\n\n## second"

    def test_a_bodied_extension_and_decision_list_keep_their_blocks(self):
        for container, item, expected in (
            ("bodiedExtension", "paragraph", "alpha\n\nbeta"),
            ("decisionList", "decisionItem", "alpha\n\nbeta"),
        ):
            adf = {
                "type": "doc",
                "content": [
                    {
                        "type": container,
                        "content": [
                            {"type": item, "content": [{"type": "text", "text": "alpha"}]},
                            {"type": item, "content": [{"type": "text", "text": "beta"}]},
                        ],
                    }
                ],
            }
            assert source._adf_to_markdown(adf) == expected

    def test_a_block_card_keeps_its_url(self):
        """A block-level card carries its URL in an attribute and has no content,
        so the inline fallthrough rendered it as the empty string -- the URL was
        lost outright, the same unrecoverable loss this change exists to fix."""
        for card in ("blockCard", "embedCard"):
            adf = {"type": "doc", "content": [{"type": card, "attrs": {"url": "https://e.com/p"}}]}
            assert source._adf_to_markdown(adf) == "[https://e.com/p](https://e.com/p)"

    def test_no_node_type_emits_an_attribute_without_passing_a_gate(self):
        """Redaction lives at two chokepoints -- `_adf_attr_label` for attribute
        text and `_adf_url_link` for a URL destination. That invariant is
        conventional unless something checks it, so the node types and attribute
        names are read back OUT of the module and every combination is tried:
        a type added later is covered without anyone remembering this test."""
        src = inspect.getsource(inspect.getmodule(source._adf_to_markdown))
        body = src[src.index("def _adf_block_to_markdown") : src.index("def _adf_plain_text")]
        attr_names = set(re.findall(r'attrs\.get\("([a-zA-Z]+)"\)', body))
        node_types = set(source._ADF_BLOCK_TYPES) | set(
            re.findall(r'node_type (?:==|in \()\s*"([a-zA-Z]+)"', body)
        )
        assert "url" in attr_names and len(node_types) > 10, (attr_names, node_types)

        secret = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv1Wx4Yz7Bc0Ef3Gh6"
        leaked = []
        for node_type in sorted(node_types):
            node = {
                "type": node_type,
                "attrs": {name: secret for name in attr_names},
                "content": [{"type": "text", "text": "body"}],
            }
            rendered = source._adf_to_markdown({"type": "doc", "content": [node]})
            if secret in rendered.replace("\\", ""):
                leaked.append(node_type)
        assert not leaked, f"attribute reached the document unredacted for: {leaked}"

    def test_overlapping_adjacent_marks_stay_unambiguous(self):
        """Wrapping each node on its own emitted `**a*****b****c*`, which a
        CommonMark parser reads as strong(a), a LITERAL `***b***`, then em(c):
        the delimiters showed as text and the middle node lost both marks. Each
        expectation below was checked through a parser, not reasoned about.
        """

        def t(txt, *kinds):
            return self._text(txt, [{"type": k} for k in kinds])

        # `**a*b***_c_`   -> <strong>a<em>b</em></strong><em>c</em>
        # `_a**b**_**c**` -> <em>a<strong>b</strong></em><strong>c</strong>
        # `**a**_b_**c**` -> <strong>a</strong><em>b</em><strong>c</strong>
        for nodes, expected in (
            ((t("a", "strong"), t("b", "strong", "em"), t("c", "em")), "**a*b***_c_"),
            ((t("a", "em"), t("b", "em", "strong"), t("c", "strong")), "_a**b**_**c**"),
            ((t("a", "strong"), t("b", "em"), t("c", "strong")), "**a**_b_**c**"),
        ):
            rendered = source._adf_to_markdown(self._doc(self._para(*nodes)))
            assert rendered == expected
            # No delimiter run longer than the three of a nested strong+em.
            assert "****" not in rendered

    def test_an_unrenderable_emphasis_is_dropped_not_corrupted(self):
        """When a run needs `*` at one end and `_` at the other -- an em that
        both abuts an asterisk run and is followed by a word -- neither spelling
        works. The mark is dropped and the text kept, because emitting a
        delimiter anyway would show it as content AND lose the italic."""

        def t(txt, *kinds):
            return self._text(txt, [{"type": k} for k in kinds])

        rendered = source._adf_to_markdown(
            self._doc(
                self._para(
                    t("a", "strong"), t("b", "strong", "em"), t("c", "em"), t("d")
                )
            )
        )
        assert rendered == "**a*b***cd"
        assert "_" not in rendered

    def test_a_language_with_a_trailing_newline_is_rejected(self):
        """`$` also matches just before a trailing newline, so `re.match` accepted
        `"python\\n"` and the fence emitted a blank first line inside the block."""
        adf = self._doc(
            {
                "type": "codeBlock",
                "attrs": {"language": "python\n"},
                "content": [{"type": "text", "text": "body"}],
            }
        )
        assert source._adf_to_markdown(adf) == "```\nbody\n```"

    def test_a_wide_alternating_mark_run_stays_unambiguous(self):
        """The emitter looks at the character before each mark, so it must carry
        one character forward rather than the accumulated output -- passing the
        prefix made it quadratic (13.85s for 100k nodes against 0.72s)."""
        nodes = []
        for i in range(3000):
            kinds = (("strong",), ("strong", "em"), ("em",))[i % 3]
            nodes.append(self._text(f"w{i}", [{"type": k} for k in kinds]))
        rendered = source._adf_to_markdown(self._doc({"type": "paragraph", "content": nodes}))
        # Four in a row is the signature of two delimiter runs that have merged.
        assert "****" not in rendered
        assert "w2999" in rendered

    def test_a_code_body_round_trips_its_own_trailing_newlines(self):
        """The newline before the closing fence SEPARATES the body from it. Adding
        it unconditionally changed the content: a source ending in one newline came
        back with two, and an empty body became a block holding a blank line."""
        for body, expected in (
            ("x", "```\nx\n```"),
            ("x\n", "```\nx\n```"),
            ("", "```\n```"),
            ("x\n\n", "```\nx\n\n```"),
            ("a\nb", "```\na\nb\n```"),
        ):
            adf = self._doc(
                {
                    "type": "codeBlock",
                    "content": [{"type": "text", "text": body}] if body else [],
                }
            )
            assert source._adf_to_markdown(adf) == expected

    def test_an_explicit_zero_start_is_preserved(self):
        """ADF allows `order: 0` and CommonMark honours it as `<ol start="0">`.
        Coercing with `or 1` silently renumbered the list from 1."""
        adf = self._doc(
            {
                "type": "orderedList",
                "attrs": {"order": 0},
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": t}]}
                        ],
                    }
                    for t in ("a", "b")
                ],
            }
        )
        assert source._adf_to_markdown(adf) == "0. a\n1. b"

    def test_an_out_of_range_list_start_falls_back(self):
        """A marker of more than nine digits is not a list start: CommonMark reads
        `1000000000. a` as a paragraph, so the whole list would render as literal
        text. The last item's marker is what has to fit."""
        for order, expected_first in ((10**9, "1."), (999999999, "1."), (999999998, "999999998.")):
            adf = self._doc(
                {
                    "type": "orderedList",
                    "attrs": {"order": order},
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": t}]}
                            ],
                        }
                        for t in ("a", "b")
                    ],
                }
            )
            assert source._adf_to_markdown(adf).startswith(expected_first)

    def test_a_non_integer_order_falls_back_to_one(self):
        for order in ("3", True, -1, None, 1.5):
            adf = self._doc(
                {
                    "type": "orderedList",
                    "attrs": {"order": order},
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {"type": "paragraph", "content": [{"type": "text", "text": "a"}]}
                            ],
                        }
                    ],
                }
            )
            assert source._adf_to_markdown(adf) == "1. a"

    def test_every_scan_terminating_character_is_covered(self):
        """`_URL_RE`'s path class is `[^\\s)\\"'>]*`, so a character from it ends
        the match and puts the rest of the query outside every exfiltration check.

        A link DESTINATION is scanned as one address, whitespace included, because
        that is what gets emitted: the angle-bracket form percent-encodes a space,
        so a space does not end the URL there. All ten characters are therefore
        sealed on this path -- each one measured as leaking before.
        """
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        for ch in (")", "'", '"', ">", " ", "\t", "\n", "\r", "\v", "\f"):
            href = f"https://evil.example.com/a{ch}b?data={blob}"
            adf = self._doc(
                self._para(self._text("click", [{"type": "link", "attrs": {"href": href}}]))
            )
            rendered = source._adf_to_markdown(adf)
            assert blob not in rendered, f"leaked past {ch!r}"
            assert "evil.example.com" not in rendered

    def test_a_url_followed_by_prose_is_left_alone(self):
        """In PROSE a space really does end the URL -- verified against the real
        renderer, where `https://host/a?data= <blob>` yields an anchor whose href
        is `https://host/a?data=`, so following text cannot ride along in a
        fetchable address. Encoding whitespace here anyway treated a URL and the
        next word as one address: a URL followed by a 40-character commit SHA
        scanned as a query carrying the SHA, the entropy heuristic fired, and the
        whole paragraph became `see%20[REDACTED: suspicious URL ...]` with every
        word after the URL gone."""
        sha = "9f2c1ab4de5607893bcf24e01a7d6b3958e04c12"
        text = f"see https://example.com/pr?id=7 {sha} for detail"
        rendered = source._adf_to_markdown(self._doc(self._para(self._text(text))))
        assert rendered == text
        assert "%20" not in rendered and "REDACTED" not in rendered

    def test_a_benign_url_with_an_apostrophe_keeps_its_link(self):
        """The scan must not cost legitimate links: a page title with an
        apostrophe is ordinary, and encoding it is only for the scan."""
        href = "https://co.atlassian.net/wiki/spaces/X/pages/1/Bob's_Plan?focus=1"
        adf = self._doc(self._para(self._text("doc", [{"type": "link", "attrs": {"href": href}}])))
        assert source._adf_to_markdown(adf) == f"[doc]({href})"

    @staticmethod
    def _table_row(*cells):
        return {
            "type": "tableRow",
            "content": [
                {
                    "type": "tableCell",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": c}]}],
                }
                for c in cells
            ],
        }

    def test_a_row_wider_than_the_header_keeps_its_cells(self):
        """GFM fixes the table width at the HEADER and DROPS a longer row's excess
        -- the text is gone, not wrapped. Verified against a GFM parser: under a
        two-column header, `| c | d | e | f |` renders only c and d."""
        adf = self._doc(
            {
                "type": "table",
                "content": [self._table_row("a", "b"), self._table_row("c", "d", "e", "f")],
            }
        )
        rendered = source._adf_to_markdown(adf)
        assert rendered == "| a | b |  |  |\n| --- | --- | --- | --- |\n| c | d | e | f |"

    def test_widening_the_header_does_not_pad_every_row(self):
        """Only the header and separator grow. Padding every row to the widest is
        what made this quadratic before: one wide row among many narrow ones cost
        rows x width cells for the handful actually carried."""
        rows = [self._table_row(*[f"c{i}" for i in range(400)])]
        rows.extend(self._table_row("x") for _ in range(400))
        rendered = source._adf_to_markdown(self._doc({"type": "table", "content": rows}))
        # Header + separator carry 400 each; the 400 narrow rows carry one apiece.
        assert rendered.count("|") < 2500

    def test_the_escape_set_is_pinned_to_the_renderers_plugins(self):
        """This escape set is DERIVED from the plugins the panel's renderer runs:
        `$` is escaped because remark-math is in that stack, `!` because rehypeRaw
        admits an image that would auto-fetch, `~` because of GFM strikethrough.
        The coupling crosses a language boundary, so adding a remark plugin with
        new syntax would reopen a hole here with nothing going red.

        This pins the stack rather than the conclusion: if the list changes,
        re-derive the escape set and only then update this test. It is the shared
        contract Design Review asked for, in the one place that can fail.
        """
        components = pathlib.Path(__file__).resolve().parents[1] / "website" / "src" / "components"
        renderer = components / "MarkdownRenderer.tsx"
        if not renderer.is_file():
            pytest.skip("frontend renderer is not part of this checkout")
        # The renderer's leading parse plugins live in a shared list it spreads
        # in (the rich-text copy uses the same list), so the stack is the union.
        sources = [renderer, components / "markdown" / "remarkBase.ts"]
        imported = set()
        for path in sources:
            imported |= set(
                re.findall(
                    r"from '((?:remark|rehype)-[a-z0-9-]+)'", path.read_text(encoding="utf-8")
                )
            )
        assert imported == {
            "remark-parse",
            "remark-gfm",
            "remark-math",
            "remark-cjk-friendly",
            "remark-cjk-friendly-gfm-strikethrough",
            "rehype-raw",
            "rehype-katex",
        }, "renderer plugin stack changed -- re-derive the escape set in _MD_INLINE_ESCAPE"

    def test_the_shared_safety_fixture_matches_the_converter(self):
        """Half of a contract the FRONTEND test asserts the other half of.

        This side pins that the converter turns each fixture `adf` into exactly
        that `markdown`, so the fixture cannot drift from the converter. The
        frontend side renders the same `markdown` through the real
        MarkdownRenderer plugin stack and asserts the selectors, which is what
        checks the escape set against the renderer that actually runs instead of
        against a comment describing it.
        """
        import json

        fixture = pathlib.Path(__file__).resolve().parent / "fixtures" / "adf_markdown_safety.json"
        cases = json.loads(fixture.read_text())["cases"]
        assert len(cases) >= 6
        for case in cases:
            rendered = source._adf_to_markdown(case["adf"])
            assert rendered == case["markdown"], case["name"]
            if case.get("forbidText"):
                assert case["forbidText"] not in rendered.replace("\\", ""), case["name"]

    def test_no_emission_site_scans_a_truncated_url(self):
        """The truncation gap is per-CALL-SITE, not per-node-type. When only the
        link destination normalised the URL before scanning, an expand title, a
        mention label, an inline card and a media URL each still leaked a
        high-entropy query -- measured one by one. All of them now go through the
        one redaction primitive."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        url = f"https://evil.example.com/a)b?data={blob}"
        sites = {
            "expand title": self._doc(
                {
                    "type": "expand",
                    "attrs": {"title": f"see {url}"},
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "x"}]}
                    ],
                }
            ),
            "mention label": self._doc(
                self._para({"type": "mention", "attrs": {"text": url}})
            ),
            "inline card": self._doc(self._para({"type": "inlineCard", "attrs": {"url": url}})),
            "media url": self._doc(
                self._para({"type": "media", "attrs": {"type": "external", "url": url}})
            ),
            "link destination": self._doc(
                self._para(self._text("c", [{"type": "link", "attrs": {"href": url}}]))
            ),
        }
        for name, adf in sites.items():
            rendered = source._adf_to_markdown(adf)
            assert blob not in rendered, name

    def test_redaction_leaves_ordinary_text_byte_identical(self):
        """The scan form is only a scan form: when nothing is found the original
        text is emitted, so no percent escape shows up in the common case."""
        adf = self._doc(
            self._para(self._text("see https://example.com/a(b)c?page=2 and 'x' > y"))
        )
        rendered = source._adf_to_markdown(adf)
        assert "%28" not in rendered and "%29" not in rendered and "%27" not in rendered
        assert "https://example.com/a(b)c?page=2" in rendered

    def test_a_bare_url_in_prose_is_scanned(self):
        """This is the case that actually linkifies. Verified against the real
        renderer: bare text `https://host/a)b?data=<blob>` becomes an anchor whose
        href carries the paren AND the whole query, so the truncated scan has to
        be corrected here or a fetchable address reaches the panel."""
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        text = f"look at https://evil.example.com/a)b?data={blob} please"
        rendered = source._adf_to_markdown(self._doc(self._para(self._text(text))))
        assert blob not in rendered
        # The marker names the host on purpose, so the reader knows what went.
        assert "REDACTED: suspicious URL to evil.example.com" in rendered
        assert rendered.startswith("look at ") and rendered.endswith(" please")

    def test_a_code_block_url_is_not_a_link_so_it_is_not_url_scanned(self):
        """A code body deliberately does NOT get the URL-entropy scan.

        Verified against the real renderer: for a fenced block and for an inline
        code span the rendered output has zero anchors and zero images, so a URL
        in code is text and not a fetchable address -- the same reasoning that
        makes whitespace end a URL in prose. Running the entropy heuristic here
        would replace legitimate code samples (an API example with a long opaque
        token reads exactly like an exfiltration query) for no reachable gain.

        Credentials are still covered: the payload-level pass is token-shaped, so
        it catches `ghp_...` inside a code block regardless of any URL truncation.
        """
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        url = f"https://api.example.com/v1)x?token={blob}"
        adf = self._doc(
            {"type": "codeBlock", "content": [{"type": "text", "text": f"curl {url}"}]}
        )
        assert source._adf_to_markdown(adf) == f"```\ncurl {url}\n```"

    def test_a_credential_split_across_a_media_alt_is_redacted(self):
        """This supersedes an earlier, narrower claim of mine.

        The emitted `[alt](url)` form brackets the alt, so its halves cannot form
        one token. But on the path where a URL failing the destination scan drops
        the link and emits the LABEL ALONE -- no brackets -- that bracketing is
        gone. Measured on that path, the output is
        `ghp\\_Ab3Df6Hj9Kl2Np5Qr8TvWx4Yz7Bc0Ef3`: one recoverable credential, with
        the backslash from escaping `ghp_` defeating the payload-level pass.

        So the run gate now reads the media ALT rather than its URL. It sees the
        contiguity the output can actually have, and errs toward MORE contiguity
        than the output has when the brackets do survive, which is the safe way to
        be wrong.
        """
        head = "ghp_Ab3Df6Hj9Kl2Np5Qr8Tv"
        tail = "Wx4Yz7Bc0Ef3"
        blob = "Xk7Qm2Rt9Wz4Yb6Nc1Vf8Hj3Lp5Sd0Ag7Ke4Ou2B"
        for url in (
            "https://ex.com/a.png",
            # Fails the destination scan (whitespace is encoded there), so the
            # link is dropped and the alt would be emitted bare.
            f"https://evil.example.com/a b?data={blob}",
        ):
            adf = self._doc(
                self._para(
                    self._text(head),
                    {"type": "media", "attrs": {"type": "external", "url": url, "alt": tail}},
                )
            )
            rendered = source._adf_to_markdown(adf)
            assert (head + tail) not in rendered.replace("\\", ""), url
            assert "REDACTED: credential" in rendered, url

    def test_an_ordinary_media_alt_still_renders_as_a_link(self):
        """The gate reading the alt must not cost the ordinary case."""
        adf = self._doc(
            self._para(
                self._text("before "),
                {
                    "type": "media",
                    "attrs": {"type": "external", "url": "https://ex.com/a.png", "alt": "shot"},
                },
            )
        )
        assert source._adf_to_markdown(adf) == "before [shot](https://ex.com/a.png)"

    def test_line_leading_list_marker_in_text_is_escaped(self):
        adf = self._doc(self._para(self._text("- not a list")), self._para(self._text("1. nor this")))
        assert source._adf_to_markdown(adf) == "\\- not a list\n\n1\\. nor this"

    def test_a_setext_underline_in_text_cannot_promote_the_line_above(self):
        """A line of `=` or `-` under a paragraph line makes it a heading, so both
        underline characters have to be escaped, not just the list-marker one."""
        adf = self._doc(self._para(self._text("Title\n===")), self._para(self._text("Sub\n---")))
        assert source._adf_to_markdown(adf) == "Title\n\\===\n\nSub\n\\---"

    def test_pipe_in_a_table_cell_is_escaped(self):
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [self._para(self._text("a|b"))],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| a\\|b |")

    def test_line_expansion_guard_refuses_a_projected_overflow(self):
        """Per-line expansion is checked by projection, before any allocation."""
        many = "x\n" * 100000
        with pytest.raises(source.SourceProviderError):
            source._md_guard_line_expansion(many, 120)
        # The same text with a two-character indent projects well under the cap.
        source._md_guard_line_expansion(many, 2)

    def test_deeply_nested_quotes_over_the_ceiling_are_refused_not_rendered(self):
        """Newlines inside one text node cost ~3 payload bytes each while 60
        levels of nesting adds 120 characters to every one, so a small document
        can project past the payload ceiling. It must raise, not allocate."""
        inner = {"type": "paragraph", "content": [{"type": "text", "text": "x\n" * 100000}]}
        node = {"type": "blockquote", "content": [inner]}
        for _ in range(59):
            node = {"type": "blockquote", "content": [node]}
        with pytest.raises(source.SourceProviderError):
            source._adf_to_markdown(self._doc(node))

    def test_nested_blockquotes_get_one_marker_per_level(self):
        """A chain of single-child quotes is prefixed in one pass, so the marker
        count must still match the nesting depth."""
        node = {"type": "blockquote", "content": [self._para(self._text("deep"))]}
        for _ in range(2):
            node = {"type": "blockquote", "content": [node]}
        assert source._adf_to_markdown(self._doc(node)) == "> > > deep"

    def test_code_span_whitespace_survives_cell_flattening(self):
        """A cell is folded to one line by collapsing NEWLINES only -- a code
        span's repeated spaces are literal content."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    self._para(self._text("a  b", [{"type": "code"}]))
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| `a  b` |")

    def test_pipe_inside_a_code_span_in_a_cell_is_escaped(self):
        """A code span is emitted literally, so its pipe would split the cell.
        GFM honours a backslash-escaped pipe inside a code span."""
        adf = self._doc(
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {
                                "type": "tableHeader",
                                "content": [
                                    self._para(self._text("a|b", [{"type": "code"}]))
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert source._adf_to_markdown(adf).startswith("| `a\\|b` |")

    def test_empty_and_non_dict_returns_empty(self):
        assert source._adf_to_markdown(None) == ""
        assert source._adf_to_markdown("just a string") == ""
        assert source._adf_to_markdown({}) == ""

    def test_unknown_node_type_still_contributes_its_text(self):
        adf = self._doc(
            {"type": "someFutureNode", "content": [self._text("kept")]},
        )
        assert source._adf_to_markdown(adf) == "kept"

    def test_traversal_is_depth_limited(self):
        def nest(levels):
            node = self._para(self._text("deep"))
            for _ in range(levels):
                node = {"type": "blockquote", "content": [node]}
            return self._doc(node)

        assert "deep" in source._adf_to_markdown(nest(3))
        assert "deep" not in source._adf_to_markdown(nest(200))

    @staticmethod
    def _nest(container, levels):
        """Wrap a 'deep' paragraph in *levels* nested *container* blocks."""
        node = {"type": "paragraph", "content": [{"type": "text", "text": "deep"}]}
        for _ in range(levels):
            if container == "blockquote":
                node = {"type": "blockquote", "content": [node]}
            elif container == "taskList":
                node = {"type": "taskList", "content": [{"type": "taskItem", "content": [node]}]}
            elif container == "table":
                node = {
                    "type": "table",
                    "content": [
                        {
                            "type": "tableRow",
                            "content": [{"type": "tableCell", "content": [node]}],
                        }
                    ],
                }
            elif container == "someFutureInline":
                node = {"type": "paragraph", "content": [{"type": "someFutureInline", "content": [node]}]}
            elif container in ("layoutSection", "bodiedExtension", "decisionList"):
                node = {"type": container, "content": [node]}
            else:
                node = {"type": container, "content": [{"type": "listItem", "content": [node]}]}
        return {"type": "doc", "content": [node]}

    @pytest.mark.parametrize(
        "container",
        [
            "blockquote",
            "bulletList",
            "orderedList",
            "taskList",
            "table",
            "someFutureInline",
            "layoutSection",
            "bodiedExtension",
            "decisionList",
        ],
    )
    def test_every_nesting_container_respects_the_depth_limit(self, container):
        """No recursing container may reach its renderer past the guarded entry.

        The depth cap lives in `_adf_to_markdown`, so a container whose renderer
        recursed straight back into the block renderer would skip the cap and
        exhaust the stack on a deeply nested document.
        """
        assert "deep" in source._adf_to_markdown(self._nest(container, 3))
        assert "deep" not in source._adf_to_markdown(self._nest(container, 350))

    def test_realistic_description_round_trips_to_markdown(self):
        """One document exercising every structure a Jira description carries."""
        adf = self._doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [self._text("Problem")]},
            self._para(
                self._text("The "),
                self._text("fetch_issue", [{"type": "code"}]),
                self._text(" helper drops "),
                self._text("every", [{"type": "strong"}]),
                self._text(" mark."),
            ),
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [self._para(self._text("headings"))]},
                    {
                        "type": "listItem",
                        "content": [
                            self._para(
                                self._text("links like "),
                                self._text(
                                    "the docs",
                                    [
                                        {
                                            "type": "link",
                                            "attrs": {"href": "https://example.com/docs"},
                                        }
                                    ],
                                ),
                            )
                        ],
                    },
                ],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "python"},
                "content": [self._text("x = 1")],
            },
            {
                "type": "table",
                "content": [
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableHeader", "content": [self._para(self._text("a"))]},
                            {"type": "tableHeader", "content": [self._para(self._text("b"))]},
                        ],
                    },
                    {
                        "type": "tableRow",
                        "content": [
                            {"type": "tableCell", "content": [self._para(self._text("1"))]},
                            {"type": "tableCell", "content": [self._para(self._text("2"))]},
                        ],
                    },
                ],
            },
            {"type": "rule"},
            self._para(self._text("See "), {"type": "mention", "attrs": {"text": "Alice"}}),
        )
        expected = "\n".join(
            [
                "## Problem",
                "",
                "The `fetch_issue` helper drops **every** mark.",
                "",
                "- headings",
                "- links like [the docs](https://example.com/docs)",
                "",
                "```python",
                "x = 1",
                "```",
                "",
                "| a | b |",
                "| --- | --- |",
                "| 1 | 2 |",
                "",
                "---",
                "",
                "See @Alice",
            ]
        )
        assert source._adf_to_markdown(adf) == expected
