from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import re
import time
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from source_providers_helpers import _clear_issue_cache  # noqa: F401
from source_providers_helpers import _mock_source_sel_fixture  # noqa: F401
from source_providers_helpers import _no_visibility_refresh_side_task  # noqa: F401
from source_providers_helpers import _app

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
            imported |= set(re.findall(r"from '((?:remark|rehype)-[a-z0-9-]+)'", path.read_text()))
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


class TestGetJiraAuth:
    """Credential resolution for Jira hosts."""

    def test_returns_none_when_no_config(self, monkeypatch):
        """No entries → None."""

        class FakeDashboard:
            jira_auth = []

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_matches_host_case_insensitively(self, monkeypatch):
        class FakeEntry:
            host = "Acme.atlassian.net"
            email = "user@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "secret123"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("user@acme.com", "secret123")

    def test_strips_port_443(self, monkeypatch):
        class FakeEntry:
            host = "jira.internal:443"
            email = ""

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "pat-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("jira.internal")
        assert result == ("", "pat-token")

    def test_raises_value_error_on_config_load_failure(self, monkeypatch):
        """Config errors propagate as ValueError, not silent None."""

        class BrokenConfig:
            @classmethod
            def load(cls):
                raise RuntimeError("corrupt config.json")

        monkeypatch.setattr(source, "KiroCrewConfig", BrokenConfig)
        with pytest.raises(ValueError, match="jira_config_error"):
            source._get_jira_auth("acme.atlassian.net")

    def test_per_host_token_takes_precedence(self, monkeypatch):
        """JIRA_TOKEN_<hex> is preferred over global JIRA_API_TOKEN."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                host_key = "acme.atlassian.net".encode().hex().upper()
                return {
                    "JIRA_API_TOKEN": "global-fallback",
                    f"JIRA_TOKEN_{host_key}": "per-host-secret",
                }

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "per-host-secret")

    def test_seeded_global_env_does_not_bypass_per_host_vault(self, monkeypatch):
        """A global JIRA_API_TOKEN that load_credentials merely SEEDED into
        os.environ (setdefault), not a real pre-existing operator override,
        must NOT be treated as a live override: a host with its own per-host
        vault token still gets that per-host token. The snapshot is captured
        BEFORE load_credentials runs, so a value that did not exist in the
        environment beforehand is not seen as an override."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        # No REAL operator override present before the call.
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # Simulate load_credentials' setdefault seeding the .env global
                # into the process environment during the call. Use monkeypatch
                # so the seed is auto-reverted at test teardown and cannot leak
                # into later tests (a raw os.environ.setdefault would persist).
                monkeypatch.setenv("JIRA_API_TOKEN", "seeded-global")
                return {"JIRA_API_TOKEN": "seeded-global"}

        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "per-host-vault" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Per-host vault token wins; the seeded global is ignored.
        assert result == ("dev@acme.com", "per-host-vault")

    def test_vault_token_preferred_over_env(self, monkeypatch):
        """A vault secret wins over the legacy .env value for the same host."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        host_key = "acme.atlassian.net".encode().hex().upper()
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-token" if name == f"JIRA_TOKEN_{host_key}" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-token")

    def test_vault_miss_falls_back_to_env(self, monkeypatch):
        """When the vault has no entry, the .env / environ value is used."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": "env-token"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-token")

    def test_vault_single_host_global_token(self, monkeypatch):
        """Single host with no per-host vault entry uses the global vault secret."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "vault-global" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "vault-global")

    def test_global_env_override_beats_stale_global_vault(self, monkeypatch):
        """A nonempty process-environment JIRA_API_TOKEN overrides even a stale
        global vault entry.

        `load_credentials` overlays `os.environ` over the .env for this key, so
        a live env var is the effective credential — and `secrets import` skips
        migrating the key while such an override is set. A vault entry left by an
        EARLIER migration must NOT shadow that override under vault-first
        resolution. Per-host keys are unaffected (not env-overlaid)."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # env overlay would also place it here; the resolver reads the
                # override directly from os.environ before the global vault.
                return {"JIRA_API_TOKEN": "env-override"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Stale global vault entry that must NOT win.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "stale-vault" if name == "JIRA_API_TOKEN" else "",
        )
        monkeypatch.setenv("JIRA_API_TOKEN", "env-override")
        result = source._get_jira_auth("acme.atlassian.net")
        assert result == ("dev@acme.com", "env-override")

    def test_catalog_slots_match_runtime_precedence(self, monkeypatch):
        """Catalog output names the same vault slot the runtime resolves."""
        from kiro_crew.dashboard.handlers.secrets import _managed_secret_catalog

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
        host_name = source.jira_host_token_name(FakeEntry.host)
        cases = (
            ([host_name], {host_name: "host-value"}, host_name),
            ([], {"JIRA_API_TOKEN": "global-value"}, "JIRA_API_TOKEN"),
        )
        for stored_names, vault_values, expected_name in cases:
            monkeypatch.setattr(
                source,
                "_resolve_jira_token_from_vault",
                lambda name, values=vault_values: values.get(name, ""),
            )
            assert source._get_jira_auth(FakeEntry.host) == (
                FakeEntry.email,
                vault_values[expected_name],
            )
            catalog = _managed_secret_catalog(
                stored_names,
                [FakeEntry.host],
                jira_global_applicable=True,
                wakatime_enabled=False,
            )
            assert expected_name in {entry["name"] for entry in catalog}

    def test_migrated_secret_ref_in_env_resolves_from_vault_not_uri(self, monkeypatch):
        """After `secrets import --apply`, the .env line is
        `JIRA_API_TOKEN=secret://JIRA_API_TOKEN` and `load_credentials`
        propagates that ref into os.environ AND the creds dict. The resolver
        must NOT hand the `secret://` URI to Jira as the token — it must treat
        it as a vault reference and resolve the real secret from the vault."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                # load_credentials overlays the migrated secret:// ref here too.
                return {"JIRA_API_TOKEN": "secret://JIRA_API_TOKEN"}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: "real-vault-secret" if name == "JIRA_API_TOKEN" else "",
        )
        # The migrated ref is propagated into the environment by load_credentials.
        monkeypatch.setenv("JIRA_API_TOKEN", "secret://JIRA_API_TOKEN")
        result = source._get_jira_auth("acme.atlassian.net")
        # The vault secret is used — NOT the secret:// URI.
        assert result == ("dev@acme.com", "real-vault-secret")

    def test_returns_none_when_no_token_anywhere(self, monkeypatch):
        """Configured host but neither vault nor env holds a token → None."""

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(source, "_resolve_jira_token_from_vault", lambda name: "")
        assert source._get_jira_auth("acme.atlassian.net") is None

    def test_env_override_equal_to_env_file_is_not_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] equals the .env file value (i.e. it
        was seeded there by GatewayOrchestrator's startup load_credentials call),
        it must NOT beat a vault entry — the vault's rotated value should win.

        This is the Finding 2 fix: a value that merely came from .env via
        load_credentials' setdefault is NOT a genuine operator override.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _VAULT_TOKEN = "fresh-vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _ENV_FILE_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _ENV_FILE_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        # Per-host vault returns nothing; global vault has the rotated token.
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: (
                _VAULT_TOKEN if name == "JIRA_API_TOKEN" else ""
            ),
        )
        # .env file contains the same value as os.environ (startup-seeded).
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # Vault token must win; the .env-seeded env value must NOT override it.
        assert result == ("dev@acme.com", _VAULT_TOKEN), (
            "Vault token should win when env value equals .env file value "
            f"(startup-seeded); got {result}"
        )

    def test_env_override_differing_from_env_file_is_genuine_override(self, monkeypatch):
        """When os.environ['JIRA_API_TOKEN'] DIFFERS from the .env file value,
        the operator explicitly set it at runtime — it must beat the vault entry.
        """

        class FakeEntry:
            host = "acme.atlassian.net"
            email = "dev@acme.com"

        class FakeDashboard:
            jira_auth = [FakeEntry()]

        _ENV_FILE_TOKEN = "stale-env-token"
        _OPERATOR_TOKEN = "operator-set-at-runtime"
        _VAULT_TOKEN = "vault-token"

        monkeypatch.setenv("JIRA_API_TOKEN", _OPERATOR_TOKEN)

        class FakeConfig:
            dashboard = FakeDashboard()

            @classmethod
            def load(cls):
                return cls()

            def load_credentials(self):
                return {"JIRA_API_TOKEN": _OPERATOR_TOKEN}

        monkeypatch.setattr(source, "KiroCrewConfig", FakeConfig)
        monkeypatch.setattr(
            source,
            "_resolve_jira_token_from_vault",
            lambda name: _VAULT_TOKEN if name == "JIRA_API_TOKEN" else "",
        )
        # .env file contains a different (older) value — operator set a new one.
        monkeypatch.setattr(
            source,
            "read_env_file_credential",
            lambda key: _ENV_FILE_TOKEN if key == "JIRA_API_TOKEN" else "",
        )
        result = source._get_jira_auth("acme.atlassian.net")
        # The differing env value is a genuine override; it must win over vault.
        assert result == ("dev@acme.com", _OPERATOR_TOKEN), (
            "Operator runtime override should win over vault when it differs "
            f"from .env file value; got {result}"
        )


class TestJiraIsCloud:
    def test_cloud_host(self):
        assert source._jira_is_cloud("acme.atlassian.net") is True

    def test_server_host(self):
        assert source._jira_is_cloud("jira.internal.corp") is False

    def test_subdomain_required_for_cloud(self):
        # The URL parser already rejects bare .atlassian.net, but _jira_is_cloud
        # is about suffix matching, not URL parsing. Any valid Cloud host has a
        # non-empty org prefix.
        assert source._jira_is_cloud("x.atlassian.net") is True


@pytest.mark.asyncio
async def test_fetch_jira_issue_no_credentials_raises_value_error(monkeypatch):
    """When no credentials are configured, a ValueError with the expected prefix is raised."""
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: None)
    ref = source.SourceRef(
        provider="jira",
        url="https://acme.atlassian.net/browse/PROJ-123",
        host="acme.atlassian.net",
        owner="",
        repo="PROJ",
        number=123,
        kind="issue",
    )
    with pytest.raises(ValueError, match="jira_no_credentials"):
        await source._fetch_jira_issue(ref)


def test_parse_jira_cloud_url() -> None:
    """Jira Cloud URLs parse correctly."""
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-42")
    assert ref.provider == "jira"
    assert ref.host == "acme.atlassian.net"
    assert ref.repo == "PROJ"
    assert ref.number == 42
    assert ref.kind == "issue"
    assert ref.url == "https://acme.atlassian.net/browse/PROJ-42"


def test_parse_jira_self_hosted_url(monkeypatch) -> None:
    """Self-hosted Jira URLs parse when host is in the allowlist."""
    monkeypatch.setattr(source, "_allowed_jira_hosts", lambda: frozenset({"jira.internal.corp"}))
    ref = source.parse_source_url("https://jira.internal.corp/browse/TEAM-99")
    assert ref.provider == "jira"
    assert ref.host == "jira.internal.corp"
    assert ref.repo == "TEAM"
    assert ref.number == 99


# --- Jira linked issues ---


class TestJiraLinkedChanges:
    """Tests for _jira_linked_changes parsing."""

    def test_outward_link_parsed(self) -> None:
        """An outward issue link is parsed with the correct relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "PROJ-456",
                        "fields": {
                            "summary": "Blocked task",
                            "status": {"statusCategory": {"key": "new"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1
        assert result[0]["provider"] == "jira"
        assert result[0]["url"] == "https://acme.atlassian.net/browse/PROJ-456"
        assert result[0]["number"] == 456
        assert result[0]["title"] == "Blocked task"
        assert result[0]["state"] == "open"
        assert result[0]["relation"] == "blocks"
        assert result[0]["issueKey"] == "PROJ-456"

    def test_inward_link_parsed(self) -> None:
        """An inward issue link is parsed with the inward relation label."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "inwardIssue": {
                        "key": "TEAM-10",
                        "fields": {
                            "summary": "Upstream dep",
                            "status": {"statusCategory": {"key": "indeterminate"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://jira.corp/jira")
        assert len(result) == 1
        assert result[0]["relation"] == "is blocked by"
        assert result[0]["url"] == "https://jira.corp/jira/browse/TEAM-10"
        assert result[0]["state"] == "open"
        assert result[0]["issueKey"] == "TEAM-10"

    def test_done_status_maps_to_closed(self) -> None:
        """A linked issue with statusCategory 'done' maps to state 'closed'."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
                    "outwardIssue": {
                        "key": "FIX-7",
                        "fields": {
                            "summary": "Done fix",
                            "status": {"statusCategory": {"key": "done"}},
                        },
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["state"] == "closed"

    def test_duplicate_keys_deduped(self) -> None:
        """Duplicate issue keys are folded."""
        link = {
            "type": {"name": "Relates", "inward": "relates to", "outward": "relates to"},
            "outwardIssue": {
                "key": "DUP-1",
                "fields": {"summary": "Dup", "status": {"statusCategory": {"key": "new"}}},
            },
        }
        fields = {"issuelinks": [link, link]}
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert len(result) == 1

    def test_empty_issuelinks(self) -> None:
        """Empty or missing issuelinks returns an empty list."""
        assert source._jira_linked_changes({}, "https://x") == []
        assert source._jira_linked_changes({"issuelinks": []}, "https://x") == []

    def test_malformed_link_skipped(self) -> None:
        """A link with neither inwardIssue nor outwardIssue is skipped."""
        fields = {
            "issuelinks": [
                {"type": {"name": "Bad", "inward": "x", "outward": "y"}},
                "not a dict",
                None,
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result == []

    def test_missing_summary_uses_key_as_title(self) -> None:
        """When summary is missing, the issue key is used as the title."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Rel", "inward": "r", "outward": "r"},
                    "outwardIssue": {
                        "key": "NO-SUM-1",
                        "fields": {"status": {"statusCategory": {"key": "new"}}},
                    },
                }
            ]
        }
        result = source._jira_linked_changes(fields, "https://acme.atlassian.net")
        assert result[0]["title"] == "NO-SUM-1"

    def test_multiple_links(self) -> None:
        """Multiple links are all returned in order."""
        fields = {
            "issuelinks": [
                {
                    "type": {"name": "Blocks", "inward": "is blocked by", "outward": "blocks"},
                    "outwardIssue": {
                        "key": "A-1",
                        "fields": {"summary": "First", "status": {"statusCategory": {"key": "new"}}},
                    },
                },
                {
                    "type": {"name": "Duplicates", "inward": "is duplicated by", "outward": "duplicates"},
                    "inwardIssue": {
                        "key": "B-2",
                        "fields": {"summary": "Second", "status": {"statusCategory": {"key": "done"}}},
                    },
                },
            ]
        }
        result = source._jira_linked_changes(fields, "https://x.atlassian.net")
        assert len(result) == 2
        assert result[0]["issueKey"] == "A-1"
        assert result[0]["relation"] == "blocks"
        assert result[1]["issueKey"] == "B-2"
        assert result[1]["relation"] == "is duplicated by"
        assert result[1]["state"] == "closed"


# --- Jira fix versions as the milestone equivalent ---


class TestJiraFixVersionMilestone:
    """Tests for _jira_fix_versions and _jira_fix_version_milestone."""

    def test_unreleased_version_maps_to_open_milestone(self) -> None:
        """name -> title, releaseDate -> dueOn, unreleased -> open."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0]) == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }

    def test_released_version_maps_to_closed(self) -> None:
        """A shipped release is a closed milestone."""
        version = {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_archived_version_maps_to_closed(self) -> None:
        """An archived version takes no work, so it is not open."""
        version = {"name": "1.0.0", "released": False, "archived": True}
        assert source._jira_fix_version_milestone(version)["state"] == "closed"

    def test_missing_release_date_keeps_the_name(self) -> None:
        """A version with no release date still carries its target release."""
        version = {"name": "Next", "released": False}
        assert source._jira_fix_version_milestone(version) == {
            "title": "Next",
            "state": "open",
            "dueOn": "",
        }

    def test_locale_formatted_release_date_is_not_used(self) -> None:
        """userReleaseDate is locale-formatted, so only releaseDate feeds dueOn."""
        version = {"name": "3.0", "releaseDate": "2026-12-01", "userReleaseDate": "01/Dec/26"}
        assert source._jira_fix_version_milestone(version)["dueOn"] == "2026-12-01"

    def test_first_usable_version_wins_over_malformed_head(self) -> None:
        """A malformed or nameless leading entry does not hide a usable one."""
        fields = {
            "fixVersions": [
                "not a dict",
                None,
                {"name": "   "},
                {"name": "", "releaseDate": "2026-01-01"},
                {"name": "4.1", "releaseDate": "2026-11-05"},
            ]
        }
        versions = source._jira_fix_versions(fields)
        assert len(versions) == 1
        assert source._jira_fix_version_milestone(versions[0])["title"] == "4.1"

    def test_name_is_trimmed(self) -> None:
        """Surrounding whitespace in a version name is not rendered."""
        version = {"name": "  2.5.0  "}
        assert source._jira_fix_version_milestone(version)["title"] == "2.5.0"

    def test_no_fix_versions_yields_nothing_usable(self) -> None:
        """Missing, empty, and non-list fixVersions all yield no milestone."""
        assert source._jira_fix_versions({}) == []
        assert source._jira_fix_versions({"fixVersions": []}) == []
        assert source._jira_fix_versions({"fixVersions": None}) == []
        assert source._jira_fix_versions({"fixVersions": "1.0"}) == []

    def test_multiple_versions_are_all_returned(self) -> None:
        """The filter keeps every usable version so the caller can flag partial."""
        fields = {
            "fixVersions": [
                {"name": "2.4.0"},
                {"name": "2.5.0"},
            ]
        }
        assert [v["name"] for v in source._jira_fix_versions(fields)] == ["2.4.0", "2.5.0"]

    def test_milestone_keys_match_the_frontend_contract(self) -> None:
        """The mapped shape is exactly IssueMilestone: title, state, dueOn."""
        version = {"name": "2.4.0", "releaseDate": "2026-09-30"}
        assert set(source._jira_fix_version_milestone(version)) == {"title", "state", "dueOn"}


class TestJiraPickFixVersion:
    """Tests for _jira_pick_fix_version."""

    def test_mixed_versions_prefer_the_unreleased_one(self) -> None:
        """A pending release wins over an already-shipped one ahead of it."""
        fix_versions = [
            {"name": "2.3.0", "released": True},
            {"name": "2.4.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.4.0"

    def test_all_released_falls_back_to_the_first(self) -> None:
        """A ticket whose versions all shipped still shows one milestone."""
        fix_versions = [
            {"name": "2.2.0", "released": True},
            {"name": "2.3.0", "archived": True},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "2.2.0"

    def test_empty_list_yields_none(self) -> None:
        """No usable fix versions means no milestone."""
        assert source._jira_pick_fix_version([]) is None

    def test_archived_is_not_pending(self) -> None:
        """An archived-but-unreleased version does not count as pending."""
        fix_versions = [
            {"name": "1.0.0", "released": False, "archived": True},
            {"name": "1.1.0", "released": False},
        ]
        chosen = source._jira_pick_fix_version(fix_versions)
        assert chosen is not None
        assert chosen["name"] == "1.1.0"


class _JiraFakeContent:
    """Minimal ``StreamReader`` stand-in for the capped-response reader."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def iter_chunked(self, _n: int):
        if self._body:
            yield self._body


class _JiraFakeResponse:
    def __init__(self, body: bytes) -> None:
        self.status = 200
        self.content = _JiraFakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _JiraRecordingSession:
    """Fake ``aiohttp.ClientSession`` that records the URL it was asked for."""

    def __init__(self, body: bytes, seen: list[str]) -> None:
        self._body = body
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def get(self, url, **_kwargs):
        self._seen.append(url)
        return _JiraFakeResponse(self._body)


def _jira_recording_session(monkeypatch, fields: dict) -> list[str]:
    """Arm a fake Jira endpoint serving *fields*; return the recorded URL list."""
    body = json.dumps({"fields": fields}).encode("utf-8")
    seen: list[str] = []
    monkeypatch.setattr(
        source.aiohttp, "ClientSession", lambda *a, **kw: _JiraRecordingSession(body, seen)
    )
    monkeypatch.setattr(source, "_get_jira_auth", lambda host: ("e@example.com", "tok"))
    return seen


async def _jira_fetch(monkeypatch, fields: dict) -> tuple[dict, list[str]]:
    """Drive ``_fetch_jira_issue`` over a canned payload; return it and the URLs."""
    seen = _jira_recording_session(monkeypatch, fields)
    ref = source.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")
    return await source._fetch_jira_issue(ref), seen


class TestJiraFixVersionInPayload:
    """End-to-end pins: the field is requested and reaches the payload."""

    @pytest.mark.asyncio
    async def test_fix_versions_is_requested_from_the_rest_api(self, monkeypatch) -> None:
        """An unrequested field is absent from the response, so it must be asked for."""
        _issue, seen = await _jira_fetch(monkeypatch, {"summary": "x"})
        assert len(seen) == 1
        assert "fixVersions" in seen[0]

    @pytest.mark.asyncio
    async def test_milestone_carries_the_fix_version(self, monkeypatch) -> None:
        """The Issues panel milestone chip is fed by the first fix version."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it",
                "fixVersions": [{"name": "2.4.0", "releaseDate": "2026-09-30"}],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" not in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_multiple_fix_versions_are_declared_partial(self, monkeypatch) -> None:
        """Dropping the tail of a multi-release ticket is disclosed, not silent."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Ship it twice",
                "fixVersions": [{"name": "2.4.0"}, {"name": "2.5.0"}],
            },
        )
        assert issue["milestone"]["title"] == "2.4.0"
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_pending_version_wins_over_a_shipped_one(self, monkeypatch) -> None:
        """A released version ahead of a pending one does not steal the chip."""
        issue, _seen = await _jira_fetch(
            monkeypatch,
            {
                "summary": "Fixed in the next release",
                "fixVersions": [
                    {"name": "2.3.0", "releaseDate": "2026-06-30", "released": True},
                    {"name": "2.4.0", "releaseDate": "2026-09-30", "released": False},
                ],
            },
        )
        assert issue["milestone"] == {
            "title": "2.4.0",
            "state": "open",
            "dueOn": "2026-09-30",
        }
        assert "fix versions" in issue["partialSections"]

    @pytest.mark.asyncio
    async def test_no_fix_version_leaves_milestone_null(self, monkeypatch) -> None:
        """A ticket with no fix version keeps the panel's 'no milestone' state."""
        issue, _seen = await _jira_fetch(monkeypatch, {"summary": "Unscheduled"})
        assert issue["milestone"] is None
        assert "fix versions" not in issue["partialSections"]


class _ReapProbe:
    """A PIPE-stdio child double that records how it is reaped.

    A killed child blocking on a write into a full pipe -- or a surviving
    descendant still holding the pipes open -- makes a bare ``await
    proc.wait()`` hang the caller forever. The bounded reap must
    therefore drain the pipes via ``communicate()`` and must never touch
    ``wait()``.
    """

    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: "int | None" = None
        self.kill_calls = 0
        self.wait_calls = 0
        self.communicate_calls = 0

    async def communicate(self):
        self.communicate_calls += 1
        self.returncode = -9
        return b"", b""

    def kill(self) -> None:
        self.kill_calls += 1

    async def wait(self) -> int:
        self.wait_calls += 1
        return -9


@pytest.mark.asyncio
async def test_terminate_process_reaps_via_communicate_not_wait(monkeypatch):
    """``_terminate_process`` must route through the bounded, pipe-draining
    ``kill_and_reap`` -- a bare ``await proc.wait()`` here can hang the gateway
    task forever when the child is killed with a full pipe."""
    from kiro_crew import platform_compat

    proc = _ReapProbe()
    tree_kills: "list[tuple[int, int]]" = []

    async def _fake_tree(pid, sig):
        tree_kills.append((pid, sig))
        return True

    # Fake pid + patched tree kill so no test can reach a real killpg. Both the
    # async helper (used by kill_and_reap) and the legacy sync entry point are
    # patched so the pin stays safe even when run against unmodified code.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", _fake_tree)
    monkeypatch.setattr(
        platform_compat, "kill_process_tree", lambda *a, **k: tree_kills.append(a)
    )

    await source._terminate_process(proc)

    assert proc.communicate_calls == 1
    assert proc.wait_calls == 0
    assert tree_kills and tree_kills[0][0] == proc.pid


def test_parse_repo_url_accepts_github_repo() -> None:
    ref = source.parse_repo_url("https://github.com/acme/console")
    assert ref.provider == "github"
    assert ref.host == "github.com"
    assert ref.owner == "acme"
    assert ref.repo == "console"


def test_parse_repo_url_strips_git_suffix_and_trailing_slash() -> None:
    ref = source.parse_repo_url("https://github.com/acme/repo.git/")
    assert (ref.owner, ref.repo) == ("acme", "repo")


def test_parse_repo_url_rejects_pull_request_url() -> None:
    # A pull URL has extra path segments and is not a repository root.
    with pytest.raises(ValueError):
        source.parse_repo_url("https://github.com/acme/repo/pull/12")


def test_parse_repo_url_rejects_userinfo() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("https://user:pass@github.com/acme/repo")


def test_parse_repo_url_rejects_non_https() -> None:
    with pytest.raises(ValueError):
        source.parse_repo_url("http://github.com/acme/repo")


def test_parse_repo_url_rejects_non_allowlisted_host(monkeypatch) -> None:
    # Cold allowlist snapshot: a self-managed host is not recognized (fails closed).
    monkeypatch.setattr(source, "_gitlab_hosts_snapshot", frozenset())
    with pytest.raises(ValueError):
        source.parse_repo_url("https://git.internal.example.com/acme/repo")


def test_parse_repo_url_accepts_public_gitlab() -> None:
    ref = source.parse_repo_url("https://gitlab.com/group/subgroup/project")
    assert ref.provider == "gitlab"
    assert ref.owner == "group/subgroup"
    assert ref.repo == "project"


@pytest.mark.asyncio
async def test_fetch_app_contributors_uses_profile_name(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            return [
                {"login": "octocat", "avatar_url": "https://avatars.example/1"},
                {"login": "hubot", "avatar_url": "https://avatars.example/2"},
            ]
        if command.endswith("users/octocat"):
            return {"name": "The Octocat"}
        if command.endswith("users/hubot"):
            return {"name": None}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    assert result == [
        {
            "login": "octocat",
            "name": "The Octocat",
            "avatarUrl": "https://avatars.example/1",
            "profileUrl": "https://github.com/octocat",
        },
        {
            "login": "hubot",
            "name": "hubot",  # null profile name falls back to the login
            "avatarUrl": "https://avatars.example/2",
            "profileUrl": "https://github.com/hubot",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_app_contributors_non_github_returns_empty(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    run = AsyncMock()
    monkeypatch.setattr(source, "_run_json", run)

    assert await source.fetch_app_contributors("https://gitlab.com/group/project") == []
    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_app_contributors_coalesces_concurrent_calls(monkeypatch) -> None:
    source._contributors_cache.clear()
    source._contributors_inflight.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    calls = {"n": 0}

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        if "/contributors?" in command:
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return [{"login": "octocat", "avatar_url": "a"}]
        if command.endswith("users/octocat"):
            return {"name": "Octo"}
        raise AssertionError(command)

    monkeypatch.setattr(source, "_run_json", fake_run)
    first, second = await asyncio.gather(
        source.fetch_app_contributors("https://github.com/acme/repo"),
        source.fetch_app_contributors("https://github.com/acme/repo"),
    )

    assert first == second
    assert calls["n"] == 1  # one shared provider fanout for both callers
    # A second identical read is served from cache without another provider call.
    await source.fetch_app_contributors("https://github.com/acme/repo")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_fetch_app_contributors_skips_profile_for_unsafe_login(monkeypatch) -> None:
    source._contributors_cache.clear()
    monkeypatch.setattr(source, "ensure_gitlab_hosts_loaded", AsyncMock(return_value=frozenset()))
    seen: list[str] = []

    async def fake_run(*argv: str, **kwargs: int):
        command = " ".join(argv)
        seen.append(command)
        if "/contributors?" in command:
            return [{"login": "../etc", "avatar_url": "a"}]
        raise AssertionError(f"unexpected profile lookup: {command}")

    monkeypatch.setattr(source, "_run_json", fake_run)
    result = await source.fetch_app_contributors("https://github.com/acme/repo")

    # A malformed login never reaches the users/<login> path; name falls back.
    assert result == [
        {
            "login": "../etc",
            "name": "../etc",
            "avatarUrl": "a",
            "profileUrl": "https://github.com/../etc",
        }
    ]
    assert not any("users/" in c for c in seen)


@pytest.mark.asyncio
async def test_app_contributors_endpoint_requires_owner_claim(monkeypatch) -> None:
    fetch = AsyncMock()
    monkeypatch.setattr(source, "fetch_app_contributors", fetch)

    async with TestClient(TestServer(_app(user="U_OTHER"))) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 403
        assert await response.json() == {"error": "forbidden"}
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_app_contributors_endpoint_returns_list_for_owner(monkeypatch) -> None:
    payload = [
        {
            "login": "octocat",
            "name": "Octo",
            "avatarUrl": "https://a/1",
            "profileUrl": "https://github.com/octocat",
        }
    ]
    monkeypatch.setattr(source, "fetch_app_contributors", AsyncMock(return_value=payload))

    async with TestClient(TestServer(_app())) as client:
        response = await client.post(
            "/api/source/contributors", json={"url": "https://github.com/acme/repo"}
        )
        assert response.status == 200
        assert await response.json() == {"contributors": payload}


@pytest.mark.asyncio
async def test_app_contributors_endpoint_maps_bad_url_to_400(monkeypatch) -> None:
    monkeypatch.setattr(
        source, "fetch_app_contributors", AsyncMock(side_effect=ValueError("bad url"))
    )

    async with TestClient(TestServer(_app())) as client:
        response = await client.post("/api/source/contributors", json={"url": "nope"})
        assert response.status == 400
        assert (await response.json())["error"] == "bad url"
