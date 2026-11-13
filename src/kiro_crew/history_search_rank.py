"""How much each query word counts when session search ranks its matches.

A word few sessions hold says more about which session is meant than a word most
of them hold, so each needle is weighted by its BM25 idf over the search window,
and its content hits saturate, so how OFTEN a long transcript repeats one word
cannot outweigh holding a word few sessions hold.

The weights need the EXACT number of sessions holding each needle, and they must
come out the same whether the index answered or the scan did, or the index would
reorder results. So only needles whose presence the index answers exactly are
counted, from its postings for a session it vouches for (no read) and by counting
for a session that was scanned. Private sessions are left out, so their contents
cannot move the order of anyone else's rows.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from kiro_crew.history_search_query import SearchNeedle, count_needle

#: BM25's term-frequency saturation and length-normalization strengths: a needle's
#: content factor rises steeply for its first few hits and levels off below
#: ``k1 + 1``. ``k1`` is 5, not BM25's usual 1.2, because the recency boost
#: multiplies the score by up to 2.5: a content factor capped near 2.2 would let
#: recency decide every content-only comparison, while 5 keeps a session with
#: three or more hits a year ago ahead of one passing mention today.
_BM25_K1 = 5.0
_BM25_B = 0.75
#: Added to every length before comparing it with the mean, as the square-root
#: divisor raw counts used did: between two short sessions a few lines more or less
#: still settles a tie, but cannot outweigh the recency boost.
_LENGTH_SMOOTHING_CHARS = 1024

#: The weight of a needle whose document frequency is not known exactly: what it
#: would have in a store where half the sessions hold it.
UNKNOWN_DF_IDF = math.log(2.0)


def inverse_document_frequency(holding: int, sessions: int) -> float:
    """BM25's idf of a needle *holding* of *sessions* sessions contain; always > 0.

    The ``1 +`` keeps a needle every session holds positive, so it still ranks a
    session holding it often above one holding it once, and still counts in a
    title.
    """
    if sessions <= 0:
        return UNKNOWN_DF_IDF
    holding = min(max(holding, 0), sessions)
    return math.log(1.0 + (sessions - holding + 0.5) / (holding + 0.5))


def saturated_frequency(hits: int, doc_chars: int, mean_doc_chars: float) -> float:
    """BM25's term-frequency factor: 0 for no hits, approaching ``k1 + 1``.

    A session longer than the window's mean needs more hits for the same factor.
    """
    if hits <= 0:
        return 0.0
    relative_length = (doc_chars + _LENGTH_SMOOTHING_CHARS) / (
        max(mean_doc_chars, 0.0) + _LENGTH_SMOOTHING_CHARS
    )
    norm = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * relative_length)
    return hits * (_BM25_K1 + 1.0) / (hits + norm)


class WordWeights:
    """Each needle's idf and the mean session length over one search window.

    Feed it every visible session in the window exactly once, through
    :meth:`add_indexed` when the index vouches for the session's text and
    *postings* answered, otherwise through :meth:`add_scanned`.
    """

    __slots__ = ("_needles", "_postings", "_counted", "_holding", "_sessions", "_chars")

    def __init__(
        self,
        needles: Sequence[SearchNeedle],
        postings: dict[int, set[str]] | None,
        presence_is_exact: Callable[[SearchNeedle], bool],
    ) -> None:
        self._needles = needles
        self._postings = postings
        counted = [pos for pos, needle in enumerate(needles) if presence_is_exact(needle)]
        if postings is not None and not all(pos in postings for pos in counted):
            # A needle the postings do not cover cannot be counted for a session
            # only the postings describe, so no needle is weighted rather than
            # some weighted from an undercount.
            counted = []
        self._counted = counted
        self._holding = [0] * len(needles)
        self._sessions = 0
        self._chars = 0

    def add_indexed(self, key: str, title_folded: str, doc_chars: int) -> None:
        """A session whose content presence the postings answer."""
        postings = self._postings
        if postings is None:
            raise RuntimeError("no postings answered this search, so scan the session")
        self._sessions += 1
        self._chars += doc_chars
        for pos in self._counted:
            if key in postings[pos] or count_needle(self._needles[pos], title_folded):
                self._holding[pos] += 1

    def add_scanned(
        self, title_folded: str, doc_chars: int, content_count: Callable[[int], int]
    ) -> None:
        """A session read in full; *content_count* gives a needle's hits by position."""
        self._sessions += 1
        self._chars += doc_chars
        for pos in self._counted:
            if count_needle(self._needles[pos], title_folded) or content_count(pos):
                self._holding[pos] += 1

    @property
    def has_postings(self) -> bool:
        return self._postings is not None

    @property
    def mean_doc_chars(self) -> float:
        return self._chars / self._sessions if self._sessions else 0.0

    def idf(self) -> list[float]:
        """Per needle position: its idf when counted, else :data:`UNKNOWN_DF_IDF`."""
        weights = [UNKNOWN_DF_IDF] * len(self._needles)
        for pos in self._counted:
            weights[pos] = inverse_document_frequency(self._holding[pos], self._sessions)
        return weights
