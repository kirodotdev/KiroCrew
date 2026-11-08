"""Compact row-term index for the resident semantic scoring set.

The V1 fact ranking intersects the query's terms with every row's key and value
terms. Held as one ``frozenset`` per text, those terms cost ~2.7 KiB of set table
per text on a real store (19,056 texts, 52 MiB of tables), so the row-term memo
hit its 64 MiB budget at ~60% of the rows and every context build re-stemmed
the rest. Here each text's terms are ids into one shared vocabulary, stored back
to back in a single ``uint32`` array: the same store fits in ~11 MiB, and a query
finds every text's matched terms with one ``numpy.isin`` instead of one Python
set intersection per text.

A text's terms are exactly ``text_scoring._row_stem_tokens_uncached(text)``, so
a matched-term set read from here equals the set intersection it replaces.
"""

from __future__ import annotations

from array import array
from typing import Any

from kiro_crew.vector_memory_runtime import text_scoring as _text_scoring

#: Term ids the index may hold (4 bytes each). Past it the scoring set keeps
#: the per-text memo path, which bounds itself in bytes.
MAX_TERM_IDS = 16 * 1024 * 1024

#: Distinct terms :meth:`RowTermIndex.patched` may add to the vocabulary beyond what
#: the last full build held, or as many again as that build held if more. Terms no
#: surviving text carries stay in the shared vocabulary, so past this bound the
#: index is dropped and rebuilt from the live texts, which keeps only the terms in use.
VOCAB_GROWTH_SLACK = 65536


class RowTermIndex:
    """Terms of a fixed sequence of texts, as ids into one vocabulary."""

    __slots__ = ("_vocab", "_terms", "_flat", "_offsets", "_np", "_built_vocab")

    def __init__(self, vocab, terms, flat, offsets, np: Any, built_vocab: int | None = None):
        self._vocab = vocab
        self._terms = terms
        self._flat = flat
        self._offsets = offsets
        self._np = np
        # Vocabulary size at the last full build; patches grow it from there.
        self._built_vocab = len(terms) if built_vocab is None else built_vocab

    @classmethod
    def build(cls, texts: list[str], np: Any) -> RowTermIndex | None:
        """Index *texts* by position, or ``None`` past :data:`MAX_TERM_IDS` terms.

        The texts themselves are not kept: only their term ids are.
        """
        vocab: dict[str, int] = {}
        terms: list[str] = []
        # Raw machine arrays while building: a list would hold a million int
        # objects until the end, and the heap they grow is not handed back.
        flat = array("I")
        offsets = array("q", [0])
        stems = _text_scoring._row_stem_tokens_uncached
        for text in texts:
            for term in stems(text):
                term_id = vocab.get(term)
                if term_id is None:
                    term_id = vocab[term] = len(terms)
                    terms.append(term)
                flat.append(term_id)
            offsets.append(len(flat))
            if len(flat) > MAX_TERM_IDS:
                return None
        return cls(
            vocab,
            terms,
            np.frombuffer(flat, dtype=np.uint32),
            np.frombuffer(offsets, dtype=np.int64),
            np,
        )

    def patched(self, drop_row: int | None, new_texts: list[str]) -> RowTermIndex | None:
        """A copy without row *drop_row*'s two texts and with *new_texts* appended.

        Texts ``2r`` and ``2r + 1`` are row ``r``'s, so dropping a row removes
        that pair and shifts every later text down by two. *new_texts* are indexed
        after the surviving ones, exactly as :meth:`build` would index them at the
        end of the sequence. The vocabulary is shared with this index and only
        ever grows: an id no surviving text carries is never matched. ``None``
        past :data:`MAX_TERM_IDS` terms, as :meth:`build` answers, and ``None`` once the
        vocabulary has grown past :data:`VOCAB_GROWTH_SLACK` since the last full build.
        """
        np = self._np
        flat, offsets = self._flat, self._offsets
        if drop_row is not None:
            first = 2 * drop_row
            lo, hi = int(offsets[first]), int(offsets[first + 2])
            flat = np.concatenate([flat[:lo], flat[hi:]])
            offsets = np.concatenate([offsets[: first + 1], offsets[first + 3 :] - (hi - lo)])
        vocab, terms = self._vocab, self._terms
        tail = array("I")
        tail_offsets = array("q")
        stems = _text_scoring._row_stem_tokens_uncached
        base = len(flat)
        for text in new_texts:
            for term in stems(text):
                term_id = vocab.get(term)
                if term_id is None:
                    term_id = vocab[term] = len(terms)
                    terms.append(term)
                tail.append(term_id)
            tail_offsets.append(base + len(tail))
        if base + len(tail) > MAX_TERM_IDS:
            return None
        if len(terms) > self._built_vocab + max(VOCAB_GROWTH_SLACK, self._built_vocab):
            return None
        if new_texts:
            flat = np.concatenate([flat, np.frombuffer(tail, dtype=np.uint32)])
            offsets = np.concatenate([offsets, np.frombuffer(tail_offsets, dtype=np.int64)])
        return RowTermIndex(
            vocab,
            terms,
            flat.astype(np.uint32, copy=False),
            offsets.astype(np.int64, copy=False),
            np,
            self._built_vocab,
        )

    def query(self, query_terms: set[str]) -> QueryHits:
        """Every text's overlap with *query_terms*."""
        np = self._np
        wanted = [self._vocab[t] for t in query_terms if t in self._vocab]
        hits: dict[int, set[str]] = {}
        if wanted:
            positions = np.flatnonzero(np.isin(self._flat, np.asarray(wanted, dtype=np.uint32)))
            owners = np.searchsorted(self._offsets, positions, side="right") - 1
            terms = self._terms
            for owner, term_id in zip(owners.tolist(), self._flat[positions].tolist()):
                hits.setdefault(owner, set()).add(terms[term_id])
        return QueryHits(hits)


_NO_TERMS: frozenset[str] = frozenset()


class QueryHits:
    """One query's matched terms in each indexed text."""

    __slots__ = ("_hits",)

    def __init__(self, hits: dict[int, set[str]]):
        self._hits = hits

    def matched(self, text_index: int) -> frozenset[str] | set[str]:
        """``query_terms & terms_of(texts[text_index])``."""
        return self._hits.get(text_index, _NO_TERMS)
