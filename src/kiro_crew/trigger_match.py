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

Grammar, from the skills loader that established it:

* Phrases are comma-separated; surrounding whitespace is insignificant.
* A phrase's score is the fraction of ITS words present in the text, so a
  specific multi-word phrase must be matched more completely than a generic
  one-word phrase to reach the same score. An entry's score is its best phrase.
* Chinese, Japanese and Thai are written without spaces, so a run of those
  characters is one word that may hold many. A phrase word in one of them is
  present when it occurs inside such a run of the text; a run also ends where
  another script's letters start, so ``review`` is a word of "帮我review一下".
  A trigger word that joins scripts, like ``Q3报告``, still counts once, and is
  present when each of its runs is.
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

#: Scripts written without spaces between words. Their punctuation and symbols
#: (、。「」, ・, ゛, ฿) are left out, so they still separate words.
_UNSPACED = (
    "\u0e00-\u0e3e\u0e40-\u0e4e\u0e50-\u0e59"  # Thai
    "\u3005-\u3007"  # the Japanese iteration marks 々 〆 〇
    "\u3040-\u309a\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff"  # hiragana, katakana
    "\u31f0-\u31ff\uff66-\uff9f"  # more kana
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003ffff"  # CJK ideographs
)
_UNSPACED_RE = re.compile(f"[{_UNSPACED}]")

#: Compiled once. Both consumers call this per trigger PHRASE inside a
#: per-message loop over every visible skill, so the pattern cache lookup an
#: uncompiled ``re.findall`` pays is measurable there rather than theoretical.
#: A word is a run of unspaced-script characters or a run of other word
#: characters; outside those scripts this is exactly ``\w+``.
_WORD_RE = re.compile(f"[{_UNSPACED}]+|[^\\W{_UNSPACED}]+")

#: A phrase word may span scripts; it counts once, present when each :data:`_WORD_RE` run is.
_PHRASE_WORD_RE = re.compile(f"[\\w{_UNSPACED}]+")


#: Marks a case-exact phrase in a trigger list, and its words in :func:`words_of`'s
#: set. It never occurs inside a :data:`_WORD_RE` word, so the two forms cannot collide.
_EXACT = "="


def words_of(text: str) -> set[str]:
    """The word set a trigger list is scored against.

    Every word lowercased, plus every word as written behind :data:`_EXACT`,
    which is the form an ``=`` phrase looks up in :func:`trigger_score`.
    """
    exact = {_EXACT + word for word in _WORD_RE.findall(text)}
    return set(_WORD_RE.findall(text.lower())) | exact


def _present(word: str, text_words: set[str]) -> bool:
    """Whether one run of a phrase word (either form) is in the text's word set.

    An unspaced-script word may sit inside a longer run of the text. Those
    scripts have no case, so the ``=`` form needs no separate lookup there.
    """
    if word in text_words:
        return True
    bare = word.removeprefix(_EXACT)
    return bool(_UNSPACED_RE.match(bare)) and any(bare in run for run in text_words)


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
            if neg and all(_present(word, text_words) for word in neg):
                negated = True
            continue
        if written.startswith(_EXACT):
            phrase_words = {
                tuple(_EXACT + run for run in _WORD_RE.findall(word))
                for word in _PHRASE_WORD_RE.findall(written[1:])
            }
        else:
            phrase_words = {
                tuple(_WORD_RE.findall(word)) for word in _PHRASE_WORD_RE.findall(phrase)
            }
        if not phrase_words:
            continue
        present = sum(all(_present(run, text_words) for run in runs) for runs in phrase_words)
        best = max(best, present / len(phrase_words))
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
