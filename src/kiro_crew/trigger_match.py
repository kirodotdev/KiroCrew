"""Trigger-phrase matching: the one definition of "does this text match".

Two consumers score comma-separated trigger phrases against a user's message —
``SkillsLoader.get_triggered_skills`` picks skills, and :func:`rank_triggered`
picks the crew a session should route to. They MUST agree, so the scoring lives
here rather than beside either one.

A second implementation would not fail loudly. It would agree on the easy cases
and diverge on the phrasings that matter (a two-word trigger inside a longer
sentence, a ``!`` negative that only sometimes wins), and the symptom would be a
task quietly handled by the wrong crew — the memory-leak case a per-crew silo
exists to prevent, arriving through the router instead of through the store.

Grammar, unchanged from the skills loader that established it:

* Phrases are comma-separated; surrounding whitespace is insignificant.
* A phrase's score is the fraction of ITS words present in the text, so a
  specific multi-word phrase must be matched more completely than a generic
  one-word phrase to reach the same score. An entry's score is its best phrase.
* A phrase prefixed with ``!`` is a NEGATIVE: if all of its words appear in the
  text, the entry is excluded no matter what its positive phrases scored.
  Negatives are evaluated after every positive so phrase ORDER cannot change the
  outcome.
* An entry scoring below :data:`MIN_TRIGGER_OVERLAP` does not match at all.

LEAF module: stdlib only, so both consumers can import it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

#: Fraction of a trigger phrase's words that must appear in the text. Shared with
#: the skills loader, which owns the value's history: high enough that ordinary
#: prose does not fire a trigger, low enough that one inflected word does not
#: suppress a genuine match.
MIN_TRIGGER_OVERLAP = 0.7

#: Compiled once. Both consumers call this per trigger PHRASE inside a
#: per-message loop over every visible skill, so the pattern cache lookup an
#: uncompiled ``re.findall`` pays is measurable there rather than theoretical.
_WORD_RE = re.compile(r"\w+")


def words_of(text: str) -> set[str]:
    """The lowercase word set used on both sides of a comparison."""
    return set(_WORD_RE.findall(text.lower()))


def trigger_score(triggers: str, text_words: set[str]) -> tuple[float, bool]:
    """``(best positive score, negated)`` for one entry's trigger list.

    *negated* is reported separately from the score rather than folded into it,
    because a caller that audits refusals needs to tell "scored nothing" apart
    from "scored well and was vetoed" — those are a non-match and a DENY, and
    only the second is worth a log line.
    """
    best = 0.0
    negated = False
    for raw in (triggers or "").split(","):
        phrase = raw.strip().lower()
        if not phrase:
            continue
        if phrase.startswith("!"):
            neg = set(_WORD_RE.findall(phrase[1:]))
            if neg and neg <= text_words:
                negated = True
            continue
        phrase_words = set(_WORD_RE.findall(phrase))
        if not phrase_words:
            continue
        best = max(best, len(phrase_words & text_words) / len(phrase_words))
    return best, negated


def rank_triggered(
    text: str, entries: Iterable[tuple[str, str]], *, limit: int = 0
) -> list[tuple[str, float]]:
    """Rank ``(name, triggers)`` pairs against *text*, best first.

    Returns only entries at or above :data:`MIN_TRIGGER_OVERLAP` that no negative
    vetoed. Ties keep the input order, which for a crew roster is the operator's
    own ordering in ``config.json`` — a stable answer beats an arbitrary one when
    two crews describe the same work, and the operator's order is the only
    ranking signal present that they actually chose.

    ``limit=0`` means no cap.
    """
    text_words = words_of(text)
    scored: list[tuple[str, float]] = []
    for name, triggers in entries:
        if not (triggers or "").strip():
            continue
        best, negated = trigger_score(triggers, text_words)
        if negated or best < MIN_TRIGGER_OVERLAP:
            continue
        scored.append((name, best))
    # `sorted` is stable, so equal scores preserve the roster order above.
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit] if limit > 0 else scored
