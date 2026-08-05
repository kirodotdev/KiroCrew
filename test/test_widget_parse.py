"""Widget-extraction tests (Python side of the parser pair).

``kiro_crew.widget_parse.parse_widgets`` must assign the SAME 0-based
``widgetIndex`` to the same widget as the frontend's ``parseBlocks``
(``website/src/hooks/useBlockAssembler.ts``), because that index feeds the slug
both sides derive. The fixtures in :data:`SHARED_FIXTURES` are duplicated in
``website/src/test/widgetSlug.test.ts`` (the ``parser parity`` block) with the
same expected index/content, so a divergence fails both suites.
"""

from __future__ import annotations

import pytest

from kiro_crew.widget_parse import _JS_TRIM_CHARS, _js_trim, mask_inline_code, parse_widgets

#: ``(label, raw_text, [(index, content, title, slug), ...])``.
SHARED_FIXTURES = [
    (
        "single multi-line widget",
        'Here you go:\n<mcwidget title="Chart">\n<div>hi</div>\n</mcwidget>\nDone.',
        [(0, "<div>hi</div>", "Chart", "")],
    ),
    (
        "single-line widget",
        '<mcwidget title="Inline"><b>x</b></mcwidget>',
        [(0, "<b>x</b>", "Inline", "")],
    ),
    (
        "two widgets get distinct indices",
        '<mcwidget title="A">1</mcwidget>\ntext\n<mcwidget title="B">2</mcwidget>',
        [(0, "1", "A", ""), (1, "2", "B", "")],
    ),
    (
        "explicit slug attribute is captured",
        '<mcwidget title="Saved" slug="my-artifact">body</mcwidget>',
        [(0, "body", "Saved", "my-artifact")],
    ),
    (
        "attribute order is free",
        '<mcwidget slug="s1" title="T">body</mcwidget>',
        [(0, "body", "T", "s1")],
    ),
    (
        "no title falls back to Widget",
        "<mcwidget>body</mcwidget>",
        [(0, "body", "Widget", "")],
    ),
    (
        "backtick-quoted tag is not a widget",
        'Use `<mcwidget title="X">html</mcwidget>` to render.',
        [],
    ),
    (
        "widget inside a fenced code block is not a widget",
        '```html\n<mcwidget title="Doc">example</mcwidget>\n```',
        [],
    ),
    (
        "fence inside a widget body keeps the body opaque",
        '<mcwidget title="W">\n```\n</mcwidget>\n```\nreal body\n</mcwidget>',
        [(0, "```\n</mcwidget>\n```\nreal body", "W", "")],
    ),
    (
        # parseBlocks marks an unterminated widget complete on FINAL text
        # (flushWidget(!streaming)), so it renders a real WidgetFrame and must be
        # registered. Only a streaming partial becomes a placeholder.
        "unterminated widget is still emitted",
        '<mcwidget title="Open">\n<div>never closed',
        [(0, "<div>never closed", "Open", "")],
    ),
    (
        # REGRESSION: Python's `\w` is Unicode-aware, JS's is ASCII-only. With a
        # bare `\w`, Python treated ```例 as a fence (making the example inert and
        # returning the REAL widget at index 0) while JS saw plain markdown
        # (returning the EXAMPLE at index 0) — same slug, different content, so the
        # frontend linked and pinned an artifact holding HTML the user never
        # starred. Both sides must agree the example IS the widget at index 0.
        "non-ASCII fence info string is NOT a fence (JS \\w is ASCII-only)",
        "\n".join(
            [
                "以下のように書きます:",
                "```例",
                '<mcwidget title="サンプル">demo</mcwidget>',
                "```",
                "実際の結果:",
                '<mcwidget title="グラフ">REAL-CHART</mcwidget>',
            ]
        ),
        [(0, "demo", "サンプル", "")],
    ),
    (
        "a documented example does not shift the real widget's index",
        'Example: `<mcwidget>demo</mcwidget>`\n<mcwidget title="Real">body</mcwidget>',
        [(0, "body", "Real", "")],
    ),
]


class TestParseWidgets:
    @pytest.mark.parametrize(
        "label,raw,expected",
        [(f[0], f[1], f[2]) for f in SHARED_FIXTURES],
        ids=[f[0] for f in SHARED_FIXTURES],
    )
    def test_shared_fixture(self, label: str, raw: str, expected: list):
        got = parse_widgets(raw)
        assert [(w.index, w.content, w.title, w.slug) for w in got] == expected

    def test_no_widget_marker_short_circuits(self):
        assert parse_widgets("just prose, no widgets here") == []

    def test_empty_input(self):
        assert parse_widgets("") == []

    def test_indices_are_contiguous_from_zero(self):
        raw = "\n".join(f'<mcwidget title="W{i}">b{i}</mcwidget>' for i in range(5))
        got = parse_widgets(raw)
        assert [w.index for w in got] == [0, 1, 2, 3, 4]

    def test_trailing_unterminated_widget_is_flagged_and_indexed_last(self):
        """An open widget flushes with ``truncated=True`` at the end.

        Matches ``parseBlocks``'s ``flushWidget(!streaming)`` on final text: the
        frontend renders it, so it needs an artifact. It is always the last block,
        so its presence cannot shift an earlier index either way.
        """
        raw = '<mcwidget title="Done">a</mcwidget>\n<mcwidget title="Open">b'
        got = parse_widgets(raw)
        assert [(w.index, w.title, w.truncated) for w in got] == [
            (0, "Done", False),
            (1, "Open", True),
        ]

    def test_text_after_close_tag_on_same_line_is_not_swallowed(self):
        raw = '<mcwidget title="A">x</mcwidget> trailing prose'
        got = parse_widgets(raw)
        assert [(w.index, w.content) for w in got] == [(0, "x")]

    def test_content_before_close_tag_on_the_closing_line_is_kept(self):
        """A multi-line widget whose last line holds both body and close tag.

        ``parseBlocks`` appends the pre-close text to the widget body, so dropping
        it here would persist an artifact whose content differs from what the user
        sees rendered.
        """
        raw = '<mcwidget title="A">\n<div>one</div>\n<div>two</div></mcwidget>'
        got = parse_widgets(raw)
        assert [(w.index, w.content) for w in got] == [(0, "<div>one</div>\n<div>two</div>")]

    # ── Nested-fence depth tracking ─────────────────────────────────────────
    #
    # These mirror ``parseBlocks``'s ``fenceNestable`` / ``innerFenceDepth``
    # logic. A miscount here ends the outer fence early, so a subsequent
    # ``<mcwidget>`` that should be inert CODE gets promoted to a real widget —
    # which shifts every later widget's index and mis-keys its artifact slug.

    def test_nested_fence_in_markdown_block_does_not_end_outer_fence_early(self):
        raw = (
            "```markdown\n"
            "```python\n"
            "x = 1\n"
            "```\n"
            '<mcwidget title="Inert">still inside the outer fence</mcwidget>\n'
            "```\n"
        )
        assert parse_widgets(raw) == []

    def test_nested_fence_tracking_is_skipped_for_code_languages(self):
        """In a code fence, a ```lang line is literal content, not structural.

        Matches the frontend: only NESTABLE_LANGS (markup/doc) track depth, so a
        python fence's inner ```python closes nothing and the first bare ```
        still ends the block.
        """
        raw = (
            "```python\n"
            "# ```python\n"
            "```\n"
            '<mcwidget title="Real">after the fence</mcwidget>'
        )
        got = parse_widgets(raw)
        assert [(w.index, w.title) for w in got] == [(0, "Real")]

    def test_bare_inner_fence_without_language_does_not_increment_depth(self):
        """``parseBlocks`` requires a LANGUAGE on the inner open (``innerMatch[2]``).

        A bare ``` inside a nestable fence is the close, not a nested open.
        """
        raw = "```markdown\n```\n" '<mcwidget title="Real">out</mcwidget>'
        got = parse_widgets(raw)
        assert [(w.index, w.title) for w in got] == [(0, "Real")]

    def test_widget_after_unclosed_fence_at_eof_is_not_emitted(self):
        """An open plain fence swallows the rest, so no widget is found.

        parseBlocks flushes that trailing run as a CODE block, never a widget —
        so agreeing on "no widgets" is the parity-correct answer.
        """
        raw = "```html\n" '<mcwidget title="Inert">never escapes the fence</mcwidget>'
        assert parse_widgets(raw) == []


class TestJsTrimParity:
    """``_JS_TRIM_CHARS`` must equal ECMAScript's trim set, exactly.

    The frontend trims the widget body with JS ``.trim()``. Python's
    ``str.strip()`` removes a different set (notably it does NOT strip U+FEFF,
    which JS does), so a mismatch persists an artifact body that differs from
    what the user sees rendered.
    """

    def test_js_trim_matches_ecmascript_whitespace_set(self):
        expected = (
            set("\f\n\r\t\v")
            | {chr(0x20), chr(0xA0), chr(0x1680)}
            | {chr(c) for c in range(0x2000, 0x200B)}
            | {chr(0x2028), chr(0x2029), chr(0x202F), chr(0x205F), chr(0x3000), chr(0xFEFF)}
        )
        assert set(_JS_TRIM_CHARS) == expected

    def test_strips_bom_which_python_strip_does_not(self):
        body = "\ufeff<div>x</div>\ufeff"
        assert _js_trim(body) == "<div>x</div>"
        assert body.strip() == body, "guards the premise: str.strip() leaves the BOM"


class TestMaskInlineCode:
    def test_masks_balanced_run_including_delimiters(self):
        assert mask_inline_code("a `b` c") == "a     c"

    def test_preserves_length(self):
        for line in ["`a`", "no ticks", "``x`` y", "unbalanced ` tail"]:
            assert len(mask_inline_code(line)) == len(line)

    def test_unbalanced_run_left_intact(self):
        # CommonMark: an unmatched run on a final line is literal text.
        assert mask_inline_code("a ` b") == "a ` b"

    def test_double_backtick_run_matched_by_equal_length(self):
        assert mask_inline_code("``a`b``") == "       "
