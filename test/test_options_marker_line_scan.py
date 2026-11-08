"""The line-anchored marker scan reads only the lines that hold its head.

``OPTIONS_RE_LINE`` runs for every open tab's last assistant message on each
``serialize_slots`` pass, and in the preview, Slack and member-waiting paths.
Its pattern opens with an optional ``^``-anchored wrapper prefix, so a scan of
the whole message attempts a match at every character: the cost grows with the
message, not with the one line the marker sits on.

A LINE match cannot leave its line -- it contains the literal ``[OPTIONS:`` and
every part of the grammar excludes ``\\n`` -- so a line without the head cannot
match. These tests pin both halves of that: the scan is bounded by the marker
lines, and what it returns is exactly what the whole-text scan returns.
"""

from __future__ import annotations

import sys

from hypothesis import example, given, settings
from hypothesis import strategies as st

from kiro_crew import constants
from kiro_crew.constants import (
    _RAW_OPTIONS_RE_LINE,
    OPTIONS_RE_LINE,
    _marker_labels_have_unmatched_opener,
)


class _SpanSpy:
    """Stands in for the compiled LINE pattern and records each span it scans."""

    def __init__(self, real):
        self._real = real
        self.pattern = real.pattern
        self.spans: list[tuple[int, int]] = []

    def finditer(self, text, pos=0, endpos=sys.maxsize):
        self.spans.append((pos, min(endpos, len(text))))
        return self._real.finditer(text, pos, endpos)


def test_scan_reads_only_the_marker_line(monkeypatch):
    marker = "[OPTIONS: Merge it | Show diff | Skip]"
    prose = "Some prose with `code` and a [link](https://x.y/z) here.\n" * 360
    text = prose + marker
    spy = _SpanSpy(_RAW_OPTIONS_RE_LINE)
    monkeypatch.setattr(constants, "_RAW_OPTIONS_RE_LINE", spy)
    monkeypatch.setattr(OPTIONS_RE_LINE, "_pattern", spy)

    matches = list(OPTIONS_RE_LINE.finditer(text))

    assert [m.group("labels") for m in matches] == [" Merge it | Show diff | Skip"]
    scanned = sum(end - start for start, end in spy.spans)
    assert scanned <= len(
        marker
    ), f"scanned {scanned} of {len(text)} chars for a {len(marker)}-char marker line"


def test_text_without_the_head_is_not_scanned(monkeypatch):
    spy = _SpanSpy(_RAW_OPTIONS_RE_LINE)
    monkeypatch.setattr(constants, "_RAW_OPTIONS_RE_LINE", spy)
    monkeypatch.setattr(OPTIONS_RE_LINE, "_pattern", spy)

    assert list(OPTIONS_RE_LINE.finditer("plain prose\n" * 1000)) == []
    assert sum(end - start for start, end in spy.spans) == 0


def _whole_text(text: str) -> list[tuple[tuple[int, int], dict[str, str | None]]]:
    """The reference answer: the raw pattern over all of *text*, plus the balance filter."""
    return [
        (m.span(), m.groupdict())
        for m in _RAW_OPTIONS_RE_LINE.finditer(text)
        if not _marker_labels_have_unmatched_opener(m.group("labels"))
    ]


#: Fragments the grammar gives meaning to, so generated text keeps landing on
#: marker heads, wrappers, line breaks and every bracket kind.
_FRAGMENTS = [
    "[OPTIONS:",
    "[OPTIONS: A | B]",
    "[",
    "]",
    "\u3010",
    "\u3011",
    "\uff3b",
    "\uff3d",
    "\u3014",
    "\u3015",
    "|",
    ",",
    " ",
    "\t",
    "\n",
    "\r\n",
    "\r",
    "`",
    "**",
    "_",
    "(x)",
    "(",
    ")",
    "Merge it",
    "arr[0]",
    "dict[str | int]",
    "OPTIONS:",
    "x",
]


@settings(max_examples=500)
@given(st.lists(st.sampled_from(_FRAGMENTS), max_size=40).map("".join))
@example("pick one\n[OPTIONS: yes | no | later]")
@example("first\n[OPTIONS: a | b]\nsecond\n[OPTIONS: c | d]")
@example("inline [OPTIONS: a | b] mid-line text")
@example("  **[OPTIONS: wrapped | bold]**  ")
@example("[OPTIONS: Fix [x logging | Skip]")
@example("[OPTIONS: Alpha ] | Bravo ]]")
@example("[OPTIONS: A \u3010x] | B]")
@example("x\r\n[OPTIONS: a | b]\r\n")
@example("[OPTIONS: a | b] [OPTIONS: c | d]")
@example("[OPTIONS: a [OPTIONS: b | c]\n[OPTIONS: d]")
# One shape per part of the grammar that would cross a line if it admitted
# ``\n`` -- the body, the leading wrapper run, the trailing run -- so such an
# edit fails here on every run, not only when generation happens to hit it.
@example("[OPTIONS: a\nb]")
@example("\n  \n**[OPTIONS: a]**")
@example("[OPTIONS: a | b]  \n\nmore")
def test_line_scan_matches_the_whole_text_scan(text):
    got = [(m.span(), m.groupdict()) for m in OPTIONS_RE_LINE.finditer(text)]
    assert got == _whole_text(text)
    assert OPTIONS_RE_LINE.sub("", text) == _RAW_OPTIONS_RE_LINE.sub(
        lambda m: m.group(0) if _marker_labels_have_unmatched_opener(m.group("labels")) else "",
        text,
    )
