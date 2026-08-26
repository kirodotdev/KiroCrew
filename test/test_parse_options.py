"""Tests for _parse_options — powers inline Waiting-lane buttons on the board."""
from kiro_crew.dashboard.state import _parse_options


def test_parse_simple_options():
    assert _parse_options("Pick one.\n[OPTIONS: A | B | C]") == ["A", "B", "C"]


def test_no_options_returns_empty():
    assert _parse_options("Just prose with no marker.") == []
    assert _parse_options("") == []


def test_single_option():
    assert _parse_options("[OPTIONS: Go]") == ["Go"]


def test_whitespace_is_stripped():
    assert _parse_options("[OPTIONS:   Apply now  |   Hold off   ]") == ["Apply now", "Hold off"]


def test_multiple_markers_uses_last():
    # An assistant message might quote an earlier OPTIONS block then ask a new question.
    txt = "Earlier I said [OPTIONS: old1 | old2]. Now choose:\n[OPTIONS: new1 | new2 | new3]"
    assert _parse_options(txt) == ["new1", "new2", "new3"]


def test_empty_parts_filtered():
    assert _parse_options("[OPTIONS: A ||| B]") == ["A", "B"]


def test_multiline_content_in_message():
    txt = """Some explanation.
Line two.
More text.

[OPTIONS: Ship it | Park it | Show diff]"""
    assert _parse_options(txt) == ["Ship it", "Park it", "Show diff"]


def test_bracket_inside_option_text():
    # A literal ']' inside an option must not truncate the pill (the old
    # [^\]]+ regex captured only "Fix [x"). Closing ']' is anchored to EOL.
    assert _parse_options("Pick one.\n[OPTIONS: Fix [x] logging | Skip]") == [
        "Fix [x] logging",
        "Skip",
    ]


def test_bracket_at_end_of_each_option():
    assert _parse_options("[OPTIONS: a[1] | b[2]]") == ["a[1]", "b[2]"]


def test_body_does_not_span_newlines():
    # The MULTILINE body must stay single-line: text mentioning "[OPTIONS:"
    # mid-line with a LATER line ending in "]" must NOT be parsed as one span
    # (that would emit bogus pills from unrelated prose). The tempered body
    # excludes \n (``[^[\n]``) so a real [OPTIONS:] block is still detected
    # while cross-line spans are not.
    assert _parse_options("Earlier [OPTIONS: draft\nmore prose ]") == []
    # A genuine single-line block after mid-text prose still parses.
    assert _parse_options("Earlier [OPTIONS text.\nNow:\n[OPTIONS: A | B]") == ["A", "B"]


def test_trailing_markdown_link_close_tolerated():
    # Models sometimes append a stray "(OPTIONS)" (or any "(...)") right after the
    # marker, e.g. "[OPTIONS: A | B](OPTIONS)". That both breaks the end anchor and
    # forms a valid [label](url) Markdown link, so the marker used to leak as a
    # clickable link instead of buttons. The optional link-close is now absorbed —
    # and stays OUTSIDE the label capture, so choices are unaffected.
    assert _parse_options("Pick one.\n[OPTIONS: A | B | C](OPTIONS)") == ["A", "B", "C"]
    assert _parse_options("[OPTIONS: Ship it | Park it](https://x)") == ["Ship it", "Park it"]
    # The "(" must abut the "]" — a spaced "] (note)" is NOT a link close, so the
    # anchor fails and the marker is left unparsed (deliberate trailing-note case).
    assert _parse_options("[OPTIONS: A | B] (see note)") == []
