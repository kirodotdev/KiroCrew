"""Short names for the memory graph's topic clusters, made at layout build time.

Each cluster is named from the terms its memories share: their tags, the
namespace parts of their fact keys, and the words and two-word phrases of their
text. Terms are scored with class-based TF-IDF (c-TF-IDF, as BERTopic does: how
often a term is carried inside the cluster, against how often across all of
them), phrases and tags are preferred, and terms that look like a handle, an id
or a code are pushed down. When even the best term is weak, the cluster takes
the short title of its most typical memory instead. No model call.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from pathlib import Path
from typing import AbstractSet, Callable, Iterable, Mapping, Sequence

#: A term must be carried by at least this share of a cluster's rows (and by
#: two rows) to name it: a term one memory carries names that memory.
MIN_SHARE = 0.05
#: Under this c-TF-IDF score the best term is weak, and the cluster is named by
#: its most typical memory's title instead.
WEAK_SCORE = 0.04
#: How much a tag or key namespace (curated by whoever wrote the memory), or a
#: two-word phrase, is preferred over a single word of free text.
TAG_WEIGHT = 3.0
PHRASE_WEIGHT = 1.0
#: A phrase from free text must be carried by this share of a cluster's rows:
#: a tag is chosen by a writer, a phrase only counts when the topic repeats it.
PHRASE_SHARE = 0.15
#: How much a term that looks like a handle, id or code, or names a bot or
#: person, is pushed down. Down, not out: a strong one can still win.
HANDLE_WEIGHT = 0.1
#: Words in a title taken from a memory.
TITLE_WORDS = 3

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'_-]*[A-Za-z0-9]|[A-Za-z]")
_MENTION = re.compile(r"(?<![\w.])@([A-Za-z][\w.-]{1,30})")
_DIGIT = re.compile(r"\d")
_VOWEL = re.compile(r"[aeiouy]", re.IGNORECASE)
_CLAUSE = re.compile(r"(?<=[.;:!?])\s+|\s[\u2014\u2013-]\s|,\s")
_LEADING_STAMP = re.compile(
    r"^(?:\d[\d\-/:.,T ]*(?:\s?(?:[AP]M|UTC|GMT|Z|[A-Z][DS]T))?\s*[:\u2014\u2013-]?\s+)+"
)

#: Words too common in memories to name a topic.
STOPWORDS = frozenset("""
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can cannot could did do does doing done down during each few for from
    further get gets got had has have having he her here hers him his how i if in into is it its itself
    just like made make makes many may me might more most much must my no nor not now of off on once
    one only or other our out over own same she should so some such than that the their them then there
    these they this those through to too under until up upon us use used uses using very via was we
    were what when where which while who whom why will with would yet you your
    new now still already never always every without within across instead per etc eg ie
    user users memory memories note notes thing things way ways time times work works worked working
    run runs ran running make making first last next one two three see seen show shows shown
    """.split())


#: Words a name must not end (or start) with: function words, adverbs and verb
#: fragments that read as a cut sentence ("Check Runs Per", "... Rather").
DANGLING = frozenset("""
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can cannot could did do does doing during each few for from further
    had has have having he her here hers him his how i if in into is it its itself just me might
    more most much must my no nor not now of off on once only or other our out over own same she
    should so some such than that the their them then there these they this those through to too
    under until up upon us very via was we were what when where which while who whom why will with
    would yet you your per rather versus toward towards onto unless whether either neither though
    although else maybe perhaps really quite even ever thus hence therefore however otherwise anyway
    besides instead already still never always every without within across etc
    chose closed kept stayed added removed said asked got went came put let ran found tried
    reverted reopened merged pushed failed passed fixed changed reported confirmed noted wrote
    don doesn didn isn wasn aren won can couldn shouldn wouldn haven hasn
    """.split())
#: Two terms join with "&" only when at least this share of the rows that carry
#: the rarer one carry both: two subjects of the same memories, not of two halves.
CO_SHARE = 0.5


def clean_term(term: str) -> str | None:
    """*term* without leading or trailing :data:`DANGLING` words (and, in a phrase,
    without a trailing past-tense verb); None when nothing that names a subject
    is left."""
    words = [w for w in re.split(r"[\s_]+", term) if w]
    while words and words[0].lower() in DANGLING:
        words = words[1:]
    while words and (
        words[-1].lower() in DANGLING or (len(words) > 1 and words[-1].lower().endswith("ed"))
    ):
        words = words[:-1]
    return " ".join(words) if words else None


def known_names(db_path: Path) -> set[str]:
    """Names of the bots that keep stores beside *db_path* (`memory_stores/
    member-<name>-<id>`), lower case: whole names (``kirocrew-captain``) and their
    last word (``captain``), which in a topic name read as a person, not a subject."""
    db_path = Path(db_path)
    roots = [db_path.parent / "memory_stores"]
    if db_path.parent.name.startswith("member-"):
        roots.append(db_path.parent.parent)
    out: set[str] = set()
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            m = re.match(r"^member-(.+)-[0-9a-f]{32}$", entry.name)
            if not m:
                continue
            slug = m.group(1).lower()
            out.add(slug)
            out.add(slug.split("-")[-1])
    return out


def looks_like_handle(term: str, names: AbstractSet[str] = frozenset()) -> bool:
    """True for a term that names a person, an account or a code rather than a
    subject: no vowel (``pmm``), any digit (``cr3119``, ``v2``), a short all-caps
    code (``SDO``), or a known bot or person name."""
    for word in re.split(r"[\s_.-]+", term):
        if not word:
            continue
        low = word.lower()
        if low in names or _DIGIT.search(word) or not _VOWEL.search(word):
            return True
        if word.isupper() and len(word) <= 4:
            return True
    return term.lower() in names


#: A word of free text: letters only, so ``check-runs``, ``per_page`` and
#: ``reviewer's`` split into their words and a possessive's "s" falls away.
_TEXT_WORD = re.compile(r"[A-Za-z]+")


def text_terms(text: str) -> set[str]:
    """The two-word phrases of *text*, lower case: two words of 3+ letters, not
    stop words, side by side. Single words of free text are left to the tags,
    which name a subject far more often than a stray word does."""
    words = [w.lower() for w in _TEXT_WORD.findall(text or "")]
    keep = [w if len(w) >= 3 and w not in STOPWORDS and w not in DANGLING else None for w in words]
    return {f"{a} {b}" for a, b in zip(keep, keep[1:]) if a and b and a != b and "'" not in a + b}


_SAYS = re.compile(
    r"\b([A-Z][a-z]{2,})(?:'s)?\s+(?:said|says|asked|asks|wants|wanted|decided|chose|closed|approved|"
    r"rejected|merged|noted|wrote|reported|confirmed|prefers|preferred|told|replied)\b"
)


def mentions(texts: Iterable[str]) -> set[str]:
    """People named in *texts*, lower case: ``@handles``, and a capitalised word
    right before a reporting verb ("Ray decided", "Bolin said")."""
    out: set[str] = set()
    for text in texts:
        out |= {m.lower().rstrip(".") for m in _MENTION.findall(text or "")}
        out |= {m.lower() for m in _SAYS.findall(text or "")}
    return out


def memory_title(text: str, names: AbstractSet[str] = frozenset()) -> str:
    """A short title from a memory's text: the first :data:`TITLE_WORDS` words
    of its first clause that carry meaning (no stop words, handles, ids or
    names), a leading date or time skipped."""
    flat = re.sub(r"\s+", " ", text or "").strip()
    body = _LEADING_STAMP.sub("", flat) or flat
    clause = next(
        (p.strip() for p in _CLAUSE.split(body) if len(re.findall(r"[A-Za-z]", p)) >= 3), body
    )
    words = [
        w
        for w in _TEXT_WORD.findall(clause)
        if w.lower() not in STOPWORDS
        and w.lower() not in DANGLING
        and len(w) >= 3
        and not looks_like_handle(w, names)
    ]
    title = clean_term(" ".join(words[:TITLE_WORDS])) or ""
    # One past-tense verb ("Opened") names an act, not a subject.
    return "" if " " not in title and title.lower().endswith("ed") else title


def cluster_terms(
    members: Mapping[int, Sequence[str]],
    tokens: Mapping[str, Sequence[str]],
    texts: Mapping[str, str],
    names: AbstractSet[str],
    usable_token: Callable[[str], bool],
) -> tuple[dict[int, list[tuple[str, float]]], dict[str, dict[str, float]]]:
    """Each cluster's terms best first, with their scores: c-TF-IDF over the
    rows' tags/namespaces (where *usable_token* allows them) and text terms,
    each trimmed of dangling words (:func:`clean_term`). Also each row's terms,
    for :func:`name_terms` to check which terms share memories."""
    row_terms: dict[str, dict[str, float]] = {}
    for rows in members.values():
        for rid in rows:
            terms: dict[str, float] = {}
            for t in tokens.get(rid, ()):
                if usable_token(t):
                    clean = clean_term(
                        t.replace("_", " ").replace("-", " ").replace(".", " ").strip()
                    )
                    if clean:
                        terms[clean] = TAG_WEIGHT
            for t in text_terms(texts.get(rid, "")):
                clean = clean_term(t)
                if clean and " " in clean:
                    terms.setdefault(clean, PHRASE_WEIGHT)
            row_terms[rid] = terms
    df_all: dict[str, int] = defaultdict(int)
    for terms in row_terms.values():
        for t in terms:
            df_all[t] += 1
    avg = sum(len(r) for r in members.values()) / max(1, len(members))
    out: dict[int, list[tuple[str, float]]] = {}
    for cid, rows in members.items():
        if not rows:
            continue
        df: dict[str, int] = defaultdict(int)
        weight: dict[str, float] = {}
        for rid in rows:
            for t, w in row_terms.get(rid, {}).items():
                df[t] += 1
                weight[t] = max(weight.get(t, 0.0), w)
        floor = max(2, math.ceil(MIN_SHARE * len(rows))) if len(rows) > 2 else 1
        scored = []
        for t, n in df.items():
            if n < floor or (weight[t] == PHRASE_WEIGHT and n < PHRASE_SHARE * len(rows)):
                continue
            # An id (a hash, a ticket or PR number) never names a topic.
            if _DIGIT.search(t):
                continue
            score = (n / len(rows)) * math.log(1 + avg / df_all[t]) * weight[t]
            if looks_like_handle(t, names):
                score *= HANDLE_WEIGHT
            scored.append((t, score))
        scored.sort(key=lambda ts: (-ts[1], ts[0]))
        out[cid] = scored
    return out, row_terms


def name_terms(
    scored: Sequence[tuple[str, float]],
    rows: Sequence[str],
    row_terms: Mapping[str, Mapping[str, float]],
) -> list[str]:
    """The terms a topic's name is made of: the best one, and the next best that
    shares no word with it only when the two are carried by the same memories
    (:data:`CO_SHARE` of the rows that carry the rarer one)."""
    picked = pick_terms(scored)
    if len(picked) < 2:
        return picked
    a, b = picked
    with_a = sum(1 for r in rows if a in row_terms.get(r, {}))
    with_b = sum(1 for r in rows if b in row_terms.get(r, {}))
    both = sum(1 for r in rows if a in row_terms.get(r, {}) and b in row_terms.get(r, {}))
    return picked if both >= CO_SHARE * max(1, min(with_a, with_b)) else [a]


def pick_terms(scored: Sequence[tuple[str, float]], count: int = 2) -> list[str]:
    """The best *count* terms that share no word: a phrase and a word inside it
    (``pr review``, ``review``) are one."""
    picked: list[str] = []
    seen: set[str] = set()
    for t, _ in scored:
        words = {w.rstrip("s") for w in t.split()}
        if words & seen:
            continue
        seen |= words
        picked.append(t)
        if len(picked) == count:
            break
    return picked


def is_weak(scored: Sequence[tuple[str, float]]) -> bool:
    """True when even the best term scores under :data:`WEAK_SCORE`."""
    return not scored or scored[0][1] < WEAK_SCORE
