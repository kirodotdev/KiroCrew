"""The inline-image scan reads each character a bounded number of times.

``iter_local_refs``, ``open_ref_start`` and ``hide_local_refs`` run on every
outbound message, and ``hide_local_refs`` on every streamed frame of a reply, so
text made of many openers must not make each opener re-read the rest of it.

Two properties are pinned. The scan returns exactly what the straightforward
reading returns -- ``IMAGE_MD_RE.finditer``, a destination walk from each match,
and a per-marker literal check -- on random markup-dense text. And the work it
does, counted in characters handed to its primitives, at most doubles when the
input doubles (testing-conventions class 5: count the work, do not time it).
"""

from __future__ import annotations

import itertools
import random
import re

import pytest

from kiro_crew import image_artifacts
from kiro_crew.messaging import outbound_files as of
from kiro_crew.widget_parse import mask_inline_code

#: The pattern itself, held before any test swaps the module attribute.
_IMAGE_MD_RE = of.IMAGE_MD_RE

# -- the straightforward reading, kept here as the oracle ----------------------


def _oracle_literal(text: str, offset: int, fenced: list[tuple[int, int]]) -> bool:
    prefix = text[:offset]
    escaped = (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 1
    line_start = text.rfind("\n", 0, offset) + 1
    line = text[line_start:].split("\n", 1)[0]
    column = offset - line_start
    boundaries = [*fenced, *(m.span() for m in of._BLANK_LINE_RE.finditer(text))]
    block_start = max((end for _s, end in boundaries if end <= offset), default=0)
    block_end = min((s for s, _e in boundaries if s > offset), default=len(text))
    return (
        escaped
        or any(start <= offset < end for start, end in fenced)
        or line[:column].expandtabs(4).startswith("    ")
        or mask_inline_code(text[block_start:block_end])[offset - block_start] == " "
    )


def _oracle_refs(text: str) -> list[tuple[int, int, str, str]]:
    fenced = list(of.iter_fence_spans(text))
    refs = []
    for match in of.IMAGE_MD_RE.finditer(text):
        if _oracle_literal(text, match.start(), fenced):
            continue
        dest, consumed = of._walk_destination(text[match.end() :])
        if not dest or of.is_remote_destination(dest):
            continue
        alt = of.unescape_md(match.group(1) or "").strip()
        refs.append((match.start(), match.end() + consumed, dest, alt))
    return refs


def _oracle_open_start(text: str) -> int | None:
    fenced = list(of.iter_fence_spans(text))
    for opener in re.finditer(r"!\[", text):
        if _oracle_literal(text, opener.start(), fenced):
            continue
        closed = of.IMAGE_MD_RE.match(text, opener.start())
        if closed is None or of._walk_destination(text[closed.end() :])[0] is None:
            return opener.start()
    return None


def _oracle_destinations(text: str) -> list[tuple[int, str | None]]:
    return [(m.start(), of.md_destination(text[m.end() :])) for m in of.IMAGE_MD_RE.finditer(text)]


#: Fragments chosen so random text is dense in every construct the scan
#: distinguishes: openers, escapes of each kind, brackets, nested and unbalanced
#: parentheses, code spans, fences, indents, blank lines and destinations.
_FRAGMENTS = [
    "![", "!", "[", "]", "](", "(", ")", "\\", "\\!", "\\[", "\\]", "\\(", "\\)",
    "\\\\", "`", "``", "\n", "\n\n", "\n```\n", "    ", "\t", " ", "a", "/tmp/x.png",
    "<", ">", '"t"', "http://h/p.png", "\r", "\x01",
]  # fmt: skip


def _random_texts(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    return [
        "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(0, 40))) for _ in range(count)
    ]


@pytest.mark.parametrize("seed", range(4))
def test_scan_matches_the_straightforward_reading(seed: int) -> None:
    for text in _random_texts(seed, 2500):
        got = [(r.start, r.end, r.dest, r.alt) for r in of.iter_local_refs(text)]
        assert got == _oracle_refs(text), text
        assert of.open_ref_start(text) == _oracle_open_start(text), text
        assert [(m.start, m.end, m.alt) for m in of.iter_image_matches(text)] == [
            (m.start(), m.end(), m.group(1)) for m in of.IMAGE_MD_RE.finditer(text)
        ], text
        destinations = [(m.start, dest) for m, dest, _ in of.iter_image_destinations(text)]
        assert destinations == _oracle_destinations(text), text


def test_literal_reader_matches_the_per_marker_check() -> None:
    for text in _random_texts(99, 2500):
        fenced = list(of.iter_fence_spans(text))
        reader = of._MarkerReader(text, fenced)
        for offset in range(len(text)):
            assert reader.literal(offset) == _oracle_literal(text, offset, fenced), (text, offset)


@pytest.mark.parametrize(
    ("text", "dest", "alt"),
    [
        (r"![Revenue \[Q1\]](/tmp/p.png)", "/tmp/p.png", "Revenue [Q1]"),
        ("![a ![b](/tmp/p.png)", "/tmp/p.png", "a ![b"),
        ("![s](/tmp/screenshot(1).png)", "/tmp/screenshot(1).png", "s"),
        (r"![w](C:\Users\me\p.png)", r"C:\Users\me\p.png", "w"),
        ('![c](</tmp/a b/c.png> "t")', "/tmp/a b/c.png", "c"),
    ],
)
def test_ordinary_forms_still_extract(text: str, dest: str, alt: str) -> None:
    assert [(r.dest, r.alt) for r in of.iter_local_refs(text)] == [(dest, alt)]


# -- the work the scan does, counted -------------------------------------------


class _Work:
    """Characters the scan reads, counted where it reads them.

    The text is handed in as :class:`_CountingText`, so every index, slice,
    split, strip and search on it is charged by the characters it touches --
    including the per-character loops of the label scan and the scan's indexes,
    so an index rebuilt per image is charged once per image. The destination walk
    and the inline-code mask receive plain strings and are wrapped at their
    module call sites instead, and any use of ``IMAGE_MD_RE`` over the text is
    charged as quadratic.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.chars = 0
        real_walk, real_mask = of._walk_destination, of.mask_inline_code
        real_reader = of._MarkerReader
        work = self

        class _NoRegexOverText:
            """The shipped scan reads the text by hand; any regex pass is charged."""

            def match(self, text, pos=0):  # type: ignore[no-untyped-def]
                work.chars += len(text) ** 2
                return _IMAGE_MD_RE.match(text, pos)

            def finditer(self, text):  # type: ignore[no-untyped-def]
                work.chars += len(text) ** 2
                return _IMAGE_MD_RE.finditer(text)

        class _CountingReader(real_reader):  # type: ignore[misc, valid-type]
            def __init__(self, text, fenced):  # type: ignore[no-untyped-def]
                work.chars += len(text) + 1  # the regex passes over the whole text
                super().__init__(text, fenced)

        def walk(rest):  # type: ignore[no-untyped-def]
            work.chars += len(rest) + 1
            return real_walk(rest)

        def mask(line):  # type: ignore[no-untyped-def]
            work.chars += len(line) + 1
            return real_mask(line)

        monkeypatch.setattr(of, "IMAGE_MD_RE", _NoRegexOverText())
        monkeypatch.setattr(of, "_MarkerReader", _CountingReader)
        monkeypatch.setattr(of, "_walk_destination", walk)
        monkeypatch.setattr(of, "mask_inline_code", mask)

    def text(self, value: str) -> str:
        return _CountingText(value, self)


class _CountingText(str):
    """A ``str`` that charges its reader for every character an operation touches."""

    _work: _Work

    def __new__(cls, value: str, work: _Work):  # type: ignore[no-untyped-def]
        obj = super().__new__(cls, value)
        obj._work = work
        return obj

    def __getitem__(self, key):  # type: ignore[no-untyped-def]
        got = super().__getitem__(key)
        self._work.chars += len(got) + 1
        return got

    def _span(self, start, end):  # type: ignore[no-untyped-def]
        lo = 0 if start is None else start
        hi = len(self) if end is None else end
        self._work.chars += max(hi - lo, 0) + 1

    def find(self, sub, start=None, end=None):  # type: ignore[no-untyped-def]
        got = super().find(sub, start, end)
        self._span(start, (got + len(sub)) if got >= 0 else end)
        return got

    def rfind(self, sub, start=None, end=None):  # type: ignore[no-untyped-def]
        got = super().rfind(sub, start, end)
        self._span(got if got >= 0 else start, end)
        return got

    def split(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self._work.chars += len(self) + 1
        return super().split(*args, **kwargs)

    def rstrip(self, *args):  # type: ignore[no-untyped-def]
        self._work.chars += len(self) + 1
        return super().rstrip(*args)

    def strip(self, *args):  # type: ignore[no-untyped-def]
        self._work.chars += len(self) + 1
        return super().strip(*args)


#: Input shapes that each made one of the scans re-read the rest of the text per
#: opener: unclosed openers, escaped openers, a backslash run that hides every
#: opener, many complete images on one line and on many lines, many unclosed
#: destinations, openers in code spans, destinations that open more parentheses
#: than they close, and images nested in each other's destinations.
_PUMPS = {
    "openers": lambda n: "![" * n,
    "escaped-openers": lambda n: "\\![" * n,
    "backslash-runs": lambda n: "![\\" * n,
    "doubled-brackets": lambda n: "![[" * n,
    "images": lambda n: "![a](/tmp/x.png) " * n,
    "image-lines": lambda n: "![a](/tmp/x.png)\n" * n,
    "unclosed-destinations": lambda n: "![a](" * n,
    "code-span-openers": lambda n: "`![a]`" * n,
    "nested-parens": lambda n: "![a](/tmp/(" * n,
    "nested-images": lambda n: "![a](" * n + ")" * n,
    "nested-images-with-titles": lambda n: "![a](x " * n + ")" * n,
}

_SCANS = {
    "iter_local_refs": lambda t: of.iter_local_refs(t),
    "open_ref_start": lambda t: of.open_ref_start(t),
    "hide_local_refs": lambda t: of.hide_local_refs(t),
    "register_images-scan": lambda t: list(of.iter_image_destinations(t)),
}


@pytest.mark.parametrize("scan", sorted(_SCANS))
@pytest.mark.parametrize("pump", sorted(_PUMPS))
def test_scan_work_is_linear(monkeypatch: pytest.MonkeyPatch, pump: str, scan: str) -> None:
    """Doubling the input at most doubles the characters the scan reads."""
    work = _Work(monkeypatch)
    readings = []
    for n in (500, 1000, 2000):
        work.chars = 0
        _SCANS[scan](work.text(_PUMPS[pump](n)))
        readings.append(work.chars)
    for smaller, larger in itertools.pairwise(readings):
        # 2x for the doubled input, plus slack for a constant number of passes.
        assert larger <= 2 * smaller + 64, readings


def test_deep_destination_nesting_is_capped() -> None:
    """Nesting past the cap leaves a destination unclosed, as cmark does at the same depth."""
    depth = of._MAX_DESTINATION_PAREN_DEPTH
    within = "/tmp/" + "(" * (depth - 1) + ")" * (depth - 1) + ".png)"
    past = "/tmp/" + "(" * depth + ")" * depth + ".png)"
    assert of.md_destination(within) == within[:-1]
    assert of.md_destination(past) is None
    for rest, expected in ((within, within[:-1]), (past, None)):
        text = "![a](" + rest
        assert [d for _m, d, _c in of.iter_image_destinations(text)] == [expected]


class _CountingCuts(list[tuple[int, int]]):
    """Cut spans that count every element read."""

    reads = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        for cut in super().__iter__():
            self.reads += 1
            yield cut

    def __getitem__(self, index):  # type: ignore[no-untyped-def]
        self.reads += 1
        return super().__getitem__(index)


def test_cuts_are_read_once_not_once_per_line() -> None:
    lines = 2000
    text = "![a](/tmp/x.png)\n" * lines
    cuts = _CountingCuts((i * 17, i * 17 + 16) for i in range(lines))
    assert of._apply_cuts(text, cuts) == ""
    assert cuts.reads <= 2 * lines, cuts.reads


def test_register_images_uses_the_indexed_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The artifact scan reads the same matches through the linear iterator."""
    seen: list[str] = []
    real = of.iter_image_destinations

    def spy(text):  # type: ignore[no-untyped-def]
        seen.append(text)
        return real(text)

    monkeypatch.setattr(image_artifacts, "iter_image_destinations", spy)
    monkeypatch.setattr(image_artifacts, "get_default_store", lambda: None)
    image_artifacts.register_images("![a](https://h/p.png)", "1.0", "s")
    assert seen == ["![a](https://h/p.png)"]
