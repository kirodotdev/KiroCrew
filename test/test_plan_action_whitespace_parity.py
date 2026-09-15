"""The two guards must normalise whitespace identically, measured rather than declared.

The plan-action guard runs on both surfaces, and each surface trimmed with its own language's
notion of whitespace. Those notions are NOT the same: Python's ``str.strip()`` removes
U+001C-U+001F and U+0085, which JS ``trim()`` leaves; JS removes U+FEFF, which Python leaves.
So ``(recommended) \\u0085Go`` passed the JS guard, had its marker stripped, and then matched
the plan-action dispatcher on the Python side -- an unattended auto-run from a label the
guard was supposed to refuse.

This does not pin a LIST, which is the weakness of the mirrored deny-lists: it derives both
whitespace sets by measurement and asserts they are equal, so a future divergence in either
language's normalisation fails here rather than becoming a promotion path.
"""

from __future__ import annotations

import re
from pathlib import Path

from kiro_crew.constants import _PLAN_ACTION_TRIM_RE, strip_recommended_marker

TS = (
    Path(__file__).resolve().parents[1]
    / "website"
    / "src"
    / "app-sdk"
    / "protocol"
    / "recommendation.ts"
)

# Every codepoint Python's argument-less strip() removes. Derived, never enumerated by hand.
PYTHON_STRIP_SET = frozenset(
    cp for cp in range(0x110000) if (chr(cp) + "x" + chr(cp)).strip() == "x"
)


def _ts_class_source() -> str:
    text = TS.read_text(encoding="utf-8")
    match = re.search(r"const PLAN_ACTION_TRIM_RE\s*=\s*\n?\s*/\^\[([^\]]*)\]", text)
    assert match, "PLAN_ACTION_TRIM_RE's leading character class was not found"
    return match.group(1)


def _expand(klass: str) -> set[int]:
    """Expand a JS regex character class of \\uXXXX escapes and ranges to codepoints."""
    out: set[int] = set()
    tokens = re.findall(r"\\u([0-9A-Fa-f]{4})(?:-\\u([0-9A-Fa-f]{4}))?", klass)
    assert tokens, f"no \\uXXXX escapes parsed from {klass!r}"
    consumed = sum(len(m) for m in re.findall(r"\\u[0-9A-Fa-f]{4}(?:-\\u[0-9A-Fa-f]{4})?", klass))
    assert consumed == len(klass), f"the class carries syntax this parser does not model: {klass!r}"
    for lo, hi in tokens:
        start = int(lo, 16)
        end = int(hi, 16) if hi else start
        out.update(range(start, end + 1))
    return out


class TestTheParserCanActuallyRead:
    """Guard the guard: a silent parse failure would make every assertion below vacuous."""

    def test_the_class_is_found_and_non_trivial(self):
        assert len(_expand(_ts_class_source())) > 10

    def test_python_set_is_measured_not_empty(self):
        assert len(PYTHON_STRIP_SET) > 10
        assert 0x0085 in PYTHON_STRIP_SET
        assert 0xFEFF not in PYTHON_STRIP_SET


class TestTheTwoNormalisationsAgree:
    def test_the_ts_class_equals_the_python_guards_class(self):
        ts = _expand(_ts_class_source())
        py = {cp for cp in range(0x10000) if _PLAN_ACTION_TRIM_RE.sub("", chr(cp)) == ""}
        missing = sorted(py - ts)
        extra = sorted(ts - py)
        assert not missing, "the TS guard would not strip: " + ", ".join(
            f"U+{cp:04X}" for cp in missing
        )
        assert not extra, "the TS guard strips what the Python guard keeps: " + ", ".join(
            f"U+{cp:04X}" for cp in extra
        )

    def test_the_python_guard_covers_everything_strip_removes(self):
        """The guard may be stricter than ``strip()``, never laxer."""
        py = {cp for cp in range(0x10000) if _PLAN_ACTION_TRIM_RE.sub("", chr(cp)) == ""}
        assert not sorted(PYTHON_STRIP_SET - py), "the guard trims less than strip() does"

    def test_the_regression_codepoints_are_covered(self):
        # The five that produced the promotion path, named so a failure says why.
        ts = _expand(_ts_class_source())
        for cp in (0x001C, 0x001D, 0x001E, 0x001F, 0x0085):
            assert cp in ts, f"U+{cp:04X} evades the plan-action guard again"

    def test_the_bom_is_included_on_both_sides(self):
        """The dashboard send path trims with JS ``trim()``, which removes U+FEFF.

        Excluding it let a BOM-tailed plan label strip its marker here and then dispatch as
        an auto-run, so both guards must trim it even though Python's ``strip()`` does not.
        """
        assert 0xFEFF in _expand(_ts_class_source())
        assert _PLAN_ACTION_TRIM_RE.sub("", "Go All\ufeff") == "Go All"
        assert (
            strip_recommended_marker("(recommended) Go All\ufeff") == "(recommended) Go All\ufeff"
        )


class TestTheGuardUsesTheClass:
    def test_neither_seam_falls_back_to_plain_trim(self):
        text = TS.read_text(encoding="utf-8")
        assert ".trim()" not in text, "a plain trim() reintroduces the language divergence"
        assert text.count("PLAN_ACTION_TRIM_RE") >= 3
