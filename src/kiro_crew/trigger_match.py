"""Trigger-phrase matching: the one definition of "does this text match".

Scores comma-separated trigger phrases against a user's message for
``SkillsLoader.get_triggered_skills`` (and the skill-selection decision point
that explains its choices). The scoring lives here, in a leaf module, so that
every consumer agrees: a second implementation would not fail loudly. It would
agree on the easy cases and diverge on the phrasings that matter (a two-word
trigger inside a longer sentence, a ``!`` negative that only sometimes wins).

Grammar, from the skills loader that established it:

* Phrases are comma-separated; surrounding whitespace is insignificant.
* A phrase's score is the fraction of ITS words present in the text, so a
  specific multi-word phrase must be matched more completely than a generic
  one-word phrase to reach the same score. An entry's score is its best phrase.
* Matching ignores case. A positive phrase prefixed with ``=`` is the one
  exception: its words must appear exactly as written, so ``=Word`` fires on
  "open it in Word" but not on "reply with the word pong". It is for a name
  that is also an ordinary word; the author marks it, so no other trigger's
  matching changes.
* A phrase prefixed with ``!`` is a NEGATIVE: if all of its words appear in the
  text, the entry is excluded no matter what its positive phrases scored.
  Negatives are evaluated after every positive so phrase ORDER cannot change the
  outcome.
* An entry scoring below :data:`MIN_TRIGGER_OVERLAP` does not match at all.

LEAF module: stdlib only, so every consumer can import it without a cycle.
"""

from __future__ import annotations

import re

#: Fraction of a trigger phrase's words that must appear in the text. Shared with
#: the skills loader, which owns the value's history: high enough that ordinary
#: prose does not fire a trigger, low enough that one inflected word does not
#: suppress a genuine match.
MIN_TRIGGER_OVERLAP = 0.7

#: Compiled once. Consumers call this per trigger PHRASE inside a per-message
#: loop over every visible skill, so the pattern cache lookup an uncompiled
#: ``re.findall`` pays is measurable there rather than theoretical.
_WORD_RE = re.compile(r"\w+")


#: Marks a case-exact phrase in a trigger list, and its words in :func:`words_of`'s
#: set. It never occurs inside a ``\w+`` word, so the two forms cannot collide.
_EXACT = "="


def words_of(text: str) -> set[str]:
    """The word set a trigger list is scored against.

    Every word lowercased, plus every word as written behind :data:`_EXACT`,
    which is the form an ``=`` phrase looks up in :func:`trigger_score`.
    """
    exact = {_EXACT + word for word in _WORD_RE.findall(text)}
    return set(_WORD_RE.findall(text.lower())) | exact


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
        written = raw.strip()
        phrase = written.lower()
        if not phrase:
            continue
        if phrase.startswith("!"):
            neg = set(_WORD_RE.findall(phrase[1:]))
            if neg and neg <= text_words:
                negated = True
            continue
        if written.startswith(_EXACT):
            phrase_words = {_EXACT + word for word in _WORD_RE.findall(written[1:])}
        else:
            phrase_words = set(_WORD_RE.findall(phrase))
        if not phrase_words:
            continue
        best = max(best, len(phrase_words & text_words) / len(phrase_words))
    return best, negated
