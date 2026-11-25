"""``clean_compact_focus``: the one place a channel's ``/compact <text>`` is cleaned."""

from __future__ import annotations

import pytest

from kiro_crew.messaging.commands import clean_compact_focus


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   \n\t ", ""),
        ("keep the API decisions", "keep the API decisions"),
        ("  keep\n the\ttab   and  gaps ", "keep the tab and gaps"),
        ("keep\x00 this\x1b[31m", "keep this[31m"),
        ("a" * 600, "a" * 500),
    ],
)
def test_focus_is_one_clean_line(raw: str, expected: str) -> None:
    assert clean_compact_focus(raw) == expected


def test_the_cut_never_leaves_a_trailing_space() -> None:
    assert clean_compact_focus("a" * 499 + " b", limit=500) == "a" * 499


def test_a_custom_limit_applies() -> None:
    assert clean_compact_focus("abcdef", limit=3) == "abc"
