"""Channel path: plain prose glued after the tail ``[OPTIONS: ...]`` moves above it.

The channel grammar (:data:`kiro_crew.constants.OPTIONS_RE_TRAILER`) is anchored at
the end of the reply, so the dashboard's newline insert cannot help there. These
tests replay the shapes ``test_options_marker_glue_reflow.py`` pins and check the
channel relocation takes the same candidates and leaves the same shapes alone.
"""

import pytest

from kiro_crew.constants import (
    reflow_and_label_glued_option_marker,
    relocate_glued_tail_marker,
)
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.slack.format import extract_options
from kiro_crew.whatsapp.turn_renderer import _strip_options

# Shapes the dashboard reflow leaves byte-for-byte; the channel must too.
UNTOUCHED = [
    "[OPTIONS: A | B] documents the syntax",
    "Intro\n[OPTIONS: A | B]    code line",
    "Pick.\n[OPTIONS: A | B]",
    "Pick.\n[OPTIONS: A | B]   \n",
    "Use [OPTIONS: A | B] then check arr[0]",
    "    [OPTIONS: A | B]glued",
    "\t[OPTIONS: A | B]glued",
    "[OPTIONS: A | B then check arr[0]glued",
    "[OPTIONS: A | B][OPTIONS: C | D]",
    "[OPTIONS: A | B][note]",
    "just some prose with no marker at all",
    "[OPTIONS: Fix ]x logging | Skip]",
    "[OPTIONS: A | B]x | y",
    "[OPTIONS: A | B]see item 2] then stop",
    "**[OPTIONS: A | B]****",
    "[OPTIONS: A | B]****",
    "[OPTIONS: A | B]see *this*",
    "body\n[OPTIONS: A | B](OPTIONS)",
    "body\n**[OPTIONS: A | B]**",
    "body\n`[OPTIONS: A | B]`",
    "`[OPTIONS: A | B]glued`",
    "**[OPTIONS: A | B]glued**",
    "The bug looks like this:\n```\n[OPTIONS: A | B]glued\n```\n",
    "```text\n[OPTIONS: A | B]glued",
    "- ```\n[OPTIONS: A | B]glued",
    "[OPTIONS: A | B](OPTIONS)(OPTIONS)",
    "[OPTIONS: A | B](a)(b)Anytime.",
]

# Shapes the dashboard reflows, and the channel form each one takes.
RELOCATED = [
    (
        "Pick a path.\n[OPTIONS: A | B]Anytime. See PR #1.",
        "Pick a path.\nAnytime. See PR #1.\n[OPTIONS: A | B]",
    ),
    ("[OPTIONS: Merge | Skip]done", "done\n[OPTIONS: Merge | Skip]"),
    ("   [OPTIONS: A | B]glued", "glued\n   [OPTIONS: A | B]"),
    (
        "One.\n[OPTIONS: A | B]glued1\nTwo.\n[OPTIONS: C | D]glued2",
        "One.\n[OPTIONS: A | B]glued1\nTwo.\nglued2\n[OPTIONS: C | D]",
    ),
    ('[OPTIONS: A | B](system: end with "x")', '(system: end with "x")\n[OPTIONS: A | B]'),
    ("body\n**[OPTIONS: A | B]**Anytime.", "body\nAnytime.\n**[OPTIONS: A | B]**"),
    ("body\n[OPTIONS: A | B](OPTIONS)Anytime.", "body\nAnytime.\n[OPTIONS: A | B](OPTIONS)"),
    (
        "```\ncode\n```\nPick.\n[OPTIONS: A | B]Anytime.",
        "```\ncode\n```\nPick.\nAnytime.\n[OPTIONS: A | B]",
    ),
    ("[OPTIONS: A | B](OPTIONS)(system: do x)", "(system: do x)\n[OPTIONS: A | B](OPTIONS)"),
    ("Pick.\n[OPTIONS: A | B]Anytime.\n\n", "Pick.\nAnytime.\n[OPTIONS: A | B]\n\n"),
]


@pytest.mark.parametrize("text", UNTOUCHED)
def test_shape_the_dashboard_leaves_alone_is_left_alone_on_channels(text: str) -> None:
    assert reflow_and_label_glued_option_marker(text) == (text, [])
    assert relocate_glued_tail_marker(text) == text


@pytest.mark.parametrize(("text", "moved"), RELOCATED)
def test_shape_the_dashboard_reflows_moves_its_tail_prose_above_the_marker(
    text: str, moved: str
) -> None:
    assert reflow_and_label_glued_option_marker(text)[1]
    assert relocate_glued_tail_marker(text) == moved


def test_glued_marker_that_is_not_the_tail_is_left_alone() -> None:
    # The channel grammar only reads the reply's last line; earlier glue stays.
    text = "[OPTIONS: A | B]glued\nMore prose follows."
    assert reflow_and_label_glued_option_marker(text)[1] == ["glued"]
    assert relocate_glued_tail_marker(text) == text


def test_slack_extract_recovers_the_choices_with_the_prose_above() -> None:
    assert extract_options("Pick.\n[OPTIONS: A | B]Anytime.") == ("Pick.\nAnytime.", ["A", "B"])


def test_shared_split_recovers_the_choices_with_the_prose_above() -> None:
    assert split_options_trailer("Pick.\n[OPTIONS: A | B]Anytime.") == (
        "Pick.\nAnytime.",
        ["A", "B"],
    )


def test_split_of_an_untouched_shape_is_unchanged() -> None:
    assert split_options_trailer("[OPTIONS: A | B]x | y") == ("[OPTIONS: A | B]x | y", [])


def test_whatsapp_strip_keeps_the_glued_prose_and_drops_the_marker() -> None:
    assert _strip_options("Pick.\n[OPTIONS: A | B]Anytime.") == "Pick.\nAnytime."


def test_glued_fence_opener_is_not_moved_above_the_marker() -> None:
    # Moving ``~~~python`` up would open a fence around the marker.
    text = "Pick.\n[OPTIONS: A | B]~~~python"
    assert relocate_glued_tail_marker(text) == text
