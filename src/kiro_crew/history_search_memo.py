"""What every search over one transcript directory shares: its memos and running searches.

The byte budgets of the log's fold and snippet caches; the content-addressed count and
snippet memos, their byte ceilings, and the pair of them that every
``SessionCatalogProjection`` over one directory shares; the single-flight table, through
which callers asking the same unrestricted query wait for the search already running
instead of repeating it; and the memo-gated counter that scores a session the index
vouches for without reading its document when every answer is already held.

:mod:`kiro_crew.history_search` re-exports every name defined here and is the import path
callers use. A ceiling patched through it reaches :func:`_memos_for` here, because the
facade forwards the write to every member that binds the name
(:mod:`kiro_crew._facade_mirror`).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from typing import TYPE_CHECKING

from kiro_crew.history_cache import _GroupRecencyMemo
from kiro_crew.history_index import IndexRow
from kiro_crew.history_search_query import SearchNeedle, count_needle

if TYPE_CHECKING:
    from kiro_crew.history_search import SessionCatalogProjection


# Hard ceilings on the memory the two search memos may hold, in real retained
# bytes as reported by ``str.__sizeof__`` — NOT in characters.
#
# ``len()`` is the wrong unit and dangerously so: CPython stores a ``str`` in the
# narrowest width its contents allow, so one character is 1 byte for latin-1, 2
# for the BMP, and 4 for astral planes. A ceiling of 160 MB counted in characters
# retains 168 MB of ASCII, 336 MB of CJK, or 671 MB of emoji — and CJK is the
# ordinary case for a non-English corpus, not a pathological one. The sizers
# therefore call ``__sizeof__`` per string, and the snippet memo also charges for
# its list container.
#
# Why bytes and not an entry count: a session is read up to
# ``history._SESSION_MAX_BYTES`` (10 MB), so ``_SEARCH_SCAN_WINDOW`` entries is
# anywhere from a few MB to ~5 GB depending on the corpus. An entry count therefore
# bounds nothing that matters; it only *looked* safe because real sessions are
# small (a 171 MB / 230-session corpus folds to ~8 MB).
#
# The two are separate rather than one shared pool so a corpus that blows the
# snippet budget cannot starve the fold, which is the one that keeps matching
# off the critical path. Their sum is the ceiling to reason about.
_SEARCH_FOLD_BUDGET_BYTES = 96 * 1024 * 1024
_SEARCH_SNIPPET_BUDGET_BYTES = 64 * 1024 * 1024

#: Byte ceilings for the content-addressed memos below, charged per entry by
#: :func:`_memo_entry_cost`. ``0`` turns a memo off. Read when a
#: :class:`SessionCatalogProjection` is built.
_SEARCH_COUNT_MEMO_BYTES = 8 * 1024 * 1024
_SEARCH_SNIPPET_MEMO_BYTES = 2 * 1024 * 1024

#: Fixed per-entry overhead charged on top of the key and value strings: the
#: value tuple, the int, and the dict slot. Set so a full memo's charge is at
#: least what tracemalloc measures it holding.
_MEMO_ENTRY_OVERHEAD = 130

#: Hex characters of a memo key that name the session's content digest.
_DIGEST_HEX_CHARS = 32

#: A memo value: the memoized result and the bytes its entry is charged.
_MemoValue = tuple[object, int]


def _memo_entry_cost(key: str, value: object) -> int:
    extra = value.__sizeof__() if isinstance(value, str) else 0
    return _MEMO_ENTRY_OVERHEAD + key.__sizeof__() + extra


def _memo_group(key: str) -> str:
    """The query term a memo key names: everything after the content digest."""
    return key[_DIGEST_HEX_CHARS:]


def _new_memo(max_bytes: int, label: str) -> "_GroupRecencyMemo[_MemoValue] | None":
    """A content-addressed memo that admits by recency, or ``None`` when off.

    Its eviction unit is a query term across every session (see
    :class:`_GroupRecencyMemo`): a search walks its window in the same order
    every query, so evicting single entries by LRU would make a query evict the
    entry it reads next.
    """
    if max_bytes <= 0:
        return None
    return _GroupRecencyMemo(max_bytes, lambda v: v[1], _memo_group, label)


#: Transcript directories whose memos stay alive with no projection holding
#: them, most recently used last. A memo is keyed by content digest, so every
#: ``ConversationLog`` over one directory can share one: a log built per call,
#: as a tool call does, then finds the answers earlier calls computed.
_SHARED_MEMO_DIRS = 4
_MemoPair = tuple["_GroupRecencyMemo[_MemoValue] | None", "_GroupRecencyMemo[_MemoValue] | None"]
_shared_memos: "OrderedDict[tuple[str, int, int], _MemoPair]" = OrderedDict()
_shared_memos_lock = threading.Lock()


def _memos_for(directory: str) -> _MemoPair:
    """The count and snippet memos every projection over *directory* shares.

    The ceilings are part of the key so a changed ceiling takes effect for the
    next projection built, as it did when each projection built its own.
    """
    slot = (directory, _SEARCH_COUNT_MEMO_BYTES, _SEARCH_SNIPPET_MEMO_BYTES)
    with _shared_memos_lock:
        memos = _shared_memos.get(slot)
        if memos is None:
            memos = (
                _new_memo(_SEARCH_COUNT_MEMO_BYTES, "count"),
                _new_memo(_SEARCH_SNIPPET_MEMO_BYTES, "snippet"),
            )
            _shared_memos[slot] = memos
            while len(_shared_memos) > _SHARED_MEMO_DIRS:
                _shared_memos.popitem(last=False)
        else:
            _shared_memos.move_to_end(slot)
        return memos


def _pinned(
    memo: "_GroupRecencyMemo[_MemoValue] | None", groups: Iterable[str]
) -> AbstractContextManager[None]:
    """``memo.pinned(groups)``, or a no-op for a memo that is switched off."""
    return nullcontext() if memo is None else memo.pinned(groups)


class _Flight:
    """One unrestricted search in progress, for callers asking the same thing."""

    __slots__ = ("epoch", "done", "result")

    def __init__(self, epoch: int) -> None:
        self.epoch = epoch
        self.done = threading.Event()
        self.result: list[dict] | None = None


_flights: dict[tuple[str, str, int], _Flight] = {}
_flights_lock = threading.Lock()


class _LiveDigests:
    """Memo keys whose content digest is in *digests*, for ``retain``."""

    __slots__ = ("_digests",)

    def __init__(self, digests: set[str]) -> None:
        self._digests = digests

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and key[:_DIGEST_HEX_CHARS] in self._digests


class _ContentCounts:
    """Needle counts over one session's folded content, memoized by content digest.

    For a session the index vouches for, every answer the query may need is
    looked up under the row's digest first. When all are memoized the session
    is scored without reading its document. Otherwise the document is read at
    once, and only if the row still carries that digest, before any memoized
    answer is used, so memoized and computed answers describe one version of
    the session and a computed answer is filed only under the digest of the
    text it came from. Every other session folds through ``_folded_for``,
    exactly as the scan path did, and memoizes nothing.
    """

    __slots__ = ("_digest", "_memo", "_hits", "_loaded", "doc_chars", "_folded")

    def __init__(
        self,
        projection: "SessionCatalogProjection",
        key: str,
        rowids: "dict[str, IndexRow]",
        gate: "_MemoGate",
        title_folded: str,
    ) -> None:
        memo = projection._count_memo
        row = rowids.get(key)
        self._memo: "_GroupRecencyMemo[_MemoValue] | None" = None
        self._hits: dict[str, int] = {}
        self._digest = ""
        self._folded = ""
        self._loaded = True
        if row is not None and memo is not None:
            hits: dict[str, int] = {}
            for memo_key in gate.memo_keys:
                hit = memo.get(row.digest + memo_key)
                if hit is not None:
                    hits[memo_key] = hit[0]  # type: ignore[assignment]
            if gate.answered_by(hits, title_folded):
                self._hits = hits
                self._loaded = False
                self.doc_chars = row.doc_chars
                return
            doc = projection.search_index.document(row.rowid, key, row.digest)
            if doc is not None:
                self._memo = memo
                self._hits = hits
                self._digest = row.digest
                self.doc_chars, self._folded = doc
                return
        self.doc_chars, self._folded = projection._folded_for(key, rowids)

    def count(self, needle: SearchNeedle, needle_key: str) -> int:
        """Occurrences of *needle*; *needle_key* is :func:`_needle_memo_key` of it."""
        hit = self._hits.get(needle_key)
        if hit is not None:
            return hit
        if not self._loaded:
            raise RuntimeError("the memo gate let scoring reach an unanswered needle")
        value = count_needle(needle, self._folded)
        self._remember(needle_key, value)
        return value

    def count_phrase(self, phrase: str, phrase_key: str) -> int:
        """Occurrences of *phrase*; *phrase_key* is :func:`_phrase_memo_key` of it."""
        hit = self._hits.get(phrase_key)
        if hit is not None:
            return hit
        if not self._loaded:
            raise RuntimeError("the memo gate let scoring reach an unanswered phrase")
        value = self._folded.count(phrase) if self._folded else 0
        self._remember(phrase_key, value)
        return value

    def _remember(self, memo_key: str, value: int) -> None:
        if self._memo is not None:
            mkey = self._digest + memo_key
            self._memo[mkey] = (value, _memo_entry_cost(mkey, value))


class _MemoGate:
    """The complete set of count-memo keys one query may need.

    The document may be skipped only when every answer is already held.
    Session disqualification belongs to the scoring loop, not this gate.
    """

    __slots__ = ("needle_keys", "phrase_key", "memo_keys", "multi")

    def __init__(self, needles: Sequence[SearchNeedle], phrase: str, multi: bool) -> None:
        self.multi = multi
        self.needle_keys = [_needle_memo_key(n) for n in needles]
        self.phrase_key = _phrase_memo_key(phrase)
        keys = [*self.needle_keys, self.phrase_key] if multi else self.needle_keys
        self.memo_keys = list(dict.fromkeys(keys))

    def answered_by(self, hits: dict[str, int], _title_folded: str) -> bool:
        return all(key in hits for key in self.memo_keys)


def _needle_memo_key(needle: SearchNeedle) -> str:
    """The part of a count-memo key after the digest: what ``count_needle`` reads."""
    return repr(("needle", needle.text, needle.alts, needle.digit_bounded))


def _phrase_memo_key(phrase: str) -> str:
    return repr(("phrase", phrase))
