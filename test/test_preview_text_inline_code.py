"""Inline code and ``~~~`` fences in `preview_text.strip_markdown_preview`.

Inline code keeps its literal text: the emphasis, strike and link passes
never touch the code's contents, so `__init__` and `**kwargs` stay as typed.
A ``~~~`` fence is a fence, so its tildes become a ``(code)`` placeholder.
The dashboard's own ``stripMd`` (website notifMeta.tsx) behaves the same.
"""

from __future__ import annotations

import pytest

from kiro_crew.preview_text import speech_preview, strip_markdown_preview


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("call `__init__` now", "call __init__ now"),
        ("pass `**kwargs` and `*args`", "pass **kwargs and *args"),
        ("use `~~x~~` here", "use ~~x~~ here"),
        ("see `[a](b)` there", "see [a](b) there"),
        ("type `# not a header`", "type # not a header"),
    ],
)
def test_inline_code_keeps_markdown_markers(src, expected):
    assert strip_markdown_preview(src) == expected


def test_emphasis_around_inline_code_still_stripped():
    assert strip_markdown_preview("**run `make test` first**") == "run make test first"


def test_link_label_with_inline_code_keeps_code():
    assert strip_markdown_preview("see [`__init__`](https://x.test/a)") == "see __init__"


def test_several_spans_restore_in_order():
    assert strip_markdown_preview("`a_*_b` then `__c__` then `d`") == "a_*_b then __c__ then d"


def test_nul_in_input_cannot_forge_a_span_token():
    assert strip_markdown_preview("x \x000\x00 `y`") == "x 0 y"


def test_tilde_fence_becomes_placeholder():
    assert strip_markdown_preview("~~~py\nx=1\n~~~\nafter") == "(code) after"


def test_tilde_diff_fence_becomes_diff_placeholder():
    assert strip_markdown_preview("~~~diff\n--- a\n+++ b\n~~~") == "(diff)"


def test_unterminated_tilde_fence_still_stripped():
    assert strip_markdown_preview("Done:\n~~~\nprint(1)") == "Done: (code)"


def test_backticks_inside_tilde_fence_stay_in_the_block():
    assert strip_markdown_preview("~~~md\n```js\nx\n```\n~~~\nend") == "(code) end"


def test_short_tilde_run_does_not_close_a_longer_fence():
    assert strip_markdown_preview("~~~~py\na\n~~~\nb\n~~~~\nafter") == "(code) after"


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        ("show ``a`__b__`` now", "show a`__b__ now"),
        ("show `` `**x**` `` now", "show `**x**` now"),
        ("lone `` run and `__y__`", "lone `` run and __y__"),
    ],
)
def test_multi_backtick_spans_keep_code_intact(src, expected):
    assert strip_markdown_preview(src) == expected


@pytest.mark.parametrize("marker", ["`[OPTIONS: Yes | No]`", "[OPTIONS: Yes | No]"])
def test_options_line_still_dropped(marker):
    assert strip_markdown_preview(f"Pick one\n{marker}") == "Pick one"


def test_strike_still_stripped_outside_code():
    assert strip_markdown_preview("~~old~~ and `~~kept~~`") == "old and ~~kept~~"


def test_speech_preview_keeps_inline_code_markers():
    assert speech_preview("set `__all__`") == "set __all__"
