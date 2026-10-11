"""Tests for the Markdown rendering of an export bundle.

The emphasis is the one thing a Markdown export can get structurally wrong:
message content is written VERBATIM so its fenced code blocks survive, which
means a turn that ends inside an unterminated fence would swallow every later
turn into one code block, and one ending inside an unterminated ``<!--`` would
hide them from the renderer entirely. That is silent data loss at read time — the
text is in the file but no renderer shows it — so the balancing cases are pinned
first, including the CommonMark rules that decide what actually opens and closes
each construct.

The opener rules get as much weight as the closers, because a MIS-detected opener
is worse than no guard at all: it makes the renderer append a closer that is
itself a valid opener, causing the very loss the guard exists to prevent.

The rest holds the document's shape: the role headings and timestamps a reader
navigates by, the provenance table, and the table-cell escaping that is the one
place content cannot be verbatim.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser

import pytest
from markdown_it import MarkdownIt

from kiro_crew.dashboard import session_markdown as sm

_MARKDOWN = MarkdownIt("commonmark")
_MARKDOWN_HTML = MarkdownIt("commonmark", {"html": True})


def _render(bundle) -> str:
    return "".join(sm.iter_session_markdown(bundle))


def _assert_later_heading_is_structural(out: str) -> None:
    tokens = _MARKDOWN.parse(out)
    assert any(
        token.type == "heading_open"
        and token.tag == "h2"
        and tokens[index + 1].type == "inline"
        and tokens[index + 1].content == "User — t2"
        for index, token in enumerate(tokens[:-1])
    )
    # The token-stream check above only proves markdown-it's own PARSER sees a
    # structural heading. It says nothing about the actually rendered HTML: a
    # manufactured closer can satisfy the parser while an HTML comment it did not
    # correctly close keeps consuming raw HTML past that heading, which the
    # token stream cannot see because this module writes real HTML, not a
    # markdown-it extension. Render with raw HTML enabled and track with a real
    # HTML parser whether a comment is still open when "User — t2" is reached.
    _assert_no_unclosed_comment_before_later_heading(out)


class _CommentAndHeadingTracker(HTMLParser):
    """Tracks whether an HTML comment is open when the h2 "User" text is seen."""

    def __init__(self) -> None:
        super().__init__()
        self._comment_open = False
        self._in_h2 = False
        self.comment_open_at_user_heading: bool | None = None

    def handle_comment(self, data: str) -> None:
        # html.parser calls this only for a comment it found BOTH delimiters
        # for; an unterminated "<!--" with no later "-->" in the fed text is
        # never reported here at all -- it is simply still open when feed()
        # returns, which is exactly the state this test needs to catch.
        pass

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "h2":
            self._in_h2 = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self._in_h2 = False

    def handle_data(self, data: str) -> None:
        if self._in_h2 and "User" in data and self.comment_open_at_user_heading is None:
            self.comment_open_at_user_heading = False


def _assert_no_unclosed_comment_before_later_heading(markdown_text: str) -> None:
    """Prove, with a real HTML parser, that no comment hides the "User" heading.

    html.parser silently stops emitting ANY further events once it hits an
    unterminated "<!--" with no "-->" anywhere later in the fed string -- the
    exact failure mode this module exists to prevent, since that is also what a
    browser does. So the absence of the "User" heading event IS the failure
    signal; there is no separate "comment still open" event to check.
    """
    html = _MARKDOWN_HTML.render(markdown_text)
    tracker = _CommentAndHeadingTracker()
    tracker.feed(html)
    assert tracker.comment_open_at_user_heading is False, (
        "the later role heading's text never reached the HTML parser -- an HTML "
        f"comment is still open and is hiding it. Rendered HTML:\n{html}"
    )


def _bundle(messages, **over):
    base = {
        "bundle_version": 2,
        "origin": "",
        "title": "Design chat",
        "agent": "default",
        "messages": messages,
    }
    base.update(over)
    return base


MSGS = [
    {"role": "user", "content": "how does the tunnel work?", "ts": "2026-09-09T08:32:44+00:00"},
    {"role": "assistant", "content": "it forwards loopback", "ts": "2026-09-09T08:32:51+00:00"},
]


# ── the fence contract: an unterminated block must not eat the transcript ──


def test_an_unterminated_fence_is_closed_at_the_message_boundary():
    # Without the closing fence the NEXT role heading renders as code, and every
    # turn after it disappears from view. The text would still be in the file,
    # which is what makes this failure silent.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": "here:\n```python\nx = 1", "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert out.count("```") % 2 == 0
    # The closer lands before the next heading, not after it.
    assert out.index("```python") < out.rindex("```") < out.index("## User")


def test_a_balanced_fence_is_left_exactly_as_written():
    content = "here:\n```python\nx = 1\n```\ndone"
    out = _render(_bundle([{"role": "assistant", "content": content, "ts": "t1"}]))
    assert content in out
    assert out.count("```") == 2


def test_a_balanced_fence_inside_a_list_item_is_left_exactly_as_written():
    # "- ```python" opens no top-level fence (the marker is not a fence line).
    # Its own closer, indented to the item's content, is tracked as a separate
    # list-item fence state so the top-level scan never mistakes it for a
    # free-floating opener and never appends a spurious closer after it.
    content = "- ```python\n  print(1)\n  ```\nmore text"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 2
    assert "## User" in out


def test_an_unterminated_list_item_fence_draws_no_closer():
    # Measured against a CommonMark renderer: the "---" separator this renderer
    # writes before every heading is dedented past the item's content column,
    # which ends the list item and closes the fence inside it. So the document
    # already reads correctly, and a manufactured closer would be a NEW
    # top-level fence that swallows the "## User" heading -- the exact loss the
    # closer exists to prevent.
    content = "- ```python\n  print(1)"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 1
    assert out.index("```python") < out.index("## User")


def test_a_balanced_fence_in_a_nested_item_whose_closer_exceeds_three_spaces_is_untouched():
    # The closer here is indented 4 -- past the top-level "at most 3 spaces"
    # fence rule -- so a scan that tests it unstripped sees no fence there at
    # all and leaves the list-item state set. Nothing is appended for that state
    # now, but the state still has to clear: a fence opened AFTER this item must
    # still be seen (see the dedent test below).
    content = "  - ```python\n    x = 1\n    ```\nafter"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 2
    assert "## User" in out


def test_a_dedent_ends_a_list_item_so_a_later_unterminated_fence_is_still_seen():
    # A line dedented past the item's content column ends the item. If the scan
    # stayed in its list-item state instead, it would skip every line after the
    # dedent -- including a genuinely unterminated top-level fence, whose
    # missing closer then swallows the next turn.
    content = "- ```python\n  print(1)\n\nDone.\n\n```js\nconst x = 1"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.index("```js") < out.rindex("```") < out.index("## User")


def test_a_lazy_continuation_keeps_the_list_item_open_for_its_own_fence():
    # CommonMark §4.8: "continued" is dedented past the item's content column, but
    # it continues the paragraph still open inside the item -- a LAZY
    # continuation -- so the item is not over and the fence below is the ITEM's.
    # Reading that dedent as a list exit makes the fence look top-level and
    # returns a column-0 closer, which is itself a valid opener: it swallows the
    # "## User" heading and every turn after it into one code block.
    content = "- item\ncontinued\n  ```python\n  code"
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 1
    # Measured against the independent renderer, not against our own scan.
    _assert_later_heading_is_structural(out)


def test_a_comment_after_a_lazy_continuation_is_closed_inside_the_item():
    # The same root cause by the other route, which is why the repair is in the
    # container tracking rather than in the fence closer: with the item wrongly
    # ended, a bare `-->` closer would land at column 0 as a PARAGRAPH, which
    # CommonMark escapes to `--&gt;`, leaving the comment open over every later
    # turn. The self-contained `<!-- -->` closer is immune to that regardless --
    # it is written at the left margin, where it is its own HTML block whether
    # the item is still open or not.
    opener = "<" + "!--"
    content = f"- item\ncontinued\n  {opener} note"
    assert sm._unterminated_blocks(content) == "<!-- -->"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert "\n<!-- -->\n" in out
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "third_line,closer",
    [
        ("# Heading", "```"),
        ("---", "```"),
        ("***", "```"),
        ("___", "```"),
        ("> quote", "```"),
        ("===", ""),
        ("--", ""),
    ],
    ids=["atx", "break-dash", "break-star", "break-underscore", "quote", "setext", "two-dashes"],
)
def test_only_a_real_block_start_ends_the_item_a_dedent_opened(third_line, closer):
    # The opposite direction of the lazy-continuation rule, and the direction that
    # withholds a closer a GENUINELY top-level fence needs. An ATX heading, a
    # block quote and a thematic break all end the paragraph, so the item is over
    # and the indented fence below is top-level. A setext underline does NOT:
    # `===` can only underline a paragraph in its own container, so one dedented
    # out of a list item is ordinary text and lazily continues -- and `--` is two
    # characters short of a thematic break, so it is text for the same reason.
    content = f"- item\ncontinued\n{third_line}\n  ```python\n  code"
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "peer_line",
    ["2. item", "2.\titem", "-\titem", "*\titem", "10) item", "2.     code"],
    ids=["ordered", "ordered-tab", "bullet-tab", "other-bullet-tab", "paren", "indented-code"],
)
def test_a_dedented_peer_marker_ends_the_item_instead_of_continuing_it(peer_line):
    # A marker dedented out of an open item fails that item before the paragraph
    # interrupt rule is consulted, so it starts a PEER item with its own content
    # column -- even an ordered marker above 1. Read as lazy text instead, the old
    # column stays live, the two-space fence below looks list-owned, and its
    # missing closer turns every later heading into code.
    content = f"- item\n{peer_line}\n  ```\n  code"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content",
    [
        "- item\n2. item\n   ```\n   code",
        "- item\n2.\t```\n    code",
        "- item\n2.\n   ```\n   code",
    ],
    ids=["space", "tab-on-marker-line", "empty-item"],
)
def test_a_fence_indented_to_the_peer_items_column_stays_list_owned(content):
    # The opposite transition: a fence at the NEW item's content column belongs to
    # that item, and a column-0 closer would itself open code over later turns.
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_a_blank_line_makes_the_following_dedent_a_real_list_exit():
    # Lazy continuation needs an OPEN paragraph. The blank line closes the item's
    # paragraph, so "continued" starts a new top-level one, the list is over, and
    # the fence indented two spaces after it is a top-level fence that must be
    # closed. Without this the item would look open forever and the closer would
    # be withheld.
    content = "- item\n\ncontinued\n  ```python\n  code"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_a_fence_line_inside_a_raw_html_block_does_not_open_a_fence():
    # A <pre> block holds its content as raw HTML, so the ``` inside it opens
    # nothing. Reading it as an opener makes the renderer append a closer that
    # IS one, swallowing every turn after it.
    content = "<pre>\n```\n</pre>"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 1
    assert "## User" in out


@pytest.mark.parametrize("tag", ["pre", "script", "style", "textarea"])
def test_an_unterminated_raw_html_block_is_closed_at_the_message_boundary(tag):
    # These four block kinds survive blank lines (CommonMark 4.6, condition 1),
    # so one left open hides every following turn from the renderer exactly as an
    # open comment does.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": f"<{tag}>\nstill open", "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert f"</{tag}>" in out
    assert out.index(f"<{tag}>") < out.index(f"</{tag}>") < out.index("## User")


@pytest.mark.parametrize(
    "content,closer",
    [
        ("<div>\ntext <!-- unfinished", "<!-- -->"),
        ("<pre>\n<!-- unfinished\n</pre>", "<!-- -->\n</pre>"),
    ],
)
def test_a_comment_inside_raw_html_is_closed_before_its_enclosing_block(content, closer):
    # Raw HTML emits a mid-line comment opener verbatim. CommonMark still ends
    # the raw block at its apparent terminator, so the remaining HTML comment
    # alone must close before the next turn -- with an effective closer, since a
    # bare `-->` manufactured after the block already ended is just a paragraph
    # that CommonMark escapes, leaving the real comment open (H1).
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert (
        out.index(content)
        < out.index(closer, out.index(content) + len(content))
        < out.index("## User")
    )
    _assert_later_heading_is_structural(out)


def test_an_unterminated_raw_html_block_inside_a_list_is_closed():
    # The closer is written at the left margin: a `</script>` line is raw HTML
    # wherever it lands, and a browser needs it whether or not the list item
    # that opened the element is still open in Markdown.
    content = "- <script>\n  unfinished"
    assert sm._unterminated_blocks(content) == "</script>"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert out.index("</script>") < out.index("## User")
    _assert_later_heading_is_structural(out)


# ── a MALFORMED raw-text end tag must not be mistaken for a real close ──
#
# A raw-text element (<script>, <style>, ...) ends only at `</name` followed by
# ASCII whitespace, `/`, or `>` (HTML §13.2.5.6). Two near-misses look like a
# close but are not one to a browser: a `>` that sits inside a quoted attribute
# value of the end tag, and a non-breaking space (U+00A0) after the tag name
# (HTML's whitespace is ASCII-only, so `</script\u00a0>` is still raw text).
# Treating either as a close drops the manufactured closer and lets the element
# swallow every later turn in a renderer — the exact silent read-time loss this
# module exists to prevent.


def test_a_quoted_gt_in_a_raw_text_end_tag_does_not_close_it():
    # The `>` lives inside data-x="...", so the end tag is unfinished: a browser
    # keeps reading raw text, and so must the scanner. The element stays open
    # (and the pending quote is recorded) so a closer is still emitted.
    state = sm._scan_rendered('<script>\n</script data-x=">')
    assert state.open_elements == ["script"]
    assert state.pending_quote == '"'
    assert not state.clean


def test_a_nonbreaking_space_in_a_raw_text_end_tag_does_not_close_it():
    # U+00A0 is not an HTML tag-name delimiter, so `</script\u00a0>` is raw text,
    # not a close tag. Python's \s would wrongly match it; the scanner must use
    # HTML's ASCII whitespace and leave the element open.
    state = sm._scan_rendered("text <script>x</script\u00a0>")
    assert state.open_elements == ["script"]
    assert not state.clean


@pytest.mark.parametrize("terminator", ["</script>", "</script >", "</script\t>", "</script/>"])
def test_a_well_formed_raw_text_end_tag_still_closes(terminator):
    # The fix must not over-reach: every HTML-legal terminator (`>`, ASCII
    # whitespace, `/`) still closes the element cleanly.
    state = sm._scan_rendered(f"<script>x{terminator}")
    assert state.open_elements == []
    assert state.clean


@pytest.mark.parametrize(
    "content",
    [
        '<script>\n</script data-x=">',
        "<script>x</script\u00a0>",
    ],
)
def test_a_malformed_raw_text_end_tag_still_shows_later_turns(content):
    # End to end: the later turn's heading must survive to the renderer. Without
    # the closer the malformed end tag leaves open, the <script> element eats it.
    assert "</script>" in sm._unterminated_blocks(content)
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert out.index("</script>") < out.index("## User")
    _assert_later_heading_is_structural(out)


# Two more raw-text near-misses, each triggered only by a malformed end tag, each
# the same silent read-time loss:
#
#  - A Unicode letter that *case-folds* to a tag-name character. HTML folds tag
#    names ASCII-only, so `</ſtyle>` (U+017F LONG S) is raw text to a browser.
#    Python's re.IGNORECASE without re.ASCII folds it to `</style`, wrongly
#    closing the element.
#  - A <script> escape run. In `<script><!--<script></script>` the parser is in
#    script-data-double-escaped state, where the inner `</script>` only exits
#    that state and does NOT close the outer <script> (HTML §13.2.5.4–5.21). A
#    plain `</script` search stops at it and reports the element closed.


def test_a_unicode_case_fold_does_not_close_a_raw_text_end_tag():
    # U+017F folds to `s` under Unicode but not under HTML's ASCII folding, so
    # `</ſtyle>` is raw text; re.ASCII keeps the match ASCII-only and the element
    # stays open so a closer is emitted.
    state = sm._scan_rendered("<style>x</\u017ftyle>")
    assert state.open_elements == ["style"]
    assert not state.clean


def test_a_double_escaped_inner_script_close_does_not_close_the_outer_script():
    # `<!--<script` enters the double-escaped state; the inner `</script>` only
    # leaves it, so the outer <script> is still open at the end of the message.
    state = sm._scan_rendered("<script><!--<script></script>")
    assert state.open_elements == ["script"]
    assert not state.clean


def test_a_double_escaped_script_closer_is_two_tags_not_one():
    # GPT 6.1 finding: a lone </script> only de-escalates double-escaped to
    # escaped (HTML 13.2.5.4-5) -- it does NOT close the element. closers()
    # must emit a second </script>, or the batch it returns is unverified-wrong
    # and every later turn stays hidden as script text.
    state = sm._scan_rendered("<script><!--<script>")
    assert state.open_elements == ["script"]
    assert state.script_double_escaped
    assert state.closers() == ["</script>", "</script>"]


def test_seven_unclosed_pre_before_a_double_escaped_script_still_closes_cleanly():
    # The exact GPT 6.1 trigger: enough <pre> openers that the old count-based
    # ceiling (len(closers) < _MAX_CLOSERS) could land exactly on the limit
    # mid-batch, before the double-escaped </script> was ever re-verified.
    content = "<pre>" * 7 + "<script><!--<script>"
    closing = sm._unterminated_blocks(content)
    assert closing.split("\n")[:2] == ["</script>", "</script>"]
    assert closing.count("</pre>") == 7

    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert out.count("</script>") == 2
    assert out.index("</script>", out.index("</script>") + 1) < out.index("## User")
    _assert_later_heading_is_structural(out)


def test_an_unresolvable_batch_raises_rather_than_shipping_a_guess(monkeypatch):
    # The other half of the fix: bound VERIFICATION PASSES, not emitted closer
    # lines, and never return a batch this module has not re-parsed and found
    # clean. Forcing the pass budget to 0 means the loop body never runs at
    # all, so the only thing standing between this and the old silent "" is
    # the mandatory final verification -- which must refuse to answer here.
    monkeypatch.setattr(sm, "_MAX_CLOSERS", 0)
    with pytest.raises(ValueError):
        sm._unterminated_blocks("<pre>\n<!-- unfinished\n</pre>")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        # Fully balanced double-escaped run: outer close really does close it.
        ("<script><!--<script></script></script>", []),
        # Single-escaped (no inner <script>): the first </script closes it.
        ("<script><!-- x --></script>", []),
        ("<script><!--</script>", []),
        # Escape left before the inner tag: a later real </script closes it.
        ("<script><!--<script></script>--></script>", []),
        # A name that merely starts with `script` is not the end tag.
        ("<script>x</scripty></script>", []),
    ],
)
def test_script_escape_states_still_close_a_balanced_script(content, expected):
    # The escape-state machine must not over-reach: a <script> that a browser
    # does close must still close cleanly here.
    state = sm._scan_rendered(content)
    assert state.open_elements == expected
    assert state.clean


@pytest.mark.parametrize(
    "content",
    [
        "<style>x</\u017ftyle>",
        "<script><!--<script></script>",
    ],
)
def test_a_folded_or_escaped_end_tag_still_shows_later_turns(content):
    # End to end: the later turn's heading survives to the renderer because the
    # element the malformed/ignored end tag left open gets a manufactured closer.
    assert "</" in sm._unterminated_blocks(content)
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# A fourth attribute-scanning near-miss: an UNQUOTED attribute value that itself
# contains `=` and a `"`. In `<pre data-x=a=" title="unfinished>` a browser reads
# `a="` as data-x's whole unquoted value (HTML §13.2.5.36 ends it only at ASCII
# whitespace), so the `"` opening `title`'s value is still open at `>`, and the
# tag is unfinished. A scanner that lets the embedded `=` restart value parsing
# flips the quote parity, reads the `>` as a clean terminator, and emits no
# _TAG_COMPLETER — so the unfinished `title="` swallows every later turn.


def test_an_embedded_equals_in_an_unquoted_value_does_not_restart_value_parsing():
    # data-x's value is the unquoted `a="`; `title="unfinished>` then leaves a
    # quote open at the end, so the tag is unfinished (quote recorded) not closed.
    end, quote = sm._scan_tag_tail('<pre data-x=a=" title="unfinished>', len("<pre"), 34)
    assert end == 34  # ran to the end: no `>` was read as a terminator
    assert quote == '"'


def test_an_unfinished_quoted_attribute_triggers_the_tag_completer():
    # The tag runs off the end with a quote still open, so it is unfinished: the
    # state records the pending quote (the element it would open is not decided
    # until the completer's `>` arrives on the next pass), and a _TAG_COMPLETER
    # is emitted so the open `title="` cannot swallow later turns.
    content = '<pre data-x=a=" title="unfinished>'
    state = sm._scan_rendered(content)
    assert state.pending_quote == '"'
    assert not state.clean
    assert sm._TAG_COMPLETER in sm._unterminated_blocks(content)


# An `=` that is itself the first character of a value (`x==`) is the same class
# of bug by a shorter route: the first `=` enters the value state, and the second
# `=` must be read as the first (unquoted) value char, NOT as a fresh value-start.
# In `<script x==" title="unfinished>` a browser reads `="` as x's unquoted value,
# leaving `title`'s `"` open at `>`. A scanner that re-arms value-start on the
# second `=` lets the next `"` open a quote the browser never opened, flips parity,
# reads `>` as a clean terminator, emits no _TAG_COMPLETER, and hides later turns.


def test_an_initial_equals_in_a_value_is_an_unquoted_value_char():
    # x's value is the unquoted `="`; `title="unfinished>` then leaves a quote open
    # at the end, so the tag is unfinished (quote recorded), not closed.
    content = '<script x==" title="unfinished>'
    end, quote = sm._scan_tag_tail(content, len("<script"), len(content))
    assert end == len(content)  # ran to the end: no `>` read as a terminator
    assert quote == '"'


def test_an_initial_equals_value_triggers_the_tag_completer_end_to_end():
    content = '<script x==" title="unfinished>'
    state = sm._scan_rendered(content)
    assert state.pending_quote == '"'
    assert not state.clean
    assert sm._TAG_COMPLETER in sm._unterminated_blocks(content)
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# A fifth near-miss, distinct from the above: a `=` with NO preceding attribute
# name at all — not even an empty one. In `<pre =" title="unfinished>` a browser
# treats a `=` seen before any attribute name as a parse error that still starts
# an attribute *name* (HTML §13.2.5.32), so `="` is consumed as (the first chars
# of) that name, and the `"` right after it opens no quote. The real `title="`
# attribute then opens the only quote, which is still unclosed at `>`. A scanner
# that instead routes a bare `=` straight into value-start pairs that `"` with
# the one right after it as if it were a complete quoted value, then reads
# `title="unfinished>` as a second, separate attribute, misses the still-open
# quote, and reports the tag closed while a browser would not.


def test_a_leading_equals_with_no_attribute_name_starts_a_name_not_a_value():
    content = '<pre =" title="unfinished>'
    end, quote = sm._scan_tag_tail(content, len("<pre"), len(content))
    assert end == len(content)  # ran to the end: no `>` was read as a terminator
    assert quote == '"'


def test_a_leading_equals_with_no_attribute_name_triggers_the_tag_completer():
    # GPT 6.1 F1 (session_markdown.py): the bare `=` must not let the scanner
    # mistake this tag for complete and swallow every later turn.
    content = '<pre =" title="unfinished>'
    state = sm._scan_rendered(content)
    assert state.pending_quote == '"'
    assert not state.clean
    assert sm._TAG_COMPLETER in sm._unterminated_blocks(content)
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# A sixth near-miss: ASCII whitespace BETWEEN a name and its `=` (HTML
# §13.2.5.34, "after attribute name"). In `<div title = "unfinished >` a
# browser still reads the space-separated `=` as `title`'s own value-start, so
# the quote `"unfinished >` opens stays open at the tag's end — the `>` inside
# it is quoted text, not the terminator. A scanner that resets straight to
# `before` on the whitespace after `title` loses that `title` is still owed a
# value: the next `=` is read as starting a brand-new (one-character) name, and
# the `"` right after THAT is read as starting another new name rather than
# opening a quote, so the real quote is never tracked and the tag is reported
# closed at the first unquoted `>` while a browser would not have closed it.


def test_whitespace_before_equals_still_lets_the_name_claim_its_value():
    content = '<div title = "unfinished >'
    end, quote = sm._scan_tag_tail(content, len("<div"), len(content))
    assert end == len(content)  # ran to the end: no `>` was read as a terminator
    assert quote == '"'


def test_whitespace_before_equals_triggers_the_tag_completer_end_to_end():
    # GPT 6.1 F1 (session_markdown.py), second round: whitespace between a name
    # and its `=` must not drop the name's claim on that `=`.
    content = '<div title = "unfinished >'
    state = sm._scan_rendered(content)
    assert state.pending_quote == '"'
    assert not state.clean
    assert sm._TAG_COMPLETER in sm._unterminated_blocks(content)
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    ("content", "end_is_eof", "quote"),
    [
        ("<a href=x>", False, ""),  # ordinary unquoted value, closed
        ("<a data-x=a=b>", False, ""),  # `=` inside an unquoted value is a value char
        ("<a x==y>", False, ""),  # leading `=` is the first unquoted value char
        ('<a title=">">x', False, ""),  # quoted `>` still not the terminator
        ("<a x=1 y=2>", False, ""),  # unquoted value ends at whitespace, next attr parsed
        ("<a href=", True, ""),  # `=` with no value yet, cut off: unfinished, no quote
    ],
)
def test_scan_tag_tail_attribute_shapes(content, end_is_eof, quote):
    # The unquoted-value state must not over-reach: well-formed tags still close,
    # and only a genuinely open quote is reported pending.
    end, q = sm._scan_tag_tail(content, content.index(" ") + 1, len(content))
    assert (end >= len(content)) == end_is_eof
    assert q == quote


def test_an_unquoted_attribute_value_shows_later_turns_end_to_end():
    content = '<pre data-x=a=" title="unfinished>'
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_an_ordered_list_starting_above_one_does_not_interrupt_a_paragraph():
    content = "example\n2. item\n   ```\n   code"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "completed_block",
    ["<pre>\n</pre>", "```\ncode\n```", "<!-- complete -->"],
    ids=["raw-html", "fence", "comment"],
)
def test_a_completed_block_restores_list_block_start_semantics(completed_block):
    content = f"{completed_block}\n2. item\n   ```\n   code"
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_a_condition_seven_tag_allows_a_greater_than_sign_inside_quotes():
    content = '<span title=">">\n```\n\n```\ncode\n``` '
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert out.count("```") == 3
    _assert_later_heading_is_structural(out)


def test_a_comment_opener_inside_a_quoted_tag_attribute_is_not_a_comment():
    content = '<span title="<!--">\n```\n\n```python\ncode'
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert "-->" not in out
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "opener,terminator",
    [
        ('<?xml version="1.0"', "?>"),
        ("<!DOCTYPE html", ">"),
        ("<![CDATA[ data", "]]>"),
    ],
)
def test_html_blocks_that_outlive_a_blank_line_get_their_own_terminator(opener, terminator):
    # CommonMark 4.6 conditions 3, 4 and 5 run to their own terminator rather than
    # to a blank line, so each reaches the next message and hides it. Which blocks
    # need a terminator is decided by whether they survive a blank line, not by how
    # ordinary they look.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": opener, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert out.index(opener) < out.index(terminator) < out.index("## User")


def test_a_condition_seven_tag_inside_a_paragraph_does_not_hide_a_fence():
    # Condition 7 may not interrupt a paragraph. Treating <span> below as an
    # HTML block skips the fence opener, then reads its closer as an opener and
    # absorbs the next turn into the manufactured fence.
    content = "Example:\n<span>\n```python\nx = 1\n\n```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert out.count("```") == 2
    assert "## User" in out


def test_a_blank_terminated_html_block_hides_a_fence_but_needs_no_terminator():
    # A <div> block ends at the next blank line, so the blank line before the
    # separator has already closed it. Its content is still raw HTML, so the fence
    # line inside it opens nothing and must not draw a closer.
    content = "<div>\n```\n</div>\n\nDone."
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 1
    assert "## User" in out


def test_an_active_list_item_fence_precedes_nested_marker_discovery():
    # The nested marker/fence-shaped line is literal content of the outer fence.
    # If marker discovery runs first, it replaces the outer backtick fence with a
    # tilde fence at a deeper column and the outer closer becomes a top-level
    # opener. The genuinely unterminated top-level tilde fence must still win.
    content = "- ```text\n  - ~~~ literal\n  ```\n\n~~~python\nx = 1"
    assert sm._unterminated_blocks(content) == "~~~"


@pytest.mark.parametrize(
    "active_html",
    [
        "<!-- open\n- ``` -->",
        "<pre>\n- ``` </pre>",
        "<div>\n- ``` literal",
    ],
    ids=["comment", "raw-text", "blank-terminated"],
)
def test_an_active_html_state_precedes_list_marker_discovery(active_html):
    # In comments/raw-text blocks, the marker/fence-shaped line carries the real
    # terminator. In blank-terminated blocks it precedes the blank transition.
    # None may start a list fence that suppresses that active-state transition.
    content = f"{active_html}\n\n~~~python\nx = 1"
    assert sm._unterminated_blocks(content) == "~~~"


def test_a_fence_on_a_later_line_of_a_list_item_is_the_items_fence():
    # The fence is not on the marker line here -- "- Example:" opens the item and
    # the fence arrives two lines later, indented to the item's content column.
    # Keying on the marker line alone misses this shape, which is the common one
    # in a truncated reply, and then appends a closer that opens a real fence.
    content = "- Example:\n\n  ```python\n  x = 1"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 1
    assert out.index("```python") < out.index("## User")


def test_an_unterminated_fence_indented_past_no_list_is_still_closed():
    # Indentation alone does not make a fence someone else's: with no list item
    # open, a fence indented up to three spaces is a top-level fence and needs its
    # closer. This is why the scan tracks the list CONTAINER rather than just
    # refusing to close anything indented.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": "  ```python\n  x = 1", "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert out.count("```") == 2
    assert out.index("```python") < out.rindex("```") < out.index("## User")


def test_a_comment_opened_inside_a_container_is_still_closed():
    # An HTML comment is an OUTPUT-level construct, not a Markdown container: one
    # opened inside a <div> block or a list item reaches the rendered HTML unclosed
    # just as readily as one at the top level, and hides every turn after it. The
    # container branches of the scan must not skip past it.
    opener = "<" + "!--"
    for content in (f"<div>\n{opener} unfinished", f"- {opener} unfinished"):
        out = _render(
            _bundle(
                [
                    {"role": "assistant", "content": content, "ts": "t1"},
                    {"role": "user", "content": "thanks", "ts": "t2"},
                ]
            )
        )
        assert "<!-- -->" in out, content
        assert out.index(opener) < out.index("<!-- -->") < out.index("## User"), content
        _assert_no_unclosed_comment_before_later_heading(out)


def test_a_comment_closer_for_a_list_item_is_indented_into_the_item():
    # A bare `-->` written at the left margin is a PARAGRAPH, not raw HTML --
    # CommonMark escapes it to `--&gt;` and the comment stays open; the
    # self-contained `<!-- -->` closer is immune to that, so it is written at the
    # left margin rather than indented into the item: there it is its own HTML
    # block, and the comment it closes does not care which construct emits it.
    opener = "<" + "!--"
    content = f"- {opener} unfinished"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert "\n<!-- -->\n" in out
    _assert_no_unclosed_comment_before_later_heading(out)


def test_a_raw_html_block_closes_on_an_uppercase_tag():
    # HTML tags are not case-sensitive, so `</PRE>` ends a block `<pre>` opened.
    # Missing that leaves the scan inside the block for the rest of the message.
    out = _render(_bundle([{"role": "assistant", "content": "<pre>\nx\n</PRE>", "ts": "t1"}]))
    assert "</pre>" not in out


def test_a_comment_inside_a_list_item_fence_stays_literal():
    # A list-item fence is Markdown structure even though the top-level scanner
    # ignores it. Its literal comment must not suppress the later top-level fence.
    content = "- ```html\n  <!--\n  ```\n\n```python\nx = 1"
    assert sm._unterminated_blocks(content) == "```"


def test_non_lf_line_separators_do_not_create_fences():
    # CommonMark treats only LF and CRLF/CR as line endings. Python's splitlines
    # would incorrectly split this vertical tab and manufacture a fence closer.
    assert sm._unterminated_blocks("hello\v```") == ""


def test_a_comment_opener_inside_a_fence_stays_literal():
    # Inside a fenced code block CommonMark escapes the content, so a comment
    # opener there is text and needs no closer -- only the fence does.
    opener = "<" + "!--"
    out = _render(_bundle([{"role": "assistant", "content": f"```\n{opener} x\n```", "ts": "t1"}]))
    assert "-->" not in out


def test_a_multiline_timestamp_cannot_hide_the_transcript():
    # The gateway only ever stamps ISO instants, but an IMPORTED bundle's `ts` is
    # accepted as any string, and it is interpolated into a heading -- which is one
    # line by definition.
    opener = "<" + "!--"
    out = _render(
        _bundle(
            [
                {
                    "role": "assistant",
                    "content": "hi",
                    "ts": f"2026-01-01T00:00:00Z\n{opener}",
                },
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert "## Assistant — 2026-01-01T00:00:00Z &lt;!--\n" in out
    assert "## User" in out


def test_a_multiline_title_cannot_hide_the_transcript():
    # A heading is one line, so a title carrying a newline renders as a heading
    # plus a separate block -- and if that block opens an HTML comment, every turn
    # beneath it disappears. The rename endpoint trims and truncates but keeps
    # newlines, so the title is the one piece of this document a user types freely.
    out = _render(
        _bundle(
            [{"role": "user", "content": "hi", "ts": "t1"}],
            title="Notes\n<!--",
        )
    )
    assert out.startswith("# Notes &lt;!--\n")
    assert len([ln for ln in out.splitlines() if ln.startswith("# ")]) == 1
    assert "## User" in out


def test_a_raw_html_block_closed_on_its_own_line_draws_no_closer():
    # The opening line is itself tested against the end condition, so this is one
    # complete block rather than an unterminated one.
    out = _render(_bundle([{"role": "assistant", "content": "<pre>x</pre>", "ts": "t1"}]))
    assert out.count("</pre>") == 1


def test_a_tag_that_merely_starts_with_a_block_tag_name_is_not_a_raw_html_block():
    # "<prefix>" begins with "<pre" but the character after it is not whitespace,
    # ">", or end of line, so it opens no raw HTML block and needs no closer.
    out = _render(_bundle([{"role": "assistant", "content": "<prefix>\ntext", "ts": "t1"}]))
    assert "</pre>" not in out


def test_a_raw_html_opener_inside_a_code_fence_needs_only_the_fence_closed():
    # An HTML block cannot start inside a fenced code block, so the "<pre>" here
    # is code text. Tracking it would suppress the fence's real closer and leave
    # the renderer appending a closer for a fence that was already closed.
    content = "```\n<pre>\n```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )
    assert content in out
    assert out.count("```") == 2
    assert "</pre>" not in out


def test_a_tilde_block_is_not_closed_by_a_backtick_fence_inside_it():
    # This is how a fenced example OF a fenced block is written, so treating the
    # inner backticks as a closer would both corrupt the example and leave the
    # real block open. CommonMark: a closer must use the same character.
    content = "~~~\n```\nnot a closer\n```\n~~~"
    out = _render(_bundle([{"role": "assistant", "content": content, "ts": "t1"}]))
    assert content in out
    # Balanced already, so nothing is appended after the content.
    assert out.rstrip().endswith("~~~")


def test_a_shorter_run_does_not_close_a_longer_fence():
    # CommonMark: the closer must be at least as long as the opener. A ``` inside
    # a ```` block is content.
    assert sm._unterminated_blocks("````\n```\nstill open") == "````"


def test_a_closer_with_an_info_string_does_not_close():
    # CommonMark forbids an info string on a closing fence, so this is a second
    # OPENING inside the first block, not a close.
    assert sm._unterminated_blocks("```sh\nls\n```sh") == "```"


def test_a_fence_indented_past_three_spaces_is_not_a_fence():
    assert sm._unterminated_blocks("    ```\nindented code, not a fence") == ""


def test_a_tab_indented_fence_is_not_a_fence():
    # A leading tab is four columns, so the line is indented code. Pinned because
    # the indent test measures spaces only and would be wrong on its own; the
    # prefix check is what rejects this, and a refactor could drop it.
    assert sm._unterminated_blocks("\t```") == ""


# ── the opener rules: a MIS-detected opener is what causes the loss ───────


def test_a_one_line_backtick_snippet_does_not_open_a_fence():
    # CommonMark §4.5: a backtick fence's info string may not contain a backtick,
    # so this one-line snippet — a shape people paste routinely — is a paragraph.
    #
    # This is the case that makes mis-detection worse than no guard at all.
    # Reading it as an open fence appends a closer, and that appended line is a
    # BARE ``` with no info string, which IS a valid opener: it swallows every
    # later turn into one code block. The guard would cause the exact silent loss
    # it exists to prevent.
    assert sm._unterminated_blocks("```npm run build```") == ""


def test_the_one_line_snippet_leaves_the_rest_of_the_transcript_readable():
    out = _render(
        _bundle(
            [
                {"role": "user", "content": "```npm run build```", "ts": "t1"},
                {"role": "assistant", "content": "that builds it", "ts": "t2"},
            ]
        )
    )
    # No closer was appended, so the later heading is not inside a code block.
    assert out.count("```") == 2
    assert "## Assistant — t2" in out
    assert "that builds it" in out


def test_a_tilde_fence_may_carry_any_info_string():
    # The backtick restriction is specific to backtick fences; a tilde fence takes
    # an arbitrary info string, so the same guard must not reject one.
    assert sm._unterminated_blocks("~~~ info ~ string\nstill open") == "~~~"


def test_a_longer_backtick_fence_with_a_clean_info_string_still_opens():
    # A 4-backtick fence with a clean info string is a valid opener, so left
    # unterminated it draws its matching closer (and the closer matches length).
    assert sm._unterminated_blocks("````python\nstill open") == "````"


# ── the other construct that hides a transcript: an HTML comment ──────────


def test_an_unterminated_html_comment_is_closed_at_the_message_boundary():
    # Same silent loss as an open fence, by a different route: the text is in the
    # file, and every renderer hides it until an effective closer arrives.
    assert sm._unterminated_blocks("<!-- scratch note") == "<!-- -->"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": "<!-- scratch note", "ts": "t1"},
                {"role": "user", "content": "still visible", "ts": "t2"},
            ]
        )
    )
    assert out.index("<!-- -->") < out.index("## User — t2")
    _assert_no_unclosed_comment_before_later_heading(out)


def test_a_balanced_html_comment_gets_no_closer():
    assert sm._unterminated_blocks("<!-- a note -->\ntext") == ""


def test_a_line_that_closes_one_comment_and_opens_another_is_still_open():
    # The reason the scan walks pairs instead of testing `"-->" in line`: this
    # line both ends its HTML block and emits an unclosed <!--.
    assert sm._unterminated_blocks("<!-- a --> <!-- b") == "<!-- -->"


def test_a_mid_line_comment_opener_is_not_treated_as_a_comment():
    # CommonMark starts a raw HTML block only when the LINE begins with <!--. One
    # appearing mid-line is inline text, and an unterminated one there is escaped
    # to literal characters rather than recognised — so it hides nothing, and
    # appending a --> would put a stray delimiter in the document.
    assert sm._unterminated_blocks("see the <!-- marker here") == ""


def test_a_comment_opener_inside_a_code_fence_needs_only_the_fence_closed():
    # Inside a fence the <!-- is escaped text, so the fence is the only thing
    # open. Two independent scans would wrongly append both closers.
    assert sm._unterminated_blocks("```html\n<!-- example") == "```"


def test_a_fence_line_inside_a_comment_does_not_open_a_fence():
    # The mirror case: inside a comment the ``` line is raw HTML, not a fence, so
    # the comment is the only thing open.
    assert sm._unterminated_blocks("<!-- note\n```") == "<!-- -->"


def test_a_comment_closed_before_a_fence_opens_reports_only_the_fence():
    assert sm._unterminated_blocks("<!-- a -->\n```py\nx = 1") == "```"


# ── the document's shape ─────────────────────────────────────────────────


def test_role_headings_carry_the_label_and_the_timestamp():
    out = _render(_bundle(MSGS))
    assert "## User — 2026-09-09T08:32:44+00:00" in out
    assert "## Assistant — 2026-09-09T08:32:51+00:00" in out


def test_a_message_without_a_timestamp_gets_a_bare_heading():
    # A placeholder would read as a recorded value; absence is the honest answer.
    out = _render(_bundle([{"role": "user", "content": "hi", "ts": ""}]))
    assert "## User\n" in out
    assert "—" not in out.split("## User")[1]


def test_an_unexpected_role_is_titled_rather_than_dropped():
    # Only user/assistant reach a bundle today. Losing a turn is worse than
    # printing a role name this renderer did not expect.
    out = _render(_bundle([{"role": "system", "content": "note", "ts": "t"}]))
    assert "## System — t" in out
    assert "note" in out


def test_the_title_leads_and_an_untitled_session_still_gets_one():
    assert _render(_bundle(MSGS)).startswith("# Design chat\n")
    assert _render(_bundle(MSGS, title="")).startswith("# Session\n")


def test_provenance_rows_are_rendered_and_absent_ones_skipped():
    out = _render(
        _bundle(
            MSGS,
            source={
                "model": "claude-opus-5",
                "exported_at": "2026-09-09T08:33:00+00:00",
                "producer": "kirocrew/0.8.0",
            },
        )
    )
    assert "| Model | claude-opus-5 |" in out
    assert "| Exported | 2026-09-09T08:33:00+00:00 |" in out
    assert "| Produced by | kirocrew/0.8.0 |" in out
    assert "| Messages | 2 |" in out
    assert "| Agent | default |" in out
    # Not reported by the source gateway, so not claimed here.
    assert "Workspace" not in out
    assert "Reasoning policy" not in out


def test_an_empty_approval_policy_is_named_rather_than_blank():
    # "" is the interactive policy, a VALUE and not an absence — collapsing them
    # would hide the field's only interesting reading, that a transcript ran
    # under auto-approval.
    out = _render(_bundle(MSGS, source={"approval_policy": ""}))
    assert "| Approval policy | interactive |" in out
    auto = _render(_bundle(MSGS, source={"approval_policy": "auto"}))
    assert "| Approval policy | auto |" in auto


def test_a_pipe_in_provenance_cannot_shift_the_table():
    # The one place content is not verbatim: an unescaped pipe ends the cell.
    out = _render(_bundle(MSGS, source={"workspace": "a|b"}))
    assert "| Workspace | a\\|b |" in out


def test_a_newline_in_provenance_cannot_end_the_row():
    out = _render(_bundle(MSGS, source={"model": "a\nb"}))
    assert "| Model | a b |" in out


def test_an_empty_message_contributes_a_heading_and_no_body():
    out = _render(
        _bundle(
            [
                {"role": "user", "content": "   ", "ts": "t1"},
                {"role": "assistant", "content": "answer", "ts": "t2"},
            ]
        )
    )
    assert "## User — t1" in out
    assert "## Assistant — t2" in out
    assert "answer" in out


def test_messages_keep_their_transcript_order():
    out = _render(_bundle(MSGS))
    assert out.index("how does the tunnel work?") < out.index("it forwards loopback")


# ── the staged file ──────────────────────────────────────────────────────


def test_write_markdown_file_writes_what_the_generator_yields(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_egress_tmp_dir", lambda: tmp_path)
    bundle = _bundle(MSGS, source={"model": "claude-opus-5"})

    path = sm.write_markdown_file(bundle)
    try:
        assert path.suffix == ".md"
        assert path.name.endswith(sm.MARKDOWN_FILE_SUFFIX)
        # ``newline=""`` keeps LF on every platform, so the bytes are the
        # generator's text and not a platform translation of it.
        assert path.read_bytes().decode("utf-8") == _render(bundle)
        assert b"\r\n" not in path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def test_write_markdown_file_leaves_nothing_behind_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "_egress_tmp_dir", lambda: tmp_path)

    class _Boom(dict):
        def get(self, *_a, **_k):
            raise OSError(28, "No space left on device")

    try:
        sm.write_markdown_file(_Boom())
    except OSError:
        pass
    else:  # pragma: no cover - the stub always raises
        raise AssertionError("expected the write to fail")
    assert list(tmp_path.iterdir()) == []


def test_a_lone_surrogate_in_content_still_writes_a_file(tmp_path, monkeypatch):
    # A transcript can carry an unpaired surrogate (the JSON wire format permits
    # one) and UTF-8 cannot encode it. The JSON export survives because
    # ensure_ascii re-escapes it, so a strict encode here would make Markdown the
    # one format that fails on a session every other export handles.
    monkeypatch.setattr(sm, "_egress_tmp_dir", lambda: tmp_path)
    path = sm.write_markdown_file(_bundle([{"role": "user", "content": "a\ud800b", "ts": "t"}]))
    try:
        text = path.read_text(encoding="utf-8")
        assert "## User — t" in text
        # Escaped visibly and reversibly rather than dropped.
        assert "\\ud800" in text
        assert text.count("a") and "b" in text
    finally:
        path.unlink(missing_ok=True)


def test_the_document_is_text_and_not_json(tmp_path, monkeypatch):
    # A reader opening this file gets a transcript, not a bundle. Pinned because
    # the two formats share every guard and one staging seam.
    monkeypatch.setattr(sm, "_egress_tmp_dir", lambda: tmp_path)
    path = sm.write_markdown_file(_bundle(MSGS))
    try:
        text = path.read_text(encoding="utf-8")
        try:
            json.loads(text)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("the Markdown export parsed as JSON")
        assert text.startswith("# ")
    finally:
        path.unlink(missing_ok=True)


# ── current-head scanner-state regressions ─────────────────────────────────


def test_nested_list_exit_restores_the_enclosing_items_fence_ownership():
    # The inner item ends at its content column. The following two-space fence
    # belongs to the outer item, whose separator dedent closes it without a
    # manufactured top-level closer.
    content = "- outer\n  - inner\n\n  ```python\n  code"
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_heading_ends_a_paragraph_before_an_ordered_list_marker():
    content = "# Examples\n2. item\n   ```python\n   code"
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_only_ascii_space_or_tab_may_pad_a_fence_closer():
    content = "```python\ncode\n```\u00a0"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("<pre>\n<!-- unfinished\n</pre>\n```python\ncode", "```\n<!-- -->\n</pre>"),
        ("- <pre>\n  <!-- unfinished\n  </pre>\n```python\ncode", "```\n<!-- -->\n</pre>"),
        ("- <div>\n  <!-- unfinished\n\n```python\ncode", "```\n<!-- -->"),
    ],
    ids=["raw-html", "raw-html-list", "blank-html-list"],
)
def test_html_container_exit_keeps_the_comment_effective_outside_later_code_fence(content, closer):
    # The raw/blank HTML block ends in Markdown before the later fence, while its
    # emitted HTML comment remains open. Close the later fence first, then the
    # comment -- with an EFFECTIVE closer: a bare "-->" here would land as a
    # paragraph outside any HTML construct and CommonMark would escape it to
    # "--&gt;", leaving the real comment open over every later turn (H1).
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_a_comment_opened_in_a_blockquote_is_closed():
    # Bare, not quoted: the quote ends at the blank line before the separator
    # either way, and `<!-- -->` at the left margin is its own HTML block.
    content = "> <!-- unfinished"
    assert sm._unterminated_blocks(content) == "<!-- -->"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_a_spaced_thematic_break_is_not_a_list_marker():
    content = "* * *\n```python\ncode"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# ── H1/H2/H3: an effective closer, proven against rendered HTML ───────────
#
# All three share one root cause: a manufactured closer or a tracked comment
# state is only correct if it holds in the ACTUALLY RENDERED HTML, not merely
# in markdown-it's token stream. `_assert_later_heading_is_structural` already
# covers both; these tests target each reported case directly and some pin the
# exact closer text too.


@pytest.mark.parametrize(
    "content",
    [
        "<pre>\n<!-- unfinished\n</pre>",
        "<pre>\n<!-- unfinished\n</pre>\n```python\ncode",
        "- <pre>\n  <!-- unfinished\n  </pre>\n```python\ncode",
        "- <div>\n  <!-- unfinished\n\n```python\ncode",
        "> <!-- a\nb",
    ],
    ids=["pre", "pre-then-fence", "raw-html-list", "blank-html-list", "quote"],
)
def test_h1_a_bare_arrow_past_its_container_does_not_close_the_rendered_comment(content):
    # H1: once the raw/blank-terminated HTML container (or quote) that held the
    # comment has itself ended in MARKDOWN, a bare "-->" placed after it lands as
    # an ordinary PARAGRAPH, which CommonMark escapes to "--&gt;". The real HTML
    # comment stays open and keeps consuming every later role heading. Proven
    # with a real HTML parser, not merely a markdown-it h2 token.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_no_unclosed_comment_before_later_heading(out)


@pytest.mark.parametrize(
    "content",
    ["<div>\n<!-- x\n\n<div>", "<div>\n<!-- x\n\n<pre>\n</pre>"],
    ids=["div-then-div", "div-then-pre"],
)
def test_h2_a_later_raw_or_blank_html_opener_does_not_drop_a_still_open_comment(content):
    # H2 regression: the FIRST <div>'s own blank-terminated Markdown container
    # ends at the blank line, but its HTML comment -- an output-level construct,
    # not a Markdown container -- is still open. A SECOND raw/blank HTML opener
    # (another <div>, or a <pre>) must not silently reset that comment state:
    # unconditionally re-masking from a fresh `False` is what dropped it before,
    # losing the only closer the still-open comment needed.
    assert sm._unterminated_blocks(content) == "<!-- -->"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_no_unclosed_comment_before_later_heading(out)


def test_h3_a_comment_inside_a_quoted_list_item_is_detected_and_closed():
    # H3: quote handling tested the quoted line directly against the comment
    # opener, so a LIST MARKER between the quote prefix and the comment --
    # "> - <!--" is a quote containing a list item containing a comment -- was
    # never detected at all, and no closer was ever emitted.
    content = "> - <!-- unfinished"
    assert sm._unterminated_blocks(content) == "<!-- -->"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_no_unclosed_comment_before_later_heading(out)


def test_the_effective_closer_is_immune_to_its_own_surrounding_container():
    # The defining property of "<!-- -->" over a bare "-->": it closes the still
    # open comment whether it lands inside an active Markdown HTML construct or
    # as an ordinary escaped paragraph, because it supplies its OWN "-->" in the
    # literal output text either way. Proven directly against a real HTML
    # parser for a case with no surrounding list/quote prefix at all.
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": "<!-- never closed", "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_no_unclosed_comment_before_later_heading(out)


# ── GPT 6.1 current-head findings 1-5 ────────────────────────────────────


@pytest.mark.parametrize(
    "content",
    [
        "- <div>\n```python\ncode",
        "- <pre>\n```python\ncode",
        "- <!-- unfinished\n```python\ncode",
    ],
    ids=["blank-html", "raw-text", "comment"],
)
def test_gpt_1_list_owned_html_ends_before_a_dedented_fence(content):
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    assert sm._unterminated_blocks(content).startswith("```")
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("Heading\n=======\n2. item\n   ```python\n   code", ""),
        ("    indented code\n2. item\n   ```python\n   code", ""),
        ("-\n\n  ```python\n  code", "```"),
    ],
    ids=["setext", "indented-code", "empty-item"],
)
def test_gpt_2_completed_blocks_and_empty_items_set_fence_ownership(content, closer):
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize("content", ["> - - <!-- unfinished", "- > <!-- unfinished"])
def test_gpt_3_comments_after_arbitrarily_nested_container_prefixes_are_closed(content):
    closer = sm._unterminated_blocks(content)
    assert closer.endswith("<!-- -->")
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content",
    ["text <script>", "> <script>\n> unfinished", "<div>\n<script>\n\ntext"],
    ids=["inline", "quote", "blank-html"],
)
def test_gpt_4_rendered_raw_text_elements_are_closed_across_markdown_contexts(content):
    assert sm._unterminated_blocks(content).endswith("</script>")
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_gpt_4_generated_metadata_cannot_open_raw_html():
    out = _render(
        _bundle(
            [{"role": "x<script>", "content": "body", "ts": "t<script>"}],
            title="Debug <script>",
            agent="agent<script>",
            source={"model": "model<script>"},
        )
    )
    assert "<script>" not in out
    assert "# Debug &lt;script&gt;" in out
    assert "| Agent | agent&lt;script&gt; |" in out
    assert "| Model | model&lt;script&gt; |" in out
    assert "## X&lt;Script&gt; — t&lt;script&gt;" in out


def test_gpt_5_unicode_digits_do_not_start_a_commonmark_list():
    content = "١. item\n   ```python\n   code"
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        # Lazy list continuation and a dedented line after an empty item are plain
        # paragraph text: their inline raw-text tag still reaches the rendered HTML.
        ("- a\nfoo <script>", "</script>"),
        ("1.\nfoo <script>", "</script>"),
        # A tab-indented continuation line is still inside the item's HTML block.
        ("* <div>\n\t<!--", "<!-- -->"),
        # An empty item holds no paragraph, so a dedented line ends it and the
        # following fence is top level.
        ("-\nx\n  ```", "```"),
        ("+\n١.\n  ~~~", "~~~"),
        # A setext underline needs a paragraph above it; a lone "=" is text, so a
        # lone tag after it is a lazy line and the fence below still opens.
        ("=\n<e>\n```", "```"),
    ],
    ids=[
        "lazy-inline",
        "empty-item-inline",
        "tab-comment",
        "empty-item-fence",
        "empty-item-digit",
        "lone-setext",
    ],
)
def test_list_exit_edges_found_by_differential_fuzzing(content, closer):
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# ── fork-lane review (GPT 6.1) findings: three more ways a turn could hide the rest ──


@pytest.mark.parametrize(
    "content,closer",
    [
        ("> <!--\n```\ncode", "```\n<!-- -->"),
        ("> <!--\n\n```\ncode", "```\n<!-- -->"),
        ("> > <!--\n> text\n```\ncode", "```\n<!-- -->"),
        ("- > <!--\n```\ncode", "```\n<!-- -->"),
        ("> - <!--\n> text\n```\ncode", "```\n<!-- -->"),
        ("> <!--\n- item\n  ```\n  code", "<!-- -->"),
        ("> <!-- a\nb", "<!-- -->"),
    ],
    ids=[
        "dedented-fence",
        "blank-then-fence",
        "inner-quote-ends",
        "list-quote-dedent",
        "quote-list-dedent",
        "fence-in-later-list",
        "plain-text",
    ],
)
def test_gpt_6_a_quoted_comment_releases_the_markdown_when_its_quote_ends(content, closer):
    # F1: "> <!--" opens a comment inside a block quote. An HTML block cannot be
    # lazily continued, so the first line without the ">" marker ENDS the quote
    # and the HTML block with it -- a dedented fence there is a real top-level
    # fence. The scanner releases the Markdown at that line instead of reading
    # on as comment content: a closer prefixed with "> " would land INSIDE the
    # unclosed fence, where CommonMark escapes "-->" and the comment stays open
    # over every later turn. The comment is still owed its closer; it has to
    # come after the fence closer, and bare, since the quote is gone.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("> <!--\n> ```\n> code", "<!-- -->"),
        ("> - <!--\n>\n>   ```\n>   code", "<!-- -->"),
    ],
    ids=["quoted-fence", "quoted-item-blank-then-fence"],
)
def test_gpt_6_a_quoted_comment_still_owns_lines_that_stay_in_its_quote(content, closer):
    # The other direction of F1: while the quote marker is still there, a fence
    # line is comment content and opens nothing, so no fence closer is drawn --
    # one would open a NEW top-level fence after the quote ends. A blank quoted
    # line keeps a list item inside the quote open, so the item's lines after it
    # are still the comment's.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("<div>\n`<script>`", "</script>"),
        ("- <div>\n  `<script>`", "</script>"),
        ("<pre>\n`<script>`\n</pre>", "</script>\n</pre>"),
        ("<!-- x --> `<script>`", "</script>"),
        ("<div>\n`<title>`", "</title>"),
    ],
    ids=["blank-html", "blank-html-list", "raw-html", "comment-line", "title"],
)
def test_gpt_7_inline_code_masking_does_not_apply_inside_raw_html(content, closer):
    # F2: inside an HTML block backticks are ordinary characters, so a <script>
    # "quoted" in them is emitted verbatim and really opens the element. The
    # rendered-element scan masked balanced inline code unconditionally, so it
    # never saw the tag and never appended the </script> every later turn needed.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


def test_gpt_7_inline_code_in_a_paragraph_is_still_text():
    # The masking is right where CommonMark interprets the backticks: a tag in
    # inline code inside a paragraph is escaped, opens nothing, and must draw no
    # closer -- a stray "</script>" is harmless, but it is still not content.
    assert sm._unterminated_blocks("use `<script>` here") == ""
    assert sm._unterminated_blocks("use `<title>` here") == ""


@pytest.mark.parametrize("tag", ["title", "iframe", "xmp", "noembed", "noframes"])
def test_gpt_8_rcdata_and_raw_text_elements_outside_commonmarks_set_are_closed(tag):
    # F3: <title> and <iframe> open only a blank-terminated HTML block in
    # CommonMark, which needs no Markdown terminator -- but a browser reads
    # everything after them as element text until the matching close tag, so an
    # unterminated one still swallowed every later heading. The rendered set is
    # therefore tracked separately from CommonMark's condition-1 set.
    for content in (f"<{tag}>unfinished", f"text <{tag}>", f"<{tag}>\n```\ncode"):
        assert sm._unterminated_blocks(content).endswith(f"</{tag}>"), content
    assert sm._unterminated_blocks(f"<{tag}>a</{tag}>") == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": f"<{tag}>unfinished", "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    assert f"</{tag}>" in out.split("## User")[0]
    _assert_later_heading_is_structural(out)


def test_gpt_8_a_title_draws_only_the_element_closer():
    # <title> is a blank-terminated block in Markdown (closed by the blank line
    # before the separator) and a raw-text element in a browser: it draws the
    # element closer and nothing else, and an element opened inside <pre> is
    # closed before the </pre> that ends both the element and the block.
    assert sm._unterminated_blocks("<title>x") == "</title>"
    assert sm._unterminated_blocks("<pre>\n<title>x\n</pre>") == "</title>\n</pre>"


# ── elements a browser parses but does not display ──

#: Elements whose children a browser leaves inert, hidden, collapsed or as fallback
#: content when one is left open, so every later turn disappears into it. The
#: fallback-content ones end only with a plain end tag, which a paragraph open
#: inside makes the browser ignore.
_FALLBACK_CONTENT = ("datalist", "rp", "audio", "video", "canvas", "meter", "progress")
_UNDISPLAYED = ("template", "dialog", "details", "select") + _FALLBACK_CONTENT


class _OpenElementsAtHeadings(HTMLParser):
    """The undisplayed elements still open when each ``<h2>`` starts.

    Read with the stdlib tokenizer, independently of the module under test: a name
    stack is enough for the simple nesting these cases use. A slash on a non-void
    element does not close it in HTML, so ``<template/>`` opens one here as well.
    """

    def __init__(self) -> None:
        super().__init__()
        self._open: list[str] = []
        self.at_heading: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag == "h2":
            self.at_heading.append(list(self._open))
        if tag in _UNDISPLAYED:
            self._open.append(tag)

    def handle_startendtag(self, tag: str, attrs: object) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]


def _undisplayed_open_at_later_heading(markdown_text: str) -> list[str]:
    tracker = _OpenElementsAtHeadings()
    tracker.feed(_MARKDOWN_HTML.render(markdown_text))
    return tracker.at_heading[-1]


@pytest.mark.parametrize("tag", _UNDISPLAYED)
def test_an_unclosed_element_a_browser_does_not_display_is_closed(tag):
    # None of these is a raw-text element, so the Markdown is already closed and
    # the parse reports nothing wrong. A browser still parks every later heading
    # inside the element: inert (template), hidden (dialog, datalist, rp),
    # collapsed (details) or as fallback content (select, audio, video, canvas,
    # meter, progress). Only the tokenizer's view of the rendered HTML sees it.
    for content in (
        f"<{tag}>\nunfinished",
        f"<{tag}>unfinished",
        f"text <{tag}> more",
        f"<{tag.upper()}>\nunfinished",
        f"<{tag}/>\nunfinished",
        f"<{tag}>\n\nafter a blank line",
        f"<div>\n<{tag}>\nunfinished",
    ):
        assert sm._unterminated_blocks(content).endswith(f"</{tag}>"), content
    assert sm._unterminated_blocks(f"<{tag}>a</{tag}>") == ""
    # The control: without the closer the same tokenizer finds the element open at
    # the later heading, so the check below can fail.
    assert _undisplayed_open_at_later_heading(
        f"<{tag}>\nunfinished{sm._SEPARATOR}## User — t2\n"
    ) == [tag]
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": f"<{tag}>\nunfinished", "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    assert out.index(f"</{tag}>") < out.index("## User")
    _assert_later_heading_is_structural(out)
    assert _undisplayed_open_at_later_heading(out) == []


def test_closers_for_nested_unclosed_elements_come_innermost_first():
    assert (
        sm._unterminated_blocks("<details>\n<dialog>\n<template>x")
        == "</template>\n</dialog>\n</details>"
    )
    assert sm._unterminated_blocks("<pre>\n<template>x") == "</template>\n</pre>"
    assert sm._unterminated_blocks("<template><script>x") == "</script>\n</template>"
    assert sm._unterminated_blocks("<template>x") == "</template>"
    assert sm._unterminated_blocks("<details>\n<video>\nx") == "\n</video>\n</details>"


@pytest.mark.parametrize("tag", _FALLBACK_CONTENT)
def test_a_fallback_content_closer_is_a_block_of_its_own(tag):
    # A browser ends these elements with a plain end tag only while no paragraph is
    # open inside, and a closer on the line after a paragraph is part of that
    # paragraph. After a blank line it is an HTML block, which is emitted outside
    # any paragraph, so the closer is honoured.
    for content in (f"<{tag}>\n\nafter a blank line", f"text <{tag}> more", f"<{tag}>unfinished"):
        assert sm._unterminated_blocks(content) == f"\n</{tag}>", content
        out = _render(
            _bundle(
                [
                    {"role": "assistant", "content": content, "ts": "t1"},
                    {"role": "user", "content": "later", "ts": "t2"},
                ]
            )
        )
        blocks = [
            token.content for token in _MARKDOWN_HTML.parse(out) if token.type == "html_block"
        ]
        assert f"</{tag}>\n" in blocks, content


@pytest.mark.parametrize(
    "content,closers",
    [
        ("<details>\n<template>\n</details>", "</template>\n</details>"),
        ("<pre>\n<template>\n</pre>", "</template>\n</pre>"),
        ("<details>\n<select>\n</details>", "</select>\n</details>"),
    ],
    ids=["details-across-template", "pre-across-template", "details-across-select"],
)
def test_an_end_tag_that_cannot_reach_its_element_does_not_count_as_closing_it(content, closers):
    # A browser ignores `</details>` while a template or select is open inside it:
    # an end tag cannot cross a template's scope boundary, and a select swallows
    # every end tag but its own. The outer element stays open and hides every later
    # turn, so its closer is still owed after the inner one.
    assert sm._unterminated_blocks(content) == closers


@pytest.mark.parametrize(
    "content",
    [
        "<template>a</template>",
        "<TEMPLATE>a</Template>",
        "<details>\n<summary>s</summary>\n\ntext\n</details>",
        "<details><dialog>a</dialog></details>",
        "`<template>`",
        "```html\n<template>\n```",
        "<templates>x",
        "<template-card>x",
    ],
)
def test_a_balanced_quoted_or_unrelated_element_draws_no_closer(content):
    assert sm._unterminated_blocks(content) == ""


# ── fork-lane review, second round ──


@pytest.mark.parametrize(
    "content,closer",
    [
        ('<div>\n<script\n type="text/javascript">', "</script>"),
        ("<script\nsrc=x>", "</script>"),
        ("text <script\nsrc=x>\nmore", "</script>"),
        ("<div>\n<script\nsrc=x>\n</script>", ""),
        ("<div>\n<script\nsrc=x>\nstill attributes\n>", "</script>"),
    ],
    ids=["blank-html", "raw-html", "inline", "closed", "three-line-tag"],
)
def test_gpt_9_a_tag_broken_across_lines_still_opens_its_element(content, closer):
    # An HTML tag may put its attributes on later lines, so "<script" alone on
    # a line and "type=…>" on the next is ONE tag to a browser. The scanner
    # read each line on its own and required the ">" on the opener's line, so
    # the element was never tracked and every later turn rendered as script
    # text. The open-tag state is now carried across lines, and until the ">"
    # arrives the text is the tag's own attributes.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("Heading\n--\n2. item\n   ```python\n   code", ""),
        ("Heading\n-\n2. item\n   ```python\n   code", ""),
        ("- item\n  Heading\n  --\n  ```py", ""),
        ("para\n--\n```py", "```"),
        ("--\n```py", "```"),
        ("text\n-\n2. item\n   ```py", ""),
    ],
    ids=["h2-then-list", "single-hyphen", "in-list", "top-fence", "no-paragraph", "lone-hyphen"],
)
def test_gpt_10_a_hyphen_setext_underline_ends_the_paragraph(content, closer):
    # "--" under paragraph text is a setext h2 underline, exactly as "===" is an
    # h1 underline, and it ends the paragraph. Only "=" was recognised, so the
    # paragraph was read as still open, an ordered list starting above 1 could
    # not "interrupt" it, and the list item's own fence was taken as top-level:
    # its manufactured closer opened a new fence over every later turn. A lone
    # "-" after text is the same underline (an empty item cannot interrupt a
    # paragraph); at a block start "--" is plain text and opens nothing.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("-\n  +\ntext\n  ```py", "```"),
        ("- a\n-\ntext\n  ```py", "```"),
        ("-\n  text\n  ```py", ""),
    ],
    ids=["nested-empty-items", "sibling-empty-item", "item-filled-later"],
)
def test_gpt_11_an_empty_list_item_opens_no_paragraph(content, closer):
    # "-" alone is an empty item: it holds no paragraph, so the next line is at
    # a block start. A nested marker there opens a nested (empty) item, and a
    # dedented text line is a NEW top-level paragraph, not a lazy continuation
    # of a paragraph that never existed. The scanner left the paragraph flag
    # set after the marker line, so "text" was read as lazy continuation, the
    # list stayed open, and the fence after it was attributed to the item and
    # went unclosed. Text indented INTO the empty item does fill it, and a
    # fence there is still the item's.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("> quote\ncontinued\n2. item\n   ```py", ""),
        ("> quote\n2. item\n   ```py", ""),
        ("> quote\ncontinued\n```py", "```"),
        ("> quote\ncontinued\n--\n2. item\n   ```py", ""),
        ("> quote\ncontinued\n<div>\n```py", ""),
        ("> ```\n> code\ncontinued\n2. item\n   ```py", "```"),
        ("> > quote\n> text\n2. item\n   ```py", ""),
        ("- item\n  > quote\ncontinued\n  ```py", ""),
        ("> quote\n\n2. item\n   ```py", ""),
    ],
    ids=[
        "lazy-then-list",
        "list-right-after",
        "lazy-then-fence",
        "lazy-setext-is-text",
        "lazy-then-html",
        "quoted-fence-not-lazy",
        "nested-quote",
        "quote-in-list-item",
        "blank-ends-quote",
    ],
)
def test_gpt_12_a_lazy_quote_continuation_opens_no_top_level_paragraph(content, closer):
    # "continued" after "> quote" is a LAZY continuation of the quote's
    # paragraph (CommonMark §5.1), not a top-level paragraph. The scanner read
    # it as one, so "2. item" — which cannot interrupt a paragraph — was taken
    # as text, and the item's fence as a top-level one whose closer opened a
    # fence over every later turn. In fact the quote did not match that line,
    # so its paragraph is not the container there: the list starts, and the
    # fence is the item's. A setext-looking line is lazy text too (an underline
    # cannot be lazy, example 94); a quoted FENCE is not a paragraph, so the
    # line after it ends the quote and is a real paragraph; and a lazy line
    # inside a list item leaves the item open.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


# ── fork-lane review, third round: the parser decides, the tokenizer closes ──


@pytest.mark.parametrize(
    "content,closer",
    [
        ("<script", "<!-- \" ' -->\n</script>"),
        ('<script src="x', "<!-- \" ' -->\n</script>"),
        ("<div>\n<script a='b", "<!-- \" ' -->\n</script>"),
        ("<div>\n<script\n\n", "<!-- \" ' -->\n</script>"),
    ],
    ids=["bare", "open-double-quote", "open-single-quote-in-div", "block-already-ended"],
)
def test_gpt_13_an_unfinished_opening_tag_is_completed_before_it_is_closed(content, closer):
    # A turn ending in "<script" (no ">") leaves a browser INSIDE the tag: a
    # "</script>" appended straight after is read as that tag's attributes, its
    # ">" completes the opener, and the element is still open over every later
    # turn. The tag is completed first, with a line that ends whichever quoted
    # attribute value was open and then the tag, and only then is the element
    # closed -- on the next pass, because what the completed tag opened is only
    # known once it is complete. The completer begins with "<!--", so CommonMark
    # emits it raw even when the HTML block that held the tag has already ended.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)
    assert sm._scan_rendered(_MARKDOWN_HTML.render(out)).clean


def test_gpt_14_a_raw_block_ended_by_another_tags_terminator_frees_the_fence():
    # CommonMark ends a <pre> block at ANY of </pre>, </script>, </style> or
    # </textarea>, so the "~~~" after "</script>" is a real fence that must be
    # closed -- while a browser, for which </script> closed nothing, still has
    # the <pre> open. Both are closed, fence first.
    content = "<pre>\n</script>\n~~~"
    assert sm._unterminated_blocks(content) == "~~~\n</pre>"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("> note\n<span>\n```python\ncode", "```"),
        ("> ```\n- > a\n===\n  ```", ""),
        ("text\n    - a\n-\n  ```", "```"),
    ],
    ids=["lazy-span-then-fence", "quoted-fence-changes-owner", "over-indented-marker"],
)
def test_gpt_15_container_edge_cases_are_decided_by_the_parser(content, closer):
    # Three shapes the hand-written scanner got wrong, and the reason there is
    # no scanner any more: a lone "<span>" cannot interrupt a paragraph, so it
    # lazily continues the quote and the fence after it is top-level; a quoted
    # fence whose quote is swallowed by a new list item is closed by that item;
    # and a list marker indented four columns is paragraph text, so the "-"
    # under it is a setext underline and the fence below is top-level. The
    # parser answers each one; this module only asks.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "rendered,pending_quote,comment_open,open_elements",
    [
        ("<p>text</p>", None, False, []),
        ("<script", "", False, []),
        ('<script src="x', '"', False, []),
        ("<a href='x'>", None, False, []),
        ("<a title='>'>text", None, False, []),
        ('<a title=">">text', None, False, []),
        ("<!-- open", None, True, []),
        ("<!-- a --> <!-- b", None, True, []),
        ("<!-->", None, False, []),
        ("<!--->", None, False, []),
        ("<!-- a --!>", None, False, []),
        ("<?php", None, True, []),
        ("<!DOCTYPE", None, True, []),
        ("<script></script>", None, False, []),
        ("<script>a</scriptx></script>", None, False, []),
        ("<script>a</script >", None, False, []),
        ("<script>a</script", None, False, ["script"]),
        ("<script><!-- x", None, False, ["script"]),
        ("<title>x</pre>", None, False, ["title"]),
        ("<pre><title>x</pre>", None, False, ["pre", "title"]),
        ("<pre>a</pre>", None, False, []),
        ("<template>x", None, False, ["template"]),
        ("<template>x</template>", None, False, []),
        ("<TEMPLATE/>", None, False, ["template"]),
        ("<details><template>x</details>", None, False, ["details", "template"]),
        ("<template><script>a", None, False, ["template", "script"]),
        ("<textarea><b></textarea>", None, False, []),
        ("<IFRAME>x", None, False, ["iframe"]),
        ("`<script>`", None, False, ["script"]),
    ],
)
def test_the_html_tokenizer_reads_what_a_browser_would_have_open(
    rendered, pending_quote, comment_open, open_elements
):
    # The second half of the guard: once the Markdown is closed, what a BROWSER
    # still has open in the rendered HTML. Pinned on raw HTML, independently of
    # any Markdown: unfinished tags with and without an open quote; ">" inside
    # a quoted value not ending the tag; comments, including the abruptly closed
    # "<!-->" and the "--!>" terminator a browser accepts; bogus comments; and
    # raw-text elements, whose content is opaque until "</name" followed by
    # whitespace, "/" or ">" -- so "</scriptx>" closes nothing and "<!--"
    # inside a script opens nothing, and "</script" cut off by the end of the
    # text is script text (HTML 13.2.5.17), not an unfinished tag.
    state = sm._scan_rendered(rendered)
    assert (state.pending_quote, state.comment_open, state.open_elements) == (
        pending_quote,
        comment_open,
        open_elements,
    )
    assert state.clean == (pending_quote is None and not comment_open and not open_elements)


def test_a_block_this_module_cannot_close_draws_nothing():
    # The ceiling on closers and the "unknown block" exit are the two ways the
    # loop ends without a clean parse. Neither invents a closer: a wrong one is
    # worse than none, because it can open the very construct it claims to end.
    assert sm._MAX_CLOSERS >= 4
    assert sm._markdown_need(sm._PARSER.parse("plain"), sm._SENTINEL_BASE) == (False, None)


@pytest.mark.parametrize(
    "content,closer",
    [
        ("kcsession-turn-boundary-6f3a9c1e\n\n<!-- unfinished", "<!-- -->"),
        ("## kcsession-turn-boundary-6f3a9c1e\n\n```py\ncode", "```"),
        (
            "## kcsession-turn-boundary-6f3a9c1e-1\n## kcsession-turn-boundary-6f3a9c1e\n<script>",
            "</script>",
        ),
        ("kcsession-turn-boundary-6f3a9c1e", ""),
    ],
    ids=["paragraph-then-comment", "heading-then-fence", "two-lengthened-forms", "nothing-open"],
)
def test_gpt_16_message_content_cannot_pose_as_the_boundary(content, closer):
    # The checks find the stand-in heading by its text. A message carrying that
    # text would be found FIRST -- as a paragraph, or as a heading that reads as
    # "the boundary survived" -- and whatever the message really left open would
    # hide every later turn. So the stand-in is lengthened until the message
    # provably does not contain it; the base text and its lengthened forms in
    # the content are then just content.
    assert sm._SENTINEL_BASE in content
    assert sm._boundary(content) not in content
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_is_structural(out)
