"""The frontend dispatch fence is held to the code that actually dispatches.

``dispatchShape.ts`` decides whether quick-send refuses a chip. The things it must not
let through are defined elsewhere: the skill expander resolves ``$name``, and the send
path trims the label (``String.prototype.trim`` in the browser, ``str.strip`` on the
server) before anything reads its first character. Those are the ground truth, so the
fence is compared against them directly rather than against a second copy kept in step
by hand -- a copy passes its own test while the fence that does the refusing drifts.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

from kiro_crew.skills import _DOLLAR_SKILL_PATTERN

_WEB_SRC = Path(__file__).resolve().parents[1] / "website" / "src"
_DISPATCH_SHAPE = _WEB_SRC / "app-sdk" / "protocol" / "dispatchShape.ts"
_SKILL_TOKEN = _WEB_SRC / "utils" / "skillToken.ts"
_SKILL_TOKEN_DECL_RE = re.compile(
    r"export const SKILL_TOKEN_RE = /(?P<body>.+)/(?P<flags>[a-z]*)$",
    re.MULTILINE,
)
_LEADING_DECL_RE = re.compile(r"const DISPATCH_LEADING_RE =\s*/\^\[(?P<cls>[^\]]+)\]\+/")


def _skill_token_decl() -> re.Match[str]:
    match = _SKILL_TOKEN_DECL_RE.search(_SKILL_TOKEN.read_text(encoding="utf-8"))
    assert match, "frontend skill-token pattern not found -- was it renamed or reshaped?"
    return match


def _frontend_skill_token_re() -> re.Pattern[str]:
    match = _skill_token_decl()
    flags = re.IGNORECASE if "i" in match.group("flags") else 0
    return re.compile(match.group("body"), flags)


def _frontend_leading_re() -> re.Pattern[str]:
    match = _LEADING_DECL_RE.search(_DISPATCH_SHAPE.read_text(encoding="utf-8"))
    assert match, "DISPATCH_LEADING_RE not found -- was it renamed or reshaped?"
    return re.compile("[" + match.group("cls") + "]")


class TestTheFenceTracksTheExpander:
    ACCEPTED = (
        "$deploy",
        "Run the $deploy skill",
        "trailing token $a",
        "$a/b_c-d",
    )
    REJECTED = ("$$deploy", "mid$word", "plain prose", "$", "$Deploy")

    def test_the_frontend_declares_a_pattern_this_test_can_read(self):
        assert _frontend_skill_token_re().search("$deploy"), "extracted pattern matches nothing"

    def test_the_fence_accepts_what_the_expander_accepts(self):
        fence = _frontend_skill_token_re()
        for text in self.ACCEPTED:
            assert _DOLLAR_SKILL_PATTERN.search(text), "expander changed: %r" % text
            assert fence.search(text), "fence narrower than expander: %r" % text

    def test_neither_side_matches_a_non_token(self):
        fence = _frontend_skill_token_re()
        for text in self.REJECTED:
            assert not _DOLLAR_SKILL_PATTERN.search(text), "expander widened: %r" % text
            assert not fence.search(text), "fence wider than expander: %r" % text

    def test_the_frontend_declares_the_pattern_once(self):
        body = _skill_token_decl().group("body")
        holders = [
            path.relative_to(_WEB_SRC).as_posix()
            for path in _WEB_SRC.rglob("*.ts*")
            if ".test." not in path.name and body in path.read_text(encoding="utf-8")
        ]
        assert holders == ["utils/skillToken.ts"], holders


class TestEveryResolvableTokenIsFenced:
    """The resolver loads any token the expander pattern matches, digits-only included."""

    def test_the_expander_matches_a_digit_only_token(self):
        assert [m.group(1) for m in _DOLLAR_SKILL_PATTERN.finditer("Approve $40 spend")] == ["40"]

    def test_the_fence_narrows_the_pattern_with_no_second_filter(self):
        source = _DISPATCH_SHAPE.read_text(encoding="utf-8")
        assert "return SKILL_TOKEN_RE.test(text)" in source


def _js_trim_set() -> set[str]:
    # ECMAScript WhiteSpace plus LineTerminator: every Zs character, and these.
    extra = "\t\n\v\f\r\ufeff\u2028\u2029"
    return {
        chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Zs"
    } | set(extra)


def _python_strip_set() -> set[str]:
    return {chr(c) for c in range(sys.maxunicode + 1) if chr(c).isspace()}


class TestTheLeadingTrimCoversEverySendPathTrim:
    """A character either trim drops, but the fence does not, would hide a leading sigil."""

    def test_the_fence_class_matches_a_known_member(self):
        assert _frontend_leading_re().match("\u00a0")

    def test_every_browser_trim_character_is_skipped(self):
        fence = _frontend_leading_re()
        missed = sorted("U+%04X" % ord(ch) for ch in _js_trim_set() if not fence.match(ch))
        assert not missed, missed

    def test_every_server_strip_character_is_skipped(self):
        fence = _frontend_leading_re()
        missed = sorted("U+%04X" % ord(ch) for ch in _python_strip_set() if not fence.match(ch))
        assert not missed, missed
