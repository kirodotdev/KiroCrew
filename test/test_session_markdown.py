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


def _assert_later_heading_parses(out: str) -> None:
    tokens = _MARKDOWN.parse(out)
    assert any(
        token.type == "heading_open"
        and token.tag == "h2"
        and tokens[index + 1].type == "inline"
        and tokens[index + 1].content == "User — t2"
        for index, token in enumerate(tokens[:-1])
    )


def _assert_later_heading_is_structural(out: str) -> None:
    _assert_later_heading_parses(out)
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
        ("<pre\u00a0>\n<!-- unfinished", "</pre>"),
        ("<script\u2003\nvar x = 1;", "</script>"),
        ("<STYLE\u3000\n.a { color: red }", "</style>"),
        ("<textarea\u2028text", "</textarea>"),
        ("<\u017fcript>\nvar x = 1;", "</script>"),
    ],
    ids=["no-break-space", "em-space", "ideographic-space", "line-separator", "long-s"],
)
def test_a_condition_one_opener_is_closed_as_the_parser_reads_it(content, closer):
    # The parser reads `\s` as Unicode whitespace and folds case by Unicode rules,
    # so `<pre` followed by a no-break space opens a block that only `</pre>` ends.
    # A closer table that stopped at ASCII found no closer for a block the parser
    # had already opened, and the export failed on text anyone can type.
    assert sm._unterminated_blocks(content) == closer
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "still readable", "ts": "t2"},
            ]
        )
    )
    assert out.index(closer) < out.index("## User")
    _assert_later_heading_parses(out)


def test_an_unresolvable_batch_raises_rather_than_shipping_a_guess(monkeypatch):
    # Never return a closer this module has not re-parsed and found clean. With
    # the budget forced to 0 no closer may be stacked at all, so the only thing
    # standing between this and a document with a hidden turn is the refusal.
    monkeypatch.setattr(sm, "_MAX_CLOSERS", 0)
    with pytest.raises(ValueError):
        sm._unterminated_blocks("<pre>\n<!-- unfinished")


# ── a message the parse cannot settle is shown as plain text, alone ──


def test_a_message_the_parse_cannot_settle_is_fenced_and_the_export_goes_on(monkeypatch):
    # With the budget at 0 the unterminated fence below has no provable closer, so
    # that one message is written as plain text inside a longer fence. The export
    # does not fail, the NEXT turn still starts a real heading, and that turn is
    # written verbatim rather than fenced.
    monkeypatch.setattr(sm, "_MAX_CLOSERS", 0)
    content = "```\nunfinished"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "thanks", "ts": "t2"},
            ]
        )
    )

    assert f"\n````\n{content}\n````\n\n---\n\n## User — t2\n" in out
    assert out.count("````") == 2, "only the unsettled message is fenced"
    _assert_later_heading_is_structural(out)


@pytest.mark.parametrize(
    "content,fence",
    [
        ("<pre>\n<!-- unfinished", "```"),
        ("```\nunfinished", "````"),
        ("`````\nunfinished", "``````"),
        ("keeps ``` and ````` mid-line", "``````"),
        ("```\r\nunfinished\r\n", "````"),
    ],
    ids=["no-backticks", "three", "five", "mid-line-run", "crlf"],
)
def test_the_fence_is_longer_than_any_backtick_run_in_the_message(content, fence):
    # A backtick fence ends only on a line of at least its own length, so one
    # longer than the longest run ANYWHERE in the message (three at least) cannot
    # be ended from inside. Verified by the parser, not by this arithmetic: the
    # wrapper is returned only if the stand-in heading after it survives.
    body = content if content.endswith("\n") else content + "\n"
    assert sm._fenced(content) == f"{fence}\n{body}{fence}\n"


def test_a_message_even_the_fence_cannot_settle_fails_the_write_and_leaves_nothing(
    tmp_path, monkeypatch
):
    # The last resort, and the one place a message still fails the export: the
    # renderer never ships a wrapper the parse has not confirmed, so when even the
    # fence does not keep the next heading structural the write ends and the
    # half-written document is removed rather than handed on.
    monkeypatch.setattr(sm, "_egress_tmp_dir", lambda: tmp_path)
    monkeypatch.setattr(sm, "_markdown_need", lambda tokens, boundary: (False, None))

    with pytest.raises(ValueError):
        sm.write_markdown_file(
            _bundle([{"role": "assistant", "content": "```\nunfinished", "ts": "t1"}])
        )

    assert list(tmp_path.iterdir()) == []


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


@pytest.mark.parametrize(
    "content",
    [
        "> <!-- unfinished",
        "- <!-- unfinished",
        "<div>\ntext <!-- unfinished",
        "- <script>\n  unfinished",
        "<template>\nunfinished",
        "use <script> in the head",
        '<div class="unfinished',
    ],
    ids=[
        "comment-in-quote",
        "comment-in-item",
        "comment-in-div",
        "script-in-item",
        "template",
        "script-in-prose",
        "unfinished-tag",
    ],
)
def test_what_commonmark_ends_without_a_terminator_draws_no_closer(content):
    # The guard's boundary, pinned so that moving it is a decision and not an
    # accident. Each of these leaves something open in HTML that a renderer passes
    # raw, yet CommonMark has already ended the construct that held it -- the quote
    # or item, the blank-terminated block, the paragraph -- so the parser reports
    # the next heading intact. The renderer acts on what the parser reports and on
    # nothing else (module docstring), so none of them draws a closer.
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
    _assert_later_heading_parses(out)


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


@pytest.mark.parametrize(
    "content, expected",
    [
        # CommonMark: the closer must be at least as long as the opener. A ``` inside
        # a ```` block is content.
        pytest.param(
            "````\n```\nstill open", "````", id="a_shorter_run_does_not_close_a_longer_fence"
        ),
        # CommonMark forbids an info string on a closing fence, so this is a second
        # OPENING inside the first block, not a close.
        pytest.param("```sh\nls\n```sh", "```", id="a_closer_with_an_info_string_does_not_close"),
        pytest.param(
            "    ```\nindented code, not a fence",
            "",
            id="a_fence_indented_past_three_spaces_is_not_a_fence",
        ),
        # A leading tab is four columns, so the line is indented code. Pinned because
        # the indent test measures spaces only and would be wrong on its own; the
        # prefix check is what rejects this, and a refactor could drop it.
        pytest.param("\t```", "", id="a_tab_indented_fence_is_not_a_fence"),
    ],
)
def test_fence_closer_and_indent_rules(content, expected):
    assert sm._unterminated_blocks(content) == expected


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


@pytest.mark.parametrize(
    "content, expected",
    [
        # The backtick restriction is specific to backtick fences; a tilde fence takes
        # an arbitrary info string, so the same guard must not reject one.
        pytest.param(
            "~~~ info ~ string\nstill open", "~~~", id="a_tilde_fence_may_carry_any_info_string"
        ),
        # A 4-backtick fence with a clean info string is a valid opener, so left
        # unterminated it draws its matching closer (and the closer matches length).
        pytest.param(
            "````python\nstill open",
            "````",
            id="a_longer_backtick_fence_with_a_clean_info_string_still_opens",
        ),
    ],
)
def test_an_info_string_opens_where_the_fence_kind_allows_it(content, expected):
    assert sm._unterminated_blocks(content) == expected


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


@pytest.mark.parametrize(
    "content, expected",
    [
        pytest.param("<!-- a note -->\ntext", "", id="a_balanced_html_comment_gets_no_closer"),
        # CommonMark starts a raw HTML block only when the LINE begins with <!--. One
        # appearing mid-line is inline text, and an unterminated one there is escaped
        # to literal characters rather than recognised — so it hides nothing, and
        # appending a --> would put a stray delimiter in the document.
        pytest.param(
            "see the <!-- marker here",
            "",
            id="a_mid_line_comment_opener_is_not_treated_as_a_comment",
        ),
        # Inside a fence the <!-- is escaped text, so the fence is the only thing
        # open. Two independent scans would wrongly append both closers.
        pytest.param(
            "```html\n<!-- example",
            "```",
            id="a_comment_opener_inside_a_code_fence_needs_only_the_fence_closed",
        ),
        # The mirror case: inside a comment the ``` line is raw HTML, not a fence, so
        # the comment is the only thing open.
        pytest.param(
            "<!-- note\n```", "<!-- -->", id="a_fence_line_inside_a_comment_does_not_open_a_fence"
        ),
        pytest.param(
            "<!-- a -->\n```py\nx = 1",
            "```",
            id="a_comment_closed_before_a_fence_opens_reports_only_the_fence",
        ),
    ],
)
def test_html_comment_and_fence_interplay(content, expected):
    assert sm._unterminated_blocks(content) == expected


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
                "workspace": "",
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


@pytest.mark.parametrize(
    "field, value, expected",
    [
        # The one place content is not verbatim: an unescaped pipe ends the cell.
        pytest.param(
            "workspace",
            "a|b",
            "| Workspace | a\\|b |",
            id="a_pipe_in_provenance_cannot_shift_the_table",
        ),
        pytest.param(
            "model", "a\nb", "| Model | a b |", id="a_newline_in_provenance_cannot_end_the_row"
        ),
    ],
)
def test_provenance_cannot_break_the_table_row(field, value, expected):
    out = _render(_bundle(MSGS, source={field: value}))
    assert expected in out


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


@pytest.mark.parametrize(
    "content, expected",
    [
        # The inner item ends at its content column. The following two-space fence
        # belongs to the outer item, whose separator dedent closes it without a
        # manufactured top-level closer.
        pytest.param(
            "- outer\n  - inner\n\n  ```python\n  code",
            "",
            id="nested_list_exit_restores_the_enclosing_items_fence_ownership",
        ),
        pytest.param(
            "# Examples\n2. item\n   ```python\n   code",
            "",
            id="heading_ends_a_paragraph_before_an_ordered_list_marker",
        ),
        pytest.param(
            "```python\ncode\n```\xa0", "```", id="only_ascii_space_or_tab_may_pad_a_fence_closer"
        ),
    ],
)
def test_scanner_state_keeps_a_later_heading_structural(content, expected):
    content = content
    assert sm._unterminated_blocks(content) == expected
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
    [
        "<pre>\n<!-- unfinished\n</pre>\n```python\ncode",
        "- <pre>\n  <!-- unfinished\n  </pre>\n```python\ncode",
        "- <div>\n  <!-- unfinished\n\n```python\ncode",
    ],
    ids=["raw-html", "raw-html-list", "blank-html-list"],
)
def test_html_container_exit_leaves_a_later_code_fence_top_level(content):
    # The raw/blank HTML block ends in Markdown before the later fence, so the
    # fence is a top-level one and owes its closer. The parser decides that, the
    # same as it does for a fence with no HTML above it.
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_parses(out)


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


# ── an effective closer, proven against rendered HTML ────────────────────
#
# A manufactured closer is only correct if it holds in the ACTUALLY RENDERED HTML,
# not merely in markdown-it's token stream. `_assert_later_heading_is_structural`
# covers both; this pins the case with no surrounding container at all.


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
    assert sm._unterminated_blocks(content) == "```"
    _assert_later_heading_parses(out)


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
        # An empty item holds no paragraph, so a dedented line ends it and the
        # following fence is top level.
        ("-\nx\n  ```", "```"),
        ("+\n١.\n  ~~~", "~~~"),
        # A setext underline needs a paragraph above it; a lone "=" is text, so a
        # lone tag after it is a lazy line and the fence below still opens.
        ("=\n<e>\n```", "```"),
    ],
    ids=["empty-item-fence", "empty-item-digit", "lone-setext"],
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
    "content",
    [
        "> <!--\n```\ncode",
        "> <!--\n\n```\ncode",
        "> > <!--\n> text\n```\ncode",
        "- > <!--\n```\ncode",
        "> - <!--\n> text\n```\ncode",
    ],
    ids=[
        "dedented-fence",
        "blank-then-fence",
        "inner-quote-ends",
        "list-quote-dedent",
        "quote-list-dedent",
    ],
)
def test_gpt_6_a_quoted_comment_releases_the_markdown_when_its_quote_ends(content):
    # F1: "> <!--" opens a comment inside a block quote. An HTML block cannot be
    # lazily continued, so the first line without the ">" marker ENDS the quote
    # and the HTML block with it -- a dedented fence there is a real top-level
    # fence, and the parser says so. That fence is the only closer owed: the
    # comment the quote ended without its terminator is outside what a parse
    # reports (see the module docstring).
    assert sm._unterminated_blocks(content) == "```"
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_parses(out)


@pytest.mark.parametrize(
    "content",
    ["> <!--\n> ```\n> code", "> - <!--\n>\n>   ```\n>   code", "> <!--\n- item\n  ```\n  code"],
    ids=["quoted-fence", "quoted-item-blank-then-fence", "fence-in-later-list"],
)
def test_gpt_6_a_quoted_comment_still_owns_lines_that_stay_in_its_quote(content):
    # The other direction of F1: while the quote marker is still there, a fence
    # line is comment content and opens nothing, so no fence closer is drawn --
    # one would open a NEW top-level fence after the quote ends. A blank quoted
    # line keeps a list item inside the quote open, so the item's lines after it
    # are still the comment's. A fence inside a later list item is the item's,
    # and the separator ends the item and the fence with it.
    assert sm._unterminated_blocks(content) == ""
    out = _render(
        _bundle(
            [
                {"role": "assistant", "content": content, "ts": "t1"},
                {"role": "user", "content": "later", "ts": "t2"},
            ]
        )
    )
    _assert_later_heading_parses(out)


# ── fork-lane review, second round ──


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


# ── fork-lane review, third round: the parser decides ──


def test_gpt_14_a_raw_block_ended_by_another_tags_terminator_frees_the_fence():
    # CommonMark ends a <pre> block at ANY of </pre>, </script>, </style> or
    # </textarea>, so the "~~~" after "</script>" is a real fence that must be
    # closed, and the <pre> block it follows needs nothing: the parser has
    # already ended it.
    content = "<pre>\n</script>\n~~~"
    assert sm._unterminated_blocks(content) == "~~~"
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


def test_a_block_this_module_cannot_close_draws_nothing():
    # The ceiling on closers and the "unknown block" exit are the two ways the
    # loop ends without a clean parse. Neither invents a closer: a wrong one is
    # worse than none, because it can open the very construct it claims to end.
    # The stand-in lands in a paragraph, and in an HTML block that ends at a blank
    # line and so has no entry in the table: both are blocks it has no closer for.
    assert sm._MAX_CLOSERS >= 4
    boundary = sm._SENTINEL_BASE
    for source in ("plain", f"swallowed {boundary}", f"<div>\n{boundary}"):
        assert sm._markdown_need(sm._PARSER.parse(source), boundary) == (False, None), source


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
