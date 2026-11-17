"""Same-line and nested ``[OPTIONS:]`` markers parse as the web parser reads them.

Cases mirror the web dashboard's same-line, nesting-depth and terminator
tests. Each is checked on every channel reader:
the line grammar's Slack and dashboard readers, and the trailer grammar's
renderer, so the two grammars cannot answer one shape differently.
"""

from __future__ import annotations

import pytest

from kiro_crew.constants import OPTIONS_RE_LINE, OPTIONS_RE_TRAILER
from kiro_crew.dashboard.state_parts.slot_transcript import _parse_options, _strip_options
from kiro_crew.messaging.renderer import split_options_trailer
from kiro_crew.slack.format import extract_options


def _readers(raw: str) -> dict[str, tuple[str, list[str]]]:
    return {
        "slack": extract_options(raw),
        "dashboard": (_strip_options(raw), _parse_options(raw)),
        "renderer": split_options_trailer(raw),
    }


def _assert_every_reader(raw: str, text: str, options: list[str]) -> None:
    for name, got in _readers(raw).items():
        assert got == (text, options), name


@pytest.mark.parametrize(
    ("raw", "text", "options"),
    [
        ("Body [OPTIONS: A] [OPTIONS: B]", "Body", ["B"]),
        ("[OPTIONS: A] [OPTIONS: B] [OPTIONS: C]", "", ["C"]),
        ("[OPTIONS: A] [OPTIONS: B then arr[0]]", "", ["B then arr[0]"]),
        ("Body [OPTIONS: A](OPTIONS) [OPTIONS: B]", "Body", ["B"]),
    ],
)
def test_a_same_line_chain_strips_every_marker_and_the_last_offers(raw, text, options):
    _assert_every_reader(raw, text, options)


def test_a_long_same_line_chain_is_one_run():
    line = "[OPTIONS: A] " * 255 + "[OPTIONS: B]"
    _assert_every_reader(line, "", ["B"])


@pytest.mark.parametrize(
    "raw",
    [
        "See [OPTIONS: A | B] for details",
        "[OPTIONS: A] [OPTIONS: B] in docs",
        "[OPTIONS: A] [OPTIONS: B] [OPTIONS: C] see above",
        "[OPTIONS: A] [OPTIONS: B then arr[0]",
        "[OPTIONS: A] [OPTIONS: B] [OPTIONS: C then arr[0]",
        "[OPTIONS: dropped [OPTIONS: A | B]",
        "[options: dropped [OPTIONS: A | B]",
        "[OPTIONS: x, then [OPTIONS: Yes | No]",
    ],
)
def test_a_line_that_does_not_parse_whole_keeps_all_its_text(raw):
    _assert_every_reader(raw, raw, [])
    assert OPTIONS_RE_LINE.sub("", raw) == raw
    assert OPTIONS_RE_TRAILER.search(raw) is None


def test_a_broken_chain_does_not_poison_a_marker_on_another_line():
    raw = "[OPTIONS: A] [OPTIONS: B then arr[0]\n[OPTIONS: C]"
    _assert_every_reader(raw, "[OPTIONS: A] [OPTIONS: B then arr[0]", ["C"])


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("Use [OPTIONS: A | B], then pick [OPTIONS: Yes | No]", "Use [OPTIONS: A | B], then pick"),
        ("Use [OPTIONS: A],then [OPTIONS: Yes | No]", "Use [OPTIONS: A],then"),
    ],
)
def test_a_head_closed_before_inline_prose_does_not_stay_open(raw, text):
    _assert_every_reader(raw, text, ["Yes", "No"])


@pytest.mark.parametrize(
    ("raw", "options"),
    [
        ("[OPTIONS: Alpha ] | Bravo ]]", ["Alpha ]", "Bravo ]"]),
        ("[OPTIONS: Alpha ], Bravo ]", ["Alpha ], Bravo"]),
    ],
)
def test_a_label_that_carries_a_closer_still_parses(raw, options):
    _assert_every_reader(raw, "", options)


def test_siblings_stay_when_the_last_marker_offers_nothing():
    raw = "[OPTIONS: A] [OPTIONS:]"
    _assert_every_reader(raw, "[OPTIONS: A]", [])


def test_a_message_over_the_head_cap_parses_no_marker():
    raw = ("[OPTIONS: A] " * 257).rstrip()
    _assert_every_reader(raw, raw, [])
    assert OPTIONS_RE_LINE.sub("", raw) == raw


class _CopyMeter(str):
    """Counts the characters ``find``, slicing and indexing walk, read or copy."""

    walked = 0

    def find(self, sub, start=0, end=None):
        stop = len(self) if end is None else end
        type(self).walked += max(stop - start, 0)
        return super().find(sub, start, end)

    def __getitem__(self, key):
        if isinstance(key, slice):
            type(self).walked += len(range(*key.indices(len(self))))
        else:
            type(self).walked += 1
        return super().__getitem__(key)


def test_the_line_walk_stays_linear_on_comma_followed_closers():
    text = _CopyMeter("[OPTIONS: " + "a], " * 400 + "[OPTIONS: B]")
    _CopyMeter.walked = 0
    list(OPTIONS_RE_LINE.finditer(text))
    assert _CopyMeter.walked <= 8 * len(text)
