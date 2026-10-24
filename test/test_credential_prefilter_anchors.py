"""The literal-led pre-filter anchors accept exactly what their plain spellings do.

`_might_contain_credential` spells two anchors so that each starts with a
literal `re` can search at memchr speed (see the comment above them in
`redaction_patterns.py`). That rewrite is only safe if every rewritten anchor accepts
EXACTLY the strings its plain spelling accepts: accepting less would let a
credential skip redaction, accepting more would only cost speed but would make
the gate's verdict drift from the one it replaced. So each anchor is compared
with its plain spelling over a corpus built around its edges (run lengths one
below, at and one above every bound) plus seeded random strings over the
characters the anchors care about.

The controls prove the corpus can tell: a mutant that is wrong by one (one digit
short, one length dropped) must disagree with the plain spelling on at least
one string.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable

import pytest

from kiro_crew.security import redaction

_CLS = "[A-Za-z0-9_-]"
_PLAIN = {
    "telegram": re.compile(r"[0-9]{5,}:[A-Za-z0-9_-]{30}"),
    "discord": re.compile(r"[MNO][A-Za-z0-9_-]{22,30}\."),
}


def _fast(name: str) -> Callable[[str], bool]:
    if name == "telegram":
        return lambda s: redaction._CREDENTIAL_PREFILTER_TELEGRAM_RE.search(s) is not None
    return lambda s: redaction._CREDENTIAL_PREFILTER_DISCORD_RE.search(s) is not None


def _search(rx: re.Pattern[str]) -> Callable[[str], bool]:
    return lambda s: rx.search(s) is not None


# Each mutant is wrong by exactly one step at one bound.
_MUTANTS: dict[str, Callable[[str], bool]] = {
    "telegram": _search(re.compile(r":(?<=[0-9]{4}:)[A-Za-z0-9_-]{30}")),
    "discord": _search(
        re.compile(
            r"\.(?<="
            + _CLS
            + r"{23}\.)(?:"
            + "|".join(rf"(?<=[MNO]{_CLS}{{{n}}}\.)" for n in range(22, 30))
            + ")"
        )
    ),
}


def _edge_corpus() -> list[str]:
    rng = random.Random(19628)
    out: list[str] = []
    seps = ["", " ", "x", ".", ":", "/", "\u4e2d", "\u3000", "M"]
    # Telegram: digit runs around 5, tails around 30, broken tails.
    for digits in range(4, 9):
        for tail in range(28, 33):
            for pre in seps:
                body = "1" * digits + ":" + "a" * tail
                out.append(pre + body)
                out.append(pre + body[:-1] + "!" + body[-1:])
                out.append(pre + "1" * digits + "x:" + "a" * tail)
    # Discord: [MNO] then runs around 22..30, then a dot, with and without a
    # longer class run in front.
    for head in ("M", "N", "O", "L", "m"):
        for run in range(20, 33):
            for pre in seps + ["a" * 5, "-" * 12]:
                for post in ("", "x", "."):
                    out.append(pre + head + "b" * run + "." + post)
                    out.append(pre + head + "b" * run + "!" + ".")
    # Seeded random strings over the characters the two anchors care about.
    alphabet = "0123456789:MNOLbZz._- \u4e2d"
    for _ in range(200_000):
        out.append("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 48))))
    return out


_CORPUS = _edge_corpus()


@pytest.mark.parametrize("name", sorted(_PLAIN))
def test_rewritten_anchors_agree_with_their_plain_spelling(name: str) -> None:
    fast, plain = _fast(name), _search(_PLAIN[name])
    hits = 0
    for text in _CORPUS:
        expected = plain(text)
        hits += expected
        assert fast(text) == expected, f"{name} anchor disagrees on {text!r}"
    assert hits >= 100, f"only {hits} corpus strings match {name}; the corpus proves little"


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_the_corpus_catches_an_anchor_wrong_by_one(name: str) -> None:
    mutant, plain = _MUTANTS[name], _search(_PLAIN[name])
    assert any(mutant(t) != plain(t) for t in _CORPUS), f"corpus cannot tell a wrong {name} anchor"
