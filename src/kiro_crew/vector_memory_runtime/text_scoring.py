"""Lexical scoring shared by every vector-memory retrieval path.

Word stemming and the per-row token memo, the keyword and hybrid scores, the
Jaccard-based MMR rerank, the dense-script keyword floor used by the episodic
LIKE fallback, and the literal Unicode predicate the list endpoints filter
with. Everything here is a pure function of its arguments: no store, no lock and
no database handle. ``kiro_crew.vector_memory`` re-exports every name.
"""

from __future__ import annotations

import functools
import heapq
import json
import re
import threading
import unicodedata
from typing import Callable

from snowballstemmer import stemmer as _snowball_stemmer

MAX_MEMORY_SEARCH_QUERY = 2000


def _normalize_memory_search_query(query: str) -> str:
    if not isinstance(query, str) or len(query) > MAX_MEMORY_SEARCH_QUERY:
        raise ValueError(
            f"Memory search query must be at most {MAX_MEMORY_SEARCH_QUERY} characters"
        )
    return unicodedata.normalize("NFKC", query.strip()).casefold()


def _contains_memory_search_text(value: str | None, query: str, json_encoded: int) -> int:
    """SQLite predicate for literal Unicode substring search over visible text.

    JSON is decoded before searching so escaped Unicode, quotes and nested values
    match the text users see. This is list filtering, not semantic retrieval.
    """
    decoded: object = value or ""
    if json_encoded:
        try:
            decoded = json.loads(value or "null")
        except (ValueError, TypeError, RecursionError):
            pass
    pending = [decoded]
    while pending:
        part = pending.pop()
        if isinstance(part, dict):
            pending.extend(part.keys())
            pending.extend(part.values())
        elif isinstance(part, list):
            pending.extend(part)
        else:
            text = part if isinstance(part, str) else json.dumps(part, ensure_ascii=False)
            if query in unicodedata.normalize("NFKC", text).casefold():
                return 1
    return 0


#: Codepoint ranges of scripts that spend enough meaning per character for a
#: TWO-character token to be an ordinary whole word: kana, Han (+ extension A
#: and the compatibility block) and Hangul syllables. Latin is deliberately
#: absent -- a two-letter English token is a function word ("to", "in", "is"),
#: and those are exactly what the keyword floor below exists to drop.
_DENSE_SCRIPT_RANGES = (
    (0x3040, 0x30FF),  # Hiragana + Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xAC00, 0xD7A3),  # Hangul syllables
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
)


_MMR_LAMBDA = 0.6  # relevance vs diversity tradeoff (higher = more relevance)


# Recall-safe upper bound on the MMR candidate pool. This is NOT a perf cap that
# changes results — it only guards against pathological pool sizes (a vector search
# returning thousands of rows) so the rerank can't blow up unbounded. It sits far
# above any realistic episodic-recall pool, so in practice MMR reranks the full
# candidate set. The real cost reduction comes from memoizing the query-independent
# pairwise Jaccard inside _mmr_rerank (see comment there), not from shrinking the pool.
_MMR_MAX_POOL = 1000


_SEMANTIC_VECTOR_WEIGHT = 0.6  # weight for vector score in hybrid semantic retrieval


_SEMANTIC_KEYWORD_WEIGHT = 0.4  # weight for keyword score in hybrid semantic retrieval


def _keyword_score(raw_overlap: int) -> float:
    """Normalize a raw keyword-overlap count to [0, 1]."""
    return min(raw_overlap / 10.0, 1.0) if raw_overlap > 0 else 0.0


def _hybrid_score(keyword: float, vector: float, *, query_has_vector: bool = False) -> float:
    """Merge keyword and vector scores, degrading to keyword-only without a vector.

    Shared by every hybrid retrieval path so the weighting cannot drift between
    them; each caller still chooses which text it matches and where its vector
    comes from, because those differ legitimately.

    ``query_has_vector`` distinguishes the two ways ``vector`` can be 0: when
    the QUERY has no embedding the whole request degrades to keyword-only and
    every row keeps the unweighted keyword score (uniform, comparable). When
    the query IS embedded but this ROW has no stored vector, the caller passes
    ``query_has_vector=True`` so the row scores on the same 0.6/0.4 scale as
    its embedded siblings — otherwise a vectorless row with keyword overlap k
    scores k while an embedded row with the same overlap scores at most
    0.6·cos + 0.4·k, and rows the backfill has not reached yet systematically
    outrank freshly embedded ones.
    """
    if vector > 0 or query_has_vector:
        return _SEMANTIC_VECTOR_WEIGHT * vector + _SEMANTIC_KEYWORD_WEIGHT * keyword
    return keyword


# snowballstemmer's pure-Python stemmers keep the word being stemmed as
# mutable instance state (set_current() -> _stem() -> get_current()), so a
# single shared instance is NOT thread-safe: concurrent context builds
# (parallel subagent spawns via run_in_embed_pool) interleave their cursor
# state and crash with IndexError("string index out of range") — or silently
# return the wrong stem. One instance per thread; construction is trivial
# (~0.1 µs once the language module is imported).
_snowball_local = threading.local()


def _get_snowball():
    stemmer = getattr(_snowball_local, "stemmer", None)
    if stemmer is None:
        stemmer = _snowball_stemmer("english")
        _snowball_local.stemmer = stemmer
    return stemmer


# The same words recur across many entries, so stemming per occurrence repeats
# work that depends only on the word. Memoize on the word: one stem per distinct
# word for the life of the process rather than one per occurrence per retrieval.
# The win grows with the store, which only ever appends.
#
# The cache holds the resulting STRING, never the stemmer. The stemmer itself
# must stay thread-local (see above) because it carries mutable cursor state;
# caching its output is safe because stemming is deterministic per word.
_STEM_CACHE_SIZE = 100_000


@functools.lru_cache(maxsize=_STEM_CACHE_SIZE)
def _stem_one(word: str) -> str:
    """Return the Snowball stem of *word*, memoized per distinct word."""
    return str(_get_snowball().stemWords([word])[0])


def _stem_words(words: set[str]) -> set[str]:
    """Stem a set of words, returning both original and stemmed forms."""
    return words | {_stem_one(word) for word in words}


# Tokenizing + stemming a STORED row depends only on that row's own text, yet
# hybrid retrieval re-derives it for every row on every query — and again from
# scratch after a gateway restart. Memoizing per word (above) removes the
# stemmer call but not the regex scan, the set build, or the set union, which
# together are the majority of a warm hybrid semantic retrieval.
#
# Keyed on the text itself, not on a row key or rowid: a row whose value changes
# hashes to a DIFFERENT entry, so a stale token set can never be served for text
# absent from the row, and there is no invalidation step for a write path
# (upsert, dashboard delete, import, migration) to forget. Module level rather
# than per-store for the same reason it is safe: the result is a pure function of
# the text, so two stores holding the same text share one entry instead of each
# paying for its own.
#
# ONLY the row side belongs here. Query text has one distinct value per user
# message, so caching it would evict the bounded row population this exists to
# keep while never being read twice — an unbounded log of user prompts. The query
# side is derived once per call, outside the row loop, and thrown away.
#
# Bounded because the keys ARE user content. A stored value is capped at
# _MAX_VALUE_BYTES and a key at _MAX_KEY_LEN, so an entry's retained text is
# bounded, and a full pass over N rows touches at most 2N entries (one for the
# key, one for the value).
#
# The bound is in ENTRIES, so it does not bound bytes: an entry retains the text
# plus the frozenset of its words and stems, and the frozenset dominates.
# Measured retention per entry — ~2.3 KiB for a 120-char value, ~39 KiB for a
# _MAX_VALUE_BYTES value of 12-char words, ~76 KiB for one of 4-char words — so a
# filled cache spans ~9 MiB to ~296 MiB depending on the population, held for the
# process's life. Size it against that ceiling, not against the entry count.
#
# A bound BELOW the scan width is worse than no cache at all: a repeated full-table
# scan is LRU's worst case, so once 2N exceeds the bound every access evicts the
# entry the next one needs and the hit rate is not merely degraded but exactly
# zero, leaving only the wrapper cost and the retention. Measured: 4,096 hits and
# 4,096 misses at 2,048 rows, then 0 hits and 10,000 misses at 2,500. Nothing caps
# `semantic_memory`, so a store crosses that width on its own — which is why the
# scan checks its own width against the bound rather than trusting it.
_ROW_STEM_CACHE_SIZE = 4_096


def _row_stem_tokens_uncached(text: str) -> frozenset[str]:
    """Word + stem tokens of a stored row's *text*.

    The caller still owns case folding, because the key and value sides fold
    differently.
    """
    return frozenset(_stem_words(set(re.findall(r"\w+", text))))


#: Memoized on the text itself, so a row whose value changes hashes to a different
#: entry and no write path owns an invalidation step.
_row_stem_tokens = functools.lru_cache(maxsize=_ROW_STEM_CACHE_SIZE)(_row_stem_tokens_uncached)


def _row_stem_tokens_for_scan(entries_touched: int) -> Callable[[str], frozenset[str]]:
    """The row-side tokenizer for a pass that will touch *entries_touched* entries.

    Returns the memoized form only when the whole pass fits the cache. Past that
    width the memo cannot hit at all (see ``_ROW_STEM_CACHE_SIZE``), so serving the
    uncached function is strictly cheaper than paying the wrapper and retaining
    entries nothing will read.

    The count is the caller's to compute because arity differs: the semantic scan
    tokenizes a key AND a value per row, while lesson ranking tokenizes one text.

    The width is read from ``kiro_crew.vector_memory`` at call time: that binding
    is the patch seam callers and tests set, while the memo's own ``maxsize`` is
    fixed when this module is imported.
    """
    from kiro_crew import vector_memory  # circular import: the facade imports this module

    if entries_touched > vector_memory._ROW_STEM_CACHE_SIZE:
        return _row_stem_tokens_uncached
    return _row_stem_tokens


def _tokenize(text: str) -> set[str]:
    """Extract lowercase word tokens for Jaccard similarity."""
    return set(re.findall(r"\w+", text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _mmr_rerank(
    candidates: list[dict],
    text_key: str = "text",
    score_key: str = "score",
    limit: int = 6,
    lam: float = _MMR_LAMBDA,
) -> list[dict]:
    """Maximal Marginal Relevance reranking for diversity.

    Greedily selects items that balance relevance (score) with diversity
    (low Jaccard similarity to already-selected items).
    """
    if len(candidates) <= 1:
        return candidates[:limit]

    # Keep the FULL candidate pool so MMR can still surface a relevant-but-diverse item
    # that ranked below the top-`limit` on pure relevance — that tail pick is the whole
    # point of MMR, and truncating the pool toward `limit` would silently drop it. The
    # only bound is a recall-safe ceiling (_MMR_MAX_POOL) far above any realistic pool,
    # purely to cap pathological inputs; it keeps the highest-relevance rows if hit.
    if len(candidates) > _MMR_MAX_POOL:
        # heapq.nlargest is O(n log k) and avoids materializing a fully-sorted list,
        # vs sorted(...)[:k] which is O(n log n). Only matters on the pathological
        # >1000-candidate path, but it's the cheaper primitive for "top-k".
        candidates = heapq.nlargest(_MMR_MAX_POOL, candidates, key=lambda c: c[score_key])

    # Normalize scores to [0, 1]. Scores can be NEGATIVE: they derive from cosine
    # similarity (faiss.IndexFlatIP / dot product of normalized vectors, range [-1, 1])
    # times positive factors, so a query dissimilar to every candidate yields an
    # all-negative set. A bare `or 1.0` only guards max_score == 0; a negative
    # max_score would make `score / max_score` GROW as the true score worsens,
    # inverting the ranking. Divide by 1.0 whenever the max is non-positive so the
    # natural score order is preserved.
    max_score = max(c[score_key] for c in candidates)
    if max_score <= 0:
        max_score = 1.0
    token_cache = [_tokenize(c.get(text_key, "")) for c in candidates]

    # The cost driver is the diversity term: each MMR iteration recomputes
    # _jaccard(idx, s) for every remaining idx against every already-selected s. But
    # candidate↔candidate Jaccard is QUERY-INDEPENDENT — it depends only on the two
    # token sets, not the request — and the same (idx, s) pair recurs across iterations.
    # Memoize it by unordered index-pair so each pair is computed at most once. This
    # collapses the repeated set-intersection work (the profiler hot spot) while
    # preserving the full pool, so recall is unchanged. (Per-pair MinHash/LSH or a
    # cross-request id-pair cache is a possible further optimization if the pool grows.)
    sim_cache: dict[tuple[int, int], float] = {}

    def _pair_sim(i: int, j: int) -> float:
        key = (i, j) if i < j else (j, i)
        cached = sim_cache.get(key)
        if cached is None:
            cached = _jaccard(token_cache[i], token_cache[j])
            sim_cache[key] = cached
        return cached

    selected: list[int] = []
    remaining = set(range(len(candidates)))

    for _ in range(min(limit, len(candidates))):
        best_idx = -1
        # Initialize to -inf, not -1.0: with negative scores (see the max_score guard
        # above) relevance is negative, so an MMR value of 0.6*relevance - 0.4*max_sim
        # can reach or fall below -1.0 (e.g. relevance=-1, max_sim=1 -> mmr=-1.0). A
        # -1.0 floor with strict `>` would then select nothing, hit `best_idx < 0`, and
        # break early — silently returning fewer results than `limit`.
        best_mmr = -float("inf")
        for idx in remaining:
            relevance = candidates[idx][score_key] / max_score
            if selected:
                max_sim = max(_pair_sim(idx, s) for s in selected)
            else:
                max_sim = 0.0
            mmr = lam * relevance - (1 - lam) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = idx
        if best_idx < 0:
            break
        selected.append(best_idx)
        remaining.discard(best_idx)

    return [candidates[i] for i in selected]


def _is_selective_keyword(word: str) -> bool:
    """Whether *word* is selective enough to spend a ``LIKE '%word%'`` scan on.

    The episodic keyword fallback matches by plain substring, so the only thing
    a term has to earn is selectivity. Counting characters is a fine proxy for
    that in Latin script -- a one- or two-character token there is a function
    word, and ``LIKE '%to%'`` matches nearly every row -- but it is the wrong
    proxy for the scripts in :data:`_DENSE_SCRIPT_RANGES`, where two characters
    is an ordinary word (``模型`` "model", ``会議`` "meeting", ``회의``
    "meeting") and the substring is highly selective. Applying the Latin floor
    to them emptied the term list, and an empty term list makes the fallback
    return nothing at all rather than merely ranking differently.

    A single character stays refused in every script: one Han character (``的``,
    ``人``) is as unselective as an English stopword, so admitting it would
    trade this recall bug for a precision one.
    """
    if len(word) > 2:
        return True
    return len(word) == 2 and any(
        any(lo <= ord(ch) <= hi for lo, hi in _DENSE_SCRIPT_RANGES) for ch in word
    )
