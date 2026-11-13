"""One relevance matcher for the session-start findings, task facts and activity blocks.

Each of those blocks decides today whether a stored row is "about" the first
message with its own test, and the findings test counts any shared word, a bare
``a`` included. The shared rule here:

* a word counts when it passes ``text_scoring._is_selective_keyword``: 3 or
  more characters, or a two-character word in a dense script (Han, kana,
  Hangul); a lone CJK character and a short Latin or digit token do not;
* a row is admitted when it shares at least one such token with the request;
* a task fact, on either store, is admitted only with the evidence the private
  store already asks for (``memory_v2.relevance_evidence``: lexical coverage, or
  a cosine above its 0.62 / 0.57 floors). With no comparable vector that is the
  lexical part alone.

The rule block keeps no relevance floor: a standing rule applies whatever the
topic. Preferences and episodes keep their own gates.

``memory.relevance_rule`` picks the mode:

* ``off`` (default) -- every block is today's, byte for byte.
* ``shadow`` -- every block is today's; the injection receipt records the ids the
  new rule would add to a block and drop from it.
* ``enforce`` -- the blocks use the new rule.

Selection only: no stored row is written or changed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Final

from kiro_crew import memory_v2
from kiro_crew.config.memory_sections import relevance_rule_mode_from
from kiro_crew.vector_memory_runtime.text_scoring import _is_selective_keyword

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

#: Bump when the token rule, the fact evidence or the section split changes, so
#: receipts taken under the old policy can be told from the new.
POLICY_VERSION: Final = 1

#: Receipt parts, one per block the rule gates.
PART_FINDINGS: Final = "findings"
PART_FACTS: Final = "facts"
PART_ACTIVITY: Final = "activity"


def configured_mode() -> str:
    """``memory.relevance_rule`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "relevance_rule", None)
    except Exception:
        logger.debug("Could not read memory.relevance_rule; using the default", exc_info=True)
        raw = None
    return relevance_rule_mode_from(raw)


def _words(text: str) -> set[str]:
    """The word set the vector store's findings test reads (``_lesson_words``), lowercased.

    Reused, not copied, so findings and activity sections share one tokenizer.
    """
    from kiro_crew.vector_memory_runtime.lessons import _lesson_words  # it imports this module

    return _lesson_words(text.lower())


def request_tokens(text: str) -> frozenset[str]:
    """The selective words of a request (``_is_selective_keyword``): 3+ characters, or a dense-script pair."""
    return frozenset(w for w in _words(text) if _is_selective_keyword(w))


def shares_token(tokens: frozenset[str], text: str) -> bool:
    """Whether stored *text* carries at least one of the request's *tokens*."""
    return bool(tokens) and not tokens.isdisjoint(_words(text))


def fact_terms(query_text: str) -> frozenset[str]:
    """The request terms a task fact's evidence is measured against.

    The private store's own terms (``memory_v2.terms``: NFKC, stop words out,
    CJK and Hangul pairs) that pass ``_is_selective_keyword``, so ``hi`` or a
    lone digit is never evidence.
    """
    return frozenset(t for t in memory_v2.terms(query_text) if _is_selective_keyword(t))


def fact_text(label: str, value_json: str) -> str:
    """A fact as the private store's evidence gate reads it: its label words, then its value."""
    return f"{label.replace('.', ' ').replace('_', ' ')} {memory_v2.visible_json(value_json)}"


def fact_admitted(query_terms: Iterable[str], text: str, cosine: float | None) -> bool:
    """Whether a task fact has the private store's relevance evidence for the request."""
    return bool(memory_v2.relevance_evidence(query_terms, text, cosine)["admitted"])


def _heading_level(line: str) -> int:
    """The markdown heading level of *line*: 1 to 6 ``#`` then a space or tab, else 0."""
    n = len(line) - len(line.lstrip("#"))
    return n if 1 <= n <= 6 and line[n : n + 1] in (" ", "\t") else 0


def _sections(text: str) -> list[tuple[int, str]]:
    """*text* cut before each markdown heading, as ``(level, section)`` pairs.

    The pieces join back to *text* exactly. Text before the first heading is
    level 0, the parent of everything after it. Lines are split on ``\\n`` only.
    """
    starts: list[tuple[int, int]] = []
    offset = 0
    for line in text.split("\n"):
        level = _heading_level(line)
        if level:
            starts.append((offset, level))
        offset += len(line) + 1
    if not starts or starts[0][0] != 0:
        starts.insert(0, (0, 0))
    pieces: list[tuple[int, str]] = []
    for i, (start, level) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        pieces.append((level, text[start:end]))
    return pieces


def _section_id(name: str, piece: str) -> str:
    digest = hashlib.sha256(piece.strip().encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"{name}:{digest}"


def _visible(pieces: list[str], ids: list[str], limit: int) -> set[str]:
    """The ids of *pieces* that start inside the first *limit* characters of their join."""
    seen: set[str] = set()
    offset = 0
    for piece, sid in zip(pieces, ids):
        if offset >= limit:
            break
        seen.add(sid)
        offset += len(piece)
    return seen


def gate_document(
    name: str, text: str, tokens: frozenset[str], limit: int
) -> tuple[str, list[str], list[str]]:
    """Gate one activity document by section; ``(kept text, would add, would drop)``.

    A section (a heading and its body) is kept when it shares a token with the
    request, or when a section under it is kept, so a kept entry keeps its day
    or project heading. The add and drop ids compare what each text shows
    within *limit* characters, the cut the block applies next. Ids are
    ``<name>:<hash of the section>``, never its text.
    """
    pieces = _sections(text)
    keep = [shares_token(tokens, piece) for _, piece in pieces]
    for i in range(len(pieces) - 1, -1, -1):
        level = pieces[i][0]
        for j in range(i + 1, len(pieces)):
            if pieces[j][0] <= level:
                break
            if keep[j]:
                keep[i] = True
                break
    texts = [piece for _, piece in pieces]
    ids = [_section_id(name, piece) for piece in texts]
    kept = [(piece, sid) for piece, sid, k in zip(texts, ids, keep) if k]
    kept_text = "".join(piece for piece, _ in kept)
    before = _visible(texts, ids, limit)
    after = _visible([p for p, _ in kept], [s for _, s in kept], limit)
    return kept_text, sorted(after - before), sorted(before - after)
